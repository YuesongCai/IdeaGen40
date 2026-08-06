"""Instrument universe: the expression vehicles an asset signal may map to.

Two populations live here.

*Listed* instruments are Futu-priceable (US + HK on this OpenD entitlement) and
are the only ones that can carry P&L: they get real daily bars, so entry bands,
stops, takes and marks are all decided against actual OHLC.

*Fund / structured* instruments come from the Olive shelf. They are real
expression vehicles and the original PM pack used them heavily, but Olive only
exposes aggregated NAV summaries rather than a daily series. They are carried in
the book at NAV cadence with an explicit `nav_stale_days` marker, and their
contribution is reported separately so a low-frequency mark can never be
mistaken for a daily one.

The registry is deliberately curated rather than scraped: the generator must
choose from a frozen, liquid, mapped set, which is what stops the "product
invents the macro signal" failure the framework's §3.1 rule 5 warns about.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from datetime import date
from typing import Iterable

from . import config, db


@dataclass(frozen=True)
class Instrument:
    key: str                # ticker (listed) or Olive id (fund)
    kind: str               # listed | fund | structured
    name: str
    exposure: str           # canonical asset-signal exposure label
    futu_code: str | None = None
    olive_key: str | None = None
    market: str = "US"
    currency: str = "USD"
    vehicle: str = "ETF"
    tags: tuple[str, ...] = ()

    @property
    def priceable(self) -> bool:
        return bool(self.futu_code) and self.market in config.PRICEABLE_MARKETS


def _L(t: str, name: str, exposure: str, *tags: str, market: str = "US",
       vehicle: str = "ETF", cur: str = "USD") -> Instrument:
    return Instrument(key=t, kind="listed", name=name, exposure=exposure,
                      futu_code=f"{market}.{t}", market=market, currency=cur,
                      vehicle=vehicle, tags=tags)


# ---------------------------------------------------------------------------
# Listed universe, grouped by the asset-signal exposure it expresses.
# Every ticker here was chosen for daily liquidity and a clean single-factor
# read; the comment on each group names the transmission it belongs to.
# ---------------------------------------------------------------------------
LISTED: list[Instrument] = [
    # ---- broad beta / benchmarks -----------------------------------------
    _L("SPY", "SPDR S&P 500 ETF", "美国大盘股", "beta", "benchmark"),
    _L("ACWI", "iShares MSCI ACWI ETF", "全球股票", "beta", "benchmark"),
    _L("RSP", "Invesco S&P 500 Equal Weight", "美股等权/广度", "breadth"),
    _L("QQQ", "Invesco QQQ Trust", "美国成长股", "beta", "growth"),
    _L("USMV", "iShares MSCI USA Min Vol", "美股低波动", "defensive", "lowvol"),
    _L("QUAL", "iShares MSCI USA Quality Factor", "美股质量因子", "quality"),
    _L("SPLV", "Invesco S&P 500 Low Volatility", "美股低波动", "defensive"),
    _L("VLUE", "iShares MSCI USA Value Factor", "美股价值因子", "value"),
    _L("MTUM", "iShares MSCI USA Momentum", "美股动量因子", "momentum"),

    # ---- rates & duration ------------------------------------------------
    _L("AGG", "iShares Core US Aggregate Bond", "美国综合债", "rates", "benchmark"),
    _L("TLT", "iShares 20+ Year Treasury", "美国长久期国债", "rates", "duration"),
    _L("IEF", "iShares 7-10 Year Treasury", "美国中久期国债", "rates"),
    _L("SHY", "iShares 1-3 Year Treasury", "美国短久期国债", "rates", "cash"),
    _L("BIL", "SPDR 1-3 Month T-Bill", "美元现金等价", "cash"),
    _L("USFR", "WisdomTree Floating Rate Treasury", "美元浮息国债", "cash"),
    _L("TIP", "iShares TIPS Bond", "美国通胀保值债", "inflation"),
    _L("STIP", "iShares 0-5 Year TIPS", "美国短久期通胀保值债", "inflation"),
    _L("MBB", "iShares MBS ETF", "美国机构按揭证券", "rates", "spread"),

    # ---- credit ----------------------------------------------------------
    _L("LQD", "iShares iBoxx Investment Grade", "美元投资级信用", "credit"),
    _L("USHY", "iShares Broad USD High Yield", "美元高收益债", "credit", "hy"),
    _L("HYG", "iShares iBoxx High Yield", "美元高收益债", "credit", "hy"),
    _L("BKLN", "Invesco Senior Loan ETF", "美元杠杆贷款", "credit", "floating"),
    _L("EMB", "iShares JPM USD EM Bond", "新兴市场美元债", "credit", "em"),
    _L("EMLC", "VanEck JPM EM Local Currency Bond", "新兴市场本币债", "em", "fx"),

    # ---- energy & commodities -------------------------------------------
    _L("XLE", "Energy Select Sector SPDR", "能源生产商", "energy"),
    _L("XOP", "SPDR S&P Oil & Gas E&P", "石油天然气勘探生产", "energy", "highbeta"),
    _L("AMLP", "Alerian MLP ETF", "能源中游基础设施", "energy", "midstream", "carry"),
    _L("OIH", "VanEck Oil Services", "油服设备", "energy"),
    _L("USO", "United States Oil Fund", "原油价格", "energy", "commodity"),
    _L("PDBC", "Invesco Optimum Yield Diversified Commodity", "广义商品篮子", "commodity"),
    _L("DBC", "Invesco DB Commodity Index", "广义商品篮子", "commodity"),
    _L("GLD", "SPDR Gold Shares", "黄金", "commodity", "haven"),
    _L("SLV", "iShares Silver Trust", "白银", "commodity"),
    _L("COPX", "Global X Copper Miners", "铜矿股", "commodity", "electrification"),
    _L("CPER", "United States Copper Index", "铜价格", "commodity"),
    _L("URA", "Global X Uranium", "铀矿与核能股", "commodity", "power"),
    _L("MOO", "VanEck Agribusiness", "农业产业链", "commodity", "agri"),
    _L("WEAT", "Teucrium Wheat", "小麦价格", "commodity", "agri"),

    # ---- AI power & infrastructure --------------------------------------
    _L("XLU", "Utilities Select Sector SPDR", "电力公用事业", "power", "ai-power"),
    _L("PAVE", "Global X US Infrastructure Development", "电网与工程基础设施", "infra", "ai-power"),
    _L("GRID", "First Trust Clean Edge Smart Grid", "智能电网与电气化", "infra", "ai-power"),
    _L("ICLN", "iShares Global Clean Energy", "清洁能源", "power", "transition"),
    _L("TAN", "Invesco Solar", "太阳能", "power", "transition"),
    _L("NLR", "VanEck Uranium+Nuclear", "核电运营与燃料", "power"),
    _L("DLR", "Digital Realty Trust", "数据中心REIT", "ai-power", "reit", vehicle="股票"),
    _L("EQIX", "Equinix Inc", "数据中心REIT", "ai-power", "reit", vehicle="股票"),
    _L("VRT", "Vertiv Holdings", "数据中心热管理设备", "ai-power", vehicle="股票"),

    # ---- AI compute & semis ---------------------------------------------
    _L("SMH", "VanEck Semiconductor", "全球半导体股", "ai-compute"),
    _L("SOXX", "iShares Semiconductor", "美国半导体股", "ai-compute"),
    _L("XSD", "SPDR S&P Semiconductor", "半导体等权", "ai-compute"),
    _L("IGV", "iShares Expanded Tech-Software", "软件股", "ai-monetisation"),
    _L("CIBR", "First Trust Nasdaq Cybersecurity", "网络安全股", "ai-monetisation"),
    _L("SKYY", "First Trust Cloud Computing", "云计算股", "ai-monetisation"),
    _L("ARKW", "ARK Next Generation Internet", "下一代科技股", "ai-monetisation", "highbeta"),

    # ---- financials ------------------------------------------------------
    _L("KRE", "SPDR S&P Regional Banking", "区域银行", "financials", "curve"),
    _L("KBE", "SPDR S&P Bank ETF", "美国银行股", "financials", "curve"),
    _L("XLF", "Financial Select Sector SPDR", "美国金融股", "financials"),
    _L("KIE", "SPDR S&P Insurance", "保险股", "financials"),

    # ---- defensives & sectors -------------------------------------------
    _L("XLV", "Health Care Select Sector SPDR", "美国医疗保健", "defensive"),
    _L("XBI", "SPDR S&P Biotech", "生物科技", "highbeta", "catalyst"),
    _L("XLP", "Consumer Staples Select Sector SPDR", "必需消费", "defensive"),
    _L("XLI", "Industrial Select Sector SPDR", "美国工业股", "cyclical"),
    _L("ITA", "iShares US Aerospace & Defense", "国防军工", "defence", "fiscal"),
    _L("PPA", "Invesco Aerospace & Defense", "国防军工", "defence", "fiscal"),
    _L("XAR", "SPDR S&P Aerospace & Defense", "国防军工", "defence"),
    _L("XHB", "SPDR S&P Homebuilders", "美国住宅建筑", "rates-sensitive"),
    _L("IYR", "iShares US Real Estate", "美国房地产", "rates-sensitive"),

    # ---- international ---------------------------------------------------
    _L("EWJ", "iShares MSCI Japan", "日本股票", "japan"),
    _L("DXJ", "WisdomTree Japan Hedged Equity", "日本出口股（对冲汇率）", "japan", "fx"),
    _L("EWG", "iShares MSCI Germany", "德国股票", "europe", "fiscal"),
    _L("EZU", "iShares MSCI Eurozone", "欧元区股票", "europe"),
    _L("FEZ", "SPDR EURO STOXX 50", "欧元区大盘股", "europe"),
    _L("EUFN", "iShares MSCI Europe Financials", "欧洲金融股", "europe", "financials"),
    _L("EWY", "iShares MSCI South Korea", "韩国股票", "asia", "ai-compute"),
    _L("EWT", "iShares MSCI Taiwan", "台湾股票", "asia", "ai-compute"),
    _L("INDA", "iShares MSCI India", "印度股票", "asia"),
    _L("MCHI", "iShares MSCI China", "中国股票", "china"),
    _L("KWEB", "KraneShares CSI China Internet", "中国互联网平台", "china", "highbeta"),
    _L("FXI", "iShares China Large-Cap", "中国大盘股", "china"),
    _L("EWH", "iShares MSCI Hong Kong", "香港股票", "china"),
    _L("EEM", "iShares MSCI Emerging Markets", "新兴市场股票", "em"),
    _L("VGK", "Vanguard FTSE Europe", "欧洲股票", "europe"),

    # ---- FX & alternatives ----------------------------------------------
    _L("UUP", "Invesco DB US Dollar Bullish", "美元指数", "fx"),
    _L("FXY", "Invesco CurrencyShares Japanese Yen", "日元", "fx"),
    _L("FXE", "Invesco CurrencyShares Euro", "欧元", "fx"),
    _L("DBMF", "iMGP DBi Managed Futures Strategy", "跨资产趋势策略", "alternative", "cta"),
    _L("KMLM", "KFA Mount Lucas Managed Futures", "跨资产趋势策略", "alternative", "cta"),
    _L("BTAL", "AGF US Market Neutral Anti-Beta", "股票市场中性", "alternative", "neutral"),
    _L("VIXY", "ProShares VIX Short-Term Futures", "股票波动率", "alternative", "vol"),

    # ---- Hong Kong listings ---------------------------------------------
    _L("02800", "Tracker Fund of Hong Kong", "香港大盘股", "china",
       market="HK", cur="HKD"),
    _L("03033", "GX Hang Seng TECH ETF", "香港科技股", "china",
       market="HK", cur="HKD"),
    _L("02840", "SPDR Gold Trust (HK)", "黄金", "commodity",
       market="HK", cur="USD"),
    _L("03199", "CSOP FactSet China Semiconductor", "中国半导体自主可控", "china", "ai-compute",
       market="HK", cur="HKD"),
    _L("09439", "GX China Electric Vehicle", "中国电动车产业链", "china",
       market="HK", cur="HKD"),
    _L("00700", "Tencent Holdings", "中国互联网平台", "china",
       market="HK", cur="HKD", vehicle="股票"),
]

LISTED_BY_KEY = {i.key: i for i in LISTED}
LISTED_BY_CODE = {i.futu_code: i for i in LISTED if i.futu_code}

# Instrument categories the generator may reference but that carry no daily
# mark. Populated from the live Olive shelf by `ideagen refresh-olive`; the
# entries below are the shelf slots the original 2026-07-27 pack used, kept so
# the seed batch reconciles.
OLIVE_SEED: list[Instrument] = [
    Instrument("L03028", "fund", "P/E FX Strategy UCITS Fund", "外汇趋势策略",
               olive_key="L03028", vehicle="私募 / UCITS", tags=("fx", "systematic")),
    Instrument("AQR-DELPHI", "fund", "AQR Delphi Long-Short Equity", "股票因子多空",
               olive_key="AQR-DELPHI", vehicle="私募", tags=("factor", "market-neutral")),
    Instrument("NB-COMMODITIES", "fund", "NB Commodities Strategy", "广义商品篮子",
               olive_key="NB-COMMODITIES", vehicle="私募", tags=("commodity",)),
    Instrument("METI", "fund", "Energy Transition Infrastructure Fund", "能源转型基础设施",
               olive_key="METI", vehicle="私募", tags=("infra", "transition")),
    Instrument("APAC-DC", "fund", "APAC Data Centre Fund", "亚太数据中心",
               olive_key="APAC-DC", vehicle="私募", tags=("ai-power", "infra")),
    Instrument("PRIVATE-CREDIT-SEC", "fund", "Private Credit Secondaries", "私募信贷二级",
               olive_key="PRIVATE-CREDIT-SEC", vehicle="私募", tags=("credit",)),
    Instrument("HELO", "fund", "Hedged Equity Overlay Fund", "对冲权益",
               olive_key="HELO", vehicle="公募", tags=("options", "defensive")),
    Instrument("JANUS-BIOTECH", "fund", "Janus Henderson Biotechnology", "生物科技",
               olive_key="JANUS-BIOTECH", vehicle="公募", tags=("catalyst",)),
    Instrument("SMD-AM-JAPAN", "fund", "SMD-AM Japan High Conviction", "日本高确信度选股",
               olive_key="SMD-AM-JAPAN", vehicle="公募", tags=("japan",)),
    Instrument("GLOBAL-SEMI", "fund", "Global Semiconductor Supply Chain Fund", "全球半导体股",
               olive_key="GLOBAL-SEMI", vehicle="公募", tags=("ai-compute",)),
    Instrument("EUROPE-INCOME", "fund", "Pan-European Income Equity", "欧洲金融与价值",
               olive_key="EUROPE-INCOME", vehicle="公募", tags=("europe",)),
    Instrument("EUROPE-GRID", "fund", "European Grid & Electrification Fund", "欧洲电网与电气化",
               olive_key="EUROPE-GRID", vehicle="公募", tags=("infra", "transition")),
    Instrument("CHINA-ALPHA", "fund", "China Selective Alpha Fund", "中国选择性alpha",
               olive_key="CHINA-ALPHA", vehicle="公募", tags=("china",)),
    Instrument("CNY-DURATION", "fund", "CNY Government Bond Duration", "人民币国债久期",
               olive_key="CNY-DURATION", vehicle="公募", currency="CNY", tags=("rates",)),
    Instrument("MARKET-NEUTRAL", "fund", "Equity Market Neutral Fund", "股票市场中性",
               olive_key="MARKET-NEUTRAL", vehicle="私募", tags=("neutral",)),
    Instrument("CASH-PLUS", "fund", "Cash Plus / Money Market", "美元现金等价",
               olive_key="CASH-PLUS", vehicle="现金", tags=("cash",)),
]

ALL: list[Instrument] = LISTED + OLIVE_SEED
BY_KEY = {i.key: i for i in ALL}


def hydrate(con) -> int:
    """Fold Olive-ingested instruments into the in-process registry.

    The listed universe is frozen in source, but the Olive shelf is discovered at
    runtime by the agent session. Without this, an idea naming a real Olive
    productCode would fail to resolve and silently degrade to `monitor`.
    """
    added = 0
    for r in db.q(con, "SELECT key,kind,name,currency,meta FROM instruments "
                       "WHERE market='OLIVE'"):
        if r["key"] in BY_KEY:
            continue
        meta = db.jl(r["meta"], {}) or {}
        inst = Instrument(
            key=r["key"], kind=r["kind"] or "fund", name=r["name"] or r["key"],
            exposure=meta.get("exposure") or _exposure_for(meta),
            olive_key=r["key"], market="OLIVE", currency=r["currency"] or "USD",
            vehicle=_vehicle_for(meta), tags=tuple(meta.get("tags") or ("olive",)))
        BY_KEY[inst.key] = inst
        ALL.append(inst)
        added += 1
    return added


def _exposure_for(meta: dict) -> str:
    g = meta.get("group")
    if g == "cash" or meta.get("asset_class") == "MM":
        return "美元现金等价"
    return {"funds": "公募基金", "structured": "结构化产品",
            "private": "私募基金"}.get(g or "", "未映射")


def _vehicle_for(meta: dict) -> str:
    g = meta.get("group")
    if g == "cash" or meta.get("asset_class") == "MM":
        return "现金"
    return {"funds": "公募", "private": "私募", "structured": "结构化"}.get(g or "", "公募")


# ---------------------------------------------------------------- persistence
def sync_registry(con) -> int:
    now = config.now_hkt().isoformat()
    rows = [{
        "key": i.key, "kind": i.kind, "futu_code": i.futu_code, "olive_key": i.olive_key,
        "name": i.name, "market": i.market, "currency": i.currency,
        "priceable": int(i.priceable),
        "meta": {"exposure": i.exposure, "vehicle": i.vehicle, "tags": list(i.tags)},
        "updated_at": now,
    } for i in ALL]
    return db.upsert_many(con, "instruments", rows, ["key"])


def resolve(token: str, register_unknown_as: str | None = None) -> Instrument | None:
    """Map whatever the generator emitted onto a registry entry.

    `register_unknown_as` lets a caller (the seed importer) mint an off-shelf
    entry for a real product that is not in the frozen registry — a named Olive
    fund, say. It becomes a `fund`-kind instrument with no price feed, so the book
    records it and discloses it as unmarkable rather than pretending it does not
    exist or, worse, silently dropping it from the batch.
    """
    if not token:
        return None
    t = token.strip().upper()
    if t in BY_KEY:
        return BY_KEY[t]
    if t in LISTED_BY_CODE:
        return LISTED_BY_CODE[t]
    if "." in t and t.split(".", 1)[1] in LISTED_BY_KEY:
        return LISTED_BY_KEY[t.split(".", 1)[1]]
    for i in ALL:                                   # name match
        if t == i.name.upper():
            return i
    if register_unknown_as:
        slug = re.sub(r"[^A-Z0-9]+", "-", t).strip("-")[:40] or "UNKNOWN"
        inst = Instrument(key=slug, kind=register_unknown_as, name=token.strip(),
                          exposure="未映射", olive_key=slug, market="OLIVE",
                          vehicle="私募/公募（待确认）", tags=("off-registry",))
        BY_KEY.setdefault(slug, inst)
        ALL.append(inst)
        return inst
    return None


def priceable_codes(extra: Iterable[str] = ()) -> list[str]:
    """Every code the price layer should keep warm: universe + benchmarks."""
    codes = {i.futu_code for i in LISTED if i.priceable}
    codes.update(c for c in config.BENCHMARKS.values() if c)
    codes.update(c for c in extra if c)
    return sorted(codes)


def tradeable(con, kind: str | None = None) -> list[Instrument]:
    """Registry entries the book can actually mark today.

    Excludes anything the OpenD historical-K-line quota has locked out, so the
    generator never proposes an instrument whose P&L cannot be computed.
    """
    from .sources import futu_px

    blocked = futu_px.quota_blocked(con)
    out = []
    for i in ALL:
        if kind and i.kind != kind:
            continue
        if i.kind == "listed" and (not i.priceable or i.futu_code in blocked):
            continue
        out.append(i)
    return out


def catalogue(con=None, kind: str | None = None) -> list[dict]:
    """Compact form handed to the generator inside the daily briefing."""
    blocked: set[str] = set()
    have: dict[str, str] = {}
    if con is not None:
        from .sources import futu_px

        blocked = futu_px.quota_blocked(con)
        have = {r["code"]: r["mx"] for r in db.q(
            con, "SELECT code, MAX(d) mx, COUNT(*) n FROM prices GROUP BY code HAVING n>=60")}
    out = []
    for i in ALL:
        if kind and i.kind != kind:
            continue
        markable = (i.kind != "listed") or (
            i.priceable and i.futu_code not in blocked
            and (not have or i.futu_code in have)
        )
        out.append({"key": i.key, "kind": i.kind, "name": i.name,
                    "exposure": i.exposure, "vehicle": i.vehicle,
                    "market": i.market, "ccy": i.currency,
                    "markable": markable,
                    "px_through": have.get(i.futu_code or ""),
                    "tags": list(i.tags)})
    return out


def exposures() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for i in ALL:
        out.setdefault(i.exposure, []).append(i.key)
    return dict(sorted(out.items()))
