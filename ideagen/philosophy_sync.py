"""WS-D: 「我的准则」 survives a deploy, and comes home to the laptop.

The bug (found by WS-B): a card written on the display node landed in
`config.DATA / "philosophy"`, which inside the image is `/app/data/philosophy/`
— the container's own filesystem. The code leg rebuilds that container whenever
main moves, so every card, pending proposal and draft written there was deleted
at the next deploy. And nothing ever copied them back to the laptop, which is
where the weekly run actually executes — so even a card that happened to survive
a few hours was never run. The panel said 「已生效」 for a rule that no run would
ever see. That is the green-while-broken shape: a write that returns 200 and
goes nowhere.

The fix copies two patterns that already work in this repo:

* **Where to store** — `lexicon.durable_themes_dir()`: beside `IDEAGEN_DB`
  whenever that is outside the checkout (the display node's `/data`, a host
  mount that outlives every container), or wherever `IDEAGEN_PHILOSOPHY_DIR`
  points. A laptop, whose database sits inside the repo, keeps `data/philosophy`
  exactly as before, so nothing moves on the machine that runs the week.
* **How to come home** — WS-B's pm_reviews: the node's ledger is already an
  append-only JSONL, so it is exported read-only over `/api/philosophy/export`,
  and the laptop's data leg pulls and merges it *before* it publishes a
  snapshot. The merge appends only events the laptop does not already hold, by
  canonical JSON identity, so it is idempotent and safe to repeat every tick.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from . import config

_ROOT = Path(__file__).resolve().parent.parent


def philosophy_dir() -> Path:
    """The durable directory for the ledger, pending proposals and drafts."""
    env = os.environ.get("IDEAGEN_PHILOSOPHY_DIR")
    if env:
        return Path(env)
    dbp = os.environ.get("IDEAGEN_DB")
    if dbp:
        d = Path(dbp).resolve().parent
        try:
            d.relative_to(_ROOT.resolve())
        except ValueError:
            # Outside the checkout: that is the data mount, the one place a
            # container has that the next deploy does not throw away.
            return d / "philosophy"
    return config.DATA / "philosophy"


def adopt_legacy(durable: Path | None = None) -> bool:
    """One-time move of a pre-fix `data/philosophy` into the durable directory.

    Only when the durable ledger does not exist yet and the legacy one does —
    never a merge, never an overwrite. Returns True if it copied anything.
    Never raises: this runs at import time of the module that the plugin scan
    loads, and a failed copy must not take the generators down with it.
    """
    try:
        durable = durable or philosophy_dir()
        legacy = config.DATA / "philosophy"
        if durable.resolve() == legacy.resolve():
            return False
        if (durable / "ledger.jsonl").exists() or not (legacy / "ledger.jsonl").exists():
            return False
        durable.mkdir(parents=True, exist_ok=True)
        for name in ("ledger.jsonl",):
            shutil.copy2(legacy / name, durable / name)
        for sub in ("pending", "drafts"):
            if (legacy / sub).is_dir() and not (durable / sub).exists():
                shutil.copytree(legacy / sub, durable / sub)
        return True
    except Exception:  # noqa: BLE001
        return False


def _canon(e: dict[str, Any]) -> str:
    return json.dumps(e, ensure_ascii=False, sort_keys=True)


def export_events() -> dict[str, Any]:
    """The node's usable ledger events, for `/api/philosophy/export`.

    Unusable lines are counted, not shipped: the laptop's run imports this file
    during the plugin scan, and one malformed line must not travel there.
    """
    from . import philosophy
    rows = philosophy._read_events()
    good = [e for e in rows if e.get("event") in ("activate", "retire")]
    return {"events": good, "n": len(good),
            "unusable": len(rows) - len(good),
            # No filesystem path: this is served remotely, and host paths are
            # what every other journal-serving route scrubs out.
            "exported_at": config.now_hkt().isoformat()}


def merge_events(events: list[Any], ledger: Path | None = None) -> dict[str, int]:
    """Append events the local ledger does not already hold. Idempotent.

    Order is preserved as received, so an activate pulled together with its
    later retire lands in that order — `cards()` reads the file top to bottom.
    """
    from . import philosophy
    path = ledger or philosophy.LEDGER
    have: set[str] = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                have.add(_canon(json.loads(line)))
            except ValueError:
                continue
    st = {"received": 0, "appended": 0, "kept": 0, "invalid": 0}
    new_lines: list[str] = []
    for e in events or []:
        st["received"] += 1
        if philosophy._bad_row(e):
            st["invalid"] += 1
            continue
        c = _canon(e)
        if c in have:
            st["kept"] += 1
            continue
        have.add(c)
        new_lines.append(c)
    if new_lines:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for c in new_lines:
                fh.write(c + "\n")
        st["appended"] = len(new_lines)
    return st


def pull(url: str | None = None, key: str | None = None,
         timeout: float = 20.0, ledger: Path | None = None) -> dict[str, Any]:
    """Fetch the display node's ledger and merge it into this machine's."""
    import urllib.request
    base = (url or config.DISPLAY_NODE_URL).rstrip("/")
    req = urllib.request.Request(f"{base}/api/philosophy/export",
                                 headers={"Accept": "application/json",
                                          **({"X-Dash-Key": key} if key else {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    events = body.get("events") if isinstance(body, dict) else None
    if not isinstance(events, list):
        # A login page or an error body is not "the node has no cards".
        raise ValueError(f"导出接口没有返回 events 列表：{str(body)[:120]}")
    return {"source": base, **merge_events(events, ledger)}
