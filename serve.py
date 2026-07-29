#!/usr/bin/env python3
"""Local dashboard server. Binds to 127.0.0.1 only - nothing leaves the machine.

    python serve.py                # http://127.0.0.1:8787
    python serve.py --port 9000
    python serve.py --no-browser
"""
import argparse
import json
import mimetypes
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from aidash import claude_ingest, config, cursor_ingest, db, stats

_ingest_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    server_version = "aidash"

    def log_message(self, fmt, *a):  # quieter console
        if "/api/" in (self.path or ""):
            sys.stderr.write(f"  {self.command} {self.path}\n")

    # -- helpers -----------------------------------------------------------
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, default=str).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    def _static(self, rel):
        path = (config.WEB_DIR / rel).resolve()
        if not str(path).startswith(str(config.WEB_DIR.resolve())) or not path.is_file():
            return self._send(404, {"error": "not found"})
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._send(200, path.read_bytes(), ctype)

    # -- routes ------------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path
        q = {k: v[0] for k, v in parse_qs(parsed.query).items() if v and v[0]}

        if route in ("/", "/index.html"):
            return self._static("index.html")
        if route.startswith("/api/data"):
            filters = {k: q.get(k) for k in ("since", "until", "project") if q.get(k)}
            con = db.connect()
            try:
                db.init(con)
                return self._send(200, stats.build_payload(con, filters))
            finally:
                con.close()
        if route == "/api/health":
            return self._send(200, {"ok": True, "db": str(config.DB_PATH)})
        if route.startswith("/api/"):
            return self._send(404, {"error": "unknown endpoint"})
        return self._static(route.lstrip("/"))

    def do_POST(self):
        route = urlparse(self.path).path
        if route != "/api/refresh":
            return self._send(404, {"error": "unknown endpoint"})
        if not _ingest_lock.acquire(blocking=False):
            return self._send(409, {"error": "an ingest is already running"})
        try:
            con = db.connect()
            db.init(con)
            result = {
                "claude": claude_ingest.run(con, full=False, verbose=False),
                "cursor": cursor_ingest.run(con, full=False, verbose=False),
            }
            con.close()
            return self._send(200, result)
        except Exception as exc:  # surfaced in the UI banner
            return self._send(500, {"error": str(exc)})
        finally:
            _ingest_lock.release()


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
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
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
