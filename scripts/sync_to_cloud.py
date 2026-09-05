"""Keep the cloud in step with this laptop, and say so out loud when it cannot.

Both directions of this were "run by hand and remember to" until 2026-09-05,
and on that morning both of them were quietly wrong at the same time:

  * The cloud display node was showing a whole trading day of stale marks.
    Publishing a state snapshot only ever happened inside `daily.sh`, and
    `daily.sh` had not run since the previous morning. The object store held
    exactly one snapshot, ever. Nothing errored; the 15-minute puller on the
    instance kept succeeding at fetching the same file.
  * Another session had committed three real fixes and believed it had pushed
    them. `origin/main` had not moved. The cloud ran three-day-old code while
    every local signal said the work was done.

Both failures look identical from the outside: everything green, nothing new.
So this script's job is not only to push — it is to **verify the push landed**
and to make drift audible. A sync that cannot prove it worked reports failure.

    python3 scripts/sync_to_cloud.py            # both legs
    python3 scripts/sync_to_cloud.py --dry-run  # decide, don't act
    python3 scripts/sync_to_cloud.py --status   # what happened last time

Two legs, deliberately different in how brave they are:

  code  local `main` ahead of `origin/main` -> run the full suite against a
        clean export of HEAD -> push -> re-fetch and confirm `origin/main` now
        *is* HEAD. The test gate is the same one the cloud updater applies
        before it swaps images; running it here too means a red commit never
        reaches the branch two production nodes follow.

  data  a content fingerprint (newest mark, newest run, row counts) decides
        whether anything worth showing changed. Without that check this would
        upload 65MB every tick forever: the dashboard process writes to the
        database on every page view, so the byte-level "unchanged?" test in
        `push_state_to_cloud` almost never fires.

Neither leg carries configuration. `runtime.env` and the boot script reach the
instances through cloud-init and need a reboot; nothing here changes that, and
saying so is part of the status output so nobody waits for a config change that
is never coming.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

STATE = ROOT / "data" / "cloud_sync_state.json"
LOCK = ROOT / "data" / ".cloud_sync.lock"
LARK = "/opt/homebrew/bin/lark-cli"
LARK_USER = "ou_8d0e4064f46c1d0de14c501c1f5db808"

#: Long enough that a run of failures does not become a run of notifications,
#: short enough that a morning of silence is not mistaken for a morning of
#: success. Only *entering* a failure state notifies immediately.
RENOTIFY_HOURS = 6

#: Consecutive failing ticks before the first alert. Three ticks is ~30 minutes
#: — longer than every outage measured here, short enough that a real stoppage
#: is still caught the same morning.
FAIL_TICKS_BEFORE_ALERT = 3


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


#: Git operations that cross the network. This laptop reaches github.com
#: through a local tunnel that fails roughly one attempt in three — measured,
#: not guessed: three consecutive `git fetch` calls gave ❌ ✅ ✅, the failure
#: being `SSL_ERROR_SYSCALL`, which is the connection dying rather than
#: anything git disagrees with. One dropped packet was abandoning the whole
#: code leg for that tick and raising an alert, so the pusher looked broken
#: while the only thing wrong was a retry it never attempted.
#:
#: Retrying both is safe. `fetch` is read-only. `push` is idempotent — if the
#: attempt that reported failure had in fact landed, the retry answers
#: "Everything up-to-date" — and the caller re-fetches afterwards to confirm
#: what the remote actually holds, so a lie in either direction is caught.
#: Spacing matters more than the count. The tunnel fails in *patches*, not as
#: independent coin flips: sampled ten times a minute apart it was 9/10, but
#: three retries three seconds apart all landed inside one bad patch and the
#: leg gave up anyway. These waits make three attempts span ~45s instead of
#: ~6s, which is wide enough to straddle the short outages actually observed.
NET_OPS = ("fetch", "push", "ls-remote", "pull")
NET_BACKOFF = (3, 10, 30)


def git(*args: str, check: bool = True) -> str:
    waits = NET_BACKOFF if args and args[0] in NET_OPS else ()
    tries = len(waits) + 1 if waits else 1
    last = ""
    for attempt in range(tries):
        r = subprocess.run(("git", *args), cwd=ROOT, capture_output=True,
                           text=True)
        if r.returncode == 0:
            return (r.stdout or "").strip()
        last = (r.stderr or r.stdout).strip()[:400]
        if attempt < len(waits):
            time.sleep(waits[attempt])
    if check:
        raise RuntimeError(f"git {' '.join(args)} 失败 {tries} 次: {last}")
    return ""


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:  # noqa: BLE001 — a missing or torn state file is a fresh start
        return {}


def save_state(st: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1, ensure_ascii=False))
    tmp.replace(STATE)  # never leave a half-written state file behind


# ---------------------------------------------------------------- notifying

def notify(text: str) -> bool:
    """Tell the human. Failing to notify must not fail the sync."""
    if not pathlib.Path(LARK).exists():
        return False
    try:
        r = subprocess.run(
            [LARK, "im", "+messages-send", "--as", "bot",
             "--user-id", LARK_USER, "--markdown", text],
            capture_output=True, text=True, timeout=45)
        return r.returncode == 0
    except Exception:  # noqa: BLE001 — see docstring
        return False


def maybe_notify(st: dict, key: str, failing: bool, text: str) -> None:
    """Notify once trouble has actually persisted, then every RENOTIFY_HOURS.

    Two thresholds, and both were learned rather than chosen. A timer that says
    "still broken" every ten minutes trains you to stop reading it, which is
    how the failures at the top of this file survived three days — hence
    RENOTIFY_HOURS. And a single failed tick is not trouble: the network here
    fails in patches, the next tick is ten minutes later, and the first version
    of this alerted on a blip that had already healed by the time anyone read
    the message. Crying wolf and staying silent are the same failure wearing
    different clothes.
    """
    marks = st.setdefault("notified", {})
    runs = st.setdefault("fail_runs", {})
    last = marks.get(key)
    if not failing:
        marks.pop(key, None)
        runs.pop(key, None)
        return
    runs[key] = int(runs.get(key) or 0) + 1
    if runs[key] < FAIL_TICKS_BEFORE_ALERT:
        return
    if last:
        try:
            age = (dt.datetime.now(dt.timezone.utc)
                   - dt.datetime.fromisoformat(last)).total_seconds()
            if age < RENOTIFY_HOURS * 3600:
                return
        except Exception:  # noqa: BLE001 — an unparseable mark re-notifies
            pass
    if notify(text):
        marks[key] = now()


# ------------------------------------------------------------------- code leg

#: Namespaces the gate must not inherit. `tests/test_core.py` builds its
#: fixtures with `os.environ.setdefault("OLIVE_MCP_URL", ...)`, so a real value
#: already in the environment silently wins and the test then asserts against
#: production URLs. The suite passed from a bare shell and failed from a shell
#: that had sourced `~/.ideagen.env` — same commit, same code, opposite verdict.
#: A gate whose answer depends on the caller's shell is not a gate, so it runs
#: the way CI would: without any of this project's real configuration.
GATE_STRIP = ("IDEAGEN_", "OLIVE_", "WISBURG_", "ARK_", "BYTEPLUS_",
              "VOLCENGINE_", "TOS_")


def gate_env(db_path: pathlib.Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(GATE_STRIP)}
    env["IDEAGEN_DB"] = str(db_path)      # a scratch database, never the live one
    env["PYTHONDONTWRITEBYTECODE"] = "1"  # no __pycache__ left in the worktree
    return env


def export_head(dest: pathlib.Path) -> None:
    """Lay out exactly what a push would publish — HEAD, not the working tree.

    Four Claude sessions share this checkout. The working tree always holds
    somebody's half-finished edit and the index may hold somebody's staged
    file, so testing either one answers a question nobody asked. A detached
    worktree answers the only question that matters: is the commit we are
    about to publish green?

    Not `git archive`: `.gitattributes` marks `/tests export-ignore`, because
    release bundles are deployment inputs and should not carry the suite. An
    archive-based gate therefore finds no tests at all — it fails closed, which
    is the right direction, but it never actually tests anything. A worktree
    ignores export-ignore and gives us the real tree.
    """
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    r = subprocess.run(
        ("git", "worktree", "add", "--detach", "--quiet", str(dest), "HEAD"),
        cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(
            f"git worktree add failed: {(r.stderr or r.stdout).strip()[:300]}")


def image_paths() -> list[str]:
    """What the Dockerfile actually copies, read from the Dockerfile.

    Not a list repeated here. The bug that made this function necessary was
    exactly a list kept in two places: the Dockerfile said which files reach
    the image, and a test independently assumed a file at the repository root
    would be there. Copying the list into this script would set up the same
    divergence one level further out.
    """
    df = (ROOT / "deploy" / "Dockerfile").read_text(encoding="utf-8")
    out = []
    for line in df.splitlines():
        line = line.strip()
        if not line.upper().startswith("COPY "):
            continue
        parts = line.split()[1:]
        if parts and parts[0].startswith("--"):   # COPY --from=... : another stage
            continue
        if len(parts) >= 2:
            out.append(parts[0])
    if not out:
        raise RuntimeError("deploy/Dockerfile 里没有解析到任何 COPY 行")
    return out


def mirror_image(full: pathlib.Path, img: pathlib.Path) -> None:
    """Lay out only what the image will contain, so the gate judges what runs.

    The local gate and the cloud gate were not testing the same tree, and that
    difference had teeth: on 2026-09-05 a commit passed here with 609 tests
    green and was refused by the cloud updater, which held production three
    hours behind. The test read `.gitignore`; a worktree has one and the image
    does not, because the Dockerfile copies six directories and nothing from
    the repository root.

    So the gate now runs against the image's file set. It still comes from a
    detached worktree of HEAD rather than `git checkout-index`: the index holds
    whatever other sessions have staged and not yet committed, while HEAD is
    precisely what a push publishes and what the cloud will check out. Testing
    the index would answer a question nobody asked.
    """
    img.mkdir(parents=True, exist_ok=True)
    for rel in image_paths():
        src = full / rel
        dst = img / rel
        if not src.exists():
            continue          # a COPY of something not in this commit
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)


def drop_worktree(dest: pathlib.Path) -> None:
    """Remove the gate's worktree and its registration, whatever went wrong.

    A worktree left behind is not inert: `git worktree list` grows an entry
    per tick, and a later `git worktree add` on the same path refuses.
    """
    subprocess.run(("git", "worktree", "remove", "--force", str(dest)),
                   cwd=ROOT, capture_output=True)
    shutil.rmtree(dest, ignore_errors=True)
    subprocess.run(("git", "worktree", "prune"), cwd=ROOT, capture_output=True)


def code_leg(st: dict, dry_run: bool) -> dict:
    out: dict = {"leg": "code", "at": now()}
    git("fetch", "origin", "--quiet")
    head = git("rev-parse", "HEAD")
    remote = git("rev-parse", "origin/main")
    ahead = int(git("rev-list", "--count", "origin/main..HEAD") or 0)
    behind = int(git("rev-list", "--count", "HEAD..origin/main") or 0)
    out.update(head=head[:7], origin_main=remote[:7], ahead=ahead, behind=behind)

    if behind:
        # Somebody else pushed while we were away. Pushing now would be a
        # non-fast-forward; rebasing unattended is not this script's call.
        out.update(action="skipped", ok=False,
                   detail=f"本地落后 origin/main {behind} 个提交，需要先手动合并")
        return out
    if ahead == 0:
        out.update(action="none", ok=True, detail="已同步")
        return out

    # The suite grew from 6 seconds to 230 as sessions added tests, and this
    # timer fires every 10 minutes. Re-running four minutes of tests against a
    # commit already judged — which is the normal case once the gate has
    # blocked and nobody has committed since — spends a quarter of this
    # laptop's CPU to re-derive an answer it already has. A commit's verdict
    # cannot change, so it is remembered by sha.
    verdict = st.get("gate") or {}
    if verdict.get("sha") == head:
        out["tests"] = f"{verdict.get('summary')}（沿用对 {head[:7]} 的判定）"
        if not verdict.get("passed"):
            out.update(action="blocked", ok=False,
                       detail=f"测试闸门此前已拦下 HEAD {head[:7]}，未重跑")
            return out
    else:
        root = pathlib.Path(tempfile.mkdtemp(prefix="ideagen-gate-"))
        tmp, img = root / "head", root / "img"
        try:
            export_head(tmp)
            mirror_image(tmp, img)
            tests = subprocess.run(
                (sys.executable, "-m", "pytest", "tests", "-q", "-x"),
                cwd=img, capture_output=True, text=True, timeout=1800,
                env=gate_env(img / "gate.db"))
            raw = (tests.stdout or tests.stderr or "").strip().splitlines()
            # 只报最后一行，等于一个说不出原因的闸门。2026-09-05 代码腿被拦了一小时,
            # `--status` 从头到尾只说 "1 failed, 32 passed" —— 够判断被拦了,
            # 不够判断拦在哪,于是没人去看。pytest 的短摘要里每个失败自带一行
            # `FAILED <文件>::<用例>`,带上它们,「被拦了」和「拦在哪」一句话说完。
            named = [ln for ln in raw if ln.startswith(("FAILED ", "ERROR "))]
            summary = raw[-1] if raw else "(no output)"
            out["tests"] = " | ".join([summary, *named])[:400]
            st["gate"] = {"sha": head, "passed": tests.returncode == 0,
                          "summary": out["tests"], "at": now()}
            if tests.returncode != 0:
                out.update(action="blocked", ok=False,
                           detail=f"测试闸门拦下 HEAD {head[:7]}，没有推送")
                return out
        except Exception as e:  # noqa: BLE001 — a gate that cannot run is a closed gate
            # Deliberately not remembered: an exception says the gate could
            # not reach a verdict, not that the commit is bad. Caching that
            # would keep refusing a commit nobody ever managed to test.
            out.update(action="blocked", ok=False,
                       detail=f"测试闸门无法运行: {type(e).__name__}: {e}"[:300])
            return out
        finally:
            drop_worktree(tmp)
            shutil.rmtree(root, ignore_errors=True)

    if dry_run:
        out.update(action="would-push", ok=True,
                   detail=f"测试通过，会推 {ahead} 个提交")
        return out

    try:
        git("push", "origin", "main")
    except Exception as e:  # noqa: BLE001 — reported, never raised past here
        out.update(action="push-failed", ok=False, detail=str(e)[:300])
        return out

    # The whole reason this script exists. A push that reports success and
    # leaves origin/main where it was is the exact failure that let two
    # production nodes run three-day-old code while everyone believed
    # otherwise. Ask the remote, do not trust the exit code.
    #
    # 但「落地」不等于「origin/main 恰好等于 HEAD」。这个仓里同时有好几个会话，
    # 每三五分钟就有人推一条；等号形式的断言会在别人抢先推上来的那几秒里
    # 把一次**成功**的推送报成 push-did-not-land，还发一条飞书。
    # 一个会误报的告警等于一个没人看的告警——恰恰是这个脚本自己警告过的事。
    # 真正要问的是：HEAD 现在是不是 origin/main 的祖先。
    git("fetch", "origin", "--quiet")
    landed = git("rev-parse", "origin/main")
    if subprocess.run(("git", "merge-base", "--is-ancestor", head, "origin/main"),
                      cwd=ROOT, capture_output=True).returncode != 0:
        out.update(action="push-did-not-land", ok=False,
                   detail=f"push 退出码为 0，但 {head[:7]} 不在 origin/main "
                          f"（现在是 {landed[:7]}）的历史里")
        return out
    if landed != head:
        out.update(action="pushed", ok=True,
                   detail=f"推了 {ahead} 个提交；期间别人又推了，"
                          f"origin/main 现在是 {landed[:7]}，{head[:7]} 已在其中")
        return out
    out.update(action="pushed", ok=True,
               detail=f"推了 {ahead} 个提交，origin/main 现在是 {head[:7]}")
    return out


# ------------------------------------------------------------------- data leg

def content_fingerprint() -> str:
    """What the dashboard would show, boiled down to one comparable string.

    Not a hash of the file: the dashboard writes to this database on every
    page view, so the bytes change constantly while the numbers on screen do
    not. These five values move only when there is genuinely something new to
    look at.
    """
    src = ROOT / "data" / "ideagen.db"
    if not src.exists():
        raise RuntimeError(f"找不到状态库 {src}")
    con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        def one(sql: str):
            try:
                return con.execute(sql).fetchone()[0]
            except sqlite3.Error:
                return None  # a table this build does not have is not a change
        parts = [one("select max(d) from mtm"),
                 one("select max(started_at) from orch_runs"),
                 one("select count(*) from orch_runs"),
                 one("select count(*) from positions"),
                 one("select count(*) from alerts")]
    finally:
        con.close()
    return "|".join("" if p is None else str(p) for p in parts)


def data_leg(st: dict, dry_run: bool, force: bool) -> dict:
    out: dict = {"leg": "data", "at": now()}
    fp = content_fingerprint()
    out["fingerprint"] = fp
    if not force and st.get("data_fingerprint") == fp:
        out.update(action="none", ok=True, detail="组合与运行记录都没变")
        return out
    if dry_run:
        out.update(action="would-publish", ok=True, detail="内容有变，会发布快照")
        return out
    try:
        import push_state_to_cloud as pusher
        rc = pusher.main([])
        if rc != 0:
            out.update(action="publish-failed", ok=False,
                       detail=f"push_state_to_cloud 返回 {rc}")
            return out
    except Exception as e:  # noqa: BLE001 — reported, never raised past here
        out.update(action="publish-failed", ok=False,
                   detail=f"{type(e).__name__}: {e}"[:300])
        return out
    st["data_fingerprint"] = fp
    out.update(action="published", ok=True,
               detail="快照已发布，展示节点 15 分钟内拉取")
    return out


# ----------------------------------------------------------------------- main

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="把本地状态与代码同步到云端")
    ap.add_argument("--dry-run", action="store_true", help="只判断，不推送")
    ap.add_argument("--status", action="store_true", help="打印上次结果后退出")
    ap.add_argument("--force-data", action="store_true",
                    help="内容指纹未变也发布快照")
    ap.add_argument("--only", choices=("code", "data"), help="只跑一条腿")
    args = ap.parse_args(argv)

    st = load_state()
    if args.status:
        print(json.dumps(st, indent=1, ensure_ascii=False))
        return 0

    # One timer, one runner. Overlapping runs would have two pushers racing on
    # the same branch and two 65MB uploads racing on the same bucket.
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        age = 0.0
        try:
            age = dt.datetime.now().timestamp() - LOCK.stat().st_mtime
        except OSError:
            pass
        if age < 3600:
            print(f"另一次同步正在跑（锁 {int(age)}s）")
            return 0
        LOCK.unlink(missing_ok=True)  # a lock older than any real run is debris
        fd = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)

    results = []
    try:
        if args.only != "data":
            try:
                results.append(code_leg(st, args.dry_run))
            except Exception as e:  # noqa: BLE001 — one leg must not kill the other
                results.append({"leg": "code", "at": now(), "ok": False,
                                "action": "error",
                                "detail": f"{type(e).__name__}: {e}"[:300]})
        if args.only != "code":
            try:
                results.append(data_leg(st, args.dry_run, args.force_data))
            except Exception as e:  # noqa: BLE001 — see above
                results.append({"leg": "data", "at": now(), "ok": False,
                                "action": "error",
                                "detail": f"{type(e).__name__}: {e}"[:300]})
    finally:
        LOCK.unlink(missing_ok=True)

    for r in results:
        st[f"last_{r['leg']}"] = r
        print(f"[{r['leg']}] {r.get('action')}: {r.get('detail')}")
        if not args.dry_run:
            maybe_notify(
                st, r["leg"], not r.get("ok", False),
                f"⚠️ 云端同步 · {r['leg']} 腿卡住了\n\n"
                f"**{r.get('action')}** — {r.get('detail')}\n\n"
                f"（本地 {ROOT}，`python3 scripts/sync_to_cloud.py --status` 看详情）")
    st["last_run"] = now()
    if not args.dry_run:
        save_state(st)
    return 0 if all(r.get("ok") for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
