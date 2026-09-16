"""Social / independent-author sources: the early leg of the diffusion path.

yifu, 2026-09-11: an idea travels X / Reddit → fringe forums and independent
writers → sell-side summaries. Wisburg is the last stop, so by the time a theme
scores well there it is already old. This module collects the earlier stops.

Three rules shape everything below, and each exists because the research leg
already paid for learning it:

* **Separate table, never the research pool.** Items land in `social_items`,
  not `documents`. Nothing here is a tier, nothing is counted by
  `scoring.collect_evidence`, and `config.SOCIAL_WEIGHT` stays 0. A blog post
  counted as a sell-side note would move TIS with no one having decided it
  should.
* **Point-in-time like research.** `items_as_of` returns only items whose
  *publication* time is at or before the cutoff — the same discipline as
  Wisburg's disclosure-time truncation. Engagement counts are refreshed on
  every re-fetch and therefore describe the fetch day, not the publication
  day; nothing downstream reads them for a historical cutoff.
* **Disabled is a status, not a silence.** X and Reddit OAuth need credentials
  this machine does not have. Each writes a status row saying exactly which
  variable is missing, so the panel can say 「未启用：需要 X_BEARER_TOKEN」
  instead of showing an empty list that reads as a quiet week (the
  「读不到 vs 没有」 shape this repo keeps finding).

Networking is stdlib urllib, which honours the environment proxy on its own.
Every network call goes through an injectable `http` callable so tests run on
fixtures and never touch the network.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from .. import config, db

Http = Callable[..., tuple[int, bytes, dict]]

SOURCE_RSS = "rss"
SOURCE_REDDIT = "reddit"
SOURCE_X = "x"

#: Aggregate feed keys the panel's `feeds` list shows, one per source family.
FEED_KEYS = {SOURCE_RSS: "social-rss", SOURCE_REDDIT: "social-reddit",
             SOURCE_X: "social-x"}

DDL = (
    """CREATE TABLE IF NOT EXISTS social_items (
    item_id      TEXT PRIMARY KEY,   -- sha1 of canonical url (or feed|title|time)
    source       TEXT NOT NULL,      -- rss | reddit | x
    feed         TEXT NOT NULL,      -- feed key / subreddit / x query label
    author       TEXT,
    url          TEXT,
    title        TEXT,
    summary      TEXT,
    published_at TEXT NOT NULL,      -- ISO UTC, 'YYYY-MM-DDTHH:MM:SSZ'
    published_d  TEXT NOT NULL,      -- HKT calendar day, same axis as documents
    fetched_at   TEXT NOT NULL,      -- first time this item was seen
    last_seen_at TEXT,
    lang         TEXT,
    engagement   TEXT,               -- JSON, as of last_seen_at (not publication)
    meta         TEXT
)""",
    "CREATE INDEX IF NOT EXISTS ix_social_pub ON social_items(published_at)",
    "CREATE INDEX IF NOT EXISTS ix_social_day ON social_items(published_d, source)",
    """CREATE TABLE IF NOT EXISTS social_sources (
    feed          TEXT PRIMARY KEY,
    source        TEXT NOT NULL,
    name          TEXT,
    enabled       INTEGER NOT NULL,
    reason        TEXT,              -- why disabled / degraded, in words
    last_attempt  TEXT,
    last_ok       TEXT,
    ok            INTEGER,
    n_fetched     INTEGER,
    n_new         INTEGER,
    error         TEXT
)""",
)


def ensure_tables(con) -> None:
    """Own the two tables here rather than in db.SCHEMA.

    The schema-drift gate baselines db.SCHEMA, and two other workstreams edit
    db.py in parallel. These tables are additive, read by this leg only, and
    created on first use — so a fresh database, the cloud node and a test's
    :memory: all get them without anyone touching the shared declaration.
    """
    for stmt in DDL:
        con.execute(stmt)


# ---------------------------------------------------------------- feed list
def load_feed_list(path: Path | str | None = None) -> dict:
    p = Path(path or config.SOCIAL_FEEDS_PATH)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"rss": [], "reddit": {"subreddits": []}, "x": {"kols": []}}


# ---------------------------------------------------------------- time
def _utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hkt_day(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(config.TZ).date().isoformat()


def parse_time(s: str | None) -> datetime | None:
    """RFC-822 (RSS) or ISO-8601 (Atom / JSON). None rather than 'now'.

    Defaulting an unparseable date to the fetch time would stamp an old post
    as fresh — exactly the look-ahead the cutoff exists to prevent. Such items
    are dropped instead.
    """
    if not s:
        return None
    s = s.strip()
    try:
        dt = parsedate_to_datetime(s)
        if dt is not None:
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, IndexError):
        pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def cutoff_utc(cutoff: date | datetime | str) -> str:
    """End of the HKT day for a date; the instant itself for a datetime."""
    if isinstance(cutoff, str):
        cutoff = (datetime.fromisoformat(cutoff) if "T" in cutoff
                  else date.fromisoformat(cutoff))
    if isinstance(cutoff, datetime):
        return _utc_iso(cutoff if cutoff.tzinfo else cutoff.replace(tzinfo=config.TZ))
    end = datetime(cutoff.year, cutoff.month, cutoff.day, 23, 59, 59, tzinfo=config.TZ)
    return _utc_iso(end)


# ---------------------------------------------------------------- text
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_CJK = re.compile(r"[一-鿿]")


def strip_html(s: str | None, limit: int = 1200) -> str:
    if not s:
        return ""
    t = html.unescape(_TAG.sub(" ", s))
    return _WS.sub(" ", t).strip()[:limit]


def guess_lang(text: str) -> str:
    if not text:
        return ""
    return "zh" if len(_CJK.findall(text)) / max(len(text), 1) > 0.15 else "en"


def canonical_url(url: str | None) -> str:
    if not url:
        return ""
    u = urllib.parse.urlsplit(url.strip())
    q = [(k, v) for k, v in urllib.parse.parse_qsl(u.query)
         if not k.lower().startswith("utm_")]
    return urllib.parse.urlunsplit((u.scheme.lower(), u.netloc.lower(), u.path,
                                    urllib.parse.urlencode(q), ""))


def item_id_for(url: str | None, feed: str, title: str, published_at: str) -> str:
    key = canonical_url(url) or f"{feed}|{title}|{published_at}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def make_item(*, source: str, feed: str, title: str, url: str | None,
              published: datetime, fetched_at: str, author: str | None = None,
              summary: str | None = None, lang: str | None = None,
              engagement: dict | None = None, meta: dict | None = None) -> dict:
    """The one item shape every adapter returns."""
    title = strip_html(title, 400)
    summ = strip_html(summary)
    pub = _utc_iso(published)
    return {
        "item_id": item_id_for(url, feed, title, pub),
        "source": source, "feed": feed,
        "author": (author or "").strip() or None,
        "url": url, "title": title, "summary": summ,
        "published_at": pub, "published_d": _hkt_day(published),
        "fetched_at": fetched_at, "last_seen_at": fetched_at,
        "lang": lang or guess_lang(f"{title} {summ}"),
        "engagement": engagement or None, "meta": meta or None,
    }


# ---------------------------------------------------------------- http
def default_http(url: str, *, headers: dict | None = None, data: bytes | None = None,
                 method: str | None = None,
                 timeout: float = config.SOCIAL_HTTP_TIMEOUT_S) -> tuple[int, bytes, dict]:
    """urllib with a User-Agent; HTTP errors come back as a status, not a raise.

    Callers branch on 401/403/429 to write a status in words, so an HTTPError
    is data here. Transport errors (DNS, TLS, timeout) still raise.
    """
    h = {"User-Agent": config.SOCIAL_USER_AGENT, "Accept": "*/*"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:  # noqa: BLE001
            body = b""
        return e.code, body, dict(e.headers or {})


# ---------------------------------------------------------------- RSS / Atom
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _child(el, name: str):
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def _children(el, name: str):
    return [c for c in el if _local(c.tag) == name]


def _text(el, *names: str) -> str:
    for n in names:
        c = _child(el, n)
        if c is not None and (c.text or "").strip():
            return c.text.strip()
    return ""


_BAD_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def parse_feed(body: bytes, *, source: str, feed: str, fetched_at: str,
               default_author: str | None = None) -> list[dict]:
    """RSS 2.0 or Atom → items. Items without a parseable date are dropped."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        root = ET.fromstring(_BAD_XML.sub("", body.decode("utf-8", "replace")).encode("utf-8"))
    out: list[dict] = []
    feed_lang = ""
    if _local(root.tag) == "rss":
        chan = _child(root, "channel")
        entries = _children(chan, "item") if chan is not None else []
        feed_lang = _text(chan, "language") if chan is not None else ""
        for it in entries:
            pub = parse_time(_text(it, "pubDate", "date", "published", "updated"))
            if pub is None:
                continue
            title = _text(it, "title")
            summ = _text(it, "description") or _text(it, "encoded")
            author = _text(it, "creator", "author") or default_author
            out.append(make_item(source=source, feed=feed, title=title,
                                 url=_text(it, "link") or _text(it, "guid"),
                                 published=pub, fetched_at=fetched_at,
                                 author=author, summary=summ,
                                 lang=(feed_lang[:2].lower() or None)))
    elif _local(root.tag) == "feed":
        for it in _children(root, "entry"):
            pub = parse_time(_text(it, "published", "updated"))
            if pub is None:
                continue
            link = ""
            for l in _children(it, "link"):
                if l.get("rel") in (None, "alternate"):
                    link = l.get("href") or ""
                    break
            a = _child(it, "author")
            author = (_text(a, "name") if a is not None else "") or default_author
            out.append(make_item(source=source, feed=feed, title=_text(it, "title"),
                                 url=link, published=pub, fetched_at=fetched_at,
                                 author=author,
                                 summary=_text(it, "summary") or _text(it, "content")))
    else:
        raise ValueError(f"不是 RSS/Atom：根元素 <{_local(root.tag)}>")
    return out


