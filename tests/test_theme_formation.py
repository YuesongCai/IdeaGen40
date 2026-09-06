"""主题从当期研报出发：Jon 2026-09-06 反馈第 1 条落地后的契约。

在此之前，发现只从「没命中任何已注册主题」的研报里挖短语。一篇研报只要提到
一次「联储」就被 POLICY-PATH 认领，它真正在争的新东西（杰克逊霍尔讲话、联储主席
人选）永远进不了发现流程。这个文件盯住改动后的六件事：

1. **全窗口发现**：已命中旧主题的研报里的新簇挖得出来；旧口径（仅未命中）
   在同一份语料上挖不出来——两条并排跑，证明差异来自口径而不是语料。
2. **relation 规则**：簇的证据里有多少篇同时命中了哪个旧主题，按常量
   `SPLIT_SHARE` / `ADJACENT_SHARE` 判成 possible_split / adjacent / distinct。
3. **别名的 as-of 钳制**：别名 `as_of` 之前的打分日看不到它，之后能匹配。这是
   回看偏差的底线——回放更早的期不能用后来才学会的词。
4. **别名不能偷别的主题的词**：与 `validate` 同一条 stolen 规则。
5. **定义集快照**：注册或别名落在 as_of 之前就换 sha，落在之后 sha 不变，
   什么都不变时稳定。
6. **周跑循环**：假模型判 same_debate+new_terms → 不注册、追加别名、journal 有
   `theme_merge_note`；判 split → 注册行带 split_from 与 rationale；没有模型
   → 说一次原因、不注册。

不调用真实模型；模型路径用脚本化的假端口验契约。注册表与别名文件都指向临时
路径，测试结束恢复并重载，`themes/registry.jsonl` 一个字节都不动。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

os.environ.setdefault("WISBURG_MCP_URL", "https://research.example/mcp")
os.environ.setdefault("OLIVE_MCP_URL", "https://catalog.example/mcp")

from ideagen import db, lexicon, themes

AS_OF = date(2026, 9, 2)
CODE = "US.IEF"

# 「杰克逊霍尔」在标题里的各种位置，前后字符都不同，免得相邻字符跟着
# 成为公共 n-gram（那会让簇的名字变成「杰克逊霍尔与」这种切法）。
_JH_TITLES = [
    "杰克逊霍尔：联储路径再评估", "联储在杰克逊霍尔的信号", "解读杰克逊霍尔后的降息节奏",
    "杰克逊霍尔讲话与联储", "联储、通胀与杰克逊霍尔", "杰克逊霍尔之后：联储下一步",
    "从杰克逊霍尔看联储", "杰克逊霍尔：降息还是观望", "联储主席杰克逊霍尔发言要点",
    "杰克逊霍尔纪要：联储分歧", "杰克逊霍尔预告了降息吗", "联储降息与杰克逊霍尔",
]
#: 没命中任何主题的簇：人形机器人。旧口径也挖得到，用来证明旧簇没丢。
_ROBOT_TITLES = [
    "人形机器人量产元年", "人形机器人：谁在供应链上", "人形机器人成本曲线",
    "人形机器人订单跟踪", "人形机器人海外订单", "人形机器人零部件国产化",
    "人形机器人估值框架", "人形机器人与执行器", "人形机器人下游需求",
    "人形机器人：政策与资本", "人形机器人产业链梳理", "人形机器人量产瓶颈",
]
_INSTS = ["GoldmanSachs", "MorganStanley", "JPMorgan", "Citi", "UBS", "CICC"]


def _seed(con, *, jackson: bool = True, robots: bool = True) -> None:
    """A window corpus plus a pre-window baseline the lift denominator needs."""
    rows = []
    n = 0
    for titles, on in ((_JH_TITLES, jackson), (_ROBOT_TITLES, robots)):
        if not on:
            continue
        for i, t in enumerate(titles):
            n += 1
            rows.append((f"t:{n}", "feed", 2, t, _INSTS[i % len(_INSTS)],
                         (AS_OF - timedelta(days=i % 4)).isoformat(), "", ""))
    # Baseline: forty earlier documents about something else entirely, so a
    # window phrase has almost no history and clears MIN_LIFT.
    for i in range(40):
        n += 1
        rows.append((f"b:{n}", "feed", 3, f"欧洲银行股回顾第{i}期", _INSTS[i % 3],
                     (AS_OF - timedelta(days=20 + i % 10)).isoformat(), "", ""))
    con.executemany(
        "INSERT INTO documents(doc_id,line,tier,title,institution,published_d,"
        "summary,body,ingested_at) VALUES (?,?,?,?,?,?,?,?,'2026-09-02T00:00:00')",
        rows)
    con.execute("INSERT OR REPLACE INTO instruments(key,kind,futu_code,name,"
                "priceable,first_seen_d) VALUES (?,?,?,?,1,'2026-01-01')",
                (CODE, "listed", CODE, "iShares 7-10Y Treasury"))


class _Reply:
    def __init__(self, text: str):
        self.text = text


class _Port:
    def __init__(self, *texts: str):
        self.queue = list(texts)
        self.prompts: list[str] = []

    def complete(self, prompt, **kw):
        self.prompts.append(prompt)
        return _Reply(self.queue.pop(0) if self.queue else "{}")


class _Isolated(unittest.TestCase):
    """Temp registry + alias files, a fresh in-memory corpus, caches cleared."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        root = Path(self.td.name)
        self._paths = (lexicon.REGISTRY_PATH, lexicon.ALIASES_PATH)
        lexicon.REGISTRY_PATH = root / "registry.jsonl"
        lexicon.ALIASES_PATH = root / "aliases.jsonl"
        lexicon.reload_registry()
        # The baseline cache is keyed by (window start, corpus size); two
        # temp corpora of the same size would otherwise share one baseline.
        themes._BASELINE_CACHE.clear()
        self.con = db.init(root / "corpus.db")

    def tearDown(self):
        lexicon.REGISTRY_PATH, lexicon.ALIASES_PATH = self._paths
        lexicon.reload_registry()
        themes._BASELINE_CACHE.clear()
        self.con.close()
        self.td.cleanup()

    def _cluster(self, result: dict, needle: str) -> dict | None:
        return next((c for c in result["candidates"]
                     if any(needle in t for t in c["terms"])), None)


