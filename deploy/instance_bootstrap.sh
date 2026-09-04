#!/bin/bash
# IdeaGen40 instance bootstrap — the source of truth for what the production
# instance does. `scripts/deploy_cloud.py` wraps this file into cloud-init
# UserData; nothing else should hand-edit the UserData.
#
# It runs on EVERY boot (cloud-init `bootcmd`), which makes a reboot the deploy:
# pull origin/main, rebuild the image, restart the stack. That indirection is not
# a preference. The operator machine's proxy breaks SSH to every host (github.com
# fails identically to the instance, on :22 and on :443), and ECS Cloud Assistant
# will not install on this image, so cloud-init is the only execution path the
# platform still offers.
#
# NO SECRETS IN THIS FILE. UserData is stored by the cloud API and readable from
# the console. runtime.env is delivered separately; until it exists this script
# installs everything else and says, on the status page, that it is waiting —
# a stack that came up half-configured would be worse than an honest "waiting".
set -x
exec >>/var/log/ideagen-bootstrap.log 2>&1
echo "=== boot $(date -u +%FT%TZ) ==="
export DEBIAN_FRONTEND=noninteractive

mkdir -p /opt/ideagen/app /opt/ideagen/config /opt/ideagen/oauth /opt/ideagen/health
chmod 700 /opt/ideagen/config /opt/ideagen/oauth
# The dashboard writes refreshed Olive tokens into the oauth mount, and the
# image runs as uid 10001 (deploy/Dockerfile), so root-owned 0700 leaves it
# with a PermissionError the moment a token needs renewing. Config stays
# root-only: nothing in the container reads it, compose passes it by env_file.
chown -R 10001:10001 /opt/ideagen/oauth   # -R: a tokens.json left
# behind by an earlier root-owned run is unreadable to the app otherwise,
# and config.olive_credentials() reads it before anything else runs.
STATUS=/opt/ideagen/health/index.html
: > "$STATUS"
say(){ echo "$(date -u +%FT%TZ)  $*" >> "$STATUS"; echo "STEP: $*"; }

# A status page on :80 is the only thing that tells "still installing" apart from
# "unreachable" — with no SSH those two look identical from the operator's side.
# Caddy takes this port later, so the probe is stopped before the stack comes up.
probe_up(){ pkill -f "http.server 80" >/dev/null 2>&1 || true; sleep 1
  ( cd /opt/ideagen/health && setsid nohup python3 -m http.server 80 >/var/log/ideagen-probe.log 2>&1 & ) ; }
probe_up

say "bootstrap start"
for i in 1 2 3; do apt-get update -qq && break || sleep 10; done
if apt-get install -y -qq docker.io docker-compose-v2 git curl ca-certificates; then say "packages ok"; else say "packages FAILED"; fi
systemctl enable --now docker && say "docker $(docker --version 2>/dev/null)"

if [ -d /opt/ideagen/app/.git ]; then
  git -C /opt/ideagen/app fetch -q origin main && git -C /opt/ideagen/app reset -q --hard origin/main
else
  git clone -q --depth 50 https://github.com/YuesongCai/IdeaGen40.git /opt/ideagen/app
fi
SHA=$(git -C /opt/ideagen/app rev-parse --short HEAD 2>/dev/null || echo none)
say "code $SHA"

cd /opt/ideagen/app || exit 1
if docker build -q -f deploy/Dockerfile -t "ideagen40:$SHA" . ; then say "image ideagen40:$SHA built"; else say "image build FAILED"; fi