def parse_substack_archive(body: bytes, *, feed: str, fetched_at: str,
                           default_author: str | None = None) -> list[dict]:
    out = []
    for p in json.loads(body.decode("utf-8")):
        pub = parse_time(p.get("post_date"))
        if pub is None:
            continue
        bylines = [b.get("name") for b in (p.get("publishedBylines") or []) if b.get("name")]
        out.append(make_item(
            source=SOURCE_RSS, feed=feed, title=p.get("title") or "",
            url=p.get("canonical_url"), published=pub, fetched_at=fetched_at,
            author=", ".join(bylines) or default_author,
            summary=p.get("subtitle") or p.get("description") or "",
            engagement={"reactions": p.get("reaction_count"),
                        "comments": p.get("comment_count")},
            meta={"audience": p.get("audience")}))
    return out


def _page_url(spec: dict, page: int, page_size: int = 25) -> str | None:
    """URL for page `page` (0-based) of a feed's history, or None if unpaged."""
    url, kind = spec["url"], spec.get("paging") or ""
    if page == 0:
        return url
    sep = "&" if "?" in url else "?"
    if kind == "wordpress":
        return f"{url}{sep}paged={page + 1}"
    if kind == "blogger":
        return f"{url}{sep}start-index={page * page_size + 1}&max-results={page_size}"
    if kind == "substack":
        u = urllib.parse.urlsplit(url)
        off = (page - 1) * page_size
        return (f"{u.scheme}://{u.netloc}/api/v1/archive?sort=new"
                f"&offset={off}&limit={page_size}")
    return None


