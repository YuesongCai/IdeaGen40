"""Portable Wisburg corpus persistence for cloud state.

The legacy ingest path is intentionally SQLite-specific. This module keeps the
cloud path narrow: normalize the existing Wisburg ``Item`` contract, archive a
private immutable snapshot, write the bounded scoring projection to RDS, and
read an as-of corpus back without consulting the network.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from . import config, schema
from .sources import wisburg

CLASSIFICATION = "licensed-private"
SOURCE = "wisburg-mcp"


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def _archive(platform: Any, *, as_of: date, rows: list[dict[str, Any]],
             source: str, classification: str) -> tuple[str, str]:
    document = {
        "format": "ideagen.portable-corpus-snapshot/v1",
        "as_of": as_of.isoformat(),
        "source": source,
        "data_classification": classification,
        "rows": rows,
    }
    raw = _canonical(document)
    digest = hashlib.sha256(raw).hexdigest()
    key = f"corpus/{source}/{as_of.isoformat()}/{digest}.json"
    if platform.blobs.exists(key):
        if platform.blobs.get(key) != raw:
            raise RuntimeError(f"immutable corpus artifact drifted: {key}")
        uri = platform.blobs.uri(key)
    else:
        uri = platform.blobs.put(
            key,
            raw,
            content_type="application/json",
            metadata={
                "classification": classification,
                "inputs-sha": digest,
            },
        )
    return uri, digest


def _item_row(item: wisburg.Item, *, ingested_at: str,
              raw_uri: str | None = None) -> dict[str, Any]:
    body = (item.body or "")[:12_000]
    return {
        "doc_id": item.doc_id,
        "published_d": item.published_d,
        "title": (item.title or "")[:1000],
        "tier": int(item.tier or 3),
        "line": item.line,
        "institution": (item.institution or "")[:500] or None,
        "summary": (item.summary or "")[:6000],
        "body": body,
        "content_hash": item.content_hash,
        "retrieval": item.retrieval,
        "raw_uri": raw_uri or item.raw_uri,
        "data_classification": CLASSIFICATION,
        "ingested_at": ingested_at,
        "metadata": json.dumps({
            "category": item.category,
            "source_id": item.source_id,
            "body_chars": len(item.body or ""),
            "body_sha256": item.body_sha256,
            "assets": len(item.assets or []),
        }, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
    }


def persist(platform: Any, rows: Iterable[dict[str, Any]], *, as_of: date,
            source: str = SOURCE,
            classification: str = CLASSIFICATION) -> dict[str, Any]:
    """Persist normalized corpus rows and one feed/run receipt."""
    schema.migrate(platform.state)
    material = [dict(row) for row in rows if row.get("doc_id")
                and row.get("published_d") and row.get("title")]
    if material:
        ids = [str(row["doc_id"]) for row in material]
        existing = {
            str(row["doc_id"]): dict(row)
            for row in platform.state.q(
                "SELECT doc_id, body, raw_uri, metadata "
                "FROM corpus_documents WHERE doc_id IN (%s)"
                % ",".join("?" * len(ids)),
                ids,
            )
        }
        for row in material:
            previous = existing.get(str(row["doc_id"]))
            if not previous:
                continue
            preserved_body = False
            if not row.get("body") and previous.get("body"):
                row["body"] = previous["body"]
                preserved_body = True
            if not row.get("raw_uri") and previous.get("raw_uri"):
                row["raw_uri"] = previous["raw_uri"]
            old_meta = previous.get("metadata") or {}
            new_meta = row.get("metadata") or {}
            if isinstance(old_meta, str):
                old_meta = json.loads(old_meta)
            if isinstance(new_meta, str):
                new_meta = json.loads(new_meta)
            row["metadata"] = {**old_meta, **new_meta}
            if preserved_body:
                row["metadata"]["body_chars"] = (
                    old_meta.get("body_chars") or len(str(row["body"]))
                )
                if old_meta.get("body_sha256"):
                    row["metadata"]["body_sha256"] = old_meta["body_sha256"]
    artifact_uri, digest = _archive(
        platform,
        as_of=as_of,
        rows=material,
        source=source,
        classification=classification,
    )
    run_id = f"ingest-{as_of:%Y%m%d}-{digest[:12]}"
    now = datetime.now(timezone.utc).isoformat()

    with platform.state.tx():
        schema.upsert(platform.state, "orch_runs", {
            "run_id": run_id,
            "as_of": as_of.isoformat(),
            "kind": "ingest",
            "platform": platform.name,
            "started_at": now,
            "ended_at": now,
            "ok": 1,
            "error": None,
            "inputs_sha": digest,
            "journal_uri": artifact_uri,
            "calls": 0,
            "data_classification": classification,
        })
        for row in material:
            normalized = {
                "doc_id": str(row["doc_id"]),
                "published_d": str(row["published_d"])[:10],
                "title": str(row["title"])[:1000],
                "tier": int(row.get("tier") or 3),
                "line": row.get("line"),
                "institution": row.get("institution"),
                "summary": str(row.get("summary") or "")[:6000],
                "body": str(row.get("body") or "")[:12_000],
                "content_hash": row.get("content_hash"),
                "retrieval": row.get("retrieval"),
                "raw_uri": row.get("raw_uri"),
                "data_classification": classification,
                "ingested_at": row.get("ingested_at") or now,
                "metadata": (row.get("metadata")
                             if isinstance(row.get("metadata"), str)
                             else json.dumps(
                                 row.get("metadata") or {},
                                 ensure_ascii=False,
                                 separators=(",", ":"),
                                 sort_keys=True)),
            }
            schema.upsert(platform.state, "corpus_documents", normalized)
        schema.upsert(platform.state, "feed_runs", {
            "run_id": run_id,
            "feed": source,
            "kind": "corpus",
            "as_of": as_of.isoformat(),
            "n_rows": len(material),
            "ok": 1,
            "error": None,
            "rows_sha": digest,
        })
    return {
        "run_id": run_id,
        "as_of": as_of.isoformat(),
        "source": source,
        "classification": classification,
        "rows": len(material),
        "artifact_uri": artifact_uri,
        "inputs_sha": digest,
    }


def _deep_fetch(client: wisburg.Wisburg, item: wisburg.Item,
                platform: Any) -> str | None:
    if item.source_id is None or not item.category:
        return None
    data = (client.article_detail(item.source_id)
            if item.line == "articles"
            else client.detail(item.source_id, item.category))
    raw = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    if not raw.strip():
        return None
    digest = hashlib.sha256(raw.encode()).hexdigest()
    key = f"corpus/raw/{item.line}/{item.source_id}/{digest}.txt"
    encoded = raw.encode()
    if platform.blobs.exists(key):
        if platform.blobs.get(key) != encoded:
            raise RuntimeError(f"immutable corpus detail drifted: {key}")
        uri = platform.blobs.uri(key)
    else:
        uri = platform.blobs.put(
            key,
            encoded,
            content_type="text/plain; charset=utf-8",
            metadata={
                "classification": CLASSIFICATION,
                "sha256": digest,
            },
        )
    item.body_sha256 = digest
    item.raw_uri = uri
    if len(raw) > len(item.body or ""):
        item.body = wisburg._strip_html(raw)[:60_000]
        item.sections = wisburg.parse_detail(raw)
        item.assets = list(dict.fromkeys(
            [*(item.assets or []), *wisburg.extract_assets(raw)]))
    return uri


def _fetch(platform: Any, *, as_of: date, full_window: bool,
           detail_limit: int, lines: Iterable[str] | None) -> dict[str, Any]:
    schema.migrate(platform.state)
    if not config.wisburg_configured():
        raise RuntimeError("Wisburg MCP key is not configured")
    client = wisburg.Wisburg()
    client.initialize()
    start = as_of - timedelta(days=config.OBSERVATION_WINDOW_DAYS - 1)
    items: dict[str, wisburg.Item] = {}
    errors: dict[str, str] = {}
    for line in (list(lines) if lines is not None else list(config.SOURCE_LINES)):
        try:
            if full_window:
                got: list[wisburg.Item] = []
                for day_offset in range((as_of - start).days + 1):
                    day = start + timedelta(days=day_offset)
                    got.extend(client.list_line(line, day, day, max_pages=12))
            else:
                got = client.list_line(
                    line, start, as_of, limit=20, max_pages=1)
        except Exception as exc:  # noqa: BLE001
            errors[line] = f"{type(exc).__name__}: {exc}"[:240]
            continue
        for item in got:
            if item.published_d and start.isoformat() <= item.published_d <= \
                    as_of.isoformat():
                items[item.doc_id] = item

    existing = {
        row["doc_id"]: dict(row) for row in platform.state.q(
            "SELECT doc_id, body, raw_uri FROM corpus_documents "
            "WHERE published_d>=? AND published_d<=?",
            (start.isoformat(), as_of.isoformat()),
        )
    }
    fresh = [item for item in items.values() if item.doc_id not in existing]
    deep_candidates = [
        item for item in items.values()
        if item.doc_id not in existing
        or not (
            existing[item.doc_id].get("body")
            and existing[item.doc_id].get("raw_uri")
        )
    ]
    deep = 0
    for item in sorted(
            deep_candidates,
            key=lambda value: (value.tier, value.published_d or ""),
        ):
        if deep >= max(0, detail_limit):
            break
        if item.tier not in config.FACT_TIERS or item.source_id is None:
            continue
        try:
            if _deep_fetch(client, item, platform):
                deep += 1
        except Exception as exc:  # noqa: BLE001
            errors[f"detail:{item.doc_id}"] = (
                f"{type(exc).__name__}: {exc}"[:240])

    now = datetime.now(timezone.utc).isoformat()
    rows = [_item_row(item, ingested_at=now) for item in items.values()]
    receipt = persist(platform, rows, as_of=as_of)
    receipt.update({
        "listed": len(items),
        "new": len(fresh),
        "deep": deep,
        "errors": errors,
    })
    return receipt


def ingest_window(platform: Any, as_of: date, *,
                  detail_limit: int = 8,
                  lines: Iterable[str] | None = None) -> dict[str, Any]:
    return _fetch(
        platform,
        as_of=as_of,
        full_window=True,
        detail_limit=detail_limit,
        lines=lines,
    )


def ingest_incremental(platform: Any, *, as_of: date | None = None,
                       detail_limit: int = 3,
                       lines: Iterable[str] | None = None) -> dict[str, Any]:
    return _fetch(
        platform,
        as_of=as_of or config.today_hkt(),
        full_window=False,
        detail_limit=detail_limit,
        lines=lines,
    )


def corpus(state: Any, *, as_of: date,
           window_days: int = config.OBSERVATION_WINDOW_DAYS
           ) -> list[dict[str, Any]]:
    start = as_of - timedelta(days=max(window_days, 1) - 1)
    rows = state.q(
        "SELECT doc_id, published_d, title, tier, line, institution, summary, "
        "body, content_hash, retrieval FROM corpus_documents "
        "WHERE published_d>=? AND published_d<=? "
        "ORDER BY published_d DESC, tier, doc_id",
        (start.isoformat(), as_of.isoformat()),
    )
    return [{
        "doc_id": row["doc_id"],
        "published_d": row["published_d"],
        "title": row["title"],
        "tier": int(row.get("tier") or 3),
        "line": row.get("line"),
        "institution": row.get("institution"),
        "summary": row.get("summary"),
        "body": row.get("body"),
        "content_hash": row.get("content_hash"),
        "retrieval": row.get("retrieval"),
    } for row in rows]
