"""Wisburg research lines as a corpus feed.

Reads what ingest already stored rather than calling the API: the feed's job is to
present rows in the corpus shape as of a date, and re-fetching here would make a
replay of an old week depend on what the vendor serves today.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Iterable

from .. import config, db
from ..feeds import register


@register("wisburg", "corpus", label="Wisburg 研报", required=True,
          params={"window_days": 3})
def wisburg_corpus(as_of: date, params: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """Research text published in the trailing window, tiered by source line."""
    n = int(params.get("window_days", config.OBSERVATION_WINDOW_DAYS))
    days = [(as_of - timedelta(days=i)).isoformat() for i in range(n)]
    con = db.init()
    rows = db.q(con,
                "SELECT doc_id, published_d, title, tier, line, institution, "
                "       summary, body, content_hash, retrieval "
                "FROM documents WHERE published_d IN (%s) "
                "ORDER BY published_d DESC, tier" % ",".join("?" * len(days)),
                days)
    for r in rows:
        yield {
            "doc_id": r["doc_id"],
            "published_d": r["published_d"],
            "title": r["title"] or "",
            "tier": int(r["tier"] or 3),
            "line": r["line"],
            "institution": r["institution"],
            "summary": r["summary"],
            "body": r["body"],
            "content_hash": r["content_hash"],
            "retrieval": r["retrieval"],
        }
