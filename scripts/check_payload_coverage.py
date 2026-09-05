#!/usr/bin/env python3
"""What does the server ship that the page never reads?

Two uses. Pruning is the boring one. The one that matters is provenance: a
field that records *how a number came to be* — `p_source`, `sd_source`,
`cash_rate.source` — is worthless if the page prints the number without it.
That is how a fallback constant came to sit on the first screen looking like a
measurement, and how a factor that had saturated to a constant kept being drawn
as a contributing bar.

The script self-tests before it reports. It has to: the first two versions of
this check were wrong and I believed both of them. The first grepped for the
bare field name and counted a coincidental substring as a use. The second put
the word-boundary guard on the wrong side of the dot, so `p.thesis` did not
match its own field and 216 of 249 fields looked unread. Neither failed loudly —
they printed a plausible list. So `_self_test` asserts that fields visibly
rendered on the page come back as used, and that an invented name does not; if
either check fails the script exits non-zero and reports nothing, because a
coverage report you cannot trust is worse than none.

    python3 scripts/check_payload_coverage.py
"""

from __future__ import annotations

import collections
import json
import os
import re
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASH = os.path.join(ROOT, "web", "dash.html")
URL = os.environ.get("IDEAGEN_STATE_URL", "http://127.0.0.1:8765/api/state")

#: Rendered on the page in plain sight; if the detector misses one it is broken.
KNOWN_USED = ("thesis", "stop_px", "last_px", "entry_px", "unrealized",
              "capital", "selector", "open_positions", "equity",
              "return_pct", "p_source")
KNOWN_ABSENT = ("__no_such_field_anywhere__",)


def _script(path: str) -> str:
    html = open(path, encoding="utf-8").read()
    return html[html.index("<script>"):html.rindex("</script>")]


def _reads(js: str, field: str) -> bool:
    """`o.field` or `o["field"]`. The dot form must not be anchored on what
    precedes the dot — that is the receiver, and it is always a word char."""
    return (re.search(r"\." + re.escape(field) + r"(?![\w$])", js) is not None
            or re.search(r"\[\s*['\"]" + re.escape(field) + r"['\"]\s*\]", js)
            is not None)


def _self_test(js: str) -> list[str]:
    problems = [f"漏判为未读：{f}" for f in KNOWN_USED if not _reads(js, f)]
    problems += [f"假阳性：{f}" for f in KNOWN_ABSENT if _reads(js, f)]
    return problems


def _leaves(obj, out: collections.Counter, depth: int = 0) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                _leaves(v, out, depth + 1)
            else:
                out[k] += 1
    elif isinstance(obj, list):
        for item in obj[:6]:            # a sample is enough for key discovery
            _leaves(item, out, depth + 1)


def main() -> int:
    js = _script(DASH)
    problems = _self_test(js)
    if problems:
        print("✗ 检查器本身不合格，不出报告：")
        for p in problems:
            print("   ", p)
        return 1
    print(f"✓ 自检通过（{len(KNOWN_USED)} 个已知在用的字段判为已用）\n")

    req = urllib.request.Request(URL, headers={"Accept-Encoding": "gzip"})
    raw = urllib.request.urlopen(req, timeout=60).read()
    if raw[:2] == b"\x1f\x8b":
        import gzip
        raw = gzip.decompress(raw)
    payload = json.loads(raw)

    counts: collections.Counter = collections.Counter()
    _leaves(payload, counts)
    unread = sorted(((c, f) for f, c in counts.items() if not _reads(js, f)),
                    reverse=True)
    print(f"接口叶子字段 {len(counts)} 个，前端从未读取 {len(unread)} 个")
    prov = [(c, f) for c, f in unread
            if re.search(r"(source|origin|default|fallback|redact|basis)", f, re.I)]
    if prov:
        print("\n其中记录「这个数怎么来的」而页面没有显示的（优先看）：")
        for c, f in prov:
            print(f"    {f:<32}{c:>6}")
    print("\n其余未读字段：")
    for c, f in unread[:20]:
        if (c, f) not in prov:
            print(f"    {f:<32}{c:>6}")
    if len(unread) > 20:
        print(f"    …另有 {len(unread) - 20} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
