# IdeaGen40

**每天拉一次全球投研语料 → 打分 → 生成 40 条战术交易想法 → 用真实行情下模拟单 → 逐日盯市与归因。**

一个月之后回头看，就知道这套「每天 40 条 idea」的流程到底值不值钱。

回答的不是「策略能不能赚钱」，而是三个更基本的问题：

1. 这些 idea 的**方向**对不对？（等权收益、胜率、超额）
2. 引擎给出的**排序**有没有信息？（赔率 vs 实际收益的 Spearman ρ）
3. 它声明的**概率**诚不诚实？（三分类 Brier 与技能分）

---

## 现在的状态

| | |
|---|---|
| 起点批次 | `B20260727` — 2026-07-27 的历史 PM pack，40 条，外部撰写，未经本系统拟合 |
| 当日批次 | `B20260807` — 40 条，由 briefing pack 驱动生成，校验 0 error 0 warning |
| 语料 | 8 条 Wisburg 线，3 日窗口 1,600+ 条（Tier1 一手 / Tier2 卖方 / Tier3 策展） |
| 行情 | Futu OpenD，90 个标的、24,810 根前复权日线 |
| 产品货架 | Olive / Nexus HK，17 只货币基金日频 NAV + 1,631 只公募可检索 |
| 组合 | 两个，各 1,000 万美元 |

两个组合共用同一批 idea，差别只在执行：

- **守纪律组合** — 方法论仓位、进场区间限价、止损止盈、到期平仓，未成交的钱留现金按货币基金收益计息
- **无脑全买组合** — 每条等权、首个可成交收盘价买入、持有到期

两条线的差额就是「仓位管理」的贡献；组合与 SPY 的差额就是「选股 + 择时」的贡献。

---

## 快速开始

```bash
git clone https://github.com/YuesongCai/IdeaGen40.git && cd IdeaGen40
pip install -r requirements.txt
```

凭证放在仓库之外的 `~/.ideagen.env`（不会被提交）：

```bash
WISBURG_MCP_TOKEN=sk-...        # 智堡开发者 API Key
FUTU_HOST=127.0.0.1
FUTU_PORT=11111
```

启动 Futu OpenD 并登录，然后：

```bash
python -m ideagen.cli doctor
```

---

## 每天做什么

```bash
python -m ideagen.cli daily          # 语料 → 行情 → 打分 → briefing → 盯市 → 告警 → 报告
```

`daily` 跑完会停在生成步骤，因为那一步需要 Claude：

```
下一步：读 data/briefings/briefing_<DATE>.json，
按 prompts/idea_generation.md 生成 40 条，然后
  python -m ideagen.cli ingest-batch data/batches/batch_<DATE>.json
```

`ingest-batch` 会逐条校验（见方法论 §6）。**error 级失败即拒绝进入模拟盘**，
批次留在 `draft` 状态等修。通过则自动在两个组合下单。

Olive 只能通过 MCP 访问，所以货架快照也由 Claude 会话捕获后喂进来：

```bash
python -m ideagen.cli olive-ingest data/snapshots/olive_capture_<DATE>.json
```

这一步每天做一次，就等于在给基金类标的**积累一条真实的日频 NAV 序列**——
Olive 只给最新 NAV，不给历史，所以这是唯一的办法，也无法回溯补齐。

---

## 全部命令

```
doctor          检查 OpenD / Wisburg / 数据覆盖，不通过会明确告诉你缺什么
ingest          拉 Wisburg 8 条线，按日分页，落 documents
olive-ingest    吃一份 Olive 货架快照 → instruments + navs
prices          同步 Futu 日线（自动跳过历史额度被挡的标的）
score           算 D/A/B/N + 独立的 M（验证）与 C（拥挤）→ themes
brief           生成 briefing pack（生成器唯一合法输入，带 SHA）
seed            导入 2026-07-27 历史 pack 作为起点批次
verify-seed     审计历史 pack 的赔率底稿与 HTML 是否真的一致
ingest-batch    校验 + 落库 + 下单
mark            把两个组合推进到最后一个已收盘交易日
monitor         止损临近 / 论点失效 / 拥挤跳升 / 临近到期 告警
settle          为每条 idea 写 outcome（含未成交的反事实收益）
report          控制台归因报告
dashboard       生成 web/index.html
daily           除生成步骤外的全流程
status          紧凑 JSON 摘要
```

