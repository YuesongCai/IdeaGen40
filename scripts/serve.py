"""The dashboard server, with inference resolved for this process only.

Same reasoning as `scripts/tick.py`: the ModelArk key lives commented out in
the operator env so ad-hoc CLI runs stay model-free, and un-commenting it would
turn inference on for everything on the machine. The dashboard needs it for one
feature — 「问它当时怎么想」, which is answered by a model reading only the
frozen material of a past run — so it is resolved here, handed to this one
process, and never written back.

With no key the server still starts; the ask endpoint then reports that this
node cannot answer instead of pretending it can.

Installed as com.ideagen40.serve's ProgramArguments.
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


def main(argv: list[str]) -> int:
    env = dict(os.environ)
    key = _ark_key()
    if key:
        env["ARK_API_KEY"] = key
        env.setdefault("IDEAGEN_ARK_MODEL", "deepseek-v4-pro-260425")
        env.setdefault("IDEAGEN_INFERENCE_BASE_URL",
                       "https://ark.ap-southeast.bytepluses.com/api/v3")
        env["IDEAGEN_INFERENCE_MODE"] = "modelark"
        # Whether inference should bypass the local proxy is not a constant.
        # Within one afternoon: the proxy hung on full-size generation
        # responses while direct worked, then direct started getting reset by
        # peer while the proxy answered in under three seconds. Hard-coding
        # either one strands the run on the day the network flips, so the
        # default follows the system settings and IDEAGEN_INFERENCE_DIRECT=1
        # switches to direct when the proxy is the broken side.
        if env.get("IDEAGEN_INFERENCE_DIRECT") == "1":
            # Only the inference host. NO_PROXY entries match by domain
            # suffix, so "bytepluses.com" would also strand TOS storage
            # (`<bucket>.tos-…​.bytepluses.com`) on the direct path — which is
            # exactly how the dashboard lost its run log: the journal lives in
            # TOS, and the blob read died of SSL EOF while inference was fine.
            for var in ("NO_PROXY", "no_proxy"):
                env[var] = ",".join(filter(None, [
                    env.get(var, ""), _inference_host(env)]))
    return subprocess.run(
        [PYBIN, "-m", "ideagen.cli", "serve", *argv],
        cwd=ROOT, env=env).returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
