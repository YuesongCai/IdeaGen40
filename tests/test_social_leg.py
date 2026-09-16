"""早期信号腿（WS-C）：社交源适配器 + 扩散诊断的契约。

yifu 2026-09-11：研报处在扩散期，要抓源头就得接独立作者 / Reddit / X。这条腿
能出错的地方都是本仓已经交过学费的形状，所以测的是这些：

1. **读不到 ≠ 没有**：X 没钥匙写「未启用：需要 X_BEARER_TOKEN」，Reddit 无钥
   路径 429 写限流，一个 RSS 源挂掉不拖垮其余源——都是状态行，不是空表。
2. **后验纪律**：`items_as_of` 按*发布*时间截断（HKT 当日末），不按抓取时间；
   已入库条目的标题/摘要不被后来的抓取改写。
3. **不进 TIS**：社交条目在自己的表里，`SOCIAL_WEIGHT` 为 0，打分模块不读它。
4. **英文整词匹配**：「war」不能命中「software」——研报的子串匹配搬到英文上就错。
5. **阶段与领先天数**：合成序列上源头期 / 扩散期 / 晚期各判得出来，领先天数符号对。
6. **热议未进研报**：多源 + 新出现 + 研报没跟三道门都要过；已登记主题的词不算。
7. **发现只收提示**：`themes.discover` 把提示记进 journal，不注册任何东西。

全部夹具在本文件里，测试不联网。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

os.environ.setdefault("WISBURG_MCP_URL", "https://research.example/mcp")
os.environ.setdefault("OLIVE_MCP_URL", "https://catalog.example/mcp")

from ideagen import config, db, diffusion, lexicon  # noqa: E402
from ideagen.sources import social  # noqa: E402

UTC = timezone.utc
FETCHED = "2026-09-17T01:00:00Z"

RSS = b"""<?xml version="1.0"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"><channel>
<title>Macro Blog</title><language>en-us</language>
<item><title>The &lt;b&gt;Fed&lt;/b&gt; and the term premium</title>
<link>https://blog.example/p/fed?utm_source=rss</link>
<pubDate>Tue, 15 Sep 2026 14:00:00 +0000</pubDate>
<dc:creator>Jane Writer</dc:creator>
<description>&lt;p&gt;Rate cut odds and the yield curve.&lt;/p&gt;</description></item>
<item><title>Undated post</title><link>https://blog.example/p/undated</link></item>
<item><title>Late night post</title><link>https://blog.example/p/late</link>
<pubDate>Wed, 16 Sep 2026 17:30:00 +0000</pubDate><description>x</description></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>r/investing</title>
<entry><author><name>/u/someone</name></author><title>Copper squeeze?</title>
<link href="https://www.reddit.com/r/investing/comments/abc/copper/"/>
<published>2026-09-14T08:00:00+00:00</published><updated>2026-09-14T09:00:00+00:00</updated>
<content type="html">&lt;div&gt;copper inventory is falling&lt;/div&gt;</content></entry>
</feed>"""


def _substack_json(dates: list[str]) -> bytes:
    return json.dumps([{"post_date": d, "title": f"Old post {d[:10]}", "subtitle": "sub",
                        "canonical_url": f"https://blog.example/p/old-{d[:10]}",
                        "publishedBylines": [{"name": "Jane Writer"}],
                        "reaction_count": 5, "comment_count": 2} for d in dates]).encode()


class FakeHttp:
    """URL-prefix → (status, body). Records every call; any unknown URL is 404."""

    def __init__(self, routes: dict[str, tuple[int, bytes]]):
        self.routes = routes
        self.calls: list[str] = []

    def __call__(self, url, *, headers=None, data=None, method=None, timeout=None):
        self.calls.append(url)
        for prefix in sorted(self.routes, key=len, reverse=True):
            if url.startswith(prefix):
                st, body = self.routes[prefix]
                return st, body, {}
        return 404, b"", {}


def _theme(tid: str, terms: tuple[str, ...], label: str = "") -> lexicon.Theme:
    return lexicon.Theme(id=tid, label=label or tid, key_question="?", terms=terms,
                         price_indicator="US.SPY")


def _con():
    con = db.init(":memory:")
    social.ensure_tables(con)
    return con


def _feeds_file(tmp: Path, rss: list[dict], subs: list[str] | None = None) -> Path:
    p = tmp / "feeds.json"
    p.write_text(json.dumps({"rss": rss, "reddit": {"subreddits": subs or []},
                             "x": {"kols": []}}), encoding="utf-8")
    return p


class ParsingAndStorage(unittest.TestCase):
    def test_rss_items_are_normalised_and_undated_ones_dropped(self):
        items = social.parse_feed(RSS, source="rss", feed="macro", fetched_at=FETCHED)
        self.assertEqual(len(items), 2, "没有发布时间的条目必须丢掉，不能拿抓取时间顶")
        it = items[0]
        self.assertEqual(it["title"], "The Fed and the term premium")
        self.assertEqual(it["summary"], "Rate cut odds and the yield curve.")
        self.assertEqual(it["author"], "Jane Writer")
        self.assertEqual(it["published_at"], "2026-09-15T14:00:00Z")
        self.assertEqual(it["published_d"], "2026-09-15")
        self.assertEqual(it["lang"], "en")
        # 17:30 UTC 是 HKT 次日 01:30——日轴必须和研报一样是 HKT。
        self.assertEqual(items[1]["published_d"], "2026-09-17")
        # utm 参数不改变身份：同一篇文章从两个入口进来只算一条。
        self.assertEqual(it["item_id"], social.item_id_for(
            "https://blog.example/p/fed", "other", "t", "x"))

    def test_atom_entry(self):
        items = social.parse_feed(ATOM, source="reddit", feed="r/investing", fetched_at=FETCHED)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["author"], "/u/someone")
        self.assertIn("copper inventory", items[0]["summary"])
        self.assertEqual(items[0]["published_at"], "2026-09-14T08:00:00Z")

    def test_store_never_rewrites_what_was_first_seen(self):
        con = _con()
        pub = datetime(2026, 9, 10, 12, tzinfo=UTC)
        a = social.make_item(source="rss", feed="f", title="Original", url="https://x.example/1",
                             published=pub, fetched_at="2026-09-10T13:00:00Z",
                             engagement={"reactions": 1})
        self.assertEqual(social.store(con, [a]), 1)
        b = social.make_item(source="rss", feed="f", title="Edited later", url="https://x.example/1",
                             published=pub, fetched_at="2026-09-16T13:00:00Z",
                             engagement={"reactions": 99})
        self.assertEqual(social.store(con, [b]), 0)
        row = db.q1(con, "SELECT * FROM social_items")
        self.assertEqual(row["title"], "Original")
        self.assertEqual(row["fetched_at"], "2026-09-10T13:00:00Z")
        self.assertEqual(json.loads(row["engagement"])["reactions"], 99)
        self.assertEqual(row["last_seen_at"], "2026-09-16T13:00:00Z")

    def test_items_as_of_cuts_on_publication_time_at_hkt_day_end(self):
        con = _con()
        mk = lambda u, dt: social.make_item(  # noqa: E731
            source="rss", feed="f", title=u, url=f"https://x.example/{u}", published=dt,
            fetched_at="2026-09-17T00:00:00Z")   # all fetched *today*
        social.store(con, [
            mk("before", datetime(2026, 9, 1, 15, 59, tzinfo=UTC)),   # 09-01 23:59 HKT
            mk("after", datetime(2026, 9, 1, 16, 1, tzinfo=UTC)),     # 09-02 00:01 HKT
        ])
        got = [r["title"] for r in social.items_as_of(con, date(2026, 9, 1))]
        self.assertEqual(got, ["before"], "截断看发布时间：今天才抓到的旧文，回放旧期时照样可见")
        self.assertEqual(len(social.items_as_of(con, "2026-09-02")), 2)


class Ingest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.now = datetime(2026, 9, 17, 9, 0, tzinfo=config.TZ)

    def test_one_dead_feed_does_not_stop_the_rest_and_x_says_why(self):
        feeds = _feeds_file(self.tmp, [
            {"key": "good", "name": "Good", "url": "https://good.example/feed", "paging": ""},
            {"key": "dead", "name": "Dead", "url": "https://dead.example/feed", "paging": ""},
        ])
        http = FakeHttp({"https://good.example/feed": (200, RSS),
                         "https://dead.example/feed": (500, b"")})
        con = _con()
        with mock.patch.dict(os.environ, {"X_BEARER_TOKEN": "", "REDDIT_CLIENT_ID": "",
                                          "REDDIT_CLIENT_SECRET": ""}):
            rep = social.ingest(con, http=http, feeds_path=feeds, now=self.now,
                                log=lambda m: None, sleep=lambda s: None)
            st = social.status(con, now=self.now)
        self.assertEqual(rep["sources"]["rss"]["ok"], 1)
        self.assertEqual(rep["sources"]["rss"]["failed"], 1)
        self.assertEqual(rep["n_new"], 2)
        by = {s["source"]: s for s in st["sources"]}
        self.assertFalse(by["x"]["enabled"])
        self.assertEqual(by["x"]["reason"], "未启用：需要 X_BEARER_TOKEN")
        self.assertTrue(by["rss"]["ok"])
        self.assertIn("1 个源失败", by["rss"]["reason"])
        dead = [f for f in st["rss_feeds"] if f["feed"] == "dead"]
        # status 读的是 SOCIAL_FEEDS_PATH 的清单，这里只核对状态表里有那一行
        row = db.q1(con, "SELECT ok, error FROM social_sources WHERE feed='dead'")
        self.assertEqual(row["ok"], 0)
        self.assertIn("HTTP 500", row["error"])
        self.assertIsInstance(dead, list)
        self.assertFalse(st["in_tis"])
        rows = social.feed_rows(con, now=self.now)
        self.assertEqual({r["kind"] for r in rows}, {"social"})
        xrow = [r for r in rows if r["feed"] == "social-x"][0]
        self.assertEqual(xrow["error"], "未启用：需要 X_BEARER_TOKEN")

    def test_paging_walks_back_until_items_predate_the_window(self):
        feeds = _feeds_file(self.tmp, [{"key": "sub", "name": "Sub",
                                        "url": "https://sub.example/feed", "paging": "substack"}])
        page1 = _substack_json(["2026-09-01T10:00:00Z", "2026-08-25T10:00:00Z"])
        page2 = _substack_json(["2026-08-10T10:00:00Z", "2026-07-01T10:00:00Z"])
        http = FakeHttp({"https://sub.example/feed": (200, RSS),
                         "https://sub.example/api/v1/archive?sort=new&offset=0": (200, page1),
                         "https://sub.example/api/v1/archive?sort=new&offset=25": (200, page2)})
        con = _con()
        social.ingest(con, http=http, feeds_path=feeds, now=self.now, sources=["rss"],
                      lookback_days=30, log=lambda m: None)
        days = sorted(r["published_d"] for r in db.q(con, "SELECT published_d FROM social_items"))
        self.assertIn("2026-08-25", days)
        self.assertNotIn("2026-08-10", days, "早于回看窗口的条目不入库")
        self.assertEqual(len([c for c in http.calls if "offset=50" in c]), 0,
                         "翻到窗口之前就该停，不再往后请求")

    def test_reddit_keyless_stops_at_first_429(self):
        feeds = _feeds_file(self.tmp, [], subs=["investing", "stocks", "economics"])
        http = FakeHttp({"https://www.reddit.com/r/investing/": (200, ATOM),
                         "https://www.reddit.com/r/stocks/": (429, b"")})
        con = _con()
        with mock.patch.dict(os.environ, {"REDDIT_CLIENT_ID": "", "REDDIT_CLIENT_SECRET": ""}):
            rep = social.ingest(con, http=http, feeds_path=feeds, now=self.now,
                                sources=["reddit"], log=lambda m: None, sleep=lambda s: None)
        self.assertEqual(rep["sources"]["reddit"]["mode"], "rss")
        self.assertFalse(any("economics" in c for c in http.calls), "429 之后不该继续打")
        row = db.q1(con, "SELECT * FROM social_sources WHERE feed='social-reddit'")
        self.assertIn("429", row["reason"])
        self.assertIn("REDDIT_CLIENT_ID", row["reason"])

    def test_reddit_oauth_path_with_credentials(self):
        feeds = _feeds_file(self.tmp, [], subs=["investing"])
        listing = json.dumps({"data": {"children": [
            {"data": {"title": "Bessent and treasury buybacks", "permalink": "/r/investing/c/1/",
                      "created_utc": 1789900000, "author": "u1", "selftext": "",
                      "score": 12, "num_comments": 3}},
            {"data": {"title": "Daily thread", "stickied": True, "created_utc": 1789900000}}]}}).encode()
        http = FakeHttp({"https://www.reddit.com/api/v1/access_token": (200, b'{"access_token":"t"}'),
                         "https://oauth.reddit.com/r/investing/new": (200, listing)})
        con = _con()
        with mock.patch.dict(os.environ, {"REDDIT_CLIENT_ID": "id", "REDDIT_CLIENT_SECRET": "sec"}):
            rep = social.ingest(con, http=http, feeds_path=feeds, now=self.now,
                                sources=["reddit"], log=lambda m: None, lookback_days=400)
        self.assertEqual(rep["sources"]["reddit"]["mode"], "oauth")
        row = db.q1(con, "SELECT * FROM social_items")
        self.assertEqual(json.loads(row["engagement"]), {"score": 12, "comments": 3})
        self.assertEqual(db.q1(con, "SELECT COUNT(*) n FROM social_items")["n"], 1, "置顶帖不是讨论")

    def test_x_queries_use_english_terms_only_and_fit_the_limit(self):
        ths = [_theme("A", ("联储", "Fed", "rate cut") + tuple(f"term{i:03d}" for i in range(200))),
               _theme("ZH", ("中文词", "另一个"))]
        qs = social.x_queries(ths, kols=["@alice", "bob"])
        labels = [q[0] for q in qs]
        self.assertEqual(labels, ["A", "kol"], "只有中文词项的主题不发空查询")
        self.assertTrue(all(len(q) <= social.X_MAX_QUERY for _, q in qs))
        self.assertIn('"rate cut"', qs[0][1])
        self.assertNotIn("联储", qs[0][1])
        self.assertIn("from:alice OR from:bob", qs[1][1])

    def test_x_budget_is_spent_once_per_day(self):
        body = json.dumps({"data": [{"id": "1", "text": "Fed rate cut soon", "author_id": "9",
                                     "created_at": "2026-09-16T01:00:00Z", "lang": "en",
                                     "public_metrics": {"like_count": 4}}],
                           "includes": {"users": [{"id": "9", "username": "macroguy"}]}}).encode()
        http = FakeHttp({social.X_SEARCH: (200, body)})
        con = _con()
        ths = [_theme(f"T{i}", ("Fed",)) for i in range(5)]
        with mock.patch.dict(os.environ, {"X_BEARER_TOKEN": "tok"}):
            items, st = social.fetch_x(con, ths, [], http=http, fetched_at=FETCHED,
                                       today=date(2026, 9, 17), budget=3)
            self.assertEqual(st["requests"], 3)
            self.assertEqual(items[0]["author"], "@macroguy")
            self.assertEqual(items[0]["url"], "https://x.com/macroguy/status/1")
            _, st2 = social.fetch_x(con, ths, [], http=http, fetched_at=FETCHED,
                                    today=date(2026, 9, 17), budget=3)
        self.assertIn("预算", st2["reason"])
        self.assertEqual(len(http.calls), 3)

    def test_monitor_tick_is_throttled_and_never_raises(self):
        con = _con()
        db.kv_set(con, "social.monitor.last_attempt", self.now.isoformat())
        out = social.monitor_tick(con, self.now + timedelta(minutes=15))
        self.assertIn("skipped", out)

        def boom(*a, **k):
            raise OSError("network down")
        with mock.patch.object(social, "ingest", side_effect=boom):
            out = social.monitor_tick(con, self.now + timedelta(days=1))
        self.assertIn("failed", out)


class NotInScoring(unittest.TestCase):
    def test_weight_is_zero_and_scoring_never_reads_social_items(self):
        self.assertEqual(config.SOCIAL_WEIGHT, 0)
        root = Path(__file__).resolve().parent.parent / "ideagen"
        for name in ("scoring.py", "lexicon.py"):
            self.assertNotIn("social", (root / name).read_text(encoding="utf-8"),
                             f"{name} 读了社交数据——社交腿不进 TIS")


def _seed_series(con, theme_terms_social: str, theme_terms_research: str, as_of: date,
                 social_by_day: dict[int, int], research_by_day: dict[int, int],
                 filler_social: int = 6, filler_research: int = 20, feed_prefix: str = "f"):
    """Day offsets are days before `as_of` (0 = as_of)."""
    items, docs = [], []
    for back in range(0, 28):
        d = as_of - timedelta(days=back)
        pub = datetime(d.year, d.month, d.day, 4, tzinfo=UTC)   # 12:00 HKT same day
        for k in range(social_by_day.get(back, 0)):
            items.append(social.make_item(source="rss", feed=f"{feed_prefix}{k % 3}",
                                          title=theme_terms_social, url=f"https://s.example/{back}/{k}",
                                          published=pub, fetched_at=FETCHED, author=f"a{k % 3}"))
        for k in range(filler_social):
            items.append(social.make_item(source="rss", feed=f"{feed_prefix}{k % 3}",
                                          title=f"unrelated gardening note {back} {k}",
                                          url=f"https://s.example/n/{back}/{k}",
                                          published=pub, fetched_at=FETCHED))
        for k in range(research_by_day.get(back, 0)):
            docs.append((f"r{back}-{k}", d.isoformat(), theme_terms_research))
        for k in range(filler_research):
            docs.append((f"n{back}-{k}", d.isoformat(), "无关的行业点评"))
    social.store(con, items)
    for doc_id, d, title in docs:
        con.execute("INSERT INTO documents(doc_id,line,tier,title,published_d,ingested_at,summary)"
                    " VALUES(?,?,?,?,?,?,?)", (doc_id, "feed", 2, title, d, FETCHED, ""))


class Diffusion(unittest.TestCase):
    AS_OF = date(2026, 9, 16)

    def test_word_bounded_matching(self):
        m = diffusion.SocialMatcher(_theme("G", ("war", "tariff", "关税")))
        self.assertFalse(m.mentions("Software award season", ""), "war 不在 software 里")
        self.assertTrue(m.mentions("Trade war returns", ""))
        self.assertFalse(m.mentions("Weekly links", "a war is mentioned once in a short blurb"),
                         "摘要里孤零零一个词不算提及")
        self.assertTrue(m.mentions("Weekly links", "war and tariff escalation"))
        self.assertTrue(m.mentions("关税升级", ""))
        self.assertFalse(diffusion.SocialMatcher(_theme("Z", ("中文",))).has_english)

    def test_source_stage_social_up_research_flat(self):
        con = _con()
        th = _theme("COPPER", ("copper", "铜"))
        # social: nothing in the prior 14 days, 3/day in the last 7; research flat 2/day.
        _seed_series(con, "Copper squeeze building", "铜 库存", self.AS_OF,
                     {b: 3 for b in range(0, 7)}, {b: 2 for b in range(0, 28)})
        d = diffusion.diagnose(con, self.AS_OF, themes=[th])
        t = d["themes"][0]
        self.assertEqual(t["stage"], diffusion.STAGE_SOURCE, t["stage_why"])
        self.assertEqual(t["social"]["recent"], 21)
        self.assertTrue(d["early_voices"]["framework"])

    def test_spread_and_late_stages(self):
        con = _con()
        th = _theme("COPPER", ("copper", "铜"))
        _seed_series(con, "Copper squeeze building", "铜 库存", self.AS_OF,
                     {b: 3 for b in range(0, 7)}, {b: (6 if b < 7 else 1) for b in range(0, 28)})
        self.assertEqual(diffusion.diagnose(con, self.AS_OF, themes=[th])["themes"][0]["stage"],
                         diffusion.STAGE_SPREAD)
        con2 = _con()
        _seed_series(con2, "Copper squeeze building", "铜 库存", self.AS_OF,
                     {b: (3 if b >= 7 else 0) for b in range(0, 21)},
                     {b: 4 for b in range(0, 28)})
        t = diffusion.diagnose(con2, self.AS_OF, themes=[th])["themes"][0]
        self.assertEqual(t["stage"], diffusion.STAGE_LATE, t["stage_why"])

    def test_thin_and_unmatchable_are_said_in_words(self):
        con = _con()
        _seed_series(con, "Copper once", "铜", self.AS_OF, {3: 1}, {b: 2 for b in range(28)},
                     filler_social=2)
        d = diffusion.diagnose(con, self.AS_OF, themes=[_theme("C", ("copper",)),
                                                         _theme("ZH", ("铜",))])
        by = {t["theme_id"]: t for t in d["themes"]}
        self.assertEqual(by["C"]["stage"], diffusion.STAGE_THIN)
        self.assertIn("少于", by["C"]["stage_why"])
        self.assertIn("词表无英文叫法", by["ZH"]["stage_why"])
        ev = d["early_voices"]
        self.assertFalse(ev["available"], "社交条目太少时早期声音如实空状态")
        self.assertIn("少于", ev["why"])
        self.assertTrue(ev["framework"])

    def test_lead_lag_sign(self):
        base = [0, 0, 0, 1, 3, 6, 9, 6, 3, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
        lagged = [0, 0, 0, 0, 0, 0] + base[:-6]
        k, c = diffusion.lead_lag([float(x) for x in base], [float(x) for x in lagged], 10)
        self.assertEqual(k, 6, "社交先起、研报 6 天后跟 → 社交领先 +6")
        self.assertGreater(c, 0.9)

    def test_chatter_gates(self):
        con = _con()
        ths = [_theme("POLICY", ("Fed", "rate cut"))]
        now = datetime(2026, 9, 15, 4, tzinfo=UTC)
        items = []
        # base weeks: plenty of unrelated posts so the lift gate is active
        for i in range(40):
            items.append(social.make_item(source="rss", feed=f"b{i % 4}", title=f"garden notes {i}",
                                          url=f"https://b.example/{i}", published=now - timedelta(days=10 + i % 14),
                                          fetched_at=FETCHED))
        for i, feed in enumerate(["alpha", "beta", "gamma"]):
            items.append(social.make_item(source="rss", feed=feed, title="Treasury buybacks are back",
                                          url=f"https://c.example/tb{i}", published=now, fetched_at=FETCHED,
                                          summary="Bessent said the programme grows."))
            items.append(social.make_item(source="rss", feed=feed, title="Fed rate cut odds",
                                          url=f"https://c.example/fed{i}", published=now, fetched_at=FETCHED))
            items.append(social.make_item(source="rss", feed="alpha", title=f"Solo hobby horse {i}",
                                          url=f"https://c.example/solo{i}", published=now, fetched_at=FETCHED))
        social.store(con, items)
        con.execute("INSERT INTO documents(doc_id,line,tier,title,published_d,ingested_at,summary)"
                    " VALUES('d1','feed',2,'无关研报','2026-09-15',?, '')", (FETCHED,))
        ch = diffusion.chatter(con, date(2026, 9, 16), themes=ths)
        phrases = [c["phrase"] for c in ch["items"]]
        self.assertIn("treasury buybacks", phrases)
        self.assertFalse(any("rate cut" in p or p == "fed" for p in phrases), "已登记主题的词不是新话题")
        self.assertFalse(any("hobby" in p for p in phrases), "只有一个源在说的不算热议")
        tb = [c for c in ch["items"] if c["phrase"] == "treasury buybacks"][0]
        self.assertEqual(tb["n_feeds"], 3)
        self.assertEqual(tb["research_docs"], 0)
        # research already covering it removes it
        for i in range(3):
            con.execute("INSERT INTO documents(doc_id,line,tier,title,published_d,ingested_at,summary)"
                        " VALUES(?,?,?,?,?,?,?)", (f"tb{i}", "feed", 2, "Treasury buybacks 解读",
                                                   "2026-09-15", FETCHED, ""))
        ch2 = diffusion.chatter(con, date(2026, 9, 16), themes=ths)
        self.assertNotIn("treasury buybacks", [c["phrase"] for c in ch2["items"]])

    def test_snapshot_state_block_picks_nearest_on_or_before(self):
        con = _con()
        with mock.patch.object(diffusion, "diagnose",
                               side_effect=lambda c, d: {"as_of": str(d), "themes": []}):
            diffusion.snapshot(con, "2026-09-09")
            diffusion.snapshot(con, "2026-09-16")
        self.assertEqual(diffusion.state_block(con, "2026-09-12")["as_of"], "2026-09-09")
        self.assertEqual(diffusion.state_block(con, None)["as_of"], "2026-09-16")
        early = diffusion.state_block(con, "2026-09-01")
        self.assertFalse(early["available"])
        self.assertIn("之前没有扩散诊断", early["why"])
        self.assertFalse(diffusion.state_block(_con())["available"])


class DiscoveryOnlyRecordsHints(unittest.TestCase):
    def test_hints_reach_the_journal_and_register_nothing(self):
        from ideagen import themes
        con = _con()
        steps = []
        with mock.patch.object(diffusion, "discovery_hints",
                               return_value=[{"phrase": "treasury buybacks", "n_items": 3,
                                              "n_feeds": 3, "research_docs": 0, "feeds": []}]):
            out = themes.discover(con, date(2026, 9, 16), None,
                                  step=lambda name, **f: steps.append((name, f)))
        self.assertEqual(out["registered"], [])
        final = [f for n, f in steps if n == "theme_discovery"][-1]
        self.assertEqual(final["social_hints"][0]["phrase"], "treasury buybacks")


if __name__ == "__main__":
    unittest.main()
