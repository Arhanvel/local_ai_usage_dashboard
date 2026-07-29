"""All dashboard aggregations. Every function returns plain JSON-able data."""
from . import config


def _rows(con, sql, params=()):
    return [dict(r) for r in con.execute(sql, params)]


def _one(con, sql, params=()):
    r = con.execute(sql, params).fetchone()
    return dict(r) if r else {}


def _scalar(con, sql, params=(), default=0):
    r = con.execute(sql, params).fetchone()
    if not r or r[0] is None:
        return default
    return r[0]


# A "user" record in a transcript is not necessarily something a person typed.
# It also carries SDK-injected prompts, task notifications and the instructions
# handed to sub-agents. Only these sources originate with the human.
HUMAN_PROMPT = "source IN ('typed','queued','suggestion_accepted')"


def _where(filters, prefix=""):
    """Build a WHERE fragment from {since, until, project} filters."""
    clauses, params = [], []
    p = prefix
    if filters.get("since"):
        clauses.append(f"{p}date >= ?")
        params.append(filters["since"])
    if filters.get("until"):
        clauses.append(f"{p}date <= ?")
        params.append(filters["until"])
    if filters.get("project"):
        clauses.append(f"{p}project = ?")
        params.append(filters["project"])
    return (" AND " + " AND ".join(clauses) if clauses else ""), params


# --------------------------------------------------------------------------
# Claude Code
# --------------------------------------------------------------------------

def cc_overview(con, f):
    w, p = _where(f)
    totals = _one(con, f"""
        SELECT COUNT(*)                       AS assistant_blocks,
               SUM(CASE WHEN usage_dupe=0 THEN 1 ELSE 0 END) AS assistant_messages,
               COUNT(DISTINCT session_id)     AS sessions,
               COUNT(DISTINCT project)        AS projects,
               COUNT(DISTINCT date)           AS active_days,
               COALESCE(SUM(input_tokens),0)  AS input_tokens,
               COALESCE(SUM(output_tokens),0) AS output_tokens,
               COALESCE(SUM(cache_write),0)   AS cache_write,
               COALESCE(SUM(cache_read),0)    AS cache_read,
               COALESCE(SUM(cost_usd),0)      AS cost_usd,
               COALESCE(SUM(thinking_blocks),0) AS thinking_blocks,
               COALESCE(SUM(thinking_chars),0)  AS thinking_chars,
               COALESCE(SUM(text_chars),0)      AS text_chars,
               COALESCE(SUM(tool_uses),0)       AS tool_uses,
               COALESCE(SUM(is_api_error),0)    AS api_errors,
               COALESCE(SUM(web_search_reqs),0) AS web_searches,
               COALESCE(SUM(web_fetch_reqs),0)  AS web_fetches,
               MIN(date) AS first_day, MAX(date) AS last_day
        FROM cc_message WHERE 1=1 {w}""", p)

    wt, pt = _where(f)
    tools = _one(con, f"""
        SELECT COUNT(*) AS tool_calls,
               COALESCE(SUM(is_error),0)    AS tool_errors,
               COALESCE(SUM(interrupted),0) AS interrupted,
               COALESCE(SUM(lines_added),0) AS lines_added,
               COALESCE(SUM(lines_removed),0) AS lines_removed,
               COUNT(DISTINCT file_path)    AS files_touched,
               SUM(CASE WHEN denial_kind IS NOT NULL THEN 1 ELSE 0 END) AS denials,
               SUM(is_mcp) AS mcp_calls,
               -- The API's server_tool_use counters stay 0 because Claude Code
               -- runs web access as its own client-side tools, so count those.
               SUM(CASE WHEN name='WebSearch' THEN 1 ELSE 0 END) AS web_search_calls,
               SUM(CASE WHEN name='WebFetch'  THEN 1 ELSE 0 END) AS web_fetch_calls
        FROM cc_tool_call WHERE 1=1 {wt}""", pt)

    prompts = _one(con, f"""
        SELECT COUNT(*) AS prompt_records,
               SUM(CASE WHEN {HUMAN_PROMPT} THEN 1 ELSE 0 END)      AS prompts,
               COALESCE(SUM(CASE WHEN {HUMAN_PROMPT} THEN chars END),0) AS prompt_chars,
               COALESCE(AVG(CASE WHEN {HUMAN_PROMPT} THEN chars END),0) AS avg_prompt_chars,
               COALESCE(SUM(CASE WHEN {HUMAN_PROMPT} THEN words END),0) AS prompt_words
        FROM cc_prompt WHERE 1=1 {w}""", p)

    turns = _one(con, f"""
        SELECT COUNT(*) AS turns, COALESCE(SUM(duration_ms),0) AS total_turn_ms,
               COALESCE(AVG(duration_ms),0) AS avg_turn_ms,
               COALESCE(MAX(duration_ms),0) AS max_turn_ms
        FROM cc_turn WHERE 1=1 {w}""", p)

    total_tokens = (totals.get("input_tokens", 0) + totals.get("output_tokens", 0)
                    + totals.get("cache_write", 0) + totals.get("cache_read", 0))
    billed_in = totals.get("input_tokens", 0) + totals.get("cache_write", 0)
    cache_hit = 0.0
    denom = totals.get("cache_read", 0) + billed_in
    if denom:
        cache_hit = totals["cache_read"] / denom * 100.0

    out = {**totals, **tools, **prompts, **turns}
    out["total_tokens"] = total_tokens
    out["cache_hit_pct"] = round(cache_hit, 1)
    out["sidechain_messages"] = _scalar(con, f"SELECT COUNT(*) FROM cc_message WHERE is_sidechain=1 {w}", p)
    out["tool_error_rate"] = round(
        (tools.get("tool_errors", 0) / tools["tool_calls"] * 100.0) if tools.get("tool_calls") else 0.0, 1)
    if out.get("active_days"):
        out["avg_cost_per_active_day"] = round(out["cost_usd"] / out["active_days"], 2)
        out["avg_tokens_per_active_day"] = int(total_tokens / out["active_days"])
    return out


