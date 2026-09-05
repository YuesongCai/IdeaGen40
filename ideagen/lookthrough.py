"""What the universe actually owns, and what follows from knowing it.

`universe.py` describes an instrument by one hand-written `exposure` string, and
`_gen.universe_block` hands those strings to the generator as the menu it picks
from. The docstring there already names the hazard it was written to fix —
without a label "the model is matching on ticker strings". A label is a better
menu than a ticker. It is still a claim about a security rather than a fact
about it, and it fails in both directions:

  *Same label, different fund.* USMV and SPLV are both 美股低波动 and share 27%
  of weight. ITA / PPA / XAR are all 国防军工 and hold 53.7% / 45.0% / 26.1% of
  the same ten defence primes. The generator sees interchangeable rows and picks
  by position in a list.

  *Different label, same fund.* VLUE is 美股价值因子 and is 21% semiconductors.
  A thesis that buys SMH and hedges it with VLUE has hedged a fifth of the bet
  with itself, and nothing in the pipeline could have said so.

Three things follow, and this module is all three.

1. **Reverse mapping.** A theme names companies before it names products. Score
   every fund by how much of its NAV sits in the theme's basket and the ranking
   is evidence rather than vocabulary: the strongest AI-power vehicle in this
   universe is GRID, labelled 智能电网与电气化, which no name match on "AI 数据
   中心" reaches, and IYR — 美国房地产 — delivers 9% of it through DLR and EQIX.

2. **Real concentration.** Ten ETFs are not ten bets. `portfolio()` reports the
   effective number of *underlying* names, which is the measurable form of the
   `robustness_drop_top` finding: an edge that dies when twenty securities are
   removed was never spread across the products that held them.

3. **A coverage floor.** Futures-backed and physically-backed funds report
   unnamed rows; DBC and PDBC look 0% alike and are both broad commodity
   baskets. Every answer here carries the fraction of NAV it could identify, and
   comparisons below `MIN_COVERAGE` return `None` rather than a number. A
   confident wrong answer is worse than a refusal, and this is the one place in
   the pipeline where the data invites one.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Sequence

from . import config, db
from .sources import fmp

#: Below this share of NAV in identified, non-cash securities, two funds are
#: not comparable and a through-weight is not a measurement. Set where it is
#: because the equity and credit funds in this universe all clear 0.9 while every
#: futures-backed one falls under 0.5 — there is nothing in between to be
#: arbitrary about.
#:
#: The measure is deliberately net of cash. FXY, FXE and UUP report identifiers
#: for 100% of NAV and hold nothing but currency deposits; read on gross
#: coverage they are perfectly transparent funds whose look-through is empty,
#: and `differentiator` duly printed "前0大（合计 0%）". Their exposure is JPY,
#: EUR, USD — which does not live in the holdings name space at all, so the only
#: honest answer is that they cannot be seen through. BIL falls the same way and
#: for the same reason: a fund that is entirely T-bills *is* cash.
MIN_COVERAGE = 0.60

#: Cash sleeves are real holdings and terrible risk exposures. Every broad ETF
#: carries a few percent of them, so leaving them in makes "CASH & EQUIVALENTS"
#: the largest look-through name of almost any portfolio — a true statement that
#: answers no question anyone asked. They are excluded from concentration and
#: theme scoring, and counted separately so the exclusion stays visible.
_CASH_RE = re.compile(
    r"cash|csh\s*fnd|money\s*(market|portfolio)|treasury\s*bill|t-bill|repo|"
    r"net\s+other\s+assets|liquidity|deposit|govt?\s+money|AGPXX|"
    r"pending\s+dividend|^US DOLLARS?$",
    re.I)


#: US Treasury bills, by identifier rather than by name. KMLM's five holdings
#: are `US912797VE44`-style ISINs labelled `B 09/29/26`: nothing in that string
#: says "cash", and the fund's actual exposure — the KFA MLM index futures — is
#: not reported at all. Read as securities they made KMLM 65% "transparent" and
#: gave it 0% overlap with DBMF, another trend fund, which then looked like a
#: finding about two same-labelled products differing. It was collateral.
#: `912797` is the CUSIP issue prefix for T-bills, so this is a fact about the
#: instrument rather than a guess about its description.
_TBILL_PREFIXES = ("US912797", "912797")

#: Futures and swaps, which a holdings file lists with a weight that is notional
#: exposure rather than share of NAV. DBMF reports four CBT contracts under
#: vendor pseudo-CUSIPs; adding those weights to equity weights is a category
#: error before it is a coverage problem, so they are excluded from the security
#: space entirely rather than counted and then apologised for.
_DERIV_RE = re.compile(
    r"\b(fut|futr|future|futures)\b|\b(cbt|cme|nymex|comex|ice|cboe|eurex|"
    r"sgx|liffe)\b|curr\s*fut|index\s*swap|total\s*return\s*swap",
    re.I)


def is_cash(name: str, key: str = "") -> bool:
    """Cash, deposits, money funds and T-bill collateral — not risk exposures."""
    if key and any(key.startswith(p) for p in _TBILL_PREFIXES):
        return True
    return bool(_CASH_RE.search(name or ""))


def is_derivative(name: str) -> bool:
    return bool(_DERIV_RE.search(name or ""))


def excluded(name: str, key: str = "") -> bool:
    """Everything that is not a look-through-able security holding."""
    return is_cash(name, key) or is_derivative(name)


# --------------------------------------------------------------- storage
# These tables are created here rather than in `schema.py`. That module is a
# shared surface several sessions edit at once, and a look-through cache is not
# state the rest of the pipeline needs to know the shape of — nothing else reads
# these tables, so nothing else needs them declared centrally.
DDL = (
    """CREATE TABLE IF NOT EXISTS etf_lookthrough (
         symbol       TEXT NOT NULL,
         as_of        TEXT NOT NULL,
         asset        TEXT NOT NULL,          -- ISIN where the vendor has one
         label        TEXT NOT NULL DEFAULT '',
         weight       REAL NOT NULL,
         PRIMARY KEY (symbol, as_of, asset)
       )""",
    """CREATE TABLE IF NOT EXISTS etf_lookthrough_runs (
         symbol       TEXT NOT NULL,
         as_of        TEXT NOT NULL,
         coverage     REAL NOT NULL,
         rows_seen    INTEGER NOT NULL,
         named        INTEGER NOT NULL,
         status       TEXT NOT NULL,
         note         TEXT,
         PRIMARY KEY (symbol, as_of)
       )""",
    "CREATE INDEX IF NOT EXISTS ix_lt_asset ON etf_lookthrough(asset, as_of)",
)


def ensure_schema(con) -> None:
    """Create the cache tables, and add columns a live table predates.

    `CREATE TABLE IF NOT EXISTS` is a no-op against a table of the same name and
    an older shape, so a column added after the first snapshot shipped has to be
    added explicitly or every read of it fails on a machine that ran the earlier
    version. That is exactly how `first_seen_d` reached HEAD in this repo with
    its DDL left behind: invisible locally, broken on every fresh clone.
    """
    for stmt in DDL:
        con.execute(stmt)
    have = {r[1] for r in con.execute("PRAGMA table_info(etf_lookthrough)")}
    if "label" not in have:
        con.execute("ALTER TABLE etf_lookthrough ADD COLUMN label TEXT"
                    " NOT NULL DEFAULT ''")
    con.commit()


@dataclass(frozen=True)
class Fund:
    """One fund's look-through as stored, with its own honesty attached."""
    symbol: str
    as_of: str
    weights: dict[str, float]      # canonical security id -> share of NAV
    labels: dict[str, str]         # same ids -> a name a person can read
    coverage: float
    rows_seen: int
    status: str          # ok | opaque | not_a_fund | error
    note: str = ""       # why, when status is not ok

    @property
    def usable(self) -> bool:
        return self.status == "ok" and self.coverage >= MIN_COVERAGE

    def name(self, key: str) -> str:
        return self.labels.get(key) or key


