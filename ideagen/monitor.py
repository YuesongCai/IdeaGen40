"""Daily position monitor.

Runs after the marks are in and answers the only question that matters between
entry and horizon: has anything changed enough that a human should look?

Alert kinds, and why each one exists rather than being left to the stop:

  stop_hit / take_hit    already executed by the engine; recorded so the daily
                         digest states what happened without reading the blotter
  stop_proximity         within 2% of the thesis stop — the stop will fire on a
                         gap, and a gap fill is worse than a planned exit
  thesis_invalidated     the theme's *pre-registered price indicator* has moved
                         against the registered direction by more than 1.5 sigma
                         since entry, while the position itself has not yet hit
                         its stop. v0.3 has no such trigger: an idea whose macro
                         premise has broken can sit in the book until the price
                         stop happens to catch up
  crowding_spike         the instrument entered the top crowding decile after
                         entry — the trade became consensus while held
  horizon_soon           three sessions from the horizon exit
  nav_stale              a fund mark is older than the disclosure threshold
  drawdown               book drawdown past a threshold
"""

from __future__ import annotations

import hashlib
from datetime import date, timedelta
from typing import Any

from . import config, db, ideas as ideas_mod, lexicon, paper
from .sources import futu_px, olive

STOP_PROXIMITY = 0.02
THESIS_SIGMA = 1.5
CROWDING_ALERT = 85.0
HORIZON_WARN_CALENDAR_DAYS = 5
BOOK_DRAWDOWN_ALERT = -0.05


def _aid(*p: Any) -> str:
    return hashlib.sha1("|".join(str(x) for x in p).encode()).hexdigest()[:16]


def run(con, d: str | None = None, verbose: bool = True) -> dict:
    d = d or futu_px.complete_through("US")
    alerts: list[dict] = []

    for book_id in config.BOOKS:   # cohorts are marked but not alerted on
        for pos in paper.positions(con, book_id, status="open"):
            alerts.extend(_check_position(con, book_id, pos, d))
        for pos in paper.positions(con, book_id, status="closed"):
            if pos["closed_d"] == d and pos["exit_reason"] in ("stop", "take", "horizon"):
                alerts.append(_mk(book_id, d,
                                  "action" if pos["exit_reason"] == "stop" else "info",
                                  f"{pos['exit_reason']}_hit", pos,
                                  f"{pos['tool']} 因 {pos['exit_reason']} 离场 @"
                                  f"{pos['close_px']:.4g}，实现 "
                                  f"{(pos['realized']/pos['cost'])*100:+.2f}%"
                                  if pos["cost"] else ""))
        eq = db.q1(con, "SELECT drawdown, equity FROM equity WHERE book_id=? AND d=?",
                   (book_id, d))
        if eq and (eq["drawdown"] or 0) <= BOOK_DRAWDOWN_ALERT:
            alerts.append({
                "alert_id": _aid("dd", book_id, d), "book_id": book_id, "d": d,
                "level": "action", "kind": "drawdown", "idea_uid": None, "code": None,
                "message": f"{paper.book_spec(book_id)['label']} 回撤 "
                           f"{eq['drawdown']*100:.2f}%，权益 ${eq['equity']:,.0f}",
            })

    if alerts:
        db.upsert_many(con, "alerts", alerts, ["alert_id"])
    by_level = {}
    for a in alerts:
        by_level[a["level"]] = by_level.get(a["level"], 0) + 1

    rep = {"d": d, "alerts": len(alerts), "by_level": by_level,
           "items": alerts}
    db.kv_set(con, f"monitor:{d}", {"d": d, "alerts": len(alerts), "by_level": by_level})
    if verbose:
        print(f"  monitor {d}: {len(alerts)} 条告警 {by_level}")
        for a in sorted(alerts, key=lambda x: x["level"] != "action")[:12]:
            print(f"    [{a['level']:<6}] {a['kind']:<20} {a['message'][:96]}")
    return rep


def _mk(book_id: str, d: str, level: str, kind: str, pos: dict, msg: str) -> dict:
    return {"alert_id": _aid(kind, book_id, pos["pos_id"], d), "book_id": book_id,
            "d": d, "level": level, "kind": kind, "idea_uid": pos["idea_uid"],
            "code": pos["code"], "message": msg}


