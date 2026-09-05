"""Stand up a cloud dashboard that already has the data, in one command.

The problem this solves: an instance can deploy perfectly and still show
nothing. That happened — the stack came up against a freshly provisioned RDS
that had never been written to, so `/api/state` returned 500 while every run,
book and backtest sat in the laptop's SQLite file. A dashboard with no data is
not a deployment.

So this ships the data with the deployment. A consistent SQLite snapshot (via
the backup API — copying the file directly can catch a half-written page) and a
SQLite-mode runtime.env both go to the project's own TOS bucket, and the
instance pulls them through short-lived presigned URLs on first boot. Nothing
long-lived is written into UserData, which the cloud API stores and shows in
the console.

Three things learned the hard way, all encoded here:

* `runcmd`, never `bootcmd` — bootcmd does not execute on this image, which
  cost two silent failures.
* `output: tee` to the serial console. Redirecting into a file on the instance
  puts the diagnosis on the one machine you cannot reach.
* Print `docker ps` and `docker logs` afterwards. "Container started" and
  "port listening" are different claims, and the gap between them is invisible
  without the logs.

The node is an observer: it displays, it does not run the weekly. Two runners
would race for the same period.

  python3 scripts/deploy_userdata_live.py [--name ideagen-live] [--no-create]
"""
from __future__ import annotations

import argparse
import base64
import json
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ZONE = "ap-southeast-1a"
IMAGE = "image-ycdr3l1zguhj8321g8aq"
ITYPE = "ecs.t2-c1m2.large"
SUBNET = "subnet-2kp8582bcfzsw4v7yvpj7mf0c"
SG = "sg-37vjl7fzbs7b44etmwfumdtrn"
KEYPAIR = "ideagen-ecs"
REPO = "https://github.com/YuesongCai/IdeaGen40.git"
URL_TTL_S = 7200
# 66MB 的状态快照在这条链路上传不完 SDK 默认的 30 秒。同一个默认值今天已经
# 让自动同步失败了大半次数，报出来是「http request timeout」，读着像网络故障。
UPLOAD_TIMEOUT_S = 600


def _blobs():
    """The operator's own store, with a timeout sized for a database."""
    from ideagen import platform as plat
    b = plat.load().blobs
    b.timeout_s = UPLOAD_TIMEOUT_S
    b._client = None          # 让下次用新超时重建客户端
    return b


def ve(*args: str) -> dict:
    r = subprocess.run(["ve", *args, "--profile", "byteplus"],
                       capture_output=True, text=True)
    if not r.stdout.strip():
        raise SystemExit(f"ve {' '.join(args[:2])} 无输出: {r.stderr[:300]}")
    return json.loads(r.stdout)


def upload_state() -> str:
    """A consistent snapshot of the state database, presigned for the instance."""
    src = ROOT / "data" / "ideagen.db"
    tmp = pathlib.Path(tempfile.gettempdir()) / "ideagen_snapshot.db"
    s, d = sqlite3.connect(str(src)), sqlite3.connect(str(tmp))
    s.backup(d)
    d.close()
    s.close()
    data = tmp.read_bytes()
    p_blobs = _blobs()
    key = f"deploy/state-{int(time.time())}.db"
    p_blobs.put(key, data, content_type="application/x-sqlite3")
    tmp.unlink()
    print(f"  数据库快照 {len(data) / 1e6:.1f} MB → {key}")
    return p_blobs.presigned_get(key, expires_s=URL_TTL_S)


def upload_env() -> str:
    """runtime.env, rewritten for a display node: SQLite, observer role."""
    env = subprocess.run([sys.executable, str(ROOT / "scripts" / "build_runtime_env.py")],
                         capture_output=True, check=True).stdout.decode()
    lines = [ln for ln in env.splitlines()
             if not ln.startswith(("IDEAGEN_STATE_ENGINE", "IDEAGEN_MYSQL_",
                                   "IDEAGEN_WEEKLY_ROLE"))]
    lines += ["IDEAGEN_STATE_ENGINE=sqlite",
              "IDEAGEN_DB=/data/ideagen.db",
              "IDEAGEN_WEEKLY_ROLE=observer"]
    p_blobs = _blobs()
    key = f"deploy/runtime-{int(time.time())}.env"
    p_blobs.put(key, ("\n".join(lines) + "\n").encode(), content_type="text/plain")
    print(f"  运行配置 {len(lines)} 项 → {key}")
    return p_blobs.presigned_get(key, expires_s=URL_TTL_S)


