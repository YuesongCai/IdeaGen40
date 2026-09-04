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
    """
    script = BOOTSTRAP.read_text(encoding="utf-8")
    if PLACEHOLDER not in script:
        raise SystemExit(f"{BOOTSTRAP.name} 缺少 {PLACEHOLDER} 占位符")
    if runtime_env_url:
        script = script.replace(PLACEHOLDER, runtime_env_url)
    body = "\n".join(("    " + ln) if ln else "" for ln in script.splitlines())
    return (
        "#cloud-config\n"
        "# IdeaGen40 production bootstrap — runs on EVERY boot (bootcmd), so a\n"
        "# reboot is the deploy. Source of truth: deploy/instance_bootstrap.sh.\n"
        "# Do not hand-edit here; edit that file and re-run deploy_userdata.py.\n"
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
    print("已下发重启，等待引导脚本……")
    return cmd_status(argparse.Namespace(wait=600))


def cmd_status(args) -> int:
    """What the instance says about itself, in its own words.

    While bootstrapping, :80 is a plain status page. Once the stack is up Caddy
    owns :80 and /healthz answers — so which one replies is itself the state.
    """
    deadline = time.time() + max(getattr(args, "wait", 0), 1)
    last = ""
    while True:
        for path, label in (("/healthz", "stack"), ("/", "bootstrap")):
            try:
                with urllib.request.urlopen(
                        f"http://{PUBLIC_IP}{path}", timeout=8) as r:
                    body = r.read(4000).decode("utf-8", "replace").strip()
                if label == "stack":
                    print(f"✅ 栈已就绪：http://{PUBLIC_IP}/review  ({body[:120]})")
                    return 0
                if body != last:
                    print(body)
                    last = body
                break
            except (urllib.error.URLError, OSError, TimeoutError):
                continue
        else:
            if not last:
                print("实例还没有任何东西在监听 :80（引导中或未重启）")
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
