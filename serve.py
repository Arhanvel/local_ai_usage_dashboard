#!/usr/bin/env python3
"""Local dashboard server. Binds to 127.0.0.1 only - nothing leaves the machine.

    python serve.py                # http://127.0.0.1:8787
    python serve.py --port 9000
    python serve.py --no-browser

Long-running work (re-ingest, exports, model-written proposals) runs as a
background job; the page polls /api/jobs/<id> for progress.
"""
import argparse
import json
import mimetypes
import sys
import threading
import webbrowser
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from aidash import claude_ingest, config, cursor_ingest, db, insights, jobs, llm, reports, stats

FILTER_KEYS = ("since", "until", "project")

# The behaviour analyses and skill mining are the heavy queries; keep a few
# recent (kind, filters, ingest run) results so flipping between two filters
# does not recompute every time. Cleared after every ingest.
_cache = OrderedDict()
_cache_lock = threading.Lock()
CACHE_SIZE = 16

# Admission to the job registry: checking "is an ingest running" and starting
# the job must be one step, or two clicks a few ms apart start two ingests.
_admit_lock = threading.Lock()

# The server is loopback-only, but a page from any other origin can still fire
# requests at 127.0.0.1 from the same browser (and DNS rebinding can point a
# hostname at it). Only these hosts, and only same-origin pages, are served.
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _filters(src):
    return {k: src.get(k) for k in FILTER_KEYS if src.get(k)}


def _open():
    con = db.connect()
    db.init(con)
    return con


def _latest_run(con):
    return con.execute("SELECT COALESCE(MAX(id),0) FROM ingest_run").fetchone()[0]


def cached(kind, con, f, compute):
    """Memoise compute(con, f) per (kind, filters, newest ingest run)."""
    key = (kind, json.dumps(f, sort_keys=True), _latest_run(con))
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None:
            _cache.move_to_end(key)
            return hit
    hit = compute(con, f)
    with _cache_lock:
        _cache[key] = hit
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)
    return hit


# ---------------------------------------------------------------- job bodies

def job_refresh(job, full):
    job.say("full rebuild" if full else "incremental ingest")
    con = _open()
    try:
        if full:
            db.reset(con, "cc_")
            db.reset(con, "cur_")
        job.say(f"Claude Code <- {config.claude_projects_dir()}")
        res_c = claude_ingest.run(con, full=full, verbose=False)
        job.say(f"  {res_c.get('files_read', 0)}/{res_c.get('files_seen', 0)} transcripts, "
                f"{res_c.get('rows', 0):,} rows" if res_c.get("ok") else f"  skipped: {res_c.get('error')}")
        job.say(f"Cursor <- {config.cursor_global_storage() or 'not found'}")
        res_u = cursor_ingest.run(con, full=full, verbose=False)
        job.say(f"  {res_u.get('bubbles', 0):,} new messages" if res_u.get("ok") else f"  skipped: {res_u.get('error')}")
    finally:
        con.close()
    with _cache_lock:
        _cache.clear()
    return {"claude": res_c, "cursor": res_u}


def job_report(job, filters, title):
    con = _open()
    try:
        job.say("building payload")
        payload = stats.build_payload(con, filters, with_insights=True)
        job.say("mining repeated prompts")
        payload["skills"] = cached("skills", con, filters, insights.skill_mining)
        out = reports.save_report(payload, title or None)
    finally:
        con.close()
    job.say(f"wrote {out['file']} ({out['bytes'] / 1e6:.1f} MB)")
    return out


def job_corpus(job, filters):
    con = _open()
    try:
        out = reports.save_corpus(con, filters)
    finally:
        con.close()
    job.say(f"wrote {out['file']} ({out['bytes'] / 1e3:.0f} kB)")
    return out


def job_propose(job, filters, model):
    con = _open()
    try:
        mining = cached("skills", con, filters, insights.skill_mining)
        return llm.propose_skills(job, con, filters, model=model, mining=mining)
    finally:
        con.close()


def job_findings(job, filters, model):
    con = _open()
    try:
        payload = stats.build_payload(con, filters, with_insights=True)
        mining = cached("skills", con, filters, insights.skill_mining)
        return llm.write_findings(job, con, filters, payload, model=model, mining=mining)
    finally:
        con.close()


JOB_KINDS = {
    "refresh": ("Refresh data", lambda body: (job_refresh, [False])),
    "refresh-full": ("Rebuild database", lambda body: (job_refresh, [True])),
    "report": ("Export HTML report", lambda body: (job_report, [_filters(body), body.get("title")])),
    "corpus": ("Export prompt corpus", lambda body: (job_corpus, [_filters(body)])),
    "propose-skills": ("Propose skills", lambda body: (job_propose, [_filters(body), body.get("model") or llm.DEFAULT_MODEL])),
    "findings": ("Write findings", lambda body: (job_findings, [_filters(body), body.get("model") or llm.DEFAULT_MODEL])),
}
EXCLUSIVE = {"refresh", "refresh-full"}


