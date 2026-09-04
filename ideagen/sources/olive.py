"""Olive (Nexus HK) product shelf.

Olive is reachable only over MCP, which means only an interactive/agent session
can talk to it — a bare cron process cannot. Since the daily run is driven by
Claude Code anyway (that is the generator), the split is:

    Claude Code session  ──MCP──>  Olive        (search shelf, read NAV)
             │
             └── writes a JSON snapshot ──> `ideagen olive-ingest`
                                              │
                                              └── instruments + navs tables

Two consequences worth stating plainly:

* Olive publishes a *latest* NAV, not a daily history (`get_fund_nav_chart`
  aggregates the series server-side before returning it). Snapshotting daily is
  therefore the only way to accumulate a genuine daily NAV series — which is
  exactly what a 30-day forward paper-trade does. Funds entering the book on day
  1 have a full daily NAV path by day 30; nothing is back-filled or interpolated.
* A fund position is marked with an explicit staleness count. If Olive did not
  publish, the mark is carried forward and flagged, never silently smoothed.
"""

from __future__ import annotations

import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit
from pathlib import Path
from typing import Any, Iterable

import requests

from .. import config, db
from .wisburg import _parse_sse, _unwrap_content

SNAPSHOT_DIR = config.SNAPSHOTS
MAX_NAV_STALE_DAYS = 10


class OliveMCPError(RuntimeError):
    pass


class OliveMCP:
    """Streamable-HTTP MCP client with Noah SSO OAuth token refresh."""

    def __init__(self, url: str | None = None, access_token: str | None = None,
                 refresh_token: str | None = None, client_id: str | None = None,
                 token_url: str | None = None, timeout: int = 90):
        credentials = config.olive_credentials()
        self.url = url or config.OLIVE_MCP_URL
        if not self.url:
            raise OliveMCPError("OLIVE_MCP_URL is not configured")
        self.access_token = (
            access_token if access_token is not None
            else credentials.get("access_token") or config.olive_access_token()
        )
        self.refresh_token = (refresh_token
                              if refresh_token is not None
                              else credentials.get("refresh_token", ""))
        self.client_id = (client_id
                          if client_id is not None
                          else credentials.get("client_id", ""))
        self.token_url = token_url or config.OLIVE_OAUTH_TOKEN_URL
        self.timeout = timeout
        self.session_id: str | None = None
        self.refreshed_tokens: dict[str, Any] | None = None
        self._id = 0
        self.s = requests.Session()
        self.s.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "IdeaGen40/1.0",
        })
        self._set_access_token(self.access_token)

    def _set_access_token(self, token: str) -> None:
        self.access_token = token
        self.s.headers["Authorization"] = f"Bearer {token}"

    def _refresh(self) -> None:
        if not self.refresh_token or not self.client_id:
            raise OliveMCPError(
                "Olive access token expired and no refresh token/client id is configured")
        if not self.token_url:
            raise OliveMCPError("OLIVE_OAUTH_TOKEN_URL is not configured")
        response = requests.post(
            self.token_url,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
                "client_id": self.client_id,
                "resource": self.url,
            },
            headers={"Accept": "application/json"},
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            raise OliveMCPError(
                f"OAuth refresh HTTP {response.status_code}: {response.text[:200]}")
        tokens = response.json()
        access = tokens.get("access_token")
        if not access:
            raise OliveMCPError("OAuth refresh response has no access_token")
        self.refresh_token = tokens.get("refresh_token") or self.refresh_token
        self.refreshed_tokens = tokens
        self._set_access_token(access)
        if config.olive_token_file() is not None:
            expires = int(tokens.get("expires_in") or 0)
            current = config.olive_credentials()
            config.store_olive_credentials({
                **current,
                "access_token": access,
                "refresh_token": self.refresh_token,
                "client_id": self.client_id,
                "expires_at": (
                    datetime.now(timezone.utc) + timedelta(seconds=expires)
                ).isoformat() if expires else current.get("expires_at", ""),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })

    def _post(self, payload: dict, *, notification: bool = False,
              retries: int = 3) -> Any:
        last: Exception | None = None
        refreshed = False
        for attempt in range(retries):
            try:
                headers = ({"Mcp-Session-Id": self.session_id}
                           if self.session_id else None)
                response = self.s.post(self.url, json=payload, headers=headers,
                                       timeout=self.timeout)
                if response.status_code == 401 and not refreshed:
                    self._refresh()
                    refreshed = True
                    continue
                if response.status_code == 429 or response.status_code >= 500:
                    last = OliveMCPError(
                        f"HTTP {response.status_code}: {response.text[:200]}")
                    if attempt + 1 < retries:
                        time.sleep(1.5 * (attempt + 1))
                        continue
                    raise last
                response.raise_for_status()
                session_id = response.headers.get("Mcp-Session-Id")
                if session_id:
                    self.session_id = session_id
                if notification or not response.content:
                    return None
                obj = _parse_sse(response.content.decode("utf-8",
                                                          errors="replace"))
                if "error" in obj:
                    raise OliveMCPError(f"{payload['method']}: {obj['error']}")
                return obj.get("result")
            except OliveMCPError:
                raise
            except Exception as exc:  # noqa: BLE001
                last = exc
                if attempt + 1 < retries:
                    time.sleep(1.5 * (attempt + 1))
        raise OliveMCPError(
            f"{payload.get('method')} failed after {retries} attempts: {last}")

    def _rpc(self, method: str, params: dict | None = None) -> Any:
        self._id += 1
        return self._post({
            "jsonrpc": "2.0",
            "id": self._id,
            "method": method,
            "params": params or {},
        })

    def initialize(self) -> dict:
        result = self._rpc("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "ideagen40", "version": "1.0"},
        })
        protocol = (result or {}).get("protocolVersion")
        if protocol:
            self.s.headers["MCP-Protocol-Version"] = str(protocol)
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"},
                   notification=True)
        return result or {}

    def tool_specs(self) -> list[dict]:
        result = self._rpc("tools/list")
        return [tool for tool in (result or {}).get("tools", [])
                if isinstance(tool, dict)]

    def tools(self) -> list[str]:
        return [str(tool.get("name")) for tool in self.tool_specs()
                if tool.get("name")]

    def call(self, tool: str, args: dict | None = None) -> Any:
        result = self._rpc("tools/call", {
            "name": tool,
            "arguments": args or {},
        })
        return _unwrap_content(result)


