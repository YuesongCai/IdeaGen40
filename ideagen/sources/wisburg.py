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

_UA = "IdeaGen40/1.0"


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
    assets: list[str] | None = None
    retrieval: str | None = None
    body_sha256: str | None = None       # sha256 of the verbatim detail markdown
    raw_uri: str | None = None           # where those exact bytes were archived

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
        if not self.url:
            raise WisburgError("WISBURG_MCP_URL is not configured")
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
        # the chart library answers in its own shape
        if "\n  image: " in data or data.lstrip().startswith("title:"):
            return parse_images_page(data)
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


_IMG_TITLE = re.compile(r"^title:\s*(?P<t>.+?)\s*$")
_IMG_URL = re.compile(r"^\s+image:\s*(?P<u>https?://\S+)")


def parse_images_page(text: str) -> tuple[list[dict], str | None, bool]:
    """`list-images` uses its own shape:

        title: 莱茵河水位创历史新低
          date: 2026-08-06T20:04:25+08:00
          image: https://<asset-host>/<hash>.jpg
          <the platform's written interpretation of the chart>

    There is no numeric id, so the image URL hash is the stable identity.
    """
    body, _, tail = text.partition(_PAGE_SEP)
    cm = _CURSOR.search(tail)
    cursor = cm.group("cur") if cm else None
    nodes: list[dict] = []
    cur: dict | None = None
    for raw in body.splitlines():
        m = _IMG_TITLE.match(raw)
        if m:
            if cur:
                nodes.append(cur)
            cur = {"title": m.group("t").strip(), "publishedAt": None,
                   "summary": "", "image": None}
            continue
        if cur is None:
            continue
        mu = _IMG_URL.match(raw)
        if mu:
            cur["image"] = mu.group("u")
            continue
        dm = _DATE_LN.match(raw)
        if dm:
            cur["publishedAt"] = dm.group("ts")
            continue
        if raw.startswith((" ", "\t")):
            cur["summary"] = (cur["summary"] + "\n" + raw.strip()).strip()
    if cur:
        nodes.append(cur)
    for n in nodes:
        if n.get("image"):
            n["id"] = int(hashlib.sha1(n["image"].encode()).hexdigest()[:12], 16)
    return [n for n in nodes if n.get("image")], cursor, bool(cursor and nodes)


# Asset URLs embedded in report bodies and chart items. These are the only
# externally verifiable references the corpus exposes.
_ASSET_URL = re.compile(r"https?://[A-Za-z0-9.\-]+/[^\s)\]\"'，。；、]+"
                        r"\.(?:jpg|jpeg|png|gif|webp|svg)", re.I)