def risk_coverage(weights: dict[str, float], labels: dict[str, str],
                  coverage: float) -> float:
    """The share of NAV that is both identified and an actual risk exposure."""
    noncash = sum(w for k, w in weights.items()
                  if not excluded(labels.get(k, k), k))
    return coverage * noncash


def _classify(weights: dict[str, float], labels: dict[str, str],
              coverage: float, rows: int) -> tuple[str, float, str]:
    """(status, risk coverage, why not) for one fund's holdings file."""
    if rows == 0:
        return "not_a_fund", 0.0, "厂商没有返回任何持仓行——这不是一只基金"
    if not weights:
        return "opaque", 0.0, (f"{rows} 行持仓全部没有标识符"
                               f"（期货 / 实物 / 掉期），无法穿透")
    rc = risk_coverage(weights, labels, coverage)
    if rc < MIN_COVERAGE:
        if rc < 0.02:
            return "opaque", rc, ("净值几乎全部是现金 / 存款 / 期货合约，"
                                  "敞口不在持仓名单这个空间里表达")
        return "opaque", rc, (f"只认出 {rc*100:.0f}% 的净值是可识别的非现金证券，"
                              f"低于 {MIN_COVERAGE*100:.0f}% 下限，不做比较")
    return "ok", rc, ""