def discover_issuer(mcp_url: str | None = None, *, timeout: int = 20) -> str:
    """Find the authorization server for an MCP endpoint, given only its URL.

    Saves the operator from having to know the issuer separately: RFC 9728
    publishes it at /.well-known/oauth-protected-resource, and a server that
    does not serve that document still names it in the WWW-Authenticate header
    of the 401 it answers an unauthenticated request with.
    """
    url = (mcp_url or config.OLIVE_MCP_URL or "").strip()
    if not url:
        raise OliveMCPError("OLIVE_MCP_URL is not configured")
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.netloc:
        raise OliveMCPError(f"OLIVE_MCP_URL must be an https URL, got {url!r}")
    origin = f"{parts.scheme}://{parts.netloc}"

    # RFC 9728 allows the path-suffixed form as well as the bare well-known.
    candidates = [f"{origin}/.well-known/oauth-protected-resource"]
    if parts.path.strip("/"):
        candidates.insert(
            0, f"{origin}/.well-known/oauth-protected-resource/"
               f"{parts.path.strip('/')}")
    attempts: list[str] = []
    for candidate in candidates:
        try:
            response = requests.get(
                candidate, headers={"Accept": "application/json"},
                timeout=timeout)
        except requests.RequestException as exc:
            attempts.append(f"{candidate} -> {type(exc).__name__}")
            continue
        if response.status_code >= 400:
            attempts.append(f"{candidate} -> HTTP {response.status_code}")
            continue
        try:
            servers = response.json().get("authorization_servers") or []
        except ValueError:
            attempts.append(f"{candidate} -> not JSON")
            continue
        if servers and isinstance(servers[0], str):
            return servers[0].rstrip("/")
        attempts.append(f"{candidate} -> no authorization_servers")

    try:  # last resort: the challenge on an unauthenticated call
        challenge = requests.post(
            url, json={"jsonrpc": "2.0", "id": 0, "method": "tools/list",
                       "params": {}},
            headers={"Accept": "application/json, text/event-stream"},
            timeout=timeout,
        ).headers.get("WWW-Authenticate", "")
        found = re.search(r'resource_metadata="([^"]+)"', challenge)
        if found:
            meta = requests.get(found.group(1), timeout=timeout).json()
            servers = meta.get("authorization_servers") or []
            if servers:
                return str(servers[0]).rstrip("/")
        attempts.append(f"WWW-Authenticate -> {challenge[:80] or 'absent'}")
    except (requests.RequestException, ValueError) as exc:
        attempts.append(f"WWW-Authenticate -> {type(exc).__name__}")

    raise OliveMCPError(
        "could not discover the OAuth issuer; set OLIVE_OAUTH_ISSUER by hand. "
        "Tried: " + "; ".join(attempts))


