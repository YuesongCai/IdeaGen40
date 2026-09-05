"""IdeaGen40 command line.

The daily cycle, in order:

    ideagen doctor                  # OpenD / Wisburg / DB liveness
    ideagen ingest                  # Wisburg -> documents
    ideagen olive-ingest <file>     # Olive shelf snapshot -> instruments + navs
    ideagen prices                  # Futu -> prices
    ideagen score                   # D/A/B/N/M/C -> themes
    ideagen theme-candidates        # macro debates the dictionary has no word for
    ideagen theme-register <file>   # admit one, stamped with today's date
    ideagen brief                   # -> data/briefings/briefing_<date>.json
    <generator writes data/batches/batch_<date>.json per prompts/idea_generation.md>
    ideagen ingest-batch <file>     # validate + store + place orders
    ideagen mark                    # advance both books to the last closed session
    ideagen monitor                 # alerts
    ideagen report                  # console attribution
    ideagen dashboard               # -> web/index.html
    ideagen serve                   # localhost dashboard, rebuilt on every refresh

`ideagen daily` runs everything except the generation step, which needs the agent.
"""

from __future__ import annotations

import argparse
import hmac
import http.server
import json
import os
import sys
import webbrowser
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import (analytics, backfill, briefing, cloud_corpus, cloud_paper, config,
               db, generator, ideas as ideas_mod, lexicon, monitor, paper,
               philosophy, poc_fixture, poc_workflow, replay,
               report as report_mod, schema,
               scoring, seed, serve as serve_mod, shelf_store, themes, universe)
from . import platform as platform_mod
from .sources import futu_px, olive, wisburg


def _as_of(args) -> date:
    if getattr(args, "as_of", None):
        return date.fromisoformat(args.as_of)
    return config.today_hkt()


def _con():
    return db.init()


# ---------------------------------------------------------------- commands
def cmd_doctor(args) -> int:
    """Liveness check.

    The exit code reflects only what the run genuinely cannot proceed without:
    the price feed and the database. Wisburg is reported but not fatal — one
    transient network failure there costs a day of fresh corpus, whereas aborting
    the run also loses that day's marks, alerts and attribution, all of which are
    computed from data already on disk.
    """
    con = _con()
    print("IdeaGen40 doctor")
    print(f"  db            {config.DB_PATH}")
    ok = True

    h = futu_px.health()
    print(f"  futu opend    {'OK' if h['ok'] else 'FAIL'}  "
          f"{h.get('probe', '')} {h.get('last', '')} {h.get('error', '')}")
    print(f"                last closed session US={futu_px.complete_through('US')} "
          f"HK={futu_px.complete_through('HK')}")
    ok &= h["ok"]

    try:
        w = wisburg.Wisburg()
        info = w.initialize()
        print(f"  wisburg       OK  {info.get('serverInfo', {})}  tools={len(w.tools())}")
    except Exception as e:  # noqa: BLE001
        print(f"  wisburg       WARN  {str(e)[:150]}")
        print("                非致命：ingest 阶段会记录失败，其余阶段照常运行")

    cv = analytics.coverage(con)
    print(f"  corpus        {cv['documents']['n']} docs / {cv['documents']['days']} days "
          f"({cv['documents']['a']} → {cv['documents']['b']})")
    print(f"  prices        {cv['prices']['codes']} codes / {cv['prices']['bars']} bars "
          f"(last {cv['prices']['last']})")
    print(f"  navs          {cv['navs']['keys']} keys / {cv['navs']['rows']} points "
          f"(last {cv['navs']['last']})")
    blocked = futu_px.quota_blocked(con)
    if blocked:
        print(f"  quota blocked {len(blocked)}: {', '.join(sorted(blocked))}")
    print(f"  registry      {len(universe.ALL)} instruments "
          f"({len([i for i in universe.ALL if i.kind == 'listed'])} listed)")
    print(f"  themes        {len(lexicon.THEMES)} in dictionary v{lexicon.LEXICON_VERSION}")
    print(f"  books         {', '.join(config.BOOKS)}")
    print(f"\n  {'READY' if ok else 'NOT READY — OpenD 不可用，行情与盯市无法进行'}")
    return 0 if ok else 1


def cmd_ingest(args) -> int:
    con = _con()
    as_of = _as_of(args)
    print(f"ingest wisburg  as_of={as_of}  lookback={args.lookback}d")
    rep = wisburg.ingest(con, as_of, lookback_days=args.lookback,
                         fetch_bodies=args.bodies)
    print(f"  total in-window {rep['total']}, new {rep['new']}, "
          f"errors {len(rep['errors'])}")
    return 0


def cmd_olive_ingest(args) -> int:
    raw = (sys.stdin.read() if args.file == "-"
           else Path(args.file).read_text(encoding="utf-8"))
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = raw                      # olive._as_list handles wrapped text
    p = platform_mod.load()
    if getattr(p.state, "paramstyle", "qmark") == "qmark":
        rep = olive.ingest(_con(), payload, as_of=_as_of(args))
        shown = {k: v for k, v in rep.items() if k != "snapshot"}
    else:
        rep = shelf_store.persist(
            p,
            payload,
            as_of=_as_of(args),
            source=shelf_store.LIVE_SOURCE,
            classification=shelf_store.LIVE_CLASSIFICATION,
        )
        shown = {
            key: value for key, value in rep.items()
            if key != "artifact_uri"
        } | {"artifact_archived": bool(rep.get("artifact_uri"))}
    print(json.dumps(shown, ensure_ascii=False))
    return 0


def cmd_mcp_check(args) -> int:
    """Handshake with configured MCP servers and report their tool names."""
    providers = ("wisburg", "olive") if args.provider == "all" else (args.provider,)
    results = {}
    failed = False
    for provider in providers:
        try:
            client = (wisburg.Wisburg() if provider == "wisburg"
                      else olive.OliveMCP())
            info = client.initialize()
            tools = client.tools()
            results[provider] = {
                "ok": True,
                "protocol": info.get("protocolVersion"),
                "server": info.get("serverInfo"),
                "tools": tools,
            }
        except Exception as exc:  # noqa: BLE001 - one provider must not hide the other
            failed = True
            results[provider] = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}"[:300],
            }
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for provider, result in results.items():
            if not result["ok"]:
                print(f"  {provider:<8} FAIL  {result['error']}")
                continue
            server = result.get("server") or {}
            print(f"  {provider:<8} OK    protocol={result.get('protocol')} "
                  f"server={server.get('name')} {server.get('version')} "
                  f"tools={len(result['tools'])}")
            for tool in result["tools"]:
                print(f"             {tool}")
    return 1 if failed else 0


