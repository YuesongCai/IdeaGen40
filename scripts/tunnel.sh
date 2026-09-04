#!/usr/bin/env bash
# Public tunnel for the dashboard. Quick tunnels get a fresh URL on every start,
# so the URL is written to a known file and DM'd to the owner each time —
# a public address nobody knows is indistinguishable from no address.
set -u
LOG=/Users/yuesongcai/IdeaGen40/data/logs/tunnel.log
URLFILE=/Users/yuesongcai/IdeaGen40/data/logs/tunnel_url.txt
: > "$LOG"
/opt/homebrew/bin/cloudflared tunnel --protocol http2 --url http://localhost:8765 >> "$LOG" 2>&1 &
CFPID=$!
# cloudflared 的失败信息里也含 api.trycloudflare.com——那是它请求隧道的接口，
# 不是隧道地址。把它当地址写进文件，就等于给用户发一个必然打不开的链接。
for i in $(seq 1 30); do
  URL=$(grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" "$LOG" \
        | grep -v "^https://api\.trycloudflare\.com$" | head -1)
  [ -n "${URL:-}" ] && break; sleep 1
done
if [ -z "${URL:-}" ]; then
  echo "$(date -u +%FT%TZ) 30s 内没拿到隧道地址，退出让 launchd 重试" >> "$LOG"
  kill $CFPID 2>/dev/null || true
  exit 1
fi
if [ -n "${URL:-}" ]; then
  OLD=$(cat "$URLFILE" 2>/dev/null || true)
  echo "$URL" > "$URLFILE"
  if [ "$URL" != "$OLD" ]; then
    KEY=$(grep '^IDEAGEN_DASH_KEY=' ~/.ideagen.env | cut -d= -f2)
    /opt/homebrew/bin/lark-cli im +messages-send --as bot \
      --user-id ou_8d0e4064f46c1d0de14c501c1f5db808 \
      --text "🌐 运行台公网地址已更新：${URL}/review?key=${KEY} （首次带钥匙打开，之后走 Cookie；地址在隧道重启后会变，变了会再通知）" \
      >/dev/null 2>&1 || true
  fi
fi
# 一个还在的进程不等于一个还能用的地址：quick tunnel 会被上游回收，
# cloudflared 却会抱着同一个已失效的隧道无限重连（日志里是
# "Unauthorized: Tunnel not found"），KeepAlive 永远等不到它退出。
# 所以自己盯：连续三次探不通 /healthz 就杀掉它退出，让 launchd 重新拉起，
# 换一条新隧道、发一条新地址。
FAILS=0
while kill -0 $CFPID 2>/dev/null; do
  sleep 60
  if [ -z "${URL:-}" ]; then break; fi
  if curl -fsS --max-time 20 -o /dev/null "$URL/healthz"; then
    FAILS=0
  else
    FAILS=$((FAILS + 1))
    echo "$(date -u +%FT%TZ) 隧道地址探测失败 ${FAILS}/3: $URL" >> "$LOG"
    if [ "$FAILS" -ge 3 ]; then
      echo "$(date -u +%FT%TZ) 连续三次探不通，重启隧道以换取新地址" >> "$LOG"
      kill $CFPID 2>/dev/null || true
      wait $CFPID 2>/dev/null || true
      exit 1
    fi
  fi
done
wait $CFPID
