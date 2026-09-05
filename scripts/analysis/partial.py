"""Does ev_c still rank returns once trailing volatility is held constant.

An information coefficient of 0.23 is several times what cross-sectional equity
signals normally carry, and the earlier falsification found ev's quintiles were
also a volatility ladder. In a rising market those two facts have the same
fingerprint, so the correlation has to be re-measured with vol partialled out
before the number means anything about semantics.
"""
from __future__ import annotations
from pathlib import Path
import sqlite3, statistics as st, json, math
from datetime import date, timedelta

ROOT = Path(__file__).resolve().parent.parent.parent
DB = str(ROOT / "data" / "ideagen.db")
OUT = Path(__file__).resolve().parent / "_out"
OUT.mkdir(exist_ok=True)
COST, HORIZON = 0.0008, 30
con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
px = {}
for r in con.execute("SELECT code, d, close FROM prices WHERE close>0"):
    px.setdefault(r["code"], {})[r["d"]] = float(r["close"])
S = {c: sorted(v) for c, v in px.items()}

def code_for(i):
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
    return None if not a or not b or b <= a else px[c][b]/px[c][a]-1.0-COST

def tvol(c, p, n=60):
    pr = [x for x in S[c] if x < p]
    if len(pr) < n+1:
        return None
    w = pr[-(n+1):]
    return st.pstdev([px[c][w[i+1]]/px[c][w[i]]-1.0 for i in range(n)])

def ranks(v):
    n = len(v); o = sorted(range(n), key=lambda i: v[i]); r = [0.0]*n
    i = 0
    while i < n:
        j = i
        while j+1 < n and v[o[j+1]] == v[o[i]]:
            j += 1
        for k in range(i, j+1):
            r[o[k]] = (i+j)/2 + 1
        i = j+1
    return r

def pearson(a, b):
    ma, mb = st.mean(a), st.mean(b)
    num = sum((x-ma)*(y-mb) for x, y in zip(a, b))
    den = math.sqrt(sum((x-ma)**2 for x in a)*sum((y-mb)**2 for y in b))
    return num/den if den else 0.0

def sp(a, b):
    return pearson(ranks(a), ranks(b))

def partial(a, b, c):
    """Spearman(a,b) with c held constant, on ranks."""
    ra, rb, rc = ranks(a), ranks(b), ranks(c)
    rab, rac, rbc = pearson(ra, rb), pearson(ra, rc), pearson(rb, rc)
    den = math.sqrt((1-rac**2)*(1-rbc**2))
    return (rab - rac*rbc)/den if den else float("nan")

periods = [r[0] for r in con.execute(
    "SELECT DISTINCT as_of FROM candidates ORDER BY as_of")]
raw, pv, evvol, volret = [], [], [], []
print(f"{'period':<12}{'n':>4}{'rho(ev,ret)':>13}{'rho(ev,vol)':>13}"
      f"{'rho(vol,ret)':>14}{'partial':>10}")
print("-" * 68)
for p in periods:
    R, E, V = [], [], []
    for r in con.execute("SELECT instrument_id,payload FROM candidates WHERE as_of=?", (p,)):
        c = code_for(r["instrument_id"] or "")
        if not c:
            continue
        f, v = fwd(c, p), tvol(c, p)
        if f is None or v is None:
            continue
        try:
            pl = json.loads(r["payload"] or "{}")
            e = (float(pl["p_up"])*float(pl["upside_pct"])
                 + float(pl["p_down"])*float(pl["downside_pct"]))
        except Exception:
            continue
        R.append(f); E.append(e); V.append(v)
    if len(R) < 20:
        continue
    a, b, cc = sp(E, R), sp(E, V), sp(V, R)
    q = partial(E, R, V)
    raw.append(a); evvol.append(b); volret.append(cc); pv.append(q)
    print(f"{p:<12}{len(R):>4}{a:>13.3f}{b:>13.3f}{cc:>14.3f}{q:>10.3f}")
print("-" * 68)
ZA, ZB = 1.959964, 0.841621
for lab, v in (("raw rho(ev,ret)", raw), ("partial | vol", pv),
               ("rho(ev,vol)", evvol), ("rho(vol,ret)", volret)):
    m, sd = st.mean(v), st.stdev(v)
    t = m/(sd/len(v)**.5)
    n = math.ceil((ZA+ZB)**2*sd**2/m**2) if abs(m) > 1e-9 else 999
    print(f"{lab:<18} mean {m:+.3f}  sd {sd:.3f}  t {t:+5.2f}  n_needed {n}")
