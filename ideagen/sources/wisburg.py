"""Wisburg client: MCP-over-HTTP, spoken directly.

The Wisburg MCP server is a plain streamable-HTTP JSON-RPC endpoint, so a cron
job does not need an MCP host to talk to it. This module implements the wire
protocol (initialize / tools/list / tools/call over SSE-framed responses) and a
thin ingest layer on top that walks all eight source lines and normalises every
item into the `documents` table.

Why all eight lines rather than the homepage daily alone: the 2026-07-28 source
diagnostic showed that pinning D and A to the daily-report count collapses 40%
of the Tactical Impact Score into a 20-cell lookup table, and starves N of the
first-hand facts it needs. v0.4 counts *items* across tiered lines instead.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable

import requests

from .. import config, db

_UA = "IdeaGen40/1.0 (+https://github.com/YuesongCai/IdeaGen40)"


class WisburgError(RuntimeError):
    pass


@dataclass
class Item:
    line: str
    category: str | None
    tier: int
    source_id: int | None
    title: str
    published_at: str | None
    url: str | None
    summary: str
    body: str
    institution: str | None
    meta: dict
    sections: dict | None = None

    @property
    def body_is_deep(self) -> bool:
        """True once the full detail document has been merged in."""
        return bool(self.sections) or len(self.body) > 1500

    @property
    def doc_id(self) -> str:
        base = f"{self.line}:{self.source_id}" if self.source_id is not None else \
               f"{self.line}:{hashlib.sha1(self.title.encode()).hexdigest()[:16]}"
        return base

    @property
    def published_d(self) -> str | None:
        return _to_hkt_date(self.published_at)

    @property
    def content_hash(self) -> str:
        norm = re.sub(r"\s+", "", (self.title or "") + (self.summary or "")[:400])
        return hashlib.sha1(norm.encode("utf-8")).hexdigest()


class Wisburg:
    """Minimal MCP-over-HTTP client. Stateless server, so no session handling."""

    def __init__(self, url: str | None = None, token: str | None = None, timeout: int = 90):
        self.url = url or config.WISBURG_URL
        self.token = token or config.wisburg_token()
        self.timeout = timeout
        self._id = 0
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": _UA,
        })
        self._tools: list[str] | None = None

    # -------------------------------------------------------- wire protocol
    def _rpc(self, method: str, params: dict | None = None, retries: int = 3) -> Any:
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
        last: Exception | None = None
        for attempt in range(retries):
            try:
                r = self.s.post(self.url, json=payload, timeout=self.timeout)
                if r.status_code == 429 or 500 <= r.status_code < 600:
                    raise WisburgError(f"HTTP {r.status_code}: {r.text[:200]}")
                r.raise_for_status()
                # server omits charset; requests would guess latin-1
                obj = _parse_sse(r.content.decode("utf-8", errors="replace"))
                if "error" in obj:
                    raise WisburgError(f"{method}: {obj['error']}")
                return obj.get("result")
            except Exception as e:  # noqa: BLE001 - retry any transport failure
                last = e
                time.sleep(1.5 * (attempt + 1))
        raise WisburgError(f"{method} failed after {retries} attempts: {last}")

    def initialize(self) -> dict:
        return self._rpc("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "ideagen40", "version": "1.0"},
        })

    def tools(self) -> list[str]:
        if self._tools is None:
            res = self._rpc("tools/list")
            self._tools = [t["name"] for t in (res or {}).get("tools", [])]
        return self._tools

    def call(self, tool: str, args: dict) -> Any:
        res = self._rpc("tools/call", {"name": tool, "arguments": args})
        return _unwrap_content(res)

    # -------------------------------------------------------- paged listing
    def list_line(self, line: str, start: date, end: date, limit: int | None = None,
                  max_pages: int = 10) -> list[Item]:
        spec = config.SOURCE_LINES[line]
        limit = limit or config.INGEST_LIMITS.get(line, 50)
        out: list[Item] = []
        seen: set[str] = set()
        cursor: str | None = None
        start_s, end_s = start.isoformat(), end.isoformat()
        for _ in range(max_pages):
            # Bare `YYYY-MM-DD` values are mis-parsed upstream and silently
            # return an empty page for narrow ranges; a full ISO-8601 stamp with
            # an explicit +08:00 offset filters correctly. Verified against
            # server 0.7.0 on 2026-08-07.
            args: dict[str, Any] = {
                "first": min(limit, 100),
                "startTime": f"{start.isoformat()}T00:00:00+08:00",
                "endTime": f"{(end + timedelta(days=1)).isoformat()}T00:00:00+08:00",
            }
            if cursor:
                args["after"] = cursor
            data = self.call(spec["tool"], args)
            nodes, cursor, _has_next = _extract_page(data)
            if not nodes:
                break
            page_dates: list[str] = []
            fresh = 0
            for n in nodes:
                it = _normalise(line, spec, n)
                if it is None or it.doc_id in seen:
                    continue
                seen.add(it.doc_id)
                out.append(it)
                fresh += 1
                if it.published_d:
                    page_dates.append(it.published_d)
            # Server-side time filters are advisory; stop once a whole page has
            # aged past the window, or the page returned nothing new.
            if not cursor or fresh == 0:
                break
            if page_dates and max(page_dates) < start_s:
                break
            if len(out) >= limit * max_pages:
                break
        return [i for i in out if not i.published_d or i.published_d <= end_s]

    def detail(self, source_id: int, category: str) -> dict:
        return self.call("get-report-detail", {"id": int(source_id), "category": category}) or {}

    def article_detail(self, article_id: int) -> dict:
        return self.call("get-article-detail", {"id": int(article_id)}) or {}


# ---------------------------------------------------------------- parsing
_SSE_FIELD = ("event:", "id:", "retry:", ":")
_DEC = json.JSONDecoder(strict=False)


def _parse_sse(text: str) -> dict:
    """Wisburg answers with text/event-stream framing even for unary calls.

    Two quirks make the naive parse wrong:
      * the server does not declare charset, so callers must decode utf-8 first;
      * JSON payloads contain raw newlines inside string values, so a payload
        spans several physical lines and `data:`-line splitting truncates it.
    We therefore strip SSE field prefixes, rejoin, and decode non-strictly.
    """
    text = text.strip()
    if not text:
        raise WisburgError("empty response")
    if text.startswith("{"):
        return _DEC.decode(text)

    kept: list[str] = []
    for ln in text.split("\n"):
        if ln.startswith("data: "):
            kept.append(ln[6:])
        elif ln.startswith(_SSE_FIELD) and not ln.startswith("data:"):
            kept.append("\x00")           # event boundary marker
        else:
            kept.append(ln)
    joined = "\n".join(kept)

    # Decode every JSON object in the stream; the last one is the RPC reply.
    last: dict | None = None
    for chunk in joined.split("\x00"):
        chunk = chunk.strip()
        while chunk.startswith("{"):
            try:
                obj, end = _DEC.raw_decode(chunk)
            except ValueError:
                break
            if isinstance(obj, dict):
                last = obj
            chunk = chunk[end:].lstrip()
    if last is None:
        raise WisburgError(f"unparseable response: {text[:300]}")
    return last


def _unwrap_content(res: Any) -> Any:
    """tools/call returns {content:[{type:'text',text:'<json or md>'}], ...}."""
    if res is None:
        return None
    if isinstance(res, dict) and "structuredContent" in res:
        return res["structuredContent"]
    if isinstance(res, dict) and "content" in res:
        chunks = []
        for c in res["content"]:
            if c.get("type") == "text":
                chunks.append(c.get("text", ""))
        joined = "\n".join(chunks).strip()
        if joined.startswith("{") or joined.startswith("["):
            try:
                return json.loads(joined)
            except ValueError:
                pass
        return joined
    return res


def _extract_page(data: Any) -> tuple[list[dict], str | None, bool]:
    """Normalise the several shapes Wisburg uses for a paged list.

    In practice (server 0.7.0) every list tool answers with a text block:

        Found 20 reports:

        [99610] 全球AI趋势追踪：中国SuperPod发展专家电话会议纪要
          date: 2026-08-06T23:36:29+08:00
          <optional indented summary, may span paragraphs>

        --- Page Info ---
        Next cursor: 2

    The JSON branches below are kept so a future server revision that returns
    GraphQL-shaped pages keeps working without a code change.
    """
    if data is None:
        return [], None, False
    if isinstance(data, str):
        return _parse_text_page(data)
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)], None, False
    if not isinstance(data, dict):
        return [], None, False

    for key in ("edges", "nodes", "items", "data", "list", "results", "records"):
        v = data.get(key)
        if isinstance(v, list):
            nodes = [(e.get("node") if isinstance(e, dict) and "node" in e else e) for e in v]
            nodes = [n for n in nodes if isinstance(n, dict)]
            page = data.get("pageInfo") or {}
            return (nodes,
                    page.get("endCursor") or data.get("endCursor") or data.get("after"),
                    bool(page.get("hasNextPage", False)))
    # single nested container, e.g. {"marketDailies": {...}}
    for v in data.values():
        if isinstance(v, dict) and any(k in v for k in ("edges", "nodes", "items", "data")):
            return _extract_page(v)
    return [], None, False


_HEAD = re.compile(r"^\[(?P<id>\d+)\]\s*(?P<title>.+?)\s*$")
_DATE_LN = re.compile(r"^\s+date:\s*(?P<ts>\S+)", re.I)
_CURSOR = re.compile(r"Next cursor:\s*(?P<cur>\S+)", re.I)
_PAGE_SEP = "--- Page Info ---"


def _parse_text_page(text: str) -> tuple[list[dict], str | None, bool]:
    body, _, tail = text.partition(_PAGE_SEP)
    cm = _CURSOR.search(tail)
    cursor = cm.group("cur") if cm else None

    nodes: list[dict] = []
    cur: dict | None = None
    for raw in body.splitlines():
        m = _HEAD.match(raw)
        if m:
            if cur:
                nodes.append(cur)
            cur = {"id": int(m.group("id")), "title": m.group("title").strip(),
                   "publishedAt": None, "summary": ""}
            continue
        if cur is None:
            continue
        dm = _DATE_LN.match(raw)
        if dm:
            cur["publishedAt"] = dm.group("ts")
            continue
        if raw.startswith((" ", "\t")):
            cur["summary"] = (cur["summary"] + "\n" + raw.strip()).strip()
    if cur:
        nodes.append(cur)
    return nodes, cursor, bool(cursor and nodes)


# The detail tool answers with a full markdown document. These headings are
# stable across categories and carry the fields v0.4 actually scores on.
_DETAIL_SECTIONS = {
    "views": ("主要观点",),
    "facts": ("事实依据",),
    "narrative": ("陈述总结",),
    "data": ("关键数据",),
    "assets": ("推荐资产标的", "相关资产", "投资标的"),
    "terms": ("专业名词及重要事件", "专业名词"),
}


def parse_detail(md: str) -> dict:
    """Split a get-report-detail markdown document into its scoreable sections."""
    if not isinstance(md, str) or not md.strip():
        return {}
    out: dict[str, Any] = {"raw_chars": len(md)}
    m = re.search(r"^#\s*(.+)$", md, re.M)
    if m:
        out["title"] = m.group(1).strip()
    m = re.search(r"^-\s*ID:\s*(\d+)", md, re.M)
    if m:
        out["source_id"] = int(m.group(1))
    m = re.search(r"^-\s*Date:\s*(\S+)", md, re.M)
    if m:
        out["published_at"] = m.group(1)

    blocks = re.split(r"^#{2,4}\s*", md, flags=re.M)
    for blk in blocks[1:]:
        head, _, rest = blk.partition("\n")
        head = head.strip()
        for key, names in _DETAIL_SECTIONS.items():
            if any(head.startswith(n) for n in names):
                out[key] = rest.strip()
    # count enumerated items in the two sections N depends on
    out["n_views"] = len(re.findall(r"^\s*\d+\.\s", out.get("views", ""), re.M))
    out["n_facts"] = len(re.findall(r"^\s*\d+\.\s", out.get("facts", ""), re.M))
    out["n_data"] = len(re.findall(r"^\s*[*\-]\s+\*\*", out.get("data", ""), re.M))
    return out


_KEYS = {
    "id": ("id", "reportId", "articleId", "feedId", "nodeId", "pk"),
    "title": ("title", "name", "headline", "subject"),
    "published": ("publishedAt", "published_at", "datetime", "date", "createdAt",
                  "created_at", "publishTime", "publish_time", "time", "updatedAt"),
    "url": ("url", "link", "reportUrl", "sourceUrl", "originalUrl", "detailUrl"),
    "summary": ("summary", "description", "abstract", "digest", "excerpt", "brief", "desc"),
    "body": ("content", "body", "markdown", "text", "html", "fullText", "detail"),
    "institution": ("institution", "org", "source", "author", "publisher", "broker",
                    "company", "provider"),
}


def _pick(node: dict, group: str) -> Any:
    for k in _KEYS[group]:
        if k in node and node[k] not in (None, "", [], {}):
            return node[k]
    return None


def _normalise(line: str, spec: dict, node: dict) -> Item | None:
    title = _pick(node, "title")
    body = _pick(node, "body") or ""
    summary = _pick(node, "summary") or ""
    if not title and not summary and not body:
        return None
    if isinstance(body, dict):
        body = json.dumps(body, ensure_ascii=False)
    body = _strip_html(str(body))
    summary = _strip_html(str(summary or ""))[:2000]
    sid = _pick(node, "id")
    try:
        sid = int(sid) if sid is not None else None
    except (TypeError, ValueError):
        sid = None
    return Item(
        line=line,
        category=spec.get("category"),
        tier=spec["tier"],
        source_id=sid,
        title=_strip_html(str(title or summary[:80])),
        published_at=_norm_ts(_pick(node, "published")),
        url=_pick(node, "url"),
        summary=summary,
        body=body[:60_000],
        institution=(str(_pick(node, "institution"))[:120]
                     if _pick(node, "institution") else None),
        meta={k: v for k, v in node.items()
              if k not in ("content", "body", "html", "markdown", "fullText")
              and isinstance(v, (str, int, float, bool, type(None)))},
    )


_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t　]+")


def _strip_html(s: str) -> str:
    s = _TAG.sub(" ", s)
    s = (s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
          .replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'"))
    return _WS.sub(" ", s).strip()


def _norm_ts(v: Any) -> str | None:
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
        n = float(v)
        if n > 1e11:
            n /= 1000.0
        try:
            return datetime.fromtimestamp(n, tz=config.TZ).isoformat()
        except (OSError, ValueError, OverflowError):
            return None
    s = str(v).strip().replace("/", "-")
    s = re.sub(r"Z$", "+00:00", s)
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s[:len("2026-01-01T00:00:00.000000+00:00")], fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=config.TZ)
            return dt.astimezone(config.TZ).isoformat()
        except ValueError:
            continue
    m = re.search(r"(20\d{2}-\d{1,2}-\d{1,2})", s)
    return f"{m.group(1)}T00:00:00+08:00" if m else None


def _to_hkt_date(ts: str | None) -> str | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts).astimezone(config.TZ).date().isoformat()
    except ValueError:
        return ts[:10] if len(ts) >= 10 else None


# ---------------------------------------------------------------- ingest
def ingest(con, as_of: date, lookback_days: int = config.OBSERVATION_WINDOW_DAYS,
           lines: Iterable[str] | None = None, fetch_bodies: int = 0,
           verbose: bool = True) -> dict:
    """Pull every source line for [as_of-lookback+1, as_of] and upsert documents.

    `fetch_bodies` optionally deep-fetches the N most recent Tier-1/2 items per
    line via get-report-detail, so N's causal-depth scoring has real text.
    """
    w = Wisburg()
    w.initialize()
    start = as_of - timedelta(days=lookback_days - 1)
    lines = list(lines or config.SOURCE_LINES)
    report: dict[str, Any] = {"as_of": as_of.isoformat(), "start": start.isoformat(),
                              "lines": {}, "total": 0, "new": 0, "errors": {}}
    now = config.now_hkt().isoformat()

    window = [start + timedelta(days=k) for k in range((as_of - start).days + 1)]

    for line in lines:
        # Paginate each calendar day separately. Pulling the whole window in one
        # cursor walk truncates the *oldest* day first, which biases A (三日升温)
        # upward — the factor would read a pagination artefact as warming.
        items: list[Item] = []
        per_day: dict[str, int] = {}
        failed = False
        for d in window:
            try:
                got = w.list_line(line, d, d, max_pages=12)
            except Exception as e:  # noqa: BLE001 - one bad day must not kill the line
                report["errors"][f"{line}@{d}"] = str(e)[:200]
                failed = True
                continue
            items.extend(got)
            per_day[d.isoformat()] = len(got)
        if failed and not items:
            if verbose:
                print(f"  ! {line:<14} all days failed")
            continue

        # keep only items inside the window (upstream filters are advisory)
        seen_ids: set[str] = set()
        keep = []
        for i in items:
            if not i.published_d or not (start.isoformat() <= i.published_d <= as_of.isoformat()):
                continue
            if i.doc_id in seen_ids:
                continue
            seen_ids.add(i.doc_id)
            keep.append(i)

        # Deep-fetch the full markdown for the lines that feed N's causal depth.
        # Only Tier 1/2 gets this treatment: those are the documents allowed to
        # establish a new fact, and the ones whose sectioned body we score.
        if fetch_bodies and config.SOURCE_LINES[line]["tier"] in config.FACT_TIERS:
            for it in keep[:fetch_bodies]:
                if it.source_id is None or it.body_is_deep:
                    continue
                try:
                    md = (w.article_detail(it.source_id) if line == "articles"
                          else w.detail(it.source_id, it.category))
                    md = md if isinstance(md, str) else json.dumps(md, ensure_ascii=False)
                    if len(md) > len(it.body):
                        it.body = _strip_html(md)[:60_000]
                        sec = parse_detail(md)
                        it.meta = {**it.meta, **{f"sec_{k}": v for k, v in sec.items()
                                                 if isinstance(v, (int, float, str))
                                                 and k.startswith("n_")}}
                        it.sections = sec
                except Exception as e:  # noqa: BLE001 - detail is best-effort
                    report.setdefault("detail_errors", {})[it.doc_id] = str(e)[:120]

        before = con.execute("SELECT COUNT(*) c FROM documents").fetchone()["c"]
        rows = [{
            "doc_id": i.doc_id, "line": i.line, "category": i.category,
            "source_id": i.source_id, "tier": i.tier, "title": i.title,
            "institution": i.institution, "published_at": i.published_at,
            "published_d": i.published_d, "ingested_at": now, "url": i.url,
            "summary": i.summary, "body": i.body, "body_chars": len(i.body),
            "content_hash": i.content_hash,
            "meta": {**i.meta, **({"sections": i.sections} if i.sections else {})},
        } for i in keep]
        db.upsert_many(con, "documents", rows, ["doc_id"])
        after = con.execute("SELECT COUNT(*) c FROM documents").fetchone()["c"]

        report["lines"][line] = {"fetched": len(items), "in_window": len(keep),
                                 "new": after - before, "per_day": per_day,
                                 "tier": config.SOURCE_LINES[line]["tier"]}
        report["total"] += len(keep)
        report["new"] += after - before
        if verbose:
            days = " ".join(f"{k[5:]}:{v}" for k, v in sorted(per_day.items()))
            print(f"  ✓ {line:<14} tier{config.SOURCE_LINES[line]['tier']} "
                  f"window={len(keep):<4} new={after-before:<4} [{days}]")

    db.kv_set(con, f"ingest:{as_of.isoformat()}", report)
    return report