class TestFullWindowDiscovery(_Isolated):
    def test_the_fixture_is_claimed_by_an_old_theme(self):
        """Premise: every 杰克逊霍尔 title also says 联储/降息, so POLICY-PATH owns it."""
        _seed(self.con)
        items = themes.window_items(self.con, AS_OF)
        jh = [it for it in items if "杰克逊霍尔" in it["title"]]
        self.assertEqual(len(jh), len(_JH_TITLES))
        self.assertTrue(all("POLICY-PATH" in it["matched"] for it in jh))

    def test_the_old_scope_cannot_see_a_debate_inside_a_claimed_report(self):
        _seed(self.con)
        old = themes.candidates(self.con, AS_OF, scope=themes.SCOPE_UNMATCHED)
        self.assertIsNone(self._cluster(old, "杰克逊霍尔"),
                          "旧口径不该看见已被 POLICY-PATH 认领的研报里的新簇——"
                          "否则这条测试证明不了口径的差别")
        self.assertIsNotNone(self._cluster(old, "人形机器人"),
                             "旧口径本来就能挖到零匹配的簇")

    def test_the_full_window_finds_it_and_says_which_theme_it_sits_inside(self):
        _seed(self.con)
        new = themes.candidates(self.con, AS_OF)
        self.assertEqual(new["scope"], themes.SCOPE_ALL)
        c = self._cluster(new, "杰克逊霍尔")
        self.assertIsNotNone(c, "全窗口口径必须在已命中旧主题的研报里挖出新簇")
        rel = c["relation"]
        self.assertEqual(rel["kind"], themes.REL_POSSIBLE_SPLIT)
        self.assertEqual(rel["of"], "POLICY-PATH")
        self.assertEqual(rel["overlap"].get("POLICY-PATH"), c["n_docs"])
        self.assertEqual(rel["n_docs_matched"], c["n_docs"])
        self.assertEqual(rel["n_docs_unmatched"], 0)
        self.assertTrue(all("POLICY-PATH" in e["matched"] for e in c["evidence"]))
        self.assertEqual(len(c["doc_ids"]), c["n_docs"])
        # The zero-match cluster is still there and still says so.
        r = self._cluster(new, "人形机器人")
        self.assertIsNotNone(r)
        self.assertEqual(r["relation"], {"overlap": {}, "kind": themes.REL_DISTINCT,
                                         "of": None, "share": 0.0,
                                         "n_docs_matched": 0,
                                         "n_docs_unmatched": r["n_docs"]})

    def test_an_old_themes_own_words_do_not_resurface_as_a_candidate(self):
        """Mining claimed reports must not hand POLICY-PATH back as a candidate."""
        _seed(self.con)
        new = themes.candidates(self.con, AS_OF)
        known = themes._known_terms(AS_OF)
        for c in new["candidates"]:
            for t in c["terms"]:
                self.assertFalse(any(t.lower() in k or k in t.lower() for k in known),
                                 f"{t!r} 已属于注册主题，不该再冒出来")

    def test_coverage_numbers_keep_their_meaning_in_both_scopes(self):
        _seed(self.con)
        old = themes.candidates(self.con, AS_OF, scope=themes.SCOPE_UNMATCHED)
        new = themes.candidates(self.con, AS_OF)
        for k in ("corpus_total", "corpus_matched", "unmatched", "coverage_pct"):
            self.assertEqual(old[k], new[k], k)
        self.assertEqual(old["mined"], old["unmatched"])
        self.assertEqual(new["mined"], new["corpus_total"])
        self.assertEqual(new["corpus_total"], len(_JH_TITLES) + len(_ROBOT_TITLES))
        self.assertEqual(new["corpus_matched"], len(_JH_TITLES))

    def test_an_unknown_scope_is_refused(self):
        with self.assertRaises(ValueError):
            themes.candidates(self.con, AS_OF, scope="everything")


