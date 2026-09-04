"""Dashboard HTTP server.

The root serves ``web/dash.html``, whose only state source is the live
``/api/state`` endpoint. The legacy generated report remains available at
``/legacy`` for local diagnostics, but it is not the cloud entry point.

Binds to 127.0.0.1 by default; set IDEAGEN_DASH_HOST=0.0.0.0 to serve behind a
reverse proxy in a container. Remote requests require the shared dash key (see
Handler._authorized), so a non-loopback bind is still gated — but the page
carries the full position blotter, so a public bind must sit behind the proxy
and its key, never be exposed directly.
"""

from __future__ import annotations

import http.server
import html
import json
import os
import socketserver
import threading
import time
import traceback
from pathlib import Path

from . import config, db, monitor, report

# Bind address. Defaults to loopback so a laptop run stays private; a container
# behind a reverse proxy sets IDEAGEN_DASH_HOST=0.0.0.0 so the proxy can reach it.
# The dash key gate (see Handler._authorized) still applies on every remote request.
HOST = os.environ.get("IDEAGEN_DASH_HOST", "127.0.0.1")
DEFAULT_PORT = 8765


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(config.WEB), **kw)

    # keep the console readable: one line per request, no noisy default format
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        print(f"  {self.address_string()} {fmt % args}")

    def log_request(self, code="-", size="-") -> None:
        # Query strings may contain the one-time dashboard key. Never write
        # them to stdout, where container log collectors would retain them.
        path = self.path.split("?", 1)[0]
        self.log_message('"%s %s %s" %s %s',
                         self.command, path, self.request_version, code, size)

    def _authorized(self) -> bool:
        """Remote requests need the shared key; localhost stays open.

        The dashboard's API serves licensed research bodies and a partner's
        product identifiers. A public URL without a gate would republish content
        that is not ours — the same boundary the publish-safety check enforces
        for GitHub Pages, applied to the live server. The key is a capability,
        not identity: one secret, rotated by editing ~/.ideagen.env.
        """
        import os
        from .platform.local import EnvSecretStore
        from . import platform as plat_mod
        key = (os.environ.get("IDEAGEN_DASH_KEY")
               or EnvSecretStore(plat_mod._ENV_FILES).get("IDEAGEN_DASH_KEY",
                                                          required=False))
        # A tunnel daemon connects from loopback, so the socket address alone
        # cannot distinguish "the operator's own browser" from "the entire
        # internet arriving through cloudflared". Any forwarding header means the
        # request crossed the tunnel and gets remote treatment — trusting the
        # socket here was a fully open backdoor, found because the very first
        # no-key probe through the tunnel returned 200.
        forwarded = bool(self.headers.get("CF-Connecting-IP")
                         or self.headers.get("X-Forwarded-For"))
        local = (self.client_address[0] in ("127.0.0.1", "::1")
                 and not forwarded)
        if not key:
            return local
        if local:
            return True
        from urllib.parse import parse_qs, urlparse
        q = parse_qs(urlparse(self.path).query)
        supplied = ((q.get("key") or [None])[0]
                    or self.headers.get("X-Dash-Key")
                    or (self.headers.get("Cookie") or "").replace(
                        "dashkey=", "").split(";")[0].strip())
        ok = bool(supplied) and supplied == key
        if ok and (q.get("key") or [None])[0]:
            # Move the key out of the URL into a cookie so links shared from the
            # browser afterwards don't carry it.
            cookie = f"dashkey={key}; Path=/; HttpOnly; SameSite=Strict"
            if self.headers.get("X-Forwarded-Proto") == "https":
                cookie += "; Secure"
            self._set_cookie = cookie
            self._strip_auth_query = True
        return ok

    def do_GET(self) -> None:  # noqa: N802
        # Any unhandled exception below reaches BaseHTTPRequestHandler, which
        # closes the socket without a status line — the browser reports
        # ERR_EMPTY_RESPONSE and the page shows "服务没响应", which points the
        # reader at the wrong thing. One malformed query parameter should cost
        # a readable 500, not a diagnosis of the server being down.
        try:
            return self._route_get()
        except Exception as exc:  # noqa: BLE001 — bounded error, no traceback
            traceback.print_exc()
            from . import ask as _ask_mod
            return self._json(
                {"error": _ask_mod._scrub_text(
                    f"{type(exc).__name__}: {exc}"[:300])}, status=500)

    def _route_get(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/api/olive/oauth/callback":
            return self._olive_callback()
        if path != "/healthz" and not self._authorized():
            return self._raw("访问需要钥匙：在链接后加 ?key=<钥匙>（仅首次，之后走 Cookie）"
                             .encode(), "text/plain; charset=utf-8", status=401)
        if getattr(self, "_strip_auth_query", False):
            return self._redirect_without_auth_query()
        if path == "/api/olive/oauth/start":
            return self._begin_olive_authorization()
        if path in ("/", "/index.html", "/review", "/review.html", "/dash"):
            return self._dashboard()
        if path == "/api/status":
            return self._json(monitor.digest(db.init()))
        if path == "/api/report":
            p = config.WEB / "report.json"
            if not p.exists():
                report.build(db.init())
            return self._raw(p.read_bytes(), "application/json; charset=utf-8")
        if path == "/api/corpus":
            from urllib.parse import parse_qs, urlparse
            from . import review
            q = parse_qs(urlparse(self.path).query)
            return self._json(review.corpus_list(
                db.init(), as_of=(q.get("as_of") or [None])[0]))
        if path == "/api/doc":
            from urllib.parse import parse_qs, urlparse
            from . import review
            q = parse_qs(urlparse(self.path).query)
            return self._json(review.doc_detail(
                db.init(), doc_id=(q.get("id") or [""])[0]))
        if path == "/api/journal":
            # The run journal is the "it really ran" evidence: each pipeline step
            # with its wall-clock timestamp, model calls with durations, feed
            # fetches with row counts. Served for the dashboard's run-log view.
            # Host and storage URIs are scrubbed — provenance is the timestamps
            # and step structure, not the machine identity behind them.
            from . import ask as _rv_ask
            import json as _j
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            from . import platform as _plat_mod
            plat_ = _plat_mod.load()
            rid = (q.get("run_id") or [None])[0]
            row = (plat_.state.q(
                "SELECT run_id, as_of FROM orch_runs WHERE run_id=?", (rid,))
                if rid else plat_.state.q(
                "SELECT run_id, as_of FROM orch_runs WHERE kind='weekly' AND ok=1 "
                "ORDER BY as_of DESC LIMIT 1"))
            if not row:
                return self._json({"error": "没有可读的运行记录"})
            r = row[0]
            try:
                j = _j.loads(plat_.blobs.get(
                    f"runs/{r['as_of']}/{r['run_id']}/journal.json"))
            except Exception as e:  # noqa: BLE001
                return self._json({"error": f"journal 读取失败: {type(e).__name__}"})
            # Host, bucket names and home paths are stripped by the one shared
            # scrubber every journal-serving path goes through.
            return self._json({"run_id": r["run_id"], "as_of": r["as_of"],
                               "journal": _rv_ask.scrub_journal(j)})
        if path == "/api/audit":
            # The audit bundle: the whole run in the reader's own hands, in an
            # order that reads without this UI. Same exposure class as
            # /api/journal — artifacts and asks, never the report bodies.
            from urllib.parse import parse_qs, urlparse
            from . import audit
            from . import platform as _plat_mod
            q = parse_qs(urlparse(self.path).query)
            blob, name = audit.build(_plat_mod.load(),
                                     (q.get("run_id") or [None])[0])
            if blob is None:
                return self._json({"error": name}, status=404)
            self._set_download = name
            return self._raw(blob, "application/zip")
        if path == "/api/asks":
            # The session record: every question put to a past run, newest
            # first. Read-only view of the same log the audit bundle ships.
            from urllib.parse import parse_qs, urlparse
            from . import ask as _ask_mod
            q = parse_qs(urlparse(self.path).query)
            try:
                lim = int((q.get("limit") or ["50"])[0])
            except ValueError:
                lim = 50
            return self._json({"asks": _ask_mod.recent_asks(
                lim, (q.get("run_id") or [None])[0])})
        if path == "/api/ask/context":
            # 「问当时的它」— the frozen material a decision saw, for display.
            from urllib.parse import parse_qs, urlparse
            from . import ask
            q = parse_qs(urlparse(self.path).query)
            obj, status = ask.handle_context(
                {k: (q.get(k) or [None])[0] for k in ("run_id", "kind", "id")})
            return self._json(obj, status=status)
        if path == "/api/state":
            from . import review
            return self._json(review.state(db.init()))
        if path == "/api/olive/status":
            from . import olive_web
            return self._json(olive_web.status())
        if path == "/olive":
            return self._olive_page()
        if path == "/legacy":
            return self._live_dashboard()
        if path == "/healthz":
            return self._json({"ok": True, "ts": config.now_hkt().isoformat()})
        return super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if not self._authorized():
            return self._json({"error": "unauthorized"}, status=401)
        request_origin = self._external_origin()
        if not self._same_origin(request_origin):
            return self._json({"error": "cross-origin request rejected"}, status=403)
        length = int(self.headers.get("Content-Length") or 0)
        if path == "/api/ask":
            # 「问当时的它」— one grounded Q&A over a run's frozen material.
            # The body carries the question plus in-drawer history, so it gets
            # a larger (still bounded) read than the drain below.
            from . import ask
            try:
                payload = json.loads(
                    self.rfile.read(min(length, 131072)) or b"{}")
                assert isinstance(payload, dict)
            except Exception:  # noqa: BLE001
                return self._json({"error": "请求体不是合法的 JSON 对象"},
                                  status=400)
            obj, status = ask.handle_ask(payload)
            return self._json(obj, status=status)
        length = min(length, 4096)
        if length:
            self.rfile.read(length)
        if path == "/api/olive/oauth/start":
            return self._begin_olive_authorization(request_origin)
        if path == "/api/olive/sync":
            from . import olive_web
            try:
                started = olive_web.start_sync()
            except Exception as exc:  # noqa: BLE001
                return self._json({
                    "error": f"{type(exc).__name__}: {exc}"[:300],
                }, status=502)
            return self._redirect("/olive?sync=" + ("started" if started else "running"))
        return self._json({"error": "not found"}, status=404)

    def _external_origin(self) -> str:
        from urllib.parse import urlsplit

        scheme = (self.headers.get("X-Forwarded-Proto") or "http").split(",", 1)[0].strip()
        host = (self.headers.get("X-Forwarded-Host")
                or self.headers.get("Host") or "").split(",", 1)[0].strip()
        try:
            parsed = urlsplit(f"{scheme}://{host}")
        except ValueError:
            return ""
        if (parsed.scheme not in ("http", "https") or not parsed.netloc
                or parsed.username or parsed.password):
            return ""
        return f"{parsed.scheme}://{parsed.netloc}"

    def _same_origin(self, expected: str | None = None) -> bool:
        expected = expected or self._external_origin()
        if not expected:
            return False
        origin = (self.headers.get("Origin") or "").rstrip("/")
        if origin and origin != "null":
            return origin == expected
        referer = self.headers.get("Referer") or ""
        if referer:
            return referer == expected or referer.startswith(expected + "/")
        return self.headers.get("Sec-Fetch-Site") == "same-origin"

    def _begin_olive_authorization(self, origin: str | None = None) -> None:
        from . import olive_web

        try:
            return self._redirect(
                olive_web.begin_authorization(origin or self._external_origin()))
        except Exception as exc:  # noqa: BLE001 - return bounded operator error
            return self._json({
                "error": f"{type(exc).__name__}: {exc}"[:300],
            }, status=502)

    def _olive_callback(self) -> None:
        from urllib.parse import urlsplit
        from . import olive_web

        try:
            result = olive_web.complete_authorization(
                urlsplit(self.path).query)
        except Exception as exc:  # noqa: BLE001 - no traceback or credential echo
            return self._olive_callback_page(
                "Olive 授权未完成",
                f"{type(exc).__name__}: {exc}"[:300],
                status=400,
            )
        server = result.get("server") or {}
        detail = (
            f"已连接 {server.get('name') or 'Olive MCP'}"
            f" {server.get('version') or ''}，"
            f"发现 {int(result.get('tool_count') or 0)} 个工具。"
            "真实货架正在后台同步。"
        )
        return self._olive_callback_page("Olive 授权完成", detail)

    def _olive_callback_page(self, title: str, detail: str,
                             status: int = 200) -> None:
        body = f"""<!doctype html>
<html lang="zh-Hans"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>
body{{margin:0;background:#f6f7f8;color:#24211f;font:14px/1.6 -apple-system,
BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}}
main{{max-width:560px;margin:12vh auto;padding:32px;background:#fff;
border:1px solid #dedbd6;border-radius:8px}}
h1{{margin:0 0 12px;font-size:22px}}p{{color:#625e58}}
a{{display:inline-block;margin-top:18px;color:#174b35;font-weight:600}}
</style></head><body><main><h1>{html.escape(title)}</h1>
<p>{html.escape(detail)}</p><a href="/olive">返回数据源</a></main></body></html>"""
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body.encode())))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body.encode())

    def _olive_page(self) -> None:
        page = config.WEB / "olive.html"
        if page.exists():
            return self._raw(page.read_bytes(), "text/html; charset=utf-8")
        return self._json({"error": "olive.html missing"}, status=500)

    def _dashboard(self) -> None:
        dash = config.WEB / "dash.html"
        if dash.exists():
            return self._raw(dash.read_bytes(), "text/html; charset=utf-8")
        return self._json({"error": "dash.html 尚未生成"})

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

    #: No indent: the state document is read by the dashboard, not by eye, and
    #: pretty-printing it cost 154 KB of the 806 KB it used to weigh. `/api/doc`
    #: and friends are small enough that one rule for all of them is simpler
    #: than a per-route exception.
    def _json(self, obj, status: int = 200) -> None:
        self._raw(json.dumps(obj, ensure_ascii=False, separators=(",", ":"),
                             default=str).encode(),
                  "application/json; charset=utf-8", status=status)

    def _redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _redirect_without_auth_query(self) -> None:
        from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

        parsed = urlsplit(self.path)
        query = urlencode([(key, value)
                           for key, value in parse_qsl(parsed.query,
                                                      keep_blank_values=True)
                           if key != "key"])
        location = urlunsplit(("", "", parsed.path, query, ""))
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Set-Cookie", self._set_cookie)
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()
        self._set_cookie = None
        self._strip_auth_query = False

    #: The dashboard is one self-contained file: inline <style>, inline
    #: <script> and inline event handlers, and it fetches only same-origin
    #: JSON. 'unsafe-inline' is therefore unavoidable for script/style, but
    #: everything a page has no business doing here is closed off: no external
    #: origins, no framing, no plugin content, no form posts, no <base>
    #: rewriting. Those are the directives that still buy something once
    #: inline code is allowed.
    CSP = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self'; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'none'; "
        "form-action 'none'; "
        "frame-ancestors 'none'"
    )

    def _security_headers(self) -> None:
        self.send_header("Content-Security-Policy", self.CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header(
            "Permissions-Policy",
            "geolocation=(), microphone=(), camera=(), payment=(), usb=()")

    #: Text bodies above this compress well enough to be worth the CPU; below
    #: it the header overhead eats the win.
    GZIP_MIN = 1400

    def _raw(self, body: bytes, ctype: str, status: int = 200) -> None:
        # The state document is ~650 KB of JSON and is refetched every 60
        # seconds. Over the tunnel that was the slowest part of a refresh;
        # gzip takes it to ~95 KB. Downloads are left alone: they are already
        # compressed archives, and re-encoding them buys nothing.
        encoding = None
        if (len(body) >= self.GZIP_MIN
                and not getattr(self, "_set_download", None)
                and ctype.split("/")[0] in ("text", "application")
                and "gzip" in self.headers.get("Accept-Encoding", "")):
            import gzip
            packed = gzip.compress(body, 6)
            if len(packed) < len(body):
                body, encoding = packed, "gzip"
        self.send_response(status)
        if getattr(self, "_set_cookie", None):
            self.send_header("Set-Cookie", self._set_cookie)
            self._set_cookie = None
        self.send_header("Content-Type", ctype)
        if encoding:
            self.send_header("Content-Encoding", encoding)
        self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        if getattr(self, "_set_download", None):
            # RFC 5987: the filename is Chinese, so only the encoded form is
            # sent — a raw non-ASCII header value is what makes browsers fall
            # back to naming the file after the endpoint.
            from urllib.parse import quote
            self.send_header(
                "Content-Disposition",
                "attachment; filename*=UTF-8''" + quote(self._set_download))
            self._set_download = None
        self.end_headers()
        self.wfile.write(body)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def serve(port: int = DEFAULT_PORT, open_browser: bool = False) -> None:
    dash = config.WEB / "dash.html"
    if not dash.exists():
        raise RuntimeError(f"dashboard asset missing: {dash}")

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
    print(f"  /              运行台（数据来自 /api/state）")
    print(f"  /legacy        本地 SQLite 旧版报表")
    print(f"  /api/status    紧凑摘要 JSON")
    print(f"  /api/report    完整归因 JSON")
    print(f"  Ctrl-C 停止\n")
    if open_browser:
        import webbrowser

        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    # Take the first port-health verdict before anyone asks for it. The probe
    # is cached and refreshed off the request path, but the very first caller
    # would otherwise be told the verdict is `pending` — honest, yet a worse
    # first paint than simply having the answer ready.
    def _warm() -> None:
        try:
            from . import platform as plat_mod, review as review_mod
            review_mod._probe_ports(plat_mod.load())
        except Exception:  # noqa: BLE001 — warm-up never blocks the server
            pass

    threading.Thread(target=_warm, name="port-health-warmup",
                     daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
