"""Model-written analysis through the local `claude` CLI.

The numbers on the dashboard are computed; the two things a model is good at
are (1) reading the evidence and naming the repeated work that deserves a
skill, and (2) writing the narrative findings. Both run `claude -p` under the
user's own login, with tools disabled and session persistence off so the run
never shows up in the transcripts this dashboard measures.
"""
import json
import os
import re
import shutil
import subprocess
import threading
from datetime import datetime
from pathlib import Path

from . import config, insights
from .config import slug, stamp

PROPOSALS_DIR = config.DATA_DIR / "skills-proposed"
FINDINGS_DIR = config.DATA_DIR / "findings"
MODELS = ["sonnet", "opus", "haiku", "fable"]
DEFAULT_MODEL = "sonnet"

SKILL_SYSTEM = """You are auditing how one developer uses Claude Code, from an evidence pack
built out of their own transcripts. Your job is to propose Agent Skills
(SKILL.md files) for the work that repeats.

Rules for a good proposal:
- A skill only wins where the trigger must be automatic: the person would not
  know, at that moment, that they needed it. Repeated instructions, repeated
  multi-step routines and repeated corrections qualify. One-off tasks do not.
- Prefer 3-6 proposals. Fewer, sharper proposals beat a long list.
- The description is what makes a skill fire. Pack it with the literal phrases
  the person actually types (quote them from the evidence), then say when NOT
  to use it.
- The body is instructions to a future Claude session: concrete numbered
  steps, what to check, what to hand back. No filler.
- Cite evidence: how many times, in how many sessions, in which projects,
  with one or two short verbatim quotes.
- Also list work that looks repetitive but should NOT become a skill, and
  why (a settings change, a CLAUDE.md rule or an existing command may fit
  better).
Write in plain English. Never invent counts that are not in the evidence."""

SKILL_SCHEMA = {
    "type": "object",
    "properties": {
        "proposals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "kebab-case skill name"},
                    "description": {"type": "string",
                                    "description": "Trigger-rich one-paragraph description for the frontmatter"},
                    "evidence": {"type": "string",
                                 "description": "Markdown: counts, sessions, projects, verbatim quotes"},
                    "body": {"type": "string",
                             "description": "Markdown body: ## What to do (numbered), ## When not to use this, ## Handoff"},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                },
                "required": ["name", "description", "evidence", "body", "confidence"],
            },
        },
        "not_skills": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"pattern": {"type": "string"}, "why": {"type": "string"},
                               "better_fix": {"type": "string"}},
                "required": ["pattern", "why", "better_fix"],
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["proposals", "not_skills", "summary"],
}

FINDINGS_SYSTEM = """You are a candid coach reviewing how one developer works with Claude Code,
from measured numbers and an evidence pack built out of their own transcripts.
Write a findings report in Markdown for that developer.

Structure:
1. **Headline** - three sentences: what the month looked like, the single
   biggest lever, the single biggest risk.
2. **What the numbers say** - the 6-8 measurements that matter, each with the
   figure and one line of interpretation. Use the numbers given; do not invent.
3. **Habits worth keeping** - 2-3, with evidence.
4. **Friction and waste** - ranked, each with: the pattern, the evidence
   (count, cost), the root cause as far as it can be inferred, and the fix.
   Say whether the fix is a skill, a CLAUDE.md rule, a settings change, a
   habit, or a model-routing change.
5. **Delivery** - what shipped (commits, tickets, source lines) versus what
   was spent, in plain terms.
6. **Next week** - at most five concrete actions, most valuable first.

Be specific, quote prompts sparingly and verbatim, and prefer a short
sentence to a hedge. Numbers are estimates at list price on a subscription -
say so once, not repeatedly."""


def claude_path():
    return shutil.which("claude")