def register_oauth_client(redirect_uri: str, *,
                          issuer: str = config.OLIVE_OAUTH_ISSUER,
                          timeout: int = 30) -> dict:
    """Register the local public PKCE client through OAuth DCR."""
    if not issuer:
        raise OliveMCPError("OLIVE_OAUTH_ISSUER is not configured")
    response = requests.post(
        f"{issuer.rstrip('/')}/api/oauth/register",
        json={
            "client_name": "IdeaGen40 MCP Client",
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": "tools:read tools:write",
        },
        headers={"Accept": "application/json"},
        timeout=timeout,
    )
    if response.status_code >= 400:
        raise OliveMCPError(
            f"OAuth registration HTTP {response.status_code}: "
            f"{response.text[:200]}")
    result = response.json()
    if not result.get("client_id"):
        raise OliveMCPError("OAuth registration response has no client_id")
    return result


def oauth_authorization(client_id: str, redirect_uri: str, *,
                        issuer: str = config.OLIVE_OAUTH_ISSUER,
                        resource_url: str = config.OLIVE_MCP_URL,
                        ) -> tuple[str, str, str]:
    """Return (authorization URL, PKCE verifier, state)."""
    import base64
    import hashlib
    import secrets
    from urllib.parse import urlencode

    if not issuer or not resource_url:
        raise OliveMCPError(
            "OLIVE_OAUTH_ISSUER and OLIVE_MCP_URL must be configured")
    verifier = secrets.token_urlsafe(64)[:86]
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(32)
    query = urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": "tools:read tools:write",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": resource_url,
    })
    return f"{issuer.rstrip('/')}/api/oauth/authorize?{query}", verifier, state


def exchange_oauth_code(code: str, verifier: str, client_id: str,
                        redirect_uri: str, *,
                        token_url: str = config.OLIVE_OAUTH_TOKEN_URL,
                        resource_url: str = config.OLIVE_MCP_URL,
                        timeout: int = 30) -> dict:
    if not token_url or not resource_url:
        raise OliveMCPError(
            "OLIVE_OAUTH_TOKEN_URL and OLIVE_MCP_URL must be configured")
    response = requests.post(
        token_url,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "resource": resource_url,
        },
        headers={"Accept": "application/json"},
        timeout=timeout,
    )
    if response.status_code >= 400:
        raise OliveMCPError(
            f"OAuth token HTTP {response.status_code}: {response.text[:200]}")
    tokens = response.json()
    if not tokens.get("access_token"):
        raise OliveMCPError("OAuth token response has no access_token")
    return tokens


# The shelf renamed every tool from get_fund_* to shelf_*. Both names are
# carried so a server on either side of the rename answers, and so snapshots
# captured before it still merge.
OLIVE_CATALOG_TOOLS = ("shelf_list", "list_funds")

OLIVE_DETAIL_TOOLS = (
    "shelf_detail",
    "shelf_summary",
    "shelf_performance",
    "shelf_portfolio",
)

_DETAIL_ALIASES = {
    "detail": ("shelf_detail", "get_fund_detail"),
    "summary": ("shelf_summary", "get_fund_summary"),
    "performance": ("shelf_performance", "get_fund_performance"),
    "portfolio": ("shelf_portfolio", "get_fund_portfolio"),
}


_CATALOG_FIELDS = (
    "productCode", "productName", "shortName", "marketType", "strategy",
    "series", "subscriptionStart", "subscriptionEnd", "channel", "bookingName",
)