class TestRelationRule(unittest.TestCase):
    def _by_doc(self, n: int, matched: dict[str, int]) -> dict:
        out = {f"d{i}": {"matched": []} for i in range(n)}
        for tid, k in matched.items():
            for i in range(k):
                out[f"d{i}"]["matched"].append(tid)
        return out

    def test_thresholds_are_the_published_constants(self):
        self.assertEqual((themes.SPLIT_SHARE, themes.ADJACENT_SHARE), (0.6, 0.2))

    def test_most_evidence_inside_one_theme_is_a_possible_split(self):
        ids = {f"d{i}" for i in range(10)}
        rel = themes._relation(ids, self._by_doc(10, {"POLICY-PATH": 7, "INFLATION": 2}))
        self.assertEqual((rel["kind"], rel["of"], rel["share"]),
                         (themes.REL_POSSIBLE_SPLIT, "POLICY-PATH", 0.7))
        self.assertEqual(rel["n_docs_matched"], 7)
        self.assertEqual(list(rel["overlap"]), ["POLICY-PATH", "INFLATION"])

    def test_a_meaningful_minority_is_adjacent(self):
        ids = {f"d{i}" for i in range(10)}
        rel = themes._relation(ids, self._by_doc(10, {"CHINA-POLICY": 4}))
        self.assertEqual((rel["kind"], rel["of"]), (themes.REL_ADJACENT, "CHINA-POLICY"))

    def test_a_sliver_is_distinct_and_names_no_theme(self):
        ids = {f"d{i}" for i in range(10)}
        rel = themes._relation(ids, self._by_doc(10, {"AI-CAPEX": 1}))
        self.assertEqual((rel["kind"], rel["of"]), (themes.REL_DISTINCT, None))
        self.assertEqual(rel["overlap"], {"AI-CAPEX": 1})

    def test_the_boundaries_belong_to_the_stronger_class(self):
        ids = {f"d{i}" for i in range(10)}
        self.assertEqual(themes._relation(ids, self._by_doc(10, {"X": 6}))["kind"],
                         themes.REL_POSSIBLE_SPLIT)
        self.assertEqual(themes._relation(ids, self._by_doc(10, {"X": 2}))["kind"],
                         themes.REL_ADJACENT)