def status():
    path = claude_path()
    return {"available": bool(path), "path": path, "models": MODELS, "default_model": DEFAULT_MODEL,
            "proposals_dir": str(PROPOSALS_DIR), "findings_dir": str(FINDINGS_DIR)}


def _clean_env():
    """Run the CLI as a fresh top-level process, not as a child of any session."""
    env = dict(os.environ)
    for key in list(env):
        if key.startswith("CLAUDE_CODE_") or key in ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT"):
            env.pop(key, None)
    return env


def run_claude(prompt, system_prompt, job, model=DEFAULT_MODEL, schema=None, max_budget_usd=3.0,
               timeout=900):
    """Run one non-interactive turn. Returns {text, structured, cost_usd, model}."""
    exe = claude_path()
    if not exe:
        raise RuntimeError("The `claude` CLI is not on PATH; install Claude Code to generate proposals.")
    if model not in MODELS:
        raise ValueError(f"model must be one of {MODELS}")
    # No tools means a single turn; the prompt arrives on stdin.
    cmd = [exe, "-p", "--model", model, "--output-format", "stream-json", "--verbose",
           "--tools", "", "--no-session-persistence",
           "--permission-mode", "dontAsk", "--disable-slash-commands",
           "--max-budget-usd", str(max_budget_usd), "--system-prompt", system_prompt]
    if schema:
        cmd += ["--json-schema", json.dumps(schema)]
    job.say(f"claude -p ({model}) - {len(prompt):,} chars of evidence")
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=str(config.DATA_DIR), env=_clean_env(), text=True, encoding="utf-8", errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
    except OSError:
        pass

    result = {"text": "", "structured": None, "cost_usd": None, "model": model}
    stderr_lines = []
    t = threading.Thread(target=lambda: stderr_lines.extend(proc.stderr.read().splitlines()), daemon=True)
    t.start()
    try:
        for line in proc.stdout:
            if job.cancelled:
                proc.kill()
                raise RuntimeError("cancelled")
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            kind = ev.get("type")
            if kind == "system" and ev.get("subtype") == "init":
                job.say(f"session started, model {ev.get('model', model)}")
            elif kind == "assistant":
                msg = ev.get("message") or {}
                for blk in msg.get("content") or []:
                    if isinstance(blk, dict) and blk.get("type") == "text" and blk.get("text"):
                        result["text"] = blk["text"]
                job.say("drafting...")
            elif kind == "result":
                result["cost_usd"] = ev.get("total_cost_usd")
                result["structured"] = ev.get("structured_output")
                if ev.get("result") and not result["text"]:
                    result["text"] = ev["result"]
                if ev.get("is_error"):
                    raise RuntimeError(f"claude returned an error: {str(ev.get('result') or ev)[:400]}")
        proc.wait(timeout=timeout)
    finally:
        if proc.poll() is None:
            proc.kill()
    t.join(timeout=2)
    if proc.returncode not in (0, None) and not result["text"] and not result["structured"]:
        raise RuntimeError("claude exited with %s: %s" % (proc.returncode, " ".join(stderr_lines)[-600:]))
    if result["structured"] is None and schema and result["text"]:
        # Some builds put the JSON in the text; recover it.
        m = re.search(r"\{.*\}", result["text"], re.S)
        if m:
            try:
                result["structured"] = json.loads(m.group(0))
            except ValueError:
                pass
    job.say(f"done, est. cost ${result['cost_usd'] or 0:.2f}")
    return result


# --------------------------------------------------------------------------
# Skill proposals
# --------------------------------------------------------------------------

_SAFE_NAME = re.compile(r"[^a-z0-9-]+")


def safe_name(name):
    slug = _SAFE_NAME.sub("-", (name or "").strip().lower()).strip("-")
    return slug[:60] or "unnamed-skill"


def _frontmatter(name, description):
    # A JSON string is a valid YAML double-quoted scalar; keep the real
    # characters rather than \u escapes so the file reads well in an editor.
    desc = " ".join(description.split())
    return f"---\nname: {name}\ndescription: {json.dumps(desc, ensure_ascii=False)}\n---\n"