_CATALOG_HEADERS = {
    "产品id": "productCode",
    "产品名称": "productName",
    "产品简称": "shortName",
    "市场类型": "marketType",
    "策略": "strategy",
    "系列": "series",
    "通道": "channel",
    "开始": "subscriptionStart",
    "认购开始": "subscriptionStart",
    "结束": "subscriptionEnd",
    "认购结束": "subscriptionEnd",
    "预约": "bookingName",
    "记账名称": "bookingName",
}

# Column order before the shelf inserted 产品简称 between name and market type.
_CATALOG_LEGACY_ORDER = (
    "productCode", "productName", "marketType", "strategy", "series",
    "subscriptionStart", "subscriptionEnd", "channel", "bookingName",
)


def _payload(value: Any) -> Any:
    """Peel Olive's ``{"result": "<json>"}`` envelope off a tool result.

    ``_unwrap_content`` has already turned the MCP text block into this dict,
    so what arrives is the envelope, the bare payload, or plain markdown.
    """
    for _ in range(3):
        if isinstance(value, dict) and set(value) == {"result"}:
            value = value["result"]
            continue
        if isinstance(value, str):
            text = value.strip()
            if text[:1] in "{[":
                try:
                    value = json.loads(text)
                    continue
                except ValueError:
                    return value
        break
    return value


def _catalog_markdown(raw: Any) -> str:
    payload = _payload(raw)
    if isinstance(payload, dict):
        payload = payload.get("markdown") or payload.get("content") or ""
    if not isinstance(payload, str) or "|" not in payload:
        raise OliveMCPError(
            "catalog tool returned "
            f"{type(payload).__name__} with no markdown table")
    return payload


def parse_catalog(markdown: str) -> list[dict]:
    """Parse the markdown table returned by Olive's shelf catalog tool.

    Columns are resolved from the header row, not by position: the shelf added
    a 产品简称 column between 产品名称 and 市场类型, which shifted every field
    after the name by one when they were read positionally.
    """
    columns: list[str | None] | None = None
    rows = []
    for line in (markdown or "").splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.split("|")[1:-1]]
        if len(cells) < 2:
            continue
        if not re.fullmatch(r"[A-Z]\d{4,6}", cells[0]):
            mapped = [_CATALOG_HEADERS.get(cell.replace(" ", "").lower())
                      for cell in cells]
            if mapped.count("productCode") == 1 and mapped.count("productName") == 1:
                columns = mapped
            continue
        row = dict.fromkeys(_CATALOG_FIELDS, "")
        for name, cell in zip(columns or _CATALOG_LEGACY_ORDER, cells):
            if name:
                row[name] = cell
        rows.append(row)
    return rows


def _latest_chart_point(summary: dict) -> dict:
    points = (((summary.get("card") or {}).get("chartData") or {})
              .get("dataPoints") or [])
    usable = [point for point in points if isinstance(point, dict)
              and point.get("date") and point.get("value") is not None]
    return max(usable, key=lambda point: str(point["date"])) if usable else {}


def _metric_value(metrics: dict, key: str) -> Any:
    value = metrics.get(key)
    return value.get("valueStr") if isinstance(value, dict) else value


def _detail_part(details: dict[str, Any], role: str) -> dict:
    for tool in _DETAIL_ALIASES[role]:
        if tool in details:
            payload = _payload(details[tool])
            return payload if isinstance(payload, dict) else {}
    return {}


def _latest_nav_point(performance: dict) -> dict:
    """Newest NAV from ``performance.series[].data.navSeries``.

    The shelf dates these points to the month only -- several NAVs share one
    ``month`` and the day is never given. The point is therefore dated to the
    first of that month: the true observation is on or after that day, so
    ``mark`` can only ever overstate how stale the NAV is, never how fresh.
    A bare "YYYY-MM" is not returned, because ``date.fromisoformat`` rejects it.
    """
    best: tuple[str, float] | None = None
    for entry in (performance.get("performance") or {}).get("series") or []:
        if not isinstance(entry, dict):
            continue
        for point in ((entry.get("data") or {}).get("navSeries") or []):
            if not isinstance(point, dict):
                continue
            month, nav = str(point.get("month") or "").strip(), _num(point.get("nav"))
            if not re.fullmatch(r"\d{4}-\d{2}", month) or nav is None:
                continue
            if best is None or month > best[0]:
                best = (month, nav)
    return {"date": f"{best[0]}-01", "value": best[1]} if best else {}


