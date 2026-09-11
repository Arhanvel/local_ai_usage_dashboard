"""Small SQLite helpers shared by the aggregation modules."""


def rows(con, sql, params=()):
    return [dict(r) for r in con.execute(sql, params)]


def one(con, sql, params=()):
    r = con.execute(sql, params).fetchone()
    return dict(r) if r else {}


def scalar(con, sql, params=(), default=0):
    r = con.execute(sql, params).fetchone()
    if not r or r[0] is None:
        return default
    return r[0]


# A "user" record in a transcript is not necessarily something a person typed;
# the ingester settles that once (see patterns.is_human_prompt).
HUMAN_PROMPT = "is_human = 1"

# Display name of a session: the person's own title wins over the generated
# one. Expects cc_session aliased as `s` and a fallback column as `{fallback}`.
SESSION_TITLE = "COALESCE(s.custom_title, s.ai_title, s.slug, {fallback})"


def where(filters, prefix=""):
    """Build a WHERE fragment (starting with ' AND ') from {since, until, project}."""
    clauses, params = [], []
    if filters.get("since"):
        clauses.append(f"{prefix}date >= ?")
        params.append(filters["since"])
    if filters.get("until"):
        clauses.append(f"{prefix}date <= ?")
        params.append(filters["until"])
    if filters.get("project"):
        clauses.append(f"{prefix}project = ?")
        params.append(filters["project"])
    return (" AND " + " AND ".join(clauses) if clauses else ""), params


def median(values):
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return 0
    n = len(vals)
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def percentile(values, p):
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return 0
    idx = min(len(vals) - 1, max(0, int(round((len(vals) - 1) * p))))
    return vals[idx]