def propose_skills(job, con, filters, model=DEFAULT_MODEL, mining=None):
    job.say("mining repeated prompts")
    mining = mining or insights.skill_mining(con, filters)
    pack = insights.evidence_pack(con, filters, mining)
    if mining["prompt_count"] < 15:
        job.say(f"only {mining['prompt_count']} human prompts in range - proposals will be thin")
    res = run_claude(pack, SKILL_SYSTEM, job, model=model, schema=SKILL_SCHEMA)
    data = res["structured"] or {}
    proposals = data.get("proposals") or []
    if not proposals:
        raise RuntimeError("the model returned no proposals; see the job log")

    day = datetime.now().strftime("%Y-%m-%d")
    PROPOSALS_DIR.mkdir(parents=True, exist_ok=True)
    written = []
    for p in proposals:
        name = safe_name(p.get("name"))
        d = PROPOSALS_DIR / name
        d.mkdir(parents=True, exist_ok=True)
        body = (_frontmatter(name, p.get("description", ""))
                + f"\n# {name}\n\n## Why this exists\n\n{p.get('evidence', '').strip()}\n\n"
                + p.get("body", "").strip() + "\n")
        (d / "SKILL.md").write_text(body, encoding="utf-8")
        meta = {"name": name, "confidence": p.get("confidence"), "generated": day, "model": model,
                "filters": filters, "description": p.get("description", "")}
        (d / "proposal.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        written.append(meta)
        job.say(f"wrote {name}/SKILL.md ({p.get('confidence')})")

    readme = [f"# Proposed skills ({day}, model {model})", "", data.get("summary", "").strip(), "",
              "| Skill | Confidence | Description |", "|---|---|---|"]
    for m in written:
        readme.append(f"| `{m['name']}` | {m['confidence']} | {' '.join(m['description'].split())[:160]} |")
    if data.get("not_skills"):
        readme += ["", "## Deliberately not skills", ""]
        for ns in data["not_skills"]:
            readme.append(f"- **{ns.get('pattern')}** - {ns.get('why')} Better: {ns.get('better_fix')}")
    readme += ["", "Install one from the dashboard's Skills tab, or copy its folder into "
                   "`~/.claude/skills/`.", ""]
    (PROPOSALS_DIR / f"README-{day}.md").write_text("\n".join(readme), encoding="utf-8")
    (PROPOSALS_DIR / "evidence-pack.md").write_text(pack, encoding="utf-8")
    return {"written": written, "not_skills": data.get("not_skills") or [],
            "summary": data.get("summary"), "cost_usd": res["cost_usd"]}


def list_proposals():
    out = []
    if not PROPOSALS_DIR.is_dir():
        return out
    installed_root = config.claude_home() / "skills"
    for d in sorted(PROPOSALS_DIR.iterdir()):
        skill = d / "SKILL.md"
        if not d.is_dir() or not skill.is_file():
            continue
        meta = {}
        mp = d / "proposal.json"
        if mp.is_file():
            try:
                meta = json.loads(mp.read_text(encoding="utf-8"))
            except ValueError:
                meta = {}
        text = skill.read_text(encoding="utf-8", errors="replace")
        desc = meta.get("description")
        if not desc:
            m = re.search(r"^description:\s*(.+)$", text, re.M)
            desc = m.group(1).strip().strip('"') if m else ""
        out.append({"name": d.name, "description": desc, "confidence": meta.get("confidence"),
                    "generated": meta.get("generated"), "model": meta.get("model"),
                    "installed": (installed_root / d.name / "SKILL.md").is_file(),
                    "mtime": datetime.fromtimestamp(skill.stat().st_mtime).isoformat(timespec="minutes")})
    return out


def read_proposal(name):
    path = PROPOSALS_DIR / safe_name(name) / "SKILL.md"
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8", errors="replace")


def install_skill(name, overwrite=False):
    """Copy a proposal into ~/.claude/skills/<name>/ where Claude Code loads it."""
    name = safe_name(name)
    src = PROPOSALS_DIR / name
    if not (src / "SKILL.md").is_file():
        raise FileNotFoundError(f"no proposal named {name}")
    dst = config.claude_home() / "skills" / name
    if dst.exists() and not overwrite:
        raise FileExistsError(f"{dst} already exists")
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src / "SKILL.md", dst / "SKILL.md")
    return str(dst)


