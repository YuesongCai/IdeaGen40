"""主题谱系：同一件事换个名字再出来，要认出来、记下来。

yifu 2026-09-11：「AI 每次自己起名，同一件事换个名字再出来，要归并」。发现流程
（`themes.discover`）已经会把「当周新叫法」记成别名，但它只在注册那一刻、只和
当时的邻居比一次；注册之后两个主题越长越像，没有任何地方会再看一眼。后果落在
复现打折上：`scoring.recurrence` 按 theme_id 数连续出现的周数，同一叙事换了 id，
打折就清零重来——「第二次出现打折」被一次改名绕过去。

这里做三件事：

1. `scan`：两两比对所有注册主题，给出每一对的证据——
     label_sim     标签的字符二元组 Dice
     kq_sim        key_question 去掉「未来1–6个月」「能否」等套话后的二元组 Dice
     term_sim      词表（含别名）互相包含的比例（「存储」⊂「存储芯片」算重合）
     ev_overlap    全部研报里两者命中集合的重叠系数 |A∩B| / min(|A|,|B|)
     ev_sim        进综合分的证据项：sqrt(重叠系数 × Jaccard)，防「窄落在宽里」
   和反证——
     co_strong_days    两者在同一天都是强主题（core/important）的天数
     co_selected       两者在同一期同时入选的期数
     split_registered  注册时一方明确写了「从另一方拆出」及理由
   **两主题同一期都是强主题 = 市场在同时谈两件事**，这是最硬的反证：同一件事
   不会在同一张排行榜上占两个位置。
2. `apply`：只有综合分过高阈值、证据重叠过高阈值、两边各有足够研报、且**无任何
   反证**的对，才追加进 `themes/lineage.jsonl`。其余全部列为「疑似重复，待审」。
   宁可少并：错并会让一个真正新的叙事被当成老故事打折，漏并只是少打几分。
3. `family_ids(theme_id, as_of)`：谱系家族，`scoring.recurrence` 按家族数复现。
   和别名一样按日期卡：一条谱系只对它登记日（含）之后的打分生效，今天认出的
   亲缘关系不许回头改上个月的打折。

`themes/lineage.jsonl` 每行（追加式，仿 aliases.jsonl）：
  {"as_of": 登记日, "theme_id": 并入的主题, "family": 家族根主题,
   "method": "auto" | "manual", "score": 综合分, "evidence": {...各项读数},
   "rationale": 为什么认定是同一件事}
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from . import config, db, lexicon

LINEAGE_PATH = lexicon._ROOT / "themes" / "lineage.jsonl"
KV_SCAN = "theme_lineage:scan"
_FIELDS = {"as_of", "theme_id", "family", "method", "score", "evidence", "rationale"}
WEIGHTS = {"label_sim": 0.20, "kq_sim": 0.15, "term_sim": 0.25, "ev_sim": 0.40}


# ---------------------------------------------------------------- file
def lineage_write_path() -> Path:
    d = lexicon.durable_themes_dir()
    return (d / "lineage.jsonl") if d else LINEAGE_PATH


def _paths(path: Path | None) -> list[Path]:
    if path is not None:
        return [path]
    d = lexicon.durable_themes_dir()
    return [LINEAGE_PATH] + ([d / "lineage.jsonl"] if d else [])


_CACHE: dict[tuple, tuple[dict, ...]] = {}


def load_lineage(path: Path | None = None) -> tuple[dict, ...]:
    """读谱系文件。坏行报错而不是跳过——静默丢掉的一条归并，和从没归并过长得一样。"""
    ps = _paths(path)
    key = tuple((str(p), p.stat().st_mtime_ns if p.exists() else None) for p in ps)
    if key in _CACHE:
        return _CACHE[key]
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for p in ps:
        if not p.exists():
            continue
        for n, raw in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{p}:{n} is not valid JSON: {exc}") from exc
            unknown = set(row) - _FIELDS
            if unknown:
                raise ValueError(f"{p}:{n} has unknown fields: {sorted(unknown)}")
            for k in ("as_of", "theme_id", "family", "rationale"):
                if not row.get(k):
                    raise ValueError(f"{p}:{n} is missing {k}")
            for k in ("theme_id", "family"):
                if row[k] not in lexicon.THEME_BY_ID:
                    raise ValueError(f"{p}:{n} names unknown theme {row[k]!r}")
            if row["theme_id"] == row["family"]:
                raise ValueError(f"{p}:{n} merges {row['theme_id']} into itself")
            k2 = (row["theme_id"], row["family"])
            if k2 in seen:      # 种子文件与持久文件重复同一行：同一次登记看了两遍
                continue
            seen.add(k2)
            out.append(row)
    res = tuple(out)
    _CACHE.clear()
    _CACHE[key] = res
    return res


def family_ids(theme_id: str, as_of: date | str | None = None,
               rows: tuple[dict, ...] | None = None) -> set[str]:
    """`theme_id` 所在谱系家族的全部 id（含自己），只算登记日 ≤ as_of 的谱系行。"""
    rows = load_lineage() if rows is None else rows
    d = None if as_of is None else (as_of if isinstance(as_of, str) else as_of.isoformat())
    adj: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        if d is not None and str(r["as_of"]) > d:
            continue
        adj[r["theme_id"]].add(r["family"])
        adj[r["family"]].add(r["theme_id"])
    fam, stack = {theme_id}, [theme_id]
    while stack:
        for nb in adj.get(stack.pop(), ()):
            if nb not in fam:
                fam.add(nb)
                stack.append(nb)
    return fam


def family_root(theme_id: str, as_of: date | str | None = None) -> str:
    """家族里注册最早的那个（同日取种子、再取字典序）。"""
    fam = family_ids(theme_id, as_of)
    return min(fam, key=lambda t: _age_key(lexicon.THEME_BY_ID.get(t), t))


def _age_key(t, tid: str) -> tuple:
    if t is None:
        return ("9999", 1, tid)
    return (t.registered_d, 0 if t.origin == "seed" else 1, tid)


# ---------------------------------------------------------------- similarity
_BOILER = re.compile(r"未来\s*1\s*[–\-—~]\s*6\s*个月[，,]?|能否|是否|会否|能不能|"
                     r"持续|显著|相关|主要|[，。、；：？！“”（）()?,.;:\s]")


def _bigrams(s: str) -> set[str]:
    s = (s or "").lower()
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else ({s} if s else set())


def dice(a: str, b: str) -> float:
    x, y = _bigrams(a), _bigrams(b)
    if not x or not y:
        return 0.0
    return 2 * len(x & y) / (len(x) + len(y))


def kq_norm(s: str) -> str:
    return _BOILER.sub("", s or "")


def term_sim(a: tuple[str, ...], b: tuple[str, ...]) -> float:
    """两边词表互相覆盖的比例，取两个方向的平均。

    精确 Jaccard 会漏掉「存储」和「存储芯片」这种一个是另一个前缀的情形——发现
    流程起的新名字恰恰常是老词加限定语。单字词（「云」「铜」）不参与包含判断：
    它们是一大把词的子串，会把什么都算成重合。
    """
    A = {t.lower() for t in a if t}
    B = {t.lower() for t in b if t}
    if not A or not B:
        return 0.0

    def cover(X, Y):
        hit = 0
        for x in X:
            if x in Y or (len(x) >= 2 and any(len(y) >= 2 and (x in y or y in x) for y in Y)):
                hit += 1
        return hit / len(X)
    return (cover(A, B) + cover(B, A)) / 2


# ---------------------------------------------------------------- scan
def scan(con, *, mentions: dict | None = None, runs: list[dict] | None = None) -> dict[str, Any]:
    """两两比对全部注册主题，返回每一对的证据、反证和处置建议。

    返回 {"computed_at", "thresholds", "pairs": [...], "auto": [...], "suspect": [...]}，
    pairs 按综合分降序，每对：
      a, b, labels, score, label_sim, kq_sim, term_sim, ev_overlap, ev_jaccard,
      n_docs_a, n_docs_b, co_strong_days, co_selected, split_registered,
      counter: [反证文字], decision: "auto" | "suspect" | "distinct",
      recorded: 是否已在 lineage.jsonl 里, why: 一句话
    """
    from . import theme_audit as ta
    runs = runs if runs is not None else ta.canonical_runs(con)
    mentions = mentions if mentions is not None else ta.mention_index(con)
    themes = list(lexicon.all_themes(None))
    strong: dict[str, set[str]] = {k: set(v) for k, v in ta.strong_index(con).items()}
    selected = {r["as_of"]: set(r["hgep_chosen"]) for r in runs}
    recorded = {frozenset((r["theme_id"], r["family"])) for r in load_lineage()}

    pairs = []
    for i, a in enumerate(themes):
        for b in themes[i + 1:]:
            da = set((mentions.get(a.id) or {}).get("full") or {})
            dbs = set((mentions.get(b.id) or {}).get("full") or {})
            inter = len(da & dbs)
            enough = min(len(da), len(dbs)) >= config.LINEAGE_MIN_DOCS
            # 研报太少时重叠系数没有意义：一个只命中 2 篇的主题，2 篇都落在大主题
            # 里就是 100%。不够篇数，证据项记 0 并在 why 里说出来，不让它推高综合分。
            ev_ov = inter / min(len(da), len(dbs)) if (da and dbs and enough) else 0.0
            ev_j = inter / len(da | dbs) if (da or dbs) else 0.0
            # 进综合分的证据项是重叠系数与 Jaccard 的几何平均：两边量级相近时它≈
            # 重叠系数；一边是另一边的十分之一时它被 Jaccard 拉低。只用重叠系数，
            # 每个窄主题都会因为「全落在宽主题里」被推成疑似重复。
            sig = {"label_sim": dice(a.label, b.label),
                   "kq_sim": dice(kq_norm(a.key_question), kq_norm(b.key_question)),
                   "term_sim": term_sim(a.terms, b.terms),
                   "ev_sim": (ev_ov * ev_j) ** 0.5 if enough else 0.0}
            score = sum(WEIGHTS[k] * v for k, v in sig.items())
            co_strong = len(strong.get(a.id, set()) & strong.get(b.id, set()))
            co_sel = sum(1 for s in selected.values() if a.id in s and b.id in s)
            split = (a.split_from == b.id) or (b.split_from == a.id)
            counter = []
            if co_strong:
                counter.append(f"同一天都是强主题 {co_strong} 天（市场在同时谈两件事）")
            if co_sel:
                counter.append(f"同一期同时入选 {co_sel} 期")
            if split:
                child = a if a.split_from == b.id else b
                counter.append(f"{child.id} 注册时写明从 {child.split_from} 拆出："
                               f"{(child.rationale or '')[:80]}")
            # 重叠系数高而 Jaccard 低 = 窄主题的研报大多也命中宽主题（「中国房地产」
            # 之于「中国政策」）。那是包含，不是换名：同一件事换个名字，两边的
            # 研报量级应当相近。单列出来给人看，不进疑似重复。
            nested = ev_ov >= config.LINEAGE_SUSPECT_EVIDENCE and ev_j < config.LINEAGE_NESTED_JACCARD
            if (score >= config.LINEAGE_AUTO_SCORE and ev_ov >= config.LINEAGE_AUTO_EVIDENCE
                    and enough and not counter):
                decision = "auto"
                why = "综合分与证据重叠均过自动归并阈值，且无任何反证"
            elif not enough and max(sig["label_sim"], sig["kq_sim"], sig["term_sim"]) >= 0.4:
                # 文字很像、研报却太少，证据项算不出来——这正是「刚换了个名字」
                # 最常见的样子，不能因为算不出重叠就当成不相干。
                decision = "suspect"
                why = (f"疑似重复，待审：名称/问句/词表相近（最高 "
                       f"{max(sig['label_sim'], sig['kq_sim'], sig['term_sim']):.2f}），"
                       f"但一方研报不足 {config.LINEAGE_MIN_DOCS} 篇，重叠无从判断")
            elif nested and score < config.LINEAGE_SUSPECT_SCORE:
                decision = "nested"
                why = (f"包含关系：较小一方 {ev_ov:.0%} 的研报也命中另一方，但两边合起来只有 "
                       f"{ev_j:.0%} 重合——窄主题落在宽主题里，不是换名")
            elif score >= config.LINEAGE_SUSPECT_SCORE or ev_ov >= config.LINEAGE_SUSPECT_EVIDENCE:
                decision = "suspect"
                blocks = []
                if counter:
                    blocks.append("有反证")
                if not enough:
                    blocks.append(f"一方研报不足 {config.LINEAGE_MIN_DOCS} 篇")
                if score < config.LINEAGE_AUTO_SCORE:
                    blocks.append(f"综合分 {score:.2f} < {config.LINEAGE_AUTO_SCORE}")
                if ev_ov < config.LINEAGE_AUTO_EVIDENCE:
                    blocks.append(f"证据重叠 {ev_ov:.2f} < {config.LINEAGE_AUTO_EVIDENCE}")
                why = "疑似重复，待审：" + "；".join(blocks)
            else:
                decision = "distinct"
                why = "相似度低于疑似阈值"
            pairs.append({
                "a": a.id, "b": b.id, "label_a": a.label, "label_b": b.label,
                "score": round(score, 3), **{k: round(v, 3) for k, v in sig.items()},
                "ev_overlap": round(ev_ov, 3),
                "ev_jaccard": round(ev_j, 3), "ev_shared": inter, "enough_docs": enough,
                "n_docs_a": len(da), "n_docs_b": len(dbs),
                "co_strong_days": co_strong, "co_selected": co_sel,
                "split_registered": split, "counter": counter,
                "decision": decision, "why": why,
                "recorded": frozenset((a.id, b.id)) in recorded})
    pairs.sort(key=lambda p: -p["score"])
    return {"computed_at": config.now_hkt().isoformat(),
            "thresholds": {"auto_score": config.LINEAGE_AUTO_SCORE,
                           "auto_evidence": config.LINEAGE_AUTO_EVIDENCE,
                           "suspect_score": config.LINEAGE_SUSPECT_SCORE,
                           "suspect_evidence": config.LINEAGE_SUSPECT_EVIDENCE,
                           "min_docs": config.LINEAGE_MIN_DOCS, "weights": WEIGHTS},
            "n_themes": len(themes), "n_pairs": len(pairs),
            "pairs": pairs,
            "auto": [p for p in pairs if p["decision"] == "auto"],
            "suspect": [p for p in pairs if p["decision"] == "suspect"],
            "nested": [p for p in pairs if p["decision"] == "nested"]}


def apply(con, result: dict, *, as_of: date | None = None, dry_run: bool = False,
          path: Path | None = None) -> list[dict]:
    """把 scan 结果里 decision=auto 且尚未登记的对追加进 lineage.jsonl。

    并入方向：注册晚的并入注册早的家族根（同日：发现的并入种子）。已经在同一
    家族里的对不重复写。返回写入（或 dry_run 时将写入）的行。
    """
    as_of = as_of or config.today_hkt()
    rows = load_lineage(path) if path is not None else load_lineage()
    out: list[dict] = []
    for p in result.get("auto") or []:
        if p.get("recorded"):
            continue
        ta_, tb = lexicon.THEME_BY_ID.get(p["a"]), lexicon.THEME_BY_ID.get(p["b"])
        older, newer = sorted([(ta_, p["a"]), (tb, p["b"])], key=lambda x: _age_key(*x))
        if newer[1] in family_ids(older[1], None, rows + tuple(out)):
            continue
        root = min(family_ids(older[1], None, rows + tuple(out)),
                   key=lambda t: _age_key(lexicon.THEME_BY_ID.get(t), t))
        line = {"as_of": as_of.isoformat(), "theme_id": newer[1], "family": root,
                "method": "auto", "score": p["score"],
                "evidence": {k: p[k] for k in ("label_sim", "kq_sim", "term_sim", "ev_sim", "ev_overlap",
                                               "ev_shared", "n_docs_a", "n_docs_b",
                                               "co_strong_days", "co_selected")},
                "rationale": (f"{newer[1]}（{p['label_b'] if newer[1] == p['b'] else p['label_a']}）与 "
                              f"{older[1]} 的研报命中重叠 {p['ev_overlap']:.0%}、词表互含 "
                              f"{p['term_sim']:.0%}、标签相似 {p['label_sim']:.0%}，"
                              f"且从未在同一天同为强主题、从未同期入选")}
        out.append(line)
    if out and not dry_run:
        target = path or lineage_write_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        new_file = not target.exists()
        with target.open("a", encoding="utf-8") as fh:
            if new_file:
                fh.write("# theme lineage, append-only. fields: as_of, theme_id, family, "
                         "method, score, evidence, rationale\n"
                         "# theme_id joins the family of `family` for scorings on or after as_of.\n")
            for line in out:
                fh.write(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n")
        _CACHE.clear()
    return out


def summary(result: dict, limit: int = 30) -> dict[str, Any]:
    """面板用的精简版：家族映射 + 疑似清单（每对只留判断要用的字段）。"""
    keep = ("a", "b", "label_a", "label_b", "score", "label_sim", "kq_sim", "term_sim",
            "ev_sim", "ev_overlap", "ev_jaccard", "ev_shared", "co_strong_days", "co_selected", "split_registered",
            "counter", "decision", "why", "recorded")
    fams: dict[str, list[str]] = {}
    for t in lexicon.THEMES:
        f = family_ids(t.id)
        if len(f) > 1:
            fams[t.id] = sorted(f)
    return {"computed_at": result.get("computed_at"), "thresholds": result.get("thresholds"),
            "n_pairs": result.get("n_pairs"), "families": fams,
            "auto": [{k: p[k] for k in keep} for p in (result.get("auto") or [])[:limit]],
            "suspect": [{k: p[k] for k in keep} for p in (result.get("suspect") or [])[:limit]],
            "nested": [{k: p[k] for k in keep} for p in (result.get("nested") or [])[:limit]]}


def cmd_theme_lineage(args) -> int:
    """CLI：`ideagen theme-lineage scan|apply [--dry-run]`。"""
    con = db.init()
    res = scan(con)
    db.kv_set(con, KV_SCAN, summary(res))
    print(f"{res['n_themes']} 个主题、{res['n_pairs']} 对：自动归并 {len(res['auto'])} 对，"
          f"疑似待审 {len(res['suspect'])} 对，包含关系 {len(res['nested'])} 对")
    for p in res["auto"] + res["suspect"]:
        print(f"  [{p['decision']}] {p['a']} ~ {p['b']}  分 {p['score']:.2f}  "
              f"标签 {p['label_sim']:.2f} 问句 {p['kq_sim']:.2f} 词表 {p['term_sim']:.2f} "
              f"证据 {p['ev_overlap']:.2f}（共 {p['ev_shared']} 篇）  {p['why']}")
        for c in p["counter"]:
            print(f"      反证：{c}")
    if args.action == "apply":
        wrote = apply(con, res, dry_run=getattr(args, "dry_run", False))
        verb = "将写入" if getattr(args, "dry_run", False) else "已写入"
        print(f"{verb} {len(wrote)} 行 → {lineage_write_path()}")
        for w in wrote:
            print("  " + json.dumps(w, ensure_ascii=False))
    return 0
