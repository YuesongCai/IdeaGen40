#!/bin/sh
# Everything the display node needs after the repo is on disk.
#
# This lives here rather than in UserData because UserData has an invisible
# ceiling: past roughly 8KB of base64 the BytePlus gateway answers with an HTML
# error page and `ve` reports `invalid character '<'` — a parse error that says
# nothing about size. Two systemd units took the previous version to 7572 of
# 8192 bytes, which is close enough that the next person to add three lines
# would have spent an evening on it.
#
# So UserData now carries only: install docker, clone the repo, run this. The
# script can grow; the query string cannot.
#
# Called with IG_ENV_URL and IG_DB_URL in the environment — short-lived
# presigned URLs, the one kind of credential that is acceptable to pass this
# way because it expires on its own.
set -u

APP=/opt/ideagen/app
DATA=/opt/ideagen/data
CONF=/opt/ideagen/config

mkdir -p "$CONF" "$DATA" && chmod 700 "$CONF" && echo "IG_DIRS"

umask 077
curl -fsS "$IG_ENV_URL" -o "$CONF/runtime.env" \
  && chmod 600 "$CONF/runtime.env" \
  && echo "IG_ENV $(grep -c = "$CONF/runtime.env")" || { echo IG_ENV_FAIL; exit 1; }
umask 022

curl -fsS "$IG_DB_URL" -o "$DATA/ideagen.db" \
  && echo "IG_DB $(stat -c%s "$DATA/ideagen.db")" || { echo IG_DB_FAIL; exit 1; }

# 镜像里跑的是 USER ideagen (uid 10001)，而下载下来的库归 root、0600。
# SQLite 连只读查询也要写(WAL/临时页)，所以不 chown 的话每个 API 都会返回
# "attempt to write a readonly database" —— 服务、数据、网络全对，只差这一步，
# 而且症状看起来像数据没到。
chown -R 10001:10001 "$DATA" && chmod 664 "$DATA/ideagen.db"
echo "IG_PERM $(stat -c'%U:%a' "$DATA/ideagen.db")"

cd "$APP" && docker build -q -t ideagen40:live -f deploy/Dockerfile . >/dev/null 2>&1 \
  && echo IG_BUILD || { echo IG_BUILD_FAIL; exit 1; }

# 账号文件必须落在挂载上，不能落在容器里。容器里的那一份每次代码部署都会被删掉，
# 而部署会自己把运行台里配置的那个管理员重新建出来 —— 于是站点看起来完全正常，
# 只有「上周加的同事今天登不进去」这一个症状，没人会把它跟一次部署联系起来。
docker rm -f ideagen-dash >/dev/null 2>&1
docker run -d --name ideagen-dash --restart always \
  --env-file "$CONF/runtime.env" \
  -e IDEAGEN_DASH_HOST=0.0.0.0 -e IDEAGEN_DB=/data/ideagen.db \
  -e IDEAGEN_ACCOUNTS_FILE=/data/accounts.json \
  -v "$DATA":/data -p 80:8765 -p 443:8765 \
  --entrypoint python3 ideagen40:live -m ideagen.cli serve --port 8765 \
  >/dev/null 2>&1 && echo IG_RUN || { echo IG_RUN_FAIL; exit 1; }

# 两条同步腿。数据腿没有的话，页面会停在部署当晚的快照上而且看起来完全正常；
# 代码腿没有的话，镜像永远停在开机克隆的那个 commit。
install -m 755 "$APP/deploy/sync_state.sh" /opt/ideagen/sync_state.sh
install -m 755 "$APP/deploy/sync_code.sh"  /opt/ideagen/sync_code.sh

write_unit() {  # name, description, exec, onboot, oninterval
  cat > "/etc/systemd/system/$1.service" <<EOF
[Unit]
Description=$2
After=docker.service
Requires=docker.service
[Service]
Type=oneshot
TimeoutStartSec=1800
ExecStart=$3
# 也送到串口控制台。journal 只有登得上机器的人看得到，而这台正是登不上的
# 那台——同步有没有按时跑，必须在 GetConsoleOutput 里看得见。
StandardOutput=journal+console
StandardError=journal+console
EOF
  cat > "/etc/systemd/system/$1.timer" <<EOF
[Unit]
Description=$2
[Timer]
OnBootSec=$4
OnUnitActiveSec=$5
[Install]
WantedBy=timers.target
EOF
}
write_unit ideagen-sync "Install the newest published state snapshot" \
  /opt/ideagen/sync_state.sh 10min 15min
write_unit ideagen-code "Track origin/main on the display node" \
  /opt/ideagen/sync_code.sh 6min 5min

systemctl daemon-reload
systemctl enable --now ideagen-sync.timer ideagen-code.timer >/dev/null 2>&1 \
  && echo "IG_TIMER $(systemctl is-enabled ideagen-sync.timer)/$(systemctl is-enabled ideagen-code.timer)" \
  || echo IG_TIMER_FAIL

# 立刻跑一次，别等第一次定时触发。同步链路要么在开机日志里被证明过，
# 要么就是没被证明过。
sleep 20
/opt/ideagen/sync_state.sh 2>&1 | sed 's/^/IG_SYNC /' || echo IG_SYNC_FAIL

sleep 20
docker ps -a --filter name=ideagen-dash --format 'IG_PS {{.Status}}'
docker logs --tail 15 ideagen-dash 2>&1 | sed 's/^/IG_LOG /'
curl -s -o /dev/null -w 'IG_HEALTHZ_%{http_code}\n' --max-time 10 http://127.0.0.1/healthz || echo IG_HEALTHZ_FAIL
curl -s -o /dev/null -w 'IG_STATE_%{http_code}\n' --max-time 30 http://127.0.0.1/api/state || echo IG_STATE_FAIL
echo "IG_DONE $(date -u +%FT%TZ)"
