"""Deploy the production stack to the ECS instance without an SSH session.

The operator machine's proxy blocks outbound TCP 22, so this drives the
instance through ECS Cloud Assistant (`RunCommand`) instead. Every step is
idempotent: re-running after a failure resumes rather than duplicating.

Secrets never appear in a command body. `runtime.env` is uploaded to the
project's own TOS bucket, fetched by the instance through a short-lived
presigned URL, written 0600, and the temporary object is deleted immediately
after. What survives in the cloud API's invocation history is a URL that has
already expired, not a key.

  python3 scripts/deploy_cloud.py [--skip-base] [--only base|config|up|verify]
"""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

INSTANCE = "i-yeu80pr2tc3z47gon4sy"
PROFILE = "byteplus"
REPO = "https://github.com/YuesongCai/IdeaGen40.git"
APP_DIR = "/opt/ideagen/app"
CFG_DIR = "/opt/ideagen/config"
PYBIN = sys.executable


def ve(*args: str) -> dict:
    r = subprocess.run(["ve", *args, "--profile", PROFILE],
                       capture_output=True, text=True)
    body = r.stdout.strip()
    if not body:
        raise RuntimeError(f"ve {' '.join(args[:2])} 无输出: {r.stderr[:200]}")
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        raise RuntimeError(f"ve 返回非 JSON: {body[:200]}")


def wait_agent(timeout_s: int = 900) -> None:
    """Cloud Assistant has to be Running before any command can execute."""
    deadline = time.time() + timeout_s
    last = ""
    while time.time() < deadline:
        st = (ve("ecs", "DescribeCloudAssistantStatus",
                 "--InstanceIds.1", INSTANCE)["Result"]["Instances"][0]["Status"])
        if st != last:
            print(f"  Cloud Assistant: {st}", flush=True)
            last = st
        if st in ("Running", "Online"):
            return
        time.sleep(20)
    raise SystemExit(f"Cloud Assistant 未就绪（最后状态 {last}）")


def run(name: str, script: str, *, timeout_s: int = 1800,
        quiet: bool = False) -> str:
    """One shell script on the instance; returns its combined output."""
    print(f"\n── {name}", flush=True)
    inv = ve("ecs", "RunCommand", "--InstanceIds.1", INSTANCE,
             "--Type", "Shell", "--InvocationName", name[:30],
             "--ContentEncoding", "Base64", "--Timeout", str(timeout_s),
             "--CommandContent",
             base64.b64encode(script.encode()).decode())["Result"]["InvocationId"]
    deadline = time.time() + timeout_s + 120
    while time.time() < deadline:
        res = ve("ecs", "DescribeInvocationResults",
                 "--InvocationId", inv)["Result"]["InvocationResults"][0]
        status = res.get("InvocationResultStatus")
        if status in ("Success", "Failed", "Timeout", "Error"):
            out = base64.b64decode(res.get("Output") or "").decode(
                "utf-8", "replace") if res.get("Output") else ""
            if not quiet:
                print(out.strip()[-3000:] or "(无输出)", flush=True)
            if status != "Success" or int(res.get("ExitCode") or 0) != 0:
                raise SystemExit(
                    f"{name} 失败：status={status} exit={res.get('ExitCode')} "
                    f"{res.get('ErrorMessage') or ''}")
            return out
        time.sleep(10)
    raise SystemExit(f"{name} 超时")


def step_base() -> None:
    run("base-setup", f"""set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq docker.io docker-compose-v2 git curl >/dev/null
systemctl enable --now docker
mkdir -p {APP_DIR} {CFG_DIR} /opt/ideagen/oauth
chmod 700 {CFG_DIR} /opt/ideagen/oauth
if [ -d {APP_DIR}/.git ]; then
  git -C {APP_DIR} fetch --quiet origin main && git -C {APP_DIR} reset --hard --quiet origin/main
else
  git clone --quiet {REPO} {APP_DIR}
fi
echo "docker: $(docker --version)"
echo "commit: $(git -C {APP_DIR} rev-parse --short HEAD)"
""")


def step_config() -> None:
    """Upload runtime.env, have the instance fetch it, then delete the object."""
    from ideagen import platform as plat
    env_text = subprocess.run(
        [PYBIN, str(ROOT / "scripts" / "build_runtime_env.py")],
        capture_output=True, text=True, check=True).stdout
    if "IDEAGEN_MYSQL_PASSWORD=" not in env_text:
        raise SystemExit("runtime.env 内容不完整")

    p = plat.load()
    key = f"deploy/runtime.env.{int(time.time())}"
    p.blobs.put(key, env_text.encode(), content_type="text/plain")
    try:
        import tos
        client = tos.TosClientV2(
            p.blobs._ak, p.blobs._sk, p.blobs._endpoint, p.blobs._region) \
            if hasattr(p.blobs, "_ak") else None
        url = (client.pre_signed_url("GET", p.blobs._bucket, key,
                                     expires=900).signed_url
               if client else None)
        if not url:
            raise SystemExit(
                "无法生成预签名链接——请检查 TOS 适配器是否暴露凭证字段")
        run("write-config", f"""set -euo pipefail
umask 077
curl -fsSL '{url}' -o {CFG_DIR}/runtime.env
chmod 600 {CFG_DIR}/runtime.env
echo "runtime.env 行数: $(wc -l < {CFG_DIR}/runtime.env)"
grep -c '=' {CFG_DIR}/runtime.env
""", quiet=False)
    finally:
        try:
            p.blobs.delete(key)
            print("  临时配置对象已删除")
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠ 临时配置对象删除失败，请手动清理 {key}: {e}")


def step_up() -> None:
    run("build-and-up", f"""set -euo pipefail
cd {APP_DIR}
export IMAGE_TAG="$(git rev-parse --short HEAD)"
docker build -q -t "ideagen40:$IMAGE_TAG" -f deploy/Dockerfile . >/dev/null
IMAGE_TAG="$IMAGE_TAG" docker compose -f deploy/compose.yaml up -d
sleep 15
docker compose -f deploy/compose.yaml ps
""", timeout_s=2400)


def step_verify() -> None:
    out = run("verify", f"""set -euo pipefail
cd {APP_DIR}
echo "--- containers ---"
docker compose -f deploy/compose.yaml ps --format '{{{{.Service}}}} {{{{.State}}}}'
echo "--- healthz (无钥匙应 200) ---"
curl -s -o /dev/null -w '%{{http_code}}\\n' http://127.0.0.1:8765/healthz || true
echo "--- api/state 无钥匙（远端应 401） ---"
curl -s -o /dev/null -w '%{{http_code}}\\n' -H 'X-Forwarded-For: 1.2.3.4' \\
  http://127.0.0.1:8765/api/state || true
""")
    print(out[-1500:])


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=("base", "config", "up", "verify"))
    args = ap.parse_args(argv)

    print(f"目标实例 {INSTANCE}")
    wait_agent()
    steps = {"base": step_base, "config": step_config,
             "up": step_up, "verify": step_verify}
    for name in (["base", "config", "up", "verify"] if not args.only
                 else [args.only]):
        steps[name]()
    print("\n部署完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
