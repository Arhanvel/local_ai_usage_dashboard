"""Incremental ingest of ~/.claude/projects/*/*.jsonl transcripts.

Transcripts are append-only, so we remember the byte offset reached on the
previous run and only decode bytes added since. A file that shrank or whose
size no longer matches the recorded state is re-read from the start.
"""
import json
import os
from datetime import datetime, timezone

from . import config
from .config import friendly_project, local_parts, parse_ts

SOURCE = "claude"

# Tool-result payload keys that hold the human-visible text, longest first.
_RESULT_TEXT_KEYS = ("stdout", "content", "result", "text")


class Pricer:
    def __init__(self, pricing: dict):
        self.models = pricing.get("models", {})
        self.default = pricing.get("default", {"input": 0.0, "output": 0.0, "confidence": "fallback"})
        cache = pricing.get("cache", {})
        self.w5 = cache.get("write_5m_multiplier", 1.25)
        self.w1h = cache.get("write_1h_multiplier", 2.0)
        self.read_mult = cache.get("read_multiplier", 0.1)
        self.seen = {}

    def cost(self, model, inp, out, eph5, eph1h, cache_write, cache_read):
        entry = self.models.get(model) or self.default
        self.seen[model] = entry.get("confidence", "fallback")
        pin = entry.get("input", 0.0) / 1_000_000.0
        pout = entry.get("output", 0.0) / 1_000_000.0
        # If the split isn't reported, treat all cache writes as 5m.
        if not eph5 and not eph1h and cache_write:
            eph5 = cache_write
        return (
            inp * pin
            + out * pout
            + eph5 * pin * self.w5
            + eph1h * pin * self.w1h
            + cache_read * pin * self.read_mult
        )


def _iter_new_lines(path, start_offset):
    """Yield (json_object, new_offset). Never consumes a trailing partial line."""
    with open(path, "rb") as fh:
        fh.seek(start_offset)
        blob = fh.read()
    if not blob:
        return
    consumed = 0
    parts = blob.split(b"\n")
    # A final chunk without a newline means the writer is mid-append.
    tail_complete = blob.endswith(b"\n")
    if not tail_complete:
        parts = parts[:-1] if len(parts) > 1 else []
    for raw in parts:
        consumed += len(raw) + 1
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw.decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError):
            continue
        yield obj, start_offset + consumed


def _ext_of(path):
    if not path:
        return None
    base = path.replace("\\", "/").rsplit("/", 1)[-1]
    if "." not in base:
        return None
    return "." + base.rsplit(".", 1)[-1].lower()


def _patch_line_counts(structured_patch):
    added = removed = 0
    if isinstance(structured_patch, list):
        for hunk in structured_patch:
            if not isinstance(hunk, dict):
                continue
            for line in hunk.get("lines") or []:
                if not isinstance(line, str) or not line:
                    continue
                if line[0] == "+":
                    added += 1
                elif line[0] == "-":
                    removed += 1
    return added, removed


def _result_text_len(payload):
    if isinstance(payload, str):
        return len(payload)
    total = 0
    if isinstance(payload, dict):
        for key in _RESULT_TEXT_KEYS:
            val = payload.get(key)
            if isinstance(val, str):
                total += len(val)
            elif isinstance(val, list):
                total += sum(len(b.get("text", "")) for b in val if isinstance(b, dict))
    elif isinstance(payload, list):
        for block in payload:
            if isinstance(block, dict):
                total += len(block.get("text", "") or "")
            elif isinstance(block, str):
                total += len(block)
    return total


def _bash_program(command):
    if not command or not isinstance(command, str):
        return None
    head = command.strip().split("\n", 1)[0].lstrip("(& ")
    for token in head.split():
        if "=" in token and not token.startswith("-"):
            continue  # VAR=x prefix
        name = token.strip("\"'").replace("\\", "/").rsplit("/", 1)[-1]
        return name[:40] or None
    return None