def refresh(con, symbols: Sequence[str], as_of: date | None = None,
            workers: int = 8) -> dict[str, Fund]:
    """Pull holdings for `symbols` and persist them under one `as_of`.

    Written concurrently because the vendor's latency scales with holdings count
    — SPY answers in three seconds and IWM in twenty-two — so a serial sweep of
    this universe costs a quarter of an hour for work that takes ninety seconds.

    A symbol that fails is stored as a run row with its reason rather than
    omitted. An absent row and a failed row look identical to every later query,
    and that is how a port silently degrades into producing nothing while
    reporting success — the failure mode this repo already met on the Olive leg.
    """
    from concurrent.futures import ThreadPoolExecutor
    ensure_schema(con)
    d = (as_of or config.today_hkt()).isoformat()

    def one(sym: str) -> Fund:
        try:
            w, labels, cov, rows = fmp.holdings(sym)
        except fmp.FMPError as e:
            # An error is not an empty fund. Re-deriving the note from the empty
            # weights below would file a failed call as "not a fund", which is
            # the same wrong answer this whole module is built to avoid.
            return Fund(sym, d, {}, {}, 0.0, 0, "error", str(e)[:300])
        status, rc, note = _classify(w, labels, cov, rows)
        # `coverage` on the Fund is the risk-bearing one, because that is what
        # every downstream comparison actually needs; the gross figure only
        # answers "did the vendor name the rows", which no caller asks.
        return Fund(sym, d, w, labels, rc, rows, status, note)

    with ThreadPoolExecutor(max(1, workers)) as ex:
        funds = list(ex.map(one, symbols))

    with db.tx(con) as c:
        for f in funds:
            c.execute("DELETE FROM etf_lookthrough WHERE symbol=? AND as_of=?",
                      (f.symbol, d))
            if f.weights:
                c.executemany(
                    "INSERT OR REPLACE INTO etf_lookthrough"
                    "(symbol, as_of, asset, label, weight) VALUES (?,?,?,?,?)",
                    [(f.symbol, d, a, f.name(a), w)
                     for a, w in f.weights.items()])
            c.execute(
                "INSERT OR REPLACE INTO etf_lookthrough_runs"
                "(symbol, as_of, coverage, rows_seen, named, status, note)"
                " VALUES (?,?,?,?,?,?,?)",
                (f.symbol, d, f.coverage, f.rows_seen, len(f.weights),
                 f.status, f.note or None))
    return {f.symbol: f for f in funds}