def _merge_fund(catalog: dict, details: dict[str, Any]) -> dict:
    detail = _detail_part(details, "detail")
    summary = _detail_part(details, "summary")
    performance = _detail_part(details, "performance")
    overview = detail.get("fundOverview") or {}
    card = summary.get("card") or {}
    perf = performance.get("performance") or {}
    perf_meta = perf.get("meta") or {}
    point = _latest_chart_point(summary) or _latest_nav_point(performance)
    main = (detail.get("mainMetrics") or summary.get("mainMetrics")
            or card.get("mainMetrics") or {})
    metrics = main.get("metrics") or main

    merged = {
        **catalog,
        "productEnglishName": (overview.get("fundName")
                               or summary.get("fundName")
                               or catalog.get("productName")),
        "currency": perf_meta.get("currency") or "USD",
        "riskLevel": _metric_value(metrics, "RISK_LEVEL")
                     or (card.get("mainMetrics") or {}).get("riskLevel"),
        "latestNav": point.get("value"),
        "navDate": point.get("date"),
        "performanceMap": {
            "1month": _metric_value(metrics, "1M_RETURN"),
            "1year": (_metric_value(metrics, "12M_RETURN")
                      or _metric_value(metrics, "ANNUALIZED_RETURN")),
            "ytd": _metric_value(metrics, "YTD_RETURN"),
            "sinceLaunch": _metric_value(metrics, "ITD_RETURN"),
        },
        "mcpTools": sorted(details),
    }
    return merged


def _catalog_group(item: dict) -> str:
    text = " ".join(str(item.get(key) or "")
                    for key in ("productName", "marketType", "strategy"))
    if "货币" in text or "现金" in text or "cash" in text.lower():
        return "cash"
    if "结构" in text or "structured" in text.lower():
        return "structured"
    if "一级" in text or "private" in text.lower():
        return "private"
    return "funds"


def pull_snapshot(client: OliveMCP, *, product_codes: Iterable[str] | None = None,
                  detail_limit: int = 0) -> dict:
    """Fetch the Olive catalog and an optional bounded detail sample.

    The limit is explicit because a complete shelf pull is hundreds of MCP
    calls. Monday's authenticated validation can start with one or two products
    before enabling the full daily snapshot.
    """
    client.initialize()
    catalog, failures = [], {}
    for tool in OLIVE_CATALOG_TOOLS:
        try:
            catalog = parse_catalog(_catalog_markdown(client.call(tool)))
        except Exception as exc:  # noqa: BLE001 - try the other tool name
            failures[tool] = f"{type(exc).__name__}: {exc}"[:240]
            continue
        if catalog:
            break
    if not catalog:
        raise OliveMCPError(
            "no catalog tool returned a shelf: "
            + (json.dumps(failures, ensure_ascii=False)
               if failures else "table parsed to zero rows"))
    wanted = {str(code) for code in (product_codes or []) if str(code)}
    if wanted:
        targets = [item for item in catalog if item["productCode"] in wanted]
    else:
        targets = catalog[:max(0, int(detail_limit))]

    details_by_code: dict[str, dict[str, Any]] = {}
    errors: dict[str, dict[str, str]] = {}
    for item in targets:
        code = item["productCode"]
        details: dict[str, Any] = {}
        for tool in OLIVE_DETAIL_TOOLS:
            try:
                details[tool] = client.call(tool, {"product_code": code})
            except Exception as exc:  # noqa: BLE001 - one tool must not erase the shelf
                errors.setdefault(code, {})[tool] = (
                    f"{type(exc).__name__}: {exc}"[:240])
        details_by_code[code] = details

    groups: dict[str, list[dict]] = {}
    for item in catalog:
        merged = _merge_fund(item, details_by_code.get(item["productCode"], {}))
        groups.setdefault(_catalog_group(merged), []).append(merged)
    return {
        **groups,
        "metadata": {
            "capturedAt": config.now_hkt().isoformat(),
            "catalogCount": len(catalog),
            "detailedCount": len(targets),
            "detailTools": list(OLIVE_DETAIL_TOOLS),
            "catalogTool": next(
                (t for t in OLIVE_CATALOG_TOOLS if t not in failures), None),
            "errors": errors,
        },
    }