class TestAliasAsOfClamp(_Isolated):
    def test_an_alias_is_invisible_before_its_day_and_matches_after(self):
        themes.add_alias(self.con, "POLICY-PATH", ["杰克逊霍尔"], AS_OF,
                         rationale="同一驱动：联储政策路径；同一验证条件")
        before = {t.id: t for t in lexicon.all_themes(AS_OF - timedelta(days=7))}
        after = {t.id: t for t in lexicon.all_themes(AS_OF)}
        self.assertNotIn("杰克逊霍尔", before["POLICY-PATH"].terms)
        self.assertIn("杰克逊霍尔", after["POLICY-PATH"].terms)
        self.assertEqual(after["POLICY-PATH"].alias_terms, ("杰克逊霍尔",))
        self.assertEqual(after["POLICY-PATH"].aliases_through, AS_OF.isoformat())
        self.assertIsNone(before["POLICY-PATH"].aliases_through)
        text = "杰克逊霍尔讲话前瞻"
        self.assertEqual(lexicon.match_theme(text, before["POLICY-PATH"]), 0)
        self.assertEqual(lexicon.match_theme(text, after["POLICY-PATH"]), 1)
        # The registry text itself is untouched: the alias is a dated overlay.
        self.assertNotIn("杰克逊霍尔", lexicon.THEME_BY_ID["POLICY-PATH"].terms)

    def test_the_alias_file_is_append_only_json_lines(self):
        themes.add_alias(self.con, "POLICY-PATH", ["杰克逊霍尔"], AS_OF,
                         evidence_doc_ids=["t:1", "t:2"], candidate_terms=["杰克逊霍尔"])
        themes.add_alias(self.con, "AI-CAPEX", ["算力租赁"], AS_OF)
        lines = [json.loads(x) for x in
                 lexicon.ALIASES_PATH.read_text("utf-8").splitlines() if x.strip()]
        self.assertEqual([x["theme_id"] for x in lines], ["POLICY-PATH", "AI-CAPEX"])
        self.assertEqual(lines[0]["evidence_doc_ids"], ["t:1", "t:2"])
        self.assertEqual(lines[0]["as_of"], AS_OF.isoformat())

    def test_a_word_another_theme_owns_is_refused(self):
        """通胀 belongs to INFLATION; giving it to POLICY-PATH double-counts D."""
        with self.assertRaises(themes.RegistrationError) as caught:
            themes.add_alias(self.con, "POLICY-PATH", ["杰克逊霍尔", "通胀"], AS_OF)
        self.assertIn("通胀", str(caught.exception))
        self.assertFalse(lexicon.ALIASES_PATH.exists(), "被拒的别名不能落盘")

    def test_an_alias_made_only_of_existing_words_is_an_error_not_a_noop(self):
        with self.assertRaises(themes.RegistrationError):
            themes.add_alias(self.con, "POLICY-PATH", ["降息", "FOMC"], AS_OF)

    def test_an_alias_for_a_theme_the_day_cannot_see_is_refused(self):
        with self.assertRaises(themes.RegistrationError):
            themes.add_alias(self.con, "NO-SUCH-THEME", ["x"], AS_OF)
        # A theme registered after the day is equally invisible on that day.
        early = date(2026, 8, 1)
        self.assertNotIn("SPACE-ECONOMY", {t.id for t in lexicon.all_themes(early)})

    def test_a_backdated_alias_line_fails_loudly_at_load(self):
        """Loading, not just writing, guards the clamp: the file is editable."""
        lexicon.ALIASES_PATH.write_text(json.dumps({
            "theme_id": "POLICY-PATH", "terms": ["杰克逊霍尔"],
            "as_of": "2026-01-01"}) + "\n", "utf-8")
        with self.assertRaises(ValueError) as caught:
            lexicon.load_aliases()
        self.assertIn("before the theme was registered", str(caught.exception))
        lexicon.ALIASES_PATH.write_text(json.dumps({
            "theme_id": "GHOST", "terms": ["x"], "as_of": AS_OF.isoformat()}) + "\n",
            "utf-8")
        with self.assertRaises(ValueError):
            lexicon.load_aliases()


