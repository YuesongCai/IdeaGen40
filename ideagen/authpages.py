"""The pages a person needs before and around the dashboard.

Kept out of serve.py so the router stays a router, and out of web/dash.html
because these have to render when the dashboard cannot — before there is a
session, and while the database is down. They are plain server-rendered HTML
with no fetches and no dependencies for that reason.
"""
from __future__ import annotations

import html

_CSS = """
:root{color-scheme:light dark;
  --bg:#f6f6f4; --card:#fff; --ink:#1a1a19; --muted:#6b6b68; --line:#e3e3df;
  --accent:#1f6feb; --err:#b3261e; --ok:#1a7f37}
@media (prefers-color-scheme:dark){:root{
  --bg:#141413; --card:#1e1e1c; --ink:#f0efec; --muted:#a3a29d; --line:#33322f;
  --accent:#6aa3ff; --err:#ff8a80; --ok:#7ee787}}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;background:var(--bg);color:var(--ink);
  font:400 14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",
  "PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
  display:flex;align-items:center;justify-content:center;padding:24px}
.card{width:100%;max-width:420px;background:var(--card);border:1px solid var(--line);
  border-radius:14px;padding:28px 26px;box-shadow:0 1px 2px rgba(0,0,0,.05),0 8px 24px rgba(0,0,0,.06)}
.wide{max-width:720px}
h1{margin:0 0 4px;font-size:19px;font-weight:650;letter-spacing:-.01em}
.sub{margin:0 0 22px;color:var(--muted);font-size:13px}
label{display:block;margin:14px 0 6px;font-size:13px;font-weight:550}
input[type=text],input[type=password]{width:100%;padding:10px 12px;font-size:14px;
  color:var(--ink);background:var(--bg);border:1px solid var(--line);border-radius:8px}
input:focus{outline:2px solid var(--accent);outline-offset:-1px;border-color:transparent}
button{margin-top:18px;width:100%;padding:10px 14px;font-size:14px;font-weight:600;
  color:#fff;background:var(--accent);border:0;border-radius:8px;cursor:pointer}
button:hover{filter:brightness(1.07)}
button.ghost{background:transparent;color:var(--ink);border:1px solid var(--line);width:auto;
  padding:7px 12px;font-weight:500;margin:0}
button.danger{background:transparent;color:var(--err);border:1px solid var(--line);width:auto;
  padding:6px 10px;font-size:12.5px;font-weight:500;margin:0}
.msg{margin:14px 0 0;padding:9px 11px;border-radius:8px;font-size:13px;
  background:color-mix(in srgb,var(--err) 12%,transparent);color:var(--err)}
.msg.ok{background:color-mix(in srgb,var(--ok) 14%,transparent);color:var(--ok)}
.foot{margin-top:20px;padding-top:14px;border-top:1px solid var(--line);
  color:var(--muted);font-size:12.5px}
a{color:var(--accent);text-decoration:none}
table{width:100%;border-collapse:collapse;margin-top:8px;font-size:13px}
th,td{text-align:left;padding:8px 6px;border-bottom:1px solid var(--line)}
th{color:var(--muted);font-weight:550;font-size:12px}
.row{display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap}
.row>div{flex:1;min-width:150px}
.row label{margin-top:0}
h2{margin:26px 0 0;font-size:15px;font-weight:600}
.you{display:inline-block;padding:2px 8px;border:1px solid var(--line);
  border-radius:999px;font-size:12px;color:var(--muted)}
"""


def _page(title: str, body: str, *, wide: bool = False) -> bytes:
    return (
        "<!doctype html><html lang=zh><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>{_CSS}</style></head>"
        f"<body><div class='card{" wide" if wide else ""}'>{body}</div></body></html>"
    ).encode()


def login_page(*, error: str | None = None, nxt: str = "/review") -> bytes:
    err = f"<div class=msg>{html.escape(error)}</div>" if error else ""
    return _page("登录 · IdeaGen40", f"""
      <h1>IdeaGen40 运行台</h1>
      <p class=sub>这台机器每周自己跑一次选题与建仓，页面是它的账本。</p>
      <form method=post action=/login>
        <input type=hidden name=next value="{html.escape(nxt)}">
        <label for=u>用户名</label>
        <input id=u name=username type=text autocomplete=username autofocus required>
        <label for=p>口令</label>
        <input id=p name=password type=password autocomplete=current-password required>
        <button type=submit>登录</button>
        {err}
      </form>
      <div class=foot>登录状态保留 30 天。忘了口令只能让管理员重置——
        服务端只存口令的散列，没有人能读回原文。</div>
    """)


