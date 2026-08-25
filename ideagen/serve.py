"""Local dashboard server.

Rebuilds the page from the database on every request for `/`, so a refresh always
shows the current state of the books rather than whatever was on disk when
`ideagen dashboard` last ran. Everything else under `web/` is served as a static
file.

Bound to 127.0.0.1 only. Nothing here is authenticated, and the page carries the
full position blotter.
"""

from __future__ import annotations

import http.server
import json
import socketserver
import threading
import time
import traceback
from pathlib import Path

from . import config, db, monitor, report

HOST = "127.0.0.1"
DEFAULT_PORT = 8765


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(config.WEB), **kw)

    # keep the console readable: one line per request, no noisy default format
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        print(f"  {self.address_string()} {fmt % args}")

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            return self._live_dashboard()
        if path == "/api/status":
            return self._json(monitor.digest(db.init()))
        if path == "/api/report":
            p = config.WEB / "report.json"
            if not p.exists():
                report.build(db.init())
            return self._raw(p.read_bytes(), "application/json; charset=utf-8")
        if path == "/api/state":
            from . import review
            return self._json(review.state(db.init()))
        if path in ("/review", "/review.html", "/dash"):
            # The Nexus dashboard is the review surface now; the old generated
            # placeholder is retired. Static file + live /api/state.
            dash = config.WEB / "dash.html"
            if dash.exists():
                return self._raw(dash.read_bytes(), "text/html; charset=utf-8")
            return self._json({"error": "dash.html 尚未生成"})
        if path == "/healthz":
            return self._json({"ok": True, "ts": config.now_hkt().isoformat()})
        return super().do_GET()

    def _live_dashboard(self) -> None:
        t0 = time.time()
        try:
            con = db.init()
            out = report.build(con)
            body = out.read_bytes()
            print(f"  rebuilt dashboard in {time.time()-t0:.2f}s "
                  f"({len(body)//1024}KB)")
            self._raw(body, "text/html; charset=utf-8")
        except Exception:                       # noqa: BLE001
            tb = traceback.format_exc()
            print(tb)
            page = (f"<!doctype html><meta charset=utf-8>"
                    f"<title>IdeaGen40 · build failed</title>"
                    f"<body style='font:14px/1.6 ui-monospace,monospace;"
                    f"padding:32px;max-width:900px'>"
                    f"<h1 style='font:600 20px system-ui'>Dashboard 生成失败</h1>"
                    f"<p>数据库仍然完好，只是渲染这一步出错了。</p>"
                    f"<pre style='background:#f6f8fa;padding:16px;overflow:auto;"
                    f"border-radius:4px'>{tb}</pre></body>")
            self._raw(page.encode(), "text/html; charset=utf-8", status=500)

    def _json(self, obj) -> None:
        self._raw(json.dumps(obj, ensure_ascii=False, indent=1, default=str).encode(),
                  "application/json; charset=utf-8")

    def _raw(self, body: bytes, ctype: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def serve(port: int = DEFAULT_PORT, open_browser: bool = False) -> None:
    con = db.init()
    report.build(con)                            # fail fast if rendering is broken

    for attempt in range(10):                    # walk forward if the port is busy
        try:
            httpd = Server((HOST, port + attempt), Handler)
            port = port + attempt
            break
        except OSError:
            continue
    else:
        raise RuntimeError(f"no free port in {port}..{port+9}")

    url = f"http://{HOST}:{port}/"
    print(f"IdeaGen40 dashboard  →  {url}")
    print(f"  /              每次刷新都从数据库重新生成")
    print(f"  /api/status    紧凑摘要 JSON")
    print(f"  /api/report    完整归因 JSON")
    print(f"  Ctrl-C 停止\n")
    if open_browser:
        import webbrowser

        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