---

## 方法论：从 v0.3 到 v0.4

完整说明见 [`docs/methodology_v0.4.md`](docs/methodology_v0.4.md)。
原始 v0.3 文档与来源诊断在 [`docs/methodology/`](docs/methodology/)。

v0.3 的问题不是想错了，而是**很多规则在它自己的数据口径下无法被执行或无法被检验**。
14 项改动里最要紧的六项：

**1 — D 和 A 从退化的查表变回连续变量。**
v0.3 把 D 的分母定为「窗口内有效日报份数」，而核心来源是每天一份的智堡首页日报。
3 日窗口下分母恒为 3，D 只有 4 个取值；A 穷举下来只有 5 个取值，
而且 (1,0,1) 与 (1,1,1) 同分——恰好抹掉了它要衡量的「持续 vs 一次性」。
合计 40% 的权重坐在一个 20 格的表上。
v0.4 把计票池换成 8 条线的**来源条目**（3 日实测 1,105 条），
A 再混入「当日份额在该主题自身过去 20 天分布中的百分位」。

**2 — N 里权重最大的子项不再是 NA。**
「意外程度」要求 `|实际−一致预期| ÷ 两年预测误差标准差`，语料从来不给这些数。
按 §12 归一化的后果是：占总分 35% 的因子，长期在缺掉自己 40% 权重的状态下运行。
v0.4 改用**可观测代理**——主题预注册指标的最大单日涨跌幅 ÷ 自身 60 日日波动，
每天都算得出来。

**3 — 情景变得可以被证伪。**
v0.3 允许概率和回报是纯 `research_judgment`，于是**任何结局都与预测相容**。
v0.4 要求上下行幅度落在标的自身已实现波动的 `[0.35σ, 2.60σ]` 内，
超出就标 `narrow` / `wide` 并写进 outcome。一个月期的想法不能写 +30% 上涨
而标的月波动只有 4%。

**4 — 计成本。**
v0.3 一个字没提佣金、滑点。v0.4 双边计入，**在算赔率之前**从三个情景回报里扣掉，
所以一条边比自己价差还薄的 idea 不会再评成好赔率。

**5 — 新增拥挤度 C。**
v0.3 里一个主题可以 TIS 90 分，而它的表达工具正处在 1 年动量极值，模型不会反对。
C = 0.45×60日动量百分位 + 0.30×距52周高点 + 0.25×低波动溢价（高位且安静才最拥挤）。
C ≥ 80 强制「等待回踩」并把仓位减半。

**6 — 记录结果，让主张可检验。**
v0.3 只打分，从不记录结果，所以它的任何能力主张都无法被驳倒。
v0.4 为每条 idea 落一条 outcome（含未成交的反事实收益），
输出排序 Spearman ρ、Brier 校准与技能分、以及按评级/期限/主题/拥挤度/幅度校验的分档。

另外八项（机构级去重的可执行实现、来源分层防双重计分、数据驱动的 hurdle、
横截面相对评级、论点失效告警、as-of 纪律等）见方法论 §0 的对照表。

**没有改的**：TIS 四因子权重、情景赔率公式、S/A/B/C 阈值、主题分级门槛 75/60/45、
三层强制层级。保留是为了让 v0.4 的分数与历史 pack 可比。

---

## 让结果诚实的那几条规则

一个「每天下单看看」的系统很容易骗自己。这些是防线：

- **不可同 bar 回看。** 只能在批次生成时刻**之后收盘**的 bar 上成交。
  批次生成时间戳被存下来，`first_fillable` 据此推导第一个可成交交易日。