def fetch_rss_feed(spec: dict, *, since: datetime, http: Http, fetched_at: str,
                   max_pages: int = config.SOCIAL_RSS_MAX_PAGES) -> tuple[list[dict], list[str]]:
    """One feed, walking back page by page until items predate `since`.

    Paging failures after page 0 are notes, not errors: the base feed already
    produced the recent items, and a blog whose archive endpoint moved should
    not read as a dead source.
    """
    items: list[dict] = []
    notes: list[str] = []
    for page in range(max_pages):
        url = _page_url(spec, page)
        if url is None:
            break
        status, body, _ = http(url)
        if status != 200:
            if page == 0:
                raise RuntimeError(f"HTTP {status}")
            notes.append(f"第 {page + 1} 页 HTTP {status}，停止回翻")
            break
        try:
            if spec.get("paging") == "substack" and page > 0:
                got = parse_substack_archive(body, feed=spec["key"], fetched_at=fetched_at,
                                             default_author=spec.get("name"))
            else:
                got = parse_feed(body, source=SOURCE_RSS, feed=spec["key"],
                                 fetched_at=fetched_at, default_author=spec.get("name"))
        except Exception as e:  # noqa: BLE001
            if page == 0:
                raise
            notes.append(f"第 {page + 1} 页解析失败（{type(e).__name__}），停止回翻")
            break
        if not got:
            break
        items.extend(got)
        oldest = min(parse_time(i["published_at"]) for i in got)
        if oldest < since:
            break
    since_iso = _utc_iso(since)
    return [i for i in items if i["published_at"] >= since_iso], notes


# ---------------------------------------------------------------- Reddit
def reddit_credentials() -> tuple[str, str] | None:
    cid = (os.environ.get("REDDIT_CLIENT_ID") or "").strip()
    sec = (os.environ.get("REDDIT_CLIENT_SECRET") or "").strip()
    return (cid, sec) if cid and sec else None


def reddit_token(http: Http, cid: str, secret: str) -> str:
    auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    status, body, _ = http("https://www.reddit.com/api/v1/access_token",
                           headers={"Authorization": f"Basic {auth}",
                                    "Content-Type": "application/x-www-form-urlencoded"},
                           data=b"grant_type=client_credentials", method="POST")
    if status != 200:
        raise RuntimeError(f"Reddit 取令牌 HTTP {status}（检查 REDDIT_CLIENT_ID/SECRET）")
    tok = json.loads(body.decode("utf-8")).get("access_token")
    if not tok:
        raise RuntimeError("Reddit 令牌响应里没有 access_token")
    return tok


