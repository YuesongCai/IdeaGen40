"""Restore report bodies erased by the shallow-re-ingest clobber.

Before `upsert_many` grew `keep_if_blank`, a document that reappeared in a
list fetch after being deep-fetched was rewritten with an empty body — 442 of
654 archived reports lost their text this way. The raw vendor bytes survived
in the content-addressed archive (`corpus/raw/<line>/<id>_<sha>.md`), so this
is a restore, not a re-fetch: read the archive, strip, truncate, put back.

Afterwards, any document with a body but no summary gets the mechanical
opening excerpt (same rule new ingests apply), so stance coding and calib's
evidence match can finally see the report lines.

Idempotent; run anywhere the state DB and blob store live (local or ECS).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ideagen import platform as plat  # noqa: E402
from ideagen.sources.wisburg import _excerpt, _strip_html  # noqa: E402


def main() -> int:
    p = plat.load()
    rows = p.state.q(
        "SELECT doc_id, raw_uri FROM documents "
        "WHERE raw_uri IS NOT NULL AND raw_uri!='' AND (body IS NULL OR body='')")
    restored, failed = 0, []
    for r in rows:
        uri = str(r["raw_uri"])
        # tos://<bucket>/<key> or a local path — the blob port takes the key.
        key = uri.split("/", 3)[3] if "://" in uri else uri
        try:
            md = p.blobs.get(key).decode("utf-8")
        except Exception as e:  # noqa: BLE001 - a missing blob is reported, not fatal
            failed.append((r["doc_id"], str(e)[:80]))
            continue
        body = _strip_html(md)[:60_000]
        if not body.strip():
            failed.append((r["doc_id"], "archive empty after strip"))
            continue
        p.state.execute(
            "UPDATE documents SET body=?, body_chars=? WHERE doc_id=?",
            (body, len(body), r["doc_id"]))
        restored += 1

    filled = 0
    for r in p.state.q(
            "SELECT doc_id, body FROM documents "
            "WHERE (summary IS NULL OR summary='') AND body IS NOT NULL AND body!=''"):
        ex = _excerpt(r["body"])
        if ex:
            p.state.execute("UPDATE documents SET summary=? WHERE doc_id=?",
                            (ex, r["doc_id"]))
            filled += 1

    print(f"restored bodies: {restored}")
    print(f"summaries filled from body excerpt: {filled}")
    if failed:
        print(f"failed: {len(failed)} (first 5: {failed[:5]})")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
