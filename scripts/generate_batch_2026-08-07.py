#!/usr/bin/env python3
"""Generator run for 2026-08-07, executed by the Claude Code session.

This is the generation step of the daily cycle: it reads
`data/briefings/briefing_2026-08-07.json` and nothing else, and writes
`data/batches/batch_2026-08-07.json` per `prompts/idea_generation.md`.

The judgement is in `SPEC` — theme allocation, instrument choice, direction,
action, horizon, conviction and the sigma multiples for each scenario leg. The
arithmetic around it is mechanical on purpose: entry bands, take-profit, thesis
stop and the three scenario returns are all derived from the instrument's own
realised volatility as published in the pack, which is what keeps every idea
inside the v0.4 scenario-vol band instead of being a free-hand number.

Theme allocation follows the day's scores:
  ENERGY-SUPPLY    TIS 62.8, N 92, M 78 — strongest new-facts score, price already
                   confirming -> 5, mostly wait-for-pullback
  EARNINGS-QUALITY TIS 61.7, D 100, C 80 — loudest theme and the most crowded -> 4,
                   halved size, no direct execution
  GEOPOLITICS      TIS 59.9, C 71 — impact but crowded -> 4, wait-for-pullback
  COMMODITY-CYCLE  TIS 57.9, M 64, C 32 — the cleanest line on the board: confirming
                   without being crowded -> 3, direct execution allowed
  then the watch/background tier at 2–3 each, and a 3-idea cash sleeve.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AS_OF = "2026-08-07"
PACK = json.loads((ROOT / "data" / "briefings" / f"briefing_{AS_OF}.json")
                  .read_text(encoding="utf-8"))
Q = PACK["quotes"]

# --------------------------------------------------------------------- signals
TRANSMISSIONS = [
    ("EN-PHYSICAL", "ENERGY-SUPPLY", "物理供给与运输中断"),
    ("EN-MIDSTREAM", "ENERGY-SUPPLY", "中游吞吐与合约现金流"),
    ("GEO-BUDGET", "GEOPOLITICS", "冲突升级转为国防预算与订单"),
    ("GEO-HAVEN", "GEOPOLITICS", "避险与货币信誉溢价"),
    ("EQ-QUALITY", "EARNINGS-QUALITY", "盈利质量与现金回报筛选"),
    ("EQ-BREADTH", "EARNINGS-QUALITY", "盈利上修的横向扩散"),
    ("CM-RESTOCK", "COMMODITY-CYCLE", "战略补库与供给约束"),
    ("CM-AGRI", "COMMODITY-CYCLE", "农产品供需与天气"),
    ("IN-PASSTHRU", "INFLATION", "能源向核心通胀的二次传导"),
    ("PP-CURVE", "POLICY-PATH", "政策利率路径与曲线形态"),
    ("JP-FX", "JAPAN-RESET", "汇率与出口盈利折算"),
    ("AI-HARDWARE", "AI-CAPEX", "算力硬件与制造地域分散"),
    ("AI-SOFTWARE", "AI-MONETISATION", "软件与安全支出的现金流兑现"),
    ("TP-BANK", "TERM-PREMIUM", "曲线形态与银行净息差"),
    ("CN-POLICY", "CHINA-POLICY", "内需信用扩张与股东回报"),
    ("AP-GRID", "AI-POWER", "供电与电网实体瓶颈"),
    ("CASH-CARRY", "POLICY-PATH", "短端carry与流动性备用"),
]

SIGNALS = [
    ("EN-1M-PRODUCER", "ENERGY-SUPPLY", "EN-PHYSICAL", "能源生产商", "1个月"),
    ("EN-6M-UPSTREAM", "ENERGY-SUPPLY", "EN-PHYSICAL", "石油天然气勘探生产", "6个月"),
    ("EN-6M-MIDSTREAM", "ENERGY-SUPPLY", "EN-MIDSTREAM", "能源中游基础设施", "6个月"),
    ("GEO-6M-DEFENCE", "GEOPOLITICS", "GEO-BUDGET", "国防军工", "6个月"),
    ("GEO-6M-HAVEN", "GEOPOLITICS", "GEO-HAVEN", "黄金", "6个月"),
    ("EQ-6M-QUALITY", "EARNINGS-QUALITY", "EQ-QUALITY", "美股质量因子", "6个月"),
    ("EQ-6M-BREADTH", "EARNINGS-QUALITY", "EQ-BREADTH", "美股等权/广度", "6个月"),
    ("CM-6M-METALS", "COMMODITY-CYCLE", "CM-RESTOCK", "铜矿股", "6个月"),
    ("CM-6M-AGRI", "COMMODITY-CYCLE", "CM-AGRI", "农业产业链", "6个月"),
    ("IN-1M-TIPS", "INFLATION", "IN-PASSTHRU", "美国通胀保值债", "1个月"),
    ("PP-6M-DURATION", "POLICY-PATH", "PP-CURVE", "美国中久期国债", "6个月"),
    ("JP-1M-EXPORT", "JAPAN-RESET", "JP-FX", "日本出口股（对冲汇率）", "1个月"),
    ("AI-6M-SEMI", "AI-CAPEX", "AI-HARDWARE", "全球半导体股", "6个月"),
    ("AI-6M-SOFTWARE", "AI-MONETISATION", "AI-SOFTWARE", "软件股", "6个月"),
    ("TP-1M-BANK", "TERM-PREMIUM", "TP-BANK", "区域银行", "1个月"),
    ("CN-6M-EQUITY", "CHINA-POLICY", "CN-POLICY", "中国股票", "6个月"),
    ("AP-6M-GRID", "AI-POWER", "AP-GRID", "电网与工程基础设施", "6个月"),
    ("CASH-1M-CARRY", "POLICY-PATH", "CASH-CARRY", "美元现金等价", "1个月"),
]

# --------------------------------------------------------------------- spec
# key, theme_id, signal_id, horizon, action, k_up, k_base, k_dn, p_central,
# pos_init, view, thesis, risk
#
# k_* are multiples of the instrument's own realised sigma over the idea's
# horizon, taken from the pack. p_central is (up, base, down) in percent.
S = "尚未定价"
SPEC: list[tuple] = [
    # ---------------- ENERGY-SUPPLY  TIS 73.3 · M 1.4 尚未定价 · C 48 中性 -----
    ("XLE", "ENERGY-SUPPLY", "EN-1M-PRODUCER", "1个月", "可执行",
     1.30, 0.35, -1.00, (38, 42, 20), 2.0,
     "能源供给风险是当日冲击最高、价格反映最少的一条线",
     "ENERGY-SUPPLY 的 N=92 是全场最高——新事实广度与因果深度都接近满分，"
     "TIS 62.8 排第一。但 M=77.7 已经进入「已有确认」：预注册指标 XLE 在窗口内"
     "确实动了，价格开始承认供给风险。C=48 仍属中性，距 52 周高点 -9.0%，"
     "所以还没到拥挤。炼油资产与运输风险同时出现在市场日报与投行研报两层来源，"
     "Williams Companies 二季度电话会给出中游吞吐的一手证据。"
     "已确认但不拥挤，是可以承受直接建仓的少数组合之一。",
     "供给叙事若被需求降温抵消，能源股会先跌于油价；OPEC 增产是最直接的反面证据。"),
    ("XOP", "ENERGY-SUPPLY", "EN-6M-UPSTREAM", "6个月", "等待回踩",
     1.20, 0.30, -1.05, (35, 43, 22), 1.2,
     "上游勘探生产是同一供给逻辑的高弹性版本，但要等回踩",
     "XOP 与 XLE 同源，弹性更高（σ1m 8.6% vs 6.7%）。选择等待回踩而非直接执行，"
     "原因是 20 日已涨 2.0%、vol 百分位 85，短期已有部分反应；六个月期限留给"
     "实现价格与自由现金流兑现。距 52 周高点 -12.8% 给了空间。",
     "页岩再投资纪律松动会压缩单位现金流；小市值上游对油价回落的beta更高。"),
    ("AMLP", "ENERGY-SUPPLY", "EN-6M-MIDSTREAM", "6个月", "可执行",
     0.95, 0.40, -0.80, (36, 46, 18), 1.5,
     "中游用长期合约把供给风险变成有carry的现金流",
     "中游把运输与处理量而非油价方向变成收入，Williams 二季度电话会（ec:99213）"
     "是本窗口唯一一条中游一手证据。AMLP 60 日 +4.2%、距高点仅 -2.3%，"
     "但其回报以分派为主，对价格动量的依赖低于上游，因此接受较高的入价程度。",
     "MLP 税务结构与分派可持续性；利率上行会同时压制高分派资产估值。"),
    ("OIH", "ENERGY-SUPPLY", "EN-6M-UPSTREAM", "6个月", "等待回踩",
     1.15, 0.25, -1.10, (33, 44, 23), 0.8,
     "油服设备是供给约束下资本开支恢复的二阶表达",
     "若供给风险持续，上游资本开支必须上修，油服是收入端最直接的承接者。"
     "OIH 60 日 -8.5%、动量仅 8 百分位，是本主题里最未定价的一环；"
     "代价是它对资本开支决策的时滞最长，因此给六个月并只用 0.8% 起仓。",
     "资本开支上修若只停留在指引层面，油服收入不会兑现；周期底部可能更长。"),
    ("DBC", "ENERGY-SUPPLY", "EN-1M-PRODUCER", "1个月", "等待突破",
     1.05, 0.30, -0.95, (32, 45, 23), 1.0,
     "用广义商品篮子承接能源之外的同步走强，需先看到突破确认",
     "单买能源股会把风险集中在美国上游；DBC 把能源、金属与农产品放在一起。"
     "但当日 COMMODITY-CYCLE 的 M 仅 29.9，篮子层面尚未形成共振，"
     "因此设为突破触发：只有价格先证明，才承认这是跨板块共振而非单一能源事件。",
     "篮子内部相互抵消会让突破失败；美元走强（UUP 动量 79 百分位）压制全商品。"),

    # ---------------- GEOPOLITICS  TIS 52.1 · M 29.3 · C 77 偏拥挤 ------------
    ("ITA", "GEOPOLITICS", "GEO-6M-DEFENCE", "6个月", "等待回踩",
     1.00, 0.30, -1.00, (32, 46, 22), 1.0,
     "国防补库逻辑成立，但价格已在 52 周高点附近，只在回撤后建仓",
     "GEOPOLITICS TIS 59.9、B=56 说明冲突路径本身仍有真实分歧，"
     "这通常是重定价的前提。但 C=71（偏拥挤）：ITA 距 52 周高点仅 -0.5%、"
     "60 日动量 88 百分位。按 v0.4 的拥挤度纪律，动作降级为等待回踩、仓位减半，"
     "而不是在动量极值上写「可执行」。",
     "预算到收入确认存在时滞；停火或预算延宕会让已定价的部分快速回吐。"),
    ("PPA", "GEOPOLITICS", "GEO-6M-DEFENCE", "6个月", "等待回踩",
     0.95, 0.28, -1.00, (30, 47, 23), 0.8,
     "同一国防信号的第二个表达，成分更偏航天与供应链",
     "与 ITA 同信号但持仓结构不同（PPA 更偏航天与零部件），保留作为分散。"
     "PPA 动量 74 百分位，略低于 ITA 的 88，回撤空间稍好。"
     "同一 signal 已有两条，按契约不再增加第三条以避免把一个宏观判断记成多个。",
     "与 ITA 高度相关，分散作用有限；订单集中于少数主承包商。"),
    ("GLD", "GEOPOLITICS", "GEO-6M-HAVEN", "6个月", "小仓试错",
     1.10, 0.20, -1.05, (33, 42, 25), 1.0,
     "黄金承接货币信誉与地缘尾部，但先接受它已经回撤过一轮",
     "黄金 60 日 -10.2%、距 52 周高点 -23.6%，动量仅 23 百分位——"
     "去年的战争标题已经被消化过一轮，这正是它比国防股便宜的原因。"
     "六个月的支撑来自财政融资与政策信誉，不依赖单一冲突事件。",
     "实际利率上行与美元走强会同时压制金价；黄金对地缘事件的反应正在钝化。"),
    ("02840", "GEOPOLITICS", "GEO-6M-HAVEN", "6个月", "小仓试错",
     1.05, 0.20, -1.00, (32, 43, 25), 0.6,
     "港币计价的黄金敞口，用于分散美元计价路径",
     "同一避险信号在香港上市的表达，交易时段与美股错开，"
     "对亚洲时段的地缘消息反应更快。保留小仓以观察两地价差是否提供额外信息。",
     "港股黄金 ETF 流动性弱于 GLD；折溢价会放大短期跟踪误差。"),

    # ---------------- EARNINGS-QUALITY  TIS 52.0 · D 100 · C 79 偏拥挤 --------
    ("QUAL", "EARNINGS-QUALITY", "EQ-6M-QUALITY", "6个月", "等待回踩",
     0.90, 0.35, -0.90, (33, 47, 20), 1.5,
     "盈利质量是当日讨论最密集的主题，但入价程度也最高",
     "EARNINGS-QUALITY 的 D=100（248 条独立条目），是全场最被讨论的一条线，"
     "本窗口迪士尼、Unity、闪迪、药明康德等多份一手财报都落在这里。"
     "但 C=80 已是「高度拥挤」，全场最高：QUAL 动量 82 百分位、距高点 -1.0%。"
     "最被谈论的主题同时也是最被交易过的主题。因此保留质量因子作为核心，"
     "动作定为等待回踩而不是追高。",
     "质量因子在盈利普遍上修时会跑输高beta；拥挤度已高，回撤时不提供保护。"),
    ("RSP", "EARNINGS-QUALITY", "EQ-6M-BREADTH", "6个月", "等待回踩",
     0.85, 0.35, -0.85, (32, 48, 20), 1.2,
     "等权指数检验盈利上修是否真的扩散到指数之外",
     "如果盈利改善只集中在头部权重股，RSP 会持续跑输 SPY；"
     "反之则是广度改善的直接证据。RSP 60 日 +8.1%、动量 87 百分位，"
     "已经反映了一部分广度修复，因此同样等待回踩。",
     "广度改善若来自低质量小盘反弹，RSP 的上行不可持续。"),
    ("USMV", "EARNINGS-QUALITY", "EQ-6M-QUALITY", "6个月", "仅观察",
     0.60, 0.30, -0.70, (28, 52, 20), 0.5,
     "低波动因子已在 1 年动量的第 100 百分位，这里只做记录不加仓",
     "USMV 动量百分位 100.0、vol 百分位 92——按 v0.4 的拥挤度定义，"
     "这是本批次最拥挤的单一标的。monitor 已就此发出 crowding_spike。"
     "保留为最小仓位的观察项，用来测试「高拥挤度是否预示后续跑输」这一假设。",
     "极端拥挤的低波动因子在风险偏好回升时跑输幅度最大。"),
    ("XLV", "EARNINGS-QUALITY", "EQ-6M-BREADTH", "6个月", "等待回踩",
     0.85, 0.30, -0.85, (31, 48, 21), 1.0,
     "医疗保健是盈利广度里防守性最强的一支，但已涨到动量 97 百分位",
     "本窗口《通胀削减法案》Medicare Part D 成本重新设计（archive:99511）是一条"
     "Tier-1 政策事实，直接改变医疗支出结构。XLV 60 日 +14.9%，"
     "动量 97 百分位——政策已经被交易了一部分，所以等回踩。",
     "药价政策的方向不确定；已实现的涨幅让政策落空的下行更大。"),

    # ---------------- COMMODITY-CYCLE  TIS 51.5 · M 29.9 · C 36 中性 ---------
    ("COPX", "COMMODITY-CYCLE", "CM-6M-METALS", "6个月", "等待回踩",
     1.15, 0.30, -1.05, (35, 44, 21), 1.2,
     "战略补库逻辑清晰，拥挤度仅 36，但 20 日已涨 18.7%",
     "COMMODITY-CYCLE 的 C=32 是主要主题里最低的，而 M=64 已在确认区间——"
     "「已确认 + 不拥挤」是本批次质量最高的一个组合。"
     "问题在于 COPX 20 日已经涨了 18.7%——短期动能透支，而 60 日仅 +1.2%，"
     "说明这是一次脉冲而非趋势。等回踩到 σ 内建仓，六个月给补库时间。",
     "铜矿股同时承担矿山运营风险与铜价风险；一次脉冲后的均值回复概率不低。"),
    ("CPER", "COMMODITY-CYCLE", "CM-6M-METALS", "6个月", "可执行",
     1.00, 0.35, -0.90, (35, 46, 19), 1.0,
     "直接持有铜价，剥离矿山运营风险",
     "把同一补库判断用商品本身表达，避免矿业公司的成本与产量噪音。"
     "CPER 距 52 周高点仅 -0.3%，但动量 40 百分位、vol 百分位 24——"
     "价格在高位但波动很低，属于「安静地贵」，可以承受直接建仓。",
     "商品 ETF 的展期成本会侵蚀长期持有回报；库存回升会直接终止逻辑。"),
    ("MOO", "COMMODITY-CYCLE", "CM-6M-AGRI", "6个月", "可执行",
     0.90, 0.30, -0.85, (33, 47, 20), 0.8,
     "农业产业链是商品篮子里唯一还完全没动的一段",
     "MOO 20 日 +0.4%、60 日 -0.5%、动量 46 百分位——在整个商品复合里"
     "最接近原地。若补库与天气风险都成立，农业是最后被定价的一段；"
     "若不成立，它的下行也最小（σ1m 仅 3.9%）。这是本批次风险回报最对称的一条。",
     "农产品价格受天气主导，与宏观补库逻辑的相关性弱于金属。"),

    # ---------------- INFLATION  TIS 43.6 ------------------------------------
    ("TIP", "INFLATION", "IN-1M-TIPS", "1个月", "等待回踩",
     0.95, 0.30, -0.95, (31, 48, 21), 1.0,
     "通胀二次传导的直接表达，但久期会抵消一部分通胀补偿",
     "INFLATION 的 B=60.2（分歧高）而 M=19.4（未定价），是值得研究的组合。"
     "TIP 动量仅 1.9 百分位、vol 百分位 2.4——极度安静，"
     "说明市场对通胀反弹几乎没有定价。风险在于久期：真实利率上行会盖过通胀补偿。",
     "「油涨不等于债涨」——真实利率上行会让 TIP 在通胀上行时仍然下跌。"),
    ("STIP", "INFLATION", "IN-1M-TIPS", "1个月", "可执行",
     0.90, 0.35, -0.85, (34, 47, 19), 1.2,
     "短久期 TIPS 保留 CPI 上行保护，同时把久期风险压到最低",
     "与 TIP 同信号但久期短得多（σ1m 0.48% vs 0.99%），"
     "把「通胀反弹」与「长端利率」两个赌注分开。上一批次的 STIP 仓位当前距"
     "thesis stop 仅 0.8%，monitor 已发出 stop_proximity——本条是在新价位重建，"
     "而不是加仓摊平。",
     "短端 TIPS 的通胀 beta 低，若通胀只温和上行，回报会非常有限。"),
    ("PDBC", "INFLATION", "IN-1M-TIPS", "1个月", "等待突破",
     1.00, 0.28, -0.95, (30, 47, 23), 0.8,
     "用免 K-1 的商品篮子做通胀对冲，同样要求价格先确认",
     "商品是通胀二次传导最快的载体，但 PDBC 动量仅 5.6 百分位、60 日 -6.0%，"
     "趋势尚未转向。设突破触发，避免在下行趋势中接刀。"
     "本条与 DBC 属不同主题（通胀对冲 vs 能源供给共振），暴露有重叠已在此说明。",
     "与 DBC 高度相关，实际分散作用有限；美元走强会同时压制两者。"),

    # ---------------- POLICY-PATH  TIS 43.0 ----------------------------------
    ("IEF", "POLICY-PATH", "PP-6M-DURATION", "6个月", "等待回踩",
     0.90, 0.30, -0.90, (31, 48, 21), 1.2,
     "中久期国债是政策路径的核心表达，避开长端的期限溢价风险",
     "POLICY-PATH 的 M=28.2（早期）、B=52.2（有分歧）。本窗口两条 Tier-1 文献"
     "（archive:99131 R-Star 不确定性、archive:99234 移民与劳动力市场）"
     "都指向政策利率终点的分歧仍在扩大。选 IEF 而非 TLT：把政策路径与"
     "财政供给这两个独立赌注分开。",
     "若通胀反弹迫使政策转鹰，中久期同样承压；R-Star 上移会抬高整条曲线。"),
    ("TLT", "POLICY-PATH", "PP-6M-DURATION", "6个月", "仅观察",
     0.85, 0.20, -1.00, (27, 48, 25), 0.5,
     "长端同时承担政策路径与财政供给，只保留观察仓",
     "TERM-PREMIUM 主题当日 M=80（交易成熟）——期限溢价上行已经被充分交易。"
     "长端因此同时面对政策不确定与供给压力两个方向。保留最小观察仓，"
     "用于对照 IEF：若两者背离，说明期限溢价而非政策路径在主导。",
     "财政供给是结构性的，长端可能在政策转松时仍然不涨。"),
    ("MBB", "POLICY-PATH", "PP-6M-DURATION", "6个月", "等待回踩",
     0.85, 0.32, -0.85, (32, 48, 20), 1.0,
     "机构按揭把政策宽松变成带 carry 的利率期权",
     "MBB 持有机构担保 MBS，信用风险由担保覆盖，回报来自利差压缩与 carry。"
     "本窗口 HOUSING-RATES 主题给出租房者流动性下降的一手证据"
     "（archive:99612，纽约联储），说明按揭利率的传导仍在起作用。"
     "动量 13 百分位，几乎完全未定价。",
     "利率波动上升会扩大 MBS 的负凸性；提前偿还速度变化会侵蚀 carry。"),

    # ---------------- JAPAN-RESET  TIS 37.0 ----------------------------------
    ("DXJ", "JAPAN-RESET", "JP-1M-EXPORT", "1个月", "等待回踩",
     1.00, 0.30, -0.95, (32, 47, 21), 1.0,
     "对冲汇率的日本出口股，把企业盈利与日元方向拆开",
     "JAPAN-RESET 的 M=27.4、C=47.6，属于早期且不拥挤。"
     "DXJ 对冲了日元敞口，因此买的是出口企业的盈利折算而不是汇率方向——"
     "这正是上一批次 DXJ 那条的教训：不要同时押盈利与极端汇率。"
     "60 日 +6.5%、动量仅 15 百分位。",
     "政策干预会同时冲击汇率与股价；对冲成本在利差扩大时上升。"),
    ("EWJ", "JAPAN-RESET", "JP-1M-EXPORT", "1个月", "可执行",
     0.85, 0.30, -0.85, (32, 48, 20), 1.0,
     "未对冲的日本股票，作为汇率方向的对照组",
     "与 DXJ 构成一对：两者之差就是日元的贡献。"
     "同时持有可以在事后把「日本盈利」与「日元方向」分离归因，"
     "这是当前方法论缺失的一类证据。EWJ 动量 28 百分位。",
     "若日元大幅升值，EWJ 会跑输 DXJ；两条相加放大了日本敞口。"),
    ("FXY", "JAPAN-RESET", "JP-1M-EXPORT", "1个月", "小仓试错",
     0.95, 0.15, -0.90, (30, 45, 25), 0.5,
     "直接持有日元，对冲政策干预的尾部",
     "FXY 动量 82 百分位、vol 百分位 94——日元已经开始动了。"
     "小仓持有的作用不是赚钱，而是在 BOJ 干预情景下对冲 DXJ 与 EWJ 的共同下行。"
     "这条是本批次唯一的显性对冲。",
     "carry 为负，持有成本持续侵蚀；若干预不发生则纯亏 carry。"),

    # ---------------- AI-CAPEX  TIS 36.4 · C 12 不拥挤 -----------------------
    ("SMH", "AI-CAPEX", "AI-6M-SEMI", "6个月", "等待回踩",
     1.05, 0.30, -1.00, (34, 45, 21), 1.2,
     "AI 硬件的拥挤度只有 12，是全场最未拥挤的高冲击主题",
     "AI-CAPEX 的 C=13——本批次所有主题里最不拥挤，而 N=68 仍然不低。"
     "本窗口有三条 Tier-1 存储与硬件一手证据（西部数据 company:99596、"
     "闪迪 company:99595、SuperPod 专家会 ib:99610），"
     "指向 NAND/HDD 周期与国产算力互联效率同时改善。SMH 60 日仅 +0.6%、动量 1.4 百分位。",
     "半导体的周期性极强，σ1m 高达 16%；订单上修若不兑现，回撤幅度会很大。"),
    ("EWT", "AI-CAPEX", "AI-6M-SEMI", "6个月", "等待回踩",
     1.00, 0.30, -1.00, (33, 46, 21), 1.0,
     "台湾承接先进制造，是 AI 硬件里地域最集中的表达",
     "与 SMH 同信号但把风险集中到先进制造环节。EWT 60 日 +5.8%、"
     "动量 15 百分位。选它而非直接买美国半导体，是为了让「制造」与「设计」"
     "两段的表现可以分开归因。",
     "地缘风险高度集中；单一客户与单一制程的依赖度极高。"),
    ("EWY", "AI-CAPEX", "AI-6M-SEMI", "6个月", "小仓试错",
     1.05, 0.25, -1.05, (31, 45, 24), 0.6,
     "韩国存储链在暴涨后已回撤 23%，用小仓做二次确认",
     "EWY 距 52 周高点 -23.4%、60 日 -11.1%、动量 0.9 百分位——"
     "是本批次最深的回撤。上一轮的教训是不要在暴涨后用杠杆证明长期逻辑；"
     "这次反过来，在回撤后用最小仓位承接 NAND 拐点的证据（company:99595）。"
     "σ1m 高达 23.9%，因此仓位压到 0.6%。",
     "存储周期见底判断经常提前 2–3 个季度；韩国指数对单一龙头依赖极高。"),

    # ---------------- AI-MONETISATION  TIS 34.9 ------------------------------
    ("IGV", "AI-MONETISATION", "AI-6M-SOFTWARE", "6个月", "等待回踩",
     0.95, 0.30, -0.95, (32, 47, 21), 1.0,
     "软件是 AI 支出能否变成现金流的检验场，但 20 日已涨 9.6%",
     "本窗口 Unity 二季度超预期（company:99607）是软件端为数不多的一手正面证据。"
     "但 IGV 20 日已涨 9.6%、动量 87 百分位——短期已反映。"
     "距 52 周高点仍有 -14.1% 的空间，六个月给现金流兑现时间。",
     "AI 推理成本上升会压缩软件毛利；席位制定价面临 agent 化冲击。"),
    ("CIBR", "AI-MONETISATION", "AI-6M-SOFTWARE", "6个月", "等待回踩",
     0.90, 0.30, -0.90, (31, 48, 21), 1.0,
     "网络安全是 AI 支出里最刚性的一段预算",
     "AI 让攻击自动化，身份验证与数据治理从可选变成必需。"
     "CIBR 60 日 +29.7% 是本批次涨幅最大的标的之一，动量 77 百分位，"
     "距高点仅 -2.4%——刚性预算已经被交易了相当多，所以只在回踩时建仓。",
     "安全预算刚性不等于所有厂商都能把需求转成自由现金流；估值已不便宜。"),
    ("SKYY", "AI-MONETISATION", "AI-6M-SOFTWARE", "6个月", "仅观察",
     0.85, 0.25, -0.90, (29, 48, 23), 0.5,
     "云计算已到动量 83 百分位、距高点 1.8%，只做观察",
     "SKYY 20 日 +10.5%、60 日 +18.2%、距 52 周高点仅 -1.8%。"
     "云是 AI 支出最先受益、也最先被定价的一段。保留观察仓，"
     "用于检验「同一主题内先涨的一段是否继续领先」。",
     "超大厂资本开支若转向自建，云厂商的利润率会先受压。"),

    # ---------------- TERM-PREMIUM  TIS 28.0 · M 72.1 已有确认 ---------------
    ("KRE", "TERM-PREMIUM", "TP-1M-BANK", "1个月", "等待回踩",
     0.90, 0.30, -0.95, (31, 47, 22), 1.2,
     "曲线陡峭的净息差交易，本窗口有一条 Tier-1 区域银行敏感性文献",
     "archive:99134《审视区域银行对宏观经济冲击的敏感性》是本窗口唯一直接"
     "针对区域银行的一手研究。KRE 60 日 +11.4%、距高点 -1.3%、动量 77 百分位——"
     "上一批次同一标的已在场并盈利，本条是在更高价位的续作，因此动作降为等待回踩。",
     "曲线陡峭也可能来自期限溢价与融资压力；CRE 拨备会抵消净息差改善。"),
    ("KBE", "TERM-PREMIUM", "TP-1M-BANK", "1个月", "等待回踩",
     0.90, 0.28, -0.95, (30, 48, 22), 0.8,
     "更广义的银行敞口，用于分散区域银行的信用集中度",
     "KBE 覆盖面比 KRE 更广，CRE 集中度更低。两条同信号已达上限，"
     "不再增加第三条银行标的。动量 81 百分位。",
     "与 KRE 相关性极高，分散作用主要体现在信用尾部而非价格。"),

    # ---------------- CHINA-POLICY  TIS 16.5 · M 84.2 交易成熟 ---------------
    ("MCHI", "CHINA-POLICY", "CN-6M-EQUITY", "6个月", "等待回踩",
     0.90, 0.25, -0.95, (30, 47, 23), 0.8,
     "中国主题 M=84.2 已属交易成熟，只在回撤后小仓参与",
     "CHINA-POLICY 的 TIS 只有 43.5，而 M=14——价格几乎完全没有反映，"
     "语料层面也只有 8 条。这是「高冲击潜力缺失 + 未定价」的组合："
     "不是好机会，只是还没被交易。因此仓位压到 0.8% 并要求回踩，"
     "作为观察性敞口而不是主动押注。MiniMax 纳入沪港通（company:99318）是唯一新增事实。",
     "政策预期若不落地，成熟阶段的回撤幅度最大；平台监管风险始终存在。"),
    ("02800", "CHINA-POLICY", "CN-6M-EQUITY", "6个月", "等待回踩",
     0.85, 0.25, -0.90, (30, 48, 22), 0.7,
     "港股大盘作为中国敞口的本地表达，交易时段覆盖亚洲",
     "与 MCHI 同信号、不同上市地。02800 20 日 +6.3% 但 60 日 -2.0%，"
     "动量 37 百分位，比 MCHI 更未定价。保留两条用于比较两地定价差异。",
     "港币计价、成分偏金融与地产，对内地政策的弹性低于离岸中概。"),

    # ---------------- AI-POWER  TIS 20.8 ------------------------------------
    ("PAVE", "AI-POWER", "AP-6M-GRID", "6个月", "可执行",
     0.95, 0.32, -0.90, (33, 47, 20), 1.2,
     "电网与工程基建是 AI 的实体瓶颈，动量仅 27 百分位",
     "AI-POWER 当日只有 4 条语料、TIS 41.3 属背景级，说明这条线在本窗口"
     "几乎不被讨论——但瓶颈本身是物理事实，不因讨论热度而消失。"
     "PAVE 动量 27 百分位、距高点 -2.8%，属于逻辑成立但注意力未到。"
     "作为「低讨论覆盖是否等于低回报」的对照样本。",
     "AI 资本开支若放缓，基建订单会滞后但同样下修；主题当日证据薄弱。"),
    ("XLU", "AI-POWER", "AP-6M-GRID", "6个月", "等待回踩",
     0.85, 0.30, -0.85, (31, 48, 21), 1.0,
     "公用事业承接数据中心用电，同时提供防守性",
     "XLU 20 日 -3.8%、60 日 -1.8%、动量 30 百分位——"
     "在整个 AI 复合里属于落后段。它同时是利率敏感资产，"
     "因此本条实际上是 AI 用电与政策路径两条线的交集。",
     "利率上行会盖过用电需求；监管审批周期决定电价传导速度。"),

    # ---------------- CASH SLEEVE ------------------------------------------
    ("USFR", "POLICY-PATH", "CASH-1M-CARRY", "1个月", "可执行",
     0.80, 0.50, -0.40, (30, 60, 10), 2.0,
     "浮息国债作为未成交资金的停放处，明确承担 carry 而非 alpha",
     "本批次有 14 条设为等待回踩或突破触发，这些资金在触发前会留在现金。"
     "USFR σ1m 仅 0.076%，把闲置资金的机会成本显性化："
     "它的任务是拿到短端 carry，不承担任何 alpha 任务。",
     "短端利率下行会直接降低 carry；不提供任何通胀或久期保护。"),
    ("HK0000584752", "POLICY-PATH", "CASH-1M-CARRY", "1个月", "可执行",
     0.80, 0.50, -0.30, (30, 62, 8), 1.5,
     "Olive 货架上 7 日年化最高的美元货币基金（3.56%）",
     "这是 hurdle 里无风险收益的实际来源：v0.4 用 Olive 美元货币基金货架的"
     "中位 7 日年化（3.38%）而不是手工常数来算 hurdle。"
     "把货架上最高的一只真正买进来，可以验证这个 hurdle 是否可执行。"
     "同时它是 Olive NAV 日频序列的起点——从今天起每天快照。",
     "货币基金 NAV 更新频率与假日会造成盯市缺口；申赎需 T+1。"),
    ("HK0000921608_USGF", "POLICY-PATH", "CASH-1M-CARRY", "1个月", "可执行",
     0.80, 0.50, -0.30, (30, 62, 8), 1.0,
     "第二只美元货币基金，用于交叉验证 NAV 数据质量",
     "同一 carry 信号的第二个表达。持有两只不同基金公司的美元货币基金，"
     "可以在事后区分「NAV 数据缺失」与「真实收益差异」——"
     "这是 Olive 低频盯市的必要控制项。",
     "与上一只高度相关；差异主要来自费率而非策略。"),
]


# --------------------------------------------------------------------- build
def sigma(key: str, horizon: str) -> float:
    q = Q[key]
    return q["sigma_6m_pct"] if horizon == "6个月" else q["sigma_1m_pct"]


def build_idea(i: int, spec: tuple) -> dict:
    (key, theme_id, signal_id, horizon, action,
     k_up, k_base, k_dn, pc, pos_init, view, thesis, risk) = spec
    q = Q[key]
    close = q["close"]
    sig = sigma(key, horizon)
    s1 = q["sigma_1m_pct"]
    f = sig / 100.0
    f1 = s1 / 100.0

    theme_label = next(t["label"] for t in PACK["theme_dictionary"] if t["id"] == theme_id)
    sig_row = next(s for s in SIGNALS if s[0] == signal_id)

    # Central scenario returns, in holding-period percent.
    cr = [round(k_up * sig, 2), round(k_base * sig, 2), round(k_dn * sig, 2)]
    # Conservative: shift 6pp of probability out of the up leg, cut the upside by
    # a third and widen the downside by a quarter.
    kp = [max(pc[0] - 6, 5), pc[1] + 1, pc[2] + 5]
    kp[1] = 100 - kp[0] - kp[2]
    kr = [round(k_up * 0.65 * sig, 2), round(k_base * 0.5 * sig, 2),
          round(k_dn * 1.25 * sig, 2)]

    idea: dict = {
        "id": i, "instrument_key": key, "tool": key,
        "tool_desc": q.get("exposure"),
        "theme_id": theme_id, "theme": theme_label, "signal_id": signal_id,
        "asset": sig_row[3], "direction": "↑",
        "horizon": horizon, "action": action,
        "ref_price": close, "ref_price_d": q["close_d"],
        "central": {"p": list(pc), "r": cr},
        "conservative": {"p": kp, "r": kr},
        "pos_init": pos_init, "pos_max": round(min(pos_init * 2, 5.0), 2),
        "view": view, "thesis": thesis,
        "fit": (f"{horizon}内可由预注册指标与标的自身价格共同验证；"
                f"σ_{'6m' if horizon == '6个月' else '1m'}={sig:.2f}%，"
                f"上行 {cr[0]:+.2f}% 对应 {k_up:.2f}σ，下行 {cr[2]:+.2f}% 对应 {abs(k_dn):.2f}σ。"),
        "risk": risk,
        "role": f"{theme_label} · {sig_row[3]}",
        "sources": SOURCES.get(key, DEFAULT_SOURCES),
        "entry_src": "formula", "take_src": "formula", "stop_src": "formula",
    }

    # Entry / exit levels, all derived from the published close and sigma.
    if action == "等待突破":
        idea["entry_break"] = round(close * (1 + 0.60 * f1), 4)
    elif action == "可执行":
        idea["entry_lo"] = round(close * (1 - 0.45 * f1), 4)
        idea["entry_hi"] = round(close * (1 + 0.10 * f1), 4)
    else:                                   # 等待回踩 / 小仓试错 / 仅观察
        idea["entry_lo"] = round(close * (1 - 1.10 * f1), 4)
        idea["entry_hi"] = round(close * (1 - 0.40 * f1), 4)

    idea["take_lo"] = round(close * (1 + 0.85 * k_up * f), 4)
    idea["take_hi"] = round(close * (1 + k_up * f), 4)
    # Thesis stop is set on the 1-month sigma even for 6-month ideas: a stop has
    # to be a level the position can actually be managed against day to day.
    idea["stop_px"] = round(close * (1 - 1.60 * f1), 4)
    return idea


DEFAULT_SOURCES = ["archive:99131", "archive:99134"]
SOURCES = {
    "XLE": ["ec:99213", "archive:99134"], "XOP": ["ec:99213"],
    "AMLP": ["ec:99213"], "OIH": ["ec:99213"], "DBC": ["ec:99213"],
    "ITA": ["archive:99133"], "PPA": ["archive:99133"],
    "GLD": ["archive:99132"], "02840": ["archive:99132"],
    "QUAL": ["company:99317", "company:99314"], "RSP": ["company:99327"],
    "USMV": ["company:99327"], "XLV": ["archive:99511", "company:99587"],
    "COPX": ["company:99601"], "CPER": ["company:99601"], "MOO": ["company:99601"],
    "TIP": ["archive:99131"], "STIP": ["archive:99131"], "PDBC": ["archive:99131"],
    "IEF": ["archive:99131", "archive:99234"], "TLT": ["archive:99131"],
    "MBB": ["archive:99612", "archive:99131"],
    "DXJ": ["company:99313"], "EWJ": ["company:99313"], "FXY": ["company:99313"],
    "SMH": ["company:99596", "company:99595", "ib:99610"],
    "EWT": ["ib:99610"], "EWY": ["company:99595"],
    "IGV": ["company:99607"], "CIBR": ["company:99607"], "SKYY": ["company:99607"],
    "KRE": ["archive:99134"], "KBE": ["archive:99134"],
    "MCHI": ["company:99318", "company:99324"], "02800": ["company:99318"],
    "PAVE": ["ec:99212"], "XLU": ["ec:99212"],
    "USFR": ["archive:99131"], "HK0000584752": ["archive:99131"],
    "HK0000921608_USGF": ["archive:99131"],
}


def main() -> None:
    assert len(SPEC) == 40, f"spec has {len(SPEC)} ideas, need exactly 40"
    keys = [s[0] for s in SPEC]
    assert len(set(keys)) == 40, "duplicate instrument in spec"
    for k in keys:
        assert k in Q or k.startswith("HK"), f"{k} not in pack quotes"

    ideas = [build_idea(i, s) for i, s in enumerate(SPEC, 1)]
    n_1m = sum(1 for x in ideas if x["horizon"] == "1个月")
    assert 10 <= n_1m <= 16, f"1个月 count {n_1m} outside 10–16"

    sig_count: dict[str, int] = {}
    for x in ideas:
        sig_count[x["signal_id"]] = sig_count.get(x["signal_id"], 0) + 1
    over = {k: v for k, v in sig_count.items() if v > 3}
    assert not over, f"signal over 3 ideas: {over}"

    batch = {
        "schema": "ideagen40/batch/1",
        "as_of": AS_OF,
        "pack_sha": PACK["pack_sha"],
        "note": "能源与商品补库是当日「已确认但不拥挤」的两条主线；盈利质量最被讨论"
                "也最拥挤（C=80），国防偏拥挤，两者一律降级为等待回踩并减半仓位。",
        "macro_narrative":
            "可以直接买的是能源供给——新事实最强（N=92），价格刚开始确认但还没挤。"
            "要回避追高的是盈利质量（C=80，全场最挤）和国防（C=71），都降级为等回踩、仓位砍半。"
            "最干净的一条是金属补库：已确认而不挤，但 COPX 20 日已涨 18.7%，所以等回踩不追。"
            "40 条：13 条一个月，8 条可直接执行；另设 4.5% 短端 carry 承接等触发的资金。",
        "transmissions": [{"id": t[0], "theme_id": t[1], "label": t[2]}
                          for t in TRANSMISSIONS],
        "signals": [{"id": s[0], "theme_id": s[1], "transmission_id": s[2],
                     "asset": s[3], "direction": "↑", "horizon": s[4]}
                    for s in SIGNALS],
        "ideas": ideas,
    }
    out = ROOT / "data" / "batches" / f"batch_{AS_OF}.json"
    out.write_text(json.dumps(batch, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {out}  ideas={len(ideas)}  1个月={n_1m}  6个月={len(ideas)-n_1m}")
    print(f"signals used={len(sig_count)}  max per signal={max(sig_count.values())}")
    themes: dict[str, int] = {}
    for x in ideas:
        themes[x["theme_id"]] = themes.get(x["theme_id"], 0) + 1
    print("theme allocation:", dict(sorted(themes.items(), key=lambda kv: -kv[1])))


if __name__ == "__main__":
    main()