def classify_transcript(jsonl_path, project_root):
    """(kind, workflow_id) for a transcript, from its path under the project dir.

    projects/<proj>/<session>.jsonl                                  -> main
    projects/<proj>/<session>/subagents/agent-*.jsonl                -> subagent
    projects/<proj>/<session>/subagents/workflows/<wf>/agent-*.jsonl -> workflow
    """
    try:
        rel = jsonl_path.relative_to(project_root)
    except ValueError:
        return "main", None
    parts = rel.parts
    if len(parts) <= 1:
        return "main", None
    if "workflows" in parts:
        idx = parts.index("workflows")
        wf = parts[idx + 1] if len(parts) > idx + 1 else None
        return "workflow", wf
    if "subagents" in parts:
        return "subagent", None
    return "main", None


class ClaudeIngester:
    def __init__(self, con, pricer, verbose=True):
        self.con = con
        self.pricer = pricer
        self.verbose = verbose
        self.rows = 0
        self.sessions = {}
        self.titles = {}
        self.transcript = "main"
        self.workflow_id = None

    # -- accumulation helpers -------------------------------------------------
    def _touch_session(self, rec, project, project_dir):
        sid = rec.get("sessionId") or rec.get("session_id")
        if not sid:
            return None
        ts = rec.get("timestamp")
        s = self.sessions.setdefault(sid, {
            "session_id": sid, "project": project, "project_dir": project_dir,
            "cwd": rec.get("cwd"), "slug": None, "ai_title": None,
            "git_branch": None, "version": None, "entrypoint": None,
            "first_ts": None, "last_ts": None,
        })
        for key, field in (("slug", "slug"), ("gitBranch", "git_branch"),
                           ("version", "version"), ("entrypoint", "entrypoint"),
                           ("cwd", "cwd")):
            val = rec.get(key)
            if val and not s[field]:
                s[field] = val
        if ts:
            if not s["first_ts"] or ts < s["first_ts"]:
                s["first_ts"] = ts
            if not s["last_ts"] or ts > s["last_ts"]:
                s["last_ts"] = ts
        return sid

    # -- per-record dispatch --------------------------------------------------
    def handle(self, rec, project, project_dir):
        rtype = rec.get("type")
        sid = self._touch_session(rec, project, project_dir)

        if rtype == "ai-title":
            if sid:
                self.titles[sid] = rec.get("aiTitle")
            return
        if rtype == "assistant":
            self._assistant(rec, sid, project)
        elif rtype == "user":
            self._user(rec, sid, project)
        elif rtype == "system":
            self._system(rec, sid, project)
        elif rtype == "attachment":
            self._attachment(rec, sid, project)
        elif rtype == "queue-operation":
            op = rec.get("operation") or {}
            self._event(rec, sid, project, "queue", op.get("type") if isinstance(op, dict) else str(op), None)

    def _event(self, rec, sid, project, kind, subkind, detail):
        uuid = rec.get("uuid") or f"{sid}:{kind}:{self.rows}"
        _iso, date, _h, _d = local_parts(parse_ts(rec.get("timestamp")))
        self.con.execute(
            "INSERT OR REPLACE INTO cc_event (uuid,session_id,project,ts,date,kind,subkind,detail)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (uuid, sid, project, rec.get("timestamp"), date, kind, subkind,
             (detail or "")[:500] if detail else None),
        )
        self.rows += 1

    def _attachment(self, rec, sid, project):
        att = rec.get("attachment")
        if isinstance(att, dict):
            self._event(rec, sid, project, "attachment", att.get("type"), None)

    def _system(self, rec, sid, project):
        sub = rec.get("subtype")
        if sub == "turn_duration":
            _iso, date, _h, _d = local_parts(parse_ts(rec.get("timestamp")))
            self.con.execute(
                "INSERT OR REPLACE INTO cc_turn (uuid,session_id,project,ts,date,duration_ms,message_count)"
                " VALUES (?,?,?,?,?,?,?)",
                (rec.get("uuid"), sid, project, rec.get("timestamp"), date,
                 rec.get("durationMs"), rec.get("messageCount")),
            )
            self.rows += 1
            return
        detail = rec.get("content") if isinstance(rec.get("content"), str) else None
        self._event(rec, sid, project, "system", sub, detail)

    def _assistant(self, rec, sid, project):
        msg = rec.get("message") or {}
        usage = msg.get("usage") or {}
        dt = parse_ts(rec.get("timestamp"))
        iso, date, hour, dow = local_parts(dt)

        inp = usage.get("input_tokens") or 0
        out = usage.get("output_tokens") or 0
        cw = usage.get("cache_creation_input_tokens") or 0
        cr = usage.get("cache_read_input_tokens") or 0
        creation = usage.get("cache_creation") or {}
        eph5 = creation.get("ephemeral_5m_input_tokens") or 0
        eph1h = creation.get("ephemeral_1h_input_tokens") or 0
        server = usage.get("server_tool_use") or {}

        model = msg.get("model")
        cost = self.pricer.cost(model, inp, out, eph5, eph1h, cw, cr)

        think_blocks = think_chars = text_chars = tool_uses = 0
        content = msg.get("content")
        blocks = content if isinstance(content, list) else []
        for blk in blocks:
            if not isinstance(blk, dict):
                continue
            bt = blk.get("type")
            if bt == "thinking":
                think_blocks += 1
                think_chars += len(blk.get("thinking") or "")
            elif bt == "text":
                text_chars += len(blk.get("text") or "")
            elif bt == "tool_use":
                tool_uses += 1

        uuid = rec.get("uuid")
        self.con.execute(
            "INSERT OR REPLACE INTO cc_message (uuid,session_id,parent_uuid,ts,date,hour,dow,type,role,"
            "model,is_sidechain,effort,request_id,message_id,input_tokens,output_tokens,cache_write,cache_read,"
            "eph_5m,eph_1h,service_tier,speed,stop_reason,thinking_blocks,thinking_chars,text_chars,"
            "tool_uses,web_search_reqs,web_fetch_reqs,is_api_error,api_error_status,cost_usd,skill,"
            "agent_id,agent_type,transcript,workflow_id,"
            "project,cwd,git_branch,version) VALUES (" + ",".join("?" * 41) + ")",
            (uuid, sid, rec.get("parentUuid"), rec.get("timestamp"), date, hour, dow, "assistant",
             msg.get("role"), model, 1 if rec.get("isSidechain") else 0, rec.get("effort"),
             rec.get("requestId"), msg.get("id"), inp, out, cw, cr, eph5, eph1h,
             usage.get("service_tier"), usage.get("speed"), msg.get("stop_reason"),
             think_blocks, think_chars, text_chars, tool_uses,
             server.get("web_search_requests") or 0, server.get("web_fetch_requests") or 0,
             1 if rec.get("isApiErrorMessage") else 0, rec.get("apiErrorStatus"), cost,
             rec.get("attributionSkill"), rec.get("agentId"), rec.get("attributionAgent"),
             self.transcript, self.workflow_id,
             project, rec.get("cwd"), rec.get("gitBranch"), rec.get("version")),
        )
        self.rows += 1

        for blk in blocks:
            if isinstance(blk, dict) and blk.get("type") == "tool_use":
                self._tool_use(blk, rec, sid, project, date, hour)

    def _tool_use(self, blk, rec, sid, project, date, hour):
        tuid = blk.get("id")
        if not tuid:
            return
        name = blk.get("name") or "?"
        tool_input = blk.get("input") if isinstance(blk.get("input"), dict) else {}
        raw = json.dumps(tool_input, default=str)
        is_mcp = 1 if name.startswith("mcp__") else 0
        mcp_server = name.split("__")[1] if is_mcp and name.count("__") >= 2 else None

        file_path = tool_input.get("file_path") or tool_input.get("path") or tool_input.get("notebook_path")
        command = tool_input.get("command")
        self.con.execute(
            "INSERT OR REPLACE INTO cc_tool_call (tool_use_id,message_uuid,session_id,project,ts,date,hour,"
            "name,is_mcp,mcp_server,input_chars,input_json,skill,transcript,agent_id,"
            "file_path,file_ext,bash_command,bash_program)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tuid, rec.get("uuid"), sid, project, rec.get("timestamp"), date, hour, name,
             is_mcp, mcp_server, len(raw), raw[:4000], rec.get("attributionSkill"),
             self.transcript, rec.get("agentId"),
             file_path, _ext_of(file_path),
             (command or "")[:1000] if isinstance(command, str) else None,
             _bash_program(command)),
        )
        self.rows += 1

    def _user(self, rec, sid, project):
        msg = rec.get("message") or {}
        content = msg.get("content")
        dt = parse_ts(rec.get("timestamp"))
        iso, date, hour, dow = local_parts(dt)

        # A user record is either a typed prompt (string content) or the
        # carrier for tool results (list content with tool_result blocks).
        if isinstance(content, str):
            self._prompt(rec, sid, project, content, date, hour, dow)
            return

        if not isinstance(content, list):
            return
        tool_results = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
        if not tool_results:
            text = " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
            if text.strip():
                self._prompt(rec, sid, project, text, date, hour, dow)
            return

        payload = rec.get("toolUseResult")
        for block in tool_results:
            self._tool_result(rec, block, payload, sid, project, date)

    def _prompt(self, rec, sid, project, text, date, hour, dow):
        origin = rec.get("origin") or {}
        self.con.execute(
            "INSERT OR REPLACE INTO cc_prompt (uuid,session_id,project,ts,date,hour,dow,prompt_id,source,"
            "origin_kind,chars,words,preview) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rec.get("uuid"), sid, project, rec.get("timestamp"), date, hour, dow,
             rec.get("promptId"), rec.get("promptSource"),
             origin.get("kind") if isinstance(origin, dict) else None,
             len(text), len(text.split()), text[:300]),
        )
        self.rows += 1

    def _tool_result(self, rec, block, payload, sid, project, date):
        tuid = block.get("tool_use_id")
        if not tuid:
            return
        is_error = 1 if block.get("is_error") else 0
        interrupted = 0
        duration_ms = None
        added = removed = 0
        file_path = None
        result_chars = _result_text_len(payload) or _result_text_len(block.get("content"))

        if isinstance(payload, dict):
            interrupted = 1 if payload.get("interrupted") else 0
            for key in ("durationMs", "totalDurationMs"):
                if isinstance(payload.get(key), (int, float)):
                    duration_ms = int(payload[key])
                    break
            file_path = payload.get("filePath")
            added, removed = _patch_line_counts(payload.get("structuredPatch"))
            if not added and payload.get("type") == "create":
                created = payload.get("content")
                if isinstance(created, str):
                    added = created.count("\n") + 1
            if isinstance(payload.get("stderr"), str) and payload["stderr"].strip() and not result_chars:
                result_chars = len(payload["stderr"])
            self._maybe_subagent(rec, payload, tuid, sid, project, date)

        result_ts = rec.get("timestamp")
        latency_ms = None
        row = self.con.execute("SELECT ts FROM cc_tool_call WHERE tool_use_id=?", (tuid,)).fetchone()
        if row and row["ts"] and result_ts:
            a, b = parse_ts(row["ts"]), parse_ts(result_ts)
            if a and b:
                latency_ms = max(0, int((b - a).total_seconds() * 1000))

        self.con.execute(
            "UPDATE cc_tool_call SET has_result=1, is_error=?, interrupted=?, denial_kind=?, result_ts=?,"
            " latency_ms=?, duration_ms=?, result_chars=?, lines_added=?, lines_removed=?,"
            " file_path=COALESCE(file_path,?), file_ext=COALESCE(file_ext,?) WHERE tool_use_id=?",
            (is_error, interrupted, rec.get("toolDenialKind"), result_ts, latency_ms, duration_ms,
             result_chars, added, removed, file_path, _ext_of(file_path), tuid),
        )
        self.rows += 1

    def _maybe_subagent(self, rec, payload, tuid, sid, project, date):
        if "agentType" not in payload and "resolvedModel" not in payload:
            return
        self.con.execute(
            "INSERT OR REPLACE INTO cc_subagent (tool_use_id,session_id,project,ts,date,agent_type,"
            "resolved_model,status,total_tokens,total_duration_ms,tool_use_count)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (tuid, sid, project, rec.get("timestamp"), date, payload.get("agentType"),
             payload.get("resolvedModel"), payload.get("status"),
             payload.get("totalTokens") or 0, payload.get("totalDurationMs") or 0,
             payload.get("totalToolUseCount") or 0),
        )

    # -- flush ----------------------------------------------------------------
    def flush_sessions(self):
        for sid, s in self.sessions.items():
            title = self.titles.get(sid)
            self.con.execute(
                "INSERT INTO cc_session (session_id,project,project_dir,cwd,slug,ai_title,git_branch,"
                "version,entrypoint,first_ts,last_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(session_id) DO UPDATE SET"
                " project=excluded.project, project_dir=excluded.project_dir,"
                " cwd=COALESCE(excluded.cwd, cc_session.cwd),"
                " slug=COALESCE(excluded.slug, cc_session.slug),"
                " ai_title=COALESCE(excluded.ai_title, cc_session.ai_title),"
                " git_branch=COALESCE(excluded.git_branch, cc_session.git_branch),"
                " version=COALESCE(excluded.version, cc_session.version),"
                " entrypoint=COALESCE(excluded.entrypoint, cc_session.entrypoint),"
                " first_ts=MIN(COALESCE(cc_session.first_ts, excluded.first_ts), excluded.first_ts),"
                " last_ts=MAX(COALESCE(cc_session.last_ts, excluded.last_ts), excluded.last_ts)",
                (sid, s["project"], s["project_dir"], s["cwd"], s["slug"], title, s["git_branch"],
                 s["version"], s["entrypoint"], s["first_ts"], s["last_ts"]),
            )
        self.sessions.clear()
        self.titles.clear()


