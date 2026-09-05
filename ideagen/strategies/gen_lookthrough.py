"""筛选B 第五种：先说出公司，再由持仓数据决定用哪只标的。

The other four arms differ in how the model reasons and agree on how it picks a
vehicle: it reads `_gen.universe_block()` — `代码 | 名称 | 标签 | 载体` — and
chooses a row whose hand-typed label sounds like the thesis. That step is not a
reasoning method at all, it is a lookup, and `lookthrough.py` measured what it
costs. ITA, PPA and XAR all read 国防军工 and hold 53.8%, 45.0% and 26.1% of the
same ten primes; VLUE reads 美股价值因子 and is 21% semiconductors. Choosing
between those by label is choosing by coin flip with extra steps.

This arm splits the decision along the line where the two competences actually
sit. **The model names companies; the holdings data names the vehicle.**

  1. Given the topic, its 打分A evidence and the corpus behind it, the model
     lists the listed companies the thesis is really about. This is semantic
     work and the model is good at it — it is the same reading that produced the
     thesis, just stated in names instead of prose.
  2. `lookthrough.resolve_theme()` scores every eligible instrument by how much
     of its NAV actually sits in that basket, and returns them ranked. This is
     arithmetic over a holdings file and the model is not needed for it.
  3. The model then writes ideas restricted to that measured shortlist, seeing
     the through-weight and the matched names rather than a label.

It is the arm most faithful to the founding premise — semantic analysis leads,
and everything downstream of the semantics is measured rather than asserted.

What it costs, stated up front
------------------------------
**Two model calls per topic instead of one.** Counted honestly: `build_prompt`
returns the extra call so `generate_per_topic` adds it to `calls`, because an
arm that costs double and reports single would look cheaper than it is exactly
where cost is being compared.

**It cannot express every theme, and it says so instead of pretending.** A gold
or FX or managed-futures thesis has no basket of identifiable securities —
those vehicles hold bullion, deposits and futures contracts, and the coverage
floor in `lookthrough.py` refuses to score them. On such a topic this arm raises
and produces nothing, which `generate_per_topic` records as a topic error.

Falling back to the label menu would be worse than producing nothing. The whole
measurement here is "does choosing vehicles by holdings beat choosing them by
label", and an arm that silently reverts to labels on the hard topics answers a
question nobody asked while looking like it answered this one. Fewer candidates
on a stated subset of themes is a real property of the method; a contaminated
comparison is not recoverable later.

Registered `exploratory`
------------------------
Same standing as `ev_rank`, and for the same reason: the mechanism was designed
after looking at the data it will be judged on. Only its live periods from here
count as evidence.
"""

from __future__ import annotations

import re
import threading
from datetime import date
from typing import Any

from . import _gen
from .. import philosophy
from ..strategy import RunContext, Verdict, register

#: How many companies to ask for. Enough that a real theme's breadth shows up —
#: a defence thesis is ten primes, not three — and few enough that the model has
#: to choose rather than list a sector.
BASKET_MIN, BASKET_MAX = 8, 20

#: Vehicles shown to stage two. Ranked by through-weight, so a longer list only
#: adds vehicles with less of the theme in them; twelve is past the point where
#: the next one is a broad index holding the basket incidentally.
SHORTLIST = 12

#: A vehicle below this share of NAV in the basket is not an expression of the
#: theme, it is an index that happens to contain it. SPY holds every defence
#: prime; at 1.6% that fact is not a defence position. Set at 3% because the
#: measured gap in this universe is wide — the real vehicles score 9-85% and the
#: incidental ones score under 3%.
MIN_THEME_WEIGHT = 0.03

#: How old a holdings snapshot may be before this arm stops trusting it.
#: Generous on purpose — funds rebalance monthly, not daily, so a snapshot four
#: days old is fine and one four months old is a different portfolio. The guard
#: exists because a strategy may not fetch: `RunContext` withholds the network so
#: a strategy cannot accidentally read the future, which means staleness has to
#: be refused here rather than repaired here. `ideagen daily` refreshes it.
MAX_SNAPSHOT_AGE_DAYS = 21