class TestSnapshot(_Isolated):
    def test_stable_when_nothing_changes(self):
        a = themes.snapshot(AS_OF)
        b = themes.snapshot(AS_OF)
        self.assertEqual(a["theme_set_sha"], b["theme_set_sha"])
        self.assertEqual(a["n_themes"], len(lexicon.all_themes(AS_OF)))
        row = next(t for t in a["themes"] if t["id"] == "POLICY-PATH")
        for k in ("id", "label", "registered_d", "terms_sha", "n_terms",
                  "aliases_through"):
            self.assertIn(k, row)

    def test_an_alias_changes_the_sha_from_its_day_and_not_before(self):
        before = themes.snapshot(AS_OF)["theme_set_sha"]
        earlier = themes.snapshot(AS_OF - timedelta(days=7))["theme_set_sha"]
        themes.add_alias(self.con, "POLICY-PATH", ["杰克逊霍尔"], AS_OF)
        self.assertNotEqual(themes.snapshot(AS_OF)["theme_set_sha"], before)
        self.assertEqual(themes.snapshot(AS_OF - timedelta(days=7))["theme_set_sha"],
                         earlier, "别名之前的期不能因为后来的别名换 sha")

    def test_a_registration_changes_the_sha_from_its_day_and_not_before(self):
        _seed(self.con)
        before = themes.snapshot(AS_OF)["theme_set_sha"]
        earlier = themes.snapshot(AS_OF - timedelta(days=7))["theme_set_sha"]
        themes.register(self.con, {
            "id": "JACKSON-HOLE-PIVOT", "label": "杰克逊霍尔转向",
            "key_question": "未来1–6个月，杰克逊霍尔释放的转向能否兑现为降息？",
            "terms": ["杰克逊霍尔", "怀俄明讲话", "央行年会信号", "年会转向"],
            "price_indicator": CODE}, AS_OF)
        self.assertNotEqual(themes.snapshot(AS_OF)["theme_set_sha"], before)
        self.assertEqual(themes.snapshot(AS_OF - timedelta(days=7))["theme_set_sha"],
                         earlier)


def _same_debate(of: str, *terms: str) -> str:
    return json.dumps({"skip": f"与 {of} 是同一争论", "relation": "same_debate",
                       "of": of, "new_terms": list(terms),
                       "rationale": "同一驱动（联储政策路径）、同一验证条件（降息落地）、"
                                    "同一事件（议息会议）"}, ensure_ascii=False)


_SPLIT_CARD = json.dumps({
    "id": "FED-CHAIR-SUCCESSION", "label": "联储主席人选与政策连续性",
    "key_question": "未来1–6个月，联储主席人选能否改变市场对降息路径的定价？",
    "terms": ["杰克逊霍尔", "联储主席人选", "主席继任", "沃什提名", "鲍威尔接班", "联储换帅"],
    "price_indicator": CODE, "related": [], "default_direction": "↑",
    "relation": "split", "of": "POLICY-PATH",
    "rationale": "驱动不同：人事而非数据；验证条件不同：提名落地而非议息决议"},
    ensure_ascii=False)

_DISTINCT_CARD_NO_RATIONALE = json.dumps({
    "id": "FED-CHAIR-SUCCESSION", "label": "联储主席人选",
    "key_question": "未来1–6个月，联储主席人选能否改变降息定价？",
    "terms": ["杰克逊霍尔", "联储主席人选", "主席继任", "沃什提名", "鲍威尔接班", "联储换帅"],
    "price_indicator": CODE, "default_direction": "↑"}, ensure_ascii=False)