PROJECT_TABLES = ("cc_message", "cc_tool_call", "cc_prompt", "cc_turn", "cc_subagent", "cc_event")


def normalize_projects(con):
    """Re-label every row from its session's cwd.

    A transcript's opening lines (mode, permission-mode, bridge-session) carry
    no cwd, so rows written before the first real record would otherwise be
    labelled with the undecodable directory name - splitting one project across
    two labels. The session's cwd is authoritative, so apply it everywhere.
    """
    con.execute("CREATE TEMP TABLE IF NOT EXISTS _proj (session_id TEXT PRIMARY KEY, project TEXT)")
    con.execute("DELETE FROM _proj")
    con.executemany(
        "INSERT OR REPLACE INTO _proj (session_id, project) VALUES (?,?)",
        [(r["session_id"], friendly_project(r["cwd"], r["project_dir"]))
         for r in con.execute("SELECT session_id, cwd, project_dir FROM cc_session")
         if r["cwd"]])

    fixed = 0
    for table in PROJECT_TABLES:
        cur = con.execute(
            f"UPDATE {table} SET project = (SELECT p.project FROM _proj p"
            f"  WHERE p.session_id = {table}.session_id)"
            f" WHERE EXISTS (SELECT 1 FROM _proj p WHERE p.session_id = {table}.session_id"
            f"   AND ({table}.project IS NULL OR {table}.project <> p.project))")
        fixed += cur.rowcount or 0
    con.commit()
    return fixed


