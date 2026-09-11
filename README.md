# AI Usage Dashboard

A local dashboard over the usage history that **Claude Code** and **Cursor** already
keep on your machine: what it cost, what you asked for, how you drive it, what
shipped, and which repeated work deserves a skill. Everything runs offline against
files that are only ever opened read-only. The one optional network path is the
**Lab** tab, which can hand the evidence to your own `claude` CLI to draft skill
proposals and a findings report.

```bash
python ingest.py     # pull data into data/usage.db  (re-run any time; incremental)
python serve.py      # open http://127.0.0.1:8787
```

No dependencies: Python 3.11+ standard library only. Tests: `python -m unittest discover -s tests`.

---

## What it reads

### Claude Code — `~/.claude/`

| Source | What's in it |
|---|---|
| `projects/<project>/<session>.jsonl` | full transcripts: per-message token usage, model, tool calls, results |
| `projects/<project>/<session>/subagents/**/agent-*.jsonl` | sub-agent and workflow-agent runs, with **their own billable usage** |
| `stats-cache.json` | daily rollup that survives after old transcripts are pruned |
| `history.jsonl` | every prompt line typed at the REPL; outlives transcript retention |

From the transcripts the ingester also recovers things that are only implied:
git **commits, pushes and PR links** from recorded shell output, **issue keys**
with where each was seen (commit, branch, title, prompt, file, lookup),
**interrupts** and **context compactions**, replies that signal a **usage or rate
limit**, and for every prompt you typed: the cleaned text, whether it was a slash
command, and a set of intent buckets.

### Cursor — `%APPDATA%/Cursor/User/globalStorage/`

| Source | What's in it |
|---|---|
| `state.vscdb` → `composerHeaders` | chats: name, mode, lines added/removed, files changed, context % |
| `state.vscdb` → `cursorDiskKV` `bubbleId:*` | messages: text, tool calls, results, timings |
| `state.vscdb` → `ItemTable` `aiCodeTrackingLines` | AI-authored line attribution by file |
| `conversation-search.db` | conversation titles |
| `workspaceStorage/<id>/workspace.json` | maps a chat's workspace hash to a real folder |

**Cursor does not store usage metering locally.** Message counts, tool calls, timings
and edited lines are reliable; tokens and cost are not, and no cost is estimated for it.

---

## The tabs

| Tab | Contents |
|---|---|
| **Overview** | headline KPIs, daily token area chart, activity calendar, spend by model |
| **Activity** | messages/sessions per day, weekday×hour punch card, turn durations, prompt volume |
| **Habits** | actions per prompt, prompts per session, how sessions open, intents, corrections, premature sends, interactive vs headless, delegation economics, headless reruns, the long-horizon record from `history.jsonl` |
| **Cost & Models** | per-model token and cost breakdown, cache economics, recovered history |
| **Projects** | per-project tokens, cost, lines, model time, branches, CLI versions |
| **Tools** | every tool with error/denial rates and latency, usage over time, shell programs, MCP servers, sub-agents, skills |
| **Files & Code** | lines added/removed by file type, most-touched files, churn ratio |
| **Delivery** | commits, pushes and PRs recovered from shell output; source lines vs docs/config; a ticket ledger with evidence tiers; a value-framing box with two editable assumptions |
| **Sessions** | session size distribution, heaviest sessions, stop reasons, effort levels |
| **Friction** | errors and limits over time, interrupts and compactions, model switches, high-burn/low-tool sessions, expensive-per-tool outliers, search-heavy Opus sessions, a routing counterfactual (the same tokens priced at Haiku / Sonnet / Opus), cache by project |
| **Skills** | repeated work mined from the prompts you typed: exact repeats, near-duplicate clusters (Jaccard ≥ 0.45), recurring phrases, slash-command usage, repeated sub-agent instructions, ranked candidates |
| **Cursor** | chats, tools, modes, AI-authored lines, projects |
| **Lab** | refresh or rebuild the data, export a self-contained HTML report or a prompt corpus, ask Claude to draft skill proposals or a findings report, install a proposal |
| **Data & Sources** | where everything came from, ingest history, caveats |

Filter by date range and project in the header; every tab and every export respects
the filter. Click any column header to sort.

---

## Exports and model-written analysis

Everything on the Lab tab runs as a background job and writes under `data/`:

| Action | Output |
|---|---|
| Refresh / Rebuild | `data/usage.db` |
| Export HTML report | `data/reports/report-<stamp>.html` — the whole dashboard on one page, data inline, prints to PDF |
| Export prompt corpus | `data/reports/corpus-<stamp>.txt` — every prompt you typed, grouped by project and session |
| Propose skills | `data/skills-proposed/<name>/SKILL.md` plus a dated README index and the evidence pack that was used |
| Write findings | `data/findings/findings-<stamp>.md` plus the measured-numbers brief it was given |

The two model-written outputs run `claude -p` under your own Claude Code login with
tools disabled, a `$3` budget cap and session persistence off, so the runs never show
up in the numbers they describe. The evidence pack is built from the Skills-tab
mining plus the habit figures; you can read it first via **View evidence pack**.
A proposal is a draft written from patterns, not a tested skill: read it, then
**Install** copies it to `~/.claude/skills/<name>/`.

