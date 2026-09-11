"""Incremental ingest of Cursor's local SQLite state.

Cursor keeps everything in %APPDATA%/Cursor/User/globalStorage:
  state.vscdb          composerHeaders (chat sessions), cursorDiskKV (message
                       'bubbles', checkpoints, diffs), ItemTable (aiCodeTrackingLines)
  conversation-search.db  conversation titles / FTS index

Note: Cursor does NOT record real token usage locally - `tokenCount` is zero on
almost every bubble because metering happens server-side. What is reliable:
message counts, tool calls, timings, lines added/removed and AI-authored lines.

Bubble keys are immutable once written, so incremental ingest = fetch the key
list (cheap), diff against what we already stored, and read only new values.
"""
import json
import os
import sqlite3
from datetime import datetime, timezone

from . import config
from .config import local_parts, parse_ts
from .patterns import ext_of as _ext_of

SOURCE = "cursor"
BUBBLE_ROLE = {1: "user", 2: "assistant"}
UNIFIED_MODE = {1: "chat", 2: "agent", 3: "edit"}


def _open_ro(path):
    """Open a possibly-live SQLite file read-only."""
    uri = "file:" + str(path).replace("\\", "/").replace("?", "%3f").replace("#", "%23") + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=20)
    con.row_factory = sqlite3.Row
    return con


def _loads(value):
    if not value:
        return None
    try:
        if isinstance(value, bytes):
            value = value.decode("utf-8", "replace")
        return json.loads(value)
    except (ValueError, UnicodeDecodeError):
        return None


def _project_from_repo(repo_path):
    if not repo_path:
        return None
    norm = str(repo_path).replace("\\", "/").rstrip("/")
    return norm.rsplit("/", 1)[-1] or norm


def workspace_folders(gs):
    """{workspaceId: folder path} from workspaceStorage/<id>/workspace.json.

    composerHeaders only records a workspace hash, and trackedGitRepos is often
    empty, so this is what actually resolves a Cursor chat to a project.
    """
    from urllib.parse import unquote

    out = {}
    ws_root = gs.parent / "workspaceStorage"
    if not ws_root.is_dir():
        return out
    for d in ws_root.iterdir():
        meta = d / "workspace.json"
        if not meta.is_file():
            continue
        try:
            with open(meta, "r", encoding="utf-8") as fh:
                folder = (json.load(fh) or {}).get("folder")
        except (OSError, ValueError):
            continue
        if not folder:
            continue
        path = unquote(str(folder))
        for prefix in ("file:///", "file://"):
            if path.startswith(prefix):
                path = path[len(prefix):]
                break
        out[d.name] = path.rstrip("/")
    return out


def _ingest_headers(con, src, ws_map=None):
    """composerHeaders -> cur_session. Small table; re-read every run."""
    ws_map = ws_map or {}
    added = 0
    try:
        rows = src.execute(
            "SELECT composerId, workspaceId, createdAt, lastUpdatedAt, isArchived, isSubagent, value"
            " FROM composerHeaders"
        ).fetchall()
    except sqlite3.Error:
        return 0

    for r in rows:
        v = _loads(r["value"]) or {}
        created = parse_ts(r["createdAt"] or v.get("createdAt"))
        updated = parse_ts(r["lastUpdatedAt"] or v.get("lastUpdatedAt") or v.get("conversationCheckpointLastUpdatedAt"))
        _iso, date, _h, _d = local_parts(created)

        repos = v.get("trackedGitRepos") or []
        repo_path = branch = None
        if isinstance(repos, list) and repos:
            first = repos[0] if isinstance(repos[0], dict) else {}
            repo_path = first.get("repoPath")
            branches = first.get("branches") or []
            if isinstance(branches, list) and branches and isinstance(branches[0], dict):
                branch = branches[0].get("branchName")

        sub = v.get("subagentInfo") or {}
        mode = v.get("unifiedMode")
        if isinstance(mode, int):
            mode = UNIFIED_MODE.get(mode, str(mode))

        # Prefer the tracked git repo; fall back to the workspace folder.
        project = _project_from_repo(repo_path) or _project_from_repo(ws_map.get(r["workspaceId"]))
        if not repo_path:
            repo_path = ws_map.get(r["workspaceId"])

        con.execute(
            "INSERT OR REPLACE INTO cur_session (composer_id,name,workspace_id,created_at,last_updated_at,"
            "date,mode,force_mode,is_archived,is_subagent,parent_composer,context_usage_pct,lines_added,"
            "lines_removed,files_changed,repo_path,project,branch,subtitle,message_count)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
            " COALESCE((SELECT message_count FROM cur_session WHERE composer_id=?),0))",
            (r["composerId"], v.get("name"), r["workspaceId"],
             created.isoformat() if created else None,
             updated.isoformat() if updated else None, date, mode, v.get("forceMode"),
             1 if r["isArchived"] else 0, 1 if r["isSubagent"] else 0,
             sub.get("parentComposerId") if isinstance(sub, dict) else None,
             v.get("contextUsagePercent"), v.get("totalLinesAdded") or 0,
             v.get("totalLinesRemoved") or 0, v.get("filesChangedCount") or 0,
             repo_path, project, branch,
             (v.get("subtitle") or "")[:300] or None, r["composerId"]),
        )
        added += 1
    return added


