"""IdeaGen40 command line.

The daily cycle, in order:

    ideagen doctor                  # OpenD / Wisburg / DB liveness
    ideagen ingest                  # Wisburg -> documents
    ideagen olive-ingest <file>     # Olive shelf snapshot -> instruments + navs
    ideagen prices                  # Futu -> prices
    ideagen score                   # D/A/B/N/M/C -> themes
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
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from . import (analytics, backfill, briefing, config, db, generator,
               ideas as ideas_mod, lexicon, monitor, paper, report as report_mod,
               scoring, seed, serve as serve_mod, universe)
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
    con = _con()
    raw = (sys.stdin.read() if args.file == "-"
           else Path(args.file).read_text(encoding="utf-8"))
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = raw                      # olive._as_list handles wrapped text
    rep = olive.ingest(con, payload, as_of=_as_of(args))
    print(json.dumps({k: v for k, v in rep.items() if k != "snapshot"},
                     ensure_ascii=False))
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
    print(f"  语料      {d['n']:,} 条")
    print(f"    可复现检索式 {d['receipt']:,} ({d['receipt']/max(d['n'],1)*100:.0f}%)"
          f"   内容哈希 {d['hash']:,} (100%)"
          f"   发布时间 {d['ts']:,} (100%)")
    print(f"    具名机构     {d['inst']:,} ({d['inst']/max(d['n'],1)*100:.0f}%)"
          f"   已深取正文 {d['deep']:,}")
    print(f"  资产      {ast['n']} 个图表/插图，覆盖 {ast['docs']} 篇")
    print(f"    已验证可达   {ast['ok']}   不可达 {ast['bad']}   未验证 {ast['unchecked']}")
    print(f"  引用      共 {c['total']} 条")
    print(f"    可解析到语料 {c['resolved']}   散文式归属 {c['prose']}"
          f"   悬空引用 {c['dangling']}")
    if c["dangling"]:
        print(f"    ⚠ {c['dangling']} 条引用有 doc_id 形状但不在语料库里——这是真缺陷")
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
    print("[1/8] wisburg ingest")

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
    print("[2/8] prices")
    stage("prices", lambda: futu_px.sync(
        con, universe.priceable_codes(lexicon.all_indicators()),
        as_of - timedelta(days=400), as_of))
    print("[3/8] score themes")
    stage("score", lambda: scoring.score_day(con, as_of))   # skips a traded date
    print("[4/8] briefing pack")
    stage("brief", lambda: briefing.build(con, as_of))
    print("[5/8] mark books (含每日组合)")
    stage("mark", lambda: cmd_mark(argparse.Namespace(since=None, to=None)))
    print("[6/8] monitor")
    stage("monitor", lambda: monitor.run(con))
    print("[7/8] verify source assets")
    stage("verify-assets", lambda: wisburg.verify_assets(con, limit=120))
    print("[8/8] settle + dashboard")
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

    s = add("prices", cmd_prices, "sync Futu daily bars")
    s.add_argument("--days", type=int, default=400)
    s.add_argument("--verbose", action="store_true")
    s.add_argument("--retry-blocked", action="store_true")

    s = add("score", cmd_score, "compute D/A/B/N/M/C for every theme")
    s.add_argument("--force", action="store_true",
                   help="re-score even if a batch was already traded against this date")
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
