"""Theme dictionary and coding lexicons.

The framework requires each theme's key question and price indicator to be
registered *before* any price data is read (§3 of 战术宏观主题评分框架 v0.3).
v0.4.1 keeps that commitment but drops a second assumption hidden inside it —
that the *set* of themes is knowable in advance. A fixed list can only score
the world its author already imagined, and measured against the real corpus the
16 seed themes below left 46% of items (2,688 of 5,836) matching nothing at all:
GLP-1 与医保准入、韩国科技股重估、人形机器人、光模块出口管制、央行购金 were all
live, well-sourced macro debates the dictionary was structurally blind to.

So the dictionary has two halves:

  * **Seed themes** — the 16 below, frozen in source, registered on
    `SEED_REGISTERED_D`, before the project's first batch.
  * **Discovered themes** — mined from the corpus by `themes.candidates()` and
    appended to `themes/registry.jsonl`, one JSON object per line, append-only.

Both halves are under version control, so any change to a theme's synonyms, key
question or registered indicator still shows up as a dated diff. What keeps the
second half honest is `registered_d`: `all_themes(as_of)` returns only themes
registered on or before `as_of`, so a theme discovered today can never score a
day it did not exist for. Without that clamp, theme discovery would be the
purest form of hindsight available — define the theme around whatever already
moved, then admire your own ranking of it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

LEXICON_VERSION = "0.4.1"

#: The seed themes count as registered before the project's first batch.
SEED_REGISTERED_D = "2026-07-26"

REGISTRY_PATH = Path(__file__).resolve().parent.parent / "themes" / "registry.jsonl"


@dataclass(frozen=True)
class Theme:
    id: str
    label: str
    key_question: str
    terms: tuple[str, ...]                  # match any (case-insensitive substring)
    price_indicator: str                    # primary M indicator, futu code
    related: tuple[str, ...] = ()           # up to 3 corroborating codes
    default_direction: str = "↑"            # direction the key question's "yes" implies
    exposures: tuple[str, ...] = ()          # exposure labels this theme can express through
    require: tuple[str, ...] = ()            # additional term that must co-occur
    registered_d: str = SEED_REGISTERED_D   # first day this theme may be scored
    origin: str = "seed"                    # "seed" | "discovered"
    provenance: tuple[str, ...] = ()        # doc_ids that justified a discovered theme


SEED_THEMES: tuple[Theme, ...] = (
    Theme(
        id="AI-CAPEX",
        label="AI资本开支与融资",
        key_question="未来1–6个月，AI订单与现金流能否覆盖资本开支与融资成本",
        terms=("AI资本开支", "算力投资", "数据中心", "AI capex", "hyperscaler", "超大规模",
               "GPU", "英伟达", "NVIDIA", "AI基础设施", "SuperPod", "算力", "AI订单",
               "推理需求", "training cluster"),
        price_indicator="US.SMH",
        related=("US.DLR", "US.VRT", "US.QQQ"),
        exposures=("全球半导体股", "数据中心REIT", "下一代科技股", "美国成长股"),
    ),
    Theme(
        id="AI-POWER",
        label="供电瓶颈与电网扩张",
        key_question="未来1–6个月，电力与电网供给约束能否转化为设备与公用事业的收入",
        terms=("电网", "输配电", "变压器", "供电", "电力需求", "grid", "transformer",
               "electrification", "电气化", "储能", "核电", "uranium", "铀", "SMR",
               "power purchase", "购电协议", "PPA", "数据中心用电"),
        price_indicator="US.XLU",
        related=("US.PAVE", "US.GRID", "US.URA"),
        exposures=("电力公用事业", "电网与工程基础设施", "智能电网与电气化",
                   "铀矿与核能股", "核电运营与燃料", "能源转型基础设施"),
    ),
    Theme(
        id="ENERGY-SUPPLY",
        label="能源供给与运输风险",
        key_question="未来1–6个月，原油与天然气供给中断能否维持实现价格与现金流",
        terms=("原油", "油价", "OPEC", "欧佩克", "布伦特", "Brent", "WTI", "天然气",
               "LNG", "航运风险", "霍尔木兹", "红海", "油轮", "炼油", "refining",
               "crude", "energy supply", "减产", "增产"),
        price_indicator="US.XLE",
        related=("US.XOP", "US.USO", "US.AMLP"),
        exposures=("能源生产商", "石油天然气勘探生产", "能源中游基础设施", "原油价格"),
    ),
    Theme(
        id="INFLATION",
        label="通胀路径与再加速风险",
        key_question="未来1–6个月，核心通胀能否维持在央行目标附近而不再加速",
        terms=("通胀", "CPI", "PCE", "核心通胀", "inflation", "通胀预期", "价格压力",
               "服务通胀", "工资增长", "breakeven", "通胀补偿", "关税成本",
               "inflation expectations", "PPI"),
        price_indicator="US.TIP",
        related=("US.STIP", "US.TLT", "US.PDBC"),
        exposures=("美国通胀保值债", "美国短久期通胀保值债", "广义商品篮子"),
    ),
    Theme(
        id="POLICY-PATH",
        label="央行政策路径与流动性",
        key_question="未来1–6个月，主要央行能否在不加息的前提下维持或放松政策",
        terms=("联储", "Fed", "FOMC", "降息", "加息", "rate cut", "rate hike", "点阵图",
               "货币政策", "QE", "QT", "缩表", "欧洲央行", "ECB", "日本央行", "BOJ",
               "政策利率", "Warsh", "议息", "central bank", "流动性"),
        price_indicator="US.IEF",
        related=("US.TLT", "US.SHY", "US.UUP"),
        exposures=("美国长久期国债", "美国中久期国债", "美国机构按揭证券"),
    ),
    Theme(
        id="TERM-PREMIUM",
        label="期限溢价与财政供给",
        key_question="未来1–6个月，长端收益率能否在财政供给压力下停止上行",
        terms=("期限溢价", "term premium", "国债供给", "财政赤字", "deficit", "发债",
               "拍卖", "auction", "债务上限", "国债收益率", "10年期", "30年期",
               "yield curve", "曲线陡峭", "fiscal", "债券供给"),
        price_indicator="US.TLT",
        related=("US.KRE", "US.MBB", "US.IEF"),
        exposures=("美国长久期国债", "区域银行", "美国机构按揭证券", "美国银行股"),
    ),
    Theme(
        id="CREDIT-STRESS",
        label="信用供给与再融资压力",
        key_question="未来1–6个月，信用利差能否在再融资高峰中不显著走阔",
        terms=("信用利差", "credit spread", "高收益", "high yield", "违约", "default",
               "再融资", "refinancing", "私募信贷", "private credit", "PIK",
               "杠杆贷款", "leveraged loan", "利息覆盖", "downgrade", "评级下调"),
        price_indicator="US.USHY",
        related=("US.LQD", "US.BKLN", "US.HYG"),
        exposures=("美元高收益债", "美元投资级信用", "美元杠杆贷款", "私募信贷二级"),
    ),
    Theme(
        id="EARNINGS-QUALITY",
        label="盈利兑现与质量分化",
        key_question="未来1–6个月，盈利上修能否扩散到指数之外并由现金流验证",
        terms=("盈利", "earnings", "业绩", "EPS", "指引", "guidance", "利润率",
               "margin", "自由现金流", "free cash flow", "ROE", "回购", "buyback",
               "分红", "股东回报", "盈利预期", "beat", "miss", "下修", "上修"),
        price_indicator="US.QUAL",
        related=("US.RSP", "US.SPY", "US.USMV"),
        exposures=("美股质量因子", "美股等权/广度", "美股低波动", "美国医疗保健"),
    ),
    Theme(
        id="JAPAN-RESET",
        label="日本政策与汇率重置",
        key_question="未来1–6个月，日元与日本利率能否在不触发干预的路径上重定价",
        terms=("日元", "yen", "美元日元", "USDJPY", "日本央行", "BOJ", "干预",
               "intervention", "YCC", "日本利率", "日本出口", "Nikkei", "日经",
               "日本股市", "carry trade", "套息"),
        price_indicator="US.EWJ",
        related=("US.DXJ", "US.FXY"),
        exposures=("日本股票", "日本出口股（对冲汇率）", "日元", "日本高确信度选股"),
    ),
    Theme(
        id="CHINA-POLICY",
        label="中国政策与内需信用",
        key_question="未来1–6个月，中国信用扩张与政策支持能否改善内需与企业现金流",
        terms=("中国经济", "社融", "信贷", "人民银行", "PBOC", "地方债", "房地产",
               "内需", "消费刺激", "中国政策", "降准", "LPR", "出海", "中国出口",
               "China stimulus", "中国股市", "港股"),
        price_indicator="US.MCHI",
        related=("US.KWEB", "HK.02800", "US.FXI"),
        exposures=("中国股票", "中国互联网平台", "香港大盘股", "中国选择性alpha",
                   "人民币国债久期"),
    ),
    Theme(
        id="EUROPE-FISCAL",
        label="欧洲财政与电气化投资",
        key_question="未来1–6个月，欧洲财政与电网投资能否从预算转为订单",
        terms=("德国财政", "欧盟", "European Commission", "欧洲财政", "基建基金",
               "国防预算", "rearmament", "再武装", "欧洲电网", "Electrification Action",
               "欧洲股市", "DAX", "STOXX", "欧洲央行财政"),
        price_indicator="US.EWG",
        related=("US.EZU", "US.EUFN", "US.ITA"),
        exposures=("德国股票", "欧元区股票", "欧洲金融股", "欧洲电网与电气化",
                   "欧洲金融与价值"),
    ),
    Theme(
        id="GEOPOLITICS",
        label="地缘冲突与国防补库",
        key_question="未来1–6个月，地缘冲突能否转化为国防订单与实际预算支出",
        terms=("地缘", "geopolitic", "冲突", "战争", "war", "制裁", "sanction",
               "国防", "defense", "defence", "军工", "补库", "关税", "tariff",
               "出口管制", "export control", "台海", "中东", "俄乌"),
        price_indicator="US.ITA",
        related=("US.PPA", "US.GLD", "US.XAR"),
        exposures=("国防军工", "黄金"),
    ),
    Theme(
        id="COMMODITY-CYCLE",
        label="金属与战略补库",
        key_question="未来1–6个月，铜与工业金属的供给约束能否推动战略补库",
        terms=("铜", "copper", "库存", "inventory", "精炼铜", "战略补库", "锂",
               "lithium", "镍", "铝", "aluminium", "稀土", "rare earth", "矿山",
               "mine supply", "商品超级周期", "农产品", "小麦", "玉米"),
        price_indicator="US.COPX",
        related=("US.CPER", "US.PDBC", "US.MOO"),
        exposures=("铜矿股", "铜价格", "广义商品篮子", "农业产业链"),
    ),
    Theme(
        id="DOLLAR-FX",
        label="美元与跨境资本流动",
        key_question="未来1–6个月，美元能否在利差收窄下停止升值",
        terms=("美元指数", "DXY", "dollar", "汇率", "exchange rate", "资本流动",
               "capital flow", "外汇储备", "de-dollarisation", "去美元化",
               "欧元汇率", "新兴市场货币", "EM currency", "利差"),
        price_indicator="US.UUP",
        related=("US.FXE", "US.EMLC", "US.EEM"),
        exposures=("美元指数", "欧元", "新兴市场本币债", "新兴市场股票", "外汇趋势策略"),
    ),
    Theme(
        id="AI-MONETISATION",
        label="AI商业化与软件现金流",
        key_question="未来1–6个月，AI相关软件与安全支出能否转化为自由现金流",
        terms=("软件", "SaaS", "云", "cloud", "网络安全", "cybersecurity", "订阅",
               "ARR", "net revenue retention", "AI应用", "copilot", "agent",
               "token成本", "推理成本", "IT预算"),
        price_indicator="US.IGV",
        related=("US.CIBR", "US.SKYY", "US.ARKW"),
        exposures=("软件股", "网络安全股", "云计算股", "下一代科技股"),
    ),
    Theme(
        id="HOUSING-RATES",
        label="利率敏感部门与住宅周期",
        key_question="未来1–6个月，按揭利率与住宅活动能否停止恶化",
        terms=("按揭", "mortgage", "房贷", "住宅", "housing", "房价", "home sales",
               "租金", "rent", "商业地产", "CRE", "REIT", "空置率", "建筑许可"),
        price_indicator="US.IYR",
        related=("US.MBB", "US.KRE"),
        exposures=("美国房地产", "美国机构按揭证券", "美国住宅建筑"),
    ),
)

#: Fields a registry line may set. Anything else is a typo, not an extension.
_REGISTRY_FIELDS = {
    "id", "label", "key_question", "terms", "price_indicator", "related",
    "default_direction", "exposures", "require", "registered_d", "origin",
    "provenance",
}


def _theme_from_row(row: dict) -> Theme:
    unknown = set(row) - _REGISTRY_FIELDS
    if unknown:
        raise ValueError(f"registry line for {row.get('id')!r} has unknown "
                         f"fields: {sorted(unknown)}")
    kw = dict(row)
    for seq in ("terms", "related", "exposures", "require", "provenance"):
        if seq in kw:
            kw[seq] = tuple(kw[seq] or ())
    kw.setdefault("origin", "discovered")
    return Theme(**kw)


def load_registry(path: Path | None = None) -> tuple[Theme, ...]:
    """Read the append-only discovered-theme registry.

    Missing file is normal — it means nothing has been discovered yet. A
    malformed or duplicate line is not: a silently-dropped theme would look
    exactly like a theme that never fired.
    """
    p = path or REGISTRY_PATH
    if not p.exists():
        return ()
    out: list[Theme] = []
    seen: set[str] = set()
    for n, raw in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{p}:{n} is not valid JSON: {exc}") from exc
        t = _theme_from_row(row)
        if t.id in seen or any(s.id == t.id for s in SEED_THEMES):
            raise ValueError(f"{p}:{n} re-registers theme id {t.id!r}; the "
                             f"registry is append-only, not editable")
        seen.add(t.id)
        out.append(t)
    return tuple(out)


#: Every theme that has ever been registered, seed or discovered. Use this for
#: *lookups* ("what is theme X"), which have no as-of dimension, and
#: `all_themes(as_of)` for *scoring*, which does.
THEMES: tuple[Theme, ...] = SEED_THEMES + load_registry()

THEME_BY_ID = {t.id: t for t in THEMES}


def all_themes(as_of: date | str | None = None) -> tuple[Theme, ...]:
    """Themes registered on or before `as_of` — the only set legal to score.

    A theme registered after `as_of` is excluded even though it exists now.
    That exclusion is the whole reason discovery is allowed at all.
    """
    if as_of is None:
        return THEMES
    d = as_of if isinstance(as_of, str) else as_of.isoformat()
    return tuple(t for t in THEMES if t.registered_d <= d)


def reload_registry() -> tuple[Theme, ...]:
    """Re-read the registry after a `theme-register`. Returns the new THEMES."""
    global THEMES, THEME_BY_ID
    THEMES = SEED_THEMES + load_registry()
    THEME_BY_ID = {t.id: t for t in THEMES}
    return THEMES


# ---------------------------------------------------------------------------
# Stance coding. Used to build the machine prior for B (关键争议). Sign is
# relative to the theme's key question: +1 = evidence the "yes" branch is
# strengthening, -1 = weakening.
# ---------------------------------------------------------------------------
POS_TERMS = (
    "上修", "超预期", "强于预期", "改善", "加速", "扩张", "回升", "上行", "增长",
    "创新高", "订单增加", "需求强劲", "beat", "upgrade", "raise", "stronger",
    "accelerat", "expansion", "robust", "resilien", "上调", "增持", "看多", "利好",
    "supportive", "improv", "outperform", "上涨动能", "补库", "回暖",
)
NEG_TERMS = (
    "下修", "低于预期", "弱于预期", "恶化", "放缓", "收缩", "回落", "下行", "萎缩",
    "创新低", "订单减少", "需求疲弱", "miss", "downgrade", "cut", "weaker",
    "slowdown", "contraction", "fragile", "下调", "减持", "看空", "利空",
    "headwind", "deteriorat", "underperform", "去库", "承压", "违约", "亏损",
)
HEDGE_TERMS = (
    "或将", "可能", "不确定", "取决于", "若", "如果", "有待", "分歧", "存在争议",
    "may ", "could ", "uncertain", "depends on", "mixed", "conditional",
    "但仍", "然而", "不过",
)

# ---------------------------------------------------------------------------
# Causal depth coding for N. Highest matching tier wins per document.
# Mirrors 框架 §8.3 but with concrete surface forms so the coding is auditable.
# ---------------------------------------------------------------------------
DEPTH_TERMS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (100, ("净利润", "归母净利", "自由现金流", "free cash flow", "EPS实际", "已落地",
           "正式实施", "生效", "签署", "通过法案", "降息决定", "加息决定", "政策落地",
           "利率决议", "final rule", "enacted", "dividend declared", "回购完成",
           "实际支出", "拨付", "disbursed")),
    (75,  ("新订单", "订单金额", "backlog", "在手订单", "营收", "revenue", "发行成本",
           "定价利率", "coupon", "融资条款", "签订协议", "purchase agreement",
           "capex guidance", "资本开支指引", "产能", "出货", "shipment", "预租",
           "pre-lease", "承购", "offtake")),
    (50,  ("收益率上行", "收益率下行", "利差走阔", "利差收窄", "价格上涨", "价格下跌",
           "汇率突破", "跌破", "涨破", "创新高", "创新低", "spread widen",
           "yield rose", "yield fell", "sold off", "rallied", "波动率上升")),
    (25,  ("表示", "认为", "预计", "展望", "讨论", "考虑", "官员称", "分析师认为",
           "said", "expects", "outlook", "considering", "signalled", "评论")),
)

FACT_TYPES = {
    "policy": ("政策", "央行", "议息", "法案", "监管", "关税", "制裁", "policy",
               "regulation", "tariff"),
    "earnings": ("财报", "业绩", "盈利", "earnings", "净利", "营收", "guidance"),
    "orders": ("订单", "backlog", "出货", "产能", "shipment", "capacity"),
    "funding": ("发行", "融资", "再融资", "issuance", "refinanc", "syndicat"),
    "data": ("数据", "指数公布", "PMI", "CPI", "PCE", "就业", "payroll", "GDP"),
    "price": ("价格", "收益率", "利差", "汇率", "price", "yield", "spread"),
}


# ---------------------------------------------------------------------------
# Institution extraction. 框架 §12 requires "同一机构、同一观点只计一次", but the
# Wisburg list payload carries no institution field, so it has to be recovered
# from the text. Where it cannot be, the item is deduped on a title signature
# instead — collapsing to the source *line* would rebuild exactly the 8×3 cell
# degeneracy v0.4 exists to remove.
# ---------------------------------------------------------------------------
INSTITUTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Nomura",        ("野村", "Nomura")),
    ("GoldmanSachs",  ("高盛", "Goldman")),
    ("MorganStanley", ("摩根士丹利", "大摩", "Morgan Stanley")),
    ("JPMorgan",      ("摩根大通", "小摩", "JPMorgan", "J.P. Morgan", "JPM")),
    ("Citi",          ("花旗", "Citi")),
    ("BofA",          ("美银", "美国银行", "Bank of America", "BofA")),
    ("Barclays",      ("巴克莱", "Barclays")),
    ("DeutscheBank",  ("德意志", "德银", "Deutsche Bank")),
    ("UBS",           ("瑞银", "UBS")),
    ("HSBC",          ("汇丰", "HSBC")),
    ("BNP",           ("法巴", "BNP Paribas")),
    ("SocGen",        ("法兴", "Societe Generale", "SocGen")),
    ("Jefferies",     ("杰富瑞", "Jefferies")),
    ("Macquarie",     ("麦格理", "Macquarie")),
    ("Mizuho",        ("瑞穗", "Mizuho")),
    ("MUFG",          ("三菱日联", "MUFG")),
    ("StanChart",     ("渣打", "Standard Chartered")),
    ("Wells",         ("富国", "Wells Fargo")),
    ("RBC",           ("加拿大皇家", "RBC")),
    ("TDSecurities",  ("道明", "TD Securities")),
    ("BlackRock",     ("贝莱德", "BlackRock")),
    ("PIMCO",         ("品浩", "PIMCO")),
    ("Vanguard",      ("先锋领航", "Vanguard")),
    ("Fidelity",      ("富达", "Fidelity")),
    ("Schroders",     ("施罗德", "Schroder")),
    ("AmundiAM",      ("东方汇理", "Amundi")),
    ("Invesco",       ("景顺", "Invesco")),
    ("AQR",           ("AQR",)),
    ("Bridgewater",   ("桥水", "Bridgewater")),
    ("CICC",          ("中金", "CICC", "China International Capital")),
    ("CITICS",        ("中信证券", "中信建投", "CITIC")),
    ("HuataiSec",     ("华泰", "Huatai")),
    ("GuotaiJunan",   ("国泰君安", "Guotai")),
    ("CMS",           ("招商证券", "招银国际", "CMB International")),
    ("IndustrialSec", ("兴业证券", "兴证")),
    ("GFSec",         ("广发证券",)),
    ("HaitongSec",    ("海通",)),
    ("ShenwanHongyuan", ("申万宏源", "申万")),
    ("Everbright",    ("光大证券",)),
    ("CSC",           ("中银国际", "中国银河", "银河证券")),
    ("Minsheng",      ("民生证券",)),
    ("TFSec",         ("天风证券",)),
    ("Fed",           ("联储", "Federal Reserve", "FOMC", "纽约联储")),
    ("ECB",           ("欧洲央行", "ECB")),
    ("BOJ",           ("日本央行", "BOJ")),
    ("PBOC",          ("人民银行", "PBOC", "央行公开市场")),
    ("IMF",           ("国际货币基金", "IMF")),
    ("BIS",           ("国际清算银行", "BIS")),
    ("OECD",          ("OECD", "经合组织")),
    ("WorldBank",     ("世界银行", "World Bank")),
)

_PUNCT = "，。、；：？！“”‘’（）《》〈〉—…·「」【】,.;:?!\"'()<>[]{}|/\\-—_ \t　"


def institution_of(text: str) -> str | None:
    """First recognised institution named in the text, or None."""
    if not text:
        return None
    head = text[:1200]
    low = head.lower()
    for canon, forms in INSTITUTIONS:
        for f in forms:
            if f.lower() in low:
                return canon
    return None


def title_signature(title: str, width: int = 22) -> str:
    """Punctuation-stripped prefix of a title, used to collapse syndication."""
    s = "".join(ch for ch in (title or "") if ch not in _PUNCT)
    return s[:width].lower()


def match_theme(text: str, theme: Theme) -> int:
    """Number of distinct dictionary terms of `theme` present in `text`."""
    if not text:
        return 0
    low = text.lower()
    hits = sum(1 for t in theme.terms if t.lower() in low)
    if theme.require and not any(r.lower() in low for r in theme.require):
        return 0
    return hits


def stance_of(text: str) -> int:
    """+1 / 0 / -1 stance of a document toward its theme's key question."""
    if not text:
        return 0
    low = text.lower()
    pos = sum(1 for t in POS_TERMS if t.lower() in low)
    neg = sum(1 for t in NEG_TERMS if t.lower() in low)
    hedge = sum(1 for t in HEDGE_TERMS if t.lower() in low)
    if pos == neg:
        return 0
    lead = abs(pos - neg)
    # A heavily hedged document needs a clearer lead to count as directional.
    if hedge >= 3 and lead < 2:
        return 0
    return 1 if pos > neg else -1


def depth_of(text: str) -> int:
    if not text:
        return 25
    low = text.lower()
    for score, terms in DEPTH_TERMS:
        if any(t.lower() in low for t in terms):
            return score
    return 25


def fact_type_of(text: str) -> str:
    low = (text or "").lower()
    for name, terms in FACT_TYPES.items():
        if any(t.lower() in low for t in terms):
            return name
    return "other"


def all_indicators() -> list[str]:
    codes = set()
    for t in THEMES:
        codes.add(t.price_indicator)
        codes.update(t.related)
    return sorted(codes)


def coverage(texts_matched: int, texts_total: int) -> float | None:
    """Share of corpus items that matched at least one registered theme.

    Reported every day so the dictionary's blind spot stays visible. A falling
    coverage number is the signal that discovery has stopped keeping up — the
    exact failure that a fixed 16-theme list hides by construction.
    """
    if not texts_total:
        return None
    return round(100.0 * texts_matched / texts_total, 1)
