"""SQLite schema and connection helpers."""
import sqlite3

from . import config

# Bump when the schema changes; init() then rebuilds the DB from source files.
SCHEMA_VERSION = 7

SCHEMA = """
PRAGMA journal_mode=WAL;

-- Incremental-ingest bookkeeping. For JSONL we remember the byte offset we
-- stopped at, so a re-run only reads bytes appended since last time.
CREATE TABLE IF NOT EXISTS ingest_state (
    source      TEXT NOT NULL,
    key         TEXT NOT NULL,
    size        INTEGER,
    mtime       REAL,
    byte_offset INTEGER DEFAULT 0,
    rows        INTEGER DEFAULT 0,
    updated_at  TEXT,
    PRIMARY KEY (source, key)
);

CREATE TABLE IF NOT EXISTS ingest_run (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT,
    finished_at TEXT,
    source      TEXT,
    files_seen  INTEGER,
    files_read  INTEGER,
    rows_added  INTEGER,
    note        TEXT
);

/* ---------------- Claude Code ---------------- */

CREATE TABLE IF NOT EXISTS cc_session (
    session_id   TEXT PRIMARY KEY,
    project      TEXT,
    project_dir  TEXT,
    cwd          TEXT,
    slug         TEXT,
    ai_title     TEXT,
    git_branch   TEXT,
    version      TEXT,
    entrypoint   TEXT,
    first_ts     TEXT,
    last_ts      TEXT
);

CREATE TABLE IF NOT EXISTS cc_message (
    uuid            TEXT PRIMARY KEY,
    session_id      TEXT,
    parent_uuid     TEXT,
    ts              TEXT,
    date            TEXT,
    hour            INTEGER,
    dow             INTEGER,
    type            TEXT,
    role            TEXT,
    model           TEXT,
    is_sidechain    INTEGER DEFAULT 0,
    effort          TEXT,
    request_id      TEXT,
    message_id      TEXT,   -- the API response id (msg_...); 1 per billed call
    input_tokens    INTEGER DEFAULT 0,
    output_tokens   INTEGER DEFAULT 0,
    cache_write     INTEGER DEFAULT 0,
    cache_read      INTEGER DEFAULT 0,
    eph_5m          INTEGER DEFAULT 0,
    eph_1h          INTEGER DEFAULT 0,
    service_tier    TEXT,
    speed           TEXT,
    stop_reason     TEXT,
    thinking_blocks INTEGER DEFAULT 0,
    thinking_chars  INTEGER DEFAULT 0,
    text_chars      INTEGER DEFAULT 0,
    tool_uses       INTEGER DEFAULT 0,
    web_search_reqs INTEGER DEFAULT 0,
    web_fetch_reqs  INTEGER DEFAULT 0,
    is_api_error    INTEGER DEFAULT 0,
    api_error_status TEXT,
    -- One API response is written to the transcript as several lines (thinking,
    -- text, one per tool_use) and EVERY line repeats the same usage block.
    -- Only one row per request_id keeps the tokens; the rest are zeroed and
    -- flagged here, so summing token columns never multi-counts a request.
    usage_dupe      INTEGER DEFAULT 0,
    cost_usd        REAL DEFAULT 0,
    skill           TEXT,
    agent_id        TEXT,
    agent_type      TEXT,
    transcript      TEXT DEFAULT 'main',   -- main | subagent | workflow
    workflow_id     TEXT,
    project         TEXT,
    cwd             TEXT,
    git_branch      TEXT,
    version         TEXT
);
CREATE INDEX IF NOT EXISTS ix_msg_transcript ON cc_message(transcript);
CREATE INDEX IF NOT EXISTS ix_msg_date    ON cc_message(date);
CREATE INDEX IF NOT EXISTS ix_msg_session ON cc_message(session_id);
CREATE INDEX IF NOT EXISTS ix_msg_project ON cc_message(project);
CREATE INDEX IF NOT EXISTS ix_msg_model   ON cc_message(model);
CREATE INDEX IF NOT EXISTS ix_msg_request ON cc_message(request_id);
CREATE INDEX IF NOT EXISTS ix_msg_msgid   ON cc_message(message_id);

CREATE TABLE IF NOT EXISTS cc_tool_call (
    tool_use_id  TEXT PRIMARY KEY,
    message_uuid TEXT,
    session_id   TEXT,
    project      TEXT,
    ts           TEXT,
    date         TEXT,
    hour         INTEGER,
    name         TEXT,
    is_mcp       INTEGER DEFAULT 0,
    mcp_server   TEXT,
    input_chars  INTEGER DEFAULT 0,
    input_json   TEXT,
    skill        TEXT,
    transcript   TEXT DEFAULT 'main',
    agent_id     TEXT,
    -- result side (filled when the matching tool_result is seen)
    has_result   INTEGER DEFAULT 0,
    is_error     INTEGER DEFAULT 0,
    interrupted  INTEGER DEFAULT 0,
    denial_kind  TEXT,
    result_ts    TEXT,
    latency_ms   INTEGER,
    duration_ms  INTEGER,
    result_chars INTEGER DEFAULT 0,
    file_path    TEXT,
    file_ext     TEXT,
    lines_added  INTEGER DEFAULT 0,
    lines_removed INTEGER DEFAULT 0,
    bash_command TEXT,
    bash_program TEXT
);
CREATE INDEX IF NOT EXISTS ix_tool_name    ON cc_tool_call(name);
CREATE INDEX IF NOT EXISTS ix_tool_date    ON cc_tool_call(date);
CREATE INDEX IF NOT EXISTS ix_tool_project ON cc_tool_call(project);
CREATE INDEX IF NOT EXISTS ix_tool_file    ON cc_tool_call(file_path);
CREATE INDEX IF NOT EXISTS ix_tool_session ON cc_tool_call(session_id);

CREATE TABLE IF NOT EXISTS cc_prompt (
    uuid        TEXT PRIMARY KEY,
    session_id  TEXT,
    project     TEXT,
    ts          TEXT,
    date        TEXT,
    hour        INTEGER,
    dow         INTEGER,
    prompt_id   TEXT,
    source      TEXT,
    origin_kind TEXT,
    chars       INTEGER DEFAULT 0,
    words       INTEGER DEFAULT 0,
    preview     TEXT
);
CREATE INDEX IF NOT EXISTS ix_prompt_date    ON cc_prompt(date);
CREATE INDEX IF NOT EXISTS ix_prompt_session ON cc_prompt(session_id);

CREATE TABLE IF NOT EXISTS cc_turn (
    uuid          TEXT PRIMARY KEY,
    session_id    TEXT,
    project       TEXT,
    ts            TEXT,
    date          TEXT,
    duration_ms   INTEGER,
    message_count INTEGER
);
CREATE INDEX IF NOT EXISTS ix_turn_session ON cc_turn(session_id);

CREATE TABLE IF NOT EXISTS cc_subagent (
    tool_use_id      TEXT PRIMARY KEY,
    session_id       TEXT,
    project          TEXT,
    ts               TEXT,
    date             TEXT,
    agent_type       TEXT,
    resolved_model   TEXT,
    status           TEXT,
    total_tokens     INTEGER DEFAULT 0,
    total_duration_ms INTEGER DEFAULT 0,
    tool_use_count   INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS cc_event (
    uuid       TEXT PRIMARY KEY,
    session_id TEXT,
    project    TEXT,
    ts         TEXT,
    date       TEXT,
    kind       TEXT,      -- attachment type / system subtype / queue op
    subkind    TEXT,
    detail     TEXT
);
CREATE INDEX IF NOT EXISTS ix_event_kind    ON cc_event(kind);
CREATE INDEX IF NOT EXISTS ix_event_session ON cc_event(session_id);
CREATE INDEX IF NOT EXISTS ix_sub_session   ON cc_subagent(session_id);

-- Claude Code prunes old transcripts but keeps a rolled-up stats-cache.json.
-- Those days are gone from cc_message, so we keep them separately rather than
-- mixing pruned aggregates into per-message tables.
CREATE TABLE IF NOT EXISTS cc_legacy_daily (
    date        TEXT PRIMARY KEY,
    messages    INTEGER DEFAULT 0,
    sessions    INTEGER DEFAULT 0,
    tool_calls  INTEGER DEFAULT 0,
    tokens      INTEGER DEFAULT 0,
    models_json TEXT
);

CREATE TABLE IF NOT EXISTS cc_legacy_model (
    model            TEXT PRIMARY KEY,
    input_tokens     INTEGER DEFAULT 0,
    output_tokens    INTEGER DEFAULT 0,
    cache_read       INTEGER DEFAULT 0,
    cache_write      INTEGER DEFAULT 0,
    web_searches     INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS cc_legacy_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

/* ---------------- Cursor ---------------- */

CREATE TABLE IF NOT EXISTS cur_session (
    composer_id       TEXT PRIMARY KEY,
    name              TEXT,
    workspace_id      TEXT,
    created_at        TEXT,
    last_updated_at   TEXT,
    date              TEXT,
    mode              TEXT,
    force_mode        TEXT,
    is_archived       INTEGER DEFAULT 0,
    is_subagent       INTEGER DEFAULT 0,
    parent_composer   TEXT,
    context_usage_pct REAL,
    lines_added       INTEGER DEFAULT 0,
    lines_removed     INTEGER DEFAULT 0,
    files_changed     INTEGER DEFAULT 0,
    repo_path         TEXT,
    project           TEXT,
    branch            TEXT,
    subtitle          TEXT,
    message_count     INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS cur_message (
    bubble_key    TEXT PRIMARY KEY,
    composer_id   TEXT,
    bubble_id     TEXT,
    role          TEXT,
    text_chars    INTEGER DEFAULT 0,
    is_agentic    INTEGER DEFAULT 0,
    thinking_blocks INTEGER DEFAULT 0,
    code_blocks   INTEGER DEFAULT 0,
    tool_name     TEXT,
    tool_status   TEXT,
    tool_error    INTEGER DEFAULT 0,
    user_decision TEXT,
    input_tokens  INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    ts            TEXT,
    date          TEXT,
    hour          INTEGER,   -- only set when the bubble had real timing data
    dow           INTEGER,
    duration_ms   INTEGER,
    -- 'exact'   = from the bubble's own timingInfo
    -- 'session' = inferred from the parent chat's creation date
    ts_source     TEXT
);
CREATE INDEX IF NOT EXISTS ix_cur_msg_composer ON cur_message(composer_id);
CREATE INDEX IF NOT EXISTS ix_cur_msg_date     ON cur_message(date);
CREATE INDEX IF NOT EXISTS ix_cur_msg_tool     ON cur_message(tool_name);

CREATE TABLE IF NOT EXISTS cur_ai_line (
    hash        TEXT PRIMARY KEY,
    composer_id TEXT,
    source      TEXT,
    file_name   TEXT,
    file_ext    TEXT
);
CREATE INDEX IF NOT EXISTS ix_cur_line_ext ON cur_ai_line(file_ext);

CREATE TABLE IF NOT EXISTS cur_conversation (
    id          TEXT PRIMARY KEY,
    source      TEXT,
    title       TEXT,
    updated_at  TEXT,
    date        TEXT,
    is_archived INTEGER DEFAULT 0
);
"""


def connect(path=None) -> sqlite3.Connection:
    path = path or config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init(con: sqlite3.Connection) -> None:
    """Create the schema, rebuilding from scratch if the version moved.

    Everything here is derived from files that still exist on disk, so a
    destructive re-create is safe - the next ingest repopulates it.
    """
    current = con.execute("PRAGMA user_version").fetchone()[0]
    if current and current != SCHEMA_VERSION:
        names = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for t in names:
            con.execute(f'DROP TABLE IF EXISTS "{t}"')
        con.commit()
    con.executescript(SCHEMA)
    con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    con.commit()


def reset(con: sqlite3.Connection, prefix: str) -> None:
    """Drop ingested rows for one source so the next run is a full rebuild."""
    tables = [r["name"] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE ?", (prefix + "%",)
    )]
    for t in tables:
        con.execute(f'DELETE FROM "{t}"')
    src = "claude" if prefix == "cc_" else "cursor"
    con.execute("DELETE FROM ingest_state WHERE source=?", (src,))
    con.commit()
