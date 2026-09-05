"""Second pass: does anything in the envelope survive a change of regime, and
what does the four-tranche structure actually do to drawdown.

The six periods the product currently reports are one regime — the market rose in
four of six. Nothing measured inside them can distinguish a rule that ranks return
from a rule that ranks risk, because over that stretch the two coincide. 105
periods contain both, so the split is the cheapest available test of every claim
made on six.
"""
from __future__ import annotations
from pathlib import Path
import sqlite3, statistics as st, random, json
from datetime import date, timedelta

ROOT = Path(__file__).resolve().parent.parent.parent
DB = str(ROOT / "data" / "ideagen.db")
OUT = Path(__file__).resolve().parent / "_out"
OUT.mkdir(exist_ok=True)
COST, HORIZON, N_PICK = 0.0008, 30, 10
con = sqlite3.connect(DB)

codes = [r[0] for r in con.execute(
    "SELECT code FROM prices GROUP BY code HAVING COUNT(*) > 400").fetchall()]
px = {c: {r[0]: float(r[1]) for r in con.execute(
    "SELECT d, close FROM prices WHERE code=? AND close>0", (c,))} for c in codes}
sessions = sorted({d for s in px.values() for d in s})
S = {c: sorted(px[c]) for c in codes}

def on_or_after(c, d):
    for x in S[c]:
        if x >= d:
            return x
    return None

def on_or_before(c, d):
    prev = None
    for x in S[c]:
        if x <= d:
            prev = x
        else:
            break
    return prev

def fwd(c, as_of):
    a = on_or_after(c, as_of)
    end = (date.fromisoformat(as_of) + timedelta(days=HORIZON)).isoformat()
    if not a or end > sessions[-1]:
        return None
    b = on_or_before(c, end)
    if not b or b <= a:
        return None
    return px[c][b] / px[c][a] - 1.0 - COST

def trail(c, as_of, lb, skip=0):
    prior = [x for x in S[c] if x < as_of]
    if len(prior) < lb + skip + 1:
        return None
    return px[c][prior[-1 - skip]] / px[c][prior[-1 - skip - lb]] - 1.0

def tvol(c, as_of, n=60):
    prior = [x for x in S[c] if x < as_of]
    if len(prior) < n + 1:
        return None
    w = prior[-(n + 1):]
    return st.pstdev([px[c][w[i+1]]/px[c][w[i]] - 1.0 for i in range(n)])

periods = []
d = date(2024, 8, 7)
while d.isoformat() <= sessions[-1]:
    if d.weekday() == 2:
        periods.append(d.isoformat())
    d += timedelta(days=1)

rows = []
for p in periods:
    fr = {c: r for c in codes if (r := fwd(c, p)) is not None}
    if len(fr) >= 40:
        rows.append((p, fr))

def picks(p, fr, arm):
    if arm == "oracle":
        return sorted(fr, key=lambda c: -fr[c])[:N_PICK]
    if arm == "hold_all":
        return list(fr)
    if arm.startswith("mom"):
        lb, sk = {"mom_21": (21, 0), "mom_63": (63, 0),
                  "mom_252_21": (231, 21)}[arm]
        sc = {c: v for c in fr if (v := trail(c, p, lb, sk)) is not None}
        return sorted(sc, key=lambda c: -sc[c])[:N_PICK]
    if arm == "high_vol":
        v = {c: x for c in fr if (x := tvol(c, p)) is not None}
        return sorted(v, key=lambda c: -v[c])[:N_PICK]
    if arm == "low_vol":
        v = {c: x for c in fr if (x := tvol(c, p)) is not None}
        return sorted(v, key=lambda c: v[c])[:N_PICK]
    raise KeyError(arm)

ARMS = ["hold_all", "mom_21", "mom_63", "mom_252_21", "high_vol", "low_vol"]
per = {a: [] for a in ARMS}
spy = []
for p, fr in rows:
    for a in ARMS:
        b = [fr[c] for c in picks(p, fr, a) if c in fr]
        per[a].append(st.mean(b) if b else 0.0)
    spy.append(fr.get("US.SPY", 0.0))

