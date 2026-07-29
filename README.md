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

## Accuracy notes

These are the places where the numbers need a caveat. They're surfaced in the UI too.

- **One API response spans many transcript lines, each repeating the same usage.**
  Claude Code writes one line per content block (thinking, text, and each `tool_use`),
  all sharing a `message_id`, and stamps the *same* `usage` object on every one. In
  this dataset that's 43,923 lines for 18,921 actual API calls — one response was
  split across 22 lines. Tokens are counted **once per `message_id`**; summing the raw
  lines inflates every total by ~2.3× on average.

  Claude Code's own `stats-cache.json` appears to sum the raw lines (a naive re-parse
  reproduces its numbers *exactly* on days with intact transcripts), so those rollups
  are inflated the same way and are **not** comparable to the deduplicated totals.
- **"Model replies" is not "your messages".** A billed API call happens on every step
  of an agent loop, so the model replies far more often than you type — 18,995 replies
  against 594 typed prompts here. Anything counting model activity is labelled
  *model replies* / *API calls*; only *prompts you typed* is you.

  A transcript's `user` turns are also not all yours: they carry SDK-injected prompts,
  task notifications, and the instructions handed to sub-agents. Only
  `promptSource` of `typed`, `queued` or `suggestion_accepted` is counted as human —
  1,631 of the 2,225 user records are automated.
- **Cost is derived, not recorded.** Token counts are real (straight from the API's
  `usage` block); the dollar figure is those tokens × `aidash/pricing.json`. Only
  entries marked `"confidence": "official"` are verified list prices — the rest are
  assumptions you should correct. Edit the file and re-run
  `python ingest.py --full` to recompute.
- **Tool "latency"** is the transcript gap between a tool request and its result. If
  the call waited on a permission prompt, that gap includes *your* idle time — one
  Bash call here measures 11 hours. The table shows a median alongside the mean.
- **Reasoning text isn't persisted.** Thinking blocks are counted, but the text is
  stored empty (signature only), so there is no thinking-character metric.
- **Cursor timestamps:** only ~2% of messages carry real timing data. The rest are
  dated from their chat's creation day, so a chat spanning a week lands entirely on
  its start date. The hour-of-day chart uses exact timestamps only.
- **Cursor's AI-line tracker is a rolling buffer** capped at 10,000 lines — a recent
  sample, not a lifetime total.
- **Pruned history** from `stats-cache.json` is shown in its own table on the
  Cost tab and is deliberately excluded from headline totals, since those days have
  no per-message rows and would otherwise double-count.
- Dates and hours are bucketed in **local** time.

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
