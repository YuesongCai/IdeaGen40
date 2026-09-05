#!/bin/sh
# Track origin/main on the single-container display node.
#
# The sibling of sync_state.sh: that one keeps the data current, this one keeps
# the code current. Without it the node is frozen at whatever commit it cloned
# at boot, and the only way to pick up a fix is to build a new instance — which
# is how this node ended up eleven commits behind in an afternoon.
#
# deploy/updater.sh does this job for the compose stack. It is not reusable
# here: it deploys with `docker compose up -d`, and this node is one plain
# container. The shape is deliberately copied from it, including the part that
# matters most —
#
# THE TESTS ARE THE GATE. Several agents push to main, so "whoever can push can
# change production" with no human in between. Building the new image and
# running the suite inside it before switching is what keeps that from also
# meaning "whoever can push can break production". A build that fails is
# reported and NOT deployed; the running container is left alone.
#
# Coordination: sync_state.sh stops the container to swap the database, and
# this script replaces the container. Interleaved, one can start the old
# container back up around a half-finished swap. Both take the same lock.
#
# Cost: this node is one vCPU, so a build plus the full suite takes minutes and
# the served page is sluggish while it runs. That only happens when origin/main
# actually moves — an idle poll is one `git fetch` — but it is worth knowing
# before someone pushes during a demo. The test run is niced so the server
# keeps priority.
set -u

APP=/opt/ideagen/app
DATA=/opt/ideagen/data
CONF=/opt/ideagen/config/runtime.env
NAME=ideagen-dash
LOCK=/var/lock/ideagen-deploy.lock
REF="${IDEAGEN_UPDATE_REF:-origin/main}"
REQUIRE_TESTS="${IDEAGEN_UPDATE_REQUIRE_TESTS:-1}"

git -C "$APP" fetch -q --tags origin main 2>/dev/null || {
  echo "IG_CODE_FETCH_FAIL $(date -u +%FT%TZ)"; exit 1; }

have=$(git -C "$APP" rev-parse --short HEAD 2>/dev/null)
want=$(git -C "$APP" rev-parse --short "$REF" 2>/dev/null)
[ -n "$want" ] || { echo "IG_CODE_NO_REF $REF"; exit 1; }
if [ "$have" = "$want" ]; then
  echo "IG_CODE_CURRENT $have $(date -u +%FT%TZ)"; exit 0
fi

echo "IG_CODE_UPDATE $have -> $want"
git -C "$APP" reset -q --hard "$REF"

# Build under a throwaway tag. Tagging :live before the suite runs would leave
# the name pointing at an image nothing has vouched for, and the next restart
# would quietly adopt it.
if ! docker build -q -t "ideagen40:cand-$want" -f "$APP/deploy/Dockerfile" "$APP" >/dev/null 2>&1; then
  echo "IG_CODE_BUILD_FAIL $want"; exit 1
fi

if [ "$REQUIRE_TESTS" = "1" ]; then
  if nice -n 15 docker run --rm --cpus 0.6 --entrypoint python3 "ideagen40:cand-$want" \
       -m pytest -q -x >/tmp/sync_code_tests.log 2>&1; then
    echo "IG_CODE_TESTS_OK $want"
  else
    # 把真实的报错打出来，一行一行。上一版只印一段挤成 240 字的尾巴，
    # 结果是「测试没过」这个事实到了，为什么没过的信息没到 —— 而这台机器
    # 登不上去，控制台是唯一的通道。查一次这样的失败花了一晚上，最后还是
    # 因为本机拉不到基础镜像而没查出来。
    echo "IG_CODE_TESTS_FAIL $want"
    grep -E '^(E |FAILED|ERROR|tests/.*(FAILED|ERROR))' /tmp/sync_code_tests.log \
      | head -25 | sed 's/^/IG_CODE_ERR /'
    tail -6 /tmp/sync_code_tests.log | sed 's/^/IG_CODE_TAIL /'
    docker rmi -f "ideagen40:cand-$want" >/dev/null 2>&1
    exit 1
  fi
fi

(
  flock -w 300 9 || { echo "IG_CODE_LOCK_TIMEOUT"; exit 1; }
  # 连同这两个脚本一起更新。装在 /opt/ideagen 的是开机那一版，代码腿
  # 只动 /opt/ideagen/app —— 不重装的话，同步脚本自己的修复永远到不了
  # 这台机器，而它恰恰是没人能登上去改的那台。放在测试通过之后：
  # 之前重装等于先跑一段没人担保过的代码。
  install -m 755 "$APP/deploy/sync_state.sh" /opt/ideagen/sync_state.sh
  install -m 755 "$APP/deploy/sync_code.sh"  /opt/ideagen/sync_code.sh
  docker tag "ideagen40:cand-$want" ideagen40:live
  docker rm -f "$NAME" >/dev/null 2>&1
  docker run -d --name "$NAME" --restart always \
    --env-file "$CONF" \
    -e IDEAGEN_DASH_HOST=0.0.0.0 -e IDEAGEN_DB=/data/ideagen.db \
    -v "$DATA":/data -p 80:8765 -p 443:8765 \
    --entrypoint python3 ideagen40:live -m ideagen.cli serve --port 8765 \
    >/dev/null 2>&1 \
    && echo "IG_CODE_DEPLOYED $want $(date -u +%FT%TZ)" \
    || { echo "IG_CODE_RUN_FAIL $want"; exit 1; }
) 9>"$LOCK"

docker rmi -f "ideagen40:cand-$want" >/dev/null 2>&1
docker image prune -f >/dev/null 2>&1

# 每次代码变动都构建一次完整镜像，而 origin/main 一天能动几十次。悬空层由
# 上面的 prune 清掉，但 **构建缓存不会**，它只增不减，最后撑满 40GB 系统盘 ——
# 而磁盘满在这台上的表现是「页面突然打不开」，不会有人联想到构建缓存。
# 所以：每次部署后报一次剩余空间（控制台是这台唯一的通道），低于 8GB 就把
# 构建缓存压到 2GB。平时不动它，构建才不会每次都从头来。
free_kb=$(df -Pk / | awk 'NR==2{print $4}')
echo "IG_CODE_DISK 剩余 $((free_kb / 1024 / 1024))GB"
if [ "$free_kb" -lt 8388608 ]; then
  echo "IG_CODE_DISK_PRUNE 剩余不足 8GB，清理构建缓存"
  docker builder prune -f --keep-storage 2GB >/dev/null 2>&1
  echo "IG_CODE_DISK 清理后剩余 $(($(df -Pk / | awk 'NR==2{print $4}') / 1024 / 1024))GB"
fi
