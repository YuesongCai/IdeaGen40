#!/bin/sh
# Keep the instance on origin/main by itself.
#
# Before this, deploying meant rebooting the instance by hand, so the cloud sat
# on whatever commit happened to be current at the last reboot while everyone
# kept pushing. "The cloud is out of date" was the normal state, and nobody
# could tell how far out of date without looking.
#
# It polls, rather than being pushed to, because the instance has no inbound
# path: no SSH, no working command agent, and a webhook would need a public
# endpoint and a shared secret to protect it. Polling git costs one request a
# couple of minutes and needs nothing opened.
#
# THE TESTS ARE THE POINT. Several agents push to main, so "whoever can push can
# change production" with no human in between. Running the suite against the
# newly built image before switching to it is what keeps that from meaning
# "whoever can push can break production". A build that fails its tests is
# reported and NOT deployed; the previous one keeps serving.
set -u

APP=/opt/ideagen/app
INTERVAL="${IDEAGEN_UPDATE_INTERVAL_S:-120}"
REQUIRE_TESTS="${IDEAGEN_UPDATE_REQUIRE_TESTS:-1}"
# What to follow. `origin/main` means every push reaches production once the
# suite passes — which is what "keep the cloud in sync with what I write" asks
# for, and also means whoever can push can change production with no person in
# between. Setting this to a tag ref instead splits "I finished writing" from
# "this can be shown to people" at the cost of one `git tag`. It is one
# variable so that choice stays a decision, not a rewrite.
REF="${IDEAGEN_UPDATE_REF:-origin/main}"
REPORT=/opt/ideagen/health/updater.json

command -v git >/dev/null 2>&1 || apk add --no-cache git >/dev/null 2>&1
docker compose version >/dev/null 2>&1 || apk add --no-cache docker-cli-compose >/dev/null 2>&1

say(){ echo "$(date -u +%FT%TZ) [updater] $*"; }

# What is actually running, as opposed to what is being attempted. They differ
# whenever a build is blocked, and reporting only the target would say the
# instance is on a commit it refused to deploy.
DEPLOYED=$(sed -n 's/.*"deployed_sha":"\([^"]*\)".*/\1/p' "$REPORT" 2>/dev/null)
DEPLOYED="${DEPLOYED:-unknown}"

report(){  # state, target sha, detail
  # `syncs` is not decoration. This updater carries CODE and nothing else: the
  # instance's boot script is what delivers configuration, and it is baked into
  # the system volume. So an instance can be perfectly up to date on code and
  # two days stale on configuration, with every health signal green — which is
  # exactly the failure a colleague found here. A status line that says "I am
  # running and I am current" without saying *what* it keeps current is the
  # same lie as a probe that reports a live box as dead.
  printf '{"at":"%s","state":"%s","sha":"%s","deployed_sha":"%s","detail":"%s",'\
'"syncs":"code from %s (image rebuilt, tests must pass)",'\
'"does_not_sync":"runtime.env, the boot script, and anything else delivered by cloud-init — those need a reboot, and a UserData change needs the system volume replaced"}\n' \
    "$(date -u +%FT%TZ)" "$1" "$2" "$DEPLOYED" "$3" "$REF" > "$REPORT" 2>/dev/null || true
}

say "watching $REF every ${INTERVAL}s (require_tests=${REQUIRE_TESTS})"
report idle "$(git -C "$APP" rev-parse --short HEAD 2>/dev/null)" "started"

while :; do
  if git -C "$APP" fetch -q --tags origin main 2>/dev/null; then
    have=$(git -C "$APP" rev-parse --short HEAD 2>/dev/null)
    want=$(git -C "$APP" rev-parse --short "$REF" 2>/dev/null)
    if [ -n "$want" ] && [ "$have" != "$want" ]; then
      say "$REF $have -> $want"
      report building "$want" "checking out and building"
      git -C "$APP" reset -q --hard "$REF"

      if docker build -q -f "$APP/deploy/Dockerfile" -t "ideagen40:$want" "$APP" >/dev/null 2>&1; then
        ok=1
        if [ "$REQUIRE_TESTS" = "1" ]; then
          say "running the suite inside ideagen40:$want"
          report testing "$want" "running the suite"
          if docker run --rm --entrypoint python3 "ideagen40:$want" \
               -m pytest -q -x >/tmp/updater-tests.log 2>&1; then
            say "tests passed"
          else
            ok=0
            tail=$(tail -3 /tmp/updater-tests.log | tr '\n' ' ' | tr -d '"' | cut -c1-300)
            say "tests FAILED, keeping the running build: $tail"
            # Do not reset back: the working tree tracking origin/main is what
            # lets the next push be tried. The *running containers* are what is
            # being protected, and they are simply left alone.
            report blocked "$want" "$tail"
          fi
        fi
        if [ "$ok" = "1" ]; then
          say "deploying $want"
          IMAGE_TAG="$want" \
          IDEAGEN_PUBLIC_HOST="${IDEAGEN_PUBLIC_HOST:-}" \
          IDEAGEN_PUBLIC_SITE="${IDEAGEN_PUBLIC_SITE:-}" \
          IDEAGEN_DEFAULT_SNI="${IDEAGEN_DEFAULT_SNI:-}" \
            docker compose -f "$APP/deploy/compose.yaml" up -d >/tmp/updater-up.log 2>&1 \
            && { say "deployed $want"; DEPLOYED="$want"
                 # The proxy's config is a bind-mounted file, so `up -d` leaves
                 # it running with the config it started with — a Caddyfile
                 # change would sit on disk, deployed and inert. Restarting it
                 # costs a second and is the only way the new config takes.
                 docker compose -f "$APP/deploy/compose.yaml" restart proxy >/dev/null 2>&1
                 report deployed "$want" "up"; } \
            || { say "compose up failed"; report failed "$want" "$(tail -2 /tmp/updater-up.log | tr -d '"' | cut -c1-200)"; }
        fi
      else
        say "image build failed"
        report failed "$want" "docker build failed"
      fi
    fi
  fi
  sleep "$INTERVAL"
done