def cc_daily(con, f):
    """Per-day rollup.

    `api_calls` is what the *model* did (one row per billed response), which is
    an order of magnitude larger than `prompts` - what the *person* typed.
    Keeping both here stops the two being confused for each other.
    """
    w, p = _where(f)
    rows = _rows(con, f"""
        SELECT date,
               SUM(CASE WHEN usage_dupe=0 THEN 1 ELSE 0 END) AS api_calls,
               COUNT(DISTINCT session_id)      AS sessions,
               SUM(input_tokens)               AS input_tokens,
               SUM(output_tokens)              AS output_tokens,
               SUM(cache_write)                AS cache_write,
               SUM(cache_read)                 AS cache_read,
               SUM(input_tokens+output_tokens+cache_write+cache_read) AS total_tokens,
               ROUND(SUM(cost_usd),4)          AS cost_usd,
               SUM(tool_uses)                  AS tool_uses,
               SUM(thinking_blocks)            AS thinking_blocks
        FROM cc_message WHERE date IS NOT NULL {w}
        GROUP BY date ORDER BY date""", p)

    typed = {r["date"]: r["n"] for r in _rows(con, f"""
        SELECT date, COUNT(*) AS n FROM cc_prompt
        WHERE date IS NOT NULL AND {HUMAN_PROMPT} {w} GROUP BY date""", p)}
    injected = {r["date"]: r["n"] for r in _rows(con, f"""
        SELECT date, COUNT(*) AS n FROM cc_prompt
        WHERE date IS NOT NULL AND NOT ({HUMAN_PROMPT}) {w} GROUP BY date""", p)}
    for r in rows:
        r["prompts"] = typed.get(r["date"], 0)
        r["injected_prompts"] = injected.get(r["date"], 0)
    return rows


def cc_hourly(con, f):
    w, p = _where(f)
    return _rows(con, f"""
        SELECT hour, SUM(CASE WHEN usage_dupe=0 THEN 1 ELSE 0 END) AS api_calls,
               SUM(input_tokens+output_tokens+cache_write+cache_read) AS tokens,
               ROUND(SUM(cost_usd),4) AS cost_usd
        FROM cc_message WHERE hour IS NOT NULL {w}
        GROUP BY hour ORDER BY hour""", p)


