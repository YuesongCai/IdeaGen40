"""Drive the production instance through cloud-init UserData.

`deploy_cloud.py` is the better path and stays the primary one: it runs shell on
the instance through ECS Cloud Assistant. But Cloud Assistant reports
InstallFailed on this image, and the operator machine's proxy breaks SSH to every
host (github.com fails identically to the instance, on :22 and on :443), so on a
cold start there is no channel at all to bootstrap it from.

UserData is the one execution path left. `deploy/instance_bootstrap.sh` runs on
every boot, which makes a reboot the deploy — it installs Docker, pulls
origin/main, builds the image, and (once runtime.env exists) starts the stack. It
also re-attempts the Cloud Assistant install, so a successful boot hands the job
back to `deploy_cloud.py`.

  python3 scripts/deploy_userdata.py push     # ship the bootstrap (no secrets)
  python3 scripts/deploy_userdata.py secrets  # + a short-lived presigned runtime.env
  python3 scripts/deploy_userdata.py reboot   # run it
  python3 scripts/deploy_userdata.py status   # what the instance says about itself

`secrets` and `reboot` change or restart production, so they are separate verbs:
nothing here reboots an instance as a side effect of shipping a file.
"""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

INSTANCE = "i-yeu80pr2tc3z47gon4sy"
REGION = "ap-southeast-1"
PUBLIC_IP = "101.47.152.106"
# The instance's own bucket, not the operator's. `plat.load()` here would read
# the laptop's ~/.ideagen.env and quietly stage production config in the wrong
# bucket — the same class of mistake as a laptop's observer role leaking into
# the instance that runs the week.
PROD_BUCKET = "ideagen-prod-4b869b"
PROD_ENDPOINT = "tos-ap-southeast-1.bytepluses.com"
BOOTSTRAP = ROOT / "deploy" / "instance_bootstrap.sh"
PLACEHOLDER = "__RUNTIME_ENV_URL__"
REPO_URL = "https://github.com/YuesongCai/IdeaGen40.git"
PRESIGN_S = 7200


def _credentialed_env() -> dict:
    """`ve` reads the same keys the pipeline does, from the operator env file.

    Without this the script only works in a shell that happened to source
    ~/.ideagen.env first, and the failure looks like a cloud permission problem
    rather than a missing export.
    """
    import os
    env = dict(os.environ)
    if env.get("VOLCENGINE_ACCESS_KEY") and env.get("VOLCENGINE_SECRET_KEY"):
        return env
    path = Path.home() / ".ideagen.env"
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.strip() in ("BYTEPLUS_ACCESS_KEY", "BYTEPLUS_SECRET_KEY"):
            env.setdefault(k.strip(), v.strip())
    env.setdefault("VOLCENGINE_ACCESS_KEY", env.get("BYTEPLUS_ACCESS_KEY", ""))
    env.setdefault("VOLCENGINE_SECRET_KEY", env.get("BYTEPLUS_SECRET_KEY", ""))
    return env


def ve(*args: str) -> dict:
    r = subprocess.run(["ve", *args, "--region", REGION],
                       capture_output=True, text=True, env=_credentialed_env())
    body = r.stdout.strip()
    if not body:
        raise SystemExit(f"ve {' '.join(args[:2])} 无输出: {r.stderr[:300]}")
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        raise SystemExit(f"ve 返回非 JSON: {body[:300]}")


def _prod_blobs():
    """A blob store pointed at the instance's bucket, using the operator's keys."""
    from ideagen.platform.byteplus import TosBlobStore
    env = _credentialed_env()
    ak = env.get("BYTEPLUS_ACCESS_KEY") or env.get("VOLCENGINE_ACCESS_KEY")
    sk = env.get("BYTEPLUS_SECRET_KEY") or env.get("VOLCENGINE_SECRET_KEY")
    return TosBlobStore(ak=ak, sk=sk, bucket=PROD_BUCKET, region=REGION,
                        endpoint=PROD_ENDPOINT)