# ---------------------------------------------------------------- ingest
def ingest(con, payload: dict | list, as_of: date | None = None,
           verbose: bool = True) -> dict:
    """Ingest an Olive snapshot captured by the agent session.

    Accepted shapes (all tolerated so the agent can paste tool output directly):
        {"funds":[{...}], "cash":[{...}], "structured":[{...}], "private":[{...}]}
        [{...}, {...}]                      # a flat list of product cards
        {"items":[{...}]}                   # a single MCP tool result
    """
    as_of = as_of or config.today_hkt()
    groups = _as_groups(payload)
    now = config.now_hkt().isoformat()
    inst_rows: list[dict] = []
    nav_rows: list[dict] = []
    skipped = 0

    for group, items in groups.items():
        for it in items:
            rec = _normalise(group, it)
            if rec is None:
                skipped += 1
                continue
            inst_rows.append({
                "key": rec["key"], "kind": rec["kind"], "futu_code": None,
                "olive_key": rec["key"], "name": rec["name"],
                "market": "OLIVE", "currency": rec["currency"], "priceable": 0,
                "meta": {k: v for k, v in rec.items()
                         if k not in ("key", "kind", "name", "currency", "nav", "nav_d")},
                "updated_at": now,
            })
            if rec.get("nav") is not None:
                nav_rows.append({"olive_key": rec["key"],
                                 "d": rec.get("nav_d") or as_of.isoformat(),
                                 "nav": float(rec["nav"]), "src": f"olive:{group}"})

    n_i = db.upsert_many(con, "instruments", inst_rows, ["key"])
    n_n = db.upsert_many(con, "navs", nav_rows, ["olive_key", "d"])

    snap = SNAPSHOT_DIR / f"olive_{as_of.isoformat()}.json"
    snap.write_text(json.dumps(
        {"as_of": as_of.isoformat(), "captured_at": now,
         "counts": {g: len(v) for g, v in groups.items()},
         "payload": groups}, ensure_ascii=False, indent=1), encoding="utf-8")

    rep = {"as_of": as_of.isoformat(), "instruments": n_i, "navs": n_n,
           "skipped": skipped, "groups": {g: len(v) for g, v in groups.items()},
           "snapshot": str(snap)}
    db.kv_set(con, f"olive:{as_of.isoformat()}", rep)
    if verbose:
        print(f"  ✓ olive  instruments={n_i} navs={n_n} skipped={skipped} "
              f"groups={rep['groups']}")
    return rep


def _as_groups(payload: Any) -> dict[str, list[dict]]:
    known = ("funds", "public", "cash", "structured", "private", "underlying")
    if isinstance(payload, dict):
        if any(k in payload for k in known):
            return {k: _as_list(v) for k, v in payload.items() if k in known}
        if "items" in payload:
            return {"funds": _as_list(payload["items"])}
        return {"funds": _as_list(payload)}
    return {"funds": _as_list(payload)}


def _as_list(v: Any) -> list[dict]:
    """Unwrap the several envelopes Olive MCP tools use, including the
    JSON-encoded-string-inside-a-text-block form."""
    if v is None:
        return []
    if isinstance(v, str):
        s = v.strip()
        # tool output sometimes arrives as `ToolName\n\n"{...escaped json...}"`
        m = re.search(r'"(\{.*\})"\s*$', s, re.S)
        if m:
            try:
                s = json.loads(f'"{m.group(1)}"')
            except ValueError:
                s = m.group(1)
        else:
            i = s.find("{")
            j = s.find("[")
            k = min([x for x in (i, j) if x >= 0], default=-1)
            if k > 0:
                s = s[k:]
        try:
            v = json.loads(s)
        except ValueError:
            return []
    if isinstance(v, dict):
        for key in ("items", "list", "data", "records", "cards"):
            if isinstance(v.get(key), list):
                return [x for x in v[key] if isinstance(x, dict)]
        return [v]
    if isinstance(v, list):
        return [x for x in v if isinstance(x, dict)]
    return []


_PCT = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?)\s*%\s*$")


def _pct(v: Any) -> float | None:
    if v in (None, "", "--"):
        return None
    if isinstance(v, (int, float)):
        return float(v) / 100.0
    m = _PCT.match(str(v))
    return float(m.group(1)) / 100.0 if m else None


