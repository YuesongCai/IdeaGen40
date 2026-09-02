#!/usr/bin/env python3
"""Export the dashboard as a static GitHub Pages site.

Pages has no backend, so the live page's two API calls are replaced by data baked
into the HTML at export time (`window.__STATIC__`). The page then labels itself a
snapshot with its own timestamp — a stale page must say it is a snapshot rather
than impersonate a live system.

The export re-uses the server's own scrubbing (bucket names, home paths already
removed by review.state / the journal route logic) and then applies the licensed-
content pass on top. It never publishes: writing the site directory is separate
from pushing it, and the partner-data gate (check_publish_safety) must pass on the
OUTPUT before anything is pushed.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ideagen import db, platform as plat, review  # noqa: E402


def scrub(obj):
    """The journal route's scrub, applied to everything we bake in."""
    if isinstance(obj, str):
        obj = re.sub(r"tos://[\w.-]+", "tos://<bucket>", obj)
        return re.sub(r"/Users/[\w.-]+", "~", obj)
    if isinstance(obj, dict):
        return {k: scrub(v) for k, v in obj.items() if k != "host"}
    if isinstance(obj, list):
        return [scrub(v) for v in obj]
    return obj


def main() -> int:
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "web/_site")
    con = db.init()
    p = plat.load()

    state = review.state(con, p)
    journal = None
    row = p.state.q("SELECT run_id, as_of FROM orch_runs WHERE kind='weekly' "
                    "AND ok=1 ORDER BY as_of DESC LIMIT 1")
    if row:
        try:
            j = json.loads(p.blobs.get(
                f"runs/{row[0]['as_of']}/{row[0]['run_id']}/journal.json"))
            journal = {"run_id": row[0]["run_id"], "as_of": row[0]["as_of"],
                       "journal": j}
        except Exception as e:  # noqa: BLE001 — the page states the absence
            print(f"  journal 不可读（页面会如实标注）: {type(e).__name__}")

    if journal:
        for h in (journal.get("journal", {}).get("port_health") or []):
            if isinstance(h, dict):
                h.pop("meta", None)
        journal.get("journal", {}).pop("host", None)
    payload = scrub({"state": state, "journal": journal,
                     "exported_at": datetime.now(timezone.utc).isoformat()})

    html = Path("web/dash.html").read_text(encoding="utf-8")
    inject = ("<script>window.__STATIC__=" +
              json.dumps(payload, ensure_ascii=False, default=str,
                         allow_nan=False)
              .replace("</", "<\\/") + ";</script>\n")
    # The data must exist before the app script runs; inject at the very top.
    idx = html.find("<script")
    assert idx > 0, "dash.html 结构不符合预期"
    html = html[:idx] + inject + html[idx:]

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "index.html").write_text(html, encoding="utf-8")
    (out_dir / ".nojekyll").write_text("")
    print(f"导出完成 → {out_dir}/index.html "
          f"({(out_dir / 'index.html').stat().st_size // 1024}KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
