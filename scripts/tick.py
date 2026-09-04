"""The scheduled tick, with inference resolved for the tick process only.

Why this wrapper exists instead of editing ~/.ideagen.env: the ModelArk key
line there is kept commented while this node runs as an observer, and
un-commenting it would enable inference for every process on the machine,
including ad-hoc CLI runs deliberately kept model-free. This resolves the key
at tick time, hands it to one child process, and writes nothing back — the
operator file stays the single place the secret lives, and reverting is
pointing launchd back at `python3 -m ideagen.scheduler tick`.

A node holding a working key is a `runner`: claiming "delegated" while able to
reach a model is how a week goes missing with every dashboard reporting
success. With no key it stays an honest observer.

Installed as com.ideagen40.scheduler's ProgramArguments.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = Path.home() / ".ideagen.env"
PYBIN = os.environ.get(
    "IDEAGEN_PYTHON",
    "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3")


def _ark_key() -> str:
    """The key as stored, whether its line is live or commented out."""
    try:
        text = ENV_FILE.read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.search(r"ARK_API_KEY=(\S+)", text)
    return m.group(1) if m else ""


def _inference_host(env: dict) -> str:
    """The single hostname inference talks to, for a surgical proxy bypass."""
    from urllib.parse import urlparse
    base = env.get("IDEAGEN_INFERENCE_BASE_URL", "")
    return urlparse(base).hostname or "ark.ap-southeast.bytepluses.com"


def main() -> int:
    env = dict(os.environ)
    key = _ark_key()
    env["ARK_API_KEY"] = key
    env.setdefault("IDEAGEN_ARK_MODEL", "deepseek-v4-pro-260425")
    env.setdefault("IDEAGEN_INFERENCE_BASE_URL",
                   "https://ark.ap-southeast.bytepluses.com/api/v3")
    if key:
        env["IDEAGEN_INFERENCE_MODE"] = "modelark"
        env.setdefault("IDEAGEN_WEEKLY_ROLE", "runner")
        # Whether inference should bypass the local proxy is not a constant.
        # Within one afternoon: the proxy hung on full-size generation
        # responses while direct worked, then direct started getting reset by
        # peer while the proxy answered in under three seconds. Hard-coding
        # either one strands the run on the day the network flips, so the
        # default follows the system settings and IDEAGEN_INFERENCE_DIRECT=1
        # switches to direct when the proxy is the broken side.
        if env.get("IDEAGEN_INFERENCE_DIRECT") == "1":
            # The inference host only: NO_PROXY matches by domain suffix, and
            # the whole domain would drag TOS storage onto the direct path too.
            for var in ("NO_PROXY", "no_proxy"):
                env[var] = ",".join(filter(None, [
                    env.get(var, ""), _inference_host(env)]))
    else:
        env["IDEAGEN_INFERENCE_MODE"] = "claude"
        env["IDEAGEN_WEEKLY_ROLE"] = "observer"
    return subprocess.run([PYBIN, "-m", "ideagen.scheduler", "tick"],
                          cwd=ROOT, env=env).returncode


if __name__ == "__main__":
    sys.exit(main())