def dedupe_usage(con):
    """Keep each API request's token usage on exactly one row.

    A single assistant response is persisted as several transcript lines - one
    per content block (thinking, text, and each tool_use) - all sharing one
    message_id, and Claude Code stamps the *same* usage object on every line.
    Summing the token columns naively multiplies one billed API call by its
    block count (up to 22x in this data, ~3.5x on average).

    Dedup is keyed on message_id, the API's response identifier: exactly one
    usage block is billed per response. Verified directly - all lines of a
    duplicated group carry an identical usage tuple and disjoint content blocks.

    The content-level columns (thinking_blocks, tool_uses, text_chars) are left
    alone: those genuinely differ per row and should still sum. Only the token
    and cost columns are zeroed on the non-primary rows, so every existing
    aggregation becomes correct without needing a filter.
    """
    # Materialise the winning uuid per response first: a correlated subquery on
    # COALESCE(...) can't use an index and degrades to O(n^2) over ~44k rows.
    con.execute("DROP TABLE IF EXISTS temp._keep")
    con.execute("""
        CREATE TEMP TABLE _keep AS
        SELECT MIN(uuid) AS uuid FROM cc_message
        WHERE COALESCE(message_id, request_id) IS NOT NULL
        GROUP BY COALESCE(message_id, request_id)""")
    con.execute("CREATE UNIQUE INDEX temp.ix_keep ON _keep(uuid)")
    con.execute("""
        UPDATE cc_message SET usage_dupe = 1
        WHERE COALESCE(message_id, request_id) IS NOT NULL
          AND uuid NOT IN (SELECT uuid FROM _keep)""")
    cur = con.execute("""
        UPDATE cc_message
        SET input_tokens=0, output_tokens=0, cache_write=0, cache_read=0,
            eph_5m=0, eph_1h=0, cost_usd=0,
            web_search_reqs=0, web_fetch_reqs=0
        WHERE usage_dupe = 1
          AND (input_tokens<>0 OR output_tokens<>0 OR cache_write<>0
               OR cache_read<>0 OR cost_usd<>0)""")
    con.commit()
    return cur.rowcount or 0