def cc_weekday_hour(con, f):
    """Punch-card matrix: activity by weekday x hour."""
    w, p = _where(f)
    return _rows(con, f"""
        SELECT dow, hour, SUM(CASE WHEN usage_dupe=0 THEN 1 ELSE 0 END) AS api_calls,
               SUM(input_tokens+output_tokens+cache_write+cache_read) AS tokens
        FROM cc_message WHERE hour IS NOT NULL AND dow IS NOT NULL {w}
        GROUP BY dow, hour""", p)


def cc_models(con, f):
    w, p = _where(f)
    rows = _rows(con, f"""
        SELECT model, SUM(CASE WHEN usage_dupe=0 THEN 1 ELSE 0 END) AS messages,
               COUNT(DISTINCT session_id) AS sessions,
               SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens,
               SUM(cache_write) AS cache_write, SUM(cache_read) AS cache_read,
               SUM(input_tokens+output_tokens+cache_write+cache_read) AS total_tokens,
               ROUND(SUM(cost_usd),4) AS cost_usd,
               SUM(thinking_blocks) AS thinking_blocks,
               -- average over billed calls only; duplicate rows were zeroed
               ROUND(SUM(output_tokens) * 1.0
                     / NULLIF(SUM(CASE WHEN usage_dupe=0 THEN 1 ELSE 0 END), 0), 1)
                   AS avg_output_tokens
        FROM cc_message WHERE model IS NOT NULL {w}
        GROUP BY model ORDER BY total_tokens DESC""", p)
    pricing = config.load_pricing()
    models = pricing.get("models", {})
    default = pricing.get("default", {})
    for r in rows:
        entry = models.get(r["model"]) or default
        r["price_in"] = entry.get("input")
        r["price_out"] = entry.get("output")
        r["price_confidence"] = entry.get("confidence", "fallback")
    return rows


def cc_projects(con, f):
    w, p = _where(f)
    rows = _rows(con, f"""
        SELECT project, SUM(CASE WHEN usage_dupe=0 THEN 1 ELSE 0 END) AS messages,
               COUNT(DISTINCT session_id) AS sessions,
               COUNT(DISTINCT date) AS active_days,
               SUM(input_tokens+output_tokens+cache_write+cache_read) AS total_tokens,
               SUM(output_tokens) AS output_tokens,
               ROUND(SUM(cost_usd),4) AS cost_usd,
               SUM(tool_uses) AS tool_uses,
               MIN(date) AS first_day, MAX(date) AS last_day
        FROM cc_message WHERE project IS NOT NULL {w}
        GROUP BY project ORDER BY total_tokens DESC""", p)
    wt, pt = _where(f)
    edits = {r["project"]: r for r in _rows(con, f"""
        SELECT project, SUM(lines_added) AS lines_added, SUM(lines_removed) AS lines_removed,
               COUNT(DISTINCT file_path) AS files_touched, COUNT(*) AS tool_calls
        FROM cc_tool_call WHERE project IS NOT NULL {wt} GROUP BY project""", pt)}
    turns = {r["project"]: r for r in _rows(con, f"""
        SELECT project, SUM(duration_ms) AS turn_ms FROM cc_turn
        WHERE project IS NOT NULL {wt} GROUP BY project""", pt)}
    for r in rows:
        e = edits.get(r["project"], {})
        r["lines_added"] = e.get("lines_added") or 0
        r["lines_removed"] = e.get("lines_removed") or 0
        r["files_touched"] = e.get("files_touched") or 0
        r["tool_calls"] = e.get("tool_calls") or 0
        r["turn_ms"] = turns.get(r["project"], {}).get("turn_ms") or 0
    return rows