_ROBOT_SKIP = json.dumps({"skip": "人形机器人是板块名词，不是宏观争论"}, ensure_ascii=False)


class _Journal:
    def __init__(self):
        self.steps: list[dict] = []

    def step(self, name, **fields):
        self.steps.append({"step": name, **fields})

    def named(self, name):
        return [s for s in self.steps if s["step"] == name]


class TestMintSeesItsNeighbours(_Isolated):
    def test_the_prompt_carries_the_overlapping_theme_and_its_question(self):
        _seed(self.con, robots=False)
        c = self._cluster(themes.candidates(self.con, AS_OF), "杰克逊霍尔")
        port = _Port(_ROBOT_SKIP)
        with self.assertRaises(themes.MintSkipped):
            themes.mint(self.con, c, AS_OF, port)
        prompt = port.prompts[0]
        pp = lexicon.THEME_BY_ID["POLICY-PATH"]
        self.assertIn("POLICY-PATH", prompt)
        self.assertIn(pp.label, prompt)
        self.assertIn(pp.key_question, prompt)
        self.assertIn("possible_split of POLICY-PATH", prompt)
        self.assertIn("same_debate", prompt)

    def test_a_card_next_to_a_neighbour_must_state_its_relation(self):
        _seed(self.con, robots=False)
        c = self._cluster(themes.candidates(self.con, AS_OF), "杰克逊霍尔")
        port = _Port(_DISTINCT_CARD_NO_RATIONALE, _SPLIT_CARD)
        card = themes.mint(self.con, c, AS_OF, port)
        self.assertEqual(len(port.prompts), 2, "第一张没说 relation 的卡该被退回重写")
        self.assertIn("relation", port.prompts[1])
        self.assertEqual(card["relation"], "split")
        self.assertEqual(card["split_from"], "POLICY-PATH")
        self.assertTrue(card["rationale"])
        self.assertEqual(card["evidence_doc_ids"], c["doc_ids"][:20])

    def test_a_merge_pointing_at_an_unseen_theme_is_retried_not_recorded(self):
        _seed(self.con, robots=False)
        c = self._cluster(themes.candidates(self.con, AS_OF), "杰克逊霍尔")
        port = _Port(_same_debate("GHOST-THEME", "杰克逊霍尔"), _ROBOT_SKIP)
        with self.assertRaises(themes.MintSkipped) as caught:
            themes.mint(self.con, c, AS_OF, port)
        self.assertNotIsInstance(caught.exception, themes.MintMerged)
        self.assertIn("GHOST-THEME", port.prompts[1])


class TestValidateRelationFields(_Isolated):
    def _row(self, **kw):
        row = {"id": "JACKSON-HOLE-PIVOT", "label": "x",
               "key_question": "未来1–6个月，能否？",
               "terms": ["a1", "a2", "a3", "a4"], "price_indicator": CODE}
        row.update(kw)
        return row

    def test_same_debate_is_never_a_registry_row(self):
        _seed(self.con)
        with self.assertRaises(themes.RegistrationError) as caught:
            themes.validate(self.con, self._row(relation="same_debate"), AS_OF)
        self.assertIn("alias", str(caught.exception))

    def test_a_split_needs_a_parent_the_day_can_see(self):
        _seed(self.con)
        with self.assertRaises(themes.RegistrationError):
            themes.validate(self.con, self._row(relation="split"), AS_OF)
        with self.assertRaises(themes.RegistrationError):
            themes.validate(self.con, self._row(relation="split",
                                                split_from="SPACE-ECONOMY"),
                            date(2026, 8, 1))
        ok = themes.validate(self.con, self._row(relation="split",
                                                 split_from="POLICY-PATH",
                                                 rationale="驱动不同"), AS_OF)
        self.assertEqual((ok["relation"], ok["split_from"], ok["rationale"]),
                         ("split", "POLICY-PATH", "驱动不同"))

    def test_a_registered_split_survives_reload_with_its_reasoning(self):
        _seed(self.con)
        themes.register(self.con, self._row(relation="split", split_from="POLICY-PATH",
                                            rationale="验证条件不同",
                                            evidence_doc_ids=["t:1"]), AS_OF)
        t = lexicon.THEME_BY_ID["JACKSON-HOLE-PIVOT"]
        self.assertEqual((t.relation, t.split_from, t.rationale, t.evidence_doc_ids),
                         ("split", "POLICY-PATH", "验证条件不同", ("t:1",)))