def cmd_olive_pull(args) -> int:
    """Capture an Olive MCP shelf snapshot without printing licensed data."""
    as_of = _as_of(args)
    codes = ([code.strip() for code in args.only.split(",") if code.strip()]
             if args.only else None)
    client = olive.OliveMCP()
    snapshot = olive.pull_snapshot(
        client,
        product_codes=codes,
        detail_limit=args.details,
    )
    out = (args.out or
           config.SNAPSHOTS / f"olive_mcp_{as_of.isoformat()}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snapshot, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    meta = snapshot["metadata"]
    print(f"  olive MCP  catalog={meta['catalogCount']} "
          f"detailed={meta['detailedCount']} errors={len(meta['errors'])}")
    print(f"  snapshot   {out}")
    if args.ingest:
        p = platform_mod.load()
        if getattr(p.state, "paramstyle", "qmark") == "qmark":
            rep = olive.ingest(_con(), snapshot, as_of=as_of)
            shown = {k: v for k, v in rep.items() if k != "snapshot"}
        else:
            rep = shelf_store.persist(
                p,
                snapshot,
                as_of=as_of,
                source=shelf_store.LIVE_SOURCE,
                classification=shelf_store.LIVE_CLASSIFICATION,
            )
            shown = {
                key: value for key, value in rep.items()
                if key != "artifact_uri"
            } | {"artifact_archived": bool(rep.get("artifact_uri"))}
        print(json.dumps(shown, ensure_ascii=False))
    return 1 if meta["errors"] else 0


def cmd_olive_auth(args) -> int:
    """Run Noah SSO Authorization Code + PKCE and persist tokens locally."""
    env_file = args.env_file
    values: dict[str, str] = {}
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip("'\"")

    redirect_uri = (values.get("OLIVE_OAUTH_REDIRECT_URI")
                    or "http://127.0.0.1:8766/callback")
    parsed = urlparse(redirect_uri)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost"):
        raise RuntimeError("Olive OAuth callback must use local HTTP loopback")
    resource = (getattr(args, "url", "") or values.get("OLIVE_MCP_URL")
                or config.OLIVE_MCP_URL)
    if not resource:
        raise RuntimeError(
            "no Olive endpoint: pass --url https://<host>/mcp, or set "
            "OLIVE_MCP_URL in the env file")
    issuer = values.get("OLIVE_OAUTH_ISSUER") or config.OLIVE_OAUTH_ISSUER
    if not issuer:
        # Only the endpoint has to be known; RFC 9728 names its issuer.
        issuer = olive.discover_issuer(resource)
        print(f"discovered OAuth issuer: {issuer}")
    client_id = values.get("OLIVE_OAUTH_CLIENT_ID", "")
    if not client_id:
        client_id = olive.register_oauth_client(
            redirect_uri, issuer=issuer)["client_id"]

    url, verifier, state = olive.oauth_authorization(
        client_id, redirect_uri, issuer=issuer, resource_url=resource)
    received: dict[str, str] = {}

    class Callback(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            query = parse_qs(urlparse(self.path).query)
            code = (query.get("code") or [""])[0]
            returned_state = (query.get("state") or [""])[0]
            ok = bool(code) and hmac.compare_digest(returned_state, state)
            if ok:
                received["code"] = code
                body = "Olive MCP authorization completed. You can close this tab."
                status = 200
            else:
                received["error"] = (
                    (query.get("error_description") or query.get("error")
                     or ["invalid state or missing code"])[0])
                body = "Olive MCP authorization failed. Return to the terminal."
                status = 400
            raw = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, fmt, *parts):  # noqa: A003
            return

    server = http.server.HTTPServer((parsed.hostname, parsed.port or 80), Callback)
    server.timeout = args.timeout
    print("Open this URL and complete Noah SSO authorization:")
    print(url)
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.handle_request()
    finally:
        server.server_close()
    if "code" not in received:
        raise RuntimeError(received.get("error") or "OAuth callback timed out")

    tokens = olive.exchange_oauth_code(
        received["code"], verifier, client_id, redirect_uri,
        token_url=f"{issuer.rstrip('/')}/api/oauth/token",
        resource_url=resource,
    )
    expires = int(tokens.get("expires_in") or 0)
    updates = {
        "OLIVE_MCP_URL": resource,
        "OLIVE_OAUTH_ISSUER": issuer,
        "OLIVE_OAUTH_CLIENT_ID": client_id,
        "OLIVE_OAUTH_REDIRECT_URI": redirect_uri,
        "OLIVE_OAUTH_ACCESS_TOKEN": tokens["access_token"],
        "OLIVE_OAUTH_REFRESH_TOKEN": tokens.get("refresh_token", ""),
        "OLIVE_OAUTH_TOKEN_EXPIRES_AT": (
            datetime.now(timezone.utc) + timedelta(seconds=expires)
        ).isoformat() if expires else "",
    }
    lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []
    seen = set()
    output = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in updates:
            output.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            output.append(line)
    if missing := [key for key in updates if key not in seen]:
        output.extend(["", "# --- Olive MCP OAuth 2.1 ---"])
        output.extend(f"{key}={updates[key]}" for key in missing)
    temp = env_file.with_suffix(env_file.suffix + ".tmp")
    temp.write_text("\n".join(output) + "\n", encoding="utf-8")
    os.chmod(temp, 0o600)
    temp.replace(env_file)
    print(f"Olive OAuth tokens stored in {env_file} (mode 0600)")
    return 0


def cmd_prices(args) -> int:
    con = _con()
    universe.sync_registry(con)
    end = _as_of(args)
    start = end - timedelta(days=args.days)
    extra = [r["futu_code"] for r in db.q(
        con, "SELECT DISTINCT futu_code FROM ideas WHERE futu_code IS NOT NULL")]
    codes = universe.priceable_codes(extra + lexicon.all_indicators())
    print(f"prices  {len(codes)} codes  {start} → {end}")
    rep = futu_px.sync(con, codes, start, end, verbose=args.verbose,
                       retry_blocked=args.retry_blocked)
    print(f"  fetched {rep['fetched']} codes, {rep['rows']} rows")
    if rep["errors"]:
        for k, v in list(rep["errors"].items())[:10]:
            print(f"  ! {k}: {v[:90]}")
    return 0


def cmd_score(args) -> int:
    con = _con()
    scoring.score_day(con, _as_of(args), force=args.force)
    return 0


def cmd_brief(args) -> int:
    con = _con()
    briefing.build(con, _as_of(args))
    return 0


def cmd_replay(args) -> int:
    """Rebuild the whole record once, forwards, from the complete corpus."""
    con = _con()
    r = replay.run(con, date.fromisoformat(args.start), date.fromisoformat(args.end),
                   verbose=not args.quiet)
    return 0 if not r["failed"] else 1



def cmd_platform(args) -> int:
    """Health of every platform port, plus which env vars are set.

    Run this before anything else on a new machine or in a fresh sandbox. It is
    the only command that works when nothing else does, which is the point.
    """
    p = platform_mod.load(platform=args.platform)
    print(f"platform: {p.name}")
    print()
    worst = 0
    probe_worst = 0
    for h in p.check():
        flag = "OK  " if h.ok else "FAIL"
        if not h.ok and h.name != "events":
            worst = 1
        print(f"  {h.name:<11}{flag}  {h.detail}")
    print()
    print(f"  ready: {p.ready()}   (events 不计入 ready：丢监控只是少看见，"
          f"拒绝运行会丢掉这一周的研报)")

    if args.env:
        print("\n环境变量（只显示是否设置，不打印值）")
        for row in platform_mod.env_report():
            mark = {"env": "env ", "file": "file"}.get(row["source"], "—   ")
            via = f" (via {row['via']})" if row.get("via") else ""
            print(f"  {mark} {row['key']:<38}{row['purpose']}{via}")

    if args.probe:
        # A real round-trip through the blob store. `check()` only proves the
        # endpoint answers; this proves we can actually write, read back the same
        # bytes, and that a second write to the same key is refused.
        import json as _j
        key = f"selftest/{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
        body = _j.dumps({"probe": True, "platform": p.name}).encode()
        print("\n产物往返自检")
        try:
            uri = p.blobs.put(key, body, content_type="application/json")
            same = p.blobs.get(key) == body
            print(f"  put  {uri}")
            print(f"  get  字节一致: {same}")
            try:
                p.blobs.put(key, b"overwrite")
                print("  不可变性 FAIL —— 覆盖成功了，不该")
                worst = 1
                probe_worst = 1
            except platform_mod.PlatformError:
                print("  不可变性 OK  —— 二次写入被拒")
        except Exception as e:  # noqa: BLE001
            print(f"  FAIL {type(e).__name__}: {e}")
            worst = 1
            probe_worst = 1

    if args.state_probe:
        print("\n云数据库状态往返自检")
        try:
            if isinstance(p.state, platform_mod.Unavailable):
                raise platform_mod.NotConfigured(p.state.check().detail)
            dialect = getattr(p.state, "dialect", "unknown")
            if dialect not in ("mysql", "postgres"):
                raise platform_mod.NotConfigured(
                    "state 仍是本地存储；请填写云数据库配置并使用 "
                    "IDEAGEN_PLATFORM=byteplus")
            schema.migrate(p.state)
            import uuid
            now = datetime.now(timezone.utc).isoformat()
            run_id = f"probe-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
            schema.upsert(p.state, "orch_runs", {
                "run_id": run_id, "as_of": config.today_hkt().isoformat(),
                "kind": "platform_probe", "platform": p.name,
                "started_at": now, "ended_at": now, "ok": 1,
                "error": None, "inputs_sha": None, "journal_uri": None,
                "calls": 0,
            }, replace=False)
            row = p.state.q(
                "SELECT run_id, kind, platform, ok, ended_at "
                "FROM orch_runs WHERE run_id=?", (run_id,))
            if len(row) != 1 or row[0]["run_id"] != run_id:
                raise RuntimeError("write succeeded but read-back did not match")
            print(f"  migrate OK  engine={dialect}  {len(schema.OWNED)} tables verified")
            print(f"  write   OK  run_id={run_id}")
            print(f"  read    OK  kind={row[0]['kind']} platform={row[0]['platform']}")
        except Exception as e:  # noqa: BLE001
            print(f"  FAIL {type(e).__name__}: {e}")
            worst = 1
            probe_worst = 1

    if args.inference_probe:
        print("\n火山模型最小调用自检")
        try:
            result = p.inference.complete(
                "Reply with exactly READY and nothing else.",
                temperature=0.0, max_tokens=16)
            tokens = result.usage.get("total_tokens")
            token_text = str(tokens) if tokens is not None else "not reported"
            print(f"  call    OK  model={result.model}")
            print(f"  latency     {result.latency_ms}ms")
            print(f"  tokens      {token_text}")
            print(f"  response    {len(result.text)} chars (content hidden)")
        except Exception as e:  # noqa: BLE001
            print(f"  FAIL {type(e).__name__}: {e}")
            worst = 1
            probe_worst = 1
    return (probe_worst if (args.probe or args.state_probe
                            or args.inference_probe) else worst)


def cmd_poc_load_public_mock(args) -> int:
    """Archive and import the public synthetic dashboard fixture."""
    fixture = poc_fixture.read(args.fixture)
    if args.verify_only:
        print(f"fixture OK  id={fixture.fixture_id}")
        print(f"  classification  {poc_fixture.CLASSIFICATION}")
        print(f"  sha256         {fixture.sha256}")
        return 0

    p = platform_mod.load(platform="byteplus")
    receipt = poc_fixture.load(p, args.fixture)
    print(f"fixture imported  id={receipt['fixture_id']}")
    print(f"  classification  {receipt['classification']}")
    print(f"  sha256         {receipt['sha256']}")
    print(f"  artifact       {receipt['artifact_uri']}")
    print(f"  run_id         {receipt['run_id']}")
    print("  RDS read-back  " + ", ".join(
        f"{table}={count}" for table, count in receipt["rows"].items()))
    return 0


def cmd_poc_load_shelf_fixture(args) -> int:
    """Persist a public synthetic shelf through the production RDS/TOS path."""
    p = platform_mod.load(platform="byteplus")
    receipt = shelf_store.persist_fixture(p, _as_of(args))
    shown = {
        key: value for key, value in receipt.items()
        if key != "artifact_uri"
    } | {"artifact_archived": bool(receipt.get("artifact_uri"))}
    print(json.dumps(shown, ensure_ascii=False, indent=2))
    return 0


def cmd_cloud_ingest(args) -> int:
    """Explicitly ingest Wisburg into portable RDS/TOS state.

    This command is intentionally manual. The unattended scheduler keeps cloud
    licensed-data persistence disabled unless IDEAGEN_CLOUD_WISBURG_ENABLED is
    explicitly enabled.
    """
    p = platform_mod.load(platform="byteplus")
    if args.incremental:
        receipt = cloud_corpus.ingest_incremental(
            p, as_of=_as_of(args), detail_limit=args.details)
    else:
        receipt = cloud_corpus.ingest_window(
            p, _as_of(args), detail_limit=args.details)
    shown = {
        key: value for key, value in receipt.items()
        if key != "artifact_uri"
    } | {"artifact_archived": bool(receipt.get("artifact_uri"))}
    print(json.dumps(shown, ensure_ascii=False, indent=2))
    return 1 if receipt.get("errors") else 0


def cmd_cloud_monitor(args) -> int:
    receipt = cloud_paper.monitor(
        platform_mod.load(platform="byteplus"), _as_of(args))
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0


def _run_poc_weekly(as_of: date, p, *, verbose: bool = True,
                    mode: str = "public-synthetic") -> dict:
    """Run one live-model POC period, or reuse its completed RDS record."""
    schema.migrate(p.state)
    prepared = poc_workflow.weekly_kwargs(as_of, p=p, mode=mode)
    classification = prepared["params"]["data_classification"]
    done = p.state.q(
        "SELECT run_id, as_of, calls, journal_uri FROM orch_runs "
        "WHERE kind='weekly' AND ok=1 AND as_of=? "
        "AND data_classification=? ORDER BY ended_at DESC LIMIT 1",
        (as_of.isoformat(), classification),
    )
    if done:
        return {
            **dict(done[0]),
            "ok": True,
            "reused": True,
            "data_classification": classification,
            "mode": mode,
        }

    result = poc_workflow.run_weekly(
        as_of, p=p, verbose=verbose, mode=mode, prepared=prepared)
    return {
        "run_id": result.run_id,
        "as_of": result.as_of,
        "ok": result.completed,
        "reused": False,
        "data_classification": classification,
        "mode": mode,
        "calls": result.calls,
        "journal_uri": result.journal,
        "topics": len(result.topics),
        "candidates": result.n_candidates,
        "generators": sorted(result.generators),
        "selectors": sorted(result.selectors),
        "error": result.error,
        "skipped": result.skipped,
    }


def cmd_poc_run_weekly(args) -> int:
    """Run Stage A/B/C with real ModelArk over one explicit input mode."""
    p = platform_mod.load(platform="byteplus")
    receipt = _run_poc_weekly(_as_of(args), p, mode=args.mode)
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0 if receipt["ok"] else 1


def cmd_poc_backtest(args) -> int:
    """Run and persist the deterministic three-month POC replay."""
    receipt = poc_workflow.run_backtest(
        _as_of(args),
        p=platform_mod.load(platform="byteplus"),
        weeks=args.weeks,
        horizon_days=args.horizon_days,
    )
    return 0 if receipt["model_calls"] == 0 else 1


def cmd_poc_run_all(args) -> int:
    """Run the live-model weekly first, then the zero-model replay."""
    p = platform_mod.load(platform="byteplus")
    weekly = _run_poc_weekly(_as_of(args), p, mode=args.mode)
    print(json.dumps({"weekly": weekly}, ensure_ascii=False, indent=2))
    if not weekly["ok"]:
        return 1
    backtest_receipt = poc_workflow.run_backtest(
        _as_of(args),
        p=p,
        weeks=args.weeks,
        horizon_days=args.horizon_days,
        verbose=False,
    )
    print(json.dumps(
        {"backtest": backtest_receipt}, ensure_ascii=False, indent=2))
    return 0 if backtest_receipt["model_calls"] == 0 else 1


def cmd_rebuild_batch(args) -> int:
    """Drop a batch's orders/positions/outcomes and re-trade it from its ideas.

    Needed when a batch's ideas were replaced under live positions, which
    rebinds `<batch_id>#<local_id>` uids to different instruments. The ideas
    themselves are kept — they are the authoritative record; only the derived
    trading state is rebuilt.
    """
    con = _con()
    bid = args.batch_id
    n_ideas = db.q1(con, "SELECT COUNT(*) n FROM ideas WHERE batch_id=?", (bid,))["n"]
    if not n_ideas:
        print(f"  no such batch: {bid}")
        return 1

    before = ideas_mod.instrument_mismatches(con)
    print(f"  {bid}: {n_ideas} ideas, "
          f"{len([b for b in before if b['batch_id'] == bid])} mismatched positions")

    with db.tx(con):
        n = {}
        for t in ("outcomes", "alerts", "trades", "orders", "positions"):
            n[t] = con.execute(
                f"DELETE FROM {t} WHERE idea_uid IN "
                f"(SELECT idea_uid FROM ideas WHERE batch_id=?)", (bid,)).rowcount
        n["mtm"] = con.execute(
            "DELETE FROM mtm WHERE pos_id NOT IN "
            "(SELECT pos_id FROM positions)").rowcount
    print("  dropped " + "  ".join(f"{k}={v}" for k, v in n.items() if v))

    for b in config.BOOKS:
        r = paper.open_batch(con, bid, b, verbose=False)
        print(f"  reopened {b:<14} filled={r.get('filled', 0)} "
              f"skipped={r.get('skipped', 0)}")
    r = paper.open_cohort(con, bid, verbose=False)
    print(f"  reopened {config.cohort_book(bid):<14} filled={r.get('filled', 0)}")

    after = ideas_mod.instrument_mismatches(con)
    print(f"\n  mismatches: {len(before)} → {len(after)}")
    if after:
        print("  STILL BROKEN — do not publish; inspect before/after by hand")
        return 1
    print("  now re-mark and re-settle:  ideagen mark && ideagen settle")
    return 0


def cmd_theme_candidates(args) -> int:
    """Show the macro debates today's corpus contains and the dictionary lacks."""
    con = _con()
    r = themes.candidates(con, _as_of(args), limit=args.limit)
    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0
    print(f"  {r['as_of']}  window {r['window_days']}d  "
          f"{r['registered']} themes registered")
    print(f"  dictionary reach {r['coverage_pct']}% "
          f"({r['corpus_matched']}/{r['corpus_total']} items); "
          f"{r['unmatched']} matched nothing")
    g = r["gates"]
    print(f"  gates: >={g['min_docs']} docs, >={g['min_institutions']} institutions, "
          f">={g['min_days']} days, lift >={g['min_lift']}, "
          f"cluster >={g['min_cluster_docs']} docs")
    if not r["candidates"]:
        print("  no candidate clears the gates")
        return 0
    for i, c in enumerate(r["candidates"], 1):
        print(f"\n  [{i}] {' · '.join(c['terms'][:8])}")
        print(f"      {c['n_docs']} docs / {c['n_institutions']} institutions / "
              f"{c['n_days']} days / lift {c['max_lift']} / tiers {c['tiers']}")
        for e in c["evidence"][:5]:
            print(f"      T{e['tier']} {e['d']} {e['institution'][:18]:<18} "
                  f"{(e['title'] or '')[:56]}")
    print("\n  A candidate is not a theme: these are phrase clusters, and company")
    print("  names and report-series titles do get through. Register only the ones")
    print("  that name a debate a trade can express:  ideagen theme-register <file>")
    return 0


def cmd_theme_register(args) -> int:
    """Append discovered themes to the registry, stamped with today's date."""
    con = _con()
    as_of = _as_of(args)
    raw = sys.stdin.read() if args.file == "-" else Path(args.file).read_text("utf-8")
    rows = json.loads(raw)
    if isinstance(rows, dict):
        rows = [rows]
    for row in rows:
        t = themes.register(con, row, as_of)
        print(f"  registered {t.id} — {t.label}  (as of {t.registered_d})")
        print(f"      key question  {t.key_question}")
        print(f"      indicator     {t.price_indicator}   related {list(t.related)}")
        print(f"      synonyms      {len(t.terms)}: {' · '.join(t.terms[:8])}")
    print(f"\n  {len(rows)} theme(s) appended to {lexicon.REGISTRY_PATH}")
    print("  They score from today forward only. Pick them up with:  ideagen score --force")
    return 0


def cmd_seed(args) -> int:
    con = _con()
    universe.sync_registry(con)
    bid, rows, rep = seed.import_pack(con)
    universe.sync_registry(con)
    if args.trade:
        for b in config.BOOKS:
            paper.reset_book(con, b)
            paper.open_batch(con, bid, b)
        paper.open_cohort(con, bid)
    return 0 if rep["pass"] else 1


def cmd_verify_seed(args) -> int:
    con = _con()
    print(json.dumps(seed.verify_worksheet(con), ensure_ascii=False, indent=1))
    return 0


def cmd_ingest_batch(args) -> int:
    con = _con()
    payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
    as_of = date.fromisoformat(payload.get("as_of") or _as_of(args).isoformat())
    bid, rows, rep = ideas_mod.build_batch(con, payload, as_of,
                                           generator=args.generator)
    s = rep["summary"]
    print(f"batch {bid}  n={s['n']}  grades={s['grades']}  kinds={s['kinds']}  "
          f"horizons={s['horizons']}")
    print(f"validation pass={rep['pass']}  errors={rep['n_errors']}  "
          f"warnings={rep['n_warnings']}")
    for c in rep["checks"]:
        if not c["ok"]:
            print(f"  [{c['severity']}] {c['check']}: "
                  f"{json.dumps(c['detail'], ensure_ascii=False)[:200]}")
    if not rep["pass"]:
        print("\nbatch stored as draft; not traded. Fix and re-ingest.")
        return 1
    if args.trade:
        for b in config.BOOKS:
            paper.open_batch(con, bid, b)
        paper.open_cohort(con, bid)      # the day's own independent book
    return 0


def cmd_mark(args) -> int:
    """Advance every book to the last closed session.

    Cohorts are included on purpose. Each past day's cohort must be re-marked at
    today's prices — opening 2026-07-28 tomorrow should show 2026-07-28's entries
    against tomorrow's closes, and positions leaving the book as their horizons
    arrive. Marking only the commingled books would freeze every cohort.
    """
    con = _con()
    end = args.to or futu_px.complete_through("US")
    books = paper.all_books(con)
    for b in books:
        first = db.q1(con, "SELECT MIN(placed_d) d FROM orders WHERE book_id=?", (b,))
        start = args.since or (first["d"] if first and first["d"] else end)
        paper.run(con, b, start, end, verbose=not config.is_cohort(b))
    n_co = sum(1 for b in books if config.is_cohort(b))
    print(f"  marked {len(books)} books ({n_co} 个当日组合) 至 {end}")
    return 0


def cmd_monitor(args) -> int:
    con = _con()
    monitor.run(con, args.on)
    return 0


def cmd_settle(args) -> int:
    con = _con()
    analytics.settle(con, book_id=args.book)
    return 0


def cmd_report(args) -> int:
    con = _con()
    rep = analytics.print_report(con)
    if args.json:
        Path(args.json).write_text(json.dumps(rep, ensure_ascii=False, indent=1,
                                              default=str), encoding="utf-8")
        print(f"  wrote {args.json}")
    return 0


def cmd_dashboard(args) -> int:
    con = _con()
    out = report_mod.build(con, Path(args.out) if args.out else None,
                           artifact=args.artifact,
                           embed_images=(False if args.public else None))
    print(f"  dashboard → {out}")
    return 0


def cmd_backfill(args) -> int:
    con = _con()
    rep = backfill.run(con, date.fromisoformat(args.start),
                       date.fromisoformat(args.end),
                       ingest=not args.no_ingest, fetch_bodies=args.bodies)
    for b in db.q(con, "SELECT batch_id FROM batches WHERE status='traded'"):
        paper.open_cohort(con, b["batch_id"])
    report_mod.build(con)
    return 0 if not rep["failed"] else 1


def cmd_generate(args) -> int:
    con = _con()
    as_of = _as_of(args)
    payload = generator.generate(con, as_of)
    if args.trade:
        bid = f"B{as_of.isoformat().replace('-', '')}"
        _, rows, val = ideas_mod.build_batch(con, payload, as_of,
                                             generator=generator.GENERATOR,
                                             batch_id=bid)
        print(f"batch {bid} pass={val['pass']} errors={val['n_errors']}")
        if val["pass"]:
            for b in config.BOOKS:
                paper.open_batch(con, bid, b)
            paper.open_cohort(con, bid)
    return 0


def cmd_sources(args) -> int:
    """Provenance audit: can every claim be traced back, and does the trail hold?"""
    con = _con()
    if args.verify:
        wisburg.verify_assets(con, limit=args.limit)
    if args.doc:
        print(json.dumps(wisburg.provenance(con, args.doc), ensure_ascii=False,
                         indent=1))
        return 0
    a = wisburg.source_audit(con)
    d, ast, c = a["documents"], a["assets"], a["citations"]
    print("溯源审计")
    print(f"  研报      {d['n']:,} 条")
    print(f"    可复现检索式 {d['receipt']:,} ({d['receipt']/max(d['n'],1)*100:.0f}%)"
          f"   内容哈希 {d['hash']:,} (100%)"
          f"   发布时间 {d['ts']:,} (100%)")
    print(f"    具名机构     {d['inst']:,} ({d['inst']/max(d['n'],1)*100:.0f}%)"
          f"   已深取正文 {d['deep']:,}")
    # Dictionary reach belongs in the provenance audit: an item the dictionary
    # cannot name is an item no idea can ever cite, however well sourced it is.
    reach = themes.candidates(con, _as_of(args), limit=3)
    print(f"    字典可命名   {reach['corpus_matched']:,}/{reach['corpus_total']:,} "
          f"({reach['coverage_pct']}%) 按 {reach['registered']} 个已注册主题"
          f"   零匹配 {reach['unmatched']:,}")
    if reach["candidates"]:
        top = "、".join(c["terms"][0] for c in reach["candidates"])
        print(f"    待判定候选   {len(reach['candidates'])} 个：{top}"
              f"   (ideagen theme-candidates)")
    print(f"  资产      {ast['n']} 个图表/插图，覆盖 {ast['docs']} 篇")
    print(f"    已验证可达   {ast['ok']}   不可达 {ast['bad']}   未验证 {ast['unchecked']}")
    print(f"  引用      共 {c['total']} 条")
    print(f"    可解析到研报 {c['resolved']}   散文式归属 {c['prose']}"
          f"   悬空引用 {c['dangling']}")
    if c["dangling"]:
        print(f"    ⚠ {c['dangling']} 条引用有 doc_id 形状但不在研报库里——这是真缺陷")
    for b, v in c["by_batch"].items():
        print(f"      {b}: resolved={v['resolved']} prose={v['prose']} "
              f"dangling={v['dangling']}")
    print(f"  行情      {a['prices']['n']:,} 根日线，全部带 src 标记 "
          f"({a['prices']['src']:,})")
    print(f"  NAV       {a['navs']['n']} 个观测，全部带 src 标记 ({a['navs']['src']})")
    print(f"\n  分线")
    for r in a["by_line"]:
        lbl = config.SOURCE_LINES.get(r["line"], {}).get("label", r["line"])
        print(f"    {lbl:<10} docs={r['docs']:<5} 检索式={r['receipt']:<5} "
              f"机构={r['inst']:<5} 资产={r['assets']}")
    print(f"\n  {a['note']}")
    return 0


def cmd_lookthrough(args) -> int:
    """穿透：这些 ETF 到底持有什么，以及由此能问出的三个问题。

    `universe.py` 用一条手写标签描述一只标的，而标签是写标签的人的断言，不是
    这只基金的事实。四个动作分别回答：现在的标签在哪里说了谎（collisions）、
    一个主题该用哪只标的表达（theme）、一组持仓真实押在哪些名字上（portfolio）、
    以及把这些答案所依赖的持仓数据拉下来（refresh）。
    """
    from . import lookthrough as lt
    from . import universe as uni
    con = _con()
    as_of = _as_of(args)

    if args.action == "refresh":
        syms = args.symbols.split(",") if args.symbols else [
            i.key for i in uni.LISTED if i.market in ("US",)]
        print(f"拉取 {len(syms)} 只标的的持仓…")
        funds = lt.refresh(con, syms, as_of)
        from collections import Counter
        cnt = Counter(f.status for f in funds.values())
        print(f"  可穿透 {cnt['ok']}   看不透 {cnt['opaque']}   "
              f"非基金 {cnt['not_a_fund']}   取数失败 {cnt['error']}")
        for sym, f in sorted(funds.items()):
            if f.status != "ok":
                print(f"    {sym:<8}{f.status:<12}{f.note}")
        return 0

    funds = lt.load(con, as_of)
    if not funds:
        print("还没有穿透快照。先跑 ideagen lookthrough refresh", file=sys.stderr)
        return 1
    stamp = next(iter(funds.values())).as_of
    ok = sum(1 for f in funds.values() if f.usable)
    print(f"穿透快照 {stamp}：{len(funds)} 只，其中 {ok} 只可穿透\n")

    if args.action == "collisions":
        labels = {i.key: i.exposure for i in uni.ALL}
        same, diff = lt.collisions(funds, labels)
        print("① 标签相同、底层不同 —— 生成器以为这些行可以互换")
        for label, a, b, o in same:
            print(f"   {label:<14}{a:>6} vs {b:<6} 实际重叠 {o*100:5.1f}%")
        if not same:
            print("   （无）")
        print("\n② 标签不同、底层相同 —— 以为分散了，其实是同一笔注")
        for a, la, b, lb, o in diff[:args.limit]:
            print(f"   {a:>6}({la}) vs {b:<6}({lb})  重叠 {o*100:5.1f}%")
        if not diff:
            print("   （无）")
        opaque = sorted(s for s, f in funds.items() if f.status == "opaque")
        if opaque:
            print(f"\n③ 不参与比较的 {len(opaque)} 只（持仓行没有 ticker，"
                  f"期货/实物/掉期）：{'、'.join(opaque)}")
        return 0

    if args.action == "theme":
        basket = [x.strip() for x in (args.names or "").split(",") if x.strip()]
        if not basket:
            print("需要 --names AAA,BBB,CCC（主题的底层名单）", file=sys.stderr)
            return 2
        labels = {i.key: i.exposure for i in uni.ALL}
        hits = lt.resolve_theme(funds, basket)
        print(f"主题名单（{len(basket)}）：{'、'.join(basket)}")
        print(f"\n  {'标的':<8}{'现有标签':<18}{'真实主题权重':>10}   命中")
        for h in hits[:args.limit]:
            print(f"  {h.symbol:<8}{labels.get(h.symbol, ''):<18}"
                  f"{h.weight*100:9.1f}%   {'、'.join(h.matched[:6])}")
        if not hits:
            print("  没有任何可穿透标的持有这个名单——不是「没有敞口」，"
                  "是这批标的里没有")
        return 0

    if args.action == "discover":
        basket = [x.strip() for x in (args.names or "").split(",") if x.strip()]
        if not basket:
            print("需要 --names AAA,BBB,CCC（主题的底层名单）", file=sys.stderr)
            return 2
        known = {i.key for i in uni.ALL}
        print(f"反查货架之外持有这批公司的基金（{len(basket)} 个名字，"
              f"每个一次厂商调用）…\n")
        hits = lt.discover(basket, known, limit=args.limit)
        if not hits:
            print("  没有货架外的候选——这个主题要么本来就没有载体，"
                  "要么货架上已经有了")
            return 0
        print(f"  {'代码':<9}{'真实主题权重':>10}   命中")
        for h in hits:
            print(f"  {h.symbol:<9}{h.weight*100:9.1f}%   {'、'.join(h.matched[:6])}")
        print("\n  这些是候选、不是结论：进 universe 前要确认载体合规、"
              "日度流动性、以及 Futu 能不能报价——持仓文件都答不了这三件事。")
        return 0

    if args.action == "portfolio":
        ins = [x.strip() for x in (args.symbols or "").split(",") if x.strip()]
        if not ins:
            print("需要 --symbols A,B,C", file=sys.stderr)
            return 2
        e = lt.portfolio(funds, ins)
        print(f"表面：{len(ins)} 只标的等权，每只 {100/len(ins):.1f}%")
        print(f"穿透：可见 {e.coverage*100:.0f}% 的仓位，"
              f"其中现金 {e.cash*100:.1f}%")
        print(f"  真实有效名数 {e.effective_names:.0f} "
              f"（表面是 {len(ins)}；差多少就是重复押注了多少）")
        if e.opaque:
            print(f"  看不透，未计入：{'、'.join(e.opaque)}")
        print(f"\n  前十大真实单名：")
        for a, w in e.top(10):
            holders = [s for s in ins
                       if funds.get(s) and funds[s].weights.get(a)]
            print(f"    {a:<26}{w*100:5.2f}%   来自 {len(holders)} 只："
                  f"{'、'.join(holders[:6])}")
        return 0

    print(f"未知动作 {args.action!r}", file=sys.stderr)
    return 2


def cmd_macro(args) -> int:
    """The macro layer: what was ingested, what it is attached to, what is off.

    Five actions rather than one report because the four attachments fail
    independently and are decided independently. `status` is the one to run
    first: it says which switches are set, and a reader who does not know that
    cannot interpret any of the others.
    """
    from . import macro
    con = db.init()
    macro.ensure_schema(con)      # every action below reads the fit cache
    as_of = _as_of(args)

    if args.action == "refresh":
        rep = macro.refresh(con, as_of, force_stats=args.force, verbose=True)
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0 if not rep["problems"] else 1

    if args.action == "status":
        f = macro.flags()
        print("开关（默认全关，因为它们都是打分/模型输入，中途开会让期次不可比）")
        for k, v in f.items():
            print(f"  {k:26s} {'ON' if v else 'off'}")
        rows = db.q(con, "SELECT kind, COUNT(*) n, MAX(date) last FROM events"
                         " GROUP BY kind ORDER BY n DESC")
        print("\nevents 表")
        for r in rows:
            print(f"  {str(r['kind']):16s} {r['n']:5d}  最新 {r['last']}")
        st = db.q(con, "SELECT status, COUNT(*) n, MAX(upto) u FROM"
                       " macro_surprise_stats GROUP BY status")
        print("\n误差分布拟合")
        if not st:
            print("  还没有拟合过。先跑 ideagen macro refresh")
        for r in st:
            print(f"  {r['status']:14s} {r['n']:5d} 条序列  (upto {r['u']})")
        return 0

    if args.action == "surprises":
        days = [(as_of - timedelta(days=i)).isoformat()
                for i in range(args.days, -1, -1)]
        rows = macro.window_surprises(con, days)
        if not rows:
            print(f"近 {args.days} 天没有已入库的宏观发布。先跑 ideagen macro refresh")
            return 1
        for r in rows:
            z = f"z={r['z']:+.2f}" if r["z"] is not None else f"z=—（{r['why']}）"
            act = "—" if r["actual"] is None else r["actual"]
            est = "—" if r["estimate"] is None else r["estimate"]
            print(f"{r['date']}  {str(r['label'])[:38]:38s} "
                  f"实际 {act!s:>8}  预期 {est!s:>8}  {z}")
        return 0

    if args.action == "positioning":
        codes = ([c.strip() for c in args.symbols.split(",") if c.strip()]
                 if args.symbols
                 else sorted(set(macro.COT_DIRECT) | set(macro.COT_PROXY)))
        for code in codes:
            v, meta = macro.positioning_crowding(con, code, as_of.isoformat())
            if v is None:
                print(f"{code:10s}  —      {meta.get('note') or meta.get('link')}")
            else:
                print(f"{code:10s}  {v:5.1f}  {meta['contract']:>3s}"
                      f"（{meta['link']}）  {meta['cot_date']}"
                      f"  滞后 {meta['lag_days']} 天")
        return 0

    if args.action == "regime":
        print(json.dumps(macro.regime(con, as_of.isoformat()),
                         ensure_ascii=False, indent=2))
        return 0

    print(f"未知动作 {args.action!r}", file=sys.stderr)
    return 2


def cmd_serve(args) -> int:
    serve_mod.serve(port=args.port, open_browser=args.open)
    return 0


def cmd_daily(args) -> int:
    """Everything a cron can do without the generator."""
    con = _con()
    as_of = _as_of(args)
    run_id = f"R{config.now_hkt().strftime('%Y%m%dT%H%M%S')}"
    stages: list[dict] = []
    db.upsert(con, "runs", {"run_id": run_id, "as_of": as_of.isoformat(),
                            "started_at": config.now_hkt().isoformat(),
                            "status": "running", "stages": stages}, ["run_id"])

    def stage(name, fn):
        t0 = config.now_hkt()
        try:
            fn()
            st = "ok"
            note = None
        except Exception as e:  # noqa: BLE001 - a broken stage must not lose the run
            st, note = "failed", f"{type(e).__name__}: {e}"
            print(f"  ! stage {name} failed: {note}")
        ms = int((config.now_hkt() - t0).total_seconds() * 1000)
        stages.append({"stage": name, "status": st, "ms": ms, "note": note})
        con.execute("UPDATE runs SET stages=? WHERE run_id=?",
                    (json.dumps(stages, ensure_ascii=False), run_id))
        return st == "ok"

    print(f"=== ideagen daily {as_of} run={run_id} ===")
    print("[1/10] wisburg ingest")

    def _ingest():
        try:
            return wisburg.ingest(con, as_of, lookback_days=args.lookback,
                                  fetch_bodies=args.bodies)
        except Exception:                     # one transport retry after a pause
            import time as _t
            _t.sleep(20)
            return wisburg.ingest(con, as_of, lookback_days=args.lookback,
                                  fetch_bodies=args.bodies)

    stage("ingest", _ingest)
    print("[2/10] prices")
    stage("prices", lambda: futu_px.sync(
        con, universe.priceable_codes(lexicon.all_indicators()),
        as_of - timedelta(days=400), as_of))
    print("[3/10] ETF 穿透快照")

    def _lookthrough():
        # Holdings move on the funds' own rebalance calendar, not daily, so this
        # is cheap insurance rather than a live feed. It is here because the
        # generator reads the snapshot and a strategy may not fetch: `RunContext`
        # gives no network on purpose, so a stale snapshot has to be prevented
        # upstream instead of repaired inside a run.
        from . import lookthrough as lt
        syms = [i.key for i in universe.LISTED if i.market == "US"]
        funds = lt.refresh(con, syms, as_of)
        ok = sum(1 for f in funds.values() if f.usable)
        print(f"      {ok}/{len(funds)} 只可穿透")

    stage("lookthrough", _lookthrough)
    print("[4/10] 宏观日历 · 波动率 · 持仓")

    def _macro():
        # Before scoring, not after. `factor_N`'s consensus surprise and
        # `factor_C`'s positioning leg both read `events`, and a factor that
        # reads a table nothing filled this morning is not degraded — it is
        # silently answering from last week. Same reason the look-through
        # snapshot sits above `score` rather than beside it.
        from . import macro as _macro_mod
        rep = _macro_mod.refresh(con, as_of)
        print(f"      日历 {rep['events_upserted']} 行"
              f"（{rep['feeds_ok']}/{rep['feeds_tried']} 个源）"
              f"　误差分布 {rep['stats']['ok']} 条序列可用")
        if rep["problems"]:
            for line in rep["problems"][:4]:
                print(f"      ! {line}")

    stage("macro", _macro)
    print("[5/10] score themes")
    stage("score", lambda: scoring.score_day(con, as_of))   # skips a traded date
    print("[6/10] briefing pack")
    stage("brief", lambda: briefing.build(con, as_of))
    print("[7/10] mark books (含每日组合)")
    stage("mark", lambda: cmd_mark(argparse.Namespace(since=None, to=None)))
    print("[8/10] monitor")
    stage("monitor", lambda: monitor.run(con))
    print("[9/10] verify source assets")
    stage("verify-assets", lambda: wisburg.verify_assets(con, limit=120))
    print("[10/10] settle + dashboard")
    stage("settle", lambda: analytics.settle(con, book_id="naive", verbose=False))
    stage("dashboard", lambda: report_mod.build(con))

    failed = [s for s in stages if s["status"] != "ok"]
    con.execute("UPDATE runs SET finished_at=?, status=?, stages=? WHERE run_id=?",
                (config.now_hkt().isoformat(),
                 "ok" if not failed else "partial",
                 json.dumps(stages, ensure_ascii=False), run_id))
    print(f"\n=== run {run_id} {'ok' if not failed else 'PARTIAL'} "
          f"({len(stages) - len(failed)}/{len(stages)} stages) ===")
    print(f"下一步：读 data/briefings/briefing_{as_of}.json，"
          f"按 prompts/idea_generation.md 生成 40 条，然后\n"
          f"  ideagen ingest-batch data/batches/batch_{as_of}.json")
    return 0 if not failed else 1


def cmd_status(args) -> int:
    con = _con()
    d = monitor.digest(con, args.on)
    print(json.dumps(d, ensure_ascii=False, indent=1, default=str))
    return 0



def cmd_weekly(args) -> int:
    """Run one weekly period through 筛选A → 筛选B → 筛选C.

    A thin wrapper on purpose. The orchestrator owns the sequence and the
    invariants; this only parses arguments and prints. Keeping the CLI thin is what
    lets the scheduled sandbox, a replay and a test all enter through the same
    function rather than through three slightly different paths.
    """
    from . import orchestrator
    as_of = _as_of(args)
    res = orchestrator.weekly(
        as_of=as_of,
        topic_scorer=args.topic_scorer,
        generators=(args.generators.split(",") if args.generators else None),
        selectors=(args.selectors.split(",") if args.selectors else None),
        # A period generated after the fact is not a period the system called
        # live, and the difference must survive into every artifact: the model
        # weights have seen the world after this date even when the documents
        # have not. `backfill` is how a chart, an export or a PM conversation
        # can tell the two apart without asking anybody.
        params={"data_classification": args.classification},
        dry_run=args.dry_run)
    if res.skipped:
        print(f"\n跳过：{res.skipped}")
        return 0
    if not res.ok:
        print(f"\n失败：{res.error}")
        return 1
    print(f"\n完成  主题 {len(res.topics)} · 候选 {res.n_candidates} · "
          f"组合 {len(res.selectors)} · 产物 {len(res.artifacts)} · "
          f"模型调用 {res.calls}")
    if args.trade and res.completed:
        from . import booking, platform as plat
        print("\n建仓：")
        p = plat.load()
        if getattr(p.state, "paramstyle", "qmark") == "qmark":
            booking.book_run(_con(), p, res.run_id)
        else:
            cloud_paper.book_run(p, res.run_id)
    return 0


def cmd_book(args) -> int:
    """Book an already-completed run's verdicts into the selector paper books."""
    from . import booking, db as _db, platform as plat
    p = plat.load()
    run_id = args.run_id
    if not run_id:
        r = p.state.q("SELECT run_id FROM orch_runs WHERE kind='weekly' AND ok=1 "
                      "ORDER BY as_of DESC, started_at DESC LIMIT 1")
        if not r:
            print("没有成功完成的周跑可以建仓"); return 1
        run_id = r[0]["run_id"]
    if getattr(p.state, "paramstyle", "qmark") == "qmark":
        booking.book_run(_con(), p, run_id)
    else:
        print(json.dumps(
            cloud_paper.book_run(p, run_id), ensure_ascii=False, indent=2))
    return 0


def cmd_philosophy(args) -> int:
    """PM 语义注入的四个动作：看、提、生效、停用。

    `propose` deliberately stops at a file. Distillation is a model call and the
    result is a proposal, not a decision — a philosophy that started steering a
    live book because a model parsed a sentence confidently would be exactly the
    unreviewed knob this whole design exists to avoid.
    """
    from .strategy import available
    as_of = _as_of(args)
    pending = config.DATA / "philosophy" / "pending"
    arms = {r["name"] for r in available("idea_generator")}

    if args.action == "list":
        # `history()` rather than `cards()`: this is the person's question
        # 「都写过什么、当时说了什么」, not the run's 「现在什么在跑」, and a
        # retired card answering only the second one is how a rule used to
        # disappear from view the moment it was stopped.
        past = philosophy.history()
        if not past:
            print("还没有任何 PM 准则卡。")
        for c in past:
            mark = ""
            if c.get("retired_on"):
                mark = f"（{c['retired_on']} 起停用"
                mark += f"，被 {c['replaced_by']} 接替）" if c.get("replaced_by") \
                    else (f"：{c['retired_reason']}）" if c.get("retired_reason")
                          else "）")
            print(f"{c['card_id']}  {c['as_of']}  → {philosophy.arm_name(c)} {mark}")
            print(f"    原话：{c['source_utterance']}")
            if c.get("replaces"):
                print(f"    改自：{c['replaces']}")
            for d in c.get("directives") or []:
                print(f"    · {d}")
            print(f"    必填字段：{'、'.join(philosophy.require_keys(c)) or '（无）'}")
        for f in sorted(pending.glob("*.json")) if pending.exists() else []:
            print(f"[待确认] {f.stem}  —  ideagen philosophy activate {f.stem}")
        # Sentences written on the panel and not yet distilled. They live in the
        # same place the panel keeps them, so the two surfaces cannot disagree
        # about what is on the desk.
        from . import philosophy_web
        for d in philosophy_web._drafts():
            print(f"[草稿] {d['id']}  {d['saved_at'][:16]}  {d['say']}")
        return 0

    if args.action == "propose":
        if not args.say:
            print("要注入什么？用 --say \"一句话\"", file=sys.stderr)
            return 2
        plat = platform_mod.load()
        card, bad = philosophy.distill(args.say, plat.inference, arm=args.arm,
                                       as_of=as_of, known_arms=arms)
        print(json.dumps(card, ensure_ascii=False, indent=2))
        print("\n—— 体检 ——")
        if bad:
            for b in bad:
                print(f"  ✗ {b}")
            print("\n未通过，没有写入待确认区。改一句话再试，或换一个更具体的说法。")
            return 1
        print("  ✓ 未触碰不可注入区，与初心不冲突，准则已落成可判定字段")
        tr = philosophy.translations(card)
        if tr:
            print("\n—— 你这句话有地方碰到硬边界，被改写了，请过目 ——")
            for t in tr:
                print(f"  ⚠ {t}")
            print("  改写不是你说的话。确认这就是你的意思，生效时才加 "
                  "--accept-translation；不是的话换一种说法重提。")
        pending.mkdir(parents=True, exist_ok=True)
        out = pending / f"{card['card_id']}.json"
        out.write_text(json.dumps(card, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        print(f"\n已存为待确认：{out.relative_to(config.ROOT)}")
        print(f"确认生效：ideagen philosophy activate {card['card_id']}")
        print(f"生效后会新增一种生成方式 {philosophy.arm_name(card)}，"
              f"与 {args.arm} 同批同料并跑；{args.arm} 本身不动。")
        return 0

    if args.action == "activate":
        f = pending / f"{args.card_id}.json"
        if not f.exists():
            print(f"待确认区里没有 {args.card_id}", file=sys.stderr)
            return 2
        card = json.loads(f.read_text(encoding="utf-8"))
        philosophy.activate(card, known_arms=arms,
                            accept_translations=args.accept_translation)
        f.unlink()
        print(f"已生效：{philosophy.arm_name(card)}（{card['as_of']} 起）")
        print("下一次周跑会多出这一种生成方式。原来那种一字不改继续跑，作为对照。")
        return 0

    if args.action == "retire":
        philosophy.retire(args.card_id, as_of, args.reason or "")
        print(f"{args.card_id} 自 {as_of.isoformat()} 起停用；"
              "它已经写下的持仓与业绩留在组合里。")
        return 0

    print(f"未知动作 {args.action!r}", file=sys.stderr)
    return 2


# ---------------------------------------------------------------- parser
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser("ideagen", description="IdeaGen40 daily pipeline")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        s = sub.add_parser(name, help=help_)
        s.set_defaults(fn=fn)
        s.add_argument("--as-of", help="YYYY-MM-DD (default: today HKT)")
        return s

    add("doctor", cmd_doctor, "check every dependency")

    s = add("ingest", cmd_ingest, "pull Wisburg corpus into documents")
    s.add_argument("--lookback", type=int, default=config.OBSERVATION_WINDOW_DAYS)
    s.add_argument("--bodies", type=int, default=6,
                   help="deep-fetch N Tier1/2 items per line for N's causal depth")

    s = add("olive-ingest", cmd_olive_ingest, "ingest an Olive shelf snapshot")
    s.add_argument("file", help="JSON file, or - for stdin")

    s = add("mcp-check", cmd_mcp_check,
            "handshake with Wisburg/Olive MCP and list tools")
    s.add_argument("--provider", choices=["all", "wisburg", "olive"],
                   default="all")
    s.add_argument("--json", action="store_true")

    s = add("olive-pull", cmd_olive_pull,
            "capture an authenticated Olive MCP shelf snapshot")
    s.add_argument("--details", type=int, default=0,
                   help="deep-fetch the first N products (4 calls each)")
    s.add_argument("--only",
                   help="comma-separated product codes to deep-fetch")
    s.add_argument("--out", type=Path,
                   help="snapshot path (default data/snapshots/olive_mcp_DATE.json)")
    s.add_argument("--ingest", action="store_true",
                   help="also persist to local SQLite or cloud RDS/TOS")

    s = add("olive-auth", cmd_olive_auth,
            "authorize Olive MCP through Noah SSO and PKCE")
    s.add_argument("--env-file", type=Path, default=Path(".byteplus.env"))
    s.add_argument("--timeout", type=int, default=600,
                   help="seconds to wait for the localhost OAuth callback")
    s.add_argument("--no-open", action="store_true",
                   help="print the authorization URL without opening a browser")
    s.add_argument("--url", default="",
                   help="Olive MCP endpoint; the OAuth issuer is discovered "
                        "from it when OLIVE_OAUTH_ISSUER is unset")

    s = add("lookthrough", cmd_lookthrough,
            "ETF 穿透：标签撒谎在哪、主题该买哪只、一组持仓真实押在什么上")
    s.add_argument("action",
                   choices=["refresh", "collisions", "theme", "portfolio",
                            "discover"])
    s.add_argument("--symbols", help="逗号分隔；refresh 时限定范围，"
                                     "portfolio 时是要穿透的那组持仓")
    s.add_argument("--names", help="theme 动作的底层名单，逗号分隔")
    s.add_argument("--limit", type=int, default=15)

    s = add("macro", cmd_macro,
            "宏观层：日历一致预期 · 隐含波动率 · CFTC 持仓 · 状态记录")
    s.add_argument("action",
                   choices=["status", "refresh", "surprises", "positioning",
                            "regime"])
    s.add_argument("--days", type=int, default=14,
                   help="surprises 回看多少天")
    s.add_argument("--symbols", help="positioning 限定标的，逗号分隔")
    s.add_argument("--force", action="store_true",
                   help="refresh 时强制重拟合误差分布（9 次调用）")

    s = add("prices", cmd_prices, "sync Futu daily bars")
    s.add_argument("--days", type=int, default=400)
    s.add_argument("--verbose", action="store_true")
    s.add_argument("--retry-blocked", action="store_true")

    s = add("score", cmd_score, "compute D/A/B/N/M/C for every theme")
    s.add_argument("--force", action="store_true",
                   help="re-score even if a batch was already traded against this date")
    s = add("philosophy", cmd_philosophy,
            "PM 一句话注入：蒸馏成准则卡，派生一种与原方式并跑的新生成方式")
    s.add_argument("action", choices=["list", "propose", "activate", "retire"])
    s.add_argument("card_id", nargs="?", help="activate / retire 的卡号")
    s.add_argument("--say", help="PM 的原话，一句就够")
    s.add_argument("--arm", default="carl_constraint",
                   help="注入到哪种生成方式上（默认 carl_constraint）")
    s.add_argument("--reason", help="retire 的原因")
    s.add_argument("--accept-translation", action="store_true",
                   help="确认蒸馏对硬边界处的改写就是你的意思")

    s = add("weekly", cmd_weekly, "one weekly run: 筛选A → 筛选B → 筛选C")
    s.add_argument("--trade", action="store_true",
                   help="book each selector's picks into its paper book after the run")
    s.add_argument("--topic-scorer", default="hgep")
    s.add_argument("--generators", help="comma-separated; default every registered one")
    s.add_argument("--selectors", help="comma-separated; default every registered one")
    s.add_argument("--dry-run", action="store_true",
                   help="run the strategies, persist nothing")
    s.add_argument("--classification", default="live",
                   choices=("live", "backfill"),
                   help="backfill = 事后补跑的历史期。文档层面卡死了 as-of，但模型"
                        "权重见过该日期之后的世界，这一点无法用代码消除，只能标注")

    s = add("book", cmd_book, "book a completed run into the selector paper books")
    s.add_argument("--run-id", help="default: the latest completed weekly run")

    s = add("platform", cmd_platform, "platform port health (run this first)")
    s.add_argument("--platform", choices=["local", "byteplus"],
                   help="override IDEAGEN_PLATFORM")
    s.add_argument("--env", action="store_true", help="also list platform env vars")
    s.add_argument("--probe", action="store_true",
                   help="write and read back a real artifact")
    s.add_argument("--state-probe", action="store_true",
                   help="create POC tables, write one RDS probe row, and read it back")
    s.add_argument("--inference-probe", action="store_true",
                   help="make one minimal model call without printing its content")

    s = add("poc-load-public-mock", cmd_poc_load_public_mock,
            "archive the public synthetic POC fixture to TOS and load it into RDS")
    s.add_argument("--fixture", type=Path, default=poc_fixture.DEFAULT_PATH,
                   help="public synthetic fixture JSON")
    s.add_argument("--verify-only", action="store_true",
                   help="validate and hash the fixture without cloud writes")

    add("poc-load-shelf-fixture", cmd_poc_load_shelf_fixture,
        "persist a versioned public shelf fixture through RDS/TOS")

    s = add("cloud-ingest", cmd_cloud_ingest,
            "explicitly ingest Wisburg into portable RDS/TOS state")
    s.add_argument("--incremental", action="store_true",
                   help="first-page incremental pull instead of the full window")
    s.add_argument("--details", type=int, default=3,
                   help="maximum detail documents to archive")

    add("cloud-monitor", cmd_cloud_monitor,
        "mark portable RDS paper books from persisted NAVs")

    s = add("poc-run-weekly", cmd_poc_run_weekly,
            "run real ModelArk weekly over one explicit input mode")
    s.add_argument("--mode", choices=poc_workflow.WEEKLY_MODES,
                   default="public-synthetic")

    s = add("poc-backtest", cmd_poc_backtest,
            "run and persist the public synthetic three-month replay")
    s.add_argument("--weeks", type=int, default=13)
    s.add_argument("--horizon-days", type=int, default=30)

    s = add("poc-run-all", cmd_poc_run_all,
            "run the POC weekly first, then the deterministic replay")
    s.add_argument("--mode", choices=poc_workflow.WEEKLY_MODES,
                   default="public-synthetic")
    s.add_argument("--weeks", type=int, default=13)
    s.add_argument("--horizon-days", type=int, default=30)

    s = add("replay", cmd_replay,
            "rebuild scores/packs/batches/trades for a range, in as-of order")
    s.add_argument("--start", required=True)
    s.add_argument("--end", required=True)
    s.add_argument("--quiet", action="store_true")

    s = add("rebuild-batch", cmd_rebuild_batch,
            "re-trade a batch whose positions disagree with its ideas")
    s.add_argument("batch_id")

    s = add("theme-candidates", cmd_theme_candidates,
            "macro debates the dictionary has no word for")
    s.add_argument("--limit", type=int, default=themes.MAX_CANDIDATES)
    s.add_argument("--json", action="store_true")

    s = add("theme-register", cmd_theme_register,
            "append discovered theme(s) to themes/registry.jsonl")
    s.add_argument("file", help="JSON object or array, or - for stdin")

    add("brief", cmd_brief, "build the generator briefing pack")

    s = add("seed", cmd_seed, "import the historical 2026-07-27 pack as batch #1")
    s.add_argument("--trade", action="store_true", help="also open both books on it")

    add("verify-seed", cmd_verify_seed, "audit the pack's odds worksheet")

    s = add("ingest-batch", cmd_ingest_batch, "validate and store a generated batch")
    s.add_argument("file")
    s.add_argument("--generator", default="claude-code")
    s.add_argument("--no-trade", dest="trade", action="store_false", default=True)

    s = add("mark", cmd_mark, "advance both books through the sessions")
    s.add_argument("--since")
    s.add_argument("--to")

    s = add("monitor", cmd_monitor, "generate position alerts")
    s.add_argument("--on")

    s = add("settle", cmd_settle, "write outcome rows for every idea")
    s.add_argument("--book", default="naive")

    s = add("report", cmd_report, "print the attribution report")
    s.add_argument("--json", help="also write the full report to this path")

    s = add("dashboard", cmd_dashboard, "build web/index.html")
    s.add_argument("--out")
    s.add_argument("--artifact", action="store_true",
                   help="body-only markup for the Claude Artifact publisher")
    s.add_argument("--public", action="store_true",
                   help="public build: link Wisburg charts instead of embedding them")

    s = add("backfill", cmd_backfill, "build one report + 40-idea batch per session")
    s.add_argument("--start", required=True)
    s.add_argument("--end", required=True)
    s.add_argument("--no-ingest", action="store_true")
    s.add_argument("--bodies", type=int, default=4)

    s = add("generate", cmd_generate, "rule-based batch for one date")
    s.add_argument("--no-trade", dest="trade", action="store_false", default=True)

    s = add("sources", cmd_sources, "provenance audit: trace every claim back")
    s.add_argument("--verify", action="store_true",
                   help="HEAD unverified asset URLs before reporting")
    s.add_argument("--limit", type=int, default=200)
    s.add_argument("--doc", help="print the full chain for one doc_id")

    s = add("serve", cmd_serve, "serve the dashboard on localhost, rebuilt per request")
    s.add_argument("--port", type=int, default=serve_mod.DEFAULT_PORT)
    s.add_argument("--open", action="store_true", help="open a browser too")

    s = add("daily", cmd_daily, "run the whole unattended cycle")
    s.add_argument("--lookback", type=int, default=config.OBSERVATION_WINDOW_DAYS)
    s.add_argument("--bodies", type=int, default=6)

    s = add("status", cmd_status, "compact digest as JSON")
    s.add_argument("--on")

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
