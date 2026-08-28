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
for i in $(seq 1 30); do
  URL=$(grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" "$LOG" | head -1)
  [ -n "${URL:-}" ] && break; sleep 1
done
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
wait $CFPID
