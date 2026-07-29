# AI Usage Dashboard

A local dashboard over the usage history that **Claude Code** and **Cursor** already
keep on your machine. Everything runs offline: no network calls, no telemetry, and
the source files are only ever opened read-only.

```bash
python ingest.py     # pull data into data/usage.db  (re-run any time; incremental)
python serve.py      # open http://127.0.0.1:8787
```

No dependencies — Python 3.11+ standard library only.

---

## What it reads

### Claude Code — `~/.claude/`

| Source | What's in it |
|---|---|
| `projects/<project>/<session>.jsonl` | full transcripts: per-message token usage, model, tool calls, results |
| `projects/<project>/<session>/subagents/agent-*.jsonl` | sub-agent runs — **their own billable token usage** |
| `.../subagents/workflows/<wf>/agent-*.jsonl` | workflow-agent runs |
| `stats-cache.json` | daily rollup that survives after old transcripts are pruned |

Per assistant message the transcripts record `input_tokens`, `output_tokens`,
`cache_creation_input_tokens`, `cache_read_input_tokens` (split into 5-minute and
1-hour ephemeral buckets), model, stop reason, service tier, reasoning effort,
Claude Code version, git branch and cwd. Tool calls carry their inputs, results,
error flags, edit patches and permission-denial reasons.

### Cursor — `%APPDATA%/Cursor/User/globalStorage/`

| Source | What's in it |
|---|---|
| `state.vscdb` → `composerHeaders` | 237 chats: name, mode, lines added/removed, files changed, context %, sub-agent links |
| `state.vscdb` → `cursorDiskKV` `bubbleId:*` | ~72k messages: text, tool calls, results, timings |
| `state.vscdb` → `ItemTable` `aiCodeTrackingLines` | AI-authored line attribution by file |
| `conversation-search.db` | conversation titles |
| `workspaceStorage/<id>/workspace.json` | maps a chat's workspace hash to a real folder |

**Cursor does not store usage metering locally.** `tokenCount` is zero on almost
every message because Cursor meters server-side. Message counts, tool calls,
timings and edited lines are reliable; tokens and cost are not, and no cost is
estimated for Cursor.

---

## Incremental ingest

Re-running `python ingest.py` is cheap — a no-op run takes well under a second.

- **Transcripts** are append-only, so the byte offset reached last time is recorded
  per file and only newly appended bytes are decoded. Files whose size and mtime are
  unchanged are skipped entirely. A file that shrank is re-read from the start, and
  a partially-written final line is never consumed.
- **Cursor bubbles** are immutable once written, so the key list is fetched (cheap)
  and diffed against what's already stored; only genuinely new values are read.

```bash
python ingest.py            # incremental
python ingest.py --full     # rebuild from scratch
python ingest.py --claude   # one source only
python ingest.py --cursor
```

The database is disposable — delete `data/usage.db` and re-ingest at any time.
Bumping `SCHEMA_VERSION` in `aidash/db.py` makes the next run rebuild automatically.

---

## The tabs

| Tab | Contents |
|---|---|
| **Overview** | headline KPIs, daily token area chart, activity calendar, spend by model |
| **Activity** | messages/sessions per day, weekday×hour punch card, turn durations, prompt volume |
| **Cost & Models** | per-model token and cost breakdown, cache economics, recovered history |
| **Projects** | per-project tokens, cost, lines, model time, branches, CLI versions |
| **Tools** | every tool with error/denial rates and latency, usage over time, shell programs, MCP servers, sub-agents, skills |
| **Files & Code** | lines added/removed by file type, most-touched files, churn ratio |
| **Sessions** | session size distribution, heaviest sessions, stop reasons, effort levels |
| **Cursor** | chats, tools, modes, AI-authored lines, projects |
| **Data & Sources** | where everything came from, ingest history, caveats |

Filter by date range and project in the header; click any column header to sort.
**Refresh data** re-runs the incremental ingest without leaving the page.

---

## Layout

```
ingest.py            CLI: pull data into SQLite
serve.py             CLI: local read-only web server (127.0.0.1)
aidash/
  config.py          path discovery, timestamp handling
  db.py              schema + versioned auto-rebuild
  claude_ingest.py   transcript parser (incremental, byte-offset resume)
  cursor_ingest.py   Cursor SQLite reader (incremental, key diff)
  stats.py           every aggregation the dashboard shows
  pricing.json       per-model rates — edit this
web/                 index.html, app.js, style.css (hand-rolled SVG charts)
data/usage.db        generated
```

## Interface

Monospace is the identity face — it carries every number, label and eyebrow, which
suits a CLI-adjacent tool and keeps digits aligned (`tabular-nums`) for free; the
grotesque is reserved for prose. Accent is petrol teal on warm-paper / blue-ink
grounds, with a six-hue data set (teal, amber, slate-blue, clay, moss, plum) and
good/warn/critical kept deliberately separate from the accent so state reads on its
own. Series colours are defined as CSS custom properties and read back in JS, so
both themes live in one place.

Both light and dark are designed, driven by `prefers-color-scheme` with
`:root[data-theme]` overrides that win in either direction. KPIs are a
hairline-separated metric strip rather than floating cards; navigation is a left rail
so dense tables get the full width. Charts are plain SVG with no dependencies — faint
grids, area fills, and an emphasised endpoint on the latest reading.
