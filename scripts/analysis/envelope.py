"""How much return is available in this shelf under this structure.

The end goal is a number — 25% a year — and no backtest can ever test it: at a
13% portfolio vol, separating 1.9%/month from the market's 0.8%/month at 80%
power needs about a hundred monthly observations, i.e. eight years. So the
question has to be changed into one that six periods, or a hundred, can answer:

    Given this universe and this 4-tranche / 10-name / 30-day structure, how much
    return exists to be captured, and what fraction of it must the ranking capture
    to reach 25%?

That is measurable today with nothing but `prices`: no corpus, no model calls.
The oracle arm is the ceiling, the anti-oracle the floor, the random draws the
null. Where 25% sits between them is the honest statement of what is being asked
of the semantic layer.

Conventions match `backtest.outcome_for`: entry is the first close on or after
as_of, exit the last close on or before as_of+30d, net of an 8bp round trip.
"""
from __future__ import annotations
from pathlib import Path
import sqlite3, statistics as st, random, math, json
from datetime import date, timedelta

ROOT = Path(__file__).resolve().parent.parent.parent
DB = str(ROOT / "data" / "ideagen.db")
OUT = Path(__file__).resolve().parent / "_out"
OUT.mkdir(exist_ok=True)
COST = 0.0008
HORIZON = 30
N_PICK = 10
START = date(2024, 8, 7)          # first Wednesday with 21 sessions of history
RANDOM_DRAWS = 2000

con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row

codes = [r[0] for r in con.execute(
    "SELECT code FROM prices GROUP BY code HAVING COUNT(*) > 400").fetchall()]
px: dict[str, dict[str, float]] = {}
days_all: set[str] = set()
for c in codes:
    s = {r[0]: float(r[1]) for r in con.execute(
        "SELECT d, close FROM prices WHERE code=? AND close>0", (c,)).fetchall()}
    px[c] = s
    days_all |= set(s)
sessions = sorted(days_all)
print(f"universe {len(codes)} codes, {len(sessions)} sessions "
      f"{sessions[0]} -> {sessions[-1]}")

def on_or_after(c: str, d: str) -> tuple[str, float] | None:
    s = px[c]
    for x in sessions:
        if x >= d and x in s:
            return x, s[x]
    return None

def on_or_before(c: str, d: str) -> tuple[str, float] | None:
    s = px[c]
    for x in reversed(sessions):
        if x <= d and x in s:
            return x, s[x]
    return None

# Index sessions for speed.
idx = {d: i for i, d in enumerate(sessions)}

def fwd_ret(c: str, as_of: str) -> float | None:
    a = on_or_after(c, as_of)
    if not a:
        return None
    end = (date.fromisoformat(as_of) + timedelta(days=HORIZON)).isoformat()
    if end > sessions[-1]:
        return None
    b = on_or_before(c, end)
    if not b or b[0] <= a[0]:
        return None
    return b[1] / a[1] - 1.0 - COST

def trail_ret(c: str, as_of: str, lookback_sessions: int,
              skip: int = 0) -> float | None:
    """Momentum measured strictly before as_of — no as_of bar in the window."""
    prior = [x for x in sessions if x < as_of and x in px[c]]
    if len(prior) < lookback_sessions + skip + 1:
        return None
    end = prior[-1 - skip]
    start = prior[-1 - skip - lookback_sessions]
    return px[c][end] / px[c][start] - 1.0

def trail_vol(c: str, as_of: str, n: int = 60) -> float | None:
    prior = [x for x in sessions if x < as_of and x in px[c]]
    if len(prior) < n + 1:
        return None
    w = prior[-(n + 1):]
    rs = [px[c][w[i + 1]] / px[c][w[i]] - 1.0 for i in range(len(w) - 1)]
    return st.pstdev(rs) if len(rs) > 1 else None

# Weekly Wednesdays.
periods: list[str] = []
d = START
while d.isoformat() <= sessions[-1]:
    if d.weekday() == 2:
        periods.append(d.isoformat())
    d += timedelta(days=1)

rows = []
for p in periods:
    fr = {c: r for c in codes if (r := fwd_ret(c, p)) is not None}
    if len(fr) < 40:
        continue
    rows.append((p, fr))
print(f"{len(rows)} weekly periods with a complete 30-day window "
      f"{rows[0][0]} -> {rows[-1][0]}")

