#!/usr/bin/env bash
# Unattended half of the daily cycle. The generation step needs Claude and is
# therefore not here; see prompts/idea_generation.md.
#
#   crontab -e
#   17 8 * * 1-5  /Users/yuesongcai/Downloads/IdeaGen40/scripts/daily.sh >> \
#                 /Users/yuesongcai/Downloads/IdeaGen40/data/logs/daily.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p data/logs
[ -f "$HOME/.ideagen.env" ] && set -a && . "$HOME/.ideagen.env" && set +a
echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') ==="
if ! python3 -m ideagen.cli doctor; then
  echo "doctor failed — is Futu OpenD running and logged in?"
  exit 1
fi
python3 -m ideagen.cli daily