# --------------------------------------------------------------------------
# Findings
# --------------------------------------------------------------------------

def _fmt(n):
    if isinstance(n, float):
        return f"{n:,.2f}"
    if isinstance(n, int):
        return f"{n:,}"
    return str(n)


def metrics_brief(payload):
    """Compact markdown of the measured numbers, for the findings prompt."""
    c = payload["claude"]
    o, ins = c["overview"], payload["insights"]
    h, fr, d = ins["habits"], ins["friction"], ins["delivery"]
    lines = ["# Measured numbers", ""]
    if payload.get("filters"):
        lines.append(f"Range/filter: {payload['filters']}")
    lines += [f"Period: {o.get('first_day')} to {o.get('last_day')}, {o.get('active_days')} active days",
              f"Sessions: {o.get('sessions')}; billed API calls: {_fmt(o.get('assistant_messages'))}; "
              f"human prompts: {_fmt(o.get('prompts'))}",
              f"Tokens: {_fmt(o.get('total_tokens'))} (output {_fmt(o.get('output_tokens'))}); "
              f"cache hit {o.get('cache_hit_pct')}%",
              f"Estimated list-price spend: ${_fmt(o.get('cost_usd'))} "
              f"(${o.get('avg_cost_per_active_day')} per active day)",
              f"Tool calls: {_fmt(o.get('tool_calls'))}, error rate {o.get('tool_error_rate')}%, "
              f"denials {o.get('denials')}, interrupted {o.get('interrupted')}",
              f"Lines: +{_fmt(o.get('lines_added'))} / -{_fmt(o.get('lines_removed'))} over "
              f"{_fmt(o.get('files_touched'))} files; source (code+markup) lines added {_fmt(d['source_added'])}",
              f"Model working time: {d['active_hours']} h measured on {d['timed_sessions']} sessions "
              f"({d['time_coverage_pct']}% of tokens)",
              "", "## Habits",
              f"Actions per prompt: {h['actions_per_prompt']} API calls, {h['tools_per_prompt']} tool calls",
              f"Prompts per session: median {h['prompts_per_session_median']}, p90 {h['prompts_per_session_p90']}; "
              f"one-prompt sessions {h['one_prompt_sessions']} of {h['interactive_sessions']}",
              f"Short prompts (<25 chars): {h['short_prompt_pct']}%; corrections {h['correction_pct']}%; "
              f"premature sends {h['premature_sends']} (median gap {h['premature_median_gap_s']}s)",
              f"Continuation openers: {h['continuation_opener_pct']}%; night prompts {h['night_pct']}%; "
              f"weekend {h['weekend_pct']}%",
              f"Delegation: {h['delegating_sessions']} sessions, {h['delegated_tool_share']}% of tool calls, "
              f"{h['delegated_cost_share']}% of cost; cost per tool call delegating "
              f"${h['cost_per_tool_delegating']} vs direct ${h['cost_per_tool_direct']}",
              f"Interrupts: {h['interrupts']}; compactions: {h['compactions']}; "
              f"headless reruns: {h['redundant_reruns']}",
              "Top intents: " + ", ".join(f"{i['intent']} {i['pct']}%" for i in h["intents"][:8]),
              "", "## Friction"]
    for e in fr["errors"]:
        lines.append(f"- {e['error_kind']}: {e['n']} replies in {e['sessions']} sessions "
                     f"({e['first_day']} to {e['last_day']})")
    lines += [f"Sessions switching model mid-way: {fr['model_switch_sessions']}; "
              f"zero-tool sessions {fr['zero_tool_sessions']} costing ${fr['zero_tool_cost']}",
              f"High-burn low-tool sessions: {len(fr['high_burn_low_tool'])}; "
              f"search-heavy opus sessions: {len(fr['search_heavy_opus'])}; "
              f"cost-per-tool outliers: {len(fr['cost_per_tool_outliers'])} (median ${fr['cost_per_tool_median']})"]
    for r in fr["routing"]:
        lines.append(f"- routing '{r['scope']}': actual ${r['actual']}, at haiku ${r.get('at_haiku')}, "
                     f"sonnet ${r.get('at_sonnet')}, opus ${r.get('at_opus')}")
    lines += ["", "## Delivery",
              f"Commits seen: {d.get('commits')} (+{_fmt(d.get('commit_insertions'))}/-{_fmt(d.get('commit_deletions'))}), "
              f"pushes {d.get('pushes')}, PRs {d.get('prs')}",
              f"Tickets: {d['ticket_count']} ({d['tickets_delivered']} delivered, {d['tickets_worked']} worked)",
              "Lines by category: " + ", ".join(f"{x['category']} +{_fmt(x['added'])}" for x in d["line_categories"]),
              "", "## Models"]
    for m in c["models"][:6]:
        lines.append(f"- {m['model']}: {_fmt(m['messages'])} calls, ${m['cost_usd']}, price {m['price_confidence']}")
    lines += ["", "## Projects (top by cost)"]
    for p in c["projects"][:8]:
        lines.append(f"- {p['project']}: ${p['cost_usd']}, {p['sessions']} sessions, +{_fmt(p['lines_added'])} lines")
    lines += ["", "## Heaviest sessions"]
    for s in c["sessions"][:6]:
        lines.append(f"- {s['title'][:70]} ({s['project']}): ${s['cost_usd']}, {s['messages']} calls, {s['tool_uses']} tools")
    return "\n".join(lines)