- **突破次日开盘成交。** t 日收盘确认的信号不能在同一个收盘价上执行。
- **限价成交取较差价。** 买入限价挂在区间上沿，成交价 `min(区间上沿, 开盘价)`——
  跳空穿过给跳空价，慢慢磨进去给区间边缘，永远不给当日最低价。
- **盘中不完整的 bar 直接丢弃。** OpenD 在盘中会返回一根正在形成的日线；
  `complete_through()` 按各市场收盘时间把它挡掉。
- **不可盯市的标的绝不进组合。** 没有 NAV 历史的基金会被记录、告警、
  从 P&L 剔除并单独披露，而不是按成本价盯市（那等于凭空塞进一个 0% 收益）。
- **闲置现金按真实收益计息。** 守纪律组合有大量未成交资金，
  按 Olive 美元货币基金货架中位 7 日年化计息，否则是在惩罚被检验的那个纪律本身。
- **敞口匹配基准。** 一个按设计只投了 13% 的组合，不该拿去和满仓指数比。
  报告里并列「以自身平均净敞口持有 SPY、其余现金」的混合基准。

---

## 数据源

| 来源 | 用途 | 接入方式 |
|---|---|---|
| **Wisburg 智堡** | 8 条线全量语料：市场日报、资讯流、研究文章、投行研报、资管研报、企业研究、电话会纪要、政策文献 | MCP-over-HTTP，直接说 JSON-RPC，cron 可用 |
| **Futu OpenD** | 美股 / 港股前复权日线与快照，所有成交判定与盯市 | 本地 `127.0.0.1:11111`，`futu-api` |
| **Olive / Nexus HK** | 产品货架：公募、货币基金、结构化产品；hurdle 的无风险收益来源 | MCP（仅 Agent 会话可达）→ 快照文件 → `olive-ingest` |

关于 Wisburg 接入的两个实测坑，都已在代码里处理并注释：

- 服务端不声明 charset，`requests` 会按 latin-1 解码成乱码；必须自己 `utf-8` 解；
  SSE 载荷里有裸换行，按 `data:` 行切会截断 JSON。
- `startTime` / `endTime` 传裸 `YYYY-MM-DD` 会被上游误解析，窄区间静默返回空页；
  必须传带 `+08:00` 偏移的完整 ISO-8601。

---

## 目录结构

```
ideagen/
  config.py       路径、时区、权重、成本、组合定义；凭证只从环境和 ~/.ideagen.env 读
  db.py           SQLite schema（语料 / 主题 / idea / 订单 / 持仓 / 盯市 / outcome）
  lexicon.py      冻结的 16 个主题词典 + 立场/因果深度/机构识别词表
  scoring.py      D / A / B / N + 独立的 M、C
  briefing.py     每日 briefing pack（生成器唯一合法输入）
  ideas.py        情景数学、hurdle、成本、评级、40 条发布前校验
  paper.py        订单生命周期、成交规则、止损止盈、盯市、权益曲线
  monitor.py      告警（含 v0.3 缺失的 thesis_invalidated）
  analytics.py    归因、排序 IC、Brier 校准、分档、敞口匹配基准
  report.py       生成单文件 dashboard
  universe.py     可交易宇宙（上市标的冻结在源码；Olive 货架运行时注入）
  seed.py         导入历史 pack + 审计它的赔率底稿
  sources/        wisburg.py · futu_px.py · olive.py
prompts/
  idea_generation.md   生成契约：输入字段、输出 schema、情景约束、交付前自查
docs/
  methodology_v0.4.md  完整方法论与 v0.3 对照
  methodology/         原始 v0.3 文档与来源诊断
data/                  SQLite、briefing、batch、Olive 快照（不提交）
web/index.html         dashboard（不提交，本地生成）
```

---

## Dashboard

```bash
python -m ideagen.cli dashboard && open web/index.html
```

单文件、零外部请求、深浅色自适应、表头可排序。
结论在最上面，然后是净值曲线、想法层面能力（排序 ρ 与 Brier）、分档归因、
逐条 idea、持仓明细、主题打分、告警、数据覆盖。
同时输出 `web/report.json`，页面上的每个数字都能在里面找到。