def _num(v: Any) -> float | None:
    if v in (None, "", "--"):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _normalise(group: str, it: dict) -> dict | None:
    key = (it.get("productCode") or it.get("detailKey") or it.get("batchCode")
           or it.get("isinCode") or it.get("code"))
    if not key:
        return None
    name = (it.get("productEnglishName") or it.get("name") or it.get("productName")
            or it.get("fundName") or str(key))
    kind = "structured" if group == "structured" else "fund"
    perf = it.get("performanceMap") or it.get("metrics") or {}
    rec: dict[str, Any] = {
        "key": str(key), "kind": kind, "name": str(name)[:160],
        "currency": it.get("currency") or "USD",
        "group": group,
        "asset_class": it.get("assetClass"),
        "risk_level": it.get("riskLevel"),
        "strategy": it.get("strategy"),
        "nav": _num(it.get("latestNav") if it.get("latestNav") is not None else it.get("nav")),
        "nav_d": (it.get("navDate") or it.get("asOf") or "")[:10] or None,
        "yield7d": _pct(it.get("hebdomad")),
        "ret1m": _pct(perf.get("1month") or perf.get("ret1m")),
        "ret1y": _pct(perf.get("1year") or perf.get("ret1y")),
        "ytd": _pct(perf.get("ytd")),
        "since": _pct(perf.get("sinceLaunch")),
        "house": it.get("fundHouseNameDesc"),
        "min_initial": _num(it.get("minimumInitialInvestAmount")),
        "detail_url": it.get("detailUrl"),
    }
    if group == "structured":
        rec.update({"underlying": it.get("underlying") or it.get("underlyings"),
                    "coupon": _pct(it.get("rate") or it.get("coupon")),
                    "term": it.get("term"), "status": it.get("recruitmentStatus")})
    return rec


# ---------------------------------------------------------------- marking
def nav_on_or_before(con, olive_key: str, d: str) -> tuple[str, float] | None:
    r = db.q1(con, "SELECT d, nav FROM navs WHERE olive_key=? AND d<=? ORDER BY d DESC LIMIT 1",
              (olive_key, d))
    return (r["d"], float(r["nav"])) if r else None


def mark(con, olive_key: str, d: str) -> dict | None:
    """Mark a fund position. Returns the NAV plus how stale it is, so the caller
    can decide (and the report can disclose) rather than guess."""
    hit = nav_on_or_before(con, olive_key, d)
    if hit is None:
        return None
    nav_d, nav = hit
    stale = (date.fromisoformat(d) - date.fromisoformat(nav_d)).days
    return {"olive_key": olive_key, "d": d, "nav_d": nav_d, "nav": nav,
            "stale_days": stale, "usable": stale <= MAX_NAV_STALE_DAYS}


def cash_yield(con, currency: str = "USD") -> float | None:
    """Median 7-day annualised money-market yield on the shelf, per currency.

    This replaces the hard-coded risk-free constant in the hurdle: the account's
    actual cash alternative is what a tactical trade has to beat. The median is
    used deliberately — the top-of-shelf yield is a marketing artefact.
    """
    rows = db.q(con, "SELECT currency, meta FROM instruments WHERE market='OLIVE'")
    ys = []
    for r in rows:
        meta = db.jl(r["meta"], {}) or {}
        if meta.get("group") == "cash" and meta.get("yield7d") and r["currency"] == currency:
            ys.append(float(meta["yield7d"]))
    if not ys:
        return None
    ys.sort()
    return ys[len(ys) // 2]


def shelf(con, group: str | None = None, limit: int = 400) -> list[dict]:
    rows = db.q(con, "SELECT key,name,currency,meta FROM instruments WHERE market='OLIVE'")
    out = []
    for r in rows:
        meta = db.jl(r["meta"], {}) or {}
        if group and meta.get("group") != group:
            continue
        out.append({"key": r["key"], "name": r["name"], "ccy": r["currency"], **meta})
    return out[:limit]


def coverage(con) -> dict:
    r = db.q1(con, "SELECT COUNT(DISTINCT olive_key) k, COUNT(*) n, MIN(d) a, MAX(d) b FROM navs")
    return {"keys": r["k"], "nav_rows": r["n"], "from": r["a"], "to": r["b"]}
