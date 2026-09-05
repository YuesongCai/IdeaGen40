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

    @staticmethod
    def _acct():
        from . import accounts
        return accounts

    def _session_user(self) -> str | None:
        """The account this request is signed in as, if any. Cached per request."""
        if not hasattr(self, "_session_cache"):
            from . import accounts
            raw = self.headers.get("Cookie") or ""
            token = None
            for part in raw.split(";"):
                k, _, v = part.strip().partition("=")
                if k == accounts.SESSION_COOKIE:
                    token = v
                    break
            self._session_cache = accounts.check(token)
        return self._session_cache

    def _login_redirect(self):
        """Send a browser to the login page; tell a machine it needs a key.

        Redirecting an API call to HTML is how a fetch ends up reporting
        `Unexpected token '<'` — the caller asked for JSON and got a page. So
        the shape of the answer follows the shape of the request.
        """
        path = self.path.split("?", 1)[0]
        wants_html = "text/html" in (self.headers.get("Accept") or "")
        if path.startswith("/api/") or not wants_html:
            return self._json(
                {"error": "未登录", "login": "/login"}, status=401)
        from urllib.parse import quote
        return self._redirect(f"/login?next={quote(self.path)}")

    def _session_cookie(self, value: str, *, days: int) -> str:
        """One cookie, built the same way the key cookie is.

        Secure is set whenever the request arrived through a proxy — a request
        that carries forwarding headers reached us over the public deployment,
        where TLS terminates at the proxy. Local direct access has no TLS to
        demand, and forcing the flag there makes the browser drop the cookie and
        loop back to the login page forever.
        """
        maxage = days * 86400
        cookie = (f"{self._acct().SESSION_COOKIE}={value}; Path=/; HttpOnly; "
                  f"SameSite=Strict; Max-Age={maxage}")
        if (self.headers.get("X-Forwarded-Proto") == "https"
                or self.headers.get("X-Forwarded-For")
                or self.headers.get("CF-Connecting-IP")):
            cookie += "; Secure"
        return cookie

    def _deploy_state(self, *, json_only: bool):
        """What code this instance is running, and whether it is current.

        The self-updater is the only thing keeping the cloud level with
        origin/main, and an updater nobody can see fails silently: the day it
        stops and the day it works look identical from the outside. This reads
        the file it writes on every cycle.
        """
        import json as _json
        # The running commit comes from the updater's own record, not from
        # looking for a .git here: the image copies source without history, so
        # any check made inside the container can only ever answer "no".
        state = {"updater": None}
        for candidate in (Path("/run/ideagen-health/updater.json"),
                          Path("/opt/ideagen/health/updater.json")):
            try:
                if candidate.exists():
                    state["updater"] = _json.loads(
                        candidate.read_text(encoding="utf-8"))
                    break
            except Exception as e:  # noqa: BLE001
                state["updater"] = {"state": "unreadable",
                                    "detail": f"{type(e).__name__}"}
        state["image_sha"] = os.environ.get("IMAGE_TAG") or None
        if json_only:
            return self._json(state)
        from . import authpages
        return self._raw(authpages.deploy_page(state),
                         "text/html; charset=utf-8")

    def _account_page(self, msg: str | None = None, ok: bool = False):
        from . import authpages
        accounts = self._acct()
        who = self._session_user()
        if not who:
            # Reachable with only the machine key, which has no account behind
            # it. Saying so is better than rendering a page about "your" account
            # when there is no you.
            return self._raw(
                authpages.login_page(
                    error="这个页面是给账号用的；当前是用钥匙进来的机器身份。"),
                "text/html; charset=utf-8", status=401)
        return self._raw(
            authpages.account_page(who, admin=accounts.is_admin(who),
                                   users=accounts.list_users(),
                                   msg=msg, ok=ok),
            "text/html; charset=utf-8")

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
            return local or bool(self._session_user())
        if local:
            return True
        # A named session beats the shared key: it says *who*, and it is the
        # only credential a person is ever asked for now. The key stays for
        # machines — the proxy's probe, scripts, the deploy tooling.
        if self._session_user():
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
            # Secure whenever the request reached us through a proxy, not only
            # when the proxy volunteered X-Forwarded-Proto. Reaching this line
            # already means the request was not local — locals return above
            # without a cookie — so a forwarding header means a public
            # deployment, and the terminating proxy is where TLS lives. Making
            # the flag depend on the proxy remembering to announce its scheme
            # put the key's confidentiality in someone else's config: no header,
            # no Secure, and the browser then replays the key over any
            # downgrade. A direct remote hit with no proxy headers is the LAN
            # case, which has no TLS to require, so it is left alone rather than
            # locked into a 401 loop.
            if (self.headers.get("X-Forwarded-Proto") == "https"
                    or self.headers.get("X-Forwarded-For")
                    or self.headers.get("CF-Connecting-IP")):
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
        except (BrokenPipeError, ConnectionResetError):
            # The reader navigated away or the request timed out mid-response.
            # Nothing went wrong on this side, there is no longer a socket to
            # answer on, and writing a 500 into a closed one only raises again
            # — the first version of this handler did exactly that and filled
            # the log with tracebacks that described a client, not a fault.
            return
        except Exception as exc:  # noqa: BLE001 — bounded error, no traceback
            traceback.print_exc()
            from . import ask as _ask_mod
            try:
                return self._json(
                    {"error": _ask_mod._scrub_text(
                        f"{type(exc).__name__}: {exc}"[:300])}, status=500)
            except (BrokenPipeError, ConnectionResetError):
                return

    def _route_get(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/api/olive/oauth/callback":
            return self._olive_callback()
        # /login must be reachable without being logged in, or there is no way
        # to become logged in. It is the only such path besides the health probe.
        if path not in ("/healthz", "/login") and not self._authorized():
            return self._login_redirect()
        if getattr(self, "_strip_auth_query", False):
            return self._redirect_without_auth_query()
        if path == "/login":
            from . import authpages
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            if self._session_user():
                return self._redirect("/review")
            return self._raw(authpages.login_page(
                nxt=(q.get("next") or ["/review"])[0]),
                "text/html; charset=utf-8")
        if path == "/account":
            return self._account_page()
        if path in ("/deploy", "/api/deploy"):
            return self._deploy_state(json_only=path.startswith("/api/"))
        if path == "/api/whoami":
            who = self._session_user()
            return self._json({"user": who,
                               "admin": bool(who and self._acct().is_admin(who)),
                               "via": "session" if who else "key"})
        if path == "/api/olive/oauth/start":
            return self._begin_olive_authorization()
        if path in ("/", "/index.html", "/review", "/review.html", "/dash"):
            return self._dashboard()
        if path == "/api/status":
            # The weekly role travels with it. Two nodes both believing they
            # produce the weekly do not collide — they write to different
            # stores, so the uniqueness index guarding one period never sees the
            # other — and the only symptom is two versions of the same week.
            # Nothing surfaced which role this node had actually resolved to,
            # so a wrapper overriding the declared one was invisible.
            digest = monitor.digest(db.init())
            try:
                from .scheduler import weekly_role
                digest["weekly_role"] = weekly_role()
            except Exception as e:  # noqa: BLE001 — status must still answer
                digest["weekly_role"] = {"error": type(e).__name__}
            return self._json(digest)
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
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            from . import platform as _plat_mod
            plat_ = _plat_mod.load()
            rid = (q.get("run_id") or [None])[0]
            cols = ("run_id, as_of, kind, ok, started_at, ended_at, error, "
                    "calls")
            row = (plat_.state.q(
                f"SELECT {cols} FROM orch_runs WHERE run_id=?", (rid,))
                if rid else plat_.state.q(
                f"SELECT {cols} FROM orch_runs WHERE kind='weekly' AND ok=1 "
                "ORDER BY as_of DESC LIMIT 1"))
            if not row:
                return self._json({"error": (f"orch_runs 里没有 run_id={rid}"
                                             if rid else "没有可读的运行记录")})
            r = dict(row[0])
            # The run row goes out either way. Without it a reader who cannot
            # get the journal is told nothing at all — while the state store,
            # right here, knows when the run started, whether it finished and
            # what it said went wrong. That is a thin log, but it is a true one,
            # and it is what separates "we have no step timeline for this pass"
            # from "there is no record this pass happened".
            j, why = _rv_ask.journal_or_reason(plat_, r)
            out = {"run_id": r["run_id"], "as_of": r["as_of"],
                   "run": _rv_ask.scrub({k: r.get(k) for k in
                                         ("run_id", "as_of", "kind", "ok",
                                          "started_at", "ended_at", "error",
                                          "calls")})}
            if j is None:
                # The reason, not just the exception's class name. The previous
                # version returned `type(e).__name__`, so every distinct cause —
                # never written, wrong bucket, missing credentials — reached the
                # page as the single word "PlatformError", and the page then
                # guessed at a cause and guessed wrong.
                out["error"] = why
                return self._json(out)
            # Host, bucket names and home paths are stripped by the one shared
            # scrubber every journal-serving path goes through.
            out["journal"] = j
            return self._json(out)
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
        if path == "/api/proposals":
            # What each generation method wrote for one instrument, before the
            # merge collapsed them into a single candidate row.
            from urllib.parse import parse_qs, urlparse
            from . import review
            q = parse_qs(urlparse(self.path).query)
            return self._json(review.proposals_for(
                (q.get("instrument") or [""])[0],
                (q.get("run_id") or [None])[0]))
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
        if path == "/api/philosophy":
            # 「我的打法」— which PM rules are running, which await a decision.
            from . import philosophy_web
            obj, status = philosophy_web.handle_list()
            return self._json(obj, status=status)
        if path == "/api/philosophy/output":
            # What one rule actually wrote last period, so the panel's
            # 「点开看」 has something behind it.
            from urllib.parse import parse_qs, urlparse
            from . import philosophy_web
            q = parse_qs(urlparse(self.path).query)
            obj, status = philosophy_web.handle_output(
                {"id": (q.get("id") or [""])[0]})
            return self._json(obj, status=status)
        if path == "/api/state":
            return self._json(_state_document())
        if path == "/api/period":
            # One period's pipeline, on demand. The state document carries the
            # spine (every period, cheap) and the newest period's pipeline; the
            # other five are fetched only when someone walks back to them, so a
            # tab polling /api/state every minute does not pay for six candidate
            # pools it is not showing.
            from urllib.parse import parse_qs, urlparse
            from . import platform as _plat, review
            q = parse_qs(urlparse(self.path).query)
            as_of = (q.get("as_of") or [None])[0]
            if not as_of:
                return self._json({"error": "as_of required"}, status=400)
            try:
                blk = review.weekly_block(_plat.load(), db.init(), as_of)
            except Exception as e:  # noqa: BLE001
                return self._json(
                    {"error": f"{type(e).__name__}: {e}", "as_of": as_of},
                    status=500)
            if not blk:
                # A period with no weekly run is a real answer, not an error:
                # orders can exist against a week whose run never finished.
                return self._json({"as_of": as_of, "weekly": None,
                                   "reason": "no weekly run for this period"})
            return self._json({"as_of": as_of, "weekly": blk})
        if path == "/api/olive/status":
            from . import olive_web
            return self._json(olive_web.status())
        if path == "/olive":
            return self._olive_page()
        if path == "/legacy":
            return self._live_dashboard()
        if path == "/healthz":
            return self._json({"ok": True, "ts": config.now_hkt().isoformat(),
                               "code": _code_version()})
        return super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        # Same guard do_GET has, and for the same reason — more so here, because
        # an exception leaves BaseHTTPRequestHandler closing the socket with no
        # status line, which a reverse proxy reports as 502. A login form that
        # answers "502 Bad Gateway" tells the person nothing and the operator
        # less: it looks like the app is down when the app is running fine and
        # one handler raised.
        try:
            return self._route_post()
        except Exception as exc:  # noqa: BLE001 — bounded error, no traceback
            traceback.print_exc()
            from . import ask as _ask_mod
            return self._json(
                {"error": _ask_mod._scrub_text(
                    f"{type(exc).__name__}: {exc}"[:300])}, status=500)

    def _route_post(self) -> None:
        path = self.path.split("?", 1)[0]
        # /login is the one POST that cannot require being logged in. The
        # same-origin check below still applies to it, and with SameSite=Strict
        # on the session cookie that is what keeps a third-party page from
        # driving this form.
        if path != "/login" and not self._authorized():
            return self._json({"error": "unauthorized"}, status=401)
        request_origin = self._external_origin()
        if not self._same_origin(request_origin):
            return self._json({"error": "cross-origin request rejected"}, status=403)
        length = int(self.headers.get("Content-Length") or 0)
        if path == "/login" or path.startswith("/account") or path == "/logout":
            return self._auth_post(path, length)
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
        if path.startswith("/api/philosophy/"):
            # Propose / activate / discard / retire. Distillation is a model
            # call, so this shares the ask path's larger bounded read rather
            # than the 4 KB drain below.
            from . import philosophy_web
            try:
                payload = json.loads(
                    self.rfile.read(min(length, 16384)) or b"{}")
                assert isinstance(payload, dict)
            except Exception:  # noqa: BLE001
                return self._json({"error": "请求体不是合法的 JSON 对象"},
                                  status=400)
            fn = {"propose": philosophy_web.handle_propose,
                  "activate": philosophy_web.handle_activate,
                  "discard": philosophy_web.handle_discard,
                  "retire": philosophy_web.handle_retire,
                  }.get(path.rsplit("/", 1)[-1])
            if fn is None:
                return self._json({"error": "没有这个操作"}, status=404)
            obj, status = fn(payload)
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

    def _auth_post(self, path: str, length: int):
        """Login, logout, and the account form. All of it posts back here."""
        from urllib.parse import parse_qs
        from . import authpages
        accounts = self._acct()
        body = self.rfile.read(min(length, 8192)).decode("utf-8", "replace")
        form = {k: (v[0] if v else "")
                for k, v in parse_qs(body, keep_blank_values=True).items()}
        client = self.headers.get("X-Forwarded-For", "").split(",")[0].strip() \
            or self.client_address[0]

        if path == "/login":
            wait = accounts.throttle_wait(client)
            if wait > 0:
                return self._raw(authpages.login_page(
                    error=f"尝试太频繁，请等 {int(wait) + 1} 秒再试。"),
                    "text/html; charset=utf-8", status=429)
            user = (form.get("username") or "").strip()
            if user and accounts.verify(user, form.get("password") or ""):
                accounts.throttle_ok(client)
                accounts.note_login(user)
                self._set_cookie = self._session_cookie(
                    accounts.issue(user), days=accounts.SESSION_DAYS)
                nxt = form.get("next") or "/review"
                return self._redirect(nxt if nxt.startswith("/") else "/review")
            accounts.throttle_fail(client)
            # One message for both "no such user" and "wrong password": telling
            # them apart turns the form into a way to ask who has an account.
            return self._raw(authpages.login_page(error="用户名或口令不对。"),
                             "text/html; charset=utf-8", status=401)

        if path == "/logout":
            self._set_cookie = self._session_cookie("", days=0) + "; Max-Age=0"
            return self._redirect("/login")

        who = self._session_user()
        if not who:
            return self._redirect("/login")
        try:
            if path == "/account/password":
                if not accounts.verify(who, form.get("current") or ""):
                    return self._account_page("当前口令不对。")
                accounts.set_password(who, form.get("password") or "")
                # The change just invalidated this very session, which is the
                # point; hand back a fresh one so the person who did it stays in.
                self._set_cookie = self._session_cookie(
                    accounts.issue(who), days=accounts.SESSION_DAYS)
                return self._account_page("口令已改，其他设备上的登录都失效了。", ok=True)
            if path == "/account/revoke":
                accounts.revoke_sessions(who)
                self._set_cookie = self._session_cookie("", days=0) + "; Max-Age=0"
                return self._redirect("/login")
            if path == "/account/add":
                if not accounts.is_admin(who):
                    return self._account_page("只有管理员能新增账号。")
                accounts.add_user(form.get("username") or "",
                                  form.get("password") or "")
                return self._account_page(
                    f"已创建 {form.get('username')}。", ok=True)
            if path == "/account/remove":
                if not accounts.is_admin(who):
                    return self._account_page("只有管理员能删除账号。")
                target = form.get("username") or ""
                if target == who:
                    return self._account_page("不能删除自己。")
                accounts.remove_user(target)
                return self._account_page(f"已删除 {target}。", ok=True)
        except ValueError as e:
            return self._account_page(str(e))
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
        # Logging in ends in a redirect, so a redirect that drops the cookie
        # loses the session it just created: the browser follows to /review,
        # arrives with no credential, and is sent back to the login form it just
        # completed. Every response path that can carry a cookie must emit it.
        if getattr(self, "_set_cookie", None):
            self.send_header("Set-Cookie", self._set_cookie)
            self._set_cookie = None
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
        # 'self', not 'none': the login and account pages post back here.
        # 'none' was right while no page in this app had a form, and it fails
        # by silently refusing the submit — a console CSP violation that looks
        # nothing like an authentication problem.
        "form-action 'self'; "
        "frame-ancestors 'none'"
    )

    #: `same-origin`, not `no-referrer`. `no-referrer` looks like the stricter
    #: choice and is the reason the login form could not be used at all from
    #: outside this machine. Per Fetch, a form POST under `no-referrer` is sent
    #: with `Origin: null` and no `Referer` at all — so the app destroyed both
    #: of the strong signals `_same_origin` checks first, and the whole
    #: same-origin decision came to rest on `Sec-Fetch-Site`, the one header of
    #: the three that an intermediary is free to drop. It survives the hop to
    #: 127.0.0.1 and did not survive the hop to the public IP: the same browser
    #: with the same password got 401 locally and 403 on the cloud node, and
    #: the 403 read `cross-origin request rejected`, which describes the
    #: symptom and hides the cause. `same-origin` sends a real Origin and
    #: Referer to ourselves and still sends nothing to anyone else, so the
    #: check rests on headers the browser guarantees rather than on one a
    #: middlebox may strip. The Olive OAuth callback keeps `no-referrer` — an
    #: authorization code in a URL is exactly what a referrer must not carry.
    def _security_headers(self) -> None:
        self.send_header("Content-Security-Policy", self.CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
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


#: What code this process is actually running, reported on the one endpoint
#: that needs no login.
#:
#: The updater writes `deployed_sha` into a health file on the box, and gates
#: each self-update on the test suite passing inside the image. When that gate
#: goes red the deployment simply stops moving — and from outside, a server
#: that has been updating every five minutes and one frozen three hours ago
#: look identical. That is the same failure the page's staleness stamp exists
#: to prevent, one layer down.
#:
#: The fingerprint is over the source this process loaded, so it changes when
#: and only when the deployed code changes, with no build-time injection
#: needed — it works the same in the image, in a git checkout, and here.
#: `sha` is filled in when the environment supplies one and is null otherwise,
#: rather than guessing. Nothing here is sensitive: a hash and a timestamp.
_code_version_cache: dict[str, object] | None = None


def _code_version() -> dict[str, object]:
    global _code_version_cache
    if _code_version_cache is not None:
        return _code_version_cache
    import hashlib
    h = hashlib.sha256()
    newest = 0.0
    n = 0
    try:
        roots = [Path(__file__).resolve().parent, config.WEB]
        for root in roots:
            if not root.exists():
                continue
            for f in sorted(root.rglob("*")):
                if not f.is_file() or f.suffix not in (".py", ".html"):
                    continue
                if "__pycache__" in f.parts:
                    continue
                st = f.stat()
                # Content, not name and size: two builds can differ by a
                # single character and a fingerprint that misses it is worse
                # than none, because it would report "unchanged" about a
                # change. Read once, at the first health check.
                h.update(f.name.encode())
                h.update(f.read_bytes())
                newest = max(newest, st.st_mtime)
                n += 1
    except Exception:  # noqa: BLE001 — health must answer even if this cannot
        pass
    from datetime import datetime, timezone
    _code_version_cache = {
        "fingerprint": h.hexdigest()[:12],
        "files": n,
        "newest_source": (datetime.fromtimestamp(newest, timezone.utc).isoformat()
                          if newest else None),
        "sha": os.environ.get("IDEAGEN_BUILD_SHA") or None,
        "started": config.now_hkt().isoformat(),
    }
    return _code_version_cache


#: The state document is the page's first paint and its 60-second poll.
#:
#: This cache was added when a build cost about six seconds and concurrent
#: readers pushed it to thirty and seventy. That diagnosis was wrong about the
#: cause: the parallel session found the actual culprit, a missing
#: `ix_mtm_pos ON mtm(pos_id, d)` — `review.state` joins the latest mark by
#: pos_id while the primary key leads with book_id, so every book full-scanned
#: fourteen thousand rows. With the index a build is 0.02-0.04s warm, and the
#: remaining 2.5s cold cost is the platform port probes, not SQL.
#:
#: So this is no longer the thing that makes the page fast. It is kept for
#: what it still does: many readers opening the page at once share one build
#: instead of each starting their own, and a cold process pays the port probes
#: once rather than per request. Both matter on a demo day and neither is
#: worth much on a quiet one — a modest guard, described as one.
#:
#: The staleness it introduces is bounded by TTL and visible: `generated_at`
#: travels in the document, the page prints it, and the page separately
#: reports how old the underlying data is.
_STATE_TTL_S = 20.0
_state_lock = threading.Lock()
_state_cache: dict[str, object] = {"at": 0.0, "doc": None}


def _state_document():
    now = time.time()
    doc = _state_cache.get("doc")
    if doc is not None and (now - float(_state_cache["at"])) < _STATE_TTL_S:
        return doc
    with _state_lock:
        # Someone may have finished the build while this thread waited.
        now = time.time()
        doc = _state_cache.get("doc")
        if doc is not None and (now - float(_state_cache["at"])) < _STATE_TTL_S:
            return doc
        from . import review
        built = review.state(db.init())
        _state_cache["doc"] = built
        _state_cache["at"] = time.time()
        return built


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def serve(port: int = DEFAULT_PORT, open_browser: bool = False) -> None:
    dash = config.WEB / "dash.html"
    if not dash.exists():
        raise RuntimeError(f"dashboard asset missing: {dash}")

    # A fresh deployment would otherwise show a login page nobody can pass.
    # runtime.env already carries one operator's credentials; the first start
    # turns them into a real account and everything after that is managed in the
    # UI rather than by editing an env file.
    try:
        from . import accounts
        created = accounts.bootstrap()
        if created:
            print(f"  已从部署配置创建首个账号：{created}（管理员）")
    except Exception as e:  # noqa: BLE001 — never block serving on this
        print(f"  账号初始化跳过：{type(e).__name__}: {e}")

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
    print(f"  /login         登录页（公网访问走账号，不再是钥匙）")
    print(f"  /account       账号管理：改口令、增删用户、踢设备")
    print(f"  /legacy        本地 SQLite 旧版报表")
    print(f"  /api/period    单期流水线 JSON（?as_of=YYYY-MM-DD）")
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

    def _warm_proposals() -> None:
        # The generator artifacts live in object storage and take about nine
        # seconds to pull; the drawer that needs them is one click deep off the
        # candidate pool. Building the index here means the first reader who
        # asks "how did this idea come about" gets an answer instead of a
        # spinner. Failure is silent on purpose — this is a nicety, and the
        # request path still builds the index on demand.
        try:
            from . import platform as plat_mod, review as review_mod
            plat_ = plat_mod.load()
            rows = plat_.state.q(
                "SELECT run_id, as_of FROM orch_runs "
                "WHERE kind='weekly' AND ok=1 ORDER BY as_of DESC LIMIT 1")
            if rows:
                review_mod._proposal_index(plat_, dict(rows[0]))
        except Exception:  # noqa: BLE001 — warm-up never blocks the server
            pass

    threading.Thread(target=_warm, name="port-health-warmup",
                     daemon=True).start()
    threading.Thread(target=_warm_proposals, name="proposal-index-warmup",
                     daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
