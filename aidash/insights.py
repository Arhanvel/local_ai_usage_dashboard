"""Behavioural analyses: how the person drives Claude Code, where the
friction is, what was delivered, and which repeated work could become a skill.

Everything here is derived from the tables the ingester fills. The heavier
prompt-mining functions (Jaccard clustering, n-grams) live at the bottom and
are served on demand rather than with every page load.
"""
from collections import Counter, defaultdict
from datetime import timedelta

from . import config, patterns
from .config import parse_ts
from .pricing import Pricer
from .sql import HUMAN_PROMPT, SESSION_TITLE, median, one as _one, percentile, rows as _rows, where as _where

READONLY_TOOLS = ("Read", "Grep", "Glob", "ToolSearch", "WebFetch", "WebSearch", "LS")
MUTATING_TOOLS = ("Edit", "Write", "NotebookEdit", "MultiEdit")
_RO = ",".join("?" * len(READONLY_TOOLS))
_MUT = ",".join("?" * len(MUTATING_TOOLS))


# --------------------------------------------------------------------------
# Per-session facts, shared by several analyses
# --------------------------------------------------------------------------

def session_facts(con, f):
    """One dict per session inside the filter, with the numbers most analyses need."""
    w, p = _where(f, "m.")
    sessions = {r["session_id"]: r for r in _rows(con, f"""
        SELECT m.session_id, m.project,
               {SESSION_TITLE.format(fallback="m.session_id")} AS title,
               s.entrypoint, s.cwd,
               MIN(m.ts) AS started, MAX(m.ts) AS ended,
               SUM(CASE WHEN m.usage_dupe=0 THEN 1 ELSE 0 END) AS api_calls,
               SUM(m.input_tokens+m.output_tokens+m.cache_write+m.cache_read) AS total_tokens,
               SUM(m.cache_read) AS cache_read,
               SUM(m.input_tokens+m.cache_write) AS billed_in,
               ROUND(SUM(m.cost_usd),4) AS cost_usd,
               SUM(m.tool_uses) AS tool_uses,
               SUM(CASE WHEN m.transcript<>'main' OR m.is_sidechain=1 THEN m.cost_usd ELSE 0 END) AS nested_cost,
               SUM(CASE WHEN m.transcript<>'main' OR m.is_sidechain=1 THEN m.tool_uses ELSE 0 END) AS nested_tools,
               MAX(CASE WHEN m.model LIKE 'claude-opus%' THEN 1 ELSE 0 END) AS uses_opus,
               COUNT(DISTINCT CASE WHEN m.model<>'<synthetic>' THEN m.model END) AS distinct_models,
               SUM(CASE WHEN m.error_kind IS NOT NULL THEN 1 ELSE 0 END) AS error_replies
        FROM cc_message m LEFT JOIN cc_session s ON s.session_id = m.session_id
        WHERE 1=1 {w}
        GROUP BY m.session_id""", p)}
    if not sessions:
        return {}

    w2, p2 = _where(f)
    for r in _rows(con, f"""
        SELECT session_id, COUNT(*) AS n,
               SUM(CASE WHEN name IN ({_RO}) THEN 1 ELSE 0 END) AS readonly,
               SUM(CASE WHEN name = 'Agent' THEN 1 ELSE 0 END) AS agent_calls,
               SUM(is_error) AS errors
        FROM cc_tool_call WHERE 1=1 {w2} GROUP BY session_id""", list(READONLY_TOOLS) + p2):
        s = sessions.get(r["session_id"])
        if s:
            s.update(tool_calls=r["n"], readonly_calls=r["readonly"],
                     agent_calls=r["agent_calls"], tool_errors=r["errors"])
    for r in _rows(con, f"""
        SELECT session_id, COUNT(*) AS n, SUM(CASE WHEN {HUMAN_PROMPT} THEN 1 ELSE 0 END) AS human,
               SUM(CASE WHEN {HUMAN_PROMPT} AND intents LIKE '%correction%' THEN 1 ELSE 0 END) AS corrections,
               SUM(CASE WHEN is_slash=1 THEN 1 ELSE 0 END) AS slash
        FROM cc_prompt WHERE 1=1 {w2} GROUP BY session_id""", p2):
        s = sessions.get(r["session_id"])
        if s:
            s.update(prompts=r["human"], prompt_records=r["n"], corrections=r["corrections"],
                     slash=r["slash"])
    for r in _rows(con, f"""
        SELECT session_id, SUM(duration_ms) AS turn_ms, COUNT(*) AS turns
        FROM cc_turn WHERE 1=1 {w2} GROUP BY session_id""", p2):
        s = sessions.get(r["session_id"])
        if s:
            s.update(turn_ms=r["turn_ms"] or 0, turns=r["turns"])
    for r in _rows(con, f"""
        SELECT session_id,
               SUM(CASE WHEN kind='interrupt' THEN 1 ELSE 0 END) AS interrupts,
               SUM(CASE WHEN kind='compact' THEN 1 ELSE 0 END) AS compactions
        FROM cc_event WHERE 1=1 {w2} GROUP BY session_id""", p2):
        s = sessions.get(r["session_id"])
        if s:
            s.update(interrupts=r["interrupts"], compactions=r["compactions"])

    for s in sessions.values():
        for k in ("tool_calls", "readonly_calls", "agent_calls", "tool_errors", "prompts",
                  "prompt_records", "corrections", "slash", "turn_ms", "turns", "interrupts",
                  "compactions"):
            s.setdefault(k, 0)
        a, b = parse_ts(s["started"]), parse_ts(s["ended"])
        s["wall_ms"] = int((b - a).total_seconds() * 1000) if a and b else 0
        s["headless"] = 1 if (s.get("entrypoint") or "") not in ("cli", "", None) else 0
        s["readonly_share"] = round(s["readonly_calls"] / s["tool_calls"], 2) if s["tool_calls"] else 0
        s["cost_per_tool"] = round(s["cost_usd"] / s["tool_calls"], 4) if s["tool_calls"] else None
        # Model time above wall-clock time only happens when sub-agents run in
        # parallel: the signature of genuine delegation.
        s["parallelism"] = round(s["turn_ms"] / s["wall_ms"], 2) if s["wall_ms"] and s["turn_ms"] else None
        s["cache_hit"] = round(s["cache_read"] / (s["cache_read"] + s["billed_in"]) * 100, 1) \
            if (s["cache_read"] + s["billed_in"]) else 0
    return sessions


