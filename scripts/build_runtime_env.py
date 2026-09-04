"""Emit the production runtime.env on stdout, from the operator env file.

Secrets flow one way: ~/.ideagen.env → stdout → an encrypted channel → a 0600
file on the instance. Nothing is written locally, and no value is ever printed
to a transcript — the caller is expected to pipe this, not to look at it.

Values that differ between the operator machine and production (state engine,
bucket, weekly role) are set here rather than copied, so a laptop's observer
configuration cannot leak into the instance that actually runs the week.
"""
from __future__ import annotations

import os
import re
import sys

ENVF = os.path.expanduser("~/.ideagen.env")

# The instance's own resources, from data/cloud_inventory.md.
MYSQL_HOST = "mysql1742d36bf5de.rds.ibytepluses.com"
TOS_BUCKET = "ideagen-prod-4b869b"
TOS_ENDPOINT = "tos-ap-southeast-1.bytepluses.com"
ARK_BASE = "https://ark.ap-southeast.bytepluses.com/api/v3"
ARK_MODEL = "deepseek-v4-pro-260425"


def readenv(key: str, *, required: bool = True) -> str:
    """The value as stored, whether its line is live or commented out.

    Commented lines count because the operator file disables inference locally
    by commenting the key; production needs that same key live.
    """
    val = None
    for line in open(ENVF, encoding="utf-8"):
        m = re.match(rf"^#?\s*(?:[^=#]*\s)?{re.escape(key)}=(\S+)", line.strip())
        if m:
            val = m.group(1)
    if val is None and required:
        raise SystemExit(f"~/.ideagen.env 缺少 {key}")
    return val or ""


def main() -> int:
    pairs = [
        ("IDEAGEN_PLATFORM", "byteplus"),
        ("IDEAGEN_STATE_ENGINE", "mysql"),
        ("IDEAGEN_MYSQL_HOST", MYSQL_HOST),
        ("IDEAGEN_MYSQL_PORT", "3306"),
        ("IDEAGEN_MYSQL_DATABASE", "ideagen"),
        ("IDEAGEN_MYSQL_USER", "ideagen"),
        ("IDEAGEN_MYSQL_PASSWORD", readenv("IDEAGEN_MYSQL_PASSWORD")),
        ("IDEAGEN_TOS_BUCKET", TOS_BUCKET),
        ("IDEAGEN_TOS_PREFIX", "prod"),
        ("IDEAGEN_TOS_ENDPOINT", TOS_ENDPOINT),
        ("VOLCENGINE_ACCESS_KEY", readenv("BYTEPLUS_ACCESS_KEY")),
        ("VOLCENGINE_SECRET_KEY", readenv("BYTEPLUS_SECRET_KEY")),
        ("VOLCENGINE_REGION", "ap-southeast-1"),
        ("ARK_API_KEY", readenv("ARK_API_KEY")),
        ("ARK_MODEL_ID", ARK_MODEL),
        ("ARK_BASE_URL", ARK_BASE),
        ("IDEAGEN_ARK_MODEL", ARK_MODEL),
        # The platform reads this name specifically; ARK_BASE_URL alone leaves
        # inference unconfigured and the run dies after fetching every feed.
        ("IDEAGEN_INFERENCE_BASE_URL", ARK_BASE),
        ("IDEAGEN_INFERENCE_MODE", "modelark"),
        ("IDEAGEN_DASH_KEY", readenv("IDEAGEN_DASH_KEY")),
        ("WISBURG_MCP_URL", "https://mcp.wisburg.com/mcp"),
        ("WISBURG_MCP_TOKEN", readenv("WISBURG_MCP_TOKEN_CLOUD")),
        ("IDEAGEN_CLOUD_WISBURG_ENABLED", "true"),
        ("IDEAGEN_POC_WEEKLY_MODE", "wisburg-auto"),
        # This instance is the one that runs the week. The laptop stays an
        # observer; two runners would race for the same period.
        ("IDEAGEN_WEEKLY_ROLE", "runner"),
    ]
    sys.stdout.write("\n".join(f"{k}={v}" for k, v in pairs) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
