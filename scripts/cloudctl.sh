#!/usr/bin/env bash
# One entry point for driving the cloud account from this machine.
#
# Reads the IAM sub-account keys from ~/.ideagen.env (never from the repo, never
# echoed) and dispatches to `ve` (BytePlus/Volcengine CLI) or `agentkit`.
# The production keys (BYTEPLUS_ACCESS_KEY in the env file) stay untouched —
# they belong to the running platform; CLI operations use the dedicated
# `ideagen` IAM sub-account so cloud-console actions are attributable and
# revocable independently of the pipeline's own credentials.
#
# Usage:
#   scripts/cloudctl.sh ve ecs DescribeInstances
#   scripts/cloudctl.sh agentkit runtime list
set -euo pipefail
ENVF="$HOME/.ideagen.env"
AK=$(grep '^IDEAGEN_IAM_ACCESS_KEY=' "$ENVF" | cut -d= -f2)
SK=$(grep '^IDEAGEN_IAM_SECRET_KEY=' "$ENVF" | cut -d= -f2-)
[ -n "$AK" ] && [ -n "$SK" ] || { echo "IDEAGEN_IAM_* 未配置于 ~/.ideagen.env" >&2; exit 2; }

cmd="${1:-}"; shift || true
case "$cmd" in
  ve)
    exec ve --profile byteplus "$@" ;;
  agentkit|ak)
    BYTEPLUS_ACCESS_KEY="$AK" BYTEPLUS_SECRET_KEY="$SK" \
      exec agentkit --provider byteplus "$@" ;;
  *)
    echo "用法: $0 {ve|agentkit} <子命令...>" >&2; exit 2 ;;
esac