def cc_tools(con, f):
    """Per-tool rollup.

    Latency is the gap between the tool request and its result in the
    transcript. When a call waits on a permission prompt that gap includes
    human idle time, which badly skews the mean - so a median is reported
    alongside it.
    """
    w, p = _where(f)
    rows = _rows(con, f"""
        SELECT name, COUNT(*) AS calls,
               SUM(is_error) AS errors,
               SUM(interrupted) AS interrupted,
               SUM(CASE WHEN denial_kind IS NOT NULL THEN 1 ELSE 0 END) AS denied,
               SUM(has_result) AS completed,
               ROUND(AVG(latency_ms),0) AS avg_latency_ms,
               MAX(latency_ms) AS max_latency_ms,
               ROUND(AVG(result_chars),0) AS avg_result_chars,
               SUM(result_chars) AS total_result_chars,
               SUM(lines_added) AS lines_added, SUM(lines_removed) AS lines_removed,
               MAX(is_mcp) AS is_mcp, mcp_server
        FROM cc_tool_call WHERE 1=1 {w}
        GROUP BY name ORDER BY calls DESC""", p)
    medians = {r["name"]: r["median_latency_ms"] for r in _rows(con, f"""
        WITH ranked AS (
            SELECT name, latency_ms,
                   ROW_NUMBER() OVER (PARTITION BY name ORDER BY latency_ms) AS rn,
                   COUNT(*)     OVER (PARTITION BY name)                     AS cnt
            FROM cc_tool_call WHERE latency_ms IS NOT NULL {w}
        )
        SELECT name, ROUND(AVG(latency_ms),0) AS median_latency_ms FROM ranked
        WHERE rn IN ((cnt+1)/2, (cnt+2)/2) GROUP BY name""", p)}
    for r in rows:
        r["median_latency_ms"] = medians.get(r["name"])
    return rows


def cc_tool_daily(con, f, limit=8):
    """Daily call counts for the top-N tools (stacked area)."""
    w, p = _where(f)
    top = [r["name"] for r in _rows(con, f"""
        SELECT name, COUNT(*) c FROM cc_tool_call WHERE 1=1 {w}
        GROUP BY name ORDER BY c DESC LIMIT ?""", p + [limit])]
    if not top:
        return {"tools": [], "rows": []}
    placeholders = ",".join("?" * len(top))
    rows = _rows(con, f"""
        SELECT date, name, COUNT(*) AS calls FROM cc_tool_call
        WHERE date IS NOT NULL AND name IN ({placeholders}) {w}
        GROUP BY date, name ORDER BY date""", top + p)
    return {"tools": top, "rows": rows}


def cc_denials(con, f):
    w, p = _where(f)
    by_kind = _rows(con, f"""
        SELECT denial_kind, COUNT(*) AS n FROM cc_tool_call
        WHERE denial_kind IS NOT NULL {w} GROUP BY denial_kind ORDER BY n DESC""", p)
    by_tool = _rows(con, f"""
        SELECT name, denial_kind, COUNT(*) AS n FROM cc_tool_call
        WHERE denial_kind IS NOT NULL {w} GROUP BY name, denial_kind ORDER BY n DESC LIMIT 25""", p)
    return {"by_kind": by_kind, "by_tool": by_tool}


def cc_files(con, f, limit=40):
    w, p = _where(f)
    top = _rows(con, f"""
        SELECT file_path, file_ext, COUNT(*) AS touches,
               SUM(lines_added) AS lines_added, SUM(lines_removed) AS lines_removed,
               COUNT(DISTINCT session_id) AS sessions,
               SUM(CASE WHEN name IN ('Edit','Write','NotebookEdit') THEN 1 ELSE 0 END) AS writes,
               SUM(CASE WHEN name='Read' THEN 1 ELSE 0 END) AS reads
        FROM cc_tool_call WHERE file_path IS NOT NULL {w}
        GROUP BY file_path ORDER BY touches DESC LIMIT ?""", p + [limit])
    by_ext = _rows(con, f"""
        SELECT file_ext, COUNT(*) AS touches, COUNT(DISTINCT file_path) AS files,
               SUM(lines_added) AS lines_added, SUM(lines_removed) AS lines_removed
        FROM cc_tool_call WHERE file_ext IS NOT NULL {w}
        GROUP BY file_ext ORDER BY touches DESC LIMIT 25""", p)
    return {"top_files": top, "by_ext": by_ext}