def latest_as_of(con, on_or_before: date | None = None) -> str | None:
    ensure_schema(con)
    if on_or_before is None:
        r = con.execute(
            "SELECT MAX(as_of) FROM etf_lookthrough_runs").fetchone()
    else:
        r = con.execute("SELECT MAX(as_of) FROM etf_lookthrough_runs"
                        " WHERE as_of<=?",
                        (on_or_before.isoformat(),)).fetchone()
    return r[0] if r and r[0] else None


def load(con, as_of: date | None = None) -> dict[str, Fund]:
    """Every fund stored at the newest snapshot at or before `as_of`.

    `as_of` is honoured rather than ignored because a replay of a July period
    that reads September holdings is reading the future: NVDA's weight in QQQ is
    not a constant, and a theme score computed from today's basket would credit
    a July thesis with an allocation the fund had not yet made.
    """
    ensure_schema(con)
    d = latest_as_of(con, as_of)
    if not d:
        return {}
    runs = {r[0]: r for r in con.execute(
        "SELECT symbol, coverage, rows_seen, status, note"
        " FROM etf_lookthrough_runs"
        " WHERE as_of=?", (d,))}
    ws: dict[str, dict[str, float]] = {}
    ls: dict[str, dict[str, str]] = {}
    for sym, asset, label, w in con.execute(
            "SELECT symbol, asset, label, weight FROM etf_lookthrough"
            " WHERE as_of=?", (d,)):
        ws.setdefault(sym, {})[asset] = w
        ls.setdefault(sym, {})[asset] = label or asset
    return {sym: Fund(sym, d, ws.get(sym, {}), ls.get(sym, {}),
                      r[1], r[2], r[3], r[4] or "")
            for sym, r in runs.items()}


# ------------------------------------------------------------- comparison
def overlap(a: Fund, b: Fund) -> float | None:
    """Shared weight between two funds, or None when either cannot be seen into.

    The measure is Σ min(wᵢ) over the union — the fraction of a dollar in one
    fund that buys the same securities as a dollar in the other. It is the
    honest one for this question: correlation of returns would also answer
    "are these the same bet", but it answers it with market history, so two
    funds that merely both went up look identical.
    """
    if not (a.usable and b.usable):
        return None
    return sum(min(a.weights.get(k, 0.0), b.weights.get(k, 0.0))
               for k in set(a.weights) | set(b.weights))


def collisions(funds: dict[str, Fund], labels: dict[str, str],
               *, same_label_below: float = 0.70,
               diff_label_above: float = 0.35
               ) -> tuple[list[tuple], list[tuple]]:
    """The two ways the label menu lies, as pairs with their real overlap.

    Returns (same label but far apart, different labels but close together).
    Both lists are the point: the first says the generator is choosing blind
    between rows it thinks are equal, the second says it believes it diversified
    when it did not.
    """
    syms = sorted(s for s, f in funds.items() if f.usable)
    same, diff = [], []
    for i, a in enumerate(syms):
        for b in syms[i + 1:]:
            o = overlap(funds[a], funds[b])
            if o is None:
                continue
            la, lb = labels.get(a, ""), labels.get(b, "")
            if la and la == lb and o < same_label_below:
                same.append((la, a, b, o))
            elif la != lb and o > diff_label_above:
                diff.append((a, la, b, lb, o))
    return (sorted(same, key=lambda r: r[3]),
            sorted(diff, key=lambda r: -r[4]))


# --------------------------------------------------------- reverse mapping
@dataclass(frozen=True)
class ThemeHit:
    symbol: str
    weight: float                  # share of the fund's NAV in the basket
    matched: tuple[str, ...]       # which basket names it actually holds


