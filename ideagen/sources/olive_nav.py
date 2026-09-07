"""Olive NAV history: date the shelf's month-labelled series and load it.

Why this exists (Jon, 2026-09-07: 「为啥 AI 端到端选取没数」 → 「那你拉进来啊，
为什么拉不到呢」): three of the four idea generators propose shelf funds only,
and this node had no NAV series for any fund, so nothing they proposed could
ever be booked. The shelf *does* publish a history — `shelf_performance`
returns `performance.series[].data.navSeries`, ~20 points per month for a
daily-dealing fund — but every point is labelled with the month only
(`{"month": "2026-07", "nav": "109.47"}`), which is why `olive.py` kept the
newest point per month and called the rest unusable.

The dating rule, stated so it can be argued with: within a month the points
arrive newest first; a completed month's points are assigned to that month's
weekdays counting back from the last weekday, and the current (partial)
month's points to its weekdays counting forward from the first. A holiday
shifts a point by a day; a month with more points than weekdays is rejected
rather than squeezed. Every row is stored with `src='olive:perf:inferred-d'`
so a mark can always say the day was inferred, and `mark()`'s staleness
count is unaffected (it compares dates, and a day either way is within the
10-day tolerance).
"""

from __future__ import annotations

import calendar
import json
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .. import db

SRC = "olive:perf:inferred-d"
#: Fewer points than this and the fund cannot be marked through a 30-day
#: hold with any confidence; it stays unpriceable and the reason is recorded.
MIN_POINTS = 20
#: Points inside the series' last 90 days for the fund to count as tradable
#: in the paper engine (weekly or better); see `import_dir`.
DENSE_POINTS_90D = 12
#: A dated NAV more than this factor away from the series around it is on a
#: different basis (share class / unit), not a different observation.
BASIS_TOLERANCE = 1.25


def _nearest(rows: list[tuple[str, float]], d: str) -> float | None:
    """Series value nearest to `d` by date (rows are sorted ascending)."""
    best: tuple[int, float] | None = None
    dd = date.fromisoformat(d)
    for rd, v in rows:
        gap = abs((date.fromisoformat(rd) - dd).days)
        if best is None or gap < best[0]:
            best = (gap, v)
        elif gap > best[0]:
            break
    return best[1] if best else None


def _weekdays(year: int, month: int) -> list[date]:
    n = calendar.monthrange(year, month)[1]
    return [date(year, month, d) for d in range(1, n + 1)
            if date(year, month, d).weekday() < 5]


def _num(v: Any) -> float | None:
    try:
        f = float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def date_series(payload: dict[str, Any], *, today: date) -> tuple[list[tuple[str, float]], dict[str, Any]]:
    """Dated (d, nav) rows from one `shelf_performance` payload, plus a receipt.

    Picks the series with the most NAV points (a product may carry both the
    underlying fund's monthly returns and its own daily NAV series).
    """
    perf = (payload.get("performance") or {})
    best: list[dict] = []
    name = None
    for entry in perf.get("series") or []:
        pts = ((entry or {}).get("data") or {}).get("navSeries") or []
        if len(pts) > len(best):
            best, name = pts, (entry.get("nameCn") or entry.get("name"))
    by_month: dict[str, list[float]] = {}
    for p in best:
        m = str((p or {}).get("month") or "").strip()
        v = _num((p or {}).get("nav"))
        if re.fullmatch(r"\d{4}-\d{2}", m) and v is not None and v > 0:
            by_month.setdefault(m, []).append(v)     # newest first, as published
    rows: list[tuple[str, float]] = []
    rejected: dict[str, str] = {}
    cur = today.strftime("%Y-%m")
    for m, vals in by_month.items():
        y, mo = int(m[:4]), int(m[5:7])
        days = _weekdays(y, mo)
        if m > cur:
            rejected[m] = f"{len(vals)} 个点晚于今天"
            continue
        if len(vals) > len(days):
            rejected[m] = f"{len(vals)} 个点多于当月 {len(days)} 个工作日"
            continue
        asc = list(reversed(vals))                   # oldest first
        if m == cur:
            picks = days[:len(asc)]                  # forward from the 1st
        else:
            picks = days[len(days) - len(asc):]      # back from the last weekday
        rows.extend((d.isoformat(), v) for d, v in zip(picks, asc))
    rows.sort()
    return rows, {"series": name, "points": len(best), "dated": len(rows),
                  "months": len(by_month), "rejected": rejected,
                  "as_of": (perf.get("meta") or {}).get("asOfDate"),
                  "frequency": (perf.get("meta") or {}).get("dataFrequency")}