# Secrets arrive by one short-lived presigned GET, written by `deploy_cloud.py
# secrets`. An unsubstituted placeholder means no delivery has been authorised
# yet — that is a normal state, not an error.
#
# A successful delivery is MERGED into whatever is already on disk rather than
# skipped: skipping meant a running instance could never be given a new key
# (adding Olive to a box that already had a runtime.env was impossible without
# a shell, and there is no shell on this image). Delivered keys win; keys only
# the instance has — a token it refreshed for itself — survive. A fetch that
# fails changes nothing at all, which is what the old skip was protecting.
RUNTIME_ENV_URL='__RUNTIME_ENV_URL__'
CONF=/opt/ideagen/config/runtime.env
if [ "${RUNTIME_ENV_URL#__}" = "$RUNTIME_ENV_URL" ]; then
  if curl -fsS --max-time 60 "$RUNTIME_ENV_URL" -o "$CONF.new"; then
    if [ -s "$CONF" ]; then
      awk -F= 'NR==FNR{ if ($0 ~ /^[A-Za-z_][A-Za-z0-9_]*=/) seen[$1]=1; next }
               $0 ~ /^[A-Za-z_][A-Za-z0-9_]*=/ && !seen[$1]' \
          "$CONF.new" "$CONF" > "$CONF.keep"
      cat "$CONF.new" "$CONF.keep" > "$CONF"
      rm -f "$CONF.keep"
      say "runtime.env merged ($(grep -c = "$CONF") keys)"
    else
      cp "$CONF.new" "$CONF"
      say "runtime.env fetched"
    fi
    rm -f "$CONF.new"
    chmod 600 "$CONF"
  else
    say "runtime.env FETCH FAILED (presigned URL expired?) — keeping existing"
    rm -f "$CONF.new"
  fi
fi

if [ ! -s /opt/ideagen/config/runtime.env ]; then
  say "WAITING for /opt/ideagen/config/runtime.env — stack not started"
  say "run: python3 scripts/deploy_cloud.py secrets && python3 scripts/deploy_cloud.py reboot"
  curl -fsSL https://iam-cloud-assistant-ap-southeast-1.tos-ap-southeast-1.bytepluses.com/linux/install.sh -o /tmp/ca.sh 2>/dev/null \
    && bash /tmp/ca.sh >/dev/null 2>&1 || true
  echo "=== bootstrap paused (no runtime.env) $(date -u +%FT%TZ) ==="
  exit 0
fi
chmod 600 /opt/ideagen/config/runtime.env
say "runtime.env present ($(grep -c = /opt/ideagen/config/runtime.env) keys)"

# Hand :80 to Caddy. If the stack fails to come up the probe comes back carrying
# the tail of this log, so a failure stays visible from outside.
# The oauth mount is chowned again here, after runtime.env is in place and
# `say` exists, because the early chown at the top runs before there is any way
# to report it -- and a token directory the app cannot read is invisible until
# something needs a token. Printing the resulting mode and contents makes the
# state of this one directory readable from outside the instance, which is the
# only view of it there is on an image with no shell.
chown -R 10001:10001 /opt/ideagen/oauth 2>/dev/null || true
say "oauth dir: $(stat -c '%U:%G %a' /opt/ideagen/oauth 2>/dev/null) contents=[$(ls -A /opt/ideagen/oauth 2>/dev/null | tr '\n' ' ')]"
say "olive configured: $(grep -c '^OLIVE_' /opt/ideagen/config/runtime.env) keys"

say "starting dashboard + proxy"
pkill -f "http.server 80" >/dev/null 2>&1 || true
sleep 1
export IMAGE_TAG="$SHA"

# HTTPS, not because a dashboard is glamorous but because the credential that
# opens it travels on every single request. Let's Encrypt issues short-lived
# certificates for bare IPs, which is what this host has instead of a name.
PUBLIC_IP=$(curl -fsS --max-time 10 http://100.96.0.96/latest/meta-data/public-ipv4 2>/dev/null \
            || echo "101.47.152.106")
export IDEAGEN_PUBLIC_HOST="$PUBLIC_IP"
export IDEAGEN_PUBLIC_SITE="https://$PUBLIC_IP"
export IDEAGEN_DEFAULT_SNI="$PUBLIC_IP"