def resolve_theme(funds: dict[str, Fund], basket: Iterable[str],
                  *, floor: float = 0.005) -> list[ThemeHit]:
    """Rank funds by how much of their NAV sits in a theme's basket.

    This is the mapping the label menu cannot do. A thesis names companies —
    research reports do, which is where the basket comes from — and the question
    "which tradeable vehicle expresses this" then has a measured answer instead
    of a lexical one. Funds are ranked by through-weight, not by how many names
    they hit: holding nine of ten defence primes at 0.2% each is not a defence
    position.

    Opaque funds are absent rather than scored zero. A commodity fund is not
    "0% exposed to the semiconductor basket" — it is unmeasured, and letting the
    two share a number is the failure the coverage floor exists to prevent.
    """
    want = {n.strip().upper() for n in basket if n and n.strip()}
    out: list[ThemeHit] = []
    for sym, f in funds.items():
        if not f.usable:
            continue
        # A basket is written in tickers because that is how a thesis names a
        # company, while storage is keyed by ISIN. Match on the display label,
        # which is the ticker wherever the vendor supplied one.
        hits = {a: w for a, w in f.weights.items()
                if f.name(a).upper() in want and not excluded(f.name(a), a)}
        s = sum(hits.values())
        if s >= floor:
            out.append(ThemeHit(sym, s, tuple(
                f.name(a) for a in sorted(hits, key=lambda k: -hits[k]))))
    return sorted(out, key=lambda h: -h.weight)


# ------------------------------------------------- what the shelf is missing
#: Listings the reverse index returns that this desk cannot trade. The answer
#: for NVDA opens with three Canadian covered-call funds, and a shelf proposal
#: that leads with `ZWT-T.TO` is noise however correct its arithmetic. Plain
#: US tickers only; the exchange suffix is the vendor's own marker.
_FOREIGN = re.compile(r"\.[A-Z]{1,3}$|-")


def discover(basket: Iterable[str], known: Iterable[str],
             *, floor: float = 0.05, limit: int = 12
             ) -> list[ThemeHit]:
    """Funds outside the shelf that hold this theme, ranked by through-weight.

    `resolve_theme` answers "which of our 95 instruments expresses this"; when
    the answer is none, that is a fact about the shelf rather than about the
    theme, and the two are easy to confuse. A gold-miners thesis scores nothing
    here not because no vehicle exists but because the shelf carries physical
    GLD and no miners fund — a curation gap the label menu could never surface,
    because a gap has no label.

    Built from the reverse index rather than the local matrix on purpose: the
    local matrix only knows the shelf, so it can never name what is missing from
    it. The cost is one vendor call per basket name, which is why this is a
    deliberate command and not part of the weekly run.

    Returns candidates, not decisions. Adding an instrument to the universe
    means confirming liquidity, dealing and pricing through Futu — none of which
    a holdings file knows — so this ends at "worth looking at".
    """
    seen = {str(k).strip().upper() for k in known}
    want = [n.strip().upper() for n in basket if n and n.strip()]
    agg: dict[str, dict[str, float]] = {}
    for name in want:
        for row in fmp.asset_exposure(name):
            sym = str(row.get("symbol") or "").strip().upper()
            w = row.get("weightPercentage")
            if not sym or sym in seen or _FOREIGN.search(sym):
                continue
            if not isinstance(w, (int, float)) or w <= 0:
                continue
            agg.setdefault(sym, {})[name] = float(w) / 100.0
    out = [ThemeHit(sym, sum(hits.values()),
                    tuple(sorted(hits, key=lambda k: -hits[k])))
           for sym, hits in agg.items()]
    return sorted((h for h in out if h.weight >= floor),
                  key=lambda h: -h.weight)[:limit]


# ------------------------------------------------------------- portfolio
@dataclass(frozen=True)
class Exposure:
    """What a basket of funds actually owns."""
    names: dict[str, float]          # underlying -> share of covered NAV
    coverage: float                  # share of the basket we could see into
    opaque: tuple[str, ...]          # instruments with no look-through
    cash: float                      # share of covered NAV sitting in cash
    effective_names: float           # 1 / HHI over `names`

    def top(self, n: int = 10) -> list[tuple[str, float]]:
        return sorted(self.names.items(), key=lambda kv: -kv[1])[:n]