def parse_reddit_listing(body: bytes, *, sub: str, fetched_at: str) -> list[dict]:
    out = []
    for ch in (json.loads(body.decode("utf-8")).get("data") or {}).get("children") or []:
        d = ch.get("data") or {}
        if d.get("stickied"):
            continue      # mod announcements are not discussion
        ts = d.get("created_utc")
        if ts is None:
            continue
        out.append(make_item(
            source=SOURCE_REDDIT, feed=f"r/{sub}", title=d.get("title") or "",
            url="https://www.reddit.com" + (d.get("permalink") or ""),
            published=datetime.fromtimestamp(float(ts), tz=timezone.utc),
            fetched_at=fetched_at, author=d.get("author"),
            summary=d.get("selftext") or "",
            engagement={"score": d.get("score"), "comments": d.get("num_comments")}))
    return out


def fetch_reddit(subs: Iterable[str], *, since: datetime, http: Http, fetched_at: str,
                 sleep: Callable[[float], None] = time.sleep) -> tuple[list[dict], dict]:
    """OAuth listing when credentials exist; keyless Atom RSS otherwise.

    The keyless path returned 200 once and 429 on the very next request in the
    2026-09-17 probe, so it pauses between subreddits and stops at the first
    429 rather than hammering. It also carries no score or comment counts.
    """
    subs = list(subs)
    creds = reddit_credentials()
    items: list[dict] = []
    st: dict[str, Any] = {"enabled": 1, "notes": []}
    if creds:
        tok = reddit_token(http, *creds)
        st["mode"] = "oauth"
        for s in subs:
            status, body, _ = http(f"https://oauth.reddit.com/r/{s}/new?limit=100",
                                   headers={"Authorization": f"Bearer {tok}"})
            if status != 200:
                st["notes"].append(f"r/{s} HTTP {status}")
                continue
            items.extend(parse_reddit_listing(body, sub=s, fetched_at=fetched_at))
    else:
        st["mode"] = "rss"
        st["reason"] = ("降级：无钥 RSS（限流严重、无互动数）；填 REDDIT_CLIENT_ID / "
                        "REDDIT_CLIENT_SECRET 后改走 OAuth")
        for i, s in enumerate(subs):
            if i:
                sleep(config.SOCIAL_REDDIT_PAUSE_S)
            status, body, _ = http(f"https://www.reddit.com/r/{s}/new/.rss?limit=100")
            if status == 429:
                st["notes"].append(f"r/{s} 429 限流，本轮余下子版块跳过")
                break
            if status != 200:
                st["notes"].append(f"r/{s} HTTP {status}")
                continue
            for it in parse_feed(body, source=SOURCE_REDDIT, feed=f"r/{s}",
                                 fetched_at=fetched_at):
                items.append(it)
    since_iso = _utc_iso(since)
    return [i for i in items if i["published_at"] >= since_iso], st


# ---------------------------------------------------------------- X
X_SEARCH = "https://api.twitter.com/2/tweets/search/recent"
X_MAX_QUERY = 512


def x_token() -> str | None:
    return (os.environ.get("X_BEARER_TOKEN") or "").strip() or None


def _is_ascii_term(t: str) -> bool:
    return bool(t) and all(ord(c) < 128 for c in t)


def x_queries(themes: Iterable[Any], kols: Iterable[str] = (),
              max_len: int = X_MAX_QUERY) -> list[tuple[str, str]]:
    """(label, query) pairs: one per theme's English terms, then KOL chunks.

    Only ASCII terms go in — X search is word-based and the Chinese terms that
    discovered themes carry would match almost nothing in English timelines. A
    theme with no English term is skipped here and reported by diffusion as
    「词表无英文叫法」 rather than silently searched with nothing.
    """
    tail = " -is:retweet lang:en"
    out: list[tuple[str, str]] = []
    for th in themes:
        terms = [t for t in (list(th.terms) + list(getattr(th, "alias_terms", ()) or ()))
                 if _is_ascii_term(t) and len(t) >= 3]
        parts: list[str] = []
        for t in dict.fromkeys(terms):
            q = f'"{t}"' if " " in t else t
            cand = "(" + " OR ".join(parts + [q]) + ")" + tail
            if len(cand) > max_len:
                break
            parts.append(q)
        if parts:
            out.append((th.id, "(" + " OR ".join(parts) + ")" + tail))
    chunk: list[str] = []
    for k in [k.lstrip("@") for k in kols if k]:
        cand = "(" + " OR ".join(chunk + [f"from:{k}"]) + ") -is:retweet"
        if len(cand) > max_len and chunk:
            out.append(("kol", "(" + " OR ".join(chunk) + ") -is:retweet"))
            chunk = []
        chunk.append(f"from:{k}")
    if chunk:
        out.append(("kol", "(" + " OR ".join(chunk) + ") -is:retweet"))
    return out