def _slim(s, keys):
    return {k: s.get(k) for k in ("session_id", "project", "title", "started") + tuple(keys)}


def slash_usage(con, f, limit=40):
    """How often each slash command / skill was invoked by hand."""
    w, p = _where(f)
    return _rows(con, f"""
        SELECT slash_name AS name, COUNT(*) AS n, COUNT(DISTINCT session_id) AS sessions,
               COUNT(DISTINCT project) AS projects, MIN(date) AS first_day, MAX(date) AS last_day
        FROM cc_prompt WHERE is_slash=1 AND slash_name IS NOT NULL {w}
        GROUP BY slash_name ORDER BY n DESC LIMIT ?""", p + [limit])


# --------------------------------------------------------------------------
# Habits: how the person drives it
# --------------------------------------------------------------------------

def habits(con, f, sessions=None):
    sessions = sessions if sessions is not None else session_facts(con, f)
    w, p = _where(f)
    interactive = [s for s in sessions.values() if not s["headless"]]
    prompts_per = [s["prompts"] for s in interactive if s["prompts"]]
    total_prompts = sum(s["prompts"] for s in sessions.values())
    total_calls = sum(s["api_calls"] for s in sessions.values())
    total_tools = sum(s["tool_calls"] for s in sessions.values())

    # A 400-char head is enough for openers and the premature-send check.
    prompt_rows = _rows(con, f"""
        SELECT session_id, ts, chars, substr(text, 1, 400) AS text, is_slash, slash_name, hour, dow, intents
        FROM cc_prompt WHERE {HUMAN_PROMPT} {w} ORDER BY session_id, ts""", p)

    # Openers: the first human prompt of each session.
    openers = Counter()
    seen_sessions = set()
    for r in prompt_rows:
        if r["session_id"] in seen_sessions:
            continue
        seen_sessions.add(r["session_id"])
        t = r["text"] or ""
        if r["is_slash"]:
            openers["slash " + (r["slash_name"] or "")] += 1
        elif patterns.RE_CONTINUATION.match(t):
            openers["continuation"] += 1
        elif len(t) < 40:
            openers["short (<40 chars)"] += 1
        elif len(t) < 400:
            openers["medium"] += 1
        else:
            openers["long (400+ chars)"] += 1
    short = sum(1 for r in prompt_rows if not r["is_slash"] and (r["chars"] or 0) < 25)
    night = sum(1 for r in prompt_rows if r["hour"] is not None and r["hour"] <= 5)
    weekend = sum(1 for r in prompt_rows if r["dow"] in (5, 6))
    intents = Counter()
    for r in prompt_rows:
        for name in (r["intents"] or "").split(","):
            if name:
                intents[name] += 1

    # Premature send: the same prompt re-sent within minutes with text appended,
    # i.e. Enter was hit before the thought was finished.
    premature, gaps = 0, []
    prev = None
    for r in prompt_rows:
        if prev and prev["session_id"] == r["session_id"] and not r["is_slash"]:
            a, b = parse_ts(prev["ts"]), parse_ts(r["ts"])
            pt, ct = (prev["text"] or "").strip(), (r["text"] or "").strip()
            if a and b and len(pt) >= 12 and len(ct) > len(pt) and ct.startswith(pt) \
                    and (b - a) <= timedelta(minutes=10):
                premature += 1
                gaps.append((b - a).total_seconds())
        prev = r

    slash = slash_usage(con, f, limit=30)

    by_entry = defaultdict(lambda: {"sessions": 0, "cost_usd": 0.0, "prompts": 0, "api_calls": 0,
                                    "tool_calls": 0})
    for s in sessions.values():
        e = by_entry["headless (sdk)" if s["headless"] else "interactive (cli)"]
        e["sessions"] += 1
        e["cost_usd"] += s["cost_usd"] or 0
        e["prompts"] += s["prompts"]
        e["api_calls"] += s["api_calls"]
        e["tool_calls"] += s["tool_calls"]

    # Headless reruns: the same SDK prompt fired more than once on one day.
    reruns = _rows(con, f"""
        SELECT project, date, substr(text,1,80) AS head, COUNT(*) AS n FROM cc_prompt
        WHERE is_human=0 AND source='sdk' {w}
        GROUP BY project, date, head HAVING n > 1 ORDER BY n DESC LIMIT 15""", p)

    delegating = [s for s in sessions.values() if s["agent_calls"] > 0 or s["nested_tools"]]
    non_deleg = [s for s in sessions.values()
                 if not (s["agent_calls"] > 0 or s["nested_tools"]) and s["tool_calls"]]
    parallel = sorted((s for s in sessions.values() if s["parallelism"] and s["parallelism"] > 1.05),
                      key=lambda s: -s["parallelism"])[:12]
    overnight = [s for s in sessions.values()
                 if s["wall_ms"] > 4 * 3600_000 and parse_ts(s["started"])
                 and (parse_ts(s["started"]).astimezone().hour >= 17
                      or parse_ts(s["started"]).astimezone().hour <= 3)]

    return {
        "sessions": len(sessions),
        "interactive_sessions": len(interactive),
        "one_prompt_sessions": sum(1 for s in interactive if s["prompts"] <= 1),
        "long_sessions": sum(1 for s in interactive if s["prompts"] >= 10),
        "prompts_per_session_median": median(prompts_per),
        "prompts_per_session_p90": percentile(prompts_per, 0.9),
        "actions_per_prompt": round(total_calls / total_prompts, 1) if total_prompts else 0,
        "tools_per_prompt": round(total_tools / total_prompts, 1) if total_prompts else 0,
        "prompts": len(prompt_rows),
        "short_prompts": short,
        "short_prompt_pct": round(short / len(prompt_rows) * 100, 1) if prompt_rows else 0,
        "night_pct": round(night / len(prompt_rows) * 100, 1) if prompt_rows else 0,
        "weekend_pct": round(weekend / len(prompt_rows) * 100, 1) if prompt_rows else 0,
        "corrections": intents.get("correction", 0),
        "correction_pct": round(intents.get("correction", 0) / len(prompt_rows) * 100, 1) if prompt_rows else 0,
        "premature_sends": premature,
        "premature_median_gap_s": round(median(gaps)) if gaps else 0,
        "openers": [{"kind": k, "n": v} for k, v in openers.most_common()],
        "continuation_opener_pct": round(openers["continuation"] / max(1, sum(openers.values())) * 100, 1),
        "intents": [{"intent": k, "n": v, "pct": round(v / len(prompt_rows) * 100, 1)}
                    for k, v in intents.most_common()] if prompt_rows else [],
        "slash": slash,
        "by_entrypoint": [{"entrypoint": k, **v, "cost_usd": round(v["cost_usd"], 2)}
                          for k, v in sorted(by_entry.items())],
        "headless_reruns": reruns,
        "redundant_reruns": sum(r["n"] - 1 for r in reruns),
        "delegating_sessions": len(delegating),
        "delegated_tool_share": round(sum(s["nested_tools"] for s in sessions.values())
                                      / total_tools * 100, 1) if total_tools else 0,
        "delegated_cost_share": round(sum(s["nested_cost"] or 0 for s in sessions.values())
                                      / max(1e-9, sum(s["cost_usd"] or 0 for s in sessions.values())) * 100, 1),
        "cost_per_tool_delegating": round(median([s["cost_per_tool"] for s in delegating if s["cost_per_tool"]]), 4),
        "cost_per_tool_direct": round(median([s["cost_per_tool"] for s in non_deleg if s["cost_per_tool"]]), 4),
        "parallel_sessions": [_slim(s, ("parallelism", "turn_ms", "wall_ms", "cost_usd", "agent_calls"))
                              for s in parallel],
        "overnight_sessions": [_slim(s, ("wall_ms", "cost_usd", "prompts", "tool_calls"))
                               for s in sorted(overnight, key=lambda s: -s["wall_ms"])[:10]],
        "interrupts": sum(s["interrupts"] for s in sessions.values()),
        "compactions": sum(s["compactions"] for s in sessions.values()),
    }