def build_userdata(runtime_env_url: str | None) -> str:
    """Wrap the bootstrap into cloud-config, in plain readable text.

    Not base64: UserData is meant to be read from the console, and an operator
    who cannot read what their instance runs at boot has no way to audit it.

    The explanatory comments do not travel, though. The CLI sends this in a
    query string, and past roughly 8KB the gateway answers with an HTML error
    page instead of JSON — which surfaces as a parse error with nothing to do
    with size. The prose lives in the repo file, which the header names; what
    ships is the commands.
    """
    if PLACEHOLDER not in BOOTSTRAP.read_text(encoding="utf-8"):
        raise SystemExit(f"{BOOTSTRAP.name} 缺少 {PLACEHOLDER} 占位符")
    url = runtime_env_url or ""
    loader = f"""#!/bin/bash
mkdir -p /opt/ideagen/config /opt/ideagen/app /opt/ideagen/health /opt/ideagen/oauth
# The image runs as uid 10001 and writes refreshed OAuth tokens into this
# mount; root-owned 0700 makes even the existence check raise.
chmod 700 /opt/ideagen/oauth; chown -R 10001:10001 /opt/ideagen/oauth 2>/dev/null || true
CONF=/opt/ideagen/config/runtime.env
if [ -n '{url}' ]; then
  umask 077; printf '%s' '{url}' > /opt/ideagen/config/.runtime_env_url
  # Config delivery happens HERE rather than in the bootstrap, because the
  # bootstrap has to build an image first and an updater container recreates
  # the app from origin/main every couple of minutes regardless. That means new
  # CODE reaches the box while new CONFIG does not, and the box looks deployed
  # while the keys never arrive. The loader has nothing to stall on.
  if curl -fsS --max-time 60 '{url}' -o "$CONF.new"; then
    if [ -s "$CONF" ]; then
      awk -F= 'NR==FNR{{ if ($0 ~ /^[A-Za-z_][A-Za-z0-9_]*=/) seen[$1]=1; next }}
               $0 ~ /^[A-Za-z_][A-Za-z0-9_]*=/ && !seen[$1]' \\
          "$CONF.new" "$CONF" > "$CONF.keep"
      cat "$CONF.new" "$CONF.keep" > "$CONF"; rm -f "$CONF.keep"
    else
      cp "$CONF.new" "$CONF"
    fi
    rm -f "$CONF.new"; chmod 600 "$CONF"
  fi
fi
for i in 1 2 3; do apt-get update -qq && break || sleep 10; done
apt-get install -y -qq git ca-certificates curl || true
if [ -d /opt/ideagen/app/.git ]; then
  git -C /opt/ideagen/app fetch -q origin main \\
    && git -C /opt/ideagen/app reset -q --hard origin/main
else
  git clone -q --depth 50 {REPO_URL} /opt/ideagen/app
fi
B=/opt/ideagen/app/deploy/instance_bootstrap.sh
[ -s "$B" ] || B=/var/lib/ideagen-boot-last.sh
[ -s "$B" ] || {{ echo "no bootstrap available" >/opt/ideagen/health/index.html; exit 1; }}
cp "$B" /var/lib/ideagen-boot-last.sh
exec bash "$B"
"""
    body = "\n".join(("    " + ln) if ln else "" for ln in loader.splitlines())
    return (
        "#cloud-config\n"
        "# IdeaGen40 production bootstrap — runs on EVERY boot (bootcmd), so a\n"
        "# reboot is the deploy. This is only a loader: it fetches the repo and\n"
        "# execs deploy/instance_bootstrap.sh, which is the source of truth.\n"
        "#\n"
        "# The script used to be inlined here. UserData travels in a query\n"
        "# string that the gateway rejects past ~8KB with an HTML error page,\n"
        "# which surfaces as a JSON parse error naming no size at all — so the\n"
        "# bootstrap silently acquired a length limit nobody could see, and\n"
        "# adding a few lines to it broke deployment for everyone. A loader has\n"
        "# no such ceiling, and stops UserData being a stale copy of the file.\n"
        "bootcmd:\n"
        "  - |\n"
        "    cat > /var/lib/ideagen-boot.sh <<'IDEAGEN_BOOT_EOF'\n"
        f"{body}\n"
        "    IDEAGEN_BOOT_EOF\n"
        "    nohup bash /var/lib/ideagen-boot.sh >/dev/null 2>&1 &\n"
    )


def push(userdata: str, *, what: str) -> None:
    ve("ecs", "ModifyInstanceAttribute", "--InstanceId", INSTANCE,
       "--UserData", base64.b64encode(userdata.encode()).decode())
    print(f"UserData 已更新（{what}，{len(userdata)} 字节）。"
          "重启后生效：python3 scripts/deploy_userdata.py reboot")


def cmd_push(_args) -> int:
    push(build_userdata(None), what="无凭证")
    return 0