def parse_x_search(body: bytes, *, label: str, fetched_at: str) -> list[dict]:
    d = json.loads(body.decode("utf-8"))
    users = {u["id"]: u for u in ((d.get("includes") or {}).get("users") or [])}
    out = []
    for tw in d.get("data") or []:
        pub = parse_time(tw.get("created_at"))
        if pub is None:
            continue
        u = users.get(tw.get("author_id")) or {}
        handle = u.get("username") or tw.get("author_id") or "unknown"
        text = tw.get("text") or ""
        out.append(make_item(
            source=SOURCE_X, feed=f"x:{label}", title=text[:140],
            url=f"https://x.com/{handle}/status/{tw.get('id')}",
            published=pub, fetched_at=fetched_at, author=f"@{handle}",
            summary=text, lang=tw.get("lang"),
            engagement=tw.get("public_metrics")))
    return out


def fetch_x(con, themes: Iterable[Any], kols: Iterable[str], *, http: Http,
            fetched_at: str, today: date,
            budget: int = config.SOCIAL_X_DAILY_REQUESTS,
            max_results: int = config.SOCIAL_X_MAX_RESULTS) -> tuple[list[dict], dict]:
    """Spend at most `budget` requests per HKT day, rotating through queries.

    Rotation is by day so a budget smaller than the theme count still covers
    every theme over a few days, instead of the first ten themes forever.
    """
    tok = x_token()
    if not tok:
        return [], {"enabled": 0, "reason": "未启用：需要 X_BEARER_TOKEN"}
    queries = x_queries(themes, kols)
    used_key = f"social.x.requests.{today.isoformat()}"
    used = int(db.kv_get(con, used_key, 0) or 0)
    left = max(0, budget - used)
    st: dict[str, Any] = {"enabled": 1, "notes": [], "budget": budget, "used_before": used}
    if not queries:
        st["reason"] = "没有可查询的英文词项或 KOL"
        return [], st
    if left == 0:
        st["reason"] = f"今日预算 {budget} 次已用完"
        return [], st
    start = today.toordinal() % len(queries)
    order = queries[start:] + queries[:start]
    items: list[dict] = []
    n = min(left, len(order)) if budget else 0
    for label, q in order[:n]:
        url = X_SEARCH + "?" + urllib.parse.urlencode({
            "query": q, "max_results": max(10, min(100, max_results)),
            "tweet.fields": "created_at,public_metrics,author_id,lang",
            "expansions": "author_id", "user.fields": "username,name"})
        status, body, _ = http(url, headers={"Authorization": f"Bearer {tok}"})
        used += 1
        db.kv_set(con, used_key, used)
        if status in (401, 403):
            raise RuntimeError(f"X API HTTP {status}：X_BEARER_TOKEN 无效或套餐不含 recent search")
        if status == 429:
            st["notes"].append("429 限流，本轮停止")
            break
        if status != 200:
            st["notes"].append(f"{label} HTTP {status}")
            continue
        items.extend(parse_x_search(body, label=label, fetched_at=fetched_at))
    st["requests"] = n
    return items, st


# ---------------------------------------------------------------- storage
def store(con, items: Iterable[dict]) -> int:
    """Insert new items; for known ones refresh only engagement + last_seen.

    Title, summary and publication time are never overwritten: the first copy
    is what was knowable when it was first fetched, and a later edit to a post
    must not rewrite what a historical cutoff could see.
    """
    ensure_tables(con)
    n_new = 0
    for it in items:
        cur = con.execute(
            "INSERT OR IGNORE INTO social_items(item_id,source,feed,author,url,title,"
            "summary,published_at,published_d,fetched_at,last_seen_at,lang,engagement,meta)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (it["item_id"], it["source"], it["feed"], it.get("author"), it.get("url"),
             it.get("title"), it.get("summary"), it["published_at"], it["published_d"],
             it["fetched_at"], it.get("last_seen_at") or it["fetched_at"], it.get("lang"),
             json.dumps(it["engagement"], ensure_ascii=False) if it.get("engagement") else None,
             json.dumps(it["meta"], ensure_ascii=False) if it.get("meta") else None))
        if cur.rowcount:
            n_new += 1
        elif it.get("engagement"):
            con.execute("UPDATE social_items SET engagement=?, last_seen_at=? WHERE item_id=?",
                        (json.dumps(it["engagement"], ensure_ascii=False),
                         it["fetched_at"], it["item_id"]))
    return n_new