# The proxy needs three values and only three: who may log in, the bcrypt of
# their password, and the key it presents upstream on their behalf. They are
# read out of runtime.env one at a time rather than by sourcing it, so a
# database password cannot ride along into a container that only forwards HTTP.
getenv(){ sed -n "s/^$1=//p" /opt/ideagen/config/runtime.env | tail -1; }
export IDEAGEN_DASH_KEY="$(getenv IDEAGEN_DASH_KEY)"
export IDEAGEN_DASH_USER="$(getenv IDEAGEN_DASH_USER)"
DASH_PW="$(getenv IDEAGEN_DASH_PASSWORD)"
if [ -n "$DASH_PW" ] && [ -n "$IDEAGEN_DASH_USER" ]; then
  # caddy hashes its own passwords; nothing else here needs a bcrypt library,
  # and the plaintext never leaves this shell.
  export IDEAGEN_DASH_HASH="$(docker run --rm caddy:2-alpine \
      caddy hash-password --plaintext "$DASH_PW" 2>/dev/null | tr -d '\r\n')"
fi
if [ -z "${IDEAGEN_DASH_HASH:-}" ]; then
  # Refusing here is the point: an empty hash makes Caddy's basic_auth block
  # unparseable, and a proxy that will not start looks exactly like a dead
  # instance. Say which value is missing instead.
  say "缺少 IDEAGEN_DASH_USER / IDEAGEN_DASH_PASSWORD（或 hash 生成失败），代理不会启动"
  probe_up
  exit 1
fi
unset DASH_PW

# The database the dashboard reads starts out with no tables at all, and the
# page's first request is what discovers that — as a 500 quoting a MySQL error,
# which reads like a broken deploy rather than an empty database. Applying the
# schema here makes the first page load a true one. `--state-probe` is
# idempotent: it migrates, writes one probe row and reads it back, so a failure
# is reported before the dashboard can present it as a mystery.
say "applying database schema"
if docker compose -f deploy/compose.yaml run --rm --entrypoint python3 \
     dashboard -m ideagen platform --state-probe 2>&1 | tail -6 >> "$STATUS"; then
  echo "state-probe done"
else
  say "state-probe FAILED — dashboard will not have a database to read"
fi

if docker compose -f deploy/compose.yaml up -d dashboard proxy; then
  echo "compose up ok"
  # Give the proxy time to pull and bind before deciding anything. Restarting
  # the status server after fifteen seconds was worse than useless: it took :80
  # back while Caddy was still starting, so Caddy could never bind it and
  # restart:always turned that into a crash loop. The port stays Caddy's unless
  # the stack has genuinely failed to answer.
  # A 401 is a healthy proxy: the login prompt is the proxy answering. Only a
  # refused connection or a timeout means Caddy never came up — checking for
  # 200 here would report a working stack as broken the moment we added a
  # password.
  ok=""
  for i in $(seq 1 36); do
    code=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 6 \
           "https://127.0.0.1/healthz" 2>/dev/null || echo 000)
    case "$code" in 200|401) ok=1; break;; esac
    sleep 5
  done
  if [ -n "$ok" ]; then
    echo "healthz ok"
  else
    say "两分钟内 /healthz 没有响应，代理没起来——下面是诊断"
    docker compose -f deploy/compose.yaml ps >> "$STATUS" 2>&1
    docker compose -f deploy/compose.yaml logs --tail 25 proxy >> "$STATUS" 2>&1
    docker compose -f deploy/compose.yaml logs --tail 25 dashboard >> "$STATUS" 2>&1
    probe_up
  fi
else
  say "compose FAILED"
  tail -60 /var/log/ideagen-bootstrap.log >> "$STATUS"
  probe_up
fi

# One more attempt at the platform's own command channel; harmless if it fails
# again, and it is the difference between "reboot to deploy" and "run a command".
curl -fsSL https://iam-cloud-assistant-ap-southeast-1.tos-ap-southeast-1.bytepluses.com/linux/install.sh -o /tmp/ca.sh 2>/dev/null \
  && bash /tmp/ca.sh >/dev/null 2>&1 || true

echo "=== bootstrap done $(date -u +%FT%TZ) ==="
