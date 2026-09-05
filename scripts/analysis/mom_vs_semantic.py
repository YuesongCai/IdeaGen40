"""Put a three-line momentum rule inside the product's own candidate pool.

Every existing control — `buy_all`, `random_pick` — draws from the pool the
language model produced, so all of them share the semantic layer and none can say
whether that layer is worth its cost. The envelope run showed a 21-session
momentum screen on the raw shelf returning 20.8% CAGR against SPY's 18.5% over
105 periods, which makes it the opponent the semantic arms actually have to beat.
This scores it period by period on the same pools, paired, no model calls.
"""
from __future__ import annotations
from pathlib import Path
import sqlite3, statistics as st, json
from datetime import date, timedelta

ROOT = Path(__file__).resolve().parent.parent.parent
DB = str(ROOT / "data" / "ideagen.db")
OUT = Path(__file__).resolve().parent / "_out"
OUT.mkdir(exist_ok=True)
COST, HORIZON, N = 0.0008, 30, 10
con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row

pxrows = con.execute("SELECT code, d, close FROM prices WHERE close>0").fetchall()
px: dict[str, dict[str, float]] = {}
for r in pxrows:
    px.setdefault(r["code"], {})[r["d"]] = float(r["close"])
S = {c: sorted(v) for c, v in px.items()}
last_session = max(max(v) for v in S.values())

def code_for(iid: str) -> str | None:
    for cand in (iid, f"US.{iid}", f"HK.{iid}"):
        if cand in px:
            return cand
    return None

def fwd(c, as_of):
    a = next((x for x in S[c] if x >= as_of), None)
    end = (date.fromisoformat(as_of) + timedelta(days=HORIZON)).isoformat()
    if not a:
        return None, False
    b = None
    for x in S[c]:
        if x <= end:
            b = x
        else:
            break
    if not b or b <= a:
        return None, False
    complete = last_session >= end
    return px[c][b] / px[c][a] - 1.0 - COST, complete

def mom(c, as_of, lb=21):
    prior = [x for x in S[c] if x < as_of]
    if len(prior) < lb + 1:
        return None
    return px[c][prior[-1]] / px[c][prior[-1 - lb]] - 1.0

periods = [r[0] for r in con.execute(
    "SELECT DISTINCT as_of FROM candidates ORDER BY as_of")]

print(f"{'period':<12}{'pool':>5}{'cplt':>6}  "
      f"{'mom_21':>18}{'pool_all':>18}{'ev_rank(top20%)':>18}")
print("-" * 80)
rows = []
for p in periods:
    cands = con.execute(
        "SELECT instrument_id, payload FROM candidates WHERE as_of=?", (p,)).fetchall()
    fr, mo, ev = {}, {}, {}
    complete = True
    for r in cands:
        c = code_for(r["instrument_id"] or "")
        if not c:
            continue
        f, ok = fwd(c, p)
        if f is None:
            continue
        complete = complete and ok
        fr[c] = f
        m = mom(c, p)
        if m is not None:
            mo[c] = m
        try:
            pl = json.loads(r["payload"] or "{}")
            u, d = float(pl["upside_pct"]), float(pl["downside_pct"])
            pu, pb, pd = (float(pl["p_up"]), float(pl["p_base"]),
                          float(pl["p_down"]))
            ev[c] = pu * u + pb * 0.0 + pd * d
        except Exception:
            pass
    if len(fr) < 20:
        continue
    top_m = sorted(mo, key=lambda c: -mo[c])[:N]
    k = max(4, int(0.20 * len(ev)))
    top_e = sorted(ev, key=lambda c: -ev[c])[:k]
    r_m = st.mean([fr[c] for c in top_m])
    r_a = st.mean(fr.values())
    r_e = st.mean([fr[c] for c in top_e]) if top_e else float("nan")
    rows.append({"p": p, "n": len(fr), "complete": complete,
                 "mom": r_m, "all": r_a, "ev": r_e})
    print(f"{p:<12}{len(fr):>5}{'Y' if complete else 'n':>6}  "
          f"{r_m*100:>17.2f}%{r_a*100:>17.2f}%{r_e*100:>17.2f}%")

print("-" * 80)
for k, lab in (("mom", "mom_21"), ("all", "pool_all"), ("ev", "ev_rank")):
    v = [r[k] for r in rows]
    print(f"{lab:<12} mean {st.mean(v)*100:>7.2f}%   "
          f"win {sum(1 for x in v if x>0)/len(v)*100:>3.0f}%")
print("\npaired differences (same period, same pool):")
for a, b in (("mom", "all"), ("ev", "all"), ("ev", "mom")):
    d = [r[a] - r[b] for r in rows]
    sd = st.stdev(d) if len(d) > 1 else float("nan")
    t = st.mean(d) / (sd / len(d) ** .5) if sd else float("nan")
    print(f"  {a:>4} - {b:<9} mean {st.mean(d)*100:+6.2f}pp  "
          f"sd {sd*100:5.2f}  t={t:+5.2f}  n={len(d)}  "
          f"wins {sum(1 for x in d if x>0)}/{len(d)}")