# --------------------------------------------------------------------------
# Friction and waste
# --------------------------------------------------------------------------

def friction(con, f, sessions=None):
    sessions = sessions if sessions is not None else session_facts(con, f)
    w, p = _where(f)
    errors = _rows(con, f"""
        SELECT error_kind, COUNT(*) AS n, COUNT(DISTINCT session_id) AS sessions,
               MIN(date) AS first_day, MAX(date) AS last_day
        FROM cc_message WHERE error_kind IS NOT NULL {w} GROUP BY error_kind ORDER BY n DESC""", p)
    error_daily = _rows(con, f"""
        SELECT date, error_kind, COUNT(*) AS n FROM cc_message
        WHERE error_kind IS NOT NULL AND date IS NOT NULL {w} GROUP BY date, error_kind ORDER BY date""", p)
    wm, pm = _where(f, "m.")
    limit_sessions = _rows(con, f"""
        SELECT m.session_id, m.project, m.date, m.error_kind, COUNT(*) AS n,
               {SESSION_TITLE.format(fallback="m.session_id")} AS title
        FROM cc_message m LEFT JOIN cc_session s ON s.session_id = m.session_id
        WHERE m.error_kind IN ('usage_limit','rate_limit','billing','fallback') {wm}
        GROUP BY m.session_id, m.error_kind ORDER BY m.date DESC LIMIT 20""", pm)
    events_daily = _rows(con, f"""
        SELECT date,
               SUM(CASE WHEN kind='interrupt' THEN 1 ELSE 0 END) AS interrupts,
               SUM(CASE WHEN kind='compact' THEN 1 ELSE 0 END) AS compactions,
               SUM(CASE WHEN kind='system' AND subkind='api_error' THEN 1 ELSE 0 END) AS api_errors
        FROM cc_event WHERE date IS NOT NULL {w} GROUP BY date ORDER BY date""", p)

    switches = [s for s in sessions.values() if (s["distinct_models"] or 0) > 1]
    costs = [s["cost_usd"] or 0 for s in sessions.values()]
    cpt = [s["cost_per_tool"] for s in sessions.values() if s["cost_per_tool"] and (s["cost_usd"] or 0) >= 2]
    cpt_median = median(cpt) if cpt else 0
    outliers = sorted((s for s in sessions.values()
                       if s["cost_per_tool"] and (s["cost_usd"] or 0) >= 2
                       and cpt_median and s["cost_per_tool"] > 3 * cpt_median),
                      key=lambda s: -s["cost_per_tool"])[:12]
    high_burn = sorted((s for s in sessions.values() if (s["cost_usd"] or 0) > 1 and s["tool_calls"] < 10),
                       key=lambda s: -(s["cost_usd"] or 0))[:12]
    search_heavy = sorted((s for s in sessions.values()
                           if s["tool_calls"] >= 10 and s["readonly_share"] >= 0.6 and s["uses_opus"]),
                          key=lambda s: -(s["cost_usd"] or 0))[:12]
    zero_tool = [s for s in sessions.values() if not s["tool_calls"]]

    # Routing counterfactual: the same tokens priced at each model family, in
    # one pass. Cost is linear in tokens for a fixed model, so summing tokens
    # per scope and pricing once equals pricing every row; the 5m fallback is
    # applied per row, exactly as the ingester did for `actual`.
    scopes = {
        "all": "1",
        "nested (sub-agents)": "(m.transcript<>'main' OR m.is_sidechain=1)",
        "opus": "m.model LIKE 'claude-opus%'",
        "headless": "COALESCE(s.entrypoint,'cli')<>'cli'",
    }
    wm, pm = _where(f, "m.")
    cols = ", ".join(
        f"SUM(CASE WHEN {cond} THEN m.input_tokens ELSE 0 END) AS i{k}, "
        f"SUM(CASE WHEN {cond} THEN m.output_tokens ELSE 0 END) AS o{k}, "
        f"SUM(CASE WHEN {cond} THEN (CASE WHEN m.eph_5m=0 AND m.eph_1h=0 THEN m.cache_write ELSE m.eph_5m END)"
        f" ELSE 0 END) AS e5{k}, "
        f"SUM(CASE WHEN {cond} THEN m.eph_1h ELSE 0 END) AS e1{k}, "
        f"SUM(CASE WHEN {cond} THEN m.cache_read ELSE 0 END) AS cr{k}, "
        f"SUM(CASE WHEN {cond} THEN m.cost_usd ELSE 0 END) AS cost{k}"
        for k, cond in enumerate(scopes.values()))
    t = _one(con, f"""
        SELECT {cols} FROM cc_message m LEFT JOIN cc_session s ON s.session_id = m.session_id
        WHERE 1=1 {wm}""", pm)
    pricer = Pricer()
    counterfactual = []
    for k, label in enumerate(scopes):
        if not t.get(f"cost{k}"):
            continue
        row = {"scope": label, "actual": round(t[f"cost{k}"], 2)}
        for fam in ("haiku", "sonnet", "opus"):
            model = pricer.family_model(fam)
            if model:
                row["at_" + fam] = round(pricer.cost(model, t[f"i{k}"] or 0, t[f"o{k}"] or 0,
                                                     t[f"e5{k}"] or 0, t[f"e1{k}"] or 0, 0,
                                                     t[f"cr{k}"] or 0), 2)
        counterfactual.append(row)

    cache_by_project = _rows(con, f"""
        SELECT project, ROUND(SUM(cache_read)*100.0 / NULLIF(SUM(cache_read+input_tokens+cache_write),0),1) AS hit_pct,
               SUM(eph_1h) AS cache_1h, SUM(eph_5m) AS cache_5m, ROUND(SUM(cost_usd),2) AS cost_usd
        FROM cc_message WHERE project IS NOT NULL {w} GROUP BY project ORDER BY cost_usd DESC LIMIT 15""", p)

    return {
        "errors": errors,
        "error_daily": error_daily,
        "limit_sessions": limit_sessions,
        "events_daily": events_daily,
        "interrupts": sum(s["interrupts"] for s in sessions.values()),
        "compactions": sum(s["compactions"] for s in sessions.values()),
        "sessions_with_compaction": sum(1 for s in sessions.values() if s["compactions"]),
        "model_switch_sessions": len(switches),
        "tool_errors": sum(s["tool_errors"] for s in sessions.values()),
        "median_session_cost": round(median(costs), 2) if costs else 0,
        "cost_per_tool_median": round(cpt_median, 4),
        "cost_per_tool_outliers": [_slim(s, ("cost_usd", "tool_calls", "cost_per_tool", "api_calls")) for s in outliers],
        "high_burn_low_tool": [_slim(s, ("cost_usd", "tool_calls", "api_calls", "prompts")) for s in high_burn],
        "search_heavy_opus": [_slim(s, ("cost_usd", "tool_calls", "readonly_share")) for s in search_heavy],
        "zero_tool_sessions": len(zero_tool),
        "zero_tool_cost": round(sum(s["cost_usd"] or 0 for s in zero_tool), 2),
        "routing": counterfactual,
        "cache_by_project": cache_by_project,
    }