def cmd_secrets(_args) -> int:
    """Upload runtime.env, hand the instance one short-lived presigned GET.

    The URL is a bearer credential, so it is never printed and never stored: it
    goes straight into the UserData the instance reads at boot, expires in two
    hours, and `python3 scripts/deploy_userdata.py forget` deletes the object.
    """
    env_text = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "build_runtime_env.py")],
        capture_output=True, text=True, check=True).stdout
    if "IDEAGEN_MYSQL_PASSWORD=" not in env_text:
        raise SystemExit("runtime.env 内容不完整")
    n = len([ln for ln in env_text.splitlines() if "=" in ln])

    store = _prod_blobs()
    key = f"deploy/runtime.env.{int(time.time())}"
    store.put(key, env_text.encode(), content_type="text/plain")
    url = store.presigned_get(key, expires_s=PRESIGN_S)
    (ROOT / "data" / ".last_runtime_env_key").write_text(key, encoding="utf-8")
    print(f"runtime.env（{n} 项）已上传 {key}，presign {PRESIGN_S // 3600}h")
    push(build_userdata(url), what="含一次性 runtime.env 取回地址")
    print("实例取到后请执行：python3 scripts/deploy_userdata.py forget")
    return 0


def cmd_forget(_args) -> int:
    """Delete the temporary runtime.env object once the instance has it.

    The blob port has no `delete` on purpose — run artifacts are immutable, and
    a delete on that interface would be a foot-gun aimed at the record. This
    object is not an artifact: it is a credential in transit, and leaving it in
    the bucket is the actual risk. So the deletion goes through the SDK here,
    scoped to the one key this script wrote.
    """
    marker = ROOT / "data" / ".last_runtime_env_key"
    if not marker.exists():
        print("没有待清理的对象")
        return 0
    key = marker.read_text(encoding="utf-8").strip()
    store = _prod_blobs()
    store._c().delete_object(PROD_BUCKET, store._k(key))
    marker.unlink()
    print(f"已删除 tos://{PROD_BUCKET}/{key}")
    return 0


def cmd_reboot(_args) -> int:
    ve("ecs", "RebootInstance", "--InstanceId", INSTANCE)
    # A cold boot installs Docker and builds the image from scratch on a 2C/4G
    # box; ten minutes is normal and being told "timed out" at minute nine would
    # be the tool lying about the instance.
    print("已下发重启，等待引导脚本……（首次约 6-12 分钟：装 docker、拉代码、构建镜像）")
    return cmd_status(argparse.Namespace(wait=1200))


def cmd_status(args) -> int:
    """What the instance says about itself, in its own words.

    While bootstrapping, :80 is a plain status page. Once the stack is up the
    proxy owns both ports, redirects :80 to HTTPS and answers 401 until someone
    logs in — so this probe asks over HTTPS and treats 401 as alive. Checking
    :80 for a 200 reported a healthy instance as "nothing is listening", and a
    probe that cries dead over a working box is worse than no probe: the one
    time it is right, nobody believes it.
    """
    import ssl
    from urllib.request import Request, urlopen

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # the cert may still be being issued

    deadline = time.time() + max(getattr(args, "wait", 0), 1)
    last = ""
    while True:
        try:
            with urlopen(Request(f"https://{PUBLIC_IP}/healthz"),
                         timeout=8, context=ctx) as r:
                body = r.read(400).decode("utf-8", "replace").strip()
            print(f"✅ 栈已就绪：https://{PUBLIC_IP}/review  ({body[:120]})")
            return 0
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                print(f"✅ 栈已就绪：https://{PUBLIC_IP}/review "
                      f"（HTTP {e.code}，代理在要登录——这就是它活着的证据）")
                return 0
        except (urllib.error.URLError, OSError, TimeoutError):
            pass

        # Not up yet: the bootstrap serves a plain status page on :80 until the
        # proxy takes over, and that page is the only view into a boot with no
        # shell attached to it.
        try:
            with urlopen(f"http://{PUBLIC_IP}/", timeout=8) as r:
                body = r.read(4000).decode("utf-8", "replace").strip()
            if body and body != last:
                print(body)
                last = body
        except (urllib.error.URLError, OSError, TimeoutError):
            if not last:
                print("实例还没有任何东西在监听（引导中或未重启）")
                last = "-"
        if time.time() >= deadline:
            return 1
        time.sleep(15)


def main() -> int:
    ap = argparse.ArgumentParser("deploy_userdata")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn, help_ in (
            ("push", cmd_push, "ship the bootstrap (no credentials)"),
            ("secrets", cmd_secrets, "ship it with a one-shot runtime.env URL"),
            ("forget", cmd_forget, "delete the temporary runtime.env object"),
            ("reboot", cmd_reboot, "reboot the instance and watch it come up"),
            ("status", cmd_status, "poll what the instance reports")):
        s = sub.add_parser(name, help=help_)
        s.set_defaults(fn=fn)
        if name == "status":
            s.add_argument("--wait", type=int, default=1,
                           help="keep polling for N seconds")
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