def cc_bash(con, f, limit=30):
    w, p = _where(f)
    programs = _rows(con, f"""
        SELECT bash_program AS program, COUNT(*) AS calls,
               SUM(is_error) AS errors, SUM(interrupted) AS interrupted,
               ROUND(AVG(latency_ms),0) AS avg_latency_ms
        FROM cc_tool_call WHERE bash_program IS NOT NULL {w}
        GROUP BY bash_program ORDER BY calls DESC LIMIT ?""", p + [limit])
    return {"programs": programs}


def cc_sessions(con, f, limit=60):
    w, p = _where(f, "m.")
    return _rows(con, f"""
        SELECT m.session_id, m.project,
               COALESCE(s.ai_title, s.slug, m.session_id) AS title,
               s.git_branch, s.version, s.cwd,
               MIN(m.ts) AS started, MAX(m.ts) AS ended,
               SUM(CASE WHEN m.usage_dupe=0 THEN 1 ELSE 0 END) AS messages,
               SUM(m.input_tokens+m.output_tokens+m.cache_write+m.cache_read) AS total_tokens,
               ROUND(SUM(m.cost_usd),4) AS cost_usd,
               SUM(m.tool_uses) AS tool_uses,
               SUM(m.is_sidechain) AS sidechain_messages
        FROM cc_message m LEFT JOIN cc_session s ON s.session_id = m.session_id
        WHERE 1=1 {w}
        GROUP BY m.session_id ORDER BY total_tokens DESC LIMIT ?""", p + [limit])


def cc_session_shape(con, f):
    """Distribution of session sizes + per-session cost, for histograms."""
    w, p = _where(f)
    return _rows(con, f"""
        SELECT session_id, SUM(CASE WHEN usage_dupe=0 THEN 1 ELSE 0 END) AS messages,
               SUM(input_tokens+output_tokens+cache_write+cache_read) AS total_tokens,
               ROUND(SUM(cost_usd),4) AS cost_usd
        FROM cc_message WHERE 1=1 {w} GROUP BY session_id""", p)


def cc_prompts(con, f):
    w, p = _where(f)
    by_source = _rows(con, f"""
        SELECT COALESCE(source,'(sub-agent / legacy)') AS source, COUNT(*) AS n,
               ROUND(AVG(chars),0) AS avg_chars,
               CASE WHEN {HUMAN_PROMPT} THEN 'you' ELSE 'automated' END AS who
        FROM cc_prompt WHERE 1=1 {w} GROUP BY source ORDER BY n DESC""", p)
    by_origin = _rows(con, f"""
        SELECT COALESCE(origin_kind,'(none)') AS origin, COUNT(*) AS n
        FROM cc_prompt WHERE 1=1 {w} GROUP BY origin_kind ORDER BY n DESC""", p)
    daily = _rows(con, f"""
        SELECT date, COUNT(*) AS prompts, ROUND(AVG(chars),0) AS avg_chars
        FROM cc_prompt WHERE date IS NOT NULL AND {HUMAN_PROMPT} {w}
        GROUP BY date ORDER BY date""", p)
    longest = _rows(con, f"""
        SELECT date, project, chars, words, preview FROM cc_prompt
        WHERE {HUMAN_PROMPT} {w} ORDER BY chars DESC LIMIT 10""", p)
    return {"by_source": by_source, "by_origin": by_origin, "daily": daily, "longest": longest}