# --------------------------------------------------------------------------
# Delivery: what actually shipped
# --------------------------------------------------------------------------

TICKET_STATUS_RANK = {"delivered": 0, "worked": 1, "referenced": 2}


def _ticket_ledger(con, f):
    w, p = _where(f)
    raw = _rows(con, f"""
        SELECT key, session_id, project, evidence, SUM(n) AS n, MIN(date) AS first_day, MAX(date) AS last_day
        FROM cc_ticket WHERE 1=1 {w} GROUP BY key, session_id, evidence""", p)
    by_key = {}
    for r in raw:
        t = by_key.setdefault(r["key"], {"key": r["key"], "sessions": set(), "projects": set(),
                                         "evidence": Counter(), "first_day": None, "last_day": None})
        t["sessions"].add(r["session_id"])
        if r["project"]:
            t["projects"].add(r["project"])
        t["evidence"][r["evidence"]] += r["n"] or 0
        for k, fn in (("first_day", min), ("last_day", max)):
            if r[k]:
                t[k] = fn(t[k], r[k]) if t[k] else r[k]

    # Noise-prefix rule: a prefix seen once with no commit/branch evidence is
    # far more likely to be a regex range or an identifier than a tracker.
    by_prefix = defaultdict(list)
    for t in by_key.values():
        by_prefix[t["key"].rsplit("-", 1)[0]].append(t)
    keep, dropped = [], []
    for prefix, group in by_prefix.items():
        strong = any(t["evidence"]["commit"] or t["evidence"]["branch"] for t in group)
        if len(group) >= 2 or strong:
            keep.extend(group)
        else:
            dropped.append(prefix)

    out = []
    for t in keep:
        ev = t["evidence"]
        status = ("delivered" if ev["commit"] else
                  "worked" if (ev["branch"] or ev["title"] or ev["prompt"] or ev["file"]) else
                  "referenced")
        out.append({"key": t["key"], "status": status, "sessions": len(t["sessions"]),
                    "projects": sorted(t["projects"]), "first_day": t["first_day"],
                    "last_day": t["last_day"], "evidence": dict(ev)})
    out.sort(key=lambda t: (TICKET_STATUS_RANK[t["status"]], -t["evidence"].get("commit", 0),
                            -t["sessions"], t["key"]))
    return out, sorted(dropped)


