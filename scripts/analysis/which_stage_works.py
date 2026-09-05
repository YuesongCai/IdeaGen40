"""Which of the three stages carries anything, measured one at a time.

Everything examined so far lives in stage C — the scores a selector ranks on —
and all of it came back empty once volatility was held constant. That verdict is
about the numbers, not about the pipeline: two earlier stages have never been
scored at all, and they are where the founding claim actually sits.

Stage A: eighteen themes are registered and five are chosen each period, each
carrying a `price_indicator`. If reading the week's research picks better themes
than not reading it, the five chosen indicators should beat the thirteen passed
over, over the same thirty days. That is a clean paired test with 6 x 18 = 108
theme-periods and it costs nothing.

Stage B: the generator emits a candidate pool. If the pool is a real narrowing of
the shelf, holding it should beat holding the shelf. If the pool is most of the
shelf every week, there is no narrowing to test and the system is a ranker, not a
finder — which changes what the founding claim can even mean.

Zero model calls. Reads `verdicts`, `candidates`, `prices`.
"""
from __future__ import annotations
from pathlib import Path
import sqlite3, statistics as st, json, math
from datetime import date, timedelta

ROOT = Path(__file__).resolve().parent.parent.parent
DB = str(ROOT / "data" / "ideagen.db")
COST, HORIZON = 0.0008, 30
con = sqlite3.connect(DB); con.row_factory = sqlite3.Row

px: dict[str, dict[str, float]] = {}
for r in con.execute("SELECT code, d, close FROM prices WHERE close>0"):
    px.setdefault(r["code"], {})[r["d"]] = float(r["close"])
S = {c: sorted(v) for c, v in px.items()}

def code_for(i):
    if not i:
        return None
    for c in (i, f"US.{i}", f"HK.{i}"):
        if c in px:
            return c

def fwd(c, p):
    a = next((x for x in S[c] if x >= p), None)
    end = (date.fromisoformat(p) + timedelta(days=HORIZON)).isoformat()
    b = None
    for x in S[c]:
        if x <= end:
            b = x
        else:
            break
    return None if not a or not b or b <= a else px[c][b] / px[c][a] - 1.0 - COST

# ---------------------------------------------------------------- stage A
print("【筛选A】读了本周研报选出的 5 个主题 vs 没选中的 13 个")
print("  比的是各自 price_indicator 之后 30 天的收益，同期配对\n")
print(f"  {'期次':<12}{'选中':>8}{'落选':>8}{'差':>8}   {'选中的主题'}")
rows = []
# One verdict per period. Backfilled reruns left several rows for 08-19 and
# 09-02, and counting them as separate observations would treat one period's
# result as three — the same inflation the arm statistics had.
seen: set[str] = set()
for r in con.execute("SELECT as_of, chosen, scores FROM verdicts "
                     "WHERE kind='topic_scorer' AND strategy='hgep' "
                     "ORDER BY as_of, rowid DESC"):
    p = r["as_of"]
    if p in seen:
        continue
    seen.add(p)
    try:
        chosen = set(json.loads(r["chosen"] or "[]"))
        scores = json.loads(r["scores"] or "{}")
    except Exception:
        continue
    ins, outs = [], []
    for tid, row in scores.items():
        c = code_for(row.get("indicator", "").replace("US.", "").replace("HK.", ""))
        c = c or (row.get("indicator") if row.get("indicator") in px else None)
        if not c:
            continue
        f = fwd(c, p)
        if f is None:
            continue
        (ins if tid in chosen else outs).append(f)
    if len(ins) < 2 or len(outs) < 2:
        continue
    a, b = st.mean(ins), st.mean(outs)
    rows.append((p, a, b, len(ins), len(outs)))
    print(f"  {p:<12}{a*100:>7.2f}%{b*100:>7.2f}%{(a-b)*100:>+7.2f}pp"
          f"   {len(ins)} 选中 / {len(outs)} 落选")
if rows:
    d = [a - b for _, a, b, _, _ in rows]
    sd = st.stdev(d) if len(d) > 1 else float("nan")
    t = st.mean(d) / (sd / len(d) ** .5) if sd else float("nan")
    need = math.ceil(7.849 * sd**2 / st.mean(d)**2) if st.mean(d) else 0
    print(f"\n  配对差均值 {st.mean(d)*100:+.2f}pp   sd {sd*100:.2f}   "
          f"t {t:+.2f}   {sum(1 for x in d if x > 0)}/{len(d)} 期为正"
          f"   80%功效需要 {need} 期")

# ---------------------------------------------------------------- stage B
print("\n\n【筛选B】模型提出的候选池 vs 整个货架")
print("  池子是不是真的在收窄，还是每周把货架原样端出来\n")
shelf = sorted({c for c in px if len(px[c]) > 400})
print(f"  {'期次':<12}{'池内标的':>9}{'占货架':>8}{'池均值':>9}{'货架均值':>10}{'差':>9}")
pool_rows = []
for pr in con.execute("SELECT DISTINCT as_of FROM candidates ORDER BY as_of"):
    p = pr[0]
    codes = set()
    for r in con.execute("SELECT DISTINCT instrument_id FROM candidates WHERE as_of=?", (p,)):
        c = code_for(r[0])
        if c:
            codes.add(c)
    inpool = [f for c in codes if (f := fwd(c, p)) is not None]
    allsh = [f for c in shelf if (f := fwd(c, p)) is not None]
    if len(inpool) < 10 or len(allsh) < 30:
        continue
    a, b = st.mean(inpool), st.mean(allsh)
    pool_rows.append((p, a, b))
    print(f"  {p:<12}{len(inpool):>9}{len(inpool)/len(allsh)*100:>7.0f}%"
          f"{a*100:>8.2f}%{b*100:>9.2f}%{(a-b)*100:>+8.2f}pp")
if pool_rows:
    d = [a - b for _, a, b in pool_rows]
    sd = st.stdev(d) if len(d) > 1 else float("nan")
    t = st.mean(d) / (sd / len(d) ** .5) if sd else float("nan")
    need = math.ceil(7.849 * sd**2 / st.mean(d)**2) if st.mean(d) else 0
    print(f"\n  配对差均值 {st.mean(d)*100:+.2f}pp   sd {sd*100:.2f}   "
          f"t {t:+.2f}   {sum(1 for x in d if x > 0)}/{len(d)} 期为正"
          f"   80%功效需要 {need} 期")