class Handler(BaseHTTPRequestHandler):
    server_version = "aidash"

    def log_message(self, fmt, *a):  # quieter console: API calls only
        if "/api/" in (self.path or "") and "/api/jobs/" not in self.path:
            sys.stderr.write(f"  {self.command} {self.path}\n")

    # -- helpers -----------------------------------------------------------
    def _send(self, code, body, ctype="application/json; charset=utf-8", download=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, default=str).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{download}"')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    def _file(self, root, rel, download=False):
        root = Path(root).resolve()
        path = (root / unquote(rel)).resolve()
        if root not in path.parents or not path.is_file():
            return self._send(404, {"error": "not found"})
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._send(200, path.read_bytes(), ctype, download=path.name if download else None)

    def _origin_ok(self):
        """Reject requests that do not come from this loopback origin."""
        host = urlparse("//" + (self.headers.get("Host") or "")).hostname or ""
        if host not in LOOPBACK_HOSTS:
            return False
        origin = self.headers.get("Origin")
        if origin and origin != "null":
            o = urlparse(origin)
            if o.scheme != "http" or (o.hostname or "") not in LOOPBACK_HOSTS \
                    or (o.port or 80) != self.server.server_address[1]:
                return False
        return True

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            data = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    # -- GET ---------------------------------------------------------------
    def do_GET(self):
        if not self._origin_ok():
            return self._send(403, {"error": "loopback origin required"})
        parsed = urlparse(self.path)
        route = parsed.path
        q = {k: v[0] for k, v in parse_qs(parsed.query).items() if v and v[0]}

        if route in ("/", "/index.html"):
            return self._file(config.WEB_DIR, "index.html")
        if route.startswith("/exports/"):
            return self._file(reports.REPORTS_DIR, route[len("/exports/"):], download="download" in q)
        if not route.startswith("/api/"):
            return self._file(config.WEB_DIR, route.lstrip("/"))

        if route == "/api/data":
            con = _open()
            try:
                return self._send(200, stats.build_payload(con, _filters(q)))
            finally:
                con.close()
        if route == "/api/skills":
            return self._cached("skills", q, insights.skill_mining)
        if route == "/api/insights":
            return self._cached("insights", q, insights.build)
        if route == "/api/health":
            return self._send(200, {"ok": True, "db": str(config.DB_PATH)})
        if route == "/api/llm":
            return self._send(200, llm.status())
        if route == "/api/jobs":
            return self._send(200, {"jobs": jobs.recent()})
        if route.startswith("/api/jobs/"):
            job = jobs.get(route.rsplit("/", 1)[-1])
            return self._send(200, job.to_dict()) if job else self._send(404, {"error": "no such job"})
        if route == "/api/artifacts":
            return self._send(200, {
                "proposals": llm.list_proposals(), "findings": llm.list_findings(),
                "exports": reports.list_exports(), "llm": llm.status(),
                "skills_dir": str(config.claude_home() / "skills"),
                "jobs": jobs.recent(),
            })
        if route.startswith("/api/proposals/"):
            text = llm.read_proposal(route.rsplit("/", 1)[-1])
            return self._send(200, text, "text/markdown; charset=utf-8") if text is not None \
                else self._send(404, {"error": "no such proposal"})
        if route.startswith("/api/findings/"):
            text = llm.read_finding(route.rsplit("/", 1)[-1])
            return self._send(200, text, "text/markdown; charset=utf-8") if text is not None \
                else self._send(404, {"error": "no such finding"})
        if route == "/api/evidence":
            f = _filters(q)
            con = _open()
            try:
                mining = cached("skills", con, f, insights.skill_mining)
                return self._send(200, insights.evidence_pack(con, f, mining), "text/markdown; charset=utf-8")
            finally:
                con.close()
        return self._send(404, {"error": "unknown endpoint"})

    def _cached(self, kind, q, compute):
        con = _open()
        try:
            return self._send(200, cached(kind, con, _filters(q), compute))
        finally:
            con.close()

    # -- POST --------------------------------------------------------------
    def do_POST(self):
        if not self._origin_ok():
            return self._send(403, {"error": "loopback origin required"})
        route = urlparse(self.path).path
        body = self._body()

        if route.startswith("/api/jobs/") and route.endswith("/cancel"):
            ok = jobs.cancel(route.split("/")[3])
            return self._send(200 if ok else 404, {"ok": ok})

        if route.startswith("/api/jobs/"):
            kind = route.rsplit("/", 1)[-1]
            spec = JOB_KINDS.get(kind)
            if not spec:
                return self._send(404, {"error": f"unknown job kind {kind!r}"})
            if kind in ("propose-skills", "findings") and not llm.claude_path():
                return self._send(400, {"error": "the `claude` CLI is not on PATH"})
            label, build = spec
            fn, args = build(body)
            with _admit_lock:
                busy = jobs.running()
                # An ingest rewrites the tables every other job reads, so it
                # neither starts alongside anything nor lets anything start.
                if any(j.kind in EXCLUSIVE for j in busy):
                    return self._send(409, {"error": "an ingest is running; wait for it to finish"})
                if kind in EXCLUSIVE and busy:
                    return self._send(409, {"error": "wait for the running job before re-ingesting"})
                job = jobs.start(kind, label, fn, *args)
            return self._send(202, job.to_dict())

        if route == "/api/skills/install":
            try:
                dst = llm.install_skill(body.get("name", ""), overwrite=bool(body.get("overwrite")))
            except FileExistsError as exc:
                return self._send(409, {"error": str(exc)})
            except (FileNotFoundError, ValueError) as exc:
                return self._send(404, {"error": str(exc)})
            return self._send(200, {"ok": True, "installed_to": dst})

        return self._send(404, {"error": "unknown endpoint"})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    if not config.DB_PATH.exists():
        print("No database yet - run:  python ingest.py")
        return 1

    url = f"http://{args.host}:{args.port}/"
    LOOPBACK_HOSTS.add(args.host)
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    print(f"AI usage dashboard -> {url}   (Ctrl-C to stop)")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    return 0


if __name__ == "__main__":
    sys.exit(main())