def record_status(con, *, feed: str, source: str, name: str, enabled: bool,
                  ok: bool | None, attempted_at: str, n_fetched: int = 0,
                  n_new: int = 0, reason: str | None = None,
                  error: str | None = None) -> None:
    ensure_tables(con)
    prev = db.q1(con, "SELECT last_ok FROM social_sources WHERE feed=?", (feed,))
    last_ok = attempted_at if ok else (prev["last_ok"] if prev else None)
    con.execute(
        "INSERT INTO social_sources(feed,source,name,enabled,reason,last_attempt,last_ok,"
        "ok,n_fetched,n_new,error) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(feed) DO UPDATE SET source=excluded.source,name=excluded.name,"
        "enabled=excluded.enabled,reason=excluded.reason,last_attempt=excluded.last_attempt,"
        "last_ok=excluded.last_ok,ok=excluded.ok,n_fetched=excluded.n_fetched,"
        "n_new=excluded.n_new,error=excluded.error",
        (feed, source, name, int(bool(enabled)), reason, attempted_at, last_ok,
         None if ok is None else int(bool(ok)), n_fetched, n_new, error))


def items_as_of(con, cutoff: date | datetime | str, *, since: date | str | None = None,
                sources: Iterable[str] | None = None) -> list[dict]:
    """Items published at or before `cutoff` (a date = end of that HKT day).

    The only read path diffusion uses. Filtering on `published_at` — never on
    `fetched_at` — mirrors Wisburg's disclosure-time cut: a replay of 08-26 sees
    what had been *published* by 08-26, whether we fetched it then or today.
    """
    ensure_tables(con)
    sql = ("SELECT item_id,source,feed,author,url,title,summary,published_at,published_d,"
           "fetched_at,lang FROM social_items WHERE published_at<=?")
    args: list[Any] = [cutoff_utc(cutoff)]
    if since is not None:
        sql += " AND published_d>=?"
        args.append(since if isinstance(since, str) else since.isoformat())
    if sources:
        src = list(sources)
        sql += " AND source IN (%s)" % ",".join("?" * len(src))
        args.extend(src)
    return [dict(r) for r in db.q(con, sql + " ORDER BY published_at", args)]