def delivery(con, f, sessions=None):
    sessions = sessions if sessions is not None else session_facts(con, f)
    w, p = _where(f)
    git = _one(con, f"""
        SELECT SUM(CASE WHEN kind='commit' THEN 1 ELSE 0 END) AS commits,
               SUM(CASE WHEN kind='push' THEN 1 ELSE 0 END) AS pushes,
               SUM(CASE WHEN kind='pr' THEN 1 ELSE 0 END) AS prs,
               COALESCE(SUM(files),0) AS commit_files,
               COALESCE(SUM(insertions),0) AS commit_insertions,
               COALESCE(SUM(deletions),0) AS commit_deletions
        FROM cc_git WHERE 1=1 {w}""", p)
    commits_by_project = _rows(con, f"""
        SELECT project, COUNT(*) AS commits, SUM(insertions) AS insertions, SUM(deletions) AS deletions,
               COUNT(DISTINCT session_id) AS sessions
        FROM cc_git WHERE kind='commit' {w} GROUP BY project ORDER BY commits DESC LIMIT 15""", p)
    commits_daily = _rows(con, f"""
        SELECT date, COUNT(*) AS commits, SUM(insertions) AS insertions
        FROM cc_git WHERE kind='commit' AND date IS NOT NULL {w} GROUP BY date ORDER BY date""", p)
    recent = _rows(con, f"""
        SELECT date, ts, project, branch, subject, files, insertions, deletions
        FROM cc_git WHERE kind='commit' {w} ORDER BY ts DESC LIMIT 60""", p)
    prs = _rows(con, f"SELECT date, project, subject AS url FROM cc_git WHERE kind='pr' {w} ORDER BY ts DESC LIMIT 40", p)

    by_ext = _rows(con, f"""
        SELECT file_ext, SUM(lines_added) AS added, SUM(lines_removed) AS removed, COUNT(*) AS touches
        FROM cc_tool_call WHERE name IN ({_MUT}) {w}
        GROUP BY file_ext""", list(MUTATING_TOOLS) + p)
    by_cat = defaultdict(lambda: {"added": 0, "removed": 0, "touches": 0})
    for r in by_ext:
        c = by_cat[patterns.file_category(r["file_ext"])]
        c["added"] += r["added"] or 0
        c["removed"] += r["removed"] or 0
        c["touches"] += r["touches"] or 0
    categories = [{"category": k, **v} for k, v in sorted(by_cat.items(), key=lambda kv: -kv[1]["added"])]
    source_added = sum(by_cat[c]["added"] for c in patterns.SOURCE_CATEGORIES if c in by_cat)
    source_removed = sum(by_cat[c]["removed"] for c in patterns.SOURCE_CATEGORIES if c in by_cat)

    git_cmds = Counter()
    for r in _rows(con, f"SELECT bash_command FROM cc_tool_call WHERE bash_command LIKE '%git %' {w}", p):
        for m in patterns.RE_GIT_SUBCMD.finditer(r["bash_command"] or ""):
            git_cmds[m.group(1)] += 1

    tickets, dropped = _ticket_ledger(con, f)
    timed = [s for s in sessions.values() if s["turn_ms"]]
    total_tokens = sum(s["total_tokens"] or 0 for s in sessions.values())
    active_hours = sum(s["turn_ms"] for s in timed) / 3600_000
    return {
        **git,
        "commits_by_project": commits_by_project,
        "commits_daily": commits_daily,
        "recent_commits": recent,
        "prs_list": prs,
        "line_categories": categories,
        "source_added": source_added,
        "source_removed": source_removed,
        "git_commands": [{"cmd": k, "n": v} for k, v in git_cmds.most_common(15)],
        "tickets": tickets[:150],
        "ticket_count": len(tickets),
        "tickets_delivered": sum(1 for t in tickets if t["status"] == "delivered"),
        "tickets_worked": sum(1 for t in tickets if t["status"] == "worked"),
        "dropped_prefixes": dropped[:40],
        "active_hours": round(active_hours, 1),
        "timed_sessions": len(timed),
        "time_coverage_pct": round(sum(s["total_tokens"] or 0 for s in timed) / total_tokens * 100, 1)
        if total_tokens else 0,
        "cost_usd": round(sum(s["cost_usd"] or 0 for s in sessions.values()), 2),
    }