class TestDiscoverLoop(_Isolated):
    def test_same_debate_becomes_an_alias_and_a_merge_note_not_a_theme(self):
        _seed(self.con)
        j = _Journal()
        # Candidate order is by n_docs; both clusters have 12 docs, so answer
        # by content rather than by position.
        port = _Port()
        port.complete = lambda prompt, **kw: _Reply(
            _same_debate("POLICY-PATH", "杰克逊霍尔") if "杰克逊霍尔" in prompt
            else _ROBOT_SKIP)
        out = themes.discover(self.con, AS_OF, port, step=j.step)
        self.assertEqual(out["registered"], [])
        self.assertFalse(lexicon.REGISTRY_PATH.exists(), "归并不能变成注册行")
        self.assertEqual([m["theme_id"] for m in out["merged"]], ["POLICY-PATH"])
        notes = j.named("theme_merge_note")
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["theme_id"], "POLICY-PATH")
        self.assertEqual(notes[0]["new_terms"], ["杰克逊霍尔"])
        self.assertTrue(notes[0]["alias_written"])
        self.assertIn("同一驱动", notes[0]["rationale"])
        self.assertEqual(notes[0]["relation"]["kind"], themes.REL_POSSIBLE_SPLIT)
        after = {t.id: t for t in lexicon.all_themes(AS_OF)}
        self.assertIn("杰克逊霍尔", after["POLICY-PATH"].terms)
        final = j.named("theme_discovery")
        self.assertEqual(len(final), 1)
        self.assertEqual(final[-1]["merged"][0]["theme_id"], "POLICY-PATH")
        self.assertEqual(len(final[-1]["skipped"]), 1)
        self.assertNotIn("error", final[-1])

    def test_a_merge_without_new_words_is_noted_and_writes_nothing(self):
        _seed(self.con, robots=False)
        j = _Journal()
        out = themes.discover(self.con, AS_OF, _Port(_same_debate("POLICY-PATH")),
                              step=j.step)
        note = j.named("theme_merge_note")[0]
        self.assertFalse(note["alias_written"])
        self.assertIn("没有给出新叫法", note["error"])
        self.assertFalse(lexicon.ALIASES_PATH.exists())
        self.assertEqual(out["merged"][0]["alias_written"], False)

    def test_a_split_registers_with_its_parent_and_reasoning(self):
        _seed(self.con, robots=False)
        j = _Journal()
        out = themes.discover(self.con, AS_OF, _Port(_SPLIT_CARD), step=j.step)
        self.assertEqual(out["registered"], ["FED-CHAIR-SUCCESSION"])
        line = json.loads(lexicon.REGISTRY_PATH.read_text("utf-8").strip())
        self.assertEqual(line["relation"], "split")
        self.assertEqual(line["split_from"], "POLICY-PATH")
        self.assertIn("驱动不同", line["rationale"])
        self.assertEqual(line["registered_d"], AS_OF.isoformat())
        self.assertTrue(line["evidence_doc_ids"])
        self.assertIn("篇已命中旧主题", line["provenance"][0])
        t = lexicon.THEME_BY_ID["FED-CHAIR-SUCCESSION"]
        self.assertEqual(t.split_from, "POLICY-PATH")
        self.assertNotIn("FED-CHAIR-SUCCESSION",
                         {x.id for x in lexicon.all_themes(AS_OF - timedelta(days=1))})

    def test_no_model_is_said_once_and_registers_nothing(self):
        _seed(self.con)
        j = _Journal()
        out = themes.discover(self.con, AS_OF, None, step=j.step)
        steps = j.named("theme_discovery")
        self.assertEqual(len(steps), 1)
        self.assertIn("没有 inference 端口", steps[0]["error"])
        self.assertEqual(steps[0]["candidates"], 2)
        self.assertEqual(out["registered"], [])
        self.assertFalse(lexicon.REGISTRY_PATH.exists())
        self.assertFalse(lexicon.ALIASES_PATH.exists())