def _ingest_bubbles(con, src, full=False, verbose=True):
    """cursorDiskKV bubbleId:* -> cur_message, reading only unseen keys."""
    try:
        keys = [r[0] for r in src.execute(
            "SELECT key FROM cursorDiskKV WHERE key LIKE 'bubbleId:%'") if r[0]]
    except sqlite3.Error:
        return 0
    if full:
        known = set()
    else:
        known = {r[0] for r in con.execute("SELECT bubble_key FROM cur_message")}
    todo = [k for k in keys if k not in known]
    if verbose:
        print(f"  bubbles: {len(keys)} present, {len(todo)} new")

    added = 0
    for i in range(0, len(todo), 500):
        chunk = todo[i:i + 500]
        placeholders = ",".join("?" * len(chunk))
        rows = src.execute(
            f"SELECT key, value FROM cursorDiskKV WHERE key IN ({placeholders})", chunk).fetchall()
        for key, value in rows:
            o = _loads(value)
            if not isinstance(o, dict):
                continue
            parts = key.split(":")
            composer_id = parts[1] if len(parts) > 2 else None
            bubble_id = parts[2] if len(parts) > 2 else None

            tok = o.get("tokenCount") or {}
            tfd = o.get("toolFormerData") or {}
            timing = o.get("timingInfo") or {}
            sent = timing.get("clientRpcSendTime")
            settled = timing.get("clientSettleTime")
            dt = parse_ts(sent)
            _iso, date, hour, dow = local_parts(dt)
            duration = None
            if isinstance(sent, (int, float)) and isinstance(settled, (int, float)) and settled >= sent:
                duration = int(settled - sent)

            thinking = o.get("allThinkingBlocks")
            code_blocks = o.get("codeBlocks")
            mode = o.get("unifiedMode")

            con.execute(
                "INSERT OR REPLACE INTO cur_message (bubble_key,composer_id,bubble_id,role,text_chars,"
                "is_agentic,thinking_blocks,code_blocks,tool_name,tool_status,tool_error,user_decision,"
                "input_tokens,output_tokens,ts,date,hour,dow,duration_ms,ts_source)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (key, composer_id, bubble_id, BUBBLE_ROLE.get(o.get("type"), str(o.get("type"))),
                 len(o.get("text") or ""), 1 if o.get("isAgentic") else 0,
                 len(thinking) if isinstance(thinking, list) else 0,
                 len(code_blocks) if isinstance(code_blocks, list) else 0,
                 tfd.get("name") if isinstance(tfd, dict) else None,
                 tfd.get("status") if isinstance(tfd, dict) else None,
                 1 if isinstance(tfd, dict) and tfd.get("error") else 0,
                 json.dumps(tfd.get("userDecision"), default=str)[:60]
                 if isinstance(tfd, dict) and tfd.get("userDecision") is not None else None,
                 tok.get("inputTokens") or 0, tok.get("outputTokens") or 0,
                 dt.isoformat() if dt else None, date, hour, dow, duration,
                 "exact" if dt else None),
            )
            added += 1
        if verbose and i and i % 5000 == 0:
            con.commit()
            print(f"  ... {i}/{len(todo)} bubbles")
    con.commit()

    con.execute(
        "UPDATE cur_session SET message_count = COALESCE("
        "(SELECT COUNT(*) FROM cur_message m WHERE m.composer_id = cur_session.composer_id), 0)")

    # Only ~2% of bubbles carry timingInfo (user turns never do), so a daily
    # chart built on exact stamps alone would show a tiny slice of reality.
    # Fall back to the parent chat's date and flag it as inferred. `hour` is
    # deliberately left NULL - a session date says nothing about time of day.
    con.execute(
        "UPDATE cur_message SET"
        "  date = (SELECT s.date FROM cur_session s WHERE s.composer_id = cur_message.composer_id),"
        "  ts_source = 'session'"
        " WHERE date IS NULL AND EXISTS ("
        "  SELECT 1 FROM cur_session s WHERE s.composer_id = cur_message.composer_id"
        "  AND s.date IS NOT NULL)")
    con.commit()
    return added


