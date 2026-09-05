"""Broad market news as a second corpus line — registered, and off by default.

The user's instruction on 2026-09-05 was explicit:「不止是 wisburg 作为数据源呀」.
This is that feed. It is also the one piece of today's work that is switched off
until they say otherwise, and the reasons are worth stating rather than buried,
because "built but disabled" is only honest if the condition for enabling it is
written down.

Why it is off
-------------
**It changes what the model reads, not what the record contains.** Every other
feed added today lands in `events` and reaches the generators as extra context.
This one lands in `corpus`, which is the input to 筛选A — the topic scorer whose
own mandate is「热度为主 MECE」. Frequency-weighted topic selection over a corpus
of ~40 Wisburg research pieces plus a news wire behaves differently from the same
scorer over the research alone, and not because the news is worse: because there
is more of it. The user set this rule themselves on the look-through leg — a
model input switched on mid-stream makes later periods incomparable with earlier
ones — and it applies here with more force.

**It is not replay-clean yet.** `wisburg_corpus` reads rows ingest already
stored, so a replay of an old week sees what that week saw. This feed calls the
vendor live, so replaying 2026-08-05 through it would return September's news
under an August `as_of`. Persisting at ingest time fixes that and is the right
next step if the answer is yes; until then, turning it on would put a
look-ahead path into the one part of the pipeline that has been clean.

**The 初心 has a line pointing the other way.**「信息源 credible/stable 够了不
用多」. That is not a veto — the same document names「宏观日历」as a required
source, which is why everything else today shipped on — but it is a standing
preference that a person should override deliberately, not a flag that should
flip because a feed happened to get written.

Enable with `IDEAGEN_CORPUS_FMP_NEWS=1`. Read the three paragraphs above first.
"""

from __future__ import annotations

import hashlib
import os
from datetime import date, timedelta
from typing import Any, Iterable

from ..feeds import register
from ..sources import fmp


def enabled() -> bool:
    return (os.environ.get("IDEAGEN_CORPUS_FMP_NEWS", "") or "").strip().lower() \
        in ("1", "true", "yes", "on")


@register("fmp_news", "corpus", label="FMP 市场新闻（默认关闭）",
          expect_rows=0, params={"limit": 60, "window_days": 3})
def fmp_news(as_of: date, params: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """Recent general-market news in corpus shape, tiered 3.

    Yields nothing at all when disabled — not an error. A disabled feed is a
    configuration state, and raising here would paint every run's feed report
    with a failure that is actually a setting.

    `expect_rows=0` for the same reason: a floor would fire on every run while
    the flag is off, and a warning that is always on is a warning nobody reads.
    The cost of that choice is that a genuinely dead endpoint looks like a quiet
    day *once the flag is on*, so `enabled()` and a real floor should move
    together if this is ever switched on for good.
    """
    if not enabled():
        return

    n = int(params.get("window_days", 3))
    floor = (as_of - timedelta(days=n)).isoformat()
    for r in fmp.news_general(limit=int(params.get("limit", 60))):
        pub = str(r.get("publishedDate") or "")[:10]
        if not pub or pub < floor or pub > as_of.isoformat():
            continue
        title = str(r.get("title") or "").strip()
        if not title:
            continue
        url = str(r.get("url") or "")
        yield {
            # The vendor supplies no stable id, and two outlets run the same
            # headline. Hashing url+title keeps near-duplicates distinct while
            # making the same article from the same run idempotent.
            "doc_id": "fmpnews:" + hashlib.sha1(
                (url + "|" + title).encode("utf-8")).hexdigest()[:20],
            "published_d": pub,
            "title": title,
            # Tier 3 = curated, per the corpus schema. This is a wire, not
            # first-hand work and not sell-side research; scoring it as either
            # would let volume stand in for authority.
            "tier": 3,
            "institution": r.get("publisher") or None,
            "summary": (str(r.get("text") or "")[:600] or None),
            "url": url or None,
            "retrieval": "fmp:news/general-latest",
        }