# --------------------------------------------------------------------------
# Long horizon: history.jsonl
# --------------------------------------------------------------------------

def history(con):
    monthly = _rows(con, """
        SELECT substr(date,1,7) AS month, COUNT(*) AS prompts, COUNT(DISTINCT session_id) AS sessions,
               COUNT(DISTINCT project) AS projects, SUM(pasted) AS pasted, SUM(is_slash) AS slash
        FROM cc_history WHERE date IS NOT NULL GROUP BY month ORDER BY month""")
    totals = _one(con, """
        SELECT COUNT(*) AS prompts, MIN(date) AS first_day, MAX(date) AS last_day,
               COUNT(DISTINCT project) AS projects, SUM(pasted) AS pasted
        FROM cc_history""")
    live = {r["project"] for r in _rows(con, "SELECT DISTINCT project FROM cc_session WHERE project IS NOT NULL")}
    live_dirs = set()
    projects_dir = config.claude_projects_dir()
    if projects_dir.is_dir():
        live_dirs = {d.name for d in projects_dir.iterdir() if d.is_dir()}
    lost = []
    for r in _rows(con, """
        SELECT project, project_path, COUNT(*) AS prompts, MIN(date) AS first_day, MAX(date) AS last_day
        FROM cc_history WHERE project IS NOT NULL GROUP BY project_path ORDER BY prompts DESC"""):
        encoded = "".join(ch if ch.isalnum() else "-" for ch in (r["project_path"] or ""))
        if r["project"] not in live and encoded not in live_dirs:
            lost.append(r)
    transcript_first = _one(con, "SELECT MIN(date) AS d FROM cc_message").get("d")
    return {
        **totals,
        "monthly": monthly,
        "transcript_first_day": transcript_first,
        "prompts_before_transcripts": _one(con, "SELECT COUNT(*) AS n FROM cc_history WHERE date < ?",
                                           (transcript_first or "0000",)).get("n", 0),
        "lost_projects": lost[:25],
        "lost_project_count": len(lost),
    }


