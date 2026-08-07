"""Global configuration for IdeaGen40.

Everything that a daily unattended run depends on lives here. No secrets are
hard-coded except the Wisburg token, which is read from the environment first
and falls back to the value already provisioned on this machine.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------- paths
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DB_PATH = Path(os.environ.get("IDEAGEN_DB", DATA / "ideagen.db"))
SNAPSHOTS = DATA / "snapshots"
BRIEFINGS = DATA / "briefings"
BATCHES = DATA / "batches"
SEED = ROOT / "seed"
WEB = ROOT / "web"
PROMPTS = ROOT / "prompts"
LOGS = DATA / "logs"

for _p in (DATA, SNAPSHOTS, BRIEFINGS, BATCHES, WEB, LOGS):
    _p.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------- time
TZ = ZoneInfo("Asia/Hong_Kong")
MARKET_TZ = ZoneInfo("America/New_York")


def now_hkt() -> datetime:
    return datetime.now(TZ)


def today_hkt() -> date:
    return now_hkt().date()


def stamp() -> str:
    return now_hkt().strftime("%Y-%m-%d_%H%M_HKT")


# ---------------------------------------------------------------- secrets
# Credentials never live in this repo. They are read from the environment, and
# for convenience from ~/.ideagen.env (KEY=VALUE lines), which is outside the
# repo tree and therefore cannot be committed by accident.
ENV_FILE = Path(os.environ.get("IDEAGEN_ENV", Path.home() / ".ideagen.env"))


def _load_env_file(path: Path = ENV_FILE) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))


_load_env_file()


def require(key: str, hint: str = "") -> str:
    v = os.environ.get(key, "")
    if not v:
        raise RuntimeError(
            f"missing credential {key}. Put `{key}=...` in {ENV_FILE}"
            + (f" ({hint})" if hint else "")
        )
    return v


# ---------------------------------------------------------------- wisburg
WISBURG_URL = os.environ.get("WISBURG_MCP_URL", "https://mcp.wisburg.com/mcp")


def wisburg_token() -> str:
    return require("WISBURG_MCP_TOKEN", "Wisburg developer API key, sk-...")

# Source lines and their tier. Tier drives which factor a document may feed.
#   Tier 1  primary / first-hand    -> policy texts, earnings calls, company filings
#   Tier 2  named sell-side / AM    -> investment bank + asset manager research
#   Tier 3  curation / media        -> market daily, news feed, house articles
#
# D (coverage) and A (warming) count Tier 2 + Tier 3 items.
# N (latest change) causal depth only accepts Tier 1 + Tier 2.
# This partition is the fix for the double-counting flagged in the 2026-07-28
# source-diagnostic: a single fact can no longer inflate both D and N.
SOURCE_LINES: dict[str, dict] = {
    "market-daily": {"tool": "list-market-daily", "category": "ib", "tier": 3, "label": "市场日报"},
    "feed": {"tool": "list-feed", "category": "ib", "tier": 3, "label": "资讯流"},
    "articles": {"tool": "list-articles", "category": None, "tier": 3, "label": "研究文章"},
    "ib": {"tool": "list-institutional-reports", "category": "ib", "tier": 2, "label": "投行研报"},
    "am": {"tool": "list-am-reports", "category": "am", "tier": 2, "label": "资管研报"},
    "company": {"tool": "list-company-reports", "category": "company", "tier": 1, "label": "企业研究"},
    "ec": {"tool": "list-earning-calls", "category": "ec", "tier": 1, "label": "电话会纪要"},
    "archive": {"tool": "list-archive-reports", "category": "archive", "tier": 1, "label": "政策文献"},
    # The chart library. Every item carries a real, fetchable image URL plus the
    # platform's own written interpretation of it — the only externally verifiable
    # asset the corpus exposes, and the source for 框架 §11.1's requirement that a
    # report embed four original Wisburg charts.
    "images": {"tool": "list-images", "category": None, "tier": 3, "label": "数据图表"},
}
COUNTING_TIERS = (2, 3)   # feeds D and A
FACT_TIERS = (1, 2)       # feeds N causal depth

# Per-line page size for the daily pull. The feed line is by far the densest.
INGEST_LIMITS = {
    "market-daily": 20,
    "feed": 100,
    "articles": 40,
    "ib": 100,
    "am": 40,
    "company": 60,
    "ec": 40,
    "archive": 30,
    "images": 40,
}

# ---------------------------------------------------------------- futu
FUTU_HOST = os.environ.get("FUTU_HOST", "127.0.0.1")
FUTU_PORT = int(os.environ.get("FUTU_PORT", "11111"))

# Markets this OpenD subscription can actually price. CN market ETFs are not
# licensed on this account, so A-share instruments are registry-only and never
# enter a book.
PRICEABLE_MARKETS = ("US", "HK")

# ---------------------------------------------------------------- methodology
METHODOLOGY_VERSION = "0.4"

# 4+1+1 factor weights. v0.3 used D/A/B/N only; v0.4 keeps that Tactical Impact
# Score intact for comparability and adds two *independent* dimensions that
# v0.3 lacked: M (market validation, already independent in v0.3) and
# C (crowding). See docs/methodology_v0.4.md §3.
FACTOR_WEIGHTS = {"D": 0.15, "A": 0.25, "B": 0.25, "N": 0.35}

THEME_TIER_THRESHOLDS = {"core": 75.0, "important": 60.0, "watch": 45.0}
MAX_REPORT_THEMES = 6
OBSERVATION_WINDOW_DAYS = 3
BASELINE_WINDOW_DAYS = 20      # trailing baseline for the A intensity term
MIN_THEME_SOURCES = 3
MIN_VALID_ITEMS = 12           # v0.4: threshold on items, not on daily-report count

# Horizons the generator may emit.
HORIZONS = {"1个月": 1, "6个月": 6}

# Hurdle inputs, annualised. Converted to holding-period inside ideas.py.
RISK_FREE_ANNUAL = 0.0372      # 3M T-bill, refreshed by `ideagen refresh-hurdle`
LIQUIDITY_PREMIUM_ANNUAL = {   # by vehicle liquidity, annualised
    "ETF": 0.000,
    "股票": 0.000,
    "公募": 0.004,
    "私募 / UCITS": 0.012,
    "私募": 0.020,
    "结构化": 0.020,
    "现金": 0.000,
}
DEFAULT_LIQUIDITY_PREMIUM = 0.008

# ---------------------------------------------------------------- books
CAPITAL_USD = 10_000_000.0

GRADE_SIZE_MULT = {"S": 1.25, "A": 1.00, "B": 0.75, "C": 0.40}
MAX_GROSS_EXPOSURE = 1.00       # of book equity
MAX_SINGLE_POSITION = 0.05      # 5% of equity, hard cap on any one idea
MAX_THEME_EXPOSURE = 0.25       # 25% of equity per macro theme

# Order lifetime: how many sessions an unfilled entry-band order stays live.
ORDER_TTL_SESSIONS = 5

# Cost model. v0.3 had none, which is the single largest source of backtest
# inflation. Applied on both legs.
COSTS = {
    "US":  {"commission_bps": 1.0, "slippage_bps": 3.0},
    "HK":  {"commission_bps": 2.5, "slippage_bps": 5.0},
    "FUND": {"commission_bps": 0.0, "slippage_bps": 0.0, "entry_load_bps": 0.0},
}

# Asset hosts the corpus serves figures from. Anything outside this set is not
# treated as a Wisburg asset.
ASSET_HOSTS = ("rocks.wisburg.com", "doctext.wisburg.com", "img.wisburg.com")

# Chart images are hotlinked from Wisburg's CDN. The locally-served dashboard
# embeds them; the public GitHub Pages build shows title, interpretation and the
# source URL as a link instead, because republishing a subscription service's
# charts on an indexable page is a different act from viewing them locally.
EMBED_IMAGES_LOCAL = True
EMBED_IMAGES_PUBLIC = False

BENCHMARKS = {
    "SPY": "US.SPY",
    "ACWI": "US.ACWI",
    "AGG": "US.AGG",
    "60/40": None,   # synthesised from ACWI/AGG
}

# Per-day cohort books. The two commingled books answer "what would this do to
# the account"; they cannot answer "were 2026-08-05's ideas any good", because a
# single blended curve mixes every vintage together. Each batch therefore also
# gets its own independent book: equal-weight across that day's markable ideas,
# bought at the first fillable close, held to horizon. That is the cleanest read
# of one day's idea quality, and after 30 days there are 30 of them to compare.
COHORT_PREFIX = "c:"
COHORT_CAPITAL = 10_000_000.0
COHORT_SPEC = {
    "label": "当日组合", "desc": "当天 40 条等权买入、持有至期限，独立计价",
    "capital": COHORT_CAPITAL, "sizing": "equal", "entry": "market_close",
}


def cohort_book(batch_id: str) -> str:
    return f"{COHORT_PREFIX}{batch_id}"


def is_cohort(book_id: str) -> bool:
    return book_id.startswith(COHORT_PREFIX)


BOOKS = {
    "disciplined": {
        "label": "守纪律组合",
        "desc": "方法论仓位 + 进场区间限价 + 止损止盈 + 到期平仓；未触发的钱留现金",
        "capital": CAPITAL_USD,
        "sizing": "methodology",
        "entry": "band",
    },
    "naive": {
        "label": "无脑全买组合",
        "desc": "每条 idea 等权，生成日收盘全部市价买入，持有至期限结束",
        "capital": CAPITAL_USD,
        "sizing": "equal",
        "entry": "market_close",
    },
}


# ---------------------------------------------------------------- misc
@dataclass
class RunContext:
    as_of: date
    run_id: str
    stages: list[str] = field(default_factory=list)

    @property
    def window(self) -> list[date]:
        """The 3 calendar days ending on as_of (inclusive)."""
        return [self.as_of - timedelta(days=i) for i in range(OBSERVATION_WINDOW_DAYS - 1, -1, -1)]


def iso(d: date | datetime) -> str:
    return d.isoformat() if isinstance(d, date) and not isinstance(d, datetime) else d.date().isoformat()