# Regime = SPY's own forward 30-day return for that period.
up = [i for i, s in enumerate(spy) if s > 0]
dn = [i for i, s in enumerate(spy) if s <= 0]
print(f"{len(rows)} periods: {len(up)} with SPY up, {len(dn)} with SPY down\n")
print(f"{'arm':<13}{'all':>9}{'SPY up':>9}{'SPY down':>10}"
      f"{'up-beta':>9}{'dn-beta':>9}")
print("-" * 60)
def ann(m):
    return (1 + m) ** (365 / HORIZON) - 1
base_u, base_d = st.mean([spy[i] for i in up]), st.mean([spy[i] for i in dn])
out = {}
for a in ARMS + ["spy"]:
    v = per[a] if a in per else spy
    mu, md = st.mean([v[i] for i in up]), st.mean([v[i] for i in dn])
    out[a] = {"all": st.mean(v), "up": mu, "down": md,
              "ann_all": ann(st.mean(v))}
    print(f"{a:<13}{st.mean(v)*100:>8.2f}%{mu*100:>8.2f}%{md*100:>9.2f}%"
          f"{mu/base_u:>9.2f}{md/base_d:>9.2f}")

# Four-tranche daily NAV, cash in T-bills at 4%/yr, so drawdown is the
# portfolio's and not one basket's.
def nav_path(arm):
    cash, slots = 1.0, []          # slots: (exit_d, shares{code: qty})
    navs = []
    opens = {p for p, _ in rows}
    byp = dict(rows)
    for i, d in enumerate(sessions):
        if i:
            cash *= (1 + 0.04) ** (1 / 252)
        for s in list(slots):
            if d >= s[0]:
                cash += sum(q * px[c][on_or_before(c, s[0])]
                            for c, q in s[1].items()) * (1 - COST / 2)
                slots.remove(s)
        if d in opens and len(slots) < 4:
            fr = byp[d]
            ps = [c for c in picks(d, fr, arm) if on_or_after(c, d)]
            mv = sum(q * px[c][on_or_before(c, d)]
                     for s in slots for c, q in s[1].items())
            stake = (cash + mv) / 4.0
            if ps and stake <= cash:
                each = stake / len(ps)
                sh = {c: each / px[c][on_or_after(c, d)] for c in ps}
                cash -= stake
                end = (date.fromisoformat(d) + timedelta(days=HORIZON)).isoformat()
                slots.append((end, sh))
        mv = sum(q * (px[c].get(on_or_before(c, d)) or 0)
                 for s in slots for c, q in s[1].items())
        navs.append((d, cash + mv))
    return navs

print(f"\n{'arm':<13}{'CAGR':>9}{'vol':>8}{'maxDD':>9}{'ret/vol':>9}")
print("-" * 50)
navs_out = {}
for a in ["hold_all", "mom_21", "mom_63", "low_vol"]:
    n = nav_path(a)
    v = [x[1] for x in n]
    yrs = len(v) / 252
    cagr = (v[-1] / v[0]) ** (1 / yrs) - 1
    rets = [v[i+1]/v[i] - 1 for i in range(len(v)-1)]
    vol = st.pstdev(rets) * (252 ** .5)
    peak, dd = v[0], 0.0
    for x in v:
        peak = max(peak, x)
        dd = min(dd, x / peak - 1)
    navs_out[a] = {"cagr": cagr, "vol": vol, "maxdd": dd}
    print(f"{a:<13}{cagr*100:>8.1f}%{vol*100:>7.1f}%{dd*100:>8.1f}%"
          f"{cagr/vol:>9.2f}")
sp = [px["US.SPY"][d] for d in S["US.SPY"] if d >= sessions[0]]
yrs = len(sp) / 252
peak, dd = sp[0], 0.0
for x in sp:
    peak = max(peak, x); dd = min(dd, x / peak - 1)
rets = [sp[i+1]/sp[i] - 1 for i in range(len(sp)-1)]
vol = st.pstdev(rets) * (252 ** .5)
cagr = (sp[-1]/sp[0]) ** (1/yrs) - 1
print(f"{'SPY':<13}{cagr*100:>8.1f}%{vol*100:>7.1f}%{dd*100:>8.1f}%{cagr/vol:>9.2f}")
json.dump({"regime": out, "nav": navs_out}, open(
    str(OUT / "envelope2.json"), "w"), indent=1)