# --------------------------------------------------------------------------
# Skill mining: repeated work that could be automated
# --------------------------------------------------------------------------

def _human_prompts(con, f, min_chars=12):
    w, p = _where(f)
    return _rows(con, f"""
        SELECT uuid, session_id, project, date, ts, chars, text, is_slash, slash_name
        FROM cc_prompt WHERE {HUMAN_PROMPT} AND chars >= ? {w} ORDER BY ts""", [min_chars] + p)


def _cluster(prompts, threshold=0.45, min_size=3):
    """Greedy Jaccard clustering over content-word sets. Longest prompts seed first."""
    items = []
    for r in prompts:
        if r["is_slash"]:
            continue
        toks = patterns.prompt_tokens(r["text"])
        if len(toks) >= 6:
            items.append((toks, r))
    items.sort(key=lambda it: -len(it[0]))
    clusters = []
    for toks, r in items:
        for c in clusters:
            seed = c["seed"]
            inter = len(toks & seed)
            if inter and inter / len(toks | seed) >= threshold:
                c["members"].append(r)
                break
        else:
            clusters.append({"seed": toks, "members": [r]})
    out = []
    for c in clusters:
        if len(c["members"]) < min_size:
            continue
        m = c["members"]
        sessions = {r["session_id"] for r in m}
        projects = Counter(r["project"] for r in m if r["project"])
        out.append({
            "size": len(m), "sessions": len(sessions), "projects": [k for k, _ in projects.most_common(4)],
            "first_day": min(r["date"] for r in m if r["date"]) if any(r["date"] for r in m) else None,
            "last_day": max(r["date"] for r in m if r["date"]) if any(r["date"] for r in m) else None,
            "keywords": sorted(c["seed"])[:12],
            "examples": [{"date": r["date"], "project": r["project"], "text": (r["text"] or "")[:400]}
                         for r in sorted(m, key=lambda r: -(r["chars"] or 0))[:3]],
            "avg_chars": int(sum(r["chars"] or 0 for r in m) / len(m)),
        })
    out.sort(key=lambda c: (-c["sessions"], -c["size"]))
    return out


def _families(prompts, min_count=2):
    """Exact repeats after masking digits: the strongest slash-command signal."""
    groups = defaultdict(list)
    for r in prompts:
        if r["is_slash"]:
            continue
        groups[patterns.family_key(r["text"])].append(r)
    out = []
    for key, m in groups.items():
        sessions = {r["session_id"] for r in m}
        if len(m) < min_count or len(sessions) < 2:
            continue
        out.append({"key": key, "size": len(m), "sessions": len(sessions),
                    "projects": sorted({r["project"] for r in m if r["project"]})[:4],
                    "example": (m[0]["text"] or "")[:300],
                    "first_day": min(r["date"] for r in m if r["date"]),
                    "last_day": max(r["date"] for r in m if r["date"])})
    out.sort(key=lambda g: (-g["sessions"], -g["size"]))
    return out


def _phrases(prompts, min_sessions=4, limit=60):
    """2-4 word phrases that recur across sessions."""
    by_phrase = defaultdict(set)
    freq = Counter()
    for r in prompts:
        toks = [t for t in patterns._RE_TOKEN.findall(patterns.normalize_prompt(r["text"]))
                if len(t) > 2 and t not in patterns.STOPWORDS]
        seen = set()
        for n in (2, 3, 4):
            for i in range(len(toks) - n + 1):
                ph = " ".join(toks[i:i + n])
                freq[ph] += 1
                if ph not in seen:
                    by_phrase[ph].add(r["session_id"])
                    seen.add(ph)
    out = [{"phrase": ph, "sessions": len(s), "n": freq[ph]}
           for ph, s in by_phrase.items() if len(s) >= min_sessions and "N" not in ph.split()]
    out.sort(key=lambda x: (-x["sessions"], -x["n"], -len(x["phrase"])))
    # Drop a phrase that is a substring of a longer phrase with the same reach.
    kept = []
    for ph in out:
        if any(ph["phrase"] in k["phrase"] and k["sessions"] == ph["sessions"] for k in kept):
            continue
        kept.append(ph)
        if len(kept) >= limit:
            break
    return kept