# ---------------------------------------------------------------- orchestration
def ingest(con, as_of: date | None = None, *, lookback_days: int = config.SOCIAL_LOOKBACK_DAYS,
           http: Http | None = None, sources: Iterable[str] = (SOURCE_RSS, SOURCE_REDDIT, SOURCE_X),
           feeds_path: Path | str | None = None, now: datetime | None = None,
           log: Callable[[str], None] = print,
           sleep: Callable[[float], None] = time.sleep, full: bool = False,
           max_pages: int = config.SOCIAL_RSS_MAX_PAGES) -> dict:
    """Fetch every enabled source; one source failing never stops the others.

    `full=True` walks every feed back the whole `lookback_days` even when it
    already has history — the backfill switch. Without it a second run with a
    longer lookback would stop at the newest stored item and never reach older
    weeks.
    """
    ensure_tables(con)
    http = http or default_http
    now = now or config.now_hkt()
    fetched_at = _utc_iso(now)
    today = (as_of or now.astimezone(config.TZ).date())
    spec = load_feed_list(feeds_path)
    sources = set(sources)
    rep: dict[str, Any] = {"fetched_at": fetched_at, "sources": {}, "n_new": 0, "n_fetched": 0}

    def since_for(feed: str) -> datetime:
        base = now - timedelta(days=lookback_days)
        # Incremental: once a feed has history, only walk back to its newest
        # stored item (minus a day of slack for late-dated posts).
        r = None if full else db.q1(
            con, "SELECT MAX(published_at) m FROM social_items WHERE feed=?", (feed,))
        if r and r["m"]:
            last = parse_time(r["m"]) - timedelta(days=1)
            return max(base, last)
        return base

    if SOURCE_RSS in sources:
        ok_feeds = bad = new = got = 0
        per: list[dict] = []
        for f in spec.get("rss") or []:
            try:
                items, notes = fetch_rss_feed(f, since=since_for(f["key"]), http=http,
                                              fetched_at=fetched_at, max_pages=max_pages)
                n_new = store(con, items)
                ok_feeds += 1
                new += n_new
                got += len(items)
                record_status(con, feed=f["key"], source=SOURCE_RSS, name=f.get("name") or f["key"],
                              enabled=True, ok=True, attempted_at=fetched_at,
                              n_fetched=len(items), n_new=n_new,
                              reason="；".join(notes) or None)
                per.append({"feed": f["key"], "ok": True, "n": len(items), "new": n_new})
            except Exception as e:  # noqa: BLE001 — one dead blog must not end the leg
                bad += 1
                err = f"{type(e).__name__}: {e}"[:300]
                record_status(con, feed=f["key"], source=SOURCE_RSS, name=f.get("name") or f["key"],
                              enabled=True, ok=False, attempted_at=fetched_at, error=err)
                per.append({"feed": f["key"], "ok": False, "error": err})
        record_status(con, feed=FEED_KEYS[SOURCE_RSS], source=SOURCE_RSS,
                      name="独立作者 RSS", enabled=True, ok=ok_feeds > 0,
                      attempted_at=fetched_at, n_fetched=got, n_new=new,
                      reason=(f"{bad} 个源失败" if bad else None),
                      error=(None if ok_feeds else "全部 RSS 源失败"))
        rep["sources"][SOURCE_RSS] = {"ok": ok_feeds, "failed": bad, "n": got, "new": new, "feeds": per}
        rep["n_new"] += new
        rep["n_fetched"] += got
        log(f"      RSS {ok_feeds}/{ok_feeds + bad} 个源通，抓到 {got} 条，新增 {new}")

    if SOURCE_REDDIT in sources:
        subs = (spec.get("reddit") or {}).get("subreddits") or []
        try:
            since = min((since_for(f"r/{s}") for s in subs), default=now - timedelta(days=lookback_days))
            items, st = fetch_reddit(subs, since=since, http=http, fetched_at=fetched_at, sleep=sleep)
            n_new = store(con, items)
            notes = "；".join(st.get("notes") or [])
            record_status(con, feed=FEED_KEYS[SOURCE_REDDIT], source=SOURCE_REDDIT,
                          name="Reddit", enabled=True, ok=bool(items) or not notes,
                          attempted_at=fetched_at, n_fetched=len(items), n_new=n_new,
                          reason="；".join(x for x in (st.get("reason"), notes) if x) or None,
                          error=(notes if not items and notes else None))
            rep["sources"][SOURCE_REDDIT] = {"mode": st.get("mode"), "n": len(items), "new": n_new,
                                             "notes": st.get("notes")}
            rep["n_new"] += n_new
            rep["n_fetched"] += len(items)
            log(f"      Reddit（{st.get('mode')}）抓到 {len(items)} 条，新增 {n_new}"
                + (f"；{notes}" if notes else ""))
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"[:300]
            record_status(con, feed=FEED_KEYS[SOURCE_REDDIT], source=SOURCE_REDDIT, name="Reddit",
                          enabled=True, ok=False, attempted_at=fetched_at, error=err)
            rep["sources"][SOURCE_REDDIT] = {"error": err}
            log(f"      ! Reddit 失败：{err}")

    if SOURCE_X in sources:
        from .. import lexicon
        try:
            items, st = fetch_x(con, lexicon.all_themes(today), (spec.get("x") or {}).get("kols") or [],
                                http=http, fetched_at=fetched_at, today=today)
            if not st.get("enabled"):
                record_status(con, feed=FEED_KEYS[SOURCE_X], source=SOURCE_X, name="X",
                              enabled=False, ok=None, attempted_at=fetched_at,
                              reason=st.get("reason"))
                rep["sources"][SOURCE_X] = {"enabled": False, "reason": st.get("reason")}
                log(f"      X {st.get('reason')}")
            else:
                n_new = store(con, items)
                record_status(con, feed=FEED_KEYS[SOURCE_X], source=SOURCE_X, name="X",
                              enabled=True, ok=True, attempted_at=fetched_at,
                              n_fetched=len(items), n_new=n_new,
                              reason="；".join(x for x in [st.get("reason")] + (st.get("notes") or []) if x) or None)
                rep["sources"][SOURCE_X] = {"n": len(items), "new": n_new,
                                            "requests": st.get("requests")}
                rep["n_new"] += n_new
                rep["n_fetched"] += len(items)
                log(f"      X {st.get('requests', 0)} 次请求，抓到 {len(items)} 条，新增 {n_new}")
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"[:300]
            record_status(con, feed=FEED_KEYS[SOURCE_X], source=SOURCE_X, name="X",
                          enabled=True, ok=False, attempted_at=fetched_at, error=err)
            rep["sources"][SOURCE_X] = {"error": err}
            log(f"      ! X 失败：{err}")
    return rep