def ingest_stats_cache(con):
    """Fold in ~/.claude/stats-cache.json.

    Claude Code prunes old transcripts but keeps this rollup, so it covers days
    that no longer exist as JSONL. Stored apart from cc_* so the two are never
    double-counted.
    """
    path = config.claude_home() / "stats-cache.json"
    if not path.exists():
        return 0
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return 0

    by_date = {}
    for row in data.get("dailyActivity") or []:
        if isinstance(row, dict) and row.get("date"):
            by_date[row["date"]] = {
                "messages": row.get("messageCount") or 0,
                "sessions": row.get("sessionCount") or 0,
                "tool_calls": row.get("toolCallCount") or 0,
                "tokens": 0, "models": None,
            }
    for row in data.get("dailyModelTokens") or []:
        if not isinstance(row, dict) or not row.get("date"):
            continue
        entry = by_date.setdefault(row["date"], {"messages": 0, "sessions": 0,
                                                "tool_calls": 0, "tokens": 0, "models": None})
        models = row.get("tokensByModel") or {}
        entry["models"] = json.dumps(models)
        entry["tokens"] = sum(v for v in models.values() if isinstance(v, (int, float)))

    n = 0
    for date, e in by_date.items():
        con.execute(
            "INSERT OR REPLACE INTO cc_legacy_daily (date,messages,sessions,tool_calls,tokens,models_json)"
            " VALUES (?,?,?,?,?,?)",
            (date, e["messages"], e["sessions"], e["tool_calls"], e["tokens"], e["models"]))
        n += 1

    for model, u in (data.get("modelUsage") or {}).items():
        if not isinstance(u, dict):
            continue
        con.execute(
            "INSERT OR REPLACE INTO cc_legacy_model (model,input_tokens,output_tokens,cache_read,"
            "cache_write,web_searches) VALUES (?,?,?,?,?,?)",
            (model, u.get("inputTokens") or 0, u.get("outputTokens") or 0,
             u.get("cacheReadInputTokens") or 0, u.get("cacheCreationInputTokens") or 0,
             u.get("webSearchRequests") or 0))

    for key in ("totalSessions", "totalMessages", "firstSessionDate", "lastComputedDate",
                "longestSession", "hourCounts"):
        if key in data:
            con.execute("INSERT OR REPLACE INTO cc_legacy_meta (key,value) VALUES (?,?)",
                        (key, json.dumps(data[key], default=str)))
    con.commit()
    return n