def skill_mining(con, f):
    prompts = _human_prompts(con, f)
    clusters = _cluster(prompts)
    families = _families(prompts)
    phrases = _phrases(prompts)
    w, p = _where(f)
    slash = slash_usage(con, f, limit=40)
    agent_prompts = _rows(con, f"""
        SELECT substr(text,1,120) AS head, COUNT(*) AS n, COUNT(DISTINCT session_id) AS sessions
        FROM cc_prompt WHERE source='subagent' {w}
        GROUP BY substr(text,1,120) HAVING n > 1 ORDER BY n DESC LIMIT 20""", p)

    # Candidate = repeated multi-session work with an automatic trigger.
    candidates = []
    for c in clusters[:25]:
        candidates.append({"kind": "cluster", "score": c["sessions"] * c["size"], **c})
    for g in families[:25]:
        candidates.append({"kind": "repeat", "score": g["sessions"] * g["size"] * 2, **g})
    candidates.sort(key=lambda c: -c["score"])
    return {
        "prompt_count": len(prompts),
        "clusters": clusters[:40],
        "families": families[:40],
        "phrases": phrases,
        "slash": slash,
        "agent_prompts": agent_prompts,
        "candidates": candidates[:30],
    }


def evidence_pack(con, f, mining=None, hab=None, max_chars=60000):
    """Markdown brief handed to the model when it drafts skill proposals."""
    mining = mining or skill_mining(con, f)
    hab = hab or habits(con, f)
    w, p = _where(f)
    projects = _rows(con, f"""
        SELECT project, COUNT(DISTINCT session_id) AS sessions, COUNT(*) AS prompts
        FROM cc_prompt WHERE {HUMAN_PROMPT} {w} GROUP BY project ORDER BY prompts DESC LIMIT 15""", p)
    lines = ["# Evidence pack: how this person uses Claude Code", ""]
    if f:
        lines.append(f"Filter: {f}")
    lines += [f"Human prompts analysed: {mining['prompt_count']}",
              f"Sessions: {hab['sessions']} (interactive {hab['interactive_sessions']}, "
              f"one-prompt {hab['one_prompt_sessions']})",
              f"Median prompts per session: {hab['prompts_per_session_median']}; "
              f"model actions per prompt: {hab['actions_per_prompt']}",
              f"Correction prompts: {hab['corrections']} ({hab['correction_pct']}%); "
              f"premature sends: {hab['premature_sends']}", ""]
    lines.append("## Projects (by prompts)")
    for r in projects:
        lines.append(f"- {r['project']}: {r['prompts']} prompts, {r['sessions']} sessions")
    lines += ["", "## Intents (share of prompts)"]
    for it in hab["intents"][:14]:
        lines.append(f"- {it['intent']}: {it['n']} ({it['pct']}%)")
    lines += ["", "## Slash commands already in use"]
    for s in mining["slash"][:20]:
        lines.append(f"- {s['name']}: {s['n']} uses in {s['sessions']} sessions")
    lines += ["", "## Exact repeats (digits masked) across sessions"]
    for g in mining["families"][:20]:
        lines.append(f"- {g['size']}x in {g['sessions']} sessions ({', '.join(g['projects'])}): "
                     f"{g['example'][:200]!r}")
    lines += ["", "## Near-duplicate prompt clusters (Jaccard >= 0.45)"]
    for i, c in enumerate(mining["clusters"][:20], 1):
        lines.append(f"### Cluster {i}: {c['size']} prompts, {c['sessions']} sessions, "
                     f"projects {', '.join(c['projects'])}, {c['first_day']} to {c['last_day']}")
        lines.append(f"keywords: {' '.join(c['keywords'])}")
        for ex in c["examples"]:
            lines.append(f"- [{ex['date']} {ex['project']}] {ex['text'][:350]!r}")
    lines += ["", "## Recurring phrases (sessions, count)"]
    lines.append(", ".join(f"{p['phrase']} ({p['sessions']}/{p['n']})" for p in mining["phrases"][:40]))
    lines += ["", "## Session openers"]
    for o in hab["openers"][:10]:
        lines.append(f"- {o['kind']}: {o['n']}")
    if mining["agent_prompts"]:
        lines += ["", "## Repeated sub-agent instructions"]
        for a in mining["agent_prompts"][:10]:
            lines.append(f"- {a['n']}x: {a['head']!r}")
    text = "\n".join(lines)
    return text[:max_chars]


def build(con, f):
    """Everything light enough to ship with every /api/data call."""
    sessions = session_facts(con, f)
    return {
        "habits": habits(con, f, sessions),
        "friction": friction(con, f, sessions),
        "delivery": delivery(con, f, sessions),
        "history": history(con),
    }