def monitor_tick(con, now: datetime | None = None, *, dry_run: bool = False,
                 min_interval_s: int = config.SOCIAL_MONITOR_INTERVAL_S,
                 http: Http | None = None, log: Callable[[str], None] = lambda m: None) -> dict:
    """The scheduler's light increment: rate-limited, never raises.

    The monitor runs every 15 minutes; social sources update a few times a day
    and Reddit's keyless path 429s on the second request. So this does nothing
    unless `min_interval_s` has passed since the last attempt, and only RSS +
    X by default — Reddit's keyless path is left to the daily run.
    """
    now = now or config.now_hkt()
    try:
        ensure_tables(con)
        last = db.kv_get(con, "social.monitor.last_attempt")
        if last:
            age = (now - datetime.fromisoformat(last)).total_seconds()
            if age < min_interval_s:
                return {"skipped": f"上次社交抓取 {int(age)}s 前，间隔 {min_interval_s}s"}
        if dry_run:
            return {"skipped": "dry-run"}
        db.kv_set(con, "social.monitor.last_attempt", now.isoformat())
        srcs = [SOURCE_RSS, SOURCE_X] + ([SOURCE_REDDIT] if reddit_credentials() else [])
        rep = ingest(con, http=http, sources=srcs, now=now, log=log)
        return {"n_new": rep["n_new"], "n_fetched": rep["n_fetched"]}
    except Exception as e:  # noqa: BLE001 — the monitor must never fail on this leg
        return {"failed": f"{type(e).__name__}: {e}"[:300]}


def status(con, *, now: datetime | None = None,
           window_days: int = config.SOCIAL_LOOKBACK_DAYS) -> dict:
    """What the panel shows: per-source counts, last fetch, and why-not."""
    ensure_tables(con)
    now = now or config.now_hkt()
    since_d = (now.astimezone(config.TZ).date() - timedelta(days=window_days)).isoformat()
    rows = {r["feed"]: dict(r) for r in db.q(con, "SELECT * FROM social_sources")}
    counts = {(r["source"], r["feed"]): r["n"] for r in db.q(
        con, "SELECT source, feed, COUNT(*) n FROM social_items WHERE published_d>=? "
             "GROUP BY source, feed", (since_d,))}
    totals: dict[str, int] = {}
    for (src, _feed), n in counts.items():
        totals[src] = totals.get(src, 0) + n
    spec = load_feed_list()

    def agg(src: str, label: str, default_reason: str | None) -> dict:
        r = rows.get(FEED_KEYS[src]) or {}
        enabled = bool(r.get("enabled")) if r else default_reason is None
        return {"source": src, "feed": FEED_KEYS[src], "label": label,
                "enabled": enabled,
                "reason": r.get("reason") or (None if enabled else default_reason),
                "last_attempt": r.get("last_attempt"), "last_ok": r.get("last_ok"),
                "ok": None if r.get("ok") is None else bool(r.get("ok")),
                "error": r.get("error"), "n_window": totals.get(src, 0),
                "window_days": window_days}

    rss_feeds = []
    for f in spec.get("rss") or []:
        r = rows.get(f["key"]) or {}
        rss_feeds.append({"feed": f["key"], "name": f.get("name"), "focus": f.get("focus"),
                          "n_window": counts.get((SOURCE_RSS, f["key"]), 0),
                          "ok": None if r.get("ok") is None else bool(r.get("ok")),
                          "last_ok": r.get("last_ok"), "error": r.get("error"),
                          "note": r.get("reason")})
    x_default = None if x_token() else "未启用：需要 X_BEARER_TOKEN"
    out = {
        "sources": [
            agg(SOURCE_RSS, "独立作者 RSS", None),
            agg(SOURCE_REDDIT, "Reddit", None),
            agg(SOURCE_X, "X", x_default),
        ],
        "rss_feeds": rss_feeds,
        "total_window": sum(totals.values()),
        "last_item": (db.q1(con, "SELECT MAX(published_at) m FROM social_items") or {"m": None})["m"],
        "in_tis": False,
        "weight": config.SOCIAL_WEIGHT,
    }
    # X has no row until the first daily run; say why now, not after it runs.
    if not x_token():
        out["sources"][2].update(enabled=False, reason="未启用：需要 X_BEARER_TOKEN")
    if not reddit_credentials():
        red = out["sources"][1]
        red["reason"] = red.get("reason") or ("降级：无钥 RSS（限流严重、无互动数）；填 "
                                              "REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET 后改走 OAuth")
    return out


def feed_rows(con, *, now: datetime | None = None) -> list[dict]:
    """Status in the shape of `/api/state`'s `feeds` list (kind='social')."""
    try:
        st = status(con, now=now)
    except Exception as e:  # noqa: BLE001
        return [{"feed": "social-rss", "kind": "social", "as_of": None, "n_rows": 0,
                 "ok": False, "error": f"{type(e).__name__}: {e}"}]
    out = []
    for s in st["sources"]:
        when = s.get("last_attempt")
        as_of = parse_time(when).astimezone(config.TZ).date().isoformat() if when else None
        out.append({"feed": s["feed"], "kind": "social", "as_of": as_of,
                    "n_rows": s["n_window"],
                    "ok": bool(s["enabled"]) and s.get("ok") is not False,
                    "error": s.get("error") or (None if s["enabled"] else s.get("reason"))})
    return out