def basket(fr: dict[str, float], picks: list[str]) -> float:
    v = [fr[c] for c in picks if c in fr]
    return st.mean(v) if v else 0.0

rng = random.Random(20260905)
arms: dict[str, list[float]] = {k: [] for k in (
    "oracle", "anti_oracle", "hold_all", "mom_21", "mom_63", "mom_252_21",
    "low_vol", "high_vol", "spy")}
random_dist: list[list[float]] = []

for p, fr in rows:
    ranked = sorted(fr, key=lambda c: -fr[c])
    arms["oracle"].append(basket(fr, ranked[:N_PICK]))
    arms["anti_oracle"].append(basket(fr, ranked[-N_PICK:]))
    arms["hold_all"].append(st.mean(fr.values()))
    for name, key in (("mom_21", (21, 0)), ("mom_63", (63, 0)),
                      ("mom_252_21", (231, 21))):
        lb, sk = key
        sc = {c: v for c in fr if (v := trail_ret(c, p, lb, sk)) is not None}
        top = sorted(sc, key=lambda c: -sc[c])[:N_PICK]
        arms[name].append(basket(fr, top))
    vol = {c: v for c in fr if (v := trail_vol(c, p)) is not None}
    arms["low_vol"].append(basket(fr, sorted(vol, key=lambda c: vol[c])[:N_PICK]))
    arms["high_vol"].append(basket(fr, sorted(vol, key=lambda c: -vol[c])[:N_PICK]))
    arms["spy"].append(fr.get("US.SPY", 0.0))
    pool = list(fr)
    random_dist.append([basket(fr, rng.sample(pool, min(N_PICK, len(pool))))
                        for _ in range(RANDOM_DRAWS)])

def annualise(mean_30d: float) -> float:
    return (1.0 + mean_30d) ** (365.0 / HORIZON) - 1.0

print("\n" + "=" * 78)
print(f"{'arm':<14}{'mean 30d':>10}{'ann.':>9}{'win%':>7}"
      f"{'sd(30d)':>9}{'worst':>9}{'best':>9}")
print("-" * 78)
res = {}
for k, v in arms.items():
    m = st.mean(v)
    res[k] = {"mean_30d": m, "ann": annualise(m),
              "win": sum(1 for x in v if x > 0) / len(v),
              "sd": st.pstdev(v), "min": min(v), "max": max(v)}
    print(f"{k:<14}{m*100:>9.2f}%{annualise(m)*100:>8.1f}%"
          f"{res[k]['win']*100:>6.0f}%{res[k]['sd']*100:>8.2f}%"
          f"{min(v)*100:>8.2f}%{max(v)*100:>8.2f}%")

# The null: each draw is a full path of weekly random baskets.
paths = [st.mean([random_dist[i][j] for i in range(len(rows))])
         for j in range(RANDOM_DRAWS)]
paths.sort()
def pct_of(x: float) -> float:
    lo = sum(1 for p in paths if p < x)
    return 100.0 * lo / len(paths)

target_30d = 1.25 ** (30.0 / 365.0) - 1.0
print("-" * 78)
print(f"random null over {RANDOM_DRAWS} paths: mean {st.mean(paths)*100:.2f}% "
      f"({annualise(st.mean(paths))*100:.1f}% ann.)  "
      f"p5 {paths[int(.05*len(paths))]*100:.2f}%  "
      f"p95 {paths[int(.95*len(paths))]*100:.2f}%")
print(f"25%/yr needs a 30-day basket mean of {target_30d*100:.2f}% "
      f"-> percentile {pct_of(target_30d):.1f} of the random null")
gap = target_30d - res["hold_all"]["mean_30d"]
span = res["oracle"]["mean_30d"] - res["hold_all"]["mean_30d"]
print(f"shelf beta gives {res['hold_all']['mean_30d']*100:.2f}%; "
      f"perfect ranking adds {span*100:.2f}pp; "
      f"25% needs {gap*100:.2f}pp = {100*gap/span:.1f}% of perfect foresight")
json.dump({"arms": res, "target_30d": target_30d,
           "target_pctile": pct_of(target_30d),
           "n_periods": len(rows), "first": rows[0][0], "last": rows[-1][0],
           "capture_needed_frac": gap / span},
          open(str(OUT / "envelope.json"), "w"), indent=1)