def _unwrap(text: str) -> dict[str, Any]:
    v: Any = text
    for _ in range(3):
        if isinstance(v, str):
            s = v.strip()
            if s[:1] in "{[":
                v = json.loads(s)
                continue
            return {}
        if isinstance(v, dict) and set(v) == {"result"}:
            v = v["result"]
            continue
        break
    return v if isinstance(v, dict) else {}


def import_dir(con, folder: Path, *, today: date | None = None,
               min_points: int = MIN_POINTS) -> dict[str, Any]:
    """Load every `<code>.json` in `folder` into `navs`; flag funds priceable.

    Idempotent: `navs` is keyed on (olive_key, d) and rows carry `src`, so a
    later snapshot NAV for the same day (dated by Olive, not inferred) can
    overwrite an inferred one, never the reverse — see the `src` guard.
    """
    today = today or date.today()
    out: dict[str, Any] = {"loaded": {}, "empty": [], "short": {}, "errors": {}}
    n_rows = 0
    for f in sorted(folder.glob("*.json")):
        code = f.stem
        try:
            payload = _unwrap(f.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001 — one bad file must not stop the load
            out["errors"][code] = f"{type(e).__name__}: {e}"[:160]
            continue
        if not payload or payload.get("code") in ("800",) or payload.get("data", 1) is None:
            out["errors"][code] = str(payload.get("message") or "服务调用错误")[:120] if payload else "空文件"
            continue
        rows, rec = date_series(payload, today=today)
        if not rows:
            out["empty"].append(code)
            continue
        if len(rows) < min_points:
            out["short"][code] = len(rows)
            continue
        existing = {r["d"]: (r["src"], float(r["nav"])) for r in db.q(
            con, "SELECT d, src, nav FROM navs WHERE olive_key=?", (code,))}
        # A NAV Olive itself dated (a shelf snapshot) normally wins over an
        # inferred one. Unless it is on a different basis: on 2026-09-07 the
        # snapshot point for L03244 read 421.67 inside a series running at
        # ~15,500 — another share class or unit — and keeping it put a −97%
        # spike on 08-31 that stopped out three books at once. A dated point
        # that sits outside `BASIS_TOLERANCE` of the series around it is not
        # the same number; it is replaced with the series value for that day
        # (or dropped when the series has none) and the conflict recorded.
        by_d = dict(rows)
        conflicts: list[dict[str, Any]] = []
        for d, (src, nav) in list(existing.items()):
            if src == SRC or not (rows[0][0] <= d <= rows[-1][0]):
                continue
            ref = by_d.get(d) or _nearest(rows, d)
            if ref and not (1 / BASIS_TOLERANCE <= nav / ref <= BASIS_TOLERANCE):
                conflicts.append({"d": d, "snapshot": nav, "series": ref, "src": src})
                if d in by_d:
                    existing[d] = (SRC, by_d[d])       # let the series row overwrite
                else:
                    con.execute("DELETE FROM navs WHERE olive_key=? AND d=?", (code, d))
                    existing.pop(d)
        payload_rows = [{"olive_key": code, "d": d, "nav": v, "src": SRC}
                        for d, v in rows
                        if d not in existing or existing[d][0] == SRC]
        n_rows += db.upsert_many(con, "navs", payload_rows, ["olive_key", "d"])
        if conflicts:
            out.setdefault("basis_conflicts", {})[code] = conflicts
        # Priceable means the paper engine can actually trade it: a fill
        # needs a NAV no older than 3 days (`paper._try_fill`) and a mark no
        # older than 10. A fund that publishes monthly has a usable NAV a
        # third of the time and fills almost never, so it is loaded (marks
        # can use the points) but not flagged. Weekly or better in the last
        # 90 days is the line.
        last_d = date.fromisoformat(rows[-1][0])
        recent = sum(1 for d, _ in rows
                     if date.fromisoformat(d) >= last_d - timedelta(days=90))
        dense = recent >= DENSE_POINTS_90D
        if dense:
            con.execute("UPDATE instruments SET priceable=1 WHERE key=? OR olive_key=?",
                        (code, code))
        else:
            out["monthly_only"] = out.get("monthly_only", []) + [code]
        out["loaded"][code] = {"rows": len(payload_rows), "first": rows[0][0],
                               "last": rows[-1][0], "recent_90d": recent,
                               "priceable": dense, **rec}
    con.commit()
    out["n_rows"] = n_rows
    return out