BASKET_SHAPE = '{"names":["TICKER", ...],"why":"一句话说明这批公司为什么是这个主题"}'

SHAPE = ('[{"instrument_id":"清单里的 id","thesis":"为什么这一个月会涨",'
         '"vehicle_reason":"为什么这只标的是表达这个主题的最好载体",'
         '"upside_pct":8.0,"downside_pct":-5.0,'
         '"p_up":0.40,"p_base":0.40,"p_down":0.20}]')

_LOCK = threading.Lock()
_CACHE: dict[str, dict] = {}


def _funds(ctx: RunContext) -> dict:
    """The look-through snapshot as of the run date, loaded once per period.

    `as_of` is passed rather than defaulted. A replay of a July period that reads
    September holdings is reading the future — NVDA's weight in QQQ is not a
    constant — and would credit a July thesis with an allocation the fund had not
    yet made. Cached because `generate_per_topic` runs topics on five threads and
    each would otherwise re-read the whole snapshot.
    """
    from .. import db, lookthrough as lt
    con = db.connect()
    # Keyed on the snapshot's own date, not the run date. `ideagen daily`
    # refreshes the snapshot, and a long-lived process that had already loaded
    # the morning's version would keep serving it for the rest of the day —
    # caching under the run date makes that invisible. `latest_as_of` is a MAX()
    # over one small table; the full load is the part worth caching.
    stamp = lt.latest_as_of(con, ctx.as_of)
    if stamp is None:
        return {}
    key = f"{ctx.as_of.isoformat()}@{stamp}"
    with _LOCK:
        if key not in _CACHE:
            _CACHE[key] = lt.load(con, ctx.as_of)
        return _CACHE[key]


def _tickers(raw: Any) -> list[str]:
    """Pull tickers out of whatever shape stage one came back in.

    Models answer this question as a bare list about as often as the object that
    was asked for, and both are usable. Tickers are upper-cased and stripped of
    the decorations models add — `NASDAQ:NVDA`, `NVDA.US`, `$NVDA` — because a
    basket entry that fails to match a holdings row is silently a vote for
    nothing, and this arm's whole output depends on that match.
    """
    if isinstance(raw, dict):
        raw = raw.get("names") or raw.get("tickers") or raw.get("data") or []
    out: list[str] = []
    for x in raw or []:
        s = str(x.get("ticker") or x.get("symbol") or "") if isinstance(x, dict) \
            else str(x)
        s = s.strip().upper().lstrip("$")
        s = s.split(":")[-1]                       # NASDAQ:NVDA -> NVDA
        s = re.sub(r"\.(US|O|N|OQ)$", "", s)       # NVDA.US -> NVDA
        if re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", s) and s not in out:
            out.append(s)
    return out[:BASKET_MAX]


def basket_prompt(ctx: RunContext, topic: dict[str, Any]) -> tuple[str, int]:
    docs, n_docs = _gen.corpus_block(ctx, topic)
    return ("\n\n".join([
        f"今天是 {ctx.as_of.isoformat()}。下面是本周入选的一个主题、它的入选依据，"
        f"以及相关研报摘录。",
        _gen.topic_block(topic),
        "相关研报（新到旧）：\n" + docs,
        f"先不要谈任何 ETF 或基金。请只回答一件事：如果这个主题在未来一个月成立，"
        f"**最直接受益的上市公司**是哪些？给 {BASKET_MIN}-{BASKET_MAX} 家，"
        f"用它们主要上市地的股票代码（如 NVDA、LMT、7203.T）。"
        "只列你认为论点真正指向的公司——宁可少给，也不要为了凑数把整个板块列进来。",
        "只输出 JSON：\n" + BASKET_SHAPE,
    ]), n_docs)


