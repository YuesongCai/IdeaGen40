"""Run `ideagen reselect` with the model port enabled for this process only.

Same environment recipe as `backfill_weeks.py` (key read from the operator env
file and never printed; inference host bypasses the proxy when
IDEAGEN_INFERENCE_DIRECT=1). The AI end-to-end selector needs the model; the
other arms are mechanical and run the same way either way.

    IDEAGEN_INFERENCE_DIRECT=1 python3 scripts/reselect_arms.py \\
        --arms ai_native,generated_ai_native,generated_carl_constraint,mom_21
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import backfill_weeks as bw  # noqa: E402


def main(argv: list[str]) -> int:
    env = dict(os.environ)
    env.update({
        "ARK_API_KEY": bw._ark_key(),
        "IDEAGEN_ARK_MODEL": env.get("IDEAGEN_ARK_MODEL", "deepseek-v4-pro-260425"),
        "IDEAGEN_INFERENCE_MODE": "modelark",
        "IDEAGEN_INFERENCE_BASE_URL": env.get(
            "IDEAGEN_INFERENCE_BASE_URL", "https://ark.ap-southeast.bytepluses.com/api/v3"),
        "IDEAGEN_WEEKLY_ROLE": "runner",
        **({"NO_PROXY": ",".join(filter(None, [env.get("NO_PROXY", ""), bw._ark_host(env)])),
            "no_proxy": ",".join(filter(None, [env.get("no_proxy", ""), bw._ark_host(env)]))}
           if env.get("IDEAGEN_INFERENCE_DIRECT") == "1" else {}),
        "IDEAGEN_INFERENCE_TIMEOUT_SECONDS": env.get("IDEAGEN_INFERENCE_TIMEOUT_SECONDS", "420"),
    })
    cmd = [bw.PYBIN, "-u", "-m", "ideagen.cli", "reselect", *argv]
    return subprocess.run(cmd, cwd=ROOT, env=env).returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
