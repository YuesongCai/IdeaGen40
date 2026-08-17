#!/usr/bin/env python3
"""交接前脱敏体检：一条命令跑完 docs/handover.md 的整张清单。

    python3 scripts/preflight_handover.py            # 全量，任一 FAIL 即退出码 1
    python3 scripts/preflight_handover.py --quick    # 跳过 git 历史扫描（最慢的一步）

为什么不是把清单写在文档里让人照着敲：照着敲的清单会被跳过，跳过的那一项就是
出事的那一项。所以每条检查都必须能被机器判定，判定不了的（比如「这个数据能不
能对外发布」）就做成必须显式确认的开关 —— 见 ACKS。

设计约束：
* 永远不打印命中的值，只打印 文件:行号:命中的模式名。报告一个 secret 的内容以
  警告它，本身就是泄露。
* 比 ideagen.schema.secret_audit() 更宽。那个函数默认只看 git 跟踪的文件、只看
  HEAD、并且整目录跳过 docs/ 与 tests/。这三条各自都是合理的取舍，但交接时
  `tar` / `docker build` 装走的是整个工作目录，公开仓库里活着的是整段历史，而
  docs/ 恰好是最容易被粘进 AK/SK 的地方。两者一起跑才算体检。
* 云依赖（tos / psycopg / redis / kafka）不装也要能跑完并给出结论，否则第一次上
  机的人拿不到任何信息。
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# 需要人「明确点头」而不是脚本能替人判断的决定。设成环境变量 =1 表示接收方已经
# 自己重新做过这个决定，而不是继承了上一家的默认值。
ACKS = {
    "HANDOVER_ACK_PUBLIC_BLOTTER":
        "已确认是否继续把完整持仓明细发布到公开 GitHub Pages（handover.md §4）",
    "HANDOVER_ACK_THIRD_PARTY_DATA":
        "已确认 Wisburg 原文与 Nexus/Olive 货架数据的再发布授权（handover.md §4）",
}

# ---------------------------------------------------------------------------
# 凭证形状。比 schema.PATTERNS 多出来的都是这套系统实际会碰到的东西：飞书应用
# 凭证与 webhook、带内联口令的连接串、JWT、券商账号字段。
CRED_PATTERNS: tuple[tuple[str, str], ...] = (
    ("BytePlus AK",        r"AKAP[A-Za-z0-9]{16,}"),
    ("AWS-style AK",       r"\bAKIA[0-9A-Z]{16}\b"),
    ("sk- 形式 token",      r"\bsk-[A-Za-z0-9_-]{20,}"),
    ("私钥块",              r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"),
    ("bearer token",       r"[Bb]earer\s+[A-Za-z0-9._~+/-]{20,}"),
    ("JWT",                r"eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\."),
    ("赋值式 secret",       r"(?i)\b(secret_access_key|secret_key|access_key_secret|"
                           r"app_secret|api_key|apikey|passwd|password|token)\s*[=:]\s*"
                           r"[\"'][A-Za-z0-9_\-/+=]{16,}"),
    ("带口令的连接串",       r"(?i)\b(postgres(?:ql)?|redis|rediss|mysql|mongodb(?:\+srv)?|"
                           r"amqp)://[^\s:/@\"']+:[^\s@\"']+@"),
    ("飞书 app id",         r"\bcli_[a-z0-9]{14,}"),
    ("飞书 webhook",        r"open\.feishu\.cn/open-apis/bot/v2/hook/[0-9a-f-]{8,}"),
    ("飞书 open_id",        r"\bou_[0-9a-f]{24,}"),
    ("券商账号字段",         r"(?i)(trd_?acc_?id|acc_?id|account_?n(?:o|umber))\s*[=:]\s*\"?\d{6,}"),
)

# 身份痕迹：不是凭证，但会把「谁在跑这套系统」写进一个要转手的仓库。
IDENTITY_PATTERNS: tuple[tuple[str, str], ...] = (
    # 故意不写死上一任的用户名：把要清掉的那个名字抄进清理脚本，既是又留了一份，
    # 也换个仓库就失效。这里只认「硬编码了某个具体账号/家目录」这个形状，谁的名字
    # 由人看一眼判断。
    ("硬编码的 macOS 家目录", r"/Users/[a-z][a-z0-9._-]{2,}/"),
    ("硬编码的 GitHub 账号",  r"(?i)(?:github\.com/[A-Za-z0-9][A-Za-z0-9-]{0,38}/"
                              r"|\b[A-Za-z0-9][A-Za-z0-9-]{0,38}\.github\.io\b)"),
    # 故意不写死任何域名：写死域名等于把上一家的公司域名留在脚本里，而且换一个域名
    # 就漏。宁可命中所有邮箱地址，让人逐条判断哪些是占位、哪些是真人。
    ("邮箱地址",             r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.(?:com|cn|hk|net|org|io|co)\b"),
    # TOS bucket 名里带着账号 ID，所以一个 bucket 名就足以指认上一家的 BytePlus
    # 账号 —— 不是凭证，但接收方绝不能把 endpoint 指到它上面。
    ("上一任 BytePlus 账号/bucket",
     r"(?i)\bideagen-\d{9,}\b|\baccount[_ -]?id\b\s*[=:：]\s*\"?\d{9,}"),
)

# 合作方货架产品代码。data/snapshots/ 已被 .gitignore 挡住，但代码会流进 idea 的
# desc / sources，再从那里流进要发布的 dashboard —— 挡住原始快照不等于挡住派生物。
SHELF_CODE = r"\bL0\d{4}\b"

SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", ".mypy_cache"}
# 这个脚本自身写着上面那些模式，扫自己必然自命中。
SELF = {"scripts/preflight_handover.py", "ideagen/schema.py", "docs/handover.md"}


class Report:
    def __init__(self) -> None:
        self.fails: list[str] = []
        self.warns: list[str] = []

    def section(self, title: str) -> None:
        print(f"\n\033[1m{title}\033[0m")

    def ok(self, msg: str) -> None:
        print(f"  [ OK ] {msg}")

    def warn(self, msg: str) -> None:
        print(f"  [WARN] {msg}")
        self.warns.append(msg)

    def fail(self, msg: str) -> None:
        print(f"  [FAIL] {msg}")
        self.fails.append(msg)

    def skip(self, msg: str) -> None:
        print(f"  [SKIP] {msg}")


R = Report()


def sh(*args: str, cwd: Path = ROOT, timeout: int = 120) -> tuple[int, str]:
    try:
        p = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:  # noqa: BLE001
        return 127, f"{type(e).__name__}: {e}"


def tracked() -> set[str]:
    rc, out = sh("git", "ls-files")
    return set(out.split()) if rc == 0 else set()


def worktree_files() -> list[Path]:
    out = []
    for p in ROOT.rglob("*"):
        if p.is_file() and not any(part in SKIP_DIRS for part in p.parts):
            out.append(p)
    return out


def scan(text: str, patterns) -> list[tuple[str, int]]:
    hits = []
    for label, pat in patterns:
        for m in re.finditer(pat, text):
            hits.append((label, text[:m.start()].count("\n") + 1))
    return hits


# ---------------------------------------------------------------------------
def check_secret_audit() -> None:
    """先跑项目自带的那个，因为交接对话里引用的就是它。"""
    R.section("1. ideagen.schema.secret_audit()（仓库自带，只看 git 跟踪的文件）")
    sys.path.insert(0, str(ROOT))
    try:
        from ideagen.schema import ALLOW, secret_audit
    except Exception as e:  # noqa: BLE001
        R.fail(f"无法导入 secret_audit：{type(e).__name__}: {e}")
        return
    r = secret_audit(ROOT)
    if r["clean"]:
        R.ok(f"扫了 {r['scanned']} 个文件，0 命中")
    else:
        for f in r["findings"]:
            R.fail(f"secret_audit: {f['file']}:{f['line']} 命中「{f['kind']}」")
    n_skipped = len(tracked()) - r["scanned"]
    if n_skipped > 0:
        R.warn(f"其中 {n_skipped} 个跟踪文件因白名单 {list(ALLOW)} 被整体跳过；"
               f"下面第 2 步不认这份白名单，会把它们也扫一遍")


def check_worktree_sweep() -> None:
    """整个工作目录，不只是 git 跟踪的部分 —— tar / docker build 装走的是这些。"""
    R.section("2. 工作目录全量扫描（含未跟踪文件，不认 docs/ 白名单）")
    tr = tracked()
    n = 0
    found = 0
    for p in worktree_files():
        rel = str(p.relative_to(ROOT))
        if rel in SELF:
            continue
        try:
            text = p.read_bytes().decode("utf-8", "ignore")
        except OSError:
            continue
        n += 1
        for label, line in scan(text, CRED_PATTERNS):
            found += 1
            where = "已跟踪" if rel in tr else "未跟踪但在目录里"
            R.fail(f"{rel}:{line} 命中「{label}」（{where}）")
    if not found:
        R.ok(f"扫了 {n} 个文件（跟踪 {len(tr)} 个），0 处凭证形状命中")


def check_history() -> None:
    """HEAD 干净不等于历史干净。删掉一个 secret 的那次提交，把它永久写进了历史。"""
    R.section("3. git 历史全量扫描（所有 ref 的所有 blob）")
    rc, out = sh("git", "rev-list", "--objects", "--all", timeout=300)
    if rc != 0:
        R.fail("git rev-list 失败，历史未经检查 —— 不要在这个状态下交接")
        return
    blobs: list[tuple[str, str]] = []
    for line in out.splitlines():
        sha, _, path = line.partition(" ")
        if path:
            blobs.append((sha, path))
    hits = 0
    checked = 0
    for sha, path in blobs:
        if path in SELF:
            continue
        rc, body = sh("git", "cat-file", "-p", sha, timeout=60)
        if rc != 0:
            continue
        checked += 1
        for label, line in scan(body, CRED_PATTERNS):
            hits += 1
            R.fail(f"历史 blob {sha[:9]}（路径 {path}）:{line} 命中「{label}」——"
                   f" 需要 filter-repo 重写 + 轮换该凭证，见 handover.md §1")
    if not hits:
        R.ok(f"检查了 {checked} 个历史 blob，0 处凭证形状命中")


def check_env_file() -> None:
    R.section("4. 凭证落点")
    env_file = Path(os.environ.get("IDEAGEN_ENV", Path.home() / ".ideagen.env"))
    if not env_file.exists():
        R.warn(f"{env_file} 不存在 —— 换账号后这里应当有你们自己的 AK/SK，"
               f"或者全部走 KMS / 容器环境变量注入")
    else:
        if str(env_file.resolve()).startswith(str(ROOT)):
            R.fail(f"{env_file} 在仓库目录内 —— 一次 `git add -A` 就会被发布出去")
        else:
            R.ok(f"{env_file} 在仓库目录之外")
        mode = oct(env_file.stat().st_mode & 0o777)
        if mode in ("0o600", "0o400"):
            R.ok(f"{env_file} 权限 {mode}")
        else:
            R.fail(f"{env_file} 权限 {mode}，必须是 600")
        names = sorted({ln.split("=", 1)[0].strip()
                        for ln in env_file.read_text(encoding="utf-8").splitlines()
                        if ln.strip() and not ln.startswith("#") and "=" in ln})
        R.ok(f"该文件持有 {len(names)} 个键（只列名，不读值）：{names}")

    stray = [str(p.relative_to(ROOT)) for p in worktree_files()
             if p.name == ".ideagen.env" or p.suffix == ".env"]
    if stray:
        R.fail(f"仓库目录内存在 env 文件：{stray}")
    else:
        R.ok("仓库目录内没有 .env / .ideagen.env")


def check_identity() -> None:
    R.section("5. 身份痕迹（不是凭证，但会把上一任写进要转手的仓库）")
    tr = tracked()
    hits = 0
    for rel in sorted(tr):
        if rel in SELF:
            continue
        p = ROOT / rel
        if not p.exists():
            continue
        try:
            text = p.read_bytes().decode("utf-8", "ignore")
        except OSError:
            continue
        seen = set()
        for label, line in scan(text, IDENTITY_PATTERNS):
            if (rel, label) in seen:
                continue
            seen.add((rel, label))
            hits += 1
            R.fail(f"{rel}:{line} 含「{label}」")
    if not hits:
        R.ok("跟踪文件中未发现上一任的用户路径 / 账号 / 个人邮箱")


def check_data_boundary() -> None:
    R.section("6. 数据边界（授权问题，不只是隐私问题）")
    tr = tracked()
    for d in ("data/briefings/", "data/snapshots/"):
        leaked = sorted(f for f in tr if f.startswith(d))
        if leaked:
            R.fail(f"{d} 下有 {len(leaked)} 个文件被 git 跟踪 —— 该目录是"
                   f"第三方订阅原文 / 合作方货架数据，不得随仓库转手")
        else:
            R.ok(f"{d} 未被跟踪")

    db_tracked = [f for f in tr if f.startswith("data/ideagen.db")]
    if db_tracked:
        R.fail(f"SQLite 库被跟踪：{db_tracked}")
    else:
        R.ok("data/ideagen.db* 未被跟踪")

    # 未跟踪不等于不会跟着走。没有 .dockerignore 的 docker build、目录级 tar、
    # rsync 都会把它装进去，而库里有 documents.body 的订阅原文。
    db = ROOT / "data" / "ideagen.db"
    if db.exists():
        n_docs = n_body = 0
        try:
            import sqlite3
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            n_docs = con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            n_body = con.execute(
                "SELECT COALESCE(SUM(LENGTH(body)),0) FROM documents").fetchone()[0]
            con.close()
        except Exception:  # noqa: BLE001
            pass
        extras = [p.name for p in db.parent.glob("ideagen.db-*")]
        R.warn(f"data/ideagen.db 存在（{db.stat().st_size // 1048576} MB，"
               f"documents {n_docs} 行 / body {n_body} 字符的订阅原文），"
               f"另有 {extras}。git 挡住了它，但 tar / rsync / 无 .dockerignore 的 "
               f"docker build 会装走 —— 交接前物理删除或换成脱敏样本库")
    _check_docker_context()

    shelf_tracked: list[tuple[str, list[str]]] = []
    for rel in sorted(tr):
        p = ROOT / rel
        if not p.exists() or rel in SELF:
            continue
        try:
            text = p.read_bytes().decode("utf-8", "ignore")
        except OSError:
            continue
        codes = sorted(set(re.findall(SHELF_CODE, text)))
        if codes:
            shelf_tracked.append((rel, codes))
    if shelf_tracked:
        for rel, codes in shelf_tracked:
            R.warn(f"{rel} 含 {len(codes)} 个合作方货架产品代码 —— "
                   f"原始快照被挡住了，派生物没有")
    else:
        R.ok("跟踪文件中未发现合作方货架产品代码")


def _check_docker_context() -> None:
    """镜像层是删不掉的：后一层删了文件，前一层照样叠在下面，`docker history` 还留着
    build arg。所以关心的不是「跑起来有没有」，而是「有没有被 COPY 进任何一层」。"""
    dockerfiles = [p for p in worktree_files()
                   if p.name == "Dockerfile" or p.name.startswith("Dockerfile.")]
    if not dockerfiles:
        if not (ROOT / ".dockerignore").exists():
            R.warn("仓库没有 .dockerignore；将来加 Dockerfile 时 build 上下文会包含"
                   " data/ 整棵树")
        return
    has_ignore = (ROOT / ".dockerignore").exists()
    for df in dockerfiles:
        rel = str(df.relative_to(ROOT))
        body = df.read_text(encoding="utf-8", errors="ignore")
        wide = re.findall(r"(?im)^\s*(?:COPY|ADD)\s+(?:--[^\s]+\s+)*\.\s", body)
        copies = re.findall(r"(?im)^\s*COPY\s+(?:--[^\s]+\s+)*([^\s]+)\s", body)
        if wide:
            R.fail(f"{rel} 用 `COPY . ` 拷整个 build 上下文"
                   + ("" if has_ignore else "，而且没有 .dockerignore —— "
                      "data/ideagen.db 与 data/logs/ 会被烙进镜像层")
                   + ("；.dockerignore 存在，逐条核对它是否挡住了 data/" if has_ignore else ""))
        else:
            R.ok(f"{rel} 按路径逐个 COPY（{copies}），data/ 不进镜像")
        if any(c.strip("./") == "seed" for c in copies):
            R.warn(f"{rel} 会把 seed/ 拷进镜像，而 seed/pack_*.json 含合作方"
                   f"货架产品代码 —— 见本节最后几条")
        if re.search(r"(?im)^\s*(?:ENV|ARG)\s+\w*(KEY|SECRET|TOKEN|PASSWORD)", body):
            R.fail(f"{rel} 里有 ENV/ARG 形式的凭证声明 —— 镜像层与 docker history "
                   f"都会留下，必须改成运行时注入")
        else:
            R.ok(f"{rel} 没有把凭证写成 ENV/ARG")


def check_publishing() -> None:
    R.section("7. 对外发布面")
    rc, out = sh("git", "remote", "-v")
    if rc == 0 and out.strip():
        remotes = sorted({ln.split()[1] for ln in out.splitlines() if len(ln.split()) > 1})
        R.warn(f"remote 仍指向上一任：{remotes} —— 换成你们自己的仓库，"
               f"否则第一次 push 会推回原来的公开仓库")
    else:
        R.ok("没有配置 remote")

    rc, out = sh("git", "branch", "-a")
    if "gh-pages" in out:
        R.warn("存在 gh-pages 分支（历史里是逐日发布的完整持仓明细）。"
               "接收方要么删掉它，要么明确决定继续发布")

    daily = ROOT / "scripts" / "daily.sh"
    if daily.exists() and "publish_pages.sh" in daily.read_text(encoding="utf-8"):
        R.warn("scripts/daily.sh 仍会无人值守地调用 publish_pages.sh --yes（跳过确认）"
               " —— 第一次部署前先摘掉这一段，跑通再决定要不要装回")

    for var, why in ACKS.items():
        if os.environ.get(var) == "1":
            R.ok(f"{var}=1 —— {why}")
        else:
            R.fail(f"{var} 未设为 1 —— {why}。这一项脚本无法替你判断，"
                   f"读完 handover.md 对应小节后显式确认")


def check_platform() -> None:
    R.section("8. 平台端口健康（换账号后逐个验，见 handover.md §3）")
    py = sys.executable
    rc, out = sh(py, "-m", "ideagen.cli", "platform", "--env", timeout=180)
    for line in out.splitlines():
        if line.strip():
            print(f"       │ {line.rstrip()}")
    if rc == 0:
        R.ok("ideagen platform 退出码 0（必需端口全部就绪）")
    else:
        R.fail(f"ideagen platform 退出码 {rc} —— 有必需端口不可用。"
               f"上面每一行 FAIL 都对应一个要填的环境变量或要开的网络")


def check_silent_success() -> None:
    """区分「跑成功了」和「跑完了但什么都没做」。

    一次 ingest 抓到 0 条、然后后面每一步都对着空语料成功，退出码同样是 0。所以
    最后一道检查看的是行数，不是退出码。
    """
    R.section("9. 静默空跑检测（最后一道，也是唯一能区分真跑通的一道）")
    db = ROOT / "data" / "ideagen.db"
    if not db.exists():
        R.warn("还没有状态库，首次部署时这一项在 daily 跑完后再看")
        return
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT run_id, as_of, status, stages FROM runs "
            "ORDER BY started_at DESC LIMIT 1").fetchone()
        if not row:
            R.fail("runs 表为空：从来没有跑过一次完整 daily")
            return
        import json as _j
        stages = _j.loads(row["stages"] or "[]")
        bad = [s["stage"] for s in stages if s.get("status") != "ok"]
        docs = con.execute(
            "SELECT COUNT(*) FROM documents WHERE ingested_at >= "
            "(SELECT MAX(started_at) FROM runs)").fetchone()[0]
        px = con.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
        con.close()
    except Exception as e:  # noqa: BLE001
        R.fail(f"读状态库失败：{type(e).__name__}: {e}")
        return

    R.ok(f"最近一次 run {row['run_id']} as_of={row['as_of']} status={row['status']}"
         f" 阶段 {len(stages) - len(bad)}/{len(stages)} ok")
    if bad:
        R.warn(f"失败阶段：{bad}")
    if docs == 0:
        R.fail("最近一次 run 之后没有任何新语料落库 —— 退出码可能是 0，"
               "但这一次跑等于什么都没做（Wisburg 未授权 / 网络不通最常见）")
    else:
        R.ok(f"该次 run 之后新增语料 {docs} 条")
    if px == 0:
        R.fail("prices 表为空 —— OpenD 从未取到行情，盯市与归因全是空的")
    else:
        R.ok(f"prices 表 {px} 行")


def main() -> int:
    ap = argparse.ArgumentParser(description="IdeaGen40 交接前脱敏体检")
    ap.add_argument("--quick", action="store_true",
                    help="跳过 git 历史扫描（最慢的一步；正式交接前必须跑完整版）")
    args = ap.parse_args()

    print("IdeaGen40 交接前体检   root=" + str(ROOT))
    print("凭证值永不打印，只报位置与命中的模式名。")

    check_secret_audit()
    check_worktree_sweep()
    if args.quick:
        R.section("3. git 历史全量扫描")
        R.warn("--quick 跳过。正式交接前必须跑一次不带 --quick 的完整检查")
    else:
        check_history()
    check_env_file()
    check_identity()
    check_data_boundary()
    check_publishing()
    check_platform()
    check_silent_success()

    print("\n" + "=" * 72)
    print(f"结论：{len(R.fails)} 项 FAIL，{len(R.warns)} 项 WARN")
    if R.fails:
        print("\nFAIL 明细（全部清掉才能交接）：")
        for i, f in enumerate(R.fails, 1):
            print(f"  {i:>2}. {f}")
    if R.warns:
        print("\nWARN 明细（需要人做一次判断，判断完可以带着 WARN 交接）：")
        for i, w in enumerate(R.warns, 1):
            print(f"  {i:>2}. {w}")
    print("=" * 72)
    return 1 if R.fails else 0


if __name__ == "__main__":
    sys.exit(main())
