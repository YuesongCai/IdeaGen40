"""Generate the historical weekly periods the record is missing.

Real-backtest prerequisite: stage C can only be compared across periods that
actually have a stored candidate pool, and the live record has exactly one
(2026-08-26). This walks the earlier Wednesdays the corpus covers and runs the
full three stages for each, marked `backfill` so no chart, export or PM
conversation can mistake them for periods the system called live.

Inference is enabled for this process only — the key is read from the operator
env file and passed to the child process environment; nothing is written back,
and no value is ever printed.

  python3 scripts/backfill_weeks.py 2026-07-29 2026-08-05 ...
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = Path.home() / ".ideagen.env"
PYBIN = os.environ.get(
    "IDEAGEN_PYTHON",
    "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3")


def _ark_key() -> str:
    """The ModelArk key as stored, whether or not its line is commented out.

    The operator env keeps the key commented while the local node runs as an
    observer. Backfill needs inference for this process and this process only,
    so the value is read here and never written back.
    """
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        m = re.search(r"ARK_API_KEY=(\S+)", line)
        if m:
            return m.group(1)
    raise SystemExit("~/.ideagen.env 里找不到 ARK_API_KEY")


def main(argv: list[str]) -> int:
    if not argv:
        raise SystemExit("用法: backfill_weeks.py YYYY-MM-DD [YYYY-MM-DD ...]")
    days = [date.fromisoformat(a) for a in argv]

    env = dict(os.environ)
    env.update({
        "ARK_API_KEY": _ark_key(),
        "IDEAGEN_ARK_MODEL": env.get("IDEAGEN_ARK_MODEL",
                                     "deepseek-v4-pro-260425"),
        "IDEAGEN_INFERENCE_MODE": "modelark",
        # The endpoint is not distributed in source (ARK_BASE is empty on
        # purpose); the operator env keeps it commented alongside the key.
        "IDEAGEN_INFERENCE_BASE_URL": env.get(
            "IDEAGEN_INFERENCE_BASE_URL",
            "https://ark.ap-southeast.bytepluses.com/api/v3"),
        # The weekly-role guard is about the *scheduler* not inventing failures;
        # an explicit backfill is a deliberate operator action, not a tick.
        "IDEAGEN_WEEKLY_ROLE": "runner",
        # See scripts/tick.py: which side of the proxy works flips, so it is
        # a switch rather than a constant. IDEAGEN_INFERENCE_DIRECT=1 forces
        # direct; the default follows the system proxy settings.
        **({"NO_PROXY": ",".join(filter(None, [env.get("NO_PROXY", ""),
                                               "bytepluses.com"])),
            "no_proxy": ",".join(filter(None, [env.get("no_proxy", ""),
                                               "bytepluses.com"]))}
           if env.get("IDEAGEN_INFERENCE_DIRECT") == "1" else {}),
        "IDEAGEN_INFERENCE_TIMEOUT_SECONDS": env.get(
            "IDEAGEN_INFERENCE_TIMEOUT_SECONDS", "420"),
    })

    failed: list[str] = []
    for d in days:
        print(f"\n{'=' * 60}\n== 补跑 {d} (backfill)\n{'=' * 60}", flush=True)
        r = subprocess.run(
            [PYBIN, "-u", "-m", "ideagen.cli", "weekly", "--as-of",
             d.isoformat(), "--classification", "backfill", "--trade"],
            cwd=ROOT, env=env)
        if r.returncode != 0:
            failed.append(d.isoformat())
            print(f"!! {d} 失败（继续下一期）", flush=True)
    print(f"\n完成 {len(days) - len(failed)}/{len(days)} 期"
          + (f"；失败：{', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