Model choice defaults to `sonnet`; `opus`, `haiku` and `fable` are offered.

---

## Incremental ingest

Re-running `python ingest.py` is cheap — a no-op run takes well under a second; a
full rebuild runs at roughly a minute per gigabyte of transcripts.

- **Transcripts** are append-only, so the byte offset reached last time is recorded
  per file and only newly appended bytes are decoded. Files whose size and mtime are
  unchanged are skipped entirely. A file that shrank is re-read from the start, and
  a partially-written final line is never consumed.
- **Cursor bubbles** are immutable once written, so the key list is fetched (cheap)
  and diffed against what's already stored; only genuinely new values are read.
- **history.jsonl** is numbered by line, so only the tail is parsed.

```bash
python ingest.py            # incremental
python ingest.py --full     # rebuild from scratch
python ingest.py --claude   # one source only
python ingest.py --cursor
```

The database is disposable — delete `data/usage.db` and re-ingest at any time.
Bumping `SCHEMA_VERSION` in `aidash/db.py` makes the next run rebuild automatically.
Set `AIDASH_DATA_DIR` to keep the database and exports somewhere else, and
`CLAUDE_CONFIG_DIR` to read a different Claude home.

---

## How the numbers are made

- **One API response, many transcript lines.** Claude Code writes one line per
  content block and repeats the same `usage` on each. Tokens are counted once per
  `message_id`; summing raw lines would inflate totals by 2–3× (up to 22× on one
  response).
- **"Prompts you typed" ≠ "user turns".** A `user` record also carries SDK-injected
  prompts, task notifications, slash-command bodies and the instructions handed to
  sub-agents. A turn counts as yours when its `promptSource` is typed/queued/accepted
  (older builds that omit the field: only in an interactive `cli` session and only
  when the text is not a harness-injected block), and never when it is meta,
  sidechain, or the opening turn of a nested transcript (`aidash/patterns.py`).
  Harness noise still gets a row, flagged non-human, so automated counts stay honest.
- **Error replies** are matched on the phrasing of Claude Code's own banners, and
  only on short, tool-free replies, so a reply that *discusses* rate limits is not
  counted as hitting one.
- **Cost is derived, never read.** Tokens × `aidash/pricing.json`. Cache writes are
  charged at 1.25× (5 min) / 2× (1 h) the input rate, cache reads at 0.1×. Any model
  without an `official` entry is flagged on the Overview and Data tabs. The
  `families` block names which model stands for Haiku / Sonnet / Opus in the
  Friction tab's routing counterfactual.
- **Commits** are matched on `[branch sha] subject`, which `git commit` prints and
  nothing else does; the `N files changed` line within 400 chars gives files/±lines.
  Nothing observes review, CI or deploy.
- **Tickets** are `ABC-123` keys with a blocklist for `UTF-8`-style false positives.
  Status is the strongest evidence seen: *delivered* (commit) > *worked*
  (branch/title/prompt/file) > *referenced* (looked up only). A prefix seen once with
  no commit or branch evidence is dropped as noise.
- **Source lines** = edits to code and markup extensions only; docs, config and data
  files are charted but excluded from the headline so lock files don't inflate it.
- **Model time** comes from `turn_duration` records, which older builds didn't emit;
  the Delivery tab states what share of tokens it covers.
- Dates and hours are bucketed in **local** time.

---

## Layout

```
ingest.py            CLI: pull data into SQLite
serve.py             CLI: local web server (127.0.0.1) with background jobs
aidash/
  config.py          path discovery, timestamp handling
  db.py              schema + versioned auto-rebuild
  patterns.py        every regex and lookup that turns text into categories
  claude_ingest.py   transcript parser (incremental, byte-offset resume)
  cursor_ingest.py   Cursor SQLite reader (incremental, key diff)
  sql.py             query helpers shared by the aggregation modules
  stats.py           the per-tab aggregations
  insights.py        habits, friction, delivery, history, skill mining, evidence pack
  reports.py         standalone HTML report and prompt corpus exports
  llm.py             claude -p runner, skill proposals, findings
  jobs.py            background job registry for the web UI
  pricing.json       per-model rates — edit this
web/                 index.html, app.js (shell + charts), insights.js (behaviour tabs, Lab)
tests/               synthetic-transcript tests for the whole pipeline
data/                generated: usage.db, reports/, skills-proposed/, findings/
```

## Interface

Monospace is the identity face — it carries every number, label and eyebrow, which
suits a CLI-adjacent tool and keeps digits aligned (`tabular-nums`) for free; the
grotesque is reserved for prose. Accent is petrol teal on warm-paper / blue-ink
grounds, with a six-hue data set (teal, amber, slate-blue, clay, moss, plum) and
good/warn/critical kept deliberately separate from the accent so state reads on its
own. Both light and dark are designed, driven by `prefers-color-scheme` with
`:root[data-theme]` overrides. KPIs are a hairline-separated metric strip rather
than floating cards; navigation is a left rail so dense tables get the full width.
Charts are plain SVG with no dependencies. The exported report is the same
renderer with every tab stacked and a print stylesheet.