def run(con, full=False, verbose=True):
    """Ingest all Claude Code transcripts. Returns a summary dict."""
    projects = config.claude_projects_dir()
    started = datetime.now(timezone.utc).isoformat()
    if not projects.is_dir():
        return {"ok": False, "error": f"No Claude projects dir at {projects}"}

    pricer = Pricer(config.load_pricing())
    ing = ClaudeIngester(con, pricer, verbose)

    state = {(r["key"]): r for r in con.execute(
        "SELECT key,size,mtime,byte_offset FROM ingest_state WHERE source=?", (SOURCE,))}

    files_seen = files_read = 0
    for project_path in sorted(projects.iterdir()):
        if not project_path.is_dir():
            continue
        # rglob picks up nested subagent + workflow-agent transcripts, which
        # carry their own (billable) token usage.
        for jsonl in sorted(project_path.rglob("*.jsonl")):
            files_seen += 1
            key = str(jsonl)
            try:
                st = jsonl.stat()
            except OSError:
                continue
            prev = state.get(key)
            offset = 0
            if prev and not full:
                if prev["size"] == st.st_size and (prev["mtime"] or 0) >= st.st_mtime - 0.001:
                    continue  # untouched
                # Truncated or rewritten -> start over; otherwise resume.
                offset = prev["byte_offset"] if st.st_size >= (prev["size"] or 0) else 0

            files_read += 1
            project_dir = project_path.name
            ing.transcript, ing.workflow_id = classify_transcript(jsonl, project_path)
            project = None
            new_offset = offset
            count = 0
            for rec, off in _iter_new_lines(jsonl, offset):
                if project is None:
                    project = friendly_project(rec.get("cwd"), project_dir)
                ing.handle(rec, project or friendly_project(None, project_dir), project_dir)
                new_offset = off
                count += 1
            if project is None:
                project = friendly_project(None, project_dir)

            con.execute(
                "INSERT INTO ingest_state (source,key,size,mtime,byte_offset,rows,updated_at)"
                " VALUES (?,?,?,?,?,?,?) ON CONFLICT(source,key) DO UPDATE SET"
                " size=excluded.size, mtime=excluded.mtime, byte_offset=excluded.byte_offset,"
                " rows=ingest_state.rows+excluded.rows, updated_at=excluded.updated_at",
                (SOURCE, key, st.st_size, st.st_mtime, new_offset, count,
                 datetime.now(timezone.utc).isoformat()),
            )
            if files_read % 50 == 0:
                ing.flush_sessions()
                con.commit()
                if verbose:
                    print(f"  ... {files_read} files, {ing.rows} rows")

    ing.flush_sessions()
    con.commit()
    relabelled = normalize_projects(con)
    deduped = dedupe_usage(con)
    legacy_days = ingest_stats_cache(con)
    con.execute(
        "INSERT INTO ingest_run (started_at,finished_at,source,files_seen,files_read,rows_added,note)"
        " VALUES (?,?,?,?,?,?,?)",
        (started, datetime.now(timezone.utc).isoformat(), SOURCE, files_seen, files_read,
         ing.rows, "full" if full else "incremental"),
    )
    con.commit()
    return {
        "ok": True, "files_seen": files_seen, "files_read": files_read,
        "rows": ing.rows, "legacy_days": legacy_days, "relabelled": relabelled,
        "usage_deduped": deduped, "pricing_confidence": pricer.seen,
    }