def cc_misc(con, f):
    w, p = _where(f)
    versions = _rows(con, f"""
        SELECT version, COUNT(*) AS messages, MIN(date) AS first_day, MAX(date) AS last_day
        FROM cc_message WHERE version IS NOT NULL {w}
        GROUP BY version ORDER BY first_day DESC LIMIT 30""", p)
    branches = _rows(con, f"""
        SELECT git_branch, COUNT(*) AS messages, COUNT(DISTINCT session_id) AS sessions
        FROM cc_message WHERE git_branch IS NOT NULL {w}
        GROUP BY git_branch ORDER BY messages DESC LIMIT 20""", p)
    skills = _rows(con, f"""
        SELECT skill, COUNT(*) AS messages,
               SUM(input_tokens+output_tokens+cache_write+cache_read) AS tokens,
               ROUND(SUM(cost_usd),4) AS cost_usd
        FROM cc_message WHERE skill IS NOT NULL {w} GROUP BY skill ORDER BY messages DESC""", p)
    mcp = _rows(con, f"""
        SELECT mcp_server, COUNT(*) AS calls, COUNT(DISTINCT name) AS distinct_tools,
               SUM(is_error) AS errors
        FROM cc_tool_call WHERE is_mcp=1 {w} GROUP BY mcp_server ORDER BY calls DESC""", p)
    subagents = _rows(con, f"""
        SELECT COALESCE(agent_type,'(default)') AS agent_type, COUNT(*) AS runs,
               SUM(total_tokens) AS total_tokens,
               ROUND(AVG(total_duration_ms),0) AS avg_duration_ms,
               SUM(tool_use_count) AS tool_uses, resolved_model
        FROM cc_subagent WHERE 1=1 {w} GROUP BY agent_type, resolved_model ORDER BY runs DESC""", p)
    effort = _rows(con, f"""
        SELECT COALESCE(effort,'(unset)') AS effort, COUNT(*) AS messages,
               SUM(thinking_blocks) AS thinking_blocks
        FROM cc_message WHERE type='assistant' {w} GROUP BY effort ORDER BY messages DESC""", p)
    stop = _rows(con, f"""
        SELECT COALESCE(stop_reason,'(none)') AS stop_reason, COUNT(*) AS n
        FROM cc_message WHERE type='assistant' {w} GROUP BY stop_reason ORDER BY n DESC""", p)
    events = _rows(con, f"""
        SELECT kind, COALESCE(subkind,'') AS subkind, COUNT(*) AS n
        FROM cc_event WHERE 1=1 {w} GROUP BY kind, subkind ORDER BY n DESC LIMIT 40""", p)
    turns = _rows(con, f"""
        SELECT date, COUNT(*) AS turns, ROUND(AVG(duration_ms)/1000.0,1) AS avg_turn_s,
               ROUND(SUM(duration_ms)/60000.0,1) AS total_turn_min
        FROM cc_turn WHERE date IS NOT NULL {w} GROUP BY date ORDER BY date""", p)
    return {"versions": versions, "branches": branches, "skills": skills, "mcp": mcp,
            "subagents": subagents, "effort": effort, "stop_reasons": stop,
            "events": events, "turns_daily": turns}


def cc_legacy(con, f):
    """Pruned-transcript history recovered from stats-cache.json.

    Reported separately: these days have no per-message rows, and the overlap
    with retained transcripts would otherwise be double-counted.
    """
    daily = _rows(con, """
        SELECT date, messages, sessions, tool_calls, tokens, models_json
        FROM cc_legacy_daily ORDER BY date""")
    models = _rows(con, """
        SELECT model, input_tokens, output_tokens, cache_read, cache_write, web_searches,
               (input_tokens+output_tokens+cache_read+cache_write) AS total_tokens
        FROM cc_legacy_model ORDER BY total_tokens DESC""")
    meta_rows = {r["key"]: r["value"] for r in _rows(con, "SELECT key, value FROM cc_legacy_meta")}
    live_first = _scalar(con, "SELECT MIN(date) FROM cc_message", (), None)
    only_legacy = [d for d in daily if not live_first or d["date"] < live_first]
    return {
        "daily": daily,
        "models": models,
        "meta": meta_rows,
        "live_first_day": live_first,
        "days_only_in_cache": len(only_legacy),
        "messages_only_in_cache": sum(d["messages"] or 0 for d in only_legacy),
    }


def cc_streaks(con, f):
    days = [r["date"] for r in _rows(con, "SELECT DISTINCT date FROM cc_message WHERE date IS NOT NULL ORDER BY date")]
    if not days:
        return {"longest": 0, "current": 0, "days": 0}
    from datetime import date as _date, timedelta
    parsed = []
    for d in days:
        try:
            y, m, dd = (int(x) for x in d.split("-"))
            parsed.append(_date(y, m, dd))
        except ValueError:
            continue
    longest = cur = 1
    for i in range(1, len(parsed)):
        if parsed[i] - parsed[i - 1] == timedelta(days=1):
            cur += 1
            longest = max(longest, cur)
        else:
            cur = 1
    # current streak counts back from the most recent active day
    tail = 1
    for i in range(len(parsed) - 1, 0, -1):
        if parsed[i] - parsed[i - 1] == timedelta(days=1):
            tail += 1
        else:
            break
    return {"longest": longest, "current": tail, "days": len(parsed),
            "first": days[0], "last": days[-1]}