def build_prompt(ctx: RunContext,
                 topic: dict[str, Any],
                 card: dict[str, Any] | None = None
                 ) -> tuple[str, int, int]:
    """两段：先让模型说出公司，再由持仓数据把公司换成可买的标的。

    Returns the third element `generate_per_topic` needs to count stage one's
    call. The raises below are the method behaving correctly, not failing: each
    names a case where the treatment genuinely does not apply, and recording that
    is the point of having the arm.
    """
    from .. import lookthrough as lt

    funds = _funds(ctx)
    if not funds:
        raise RuntimeError(
            "没有可用的穿透快照（先跑 ideagen lookthrough refresh）——"
            "这一种生成方式的全部方法就是按真实持仓选标的，没有持仓它就没有产出")

    stamp = next(iter(funds.values())).as_of
    age = (ctx.as_of - date.fromisoformat(stamp)).days
    if age > MAX_SNAPSHOT_AGE_DAYS:
        raise RuntimeError(
            f"穿透快照停在 {stamp}，距本期 {age} 天（上限 "
            f"{MAX_SNAPSHOT_AGE_DAYS} 天）——按这份持仓选出来的标的，"
            f"表达的是那一天的敞口，不是这一期的")

    p1, n_docs = basket_prompt(ctx, topic)
    raw, calls = _gen.ask_json(ctx, p1)
    names = _tickers(raw)
    if len(names) < 3:
        raise RuntimeError(
            f"第一段没有给出可用的公司名单（解析出 {len(names)} 个）——"
            f"这个主题没有被换成可度量的底层")

    # Only what stage B is allowed to buy. `resolve_theme` scores the whole
    # snapshot, which includes instruments the eligibility gate has already
    # excluded for this date; offering those would produce ideas that cannot be
    # booked.
    buyable = {str(u.get("instrument_id")) for u in ctx.universe}
    hits = [h for h in lt.resolve_theme(funds, names)
            if h.symbol in buyable and h.weight >= MIN_THEME_WEIGHT][:SHORTLIST]
    if not hits:
        opaque = sorted(s for s in buyable
                        if s in funds and not funds[s].usable)
        raise RuntimeError(
            f"名单 {'、'.join(names[:8])} 在可买清单里没有任何标的的穿透权重达到 "
            f"{MIN_THEME_WEIGHT*100:.0f}%——这个主题在当前货架上没有可度量的载体"
            + (f"（{len(opaque)} 只看不透的标的未参与打分）" if opaque else ""))

    by_id = {str(u.get("instrument_id")): u for u in ctx.universe}
    lines = []
    for h in hits:
        u = by_id.get(h.symbol, {})
        lines.append(f"{h.symbol} | {u.get('name')} | "
                     f"该主题真实穿透权重 {h.weight*100:.1f}% | "
                     f"其中持有：{'、'.join(h.matched[:8])}")

    blocks = [
        f"今天是 {ctx.as_of.isoformat()}。",
        _gen.topic_block(topic),
        "你刚才判断这个主题真正指向的公司是：" + "、".join(names),
        "下面是把这批公司拿去比对**全部可买标的的真实持仓**之后，"
        "按「这只标的有多少净值确实压在这批公司上」排出来的候选。"
        "这个百分比来自基金披露的持仓文件，不是标的名称或分类标签：\n"
        + "\n".join(lines),
        f"请就这个主题给出 {_gen.PER_TOPIC} 条持有期一个月的做多想法，"
        "标的只能从上面这张候选表里选，用 instrument_id 原样填写。",
        "每条要写清：一个月内为什么涨、上行与下行幅度（百分数）、"
        "上行/持平/下行三档概率（相加为 1），"
        "以及 vehicle_reason —— 为什么在候选表里选这一只而不是穿透权重更高的那只。"
        "穿透权重最高不等于最好：更高的权重通常也意味着更集中、波动更大，"
        "这个取舍要你自己说清楚。同一主题内不要重复标的。",
    ]
    if card is not None:
        blocks.append(philosophy.render(card))
    blocks += [_gen.CITATION_RULE, "只输出 JSON 数组，形如：\n" + SHAPE]
    return "\n\n".join(blocks), n_docs, calls


@register("idea_generator", "lookthrough", "1.0", role="exploratory",
          label="穿透反查")
def lookthrough_gen(ctx: RunContext) -> Verdict:
    """模型说出公司，持仓数据决定标的：语义归语义，映射归数据。"""
    return _gen.generate_per_topic(ctx, "lookthrough", build_prompt,
                                   extra_keys=("vehicle_reason",))
