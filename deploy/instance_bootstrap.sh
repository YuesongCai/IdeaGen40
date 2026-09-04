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
# yet — that is a normal state, not an error. A runtime.env already on disk is
# never overwritten by a URL that may since have expired.
RUNTIME_ENV_URL='__RUNTIME_ENV_URL__'
if [ ! -s /opt/ideagen/config/runtime.env ] && [ "${RUNTIME_ENV_URL#__}" = "$RUNTIME_ENV_URL" ]; then
  if curl -fsS --max-time 60 "$RUNTIME_ENV_URL" -o /opt/ideagen/config/runtime.env; then
    chmod 600 /opt/ideagen/config/runtime.env
    say "runtime.env fetched"
  else
    say "runtime.env FETCH FAILED (presigned URL expired?)"
    rm -f /opt/ideagen/config/runtime.env
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
say "starting dashboard + proxy"
pkill -f "http.server 80" >/dev/null 2>&1 || true
sleep 1
export IMAGE_TAG="$SHA"
export IDEAGEN_PUBLIC_SITE=":80"

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
  ok=""
  for i in $(seq 1 24); do
    if curl -fsS --max-time 5 http://127.0.0.1/healthz >/dev/null 2>&1; then ok=1; break; fi
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