def write_findings(job, con, filters, payload, model=DEFAULT_MODEL, mining=None):
    job.say("assembling measured numbers and evidence")
    brief = metrics_brief(payload)
    pack = insights.evidence_pack(con, filters, mining, payload["insights"]["habits"], max_chars=35000)
    prompt = brief + "\n\n" + pack
    res = run_claude(prompt, FINDINGS_SYSTEM, job, model=model)
    text = (res["text"] or "").strip()
    if not text:
        raise RuntimeError("the model returned no text")
    FINDINGS_DIR.mkdir(parents=True, exist_ok=True)
    when = stamp()
    label = f"findings-{when}"
    if filters.get("project"):
        label += "-" + slug(filters["project"])
    path = FINDINGS_DIR / f"{label}.md"
    header = (f"<!-- generated {when} by claude ({model}); filters {json.dumps(filters)} -->\n\n")
    path.write_text(header + text + "\n", encoding="utf-8")
    (FINDINGS_DIR / f"{label}.brief.md").write_text(prompt, encoding="utf-8")
    job.say(f"wrote {path.name}")
    return {"file": path.name, "url": f"/api/findings/{path.name}", "cost_usd": res["cost_usd"],
            "chars": len(text)}


def list_findings():
    if not FINDINGS_DIR.is_dir():
        return []
    out = []
    for p in sorted(FINDINGS_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
        if p.name.endswith(".brief.md"):
            continue
        out.append({"file": p.name, "size": p.stat().st_size,
                    "mtime": datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="minutes")})
    return out


def read_finding(name):
    path = FINDINGS_DIR / Path(name).name
    if path.suffix != ".md" or not path.is_file():
        return None
    return path.read_text(encoding="utf-8", errors="replace")
