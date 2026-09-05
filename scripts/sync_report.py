"""One screenful: is the cloud showing what this laptop knows?

Answers the only two questions that matter, per node, and separates them —
because on 2026-09-05 they failed independently and both looked fine:

  code  is the node running the commit that origin/main points at?
  data  are the books it shows marked to the same day this laptop has?

Prints one line per fact and exits 0 only when everything lines up, so it
works as a shell condition as well as something to read.
"""
from __future__ import annotations

import json
import pathlib
import re
import ssl
import subprocess
import sqlite3
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
NODES = [("展示 101.47.28.218", "http://101.47.28.218"),
         ("生产 101.47.152.106", "https://101.47.152.106")]
LAX = ssl.create_default_context()
LAX.check_hostname = False
LAX.verify_mode = ssl.CERT_NONE  # the nodes hold self-signed certs for a bare IP


def dash_key() -> str:
    try:
        for line in open(pathlib.Path.home() / ".ideagen.env", encoding="utf-8"):
            m = re.match(r"^\s*IDEAGEN_DASH_KEY=(\S+)", line)
            if m:
                return m.group(1)
    except OSError:
        pass
    return ""


def get(url: str, key: str, timeout: int = 25):
    req = urllib.request.Request(url, headers={"X-Dash-Key": key})
    with urllib.request.urlopen(req, timeout=timeout, context=LAX) as r:
        return json.loads(r.read().decode())


def local_mark_date() -> str | None:
    src = ROOT / "data" / "ideagen.db"
    con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        return con.execute("select max(d) from mtm").fetchone()[0]
    finally:
        con.close()


def book_date(state: dict) -> str | None:
    """Newest mark date across the books.

    `equity` is a curve — a list of {d, equity} points — not a single value.
    Reading it as a scalar returns None, which this report would then have
    displayed as "out of sync" on a node that was perfectly current. A status
    tool that cries wolf is worse than no status tool.
    """
    newest = None
    for b in state.get("books") or []:
        eq = b.get("equity")
        if isinstance(eq, list) and eq:
            d = eq[-1].get("d") if isinstance(eq[-1], dict) else None
        elif isinstance(eq, dict):
            d = eq.get("d")
        else:
            d = None
        if d and (newest is None or d > newest):
            newest = d
    return newest


def main() -> int:
    key = dash_key()
    origin = subprocess.run(("git", "rev-parse", "--short", "origin/main"),
                            cwd=ROOT, capture_output=True, text=True).stdout.strip()
    head = subprocess.run(("git", "rev-parse", "--short", "HEAD"),
                          cwd=ROOT, capture_output=True, text=True).stdout.strip()
    ahead = subprocess.run(("git", "rev-list", "--count", "origin/main..HEAD"),
                           cwd=ROOT, capture_output=True, text=True).stdout.strip()
    mark = local_mark_date()

    print(f"本地      HEAD={head}  未推={ahead}  组合计价到 {mark}")
    print(f"origin/main = {origin}")

    # `healthz` reports a fingerprint of the node's own source, not a commit, so
    # a matching fingerprint is evidence and not proof. Where a node can say
    # which commit it deployed — the production node's updater does, via
    # /api/deploy — ask it, and print the answer as an answer. Where it cannot,
    # say "指纹" and leave it as the weaker claim it is, rather than dressing
    # inference up as confirmation.
    ok = ahead == "0"
    for label, base in NODES:
        try:
            h = get(base + "/healthz", key, timeout=15)
            fp = h["code"]["fingerprint"]
            files = h["code"]["files"]
        except Exception as e:  # noqa: BLE001 — an unreachable node is a reportable state
            print(f"{label:22s} 不可达: {type(e).__name__}")
            ok = False
            continue
        sha = None
        try:
            up = (get(base + "/api/deploy", key, timeout=15) or {}).get("updater")
            sha = (up or {}).get("deployed_sha")
        except Exception:  # noqa: BLE001 — only one node runs an updater
            pass
        if sha:
            # Against origin/main, not HEAD. The node follows the branch, so a
            # commit sitting unpushed on this laptop is already reported once
            # as 未推 — charging the node for it too would report one problem
            # as two and make the node look broken when it is perfectly current.
            match = "✅ 已跟上 origin/main" if sha == origin else f"⚠️ 落后于 {origin}"
            print(f"{label:22s} 已部署 {sha} {match}")
            if sha != origin:
                ok = False
        try:
            st = get(base + "/api/state", key)
            n_books = len(st.get("books") or [])
            d = book_date(st)
        except Exception as e:  # noqa: BLE001 — see above
            print(f"{label:22s} 代码指纹={fp}  组合读不到({type(e).__name__})")
            ok = False
            continue
        if n_books == 0:
            # The production node's RDS migration carried orch_runs and
            # candidates but not the ledger tables. That is a known gap waiting
            # on a decision, not a sync failure — calling it one every tick
            # would bury the failures that are real.
            print(f"{label:22s} 指纹={fp} ({files}文件)  "
                  "无组合（RDS 未迁组合表，非同步问题）")
            continue
        same = "✅" if d == mark else "⚠️ 落后"
        print(f"{label:22s} 指纹={fp} ({files}文件)  组合={d} {same}")
        if d != mark:
            ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