def account_page(user: str, *, admin: bool, users: list[dict],
                 msg: str | None = None, ok: bool = False) -> bytes:
    banner = (f"<div class='msg{" ok" if ok else ""}'>{html.escape(msg)}</div>"
              if msg else "")
    rows = "".join(
        "<tr><td>{name}{you}{tag}</td><td>{created}</td><td>{last}</td><td>{act}</td></tr>".format(
            name=html.escape(u["name"]),
            you=" <span class=you>你</span>" if u["name"] == user else "",
            tag=" <span class=you>管理员</span>" if u["admin"] else "",
            created=html.escape((u.get("created") or "—")[:10]),
            last=html.escape((u.get("last_login") or "从未登录")[:16].replace("T", " ")),
            act=("" if u["name"] == user or not admin else
                 "<form method=post action=/account/remove style=display:inline>"
                 f"<input type=hidden name=username value='{html.escape(u['name'])}'>"
                 "<button class=danger type=submit>删除</button></form>"))
        for u in users)

    admin_block = f"""
      <h2>账号</h2>
      <table><tr><th>用户</th><th>创建</th><th>最近登录</th><th></th></tr>{rows}</table>
      <h2>新增账号</h2>
      <form method=post action=/account/add>
        <div class=row>
          <div><label for=nu>用户名</label>
            <input id=nu name=username type=text required></div>
          <div><label for=np>口令（至少 8 位）</label>
            <input id=np name=password type=password required></div>
        </div>
        <button type=submit>创建</button>
      </form>""" if admin else f"""
      <h2>账号</h2>
      <table><tr><th>用户</th><th>创建</th><th>最近登录</th><th></th></tr>{rows}</table>
      <p class=sub style="margin-top:10px">只有管理员能增删账号。</p>"""

    return _page("账号 · IdeaGen40", f"""
      <h1>账号</h1>
      <p class=sub>当前登录：<b>{html.escape(user)}</b>
        {"（管理员）" if admin else ""} ·
        <a href="/review">回运行台</a></p>
      {banner}
      <h2>改口令</h2>
      <form method=post action=/account/password>
        <label for=cp>当前口令</label>
        <input id=cp name=current type=password autocomplete=current-password required>
        <label for=n1>新口令（至少 8 位）</label>
        <input id=n1 name=password type=password autocomplete=new-password required>
        <button type=submit>修改</button>
      </form>
      <p class=sub style="margin-top:8px">改完之后所有设备上的登录都会失效，包括这一台。</p>
      {admin_block}
      <div class=foot>
        <form method=post action=/logout style="display:inline">
          <button class=ghost type=submit>退出登录</button></form>
        &nbsp;
        <form method=post action=/account/revoke style="display:inline">
          <button class=ghost type=submit>踢掉我所有设备</button></form>
      </div>
    """, wide=True)


def deploy_page(state: dict) -> bytes:
    """Is this instance running what was last pushed, and if not, why not.

    Written as its own page rather than a corner of the dashboard because the
    question it answers — "is the cloud current" — is one you ask precisely when
    the dashboard might be showing you stale code.
    """
    u = state.get("updater") or {}
    st = str(u.get("state") or "unknown")
    label = {"deployed": ("已是最新", "ok"), "idle": ("守着，没有新提交", "ok"),
             "building": ("正在构建新版本", ""), "testing": ("正在跑测试", ""),
             "blocked": ("新提交没通过测试，仍在跑旧版本", "err"),
             "failed": ("上一次自更新失败", "err"),
             "unreadable": ("状态文件读不了", "err"),
             "unknown": ("自更新服务还没写过状态", "err")}.get(st, (st, ""))
    cls = {"ok": " ok", "err": ""}.get(label[1], "")
    rows = "".join(
        f"<tr><th>{html.escape(k)}</th><td>{html.escape(str(v))}</td></tr>"
        for k, v in (("状态", label[0]),
                     ("目标 commit", u.get("sha") or "—"),
                     ("镜像 tag", state.get("image_sha") or "—"),
                     ("最近一次检查", (u.get("at") or "—").replace("T", " ")),
                     ("详情", u.get("detail") or "—")))
    return _page("部署状态 · IdeaGen40", f"""
      <h1>部署状态</h1>
      <p class=sub>这台实例每两分钟看一次 origin/main：变了就构建、跑测试，
        通过才换上去。<a href="/review">回运行台</a></p>
      <div class='msg{cls}'>{html.escape(label[0])}</div>
      <table>{rows}</table>
      <div class=foot>测试没过时它<b>不会</b>上线，旧版本继续跑——所以这一页显示
        「没通过测试」时，你看到的运行台是上一个好版本，不是坏的那个。</div>
    """, wide=True)
