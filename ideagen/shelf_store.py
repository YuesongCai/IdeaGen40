"""Portable product-shelf persistence for RDS/TOS and weekly inputs.

The MCP client is only one producer of a shelf snapshot. This module owns the
durable contract after capture: a minimal normalized artifact in the immutable
blob store, queryable instruments/NAVs in the state store, and a redacted
Dashboard projection. A public fixture goes through the same path, so every
write and read can be exercised before an Olive token exists.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date, datetime, timezone
from typing import Any

from . import poc_fixture, schema
from .sources import olive

FORMAT = "ideagen.portable-shelf-snapshot/v1"
PUBLIC_FIXTURE_SOURCE = "public-shelf-fixture-v1"
PUBLIC_FIXTURE_CLASSIFICATION = "public-synthetic-shelf"
LIVE_SOURCE = "olive-mcp"
LIVE_CLASSIFICATION = "licensed-live"


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def _shelf_date(value: object) -> str | None:
    """A plain YYYY-MM-DD, or nothing.

    Guards the same trap _iso_date guards in the Olive source: several shelf
    fields are named like dates and hold something else entirely (`since` is a
    since-inception RETURN, "0.4466"). A non-date written here would not raise
    -- the eligibility comparison is a string compare, and "0.4466" sorts below
    every real date, so every affected instrument would be waved through while
    the dated-coverage count improved. Failing closed to None is the point.
    """
    text = str(value or "").strip()[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return None
    try:                      # shape is not enough: "2024-13-01" matches it
        date.fromisoformat(text)
    except ValueError:
        return None
    return None if text == "1970-01-01" else text


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", value.lower()).strip("-") or "shelf"


def _utc_timestamp(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def public_alias(instrument_id: str) -> str:
    digest = hashlib.sha256(str(instrument_id).encode()).hexdigest()[:8].upper()
    return f"FUND-{digest}"


def _vehicle(group: str, item: dict[str, Any], rec: dict[str, Any]) -> str:
    text = " ".join(str(item.get(key) or "")
                    for key in ("productName", "productEnglishName", "series",
                                "marketType", "strategy"))
    if group == "cash":
        return "现金"
    if group == "structured":
        return "结构化"
    if group == "private":
        markers = ("UCITS", "SICAV", "OEIC", "日度", "每日")
        return "私募 UCITS" if any(m.lower() in text.lower() for m in markers) \
            else "私募"
    if rec.get("kind") == "fund":
        return "公募"
    return "ETF"


def _records(payload: dict | list, as_of: date) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for group, items in olive._as_groups(payload).items():
        for item in items:
            rec = olive._normalise(group, item)
            if rec is None:
                continue
            nav_d = rec.get("nav_d") or as_of.isoformat()
            rows.append({
                **rec,
                "group_name": group,
                "vehicle": _vehicle(group, item, rec),
                "exposure": (rec.get("strategy")
                             or rec.get("asset_class")
                             or {
                                 "cash": "现金管理",
                                 "structured": "结构化产品",
                                 "private": "私募策略",
                             }.get(group, "公募基金")),
                # No fallback to today. The shelf rarely carries either of
                # these, and defaulting to as_of records "the first day we
                # looked" as if it were a fact about the product -- a date that
                # is wrong, permanent (the min() below only ever lowers it),
                # and indistinguishable from a real one. NULL is the honest
                # answer and shows up in the coverage count instead.
                #
                # NOTE this column does not mean what instruments.first_seen_d
                # means. Here it is when WE first saw the product; there it is
                # when the product began to exist. Do not backfill one from the
                # other -- the values are both valid dates, so nothing would
                # catch it.
                "first_seen_d": _shelf_date(item.get("firstSeenDate")
                                            or item.get("subscriptionStart")),
                "nav_d": str(nav_d)[:10],
            })
    rows.sort(key=lambda row: row["key"])
    return rows


def persist(platform: Any, payload: dict | list, *, as_of: date,
            source: str, classification: str,
            captured_at: str | None = None) -> dict[str, Any]:
    """Normalize, archive and upsert one snapshot.

    The blob artifact contains only the normalized fields this system consumes,
    not the raw MCP response. This narrows the licensed-data footprint while the
    source response remains available to the explicit local capture workflow.
    """
    schema.migrate(platform.state)
    source_captured_at = (
        payload.get("metadata", {}).get("capturedAt")
        if isinstance(payload, dict)
        and isinstance(payload.get("metadata"), dict)
        else None
    )
    captured_at = _utc_timestamp(
        str(captured_at or source_captured_at
            or datetime.now(timezone.utc).isoformat())
    )
    records = _records(payload, as_of)
    if not records:
        raise ValueError("shelf snapshot contains no recognizable instruments")

    normalized = {
        "format": FORMAT,
        "as_of": as_of.isoformat(),
        "source": source,
        "data_classification": classification,
        "items": records,
    }
    inputs_sha = hashlib.sha256(_canonical(normalized)).hexdigest()
    snapshot_id = f"shelf-{as_of:%Y%m%d}-{inputs_sha[:12]}"
    artifact = {**normalized, "captured_at": captured_at,
                "snapshot_id": snapshot_id,
                "inputs_sha": inputs_sha}
    raw = _canonical(artifact)
    key = (f"shelves/{_slug(source)}/{as_of.isoformat()}/"
           f"{snapshot_id}.json")
    if platform.blobs.exists(key):
        existing_raw = platform.blobs.get(key)
        try:
            existing = json.loads(existing_raw)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"immutable shelf artifact is not valid JSON: {key}"
            ) from exc
        existing_normalized = {
            field: existing.get(field) for field in normalized
        }
        if _canonical(existing_normalized) != _canonical(normalized):
            raise RuntimeError(f"immutable shelf artifact drifted: {key}")
        captured_at = str(existing.get("captured_at") or captured_at)
        artifact_uri = platform.blobs.uri(key)
    else:
        artifact_uri = platform.blobs.put(
            key,
            raw,
            content_type="application/json",
            metadata={
                "classification": classification,
                "inputs-sha": inputs_sha,
            },
        )

    nav_count = sum(1 for record in records if record.get("nav") is not None)
    with platform.state.tx():
        schema.upsert(platform.state, "shelf_snapshots", {
            "snapshot_id": snapshot_id,
            "as_of": as_of.isoformat(),
            "source": source,
            "data_classification": classification,
            "captured_at": captured_at,
            "artifact_uri": artifact_uri,
            "inputs_sha": inputs_sha,
            "item_count": len(records),
            "nav_count": nav_count,
            "ok": 1,
            "error": None,
        })
        for record in records:
            instrument_id = str(record["key"])
            existing = platform.state.q(
                "SELECT MIN(first_seen_d) AS first_seen_d "
                "FROM shelf_instruments WHERE instrument_id=?",
                (instrument_id,),
            )
            prior = (existing[0].get("first_seen_d") if existing else None)
            # as_of is deliberately not a candidate: including it meant every
            # product acquired today's date the first time it was seen, so the
            # column could never be empty and the gap could never be reported.
            candidates = [value for value in (prior, record.get("first_seen_d"))
                          if value]
            first_seen = min(candidates) if candidates else None
            metadata = {
                key: value for key, value in record.items()
                if key not in {
                    "key", "name", "kind", "group_name", "currency", "vehicle",
                    "exposure", "risk_level", "strategy", "first_seen_d", "nav",
                    "nav_d",
                }
            }
            schema.upsert(platform.state, "shelf_instruments", {
                "snapshot_id": snapshot_id,
                "instrument_id": instrument_id,
                "as_of": as_of.isoformat(),
                "name": record.get("name"),
                "kind": record.get("kind") or "fund",
                "group_name": record.get("group_name"),
                "currency": record.get("currency") or "USD",
                "vehicle": record.get("vehicle"),
                "exposure": record.get("exposure"),
                "risk_level": record.get("risk_level"),
                "strategy": record.get("strategy"),
                "first_seen_d": first_seen,
                "latest_nav": record.get("nav"),
                "nav_d": record.get("nav_d"),
                "metadata": json.dumps(
                    metadata, ensure_ascii=False, separators=(",", ":"),
                    sort_keys=True, allow_nan=False),
            })
            if record.get("nav") is not None:
                schema.upsert(platform.state, "shelf_navs", {
                    "instrument_id": instrument_id,
                    "d": record.get("nav_d") or as_of.isoformat(),
                    "nav": float(record["nav"]),
                    "snapshot_id": snapshot_id,
                    "source": source,
                    "data_classification": classification,
                })

    departed = _record_departures(
        platform, snapshot_id=snapshot_id, as_of=as_of, source=source,
        present={str(r["key"]): r for r in records}, noticed_at=captured_at)

    return {
        "snapshot_id": snapshot_id,
        "as_of": as_of.isoformat(),
        "source": source,
        "classification": classification,
        "items": len(records),
        "navs": nav_count,
        "artifact_uri": artifact_uri,
        "inputs_sha": inputs_sha,
        "departed": departed["departed"],
        "returned": departed["returned"],
    }


def _record_departures(platform: Any, *, snapshot_id: str, as_of: date,
                       source: str, present: dict[str, Any],
                       noticed_at: str) -> dict[str, list[str]]:
    """Write down what the shelf lost, at the only moment it is knowable.

    A snapshot records membership. Non-membership is recorded nowhere, so a
    product dropped from the shelf does not become "gone since September" — it
    becomes a product that was never there, retroactively, for every past period
    too. Replays then pick from a shelf the week did not have, and every past
    number quietly improves, because what leaves a shelf is rarely what was
    working. That is the survivorship sin in the only form this system can
    actually commit, and the fix is not clever: notice the difference between
    two consecutive snapshots and write it down before it is unrecoverable.

    Comebacks are stamped on the existing row rather than deleting it. Deleting
    would make "left in March and returned in May" indistinguishable from "never
    left", which is precisely the distinction a replay of April needs.

    The comparison is against the previous snapshot *of the same source*: a
    public fixture and the live Olive shelf are different universes, and
    differencing one against the other would invent a hundred departures the
    first time anyone loaded a fixture.
    """
    prior = platform.state.q(
        "SELECT snapshot_id, as_of FROM shelf_snapshots "
        "WHERE ok=1 AND source=? AND snapshot_id<>? AND as_of<=? "
        "ORDER BY as_of DESC, captured_at DESC LIMIT 1",
        (source, snapshot_id, as_of.isoformat()))
    out: dict[str, list[str]] = {"departed": [], "returned": []}
    if prior:
        prior_row = dict(prior[0])
        was = platform.state.q(
            "SELECT instrument_id, name FROM shelf_instruments WHERE snapshot_id=?",
            (prior_row["snapshot_id"],))
        for row in was:
            iid = str(dict(row)["instrument_id"])
            if iid in present:
                continue
            out["departed"].append(iid)
            schema.upsert(platform.state, "shelf_departures", {
                "instrument_id": iid,
                "source": source,
                "departed_as_of": as_of.isoformat(),
                "last_seen_as_of": str(prior_row["as_of"]),
                "last_snapshot_id": prior_row["snapshot_id"],
                "name": dict(row).get("name"),
                "returned_as_of": None,
                "noticed_at": noticed_at,
            })

    # A row can only be closed by an instrument that is back on the shelf, which
    # is why this runs whether or not there was a prior snapshot to diff.
    for row in platform.state.q(
            "SELECT instrument_id, departed_as_of FROM shelf_departures "
            "WHERE source=? AND returned_as_of IS NULL", (source,)):
        r = dict(row)
        iid = str(r["instrument_id"])
        if iid in present and str(r["departed_as_of"]) <= as_of.isoformat():
            platform.state.execute(
                "UPDATE shelf_departures SET returned_as_of=? "
                "WHERE instrument_id=? AND source=? AND departed_as_of=?",
                (as_of.isoformat(), iid, source, r["departed_as_of"]))
            out["returned"].append(iid)
    return out


def departed_by(query: Any, as_of: date, *, source: str | None = None
                ) -> dict[str, str]:
    """Instruments that had left the shelf by `as_of` and had not come back.

    Takes the query callable rather than a platform so a replay holding a plain
    connection can ask the same question as the weekly run, without either side
    growing its own copy of the "and had not come back" clause — a clause that
    is easy to forget and whose absence silently deletes an instrument from
    every period after its first departure.
    """
    sql = ("SELECT instrument_id, departed_as_of, returned_as_of "
           "FROM shelf_departures WHERE departed_as_of<=?")
    args: list[Any] = [as_of.isoformat()]
    if source:
        sql += " AND source=?"
        args.append(source)
    out: dict[str, str] = {}
    for row in query(sql, args):
        r = dict(row)
        back = r.get("returned_as_of")
        if back and str(back) <= as_of.isoformat():
            continue
        iid = str(r["instrument_id"])
        prev = out.get(iid)
        if prev is None or str(r["departed_as_of"]) < prev:
            out[iid] = str(r["departed_as_of"])
    return out


def survivorship(query: Any) -> dict[str, Any]:
    """Whether a point-in-time shelf can be reconstructed at all, and from when.

    Reported rather than assumed, because the honest answer today is "no": one
    snapshot exists, so every period before it is replayed on the shelf as it
    looks now. Stating that beside a backtest is the difference between a known
    limit and an unnoticed bias.
    """
    snaps = [dict(r) for r in query(
        "SELECT as_of, source, COUNT(*) n FROM shelf_snapshots "
        "WHERE ok=1 GROUP BY as_of, source ORDER BY as_of", ())]
    gone = [dict(r) for r in query(
        "SELECT instrument_id, departed_as_of, returned_as_of "
        "FROM shelf_departures", ())]
    dates = sorted({str(s["as_of"]) for s in snaps})
    return {
        "snapshots": len(snaps),
        "snapshot_dates": dates,
        "first_snapshot": dates[0] if dates else None,
        "departures_recorded": len(gone),
        "departures_open": sum(1 for g in gone if not g.get("returned_as_of")),
        "point_in_time_from": dates[0] if len(dates) > 1 else None,
        "note": (
            "货架的删除侧。快照只记「当时在架上的」，不记「已经不在了的」，"
            "所以一只被下架的产品会追溯性地从所有历史期消失，过去的回测数字"
            "会安静地变好——离开货架的通常正是表现差的那些。"
            "从第二次快照起，两次之间少掉的标的会被记进 shelf_departures；"
            "第一次快照之前的期次，回放用的仍然是今天这张货架，"
            "这是已知上限，不是可以忽略的细节。"),
    }


def fixture_payload(as_of: date) -> dict[str, Any]:
    """Rebase the public fixture into an Olive-shaped shelf snapshot."""
    source = poc_fixture.read().document["inputs"].get("universe") or []
    elapsed = (as_of - date(2026, 1, 1)).days
    items = []
    for index, row in enumerate(source):
        phase = index * 0.73
        nav = ((100.0 + index * 4.0)
               * (1.0 + 0.00022 * elapsed
                  + 0.012 * math.sin(elapsed / 19.0 + phase)))
        items.append({
            "productCode": row["instrument_id"],
            "productName": row["name"],
            "currency": "USD",
            "strategy": row.get("exposure"),
            "latestNav": round(nav, 6),
            "navDate": as_of.isoformat(),
            "riskLevel": f"R{2 + index % 3}",
            "subscriptionStart": "2026-01-01",
        })
    return {
        "funds": items,
        "metadata": {
            "fixture_id": "poc-public-dashboard-v1",
            "classification": PUBLIC_FIXTURE_CLASSIFICATION,
            "synthetic": True,
        },
    }


def persist_fixture(platform: Any, as_of: date) -> dict[str, Any]:
    return persist(
        platform,
        fixture_payload(as_of),
        as_of=as_of,
        source=PUBLIC_FIXTURE_SOURCE,
        classification=PUBLIC_FIXTURE_CLASSIFICATION,
        captured_at=f"{as_of.isoformat()}T07:00:00+08:00",
    )


def latest_snapshot(state: Any, *, as_of: date,
                    classification: str | None = None,
                    source: str | None = None) -> dict[str, Any] | None:
    sql = (
        "SELECT snapshot_id, as_of, source, data_classification, captured_at, "
        "artifact_uri, inputs_sha, item_count, nav_count "
        "FROM shelf_snapshots WHERE ok=1 AND as_of<=?"
    )
    args: list[Any] = [as_of.isoformat()]
    if classification:
        sql += " AND data_classification=?"
        args.append(classification)
    if source:
        sql += " AND source=?"
        args.append(source)
    sql += " ORDER BY as_of DESC, captured_at DESC LIMIT 1"
    rows = state.q(sql, args)
    return dict(rows[0]) if rows else None


def universe(state: Any, *, as_of: date,
             classification: str | None = None,
             source: str | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    snapshot = latest_snapshot(
        state, as_of=as_of, classification=classification, source=source)
    if not snapshot:
        raise RuntimeError(
            f"no successful shelf snapshot exists on or before {as_of}")
    rows = state.q(
        "SELECT instrument_id, name, kind, group_name, currency, vehicle, "
        "exposure, risk_level, strategy, first_seen_d, latest_nav, nav_d "
        "FROM shelf_instruments WHERE snapshot_id=? ORDER BY instrument_id",
        (snapshot["snapshot_id"],),
    )
    out = [{
        "instrument_id": row["instrument_id"],
        "name": row.get("name") or row["instrument_id"],
        "kind": row.get("kind") or "fund",
        "priceable": row.get("latest_nav") is not None,
        "currency": row.get("currency") or "USD",
        "vehicle": row.get("vehicle") or "公募",
        "exposure": row.get("exposure") or row.get("strategy") or "未映射",
        "olive_key": row["instrument_id"],
        "futu_code": None,
        "liquidity": "daily",
        "first_seen_d": row.get("first_seen_d"),
        "nav": row.get("latest_nav"),
        "nav_d": row.get("nav_d"),
        "risk_level": row.get("risk_level"),
        "group": row.get("group_name"),
        "snapshot_id": snapshot["snapshot_id"],
    } for row in rows]
    return snapshot, out


def nav_on_or_before(state: Any, instrument_id: str,
                     d: str, *,
                     classification: str | None = None) -> dict[str, Any] | None:
    sql = (
        "SELECT instrument_id, d, nav, snapshot_id, source, "
        "data_classification FROM shelf_navs "
        "WHERE instrument_id=? AND d<=?"
    )
    args: list[Any] = [instrument_id, d]
    if classification:
        sql += " AND data_classification=?"
        args.append(classification)
    sql += " ORDER BY d DESC LIMIT 1"
    rows = state.q(sql, args)
    return dict(rows[0]) if rows else None


def dashboard_state(state: Any, *, as_of: date | None = None,
                    show_names: bool = False) -> dict[str, Any]:
    as_of = as_of or date.today()
    snapshot = latest_snapshot(state, as_of=as_of)
    if not snapshot:
        return {}
    rows = state.q(
        "SELECT instrument_id, name, kind, group_name, currency, risk_level, "
        "latest_nav, nav_d FROM shelf_instruments "
        "WHERE snapshot_id=? ORDER BY instrument_id",
        (snapshot["snapshot_id"],),
    )
    licensed = str(snapshot["data_classification"]).startswith("licensed-")
    instruments = []
    for row in rows:
        alias = public_alias(row["instrument_id"])
        instruments.append({
            "instrument": alias if licensed else row["instrument_id"],
            "name": (row.get("name") if show_names or not licensed
                     else f"Licensed fund {alias}"),
            "kind": row.get("kind"),
            "group": row.get("group_name"),
            "currency": row.get("currency"),
            "risk_level": row.get("risk_level"),
            "latest_nav": row.get("latest_nav"),
            "nav_d": row.get("nav_d"),
        })
    return {
        "snapshot_id": snapshot["snapshot_id"],
        "as_of": snapshot["as_of"],
        "source": snapshot["source"],
        "data_classification": snapshot["data_classification"],
        "captured_at": snapshot["captured_at"],
        "items": int(snapshot.get("item_count") or 0),
        "navs": int(snapshot.get("nav_count") or 0),
        "artifact_archived": bool(snapshot.get("artifact_uri")),
        "identifiers_redacted": licensed,
        "names_redacted": licensed and not show_names,
        "instruments": instruments,
    }