def user_data(env_url: str, db_url: str) -> str:
    """A loader, not a bootstrap.

    UserData has an invisible ceiling: past roughly 8KB of base64 the gateway
    answers with an HTML error page and `ve` reports `invalid character '<'`,
    a parse error that never mentions size. The previous version of this
    function inlined every step and had reached 7572 of 8192 bytes — three more
    lines from an evening of confusion.

    So everything past "get the repo onto the disk" lives in
    deploy/display_node_bootstrap.sh, which can grow without limit. What stays
    here is what has to: installing docker, cloning, and the two presigned URLs,
    which are the one kind of credential acceptable in UserData because they
    expire on their own.

    `runcmd`, never `bootcmd` — bootcmd does not execute on this image, which
    cost two silent failures. `output: tee` sends everything to the serial
    console; a log file on the instance puts the diagnosis on the one machine
    nobody can reach.
    """
    return f"""#cloud-config
output: {{all: '| tee -a /var/log/cloud-init-output.log'}}
runcmd:
  - echo "IG_START $(date -u +%FT%TZ)"
  - [ sh, -c, "for i in 1 2 3; do apt-get update -qq && break || sleep 10; done; apt-get install -y -qq docker.io git curl >/dev/null 2>&1; systemctl enable --now docker >/dev/null 2>&1; echo IG_DOCKER $(docker --version 2>/dev/null | head -c 30)" ]
  - [ sh, -c, "git clone --quiet --depth 50 {REPO} /opt/ideagen/app && echo IG_CLONE $(git -C /opt/ideagen/app rev-parse --short HEAD) || echo IG_CLONE_FAIL" ]
  - [ sh, -c, "IG_ENV_URL='{env_url}' IG_DB_URL='{db_url}' sh /opt/ideagen/app/deploy/display_node_bootstrap.sh" ]
"""


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="ideagen-live")
    ap.add_argument("--no-create", action="store_true",
                    help="只上传数据与配置并打印 UserData，不创建实例")
    args = ap.parse_args(argv)

    print("上传数据与配置…")
    db_url, env_url = upload_state(), upload_env()
    ud = user_data(env_url, db_url)
    for secret in ("ARK_API_KEY=", "MYSQL_PASSWORD=", "DASH_KEY="):
        if secret in ud:
            raise SystemExit(f"UserData 含长期密钥 {secret}，已中止")
    if args.no_create:
        print(ud)
        return 0

    print("创建实例…")
    res = ve("ecs", "RunInstances", "--ZoneId", ZONE, "--ImageId", IMAGE,
             "--InstanceType", ITYPE, "--InstanceChargeType", "PostPaid",
             "--NetworkInterfaces.1.SubnetId", SUBNET,
             "--NetworkInterfaces.1.SecurityGroupIds.1", SG,
             "--KeyPairName", KEYPAIR, "--InstanceName", args.name,
             "--Volumes.1.VolumeType", "ESSD_PL0", "--Volumes.1.Size", "40",
             "--UserData", base64.b64encode(ud.encode()).decode())
    iid = (res.get("Result") or {}).get("InstanceIds", [None])[0]
    if not iid:
        raise SystemExit(f"创建失败: {json.dumps(res)[:300]}")
    print(f"  实例 {iid}")

    for _ in range(20):
        st = ve("ecs", "DescribeInstances", "--InstanceIds.1",
                iid)["Result"]["Instances"][0].get("Status")
        if st == "RUNNING":
            break
        time.sleep(8)
    eip = ve("vpc", "AllocateEipAddress", "--BillingType", "3",
             "--Bandwidth", "5", "--Name", f"{args.name}-eip")["Result"]
    ve("vpc", "AssociateEipAddress", "--AllocationId", eip["AllocationId"],
       "--InstanceId", iid, "--InstanceType", "EcsInstance")
    print(f"  公网 {eip['EipAddress']}")
    print(f"\n用 `ve ecs GetConsoleOutput --InstanceId {iid}` 看 IG_* 标记；"
          f"起来后访问 http://{eip['EipAddress']}/review?key=<IDEAGEN_DASH_KEY>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