def portfolio(funds: dict[str, Fund], instruments: Sequence[str],
              weights: Sequence[float] | None = None) -> Exposure:
    """Look through an equal- or explicitly-weighted basket to its securities.

    `effective_names` is 1/HHI over the result. Ten equally weighted ETFs give
    an effective count of ten at the product layer by construction; what this
    reports is the count at the layer where risk actually lives, and the two
    differ by however much the products overlap. The gap is not a curiosity:
    `robustness_drop_top` already showed the arm advantage vanishing when twenty
    securities are removed, and this is the number that says which twenty and
    how much of the book they were, before the month is traded rather than after.
    """
    inst = list(instruments)
    ws = list(weights) if weights else [1.0 / max(1, len(inst))] * len(inst)
    if len(ws) != len(inst):
        raise ValueError("weights 与 instruments 长度不一致")

    names: dict[str, float] = {}
    cash = 0.0
    covered = 0.0
    opaque: list[str] = []
    for sym, w in zip(inst, ws):
        f = funds.get(sym)
        if f is None or not f.usable:
            opaque.append(sym)
            continue
        covered += w
        for a, aw in f.weights.items():
            nm = f.name(a)
            if excluded(nm, a):
                cash += w * aw
            else:
                names[nm] = names.get(nm, 0.0) + w * aw

    total = sum(names.values())
    norm = {k: v / total for k, v in names.items()} if total else {}
    hhi = sum(v * v for v in norm.values())
    return Exposure(
        names=norm,
        coverage=covered / (sum(ws) or 1.0),
        opaque=tuple(opaque),
        cash=cash / (covered or 1.0),
        effective_names=(1.0 / hhi) if hhi else 0.0,
    )


# ------------------------------------------- what the generator gets to see
#: Below this share in the top names, listing them says nothing. A 1,300-bond
#: credit fund's four largest positions are ~1% each; printing
#: `ANHEUSER-BUSCH 4.90% 02/01/2046 0%` is noise that also happens to be the
#: longest line in the menu. What actually distinguishes such a fund is that it
#: is flat, so that is what gets said.
FLAT_BELOW = 0.10

#: Holdings descriptions run to 40 characters for bonds and 4 for equities. The
#: menu is model input for every arm, so a line's length is a real cost paid on
#: every call; a bond's coupon and maturity do not differentiate one credit fund
#: from another and are cut.
_LABEL_CHARS = 22


def differentiator(funds: dict[str, Fund], symbol: str,
                   *, n: int = 4) -> str:
    """One line saying what this fund actually holds, for the universe menu.

    Names rather than a sector breakdown. A sector split reads as more
    information and is worse for this job: SPY and QUAL have similar splits and
    43.8% common weight, while USMV and SPLV have similar splits and 27.2%.
    Names separate what sectors blur, and they are what a thesis is written
    about.

    But names only differentiate a concentrated fund. Below `FLAT_BELOW` the
    fund's own flatness is the distinguishing fact — ITA is two positions and
    XAR is a hundred equal ones, and saying so separates them where four tickers
    at 3% would not.
    """
    f = funds.get(symbol)
    if f is None:
        return ""
    if f.status == "not_a_fund":
        return "个股，无穿透"
    if not f.usable:
        return f"不可穿透（{f.status}，认出 {f.coverage*100:.0f}%）"
    ranked = [(f.name(a), w)
              for a, w in sorted(f.weights.items(), key=lambda kv: -kv[1])
              if not excluded(f.name(a), a)]
    if not ranked:
        return "不可穿透（持仓全部是现金或衍生品）"
    top = ranked[:n]
    head = sum(w for _, w in top)
    if head < FLAT_BELOW:
        return (f"{len(ranked)} 只成分高度分散，前{len(top)}大合计仅 "
                f"{head*100:.1f}%")
    body = "、".join(f"{a[:_LABEL_CHARS]} {w*100:.0f}%" for a, w in top)
    return f"前{len(top)}大 {body}（合计 {head*100:.0f}%）"