def _ingest_ai_lines(con, src):
    """ItemTable aiCodeTrackingLines -> cur_ai_line (AI-authored line hashes)."""
    try:
        row = src.execute("SELECT value FROM ItemTable WHERE key='aiCodeTrackingLines'").fetchone()
    except sqlite3.Error:
        return 0
    lines = _loads(row[0]) if row else None
    if not isinstance(lines, list):
        return 0
    added = 0
    for item in lines:
        if not isinstance(item, dict):
            continue
        meta = item.get("metadata") or {}
        name = meta.get("fileName")
        con.execute(
            "INSERT OR REPLACE INTO cur_ai_line (hash,composer_id,source,file_name,file_ext)"
            " VALUES (?,?,?,?,?)",
            (item.get("hash"), meta.get("composerId"), meta.get("source"), name,
             meta.get("fileExtension") and "." + str(meta["fileExtension"]).lower() or _ext_of(name)),
        )
        added += 1
    return added


def _ingest_conversations(con, gs):
    path = gs / "conversation-search.db"
    if not path.exists():
        return 0
    try:
        src = _open_ro(path)
    except sqlite3.Error:
        return 0
    added = 0
    try:
        for r in src.execute(
                "SELECT id, source, title, updated_at, is_archived FROM conversations"):
            dt = parse_ts(r["updated_at"])
            _iso, date, _h, _d = local_parts(dt)
            con.execute(
                "INSERT OR REPLACE INTO cur_conversation (id,source,title,updated_at,date,is_archived)"
                " VALUES (?,?,?,?,?,?)",
                (r["id"], r["source"], r["title"], dt.isoformat() if dt else None, date,
                 1 if r["is_archived"] else 0),
            )
            added += 1
    except sqlite3.Error:
        pass
    finally:
        src.close()
    return added


def run(con, full=False, verbose=True):
    gs = config.cursor_global_storage()
    started = datetime.now(timezone.utc).isoformat()
    if gs is None:
        return {"ok": False, "error": "Cursor globalStorage not found"}
    state_db = gs / "state.vscdb"
    if not state_db.exists():
        return {"ok": False, "error": f"No state.vscdb under {gs}"}

    try:
        src = _open_ro(state_db)
    except sqlite3.Error as exc:
        return {"ok": False, "error": f"Cannot open {state_db}: {exc}"}

    try:
        sessions = _ingest_headers(con, src, workspace_folders(gs))
        con.commit()
        bubbles = _ingest_bubbles(con, src, full=full, verbose=verbose)
        ai_lines = _ingest_ai_lines(con, src)
        con.commit()
    finally:
        src.close()

    convos = _ingest_conversations(con, gs)
    total = sessions + bubbles + ai_lines + convos
    con.execute(
        "INSERT INTO ingest_run (started_at,finished_at,source,files_seen,files_read,rows_added,note)"
        " VALUES (?,?,?,?,?,?,?)",
        (started, datetime.now(timezone.utc).isoformat(), SOURCE, 2, 2, total,
         "full" if full else "incremental"),
    )
    con.commit()
    return {"ok": True, "sessions": sessions, "bubbles": bubbles,
            "ai_lines": ai_lines, "conversations": convos, "rows": total}