# --------------------------------------------------------------------------
# Cursor
# --------------------------------------------------------------------------

def cur_overview(con, f):
    o = _one(con, """
        SELECT COUNT(*) AS sessions,
               SUM(lines_added) AS lines_added, SUM(lines_removed) AS lines_removed,
               SUM(files_changed) AS files_changed,
               SUM(is_subagent) AS subagent_sessions,
               SUM(is_archived) AS archived,
               ROUND(AVG(context_usage_pct),1) AS avg_context_pct,
               COUNT(DISTINCT project) AS projects,
               MIN(date) AS first_day, MAX(date) AS last_day
        FROM cur_session""")
    m = _one(con, """
        SELECT COUNT(*) AS messages,
               SUM(CASE WHEN role='user' THEN 1 ELSE 0 END) AS user_messages,
               SUM(CASE WHEN role='assistant' THEN 1 ELSE 0 END) AS assistant_messages,
               SUM(CASE WHEN tool_name IS NOT NULL THEN 1 ELSE 0 END) AS tool_calls,
               SUM(text_chars) AS text_chars,
               SUM(thinking_blocks) AS thinking_blocks,
               SUM(code_blocks) AS code_blocks,
               SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens,
               COUNT(DISTINCT date) AS active_days
        FROM cur_message""")
    ai = _one(con, "SELECT COUNT(*) AS ai_lines, COUNT(DISTINCT file_name) AS ai_files FROM cur_ai_line")
    convo = _one(con, "SELECT COUNT(*) AS conversations FROM cur_conversation")
    ts = _one(con, """
        SELECT SUM(CASE WHEN ts_source='exact'   THEN 1 ELSE 0 END) AS ts_exact,
               SUM(CASE WHEN ts_source='session' THEN 1 ELSE 0 END) AS ts_inferred,
               SUM(CASE WHEN ts_source IS NULL   THEN 1 ELSE 0 END) AS ts_none
        FROM cur_message""")
    out = {**o, **m, **ai, **convo, **ts}
    total = out.get("messages") or 1
    out["ts_exact_pct"] = round((out.get("ts_exact") or 0) / total * 100, 1)
    out["token_note"] = ("Cursor meters usage server-side; local tokenCount is 0 on most "
                         "messages, so token/cost figures here are partial.")
    # Cursor keeps only a rolling window of attributed lines, so this is a
    # recent-activity sample, not a lifetime total.
    out["ai_lines_capped"] = (out.get("ai_lines") or 0) >= 10000
    return out


def cur_daily(con, f):
    return _rows(con, """
        SELECT date, COUNT(*) AS messages,
               COUNT(DISTINCT composer_id) AS sessions,
               SUM(CASE WHEN tool_name IS NOT NULL THEN 1 ELSE 0 END) AS tool_calls,
               SUM(text_chars) AS text_chars
        FROM cur_message WHERE date IS NOT NULL GROUP BY date ORDER BY date""")


def cur_tools(con, f):
    return _rows(con, """
        SELECT tool_name AS name, COUNT(*) AS calls,
               SUM(tool_error) AS errors,
               SUM(CASE WHEN tool_status='completed' THEN 1 ELSE 0 END) AS completed,
               ROUND(AVG(duration_ms),0) AS avg_duration_ms
        FROM cur_message WHERE tool_name IS NOT NULL
        GROUP BY tool_name ORDER BY calls DESC LIMIT 40""")


def cur_projects(con, f):
    return _rows(con, """
        SELECT COALESCE(s.project,'(unknown)') AS project,
               COUNT(*) AS sessions,
               SUM(s.lines_added) AS lines_added, SUM(s.lines_removed) AS lines_removed,
               SUM(s.files_changed) AS files_changed,
               SUM(s.message_count) AS messages,
               MIN(s.date) AS first_day, MAX(s.date) AS last_day
        FROM cur_session s GROUP BY s.project ORDER BY messages DESC LIMIT 40""")


