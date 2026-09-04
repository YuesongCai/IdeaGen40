"""Deploy the production stack over SSH, in one command.

Use this the moment outbound TCP 22 to the instance works — today the
operator machine's proxy blocks it (verified: ssh to github.com:22 fails
identically while :443 completes a handshake), and ECS Cloud Assistant fails to
install on this image, so there is currently no channel at all. Adding a DIRECT
rule for the instance IP in the proxy is enough; nothing else here needs to
change.

Every step is idempotent — re-running after a failure resumes. runtime.env is
written by piping `build_runtime_env.py` straight into a 0600 file on the
instance: the secret crosses one encrypted channel and never lands on disk
locally, never enters a command line, and never appears in a log.

  python3 scripts/deploy_ssh.py [--host 101.47.152.106] [--only base|config|up|verify]
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEY = str(Path.home() / ".ssh" / "ideagen_ecs")
HOST = "101.47.152.106"
USER = "root"
REPO = "https://github.com/YuesongCai/IdeaGen40.git"
APP = "/opt/ideagen/app"
CFG = "/opt/ideagen/config"
SSH_OPTS = ["-i", KEY, "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=15", "-o", "BatchMode=yes"]


def ssh(host: str, script: str, *, stdin: bytes | None = None) -> str:
    r = subprocess.run(["ssh", *SSH_OPTS, f"{USER}@{host}", "bash -s"],
                       input=stdin if stdin is not None else script.encode(),
                       capture_output=True)
    out = r.stdout.decode("utf-8", "replace")
    if r.returncode != 0:
        raise SystemExit(f"远端命令失败（exit {r.returncode}）：\n"
                         f"{r.stderr.decode('utf-8', 'replace')[-1500:]}\n{out[-1500:]}")
    return out


def preflight(host: str) -> None:
    print(f"连通性检查 {host}:22 …", flush=True)
    r = subprocess.run(["ssh", *SSH_OPTS, f"{USER}@{host}", "echo ok"],
                       capture_output=True)
    if r.returncode != 0:
        raise SystemExit(
            "SSH 不通。本机代理拦截 22 端口是已知原因——给这个 IP 加 DIRECT "
            "规则（或暂时关掉 TUN 模式）后重跑本脚本。\n"
            + r.stderr.decode("utf-8", "replace")[-500:])
    print("  SSH 可用", flush=True)


def step_base(host: str) -> None:
    print("\n── 安装基础环境并拉取代码", flush=True)
    print(ssh(host, f"""set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq docker.io docker-compose-v2 git curl >/dev/null
systemctl enable --now docker
mkdir -p {APP} {CFG} /opt/ideagen/oauth
chmod 700 {CFG} /opt/ideagen/oauth
if [ -d {APP}/.git ]; then
  git -C {APP} fetch --quiet origin main
  git -C {APP} reset --hard --quiet origin/main
else
  git clone --quiet {REPO} {APP}
fi
echo "docker  $(docker --version)"
echo "commit  $(git -C {APP} rev-parse --short HEAD)"
"""))


def step_config(host: str) -> None:
    """Pipe runtime.env through SSH into a 0600 file. Never touches local disk."""
    print("\n── 写入 runtime.env（经加密通道，不落本地盘）", flush=True)
    env_bytes = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "build_runtime_env.py")],
        capture_output=True, check=True).stdout
    if b"IDEAGEN_MYSQL_PASSWORD=" not in env_bytes:
        raise SystemExit("runtime.env 内容不完整，已中止")
    out = ssh(host, "", stdin=(
        f"umask 077\ncat > {CFG}/runtime.env <<'IDEAGEN_EOF'\n".encode()
        + env_bytes
        + f"IDEAGEN_EOF\nchmod 600 {CFG}/runtime.env\n"
          f"echo \"runtime.env 写入 $(grep -c = {CFG}/runtime.env) 项\"\n".encode()))
    print(out.strip())


def step_up(host: str) -> None:
    print("\n── 构建镜像并启动（首次可能几分钟）", flush=True)
    print(ssh(host, f"""set -euo pipefail
cd {APP}
export IMAGE_TAG="$(git rev-parse --short HEAD)"
docker build -q -t "ideagen40:$IMAGE_TAG" -f deploy/Dockerfile . >/dev/null
IMAGE_TAG="$IMAGE_TAG" docker compose -f deploy/compose.yaml up -d
sleep 20
docker compose -f deploy/compose.yaml ps --format '{{{{.Service}}}}  {{{{.State}}}}'
"""))


def step_verify(host: str) -> None:
    print("\n── 验证", flush=True)
    print(ssh(host, f"""set -euo pipefail
cd {APP}
echo "healthz(应 200):     $(curl -s -o /dev/null -w '%{{http_code}}' http://127.0.0.1:8765/healthz || true)"
echo "远端无钥匙(应 401):  $(curl -s -o /dev/null -w '%{{http_code}}' -H 'X-Forwarded-For: 1.2.3.4' http://127.0.0.1:8765/api/state || true)"
echo "调度器状态:"
docker compose -f deploy/compose.yaml logs --tail 5 scheduler 2>/dev/null || true
"""))
    print(f"\n完成。公网入口：https://{HOST}/ （需带 IDEAGEN_DASH_KEY）")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--only", choices=("base", "config", "up", "verify"))
    args = ap.parse_args(argv)

    preflight(args.host)
    steps = {"base": step_base, "config": step_config,
             "up": step_up, "verify": step_verify}
    for name in ([args.only] if args.only
                 else ["base", "config", "up", "verify"]):
        steps[name](args.host)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