---

## 免责

模拟盘，非投资建议。所有成交均为规则化模拟，不涉及任何真实下单、资金划转或券商接口。
`futu-api` 在本项目中只用于行情，交易接口从未被调用。

---

## 每天自动跑（macOS）

用 launchd 而不是 cron，有两个实际原因：`cron` 需要 Full Disk Access；
而 launchd 完全访问不到 `~/Downloads`（TCC 保护）——**所以安装位置是 `~/IdeaGen40`**，
`~/Downloads/IdeaGen40` 只是一个软链。

```bash
cp scripts/com.ideagen40.daily.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.ideagen40.daily.plist
launchctl start com.ideagen40.daily          # 立刻跑一次
tail -f data/logs/daily.log
```

工作日 07:23 HKT 触发——美股收盘（16:15 ET）之后、港股开盘之前，
所以当天的美股日线一定是完整的。

`daily.sh` 里 python 解释器是写死绝对路径的：launchd 的 PATH 极简，
会解析到一个没有 `requests` / `futu-api` 的 `python3`。用 `IDEAGEN_PYTHON` 可覆盖。

`doctor` 只在 **OpenD 不可用**时让整个 run 退出——那种情况下盯市会算错。
Wisburg 挂了只记 warning：ingest 阶段会在 `runs` 表里记下失败，
其余阶段照常用磁盘上已有的数据跑完。一次网络抖动不应该让一整天的盯市和归因丢掉。

---

## 看板怎么打开

三种方式，按「随时能看」的程度排：

**① localhost（推荐，永远最新）**

```bash
python -m ideagen.cli serve          # http://127.0.0.1:8765
```

`/` 每次刷新都从数据库重新生成，所以打开就是当前状态，不需要先跑 `dashboard`。
另外两个端点：`/api/status`（紧凑摘要）、`/api/report`（完整归因 JSON）。

已装成常驻服务（`KeepAlive`，开机自启）：

```bash
cp scripts/com.ideagen40.serve.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.ideagen40.serve.plist
```

**② Claude Artifact（私有链接，手机也能开）**

```bash
python -m ideagen.cli dashboard --artifact --out web/artifact.html
```

再用 Artifact 工具以 `url` 参数更新到同一个链接（链接存在 `data/artifact_url.txt`）。
默认私有，需要更新时由 Claude 会话推一次。

**③ GitHub Pages（公开，默认关闭）**

```bash
scripts/publish_pages.sh
```

⚠️ **这会把完整持仓明细发布到公开可索引的 URL** ——
每一条 idea、每个成交价、每个在场仓位。
本仓库是 public，所以脚本刻意需要手动输 `yes` 确认，
并且**没有**接进每日自动流程。想公开又不想全网可见，先把 repo 转私有。

---

## 每天怎么跑

分成两半：

**自动的一半 —— 不需要你管。** launchd 工作日 07:23 HKT 跑
`ingest → prices → score → brief → mark → monitor → settle → dashboard`。
语料、行情、打分、盯市、告警、归因、看板全部自动更新。

**需要 Claude 的一半 —— 每天约 5 分钟。** 两件事必须有一个本地 Claude 会话：

1. **Olive 货架快照** —— Olive 只能走 MCP，cron 进程连不上
2. **生成当天 40 条** —— 这是你选的方案（每天 Claude 弄）

装了一个 slash command，打一下就跑完这两件事并发飞书汇报：

```
/ideagen-daily
```

定义在 `~/.claude/commands/ideagen-daily.md`，五步：Olive 快照 → `daily` →
按契约生成 40 条 → `ingest-batch` 校验下单 → 盯市报告 + 飞书 DM。

**为什么不能全自动**：云端 scheduled agent 连不上本机的 Futu OpenD 和本地数据库，
Olive MCP 在 headless 环境也不可用。所以生成这一步必须是本地会话。
漏一天的代价：当天没有新批次（已有仓位照常盯市），
Olive 基金 NAV 序列断一天且无法回补。