def cur_sessions(con, f, limit=50):
    return _rows(con, """
        SELECT composer_id, name, project, mode, date, created_at, last_updated_at,
               lines_added, lines_removed, files_changed, message_count,
               context_usage_pct, is_subagent, subtitle
        FROM cur_session ORDER BY message_count DESC LIMIT ?""", (limit,))


def cur_modes(con, f):
    return _rows(con, """
        SELECT COALESCE(mode,'(unknown)') AS mode, COUNT(*) AS sessions,
               SUM(message_count) AS messages, SUM(lines_added) AS lines_added
        FROM cur_session GROUP BY mode ORDER BY sessions DESC""")


def cur_ai_lines(con, f):
    by_ext = _rows(con, """
        SELECT COALESCE(file_ext,'(none)') AS file_ext, COUNT(*) AS lines,
               COUNT(DISTINCT file_name) AS files
        FROM cur_ai_line GROUP BY file_ext ORDER BY lines DESC LIMIT 25""")
    by_file = _rows(con, """
        SELECT file_name, file_ext, COUNT(*) AS lines
        FROM cur_ai_line GROUP BY file_name ORDER BY lines DESC LIMIT 30""")
    by_source = _rows(con, """
        SELECT COALESCE(source,'(none)') AS source, COUNT(*) AS lines
        FROM cur_ai_line GROUP BY source ORDER BY lines DESC""")
    return {"by_ext": by_ext, "by_file": by_file, "by_source": by_source}


def cur_hourly(con, f):
    return _rows(con, """
        SELECT hour, COUNT(*) AS messages FROM cur_message
        WHERE hour IS NOT NULL GROUP BY hour ORDER BY hour""")


# --------------------------------------------------------------------------

def meta(con):
    runs = _rows(con, """
        SELECT source, finished_at, files_seen, files_read, rows_added, note
        FROM ingest_run ORDER BY id DESC LIMIT 6""")
    pricing = config.load_pricing()
    used = _rows(con, "SELECT DISTINCT model FROM cc_message WHERE model IS NOT NULL")
    warn = []
    for r in used:
        entry = pricing.get("models", {}).get(r["model"])
        conf = entry.get("confidence") if entry else "fallback"
        if conf != "official":
            warn.append({"model": r["model"], "confidence": conf})
    return {
        "runs": runs,
        "pricing_warnings": warn,
        "db_path": str(config.DB_PATH),
        "claude_dir": str(config.claude_home()),
        "cursor_dir": str(config.cursor_global_storage() or ""),
    }


def project_list(con):
    return [r["project"] for r in _rows(
        con, "SELECT DISTINCT project FROM cc_message WHERE project IS NOT NULL ORDER BY project")]


def build_payload(con, filters):
    f = filters or {}
    return {
        "meta": meta(con),
        "filters": f,
        "projects": project_list(con),
        "claude": {
            "overview": cc_overview(con, f),
            "daily": cc_daily(con, f),
            "hourly": cc_hourly(con, f),
            "punchcard": cc_weekday_hour(con, f),
            "models": cc_models(con, f),
            "projects": cc_projects(con, f),
            "tools": cc_tools(con, f),
            "tool_daily": cc_tool_daily(con, f),
            "denials": cc_denials(con, f),
            "files": cc_files(con, f),
            "bash": cc_bash(con, f),
            "sessions": cc_sessions(con, f),
            "session_shape": cc_session_shape(con, f),
            "prompts": cc_prompts(con, f),
            "misc": cc_misc(con, f),
            "streaks": cc_streaks(con, f),
            "legacy": cc_legacy(con, f),
        },
        "cursor": {
            "overview": cur_overview(con, f),
            "daily": cur_daily(con, f),
            "hourly": cur_hourly(con, f),
            "tools": cur_tools(con, f),
            "projects": cur_projects(con, f),
            "sessions": cur_sessions(con, f),
            "modes": cur_modes(con, f),
            "ai_lines": cur_ai_lines(con, f),
        },
    }