class TestWeeklyRunFreezesItsThemeSet(unittest.TestCase):
    """The run writes `A_theme_set.json` and stamps the topics verdict."""

    def test_artifact_and_sha_reach_the_journal_and_the_verdict(self):
        from ideagen import orchestrator, poc_workflow, strategy
        from ideagen.platform.base import Health, Platform
        from ideagen.platform.local import (FileCache, FileEventBus,
                                            LocalBlobStore, SqliteStateStore)

        class HealthyPort:
            def __init__(self, name):
                self.name = name

            def check(self):
                return Health(True, self.name, "test")

        @strategy.register("idea_generator", "_theme_set_probe_generator", "1.0",
                           needs_model=False)
        def generate(ctx):
            topic = ctx.topics[0]["topic_id"]
            inst = ctx.universe[0]
            cand = {"id": f"probe:{topic}:{inst['instrument_id']}",
                    "instrument_id": inst["instrument_id"],
                    "instrument_name": inst["name"], "topic_id": topic,
                    "method": "_theme_set_probe_generator", "thesis": "probe",
                    "upside_pct": 5.0, "downside_pct": -3.0,
                    "p_up": 0.4, "p_base": 0.4, "p_down": 0.2}
            return strategy.Verdict(strategy="_theme_set_probe_generator",
                                    version="1.0", chosen=[cand["id"]],
                                    produced=[cand])

        try:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                state = SqliteStateStore(root / "state.db")
                blobs = LocalBlobStore(root / "blobs")
                p = Platform(name="test", blobs=blobs, state=state,
                             inference=HealthyPort("inference"),
                             events=FileEventBus(root / "events.jsonl"),
                             cache=FileCache(root / "cache"),
                             secrets=HealthyPort("secrets"))
                as_of = date(2026, 8, 30)
                res = orchestrator.weekly(
                    as_of=as_of, p=p, generators=["_theme_set_probe_generator"],
                    selectors=["buy_all"],
                    params={"top_n": 1, "skip_theme_discovery": True},
                    verbose=False, **poc_workflow.public_inputs(as_of))
                self.assertTrue(res.completed, res.error)
                key = next(k for k in blobs.list(f"runs/{as_of}/{res.run_id}")
                           if k.endswith("A_theme_set.json"))
                frozen = json.loads(blobs.get(key))
                journal = json.loads(blobs.get(
                    f"runs/{as_of}/{res.run_id}/journal.json"))
                verdicts = state.q("SELECT meta FROM verdicts WHERE run_id=? "
                                   "AND kind='topic_scorer'", (res.run_id,))
        finally:
            strategy._REGISTRY.pop(("idea_generator", "_theme_set_probe_generator"),
                                   None)

        self.assertEqual(frozen["theme_set_sha"], themes.snapshot(as_of)["theme_set_sha"])
        self.assertEqual(frozen["n_themes"], len(lexicon.all_themes(as_of)))
        by_name = {s["step"]: s for s in journal["steps"]}
        self.assertEqual(by_name["theme_set"]["sha"], frozen["theme_set_sha"])
        self.assertEqual(by_name["topics"]["theme_set_sha"], frozen["theme_set_sha"])
        self.assertTrue(verdicts)
        for v in verdicts:
            self.assertEqual(json.loads(v["meta"])["theme_set_sha"],
                             frozen["theme_set_sha"])


if __name__ == "__main__":
    unittest.main()
