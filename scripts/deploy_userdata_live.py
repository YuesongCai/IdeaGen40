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


def ve(*args: str) -> dict:
    r = subprocess.run(["ve", *args, "--profile", "byteplus"],
                       capture_output=True, text=True)
    if not r.stdout.strip():
        raise SystemExit(f"ve {' '.join(args[:2])} 无输出: {r.stderr[:300]}")
    return json.loads(r.stdout)


def upload_state() -> str:
    """A consistent snapshot of the state database, presigned for the instance."""
    from ideagen import platform as plat
    src = ROOT / "data" / "ideagen.db"
    tmp = pathlib.Path(tempfile.gettempdir()) / "ideagen_snapshot.db"
    s, d = sqlite3.connect(str(src)), sqlite3.connect(str(tmp))
    s.backup(d)
    d.close()
    s.close()
    data = tmp.read_bytes()
    p = plat.load()
    key = f"deploy/state-{int(time.time())}.db"
    p.blobs.put(key, data, content_type="application/x-sqlite3")
    tmp.unlink()
    print(f"  数据库快照 {len(data) / 1e6:.1f} MB → {key}")
    return p.blobs.presigned_get(key, expires_s=URL_TTL_S)


def upload_env() -> str:
    """runtime.env, rewritten for a display node: SQLite, observer role."""
    from ideagen import platform as plat
    env = subprocess.run([sys.executable, str(ROOT / "scripts" / "build_runtime_env.py")],
                         capture_output=True, check=True).stdout.decode()
    lines = [ln for ln in env.splitlines()
             if not ln.startswith(("IDEAGEN_STATE_ENGINE", "IDEAGEN_MYSQL_",
                                   "IDEAGEN_WEEKLY_ROLE"))]
    lines += ["IDEAGEN_STATE_ENGINE=sqlite",
              "IDEAGEN_DB=/data/ideagen.db",
              "IDEAGEN_WEEKLY_ROLE=observer"]
    p = plat.load()
    key = f"deploy/runtime-{int(time.time())}.env"
    p.blobs.put(key, ("\n".join(lines) + "\n").encode(), content_type="text/plain")
    print(f"  运行配置 {len(lines)} 项 → {key}")
    return p.blobs.presigned_get(key, expires_s=URL_TTL_S)


def user_data(env_url: str, db_url: str) -> str:
    return f"""#cloud-config
output: {{all: '| tee -a /var/log/cloud-init-output.log'}}
write_files:
  - path: /etc/systemd/system/ideagen-sync.service
    content: |
      [Unit]
      Description=Install the newest published IdeaGen state snapshot
      After=docker.service
      Requires=docker.service
      [Service]
      Type=oneshot
      ExecStart=/opt/ideagen/sync_state.sh
  - path: /etc/systemd/system/ideagen-sync.timer
    content: |
      [Unit]
      Description=Track the laptop's published state
      [Timer]
      OnBootSec=10min
      OnUnitActiveSec=15min
      [Install]
      WantedBy=timers.target
runcmd:
  - echo "IG_START $(date -u +%FT%TZ)"
  - [ sh, -c, "for i in 1 2 3; do apt-get update -qq && break || sleep 10; done; apt-get install -y -qq docker.io git curl >/dev/null 2>&1; systemctl enable --now docker >/dev/null 2>&1; echo IG_DOCKER $(docker --version 2>/dev/null | head -c 30)" ]
  - [ sh, -c, "mkdir -p /opt/ideagen/config /opt/ideagen/data && chmod 700 /opt/ideagen/config; echo IG_DIRS" ]
  - [ sh, -c, "git clone --quiet --depth 50 {REPO} /opt/ideagen/app && echo IG_CLONE $(git -C /opt/ideagen/app rev-parse --short HEAD) || echo IG_CLONE_FAIL" ]
  - [ sh, -c, "umask 077; curl -fsS '{env_url}' -o /opt/ideagen/config/runtime.env && chmod 600 /opt/ideagen/config/runtime.env && echo IG_ENV $(grep -c = /opt/ideagen/config/runtime.env) || echo IG_ENV_FAIL" ]
  - [ sh, -c, "curl -fsS '{db_url}' -o /opt/ideagen/data/ideagen.db && echo IG_DB $(stat -c%s /opt/ideagen/data/ideagen.db) || echo IG_DB_FAIL" ]
  # 镜像里跑的是 USER ideagen (uid 10001)，而下载下来的库归 root、0600。
  # SQLite 连只读查询也要写(WAL/临时页)，所以不 chown 的话每个 API 都会
  # 返回 "attempt to write a readonly database" —— 服务、数据、网络全对，
  # 只差这一步，而且症状看起来像数据没到。
  - [ sh, -c, "chown -R 10001:10001 /opt/ideagen/data && chmod 664 /opt/ideagen/data/ideagen.db && echo IG_PERM $(stat -c'%U:%a' /opt/ideagen/data/ideagen.db)" ]
  - [ sh, -c, "cd /opt/ideagen/app && docker build -q -t ideagen40:live -f deploy/Dockerfile . >/dev/null 2>&1 && echo IG_BUILD || echo IG_BUILD_FAIL" ]
  - [ sh, -c, "docker rm -f ideagen-dash >/dev/null 2>&1; docker run -d --name ideagen-dash --restart always --env-file /opt/ideagen/config/runtime.env -e IDEAGEN_DASH_HOST=0.0.0.0 -e IDEAGEN_DB=/data/ideagen.db -v /opt/ideagen/data:/data -p 80:8765 -p 443:8765 --entrypoint python3 ideagen40:live -m ideagen.cli serve --port 8765 && echo IG_RUN || echo IG_RUN_FAIL" ]
  # 数据同步：本机每天还在跑 daily，这台只是显示。没有这一步，页面会停在
  # 部署当晚的快照上，而且看起来完全正常——这是最难发现的那种错。
  - [ sh, -c, "install -m 755 /opt/ideagen/app/deploy/sync_state.sh /opt/ideagen/sync_state.sh && systemctl daemon-reload && systemctl enable --now ideagen-sync.timer >/dev/null 2>&1 && echo IG_TIMER $(systemctl is-enabled ideagen-sync.timer) || echo IG_TIMER_FAIL" ]
  # 立刻跑一次，别等 10 分钟后的第一次触发。同步链路要么在开机日志里被
  # 证明过，要么就是没被证明过。
  - [ sh, -c, "sleep 20; /opt/ideagen/sync_state.sh 2>&1 | sed 's/^/IG_SYNC /' || echo IG_SYNC_FAIL" ]
  - [ sh, -c, "sleep 25; docker ps -a --filter name=ideagen-dash --format 'IG_PS {{{{.Status}}}}'" ]
  - [ sh, -c, "docker logs --tail 25 ideagen-dash 2>&1 | sed 's/^/IG_LOG /'" ]
  - [ sh, -c, "curl -s -o /dev/null -w 'IG_HEALTHZ_%{{http_code}}\\n' --max-time 10 http://127.0.0.1/healthz || echo IG_HEALTHZ_FAIL" ]
  - [ sh, -c, "curl -s -o /dev/null -w 'IG_STATE_%{{http_code}}\\n' --max-time 20 http://127.0.0.1/api/state || echo IG_STATE_FAIL" ]
  - echo "IG_DONE $(date -u +%FT%TZ)"
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