def extract_assets(text: str) -> list[str]:
    from .. import config as _cfg

    out: list[str] = []
    for u in _ASSET_URL.findall(text or ""):
        host = u.split("/", 3)[2] if "://" in u else ""
        if host in _cfg.ASSET_HOSTS and u not in out:
            out.append(u)
    return out


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
    from .. import lexicon as _lex

    blob = " ".join(filter(None, (str(title or ""), summary, body[:1500])))
    assets = extract_assets(blob)
    if node.get("image"):
        assets = [node["image"], *[a for a in assets if a != node["image"]]]

    # A reproducible retrieval receipt. The platform is a client-rendered SPA with
    # no per-document canonical web URL — probing /article/<id>, /report/<id> and
    # friends all return the same 200 shell — so a guessed permalink would be a
    # citation that looks authoritative and resolves to nothing. The honest handle
    # is the API call that returns the document, recorded verbatim.
    cat = spec.get("category")
    if sid is None:
        receipt = f"{spec['tool']}(...)  # no numeric id; identity = content_hash"
    elif line == "articles":
        receipt = f"get-article-detail(id={sid})"
    elif line == "images":
        receipt = f"list-images(query=...)  # identity = image URL"
    elif cat:
        receipt = f"get-report-detail(id={sid}, category=\"{cat}\")"
    else:
        receipt = f"{spec['tool']}(...)  id={sid}"

    return Item(
        line=line,
        category=cat,
        tier=spec["tier"],
        source_id=sid,
        title=_strip_html(str(title or summary[:80])),
        published_at=_norm_ts(_pick(node, "published")),
        url=_pick(node, "url") or (assets[0] if line == "images" else None),
        summary=summary,
        body=body[:60_000],
        institution=(str(_pick(node, "institution"))[:120]
                     if _pick(node, "institution") else _lex.institution_of(blob)),
        meta={k: v for k, v in node.items()
              if k not in ("content", "body", "html", "markdown", "fullText")
              and isinstance(v, (str, int, float, bool, type(None)))},
        assets=assets,
        retrieval=receipt,
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


# ---------------------------------------------------------------- archive
# LICENSE RULE: the verbatim bodies archived below are licensed material. They
# live in the private artifact store (TOS in production, the local artifact root
# otherwise) and are never republished into public artifacts — reports quote and
# cite, the archive proves.

def _ensure_doc_columns(con) -> None:
    """Additive-only evolution of `documents` on the legacy sqlite path.

    The declarations live in `schema.ADD_COLUMNS` so the state-port `migrate`
    and this path cannot drift apart; this exists because the CLI ingest opens
    its connection through `db.init()`, which only runs CREATE TABLE and cannot
    add a column to a table that already shipped.
    """
    from .. import schema as _schema

    have = {r["name"] for r in con.execute("PRAGMA table_info(documents)")}
    for table, col, decl in _schema.ADD_COLUMNS:
        if table == "documents" and col not in have:
            con.execute(f"ALTER TABLE documents ADD COLUMN {col} {decl}")


def archive_raw(blobs, line: str, source_id, md: str,
                errors: dict | None = None) -> tuple[str, str | None]:
    """Write the verbatim detail markdown to the blob store, content-addressed.

    Content addressing turns provenance from "a receipt and a weak hash" into
    "the exact bytes, refetchable and provable": the sha in the key commits the
    store to the content, and the store is the same immutable one the rest of
    the system already trusts. If the vendor later edits a document, the new
    bytes hash to a new key and both versions survive — which is the point.

    Returns (sha256, uri). A put that fails still returns the sha, because the
    hash is a property of the bytes we hold, not of the store's availability.
    """
    data = md.encode("utf-8")
    sha = hashlib.sha256(data).hexdigest()
    key = f"corpus/raw/{line}/{source_id}_{sha[:12]}.md"
    try:
        if not blobs.exists(key):
            blobs.put(key, data, content_type="text/markdown; charset=utf-8")
        return sha, blobs.uri(key)
    except Exception as e:  # noqa: BLE001 - includes the already-exists race
        # `put` refuses overwrites by design. Same sha ⇒ same bytes, so losing
        # that race *is* the idempotent path; anything else is recorded and the
        # ingest carries on — a full archive must never block the corpus.
        try:
            if blobs.exists(key):
                return sha, blobs.uri(key)
        except Exception:  # noqa: BLE001
            pass
        if errors is not None:
            errors[key] = f"{type(e).__name__}: {e}"[:120]
        return sha, None


def _default_blobs():
    """The platform blob store, best-effort. Callers that already hold a
    Platform pass its blobs in; the CLI paths predate the ports and get the
    archive for free through this — a missing platform degrades to no archive,
    never to a failed ingest."""
    try:
        from .. import platform as plat
        return plat.load().blobs
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- tool drift
#: Tools this module calls by name beyond the per-line list tools.
_DETAIL_TOOLS = ("get-report-detail", "get-article-detail")


def tool_drift(w: Wisburg) -> dict | None:
    """Diff the server's tools/list against what this module hardcodes.

    The vendor adds and removes tools without notice, and a silently missing
    list tool becomes zero rows that validate cleanly — indistinguishable from
    a quiet week. Unknown new tools are worth a look too: they are usually a
    new source line the corpus is not yet reading.
    """
    expected = ({spec["tool"] for spec in config.SOURCE_LINES.values()}
                | set(_DETAIL_TOOLS))
    try:
        have = set(w.tools())
    except Exception as e:  # noqa: BLE001 - drift detection must not fail ingest
        return {"error": f"{type(e).__name__}: {e}"[:200]}
    missing = sorted(expected - have)
    unknown = sorted(have - expected)
    if not missing and not unknown:
        return None
    return {"missing": missing, "unknown": unknown}


# ---------------------------------------------------------------- shortlist
#: Deep-fetch shortlist weights. Tier dominates: a Tier-1 primary document is
#: worth more full text than any curation item. Theme hits buy their way up
#: because the scorers only read what the lexicon can see. The duplicate
#: penalty exceeds the tier gap so the third rewrite of one story loses to
#: fresh coverage of anything.
TIER_WEIGHT = {1: 4.0, 2: 2.0, 3: 0.0}
THEME_HIT_WEIGHT = 1.5
DUP_PENALTY = 5.0


def shortlist(items: list, budget: int, themes: tuple,
              known_sigs: frozenset | set = frozenset()) -> list:
    """Pick which docs get the deep-fetch budget, by value rather than arrival.

    Recency-only selection spent the budget on whatever came last, including
    five rewrites of the same story; this shortlist buys coverage of what the
    scorers will actually read. `known_sigs` carries title signatures already
    deep-fetched, so a story does not get the budget twice across runs.

    Pure function over objects with .tier/.title/.published_at — the same
    Item shape both ingest paths hold.
    """
    from .. import lexicon as _lex

    if budget <= 0 or not items:
        return []
    # Score in recency order so, within a duplicate cluster, the newest copy is
    # the one that escapes the penalty; the final sort is stable, which makes
    # recency the tie-break for equal scores.
    ordered = sorted(items, key=lambda it: it.published_at or "", reverse=True)
    seen: dict[str, int] = {}
    scored: list[tuple[float, Any]] = []
    for it in ordered:
        sig = _lex.title_signature(it.title or "")
        dups = seen.get(sig, 0) + (1 if sig in known_sigs else 0)
        seen[sig] = seen.get(sig, 0) + 1
        s = TIER_WEIGHT.get(it.tier, 0.0)
        s += THEME_HIT_WEIGHT * sum(1 for t in themes
                                    if _lex.match_theme(it.title or "", t))
        s -= DUP_PENALTY * dups
        scored.append((s, it))
    scored.sort(key=lambda p: p[0], reverse=True)
    return [it for _, it in scored[:budget]]


def _deep_sigs(con, line: str, start_d: str) -> set[str]:
    """Title signatures of docs this line already holds in full, so the
    shortlist does not spend budget re-fetching a story it can already read."""
    from .. import lexicon as _lex

    rows = db.q(con, "SELECT title FROM documents WHERE line=? AND published_d>=? "
                     "AND (body_chars>1500 OR body_sha256 IS NOT NULL)",
                (line, start_d))
    return {_lex.title_signature(r["title"] or "") for r in rows}


def _deep_fetch(w: Wisburg, it: Item, report: dict, blobs) -> bool:
    """Merge the full detail document into `it`, archiving the raw bytes first.

    The archive happens BEFORE any stripping: `_strip_html` and the 60k
    truncation are lossy, and the whole value of the archive is that it holds
    what the vendor served, not what the pipeline kept.
    """
    try:
        md = (w.article_detail(it.source_id) if it.line == "articles"
              else w.detail(it.source_id, it.category))
        md = md if isinstance(md, str) else json.dumps(md, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001 - detail is best-effort
        report.setdefault("detail_errors", {})[it.doc_id] = str(e)[:120]
        return False
    if not md.strip():
        return False
    if blobs is not None:
        errs: dict = {}
        it.body_sha256, it.raw_uri = archive_raw(blobs, it.line, it.source_id,
                                                 md, errs)
        if errs:
            report.setdefault("archive_errors", {}).update(errs)
    if len(md) > len(it.body):
        it.body = _strip_html(md)[:60_000]
        sec = parse_detail(md)
        it.meta = {**it.meta, **{f"sec_{k}": v for k, v in sec.items()
                                 if isinstance(v, (int, float, str))
                                 and k.startswith("n_")}}
        it.sections = sec
        found = extract_assets(md)
        it.assets = list(dict.fromkeys([*(it.assets or []), *found]))
    return True


# ---------------------------------------------------------------- rows
def _doc_row(i: Item, now: str) -> dict:
    """One `documents` row. `body_sha256`/`raw_uri` are deliberately absent:
    the upsert sets every listed column, so including them as NULL on a shallow
    re-ingest would erase an archive reference written by an earlier deep
    fetch. They are applied by `_apply_archive_refs` instead."""
    return {
        "doc_id": i.doc_id, "line": i.line, "category": i.category,
        "source_id": i.source_id, "tier": i.tier, "title": i.title,
        "institution": i.institution, "published_at": i.published_at,
        "published_d": i.published_d, "ingested_at": now, "url": i.url,
        "summary": i.summary, "body": i.body, "body_chars": len(i.body),
        "content_hash": i.content_hash, "retrieval": i.retrieval,
        "meta": {**i.meta, **({"sections": i.sections} if i.sections else {}),
                 **({"n_assets": len(i.assets)} if i.assets else {})},
    }


def _asset_rows(items: list[Item]) -> list[dict]:
    rows = []
    for i in items:
        for u in (i.assets or []):
            rows.append({
                "asset_id": hashlib.sha1(u.encode()).hexdigest(),
                "doc_id": i.doc_id, "url": u,
                "kind": "chart" if i.line == "images" else "figure",
                "host": u.split("/", 3)[2] if "://" in u else None,
                "caption": (i.summary or "")[:2000] if i.line == "images" else None,
                "title": i.title, "published_d": i.published_d,
            })
    return rows


def _apply_archive_refs(con, items: list[Item]) -> None:
    for i in items:
        if i.body_sha256:
            con.execute("UPDATE documents SET body_sha256=?, raw_uri=? "
                        "WHERE doc_id=?", (i.body_sha256, i.raw_uri, i.doc_id))


# ---------------------------------------------------------------- ingest
def ingest(con, as_of: date, lookback_days: int = config.OBSERVATION_WINDOW_DAYS,
           lines: Iterable[str] | None = None, fetch_bodies: int = 0,
           verbose: bool = True, blobs: Any = None, events: Any = None) -> dict:
    """Pull every source line for [as_of-lookback+1, as_of] and upsert documents.

    `fetch_bodies` optionally deep-fetches N Tier-1/2 items per line via
    get-report-detail — chosen by `shortlist`, not by recency — so N's
    causal-depth scoring has real text. `blobs`/`events` are platform ports;
    when absent they are resolved best-effort so the CLI paths need no change.
    """
    w = Wisburg()
    w.initialize()
    _ensure_doc_columns(con)
    if blobs is None:
        blobs = _default_blobs()
    start = as_of - timedelta(days=lookback_days - 1)
    lines = list(lines or config.SOURCE_LINES)
    report: dict[str, Any] = {"as_of": as_of.isoformat(), "start": start.isoformat(),
                              "lines": {}, "total": 0, "new": 0, "errors": {}}
    now = config.now_hkt().isoformat()

    drift = tool_drift(w)
    if drift:
        report["tool_drift"] = drift
        if verbose:
            print(f"  ⚠ 工具漂移：缺失 {drift.get('missing') or '-'} / "
                  f"新增 {drift.get('unknown') or '-'}")
        if events is not None:
            try:
                events.publish("ingest.tool_drift",
                               {"as_of": as_of.isoformat(), **drift})
            except Exception:  # noqa: BLE001 - a drift alert must not fail ingest
                pass

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
        # establish a new fact, and the ones whose sectioned body we score. The
        # budget is unchanged; what changed is who gets it — see `shortlist`.
        if fetch_bodies and config.SOURCE_LINES[line]["tier"] in config.FACT_TIERS:
            cand = [i for i in keep if i.source_id is not None and not i.body_is_deep]
            from .. import lexicon as _lex
            chosen = shortlist(cand, fetch_bodies, _lex.all_themes(as_of),
                               known_sigs=_deep_sigs(con, line, start.isoformat()))
            for it in chosen:
                _deep_fetch(w, it, report, blobs)

        before = con.execute("SELECT COUNT(*) c FROM documents").fetchone()["c"]
        db.upsert_many(con, "documents", [_doc_row(i, now) for i in keep], ["doc_id"])
        _apply_archive_refs(con, keep)

        asset_rows = _asset_rows(keep)
        if asset_rows:
            db.upsert_many(con, "assets", asset_rows, ["asset_id"])
            report["assets"] = report.get("assets", 0) + len(asset_rows)
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


def ingest_incremental(con, *, budget_details: int = 3,
                       lines: Iterable[str] | None = None,
                       blobs: Any = None, verbose: bool = False) -> dict:
    """First-page-only pull of every source line, for the monitor cadence.

    The economics are the design: one page (~20 items) per line and a small
    global deep-fetch budget keep a pass cheap enough to run a hundred times a
    day, which is what turns the corpus from a weekly snapshot into a rolling
    one. Completeness is deliberately NOT this function's job — the daily
    full-window `ingest` remains the backstop that paginates the whole window,
    so anything a first page misses costs freshness, never coverage.

    Per-line cursors (last seen source_id / published_at) live in the kv store
    under `ingest:cursor:{line}`. The source_id prefilter assumes the vendor's
    ids are monotonic per line; if that assumption ever breaks, the DB existence
    check below still prevents duplicates and the daily ingest heals any skip.
    """
    _ensure_doc_columns(con)
    if blobs is None:
        blobs = _default_blobs()
    w = Wisburg()
    w.initialize()
    today = config.now_hkt().date()
    start = today - timedelta(days=config.OBSERVATION_WINDOW_DAYS - 1)
    now = config.now_hkt().isoformat()
    report: dict[str, Any] = {"at": now, "new": 0, "deep": 0,
                              "lines": {}, "errors": {}}

    fresh_all: list[Item] = []
    cursors: dict[str, dict] = {}
    for line in (lines or config.SOURCE_LINES):
        try:
            items = w.list_line(line, start, today, limit=20, max_pages=1)
        except Exception as e:  # noqa: BLE001 - one dead line must not kill the pass
            report["errors"][line] = str(e)[:200]
            continue
        cur = db.kv_get(con, f"ingest:cursor:{line}") or {}
        last_sid = cur.get("source_id")
        fresh: list[Item] = []
        for it in items:
            if not it.published_d or not (start.isoformat() <= it.published_d
                                          <= today.isoformat()):
                continue
            # `images` ids are content-derived hashes, not server sequence
            # numbers, so the monotonic prefilter would skip new charts at
            # random there — the DB check is the only valid test for that line.
            if (line != "images" and last_sid is not None
                    and it.source_id is not None and it.source_id <= last_sid):
                continue
            if db.q1(con, "SELECT 1 FROM documents WHERE doc_id=?", (it.doc_id,)):
                continue
            fresh.append(it)
        fresh_all.extend(fresh)
        sids = [i.source_id for i in items if i.source_id is not None]
        stamps = [i.published_at for i in items if i.published_at]
        cursors[line] = {
            "source_id": max([*sids, *([last_sid] if last_sid else [])], default=None),
            "published_at": max([*stamps, *([cur.get("published_at")]
                                            if cur.get("published_at") else [])],
                                default=None),
            "checked_at": now,
        }
        report["lines"][line] = {"listed": len(items), "new": len(fresh)}

    # One global budget across lines: at this cadence the interesting question
    # is "what, of everything new this quarter-hour, deserves full text", not
    # "the newest per line" — the shortlist answers it with the same scorer the
    # daily ingest uses.
    cand = [i for i in fresh_all
            if config.SOURCE_LINES[i.line]["tier"] in config.FACT_TIERS
            and i.source_id is not None and not i.body_is_deep]
    from .. import lexicon as _lex
    known = set()
    for line in {i.line for i in cand}:
        known |= _deep_sigs(con, line, start.isoformat())
    for it in shortlist(cand, budget_details, _lex.all_themes(today),
                        known_sigs=known):
        if _deep_fetch(w, it, report, blobs):
            report["deep"] += 1

    if fresh_all:
        db.upsert_many(con, "documents", [_doc_row(i, now) for i in fresh_all],
                       ["doc_id"])
        _apply_archive_refs(con, fresh_all)
        asset_rows = _asset_rows(fresh_all)
        if asset_rows:
            db.upsert_many(con, "assets", asset_rows, ["asset_id"])
            report["assets"] = len(asset_rows)
    report["new"] = len(fresh_all)
    for line, cur in cursors.items():
        db.kv_set(con, f"ingest:cursor:{line}", cur)
    if verbose:
        print(f"  增量语料 +{report['new']} 条（深抓 {report['deep']}）"
              if report["new"] else "  增量语料 无新增")
    return report


# ---------------------------------------------------------------- provenance
def verify_assets(con, limit: int = 200, recheck_days: int = 14,
                  verbose: bool = True) -> dict:
    """HEAD every unverified asset URL so the citation trail is not aspirational.

    A source link that has never been fetched is a claim, not evidence. Results
    are recorded on the row (`reachable`, `bytes`, `content_type`, `checked_at`)
    and the dashboard only embeds assets that came back OK.
    """
    cutoff = (config.now_hkt() - timedelta(days=recheck_days)).isoformat()
    rows = db.q(con, "SELECT asset_id,url FROM assets "
                     "WHERE reachable IS NULL OR checked_at < ? LIMIT ?",
                (cutoff, limit))
    if not rows:
        return {"checked": 0, "ok": 0, "failed": 0}

    s = requests.Session()
    headers = {"User-Agent": _UA}
    if config.WISBURG_REFERER:
        headers["Referer"] = config.WISBURG_REFERER
    s.headers.update(headers)
    ok = bad = 0
    now = config.now_hkt().isoformat()
    out = []
    for r in rows:
        rec = {"asset_id": r["asset_id"], "checked_at": now,
               "reachable": 0, "bytes": None, "content_type": None}
        try:
            h = s.head(r["url"], timeout=15, allow_redirects=True)
            if h.status_code == 405 or (h.status_code == 200 and not h.headers.get("Content-Length")):
                h = s.get(r["url"], timeout=25, stream=True)
                h.close()
            if h.status_code == 200:
                rec["reachable"] = 1
                rec["bytes"] = int(h.headers.get("Content-Length") or 0) or None
                rec["content_type"] = (h.headers.get("Content-Type") or "")[:80]
                ok += 1
            else:
                bad += 1
        except Exception:  # noqa: BLE001 - an unreachable asset is a finding
            bad += 1
        out.append(rec)
    for rec in out:
        con.execute("UPDATE assets SET reachable=?, bytes=?, content_type=?, "
                    "checked_at=? WHERE asset_id=?",
                    (rec["reachable"], rec["bytes"], rec["content_type"],
                     rec["checked_at"], rec["asset_id"]))
    if verbose:
        print(f"  verified {len(out)} assets: {ok} reachable, {bad} unreachable")
    return {"checked": len(out), "ok": ok, "failed": bad}


def provenance(con, doc_id: str) -> dict | None:
    """The full chain for one document: what it is, how to re-fetch it, its assets."""
    r = db.q1(con, "SELECT * FROM documents WHERE doc_id=?", (doc_id,))
    if not r:
        return None
    spec = config.SOURCE_LINES.get(r["line"], {})
    return {
        "doc_id": r["doc_id"], "line": r["line"],
        "line_label": spec.get("label", r["line"]),
        "tier": r["tier"], "category": r["category"], "source_id": r["source_id"],
        "title": r["title"], "institution": r["institution"],
        "published_at": r["published_at"], "ingested_at": r["ingested_at"],
        "content_hash": r["content_hash"], "body_chars": r["body_chars"],
        "retrieval": r["retrieval"],
        "assets": [dict(a) for a in db.q(
            con, "SELECT url,kind,host,title,caption,reachable,bytes,content_type,"
                 "checked_at FROM assets WHERE doc_id=?", (doc_id,))],
    }


def source_audit(con) -> dict:
    """How well can the corpus actually be traced back? Reported, not assumed."""
    d = db.q1(con, "SELECT COUNT(*) n, SUM(retrieval IS NOT NULL) receipt, "
                   "SUM(institution IS NOT NULL) inst, "
                   "SUM(content_hash IS NOT NULL) hash, "
                   "SUM(published_at IS NOT NULL) ts, "
                   "SUM(body_chars>1000) deep FROM documents")
    a = db.q1(con, "SELECT COUNT(*) n, SUM(reachable=1) ok, SUM(reachable=0) bad, "
                   "SUM(reachable IS NULL) unchecked, COUNT(DISTINCT doc_id) docs "
                   "FROM assets")
    by_line = [dict(r) for r in db.q(
        con, "SELECT d.line, COUNT(DISTINCT d.doc_id) docs, "
             "SUM(d.retrieval IS NOT NULL) receipt, "
             "SUM(d.institution IS NOT NULL) inst, "
             "(SELECT COUNT(*) FROM assets a WHERE a.doc_id IN "
             " (SELECT doc_id FROM documents x WHERE x.line=d.line)) assets "
             "FROM documents d GROUP BY d.line ORDER BY docs DESC")]

    # Do the ideas' citations resolve? Three outcomes, kept apart on purpose:
    #   resolved  cites a doc_id that is in the corpus
    #   prose     a free-text attribution ("State Street / YFinance") — the
    #             historical pack's own convention, unverifiable by construction
    #   dangling  has doc_id shape but is not in the corpus — the only real defect
    ref_shape = re.compile(r"^(" + "|".join(map(re.escape, config.SOURCE_LINES)) + r"):")
    per_batch: dict[str, dict[str, int]] = {}
    for r in db.q(con, "SELECT batch_id, sources FROM ideas"):
        b = per_batch.setdefault(r["batch_id"],
                                 {"resolved": 0, "prose": 0, "dangling": 0})
        for sid in (db.jl(r["sources"], []) or []):
            sid = str(sid)
            if not ref_shape.match(sid):
                b["prose"] += 1
            elif db.q1(con, "SELECT 1 FROM documents WHERE doc_id=?", (sid,)):
                b["resolved"] += 1
            else:
                b["dangling"] += 1
    tot = {"resolved": 0, "prose": 0, "dangling": 0}
    for v in per_batch.values():
        for k in tot:
            tot[k] += v[k]
    ideas_no_src = db.q1(con, "SELECT COUNT(*) n FROM ideas "
                              "WHERE sources IS NULL OR sources IN ('[]','')")["n"]
    px = db.q1(con, "SELECT COUNT(*) n, SUM(src IS NOT NULL) src FROM prices")
    nav = db.q1(con, "SELECT COUNT(*) n, SUM(src IS NOT NULL) src FROM navs")

    return {
        "documents": dict(d), "assets": dict(a), "by_line": by_line,
        "citations": {**tot, "total": sum(tot.values()),
                      "by_batch": per_batch,
                      "ideas_without_source": ideas_no_src},
        "prices": dict(px), "navs": dict(nav),
        "note": ("Wisburg is a client-rendered SPA with no per-document canonical "
                 "web URL — /article/<id>, /report/<id> and friends all return the "
                 "same 200 shell — so no permalink is stored. Ground truth is the "
                 "reproducible API call in `retrieval` plus `content_hash`, and the "
                 "verified asset URLs in `assets`."),
    }