def _check_position(con, book_id: str, pos: dict, d: str) -> list[dict]:
    out: list[dict] = []
    idea = db.q1(con, "SELECT * FROM ideas WHERE idea_uid=?", (pos["idea_uid"],))
    if not idea:
        return out
    idea = dict(idea)
    m = paper.mark_price(con, idea, d)
    if not m:
        return out
    px = m["px"]

    if m["src"] == "olive:nav" and m["stale"] > 3:
        out.append(_mk(book_id, d, "warn", "nav_stale", pos,
                       f"{pos['tool']} NAV 已 {m['stale']} 天未更新（{m['d']}），"
                       f"盯市值不可作为成交依据"))

    if pos["stop_px"] and px > pos["stop_px"]:
        gap = px / pos["stop_px"] - 1
        if gap <= STOP_PROXIMITY:
            out.append(_mk(book_id, d, "action", "stop_proximity", pos,
                           f"{pos['tool']} 距 thesis stop 仅 {gap*100:.1f}%"
                           f"（现价 {px:.4g} / 止损 {pos['stop_px']:.4g}）"))

    # Thesis invalidation via the theme's pre-registered indicator.
    theme = lexicon.THEME_BY_ID.get(idea.get("theme_id") or "")
    if theme:
        a = futu_px.last_close_on_or_before(con, theme.price_indicator, pos["opened_d"])
        b = futu_px.last_close_on_or_before(con, theme.price_indicator, d)
        sig = futu_px.realized_vol(con, theme.price_indicator, d, 60)
        if a and b and a[1] and sig:
            held = max((date.fromisoformat(d) - date.fromisoformat(pos["opened_d"])).days, 1)
            sigma_h = sig * (held / 365.0) ** 0.5
            move = b[1] / a[1] - 1
            adverse = -move if (idea.get("direction") or "↑") == "↑" else move
            if sigma_h > 0 and adverse / sigma_h >= THESIS_SIGMA:
                out.append(_mk(book_id, d, "action", "thesis_invalidated", pos,
                               f"{pos['tool']} 的主题指标 {theme.price_indicator} 自建仓以来"
                               f"逆向 {move*100:+.2f}%（{adverse/sigma_h:.1f}σ），"
                               f"宏观前提已被价格否定，但仓位尚未触发止损"))

    c = futu_px.return_percentile(con, idea["futu_code"], d, 60) if idea["futu_code"] else None
    if c is not None and c >= CROWDING_ALERT:
        out.append(_mk(book_id, d, "warn", "crowding_spike", pos,
                       f"{pos['tool']} 60日动量已进入 1 年内第 {c:.0f} 百分位，"
                       f"持有期内变成了共识交易"))

    if pos["horizon_end"]:
        # Count *calendar* days to the horizon. Counting stored sessions would
        # only ever see bars that already exist, so a horizon months away would
        # read as one session left.
        days_left = (date.fromisoformat(pos["horizon_end"]) - date.fromisoformat(d)).days
        if 0 < days_left <= HORIZON_WARN_CALENDAR_DAYS:
            out.append(_mk(book_id, d, "info", "horizon_soon", pos,
                           f"{pos['tool']} 距期限平仓还有 {days_left} 个自然日"
                           f"（{pos['horizon_end']}）"))
    return out


def digest(con, d: str | None = None) -> dict:
    """Compact daily digest for the Feishu message and the dashboard banner."""
    d = d or futu_px.complete_through("US")
    books = {}
    for b in config.BOOKS:
        eq = db.q1(con, "SELECT * FROM equity WHERE book_id=? AND d<=? "
                        "ORDER BY d DESC LIMIT 1", (b, d))
        if eq:
            books[b] = {"label": config.BOOKS[b]["label"], "d": eq["d"],
                        "equity": eq["equity"], "cum_ret": eq["cum_ret"],
                        "ret_d": eq["ret_d"], "n_open": eq["n_open"],
                        "gross": eq["gross"], "drawdown": eq["drawdown"]}
    al = db.q(con, "SELECT level, kind, message FROM alerts WHERE d=? "
                   "ORDER BY CASE level WHEN 'action' THEN 0 WHEN 'warn' THEN 1 "
                   "ELSE 2 END LIMIT 20", (d,))
    todays = db.q1(con, "SELECT batch_id, n_ideas, status FROM batches WHERE as_of=?",
                   (d,))
    return {"d": d, "books": books,
            "alerts": [dict(r) for r in al],
            "batch": dict(todays) if todays else None}
