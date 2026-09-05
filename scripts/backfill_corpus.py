"""Pull historical Wisburg corpus so the backtest has more than six periods.

Why this exists: the corpus began at 2026-07-25 because ingest only ever ran
forward from the day it was switched on. That start date — not the vendor, not
the price history — is what capped the real backtest at six weekly periods, of
which 78% of positions never reached the 30-day mark the table claims to
measure. The vendor serves the same lines for arbitrary past dates (probed back
to 2024-09), and `prices` already covers 2025-07-01 onward, so a fourteen-month
window is reachable with no new data source.

It walks week-sized `as_of` windows backwards from the newest so an interrupted
run still leaves the most recent history in place. `wisburg.ingest` upserts on
doc_id with `keep_if_blank`, so re-running a covered week is idempotent and
cannot blank a body that a deep fetch already filled in.

    python3 scripts/backfill_corpus.py 2025-06-25 2026-07-24
"""
from __future__ import annotations

import os
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = Path.home() / ".ideagen.env"

for _line in ENV_FILE.read_text(encoding="utf-8").splitlines():
    _line = _line.strip()
    if not _line or _line.startswith("#") or "=" not in _line:
        continue
    _k, _v = _line.split("=", 1)
    os.environ.setdefault(_k.strip(), _v.strip())

# The vendor is reached directly, like every other network leg here: a local
# proxy hangs large responses (see scripts/tick.py for the same guard).
_no = os.environ.get("NO_PROXY", "")
os.environ["NO_PROXY"] = ",".join(filter(None, [_no, "bytepluses.com", "volces.com"]))

sys.path.insert(0, str(ROOT))

from ideagen import db  # noqa: E402
from ideagen.sources import wisburg  # noqa: E402
from ideagen.sources.wisburg import WisburgRateLimited  # noqa: E402

WINDOW = 7

#: The vendor allows 1000 calls an hour and refuses the rest in prose. One
#: week costs ~70 calls, so a full walk crosses the wall several times; waiting
#: it out is the walk, not an error in it. The pad covers clock skew between
#: the server's estimate and ours.
RETRY_PAD_S = 30
DEFAULT_WAIT_S = 20 * 60


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    start, end = (date.fromisoformat(a) for a in argv)
    if start > end:
        start, end = end, start

    con = db.init()
    stops = []
    cur = end
    while cur >= start:
        stops.append(cur)
        cur -= timedelta(days=WINDOW)

    t0 = time.time()
    total_new = 0
    waited = 0
    for n, as_of in enumerate(stops, 1):
        while True:
            try:
                rep = wisburg.ingest(con, as_of, lookback_days=WINDOW,
                                     fetch_bodies=0, verbose=False)
                break
            except WisburgRateLimited as e:
                wait = (e.retry_after or DEFAULT_WAIT_S) + RETRY_PAD_S
                waited += wait
                print(f"[{n}/{len(stops)}] {as_of} 配额用尽，等 {wait}s 后重试本周"
                      f"（累计等待 {waited // 60} 分钟）", flush=True)
                time.sleep(wait)
            except Exception as e:  # noqa: BLE001 - one bad week must not end the walk
                print(f"[{n}/{len(stops)}] {as_of} FAILED {type(e).__name__}: {e}",
                      flush=True)
                rep = None
                break
        if rep is None:
            continue
        total_new += rep["new"]
        errs = len(rep.get("errors") or {})
        print(f"[{n}/{len(stops)}] {as_of}  window={rep['total']:5d} "
              f"new={rep['new']:5d}  cum_new={total_new:6d}  "
              f"errors={errs}  {time.time() - t0:.0f}s", flush=True)

    row = db.q(con, "SELECT MIN(published_d) a, MAX(published_d) b, COUNT(*) c "
                    "FROM documents")[0]
    print(f"\ncorpus now {row['a']} → {row['b']}, {row['c']} documents "
          f"(+{total_new} this run) in {time.time() - t0:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
