"""Exports: a self-contained HTML report and a readable prompt corpus.

The report reuses the dashboard's own renderer. The page payload is embedded
as JSON and app.js runs in "report mode": every tab rendered in sequence, no
controls, no fetches - one file that opens anywhere and prints cleanly.
"""
import json
from datetime import datetime
from html import escape

from . import config
from .config import slug as _slug, stamp as _stamp
from .sql import HUMAN_PROMPT, SESSION_TITLE, rows as _rows, where as _where

REPORTS_DIR = config.DATA_DIR / "reports"


def _script_json(obj):
    """JSON safe to inline in a <script>: `</` would end the block early."""
    return json.dumps(obj, default=str).replace("</", "<\\/")


def report_html(payload, title):
    css = (config.WEB_DIR / "style.css").read_text(encoding="utf-8")
    # insights.js must come first: app.js reads window.AIDASH_EXTRA at load.
    js = "\n".join((config.WEB_DIR / name).read_text(encoding="utf-8")
                   for name in ("insights.js", "app.js"))
    generated = datetime.now().isoformat(timespec="minutes")
    boot = {"title": title, "generated": generated, "filters": payload.get("filters") or {}}
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<style>
{css}
</style>
</head>
<body class="report">
<div class="shell">
  <div class="content">
    <header class="bar">
      <div class="bar-title">
        <h1 id="page-title">{escape(title)}</h1>
        <p id="page-sub">Generated {generated} from local Claude Code and Cursor history. Costs are list-price estimates.</p>
      </div>
      <div class="controls"><button class="primary" type="button" onclick="print()">Print / PDF</button></div>
    </header>
    <main><div id="app"></div></main>
  </div>
</div>
<div class="tip" id="tip" role="status"></div>
<script>window.__AIDASH_REPORT__ = {_script_json(boot)};
window.__AIDASH_DATA__ = {_script_json(payload)};</script>
<script>
{js}
</script>
</body>
</html>
"""


def save_report(payload, title=None):
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    f = payload.get("filters") or {}
    parts = ["report", _stamp()]
    if f.get("project"):
        parts.append(_slug(f["project"]))
    if f.get("since") or f.get("until"):
        parts.append(f"{f.get('since') or 'start'}_to_{f.get('until') or 'now'}")
    name = "-".join(parts) + ".html"
    title = title or ("AI usage - " + (f.get("project") or "all projects")
                      + (f" - {f.get('since') or ''}..{f.get('until') or ''}" if f.get("since") or f.get("until") else ""))
    path = REPORTS_DIR / name
    path.write_text(report_html(payload, title), encoding="utf-8")
    return {"file": name, "url": f"/exports/{name}", "path": str(path), "bytes": path.stat().st_size}


def corpus_text(con, filters, max_prompt_chars=2500):
    """Every human prompt, grouped by project and session, readable end to end.

    This is the substrate a person (or a model) reads to find repeated work;
    the numbers say *that* something repeats, the corpus says *what*.
    """
    w, p = _where(filters, "p.")
    rows = _rows(con, f"""
        SELECT p.project, p.session_id, p.ts, p.text, p.chars, p.is_slash, p.slash_name,
               {SESSION_TITLE.format(fallback="''")} AS title, s.git_branch, s.cwd
        FROM cc_prompt p LEFT JOIN cc_session s ON s.session_id = p.session_id
        WHERE p.{HUMAN_PROMPT} {w}
        ORDER BY p.project, p.session_id, p.ts""", p)
    out = [f"# Prompt corpus - {len(rows)} human prompts - generated {datetime.now().isoformat(timespec='minutes')}",
           f"# filters: {json.dumps(filters)}", ""]
    cur_proj = cur_sess = None
    n = 0
    for r in rows:
        if r["project"] != cur_proj:
            cur_proj = r["project"]
            out += ["", "#" * 100, f"# PROJECT {cur_proj}", "#" * 100]
        if r["session_id"] != cur_sess:
            cur_sess = r["session_id"]
            n = 0
            out += ["", "=" * 100,
                    f"SESSION {cur_sess[:8]}  {r['title'][:80]}  cwd={r['cwd'] or ''}  branch={r['git_branch'] or ''}",
                    "=" * 100]
        n += 1
        tag = f"SLASH {r['slash_name']}" if r["is_slash"] else "typed"
        out.append(f"[{n:02d}] {(r['ts'] or '')[:19]}  {tag}")
        text = r["text"] or ""
        if len(text) > max_prompt_chars:
            text = text[:max_prompt_chars] + f"\n...[truncated {r['chars'] - max_prompt_chars} chars]"
        out.extend("     " + line for line in text.splitlines())
        out.append("")
    return "\n".join(out)


def save_corpus(con, filters):
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    f = filters or {}
    name = "-".join(x for x in ["corpus", _stamp(), _slug(f.get("project", ""))] if x) + ".txt"
    path = REPORTS_DIR / name
    path.write_text(corpus_text(con, f), encoding="utf-8")
    return {"file": name, "url": f"/exports/{name}", "path": str(path), "bytes": path.stat().st_size}


def list_exports():
    if not REPORTS_DIR.is_dir():
        return []
    out = []
    for p in sorted(REPORTS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if p.suffix not in (".html", ".txt", ".md"):
            continue
        out.append({"file": p.name, "kind": "report" if p.suffix == ".html" else "corpus",
                    "bytes": p.stat().st_size,
                    "mtime": datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="minutes")})
    return out
