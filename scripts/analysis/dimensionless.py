"""Is the semantic layer empty, or was one formula shaped wrong.

`ev_c = p_up*upside + p_down*downside` carries units of return, and a model asked
for an up case and a down case will write wider ones for a volatile instrument
whatever it thinks — so that number is a variance estimate by construction, which
is what `partial.py` measured (rho with 60-day vol +0.686, partial with return
+0.008). That result condemns the formula. It does not, on its own, say anything
about whether the model's reading carries information.

The probabilities do not have that problem. `p_up`, `p_up - p_down`, and the
up/down odds ratio are dimensionless: scaling every scenario by an instrument's
volatility leaves them unchanged, so they cannot be a volatility proxy by
construction. If any of them ranks realised return once volatility is held
constant, the reading is worth something and only the formula was wrong. If none
of them does, the numeric output of the semantic layer is empty and the argument
has to move to what else it produces.

Reports raw and vol-partialled rank correlation side by side for each, on the six
real periods. Zero model calls.
"""
from __future__ import annotations
from pathlib import Path
import sqlite3, statistics as st, json, math
from datetime import date, timedelta

ROOT = Path(__file__).resolve().parent.parent.parent
DB = str(ROOT / "data" / "ideagen.db")
COST, HORIZON = 0.0008, 30
ZA, ZB = 1.959964, 0.841621
con = sqlite3.connect(DB); con.row_factory = sqlite3.Row

px: dict[str, dict[str, float]] = {}
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
    return None if not a or not b or b <= a else px[c][b] / px[c][a] - 1.0 - COST

def tvol(c, p, n=60):
    pr = [x for x in S[c] if x < p]
    if len(pr) < n + 1:
        return None
    w = pr[-(n + 1):]
    return st.pstdev([px[c][w[i+1]] / px[c][w[i]] - 1.0 for i in range(n)])

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
    den = math.sqrt(sum((x-ma)**2 for x in a) * sum((y-mb)**2 for y in b))
    return num/den if den else 0.0

def sp(a, b):
    return pearson(ranks(a), ranks(b))

def partial(a, b, c):
    ra, rb, rc = ranks(a), ranks(b), ranks(c)
    ab, ac, bc = pearson(ra, rb), pearson(ra, rc), pearson(rb, rc)
    den = math.sqrt((1-ac**2) * (1-bc**2))
    return (ab - ac*bc)/den if den else float("nan")

#: Every score is a function of what the generator wrote before the period, and
#: of nothing after it. The dimensionless ones are marked, because that property
#: is the whole point of the comparison.
def scores_of(pl: dict) -> dict[str, float] | None:
    try:
        u, dn = float(pl["upside_pct"]), float(pl["downside_pct"])
        pu, pb, pd = float(pl["p_up"]), float(pl["p_base"]), float(pl["p_down"])
    except Exception:
        return None
    tot = pu + pb + pd
    if tot <= 0 or dn == 0:
        return None
    pu, pb, pd = pu/tot, pb/tot, pd/tot
    ev = pu*u + pd*dn
    sig = pl.get("sigma_1m") or pl.get("sigma_h")
    out = {
        "ev_c            (有量纲)": ev,
        "p_up            (无量纲)": pu,
        "p_up - p_down   (无量纲)": pu - pd,
        "赔率 涨幅/|跌幅| (无量纲)": u / abs(dn),
        "凯利 p*b-q      (无量纲)": pu * (u/abs(dn)) - (pd + pb),
    }
    if sig:
        out["ev / sigma_1m  (无量纲)"] = ev / float(sig)
    return out

periods = [r[0] for r in con.execute(
    "SELECT DISTINCT as_of FROM candidates ORDER BY as_of")]
acc: dict[str, list[tuple[float, float]]] = {}
ns = []
for p in periods:
    R, V, F = [], [], {}
    for r in con.execute("SELECT instrument_id,payload FROM candidates WHERE as_of=?", (p,)):
        c = code_for(r["instrument_id"] or "")
        if not c:
            continue
        f, v = fwd(c, p), tvol(c, p)
        if f is None or v is None:
            continue
        try:
            pl = json.loads(r["payload"] or "{}")
        except Exception:
            continue
        sc = scores_of(pl)
        if not sc:
            continue
        R.append(f); V.append(v)
        for k, val in sc.items():
            F.setdefault(k, []).append(val)
    if len(R) < 20:
        continue
    ns.append(len(R))
    for k, vals in F.items():
        if len(vals) != len(R):
            continue
        acc.setdefault(k, []).append((sp(vals, R), partial(vals, R, V)))

print(f"{len(ns)} 期，每期 {st.mean(ns):.0f} 只标的\n")
print(f"{'分数':<26}{'原始 rho':>10}{'t':>7}{'控波动后':>10}{'t':>7}{'需要期数':>9}")
print("-" * 70)
for k, pairs in acc.items():
    raw = [x for x, _ in pairs]; par = [y for _, y in pairs]
    def stat(v):
        m, sd = st.mean(v), (st.stdev(v) if len(v) > 1 else float("nan"))
        return m, (m/(sd/len(v)**.5) if sd else float("nan")), sd
    mr, tr, _ = stat(raw)
    mp, tp, sdp = stat(par)
    need = (math.ceil((ZA+ZB)**2 * sdp**2 / mp**2)
            if abs(mp) > 1e-9 and sdp == sdp else 99999)
    print(f"{k:<26}{mr:>+10.3f}{tr:>+7.2f}{mp:>+10.3f}{tp:>+7.2f}"
          f"{min(need, 99999):>9}")
