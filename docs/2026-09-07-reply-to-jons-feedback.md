# 对 Jon 2026-09-06 十条反馈的处理回复（2026-09-07）

对应文档：[`2026-09-06-jons-feedback.md`](2026-09-06-jons-feedback.md)。本文按同样的十条编号，每条写四件事：**改了什么、改在哪、怎么验的、还没解决的**。所有「已验证」都指本机真实库（2026-07-29 → 2026-09-02 六期）上的实测，不是设计意图。测试基线从 788 条增加到 943 条，全部通过。

> 总原则不变：任何方法都有局限，这一轮只争取把已识别的误判减少、把判断依据摆到能被检查的位置。凡是 Jon 明确说「尚未达成结论」的（E 的角色与权重），这里只修已核实的实现缺陷，不替讨论下结论。

---

## 1. 主题从当期研报出发，不再被 16 个种子主题限定发现范围

**改了什么**

- 发现范围改为**全窗口研报**：`themes.candidates(scope=SCOPE_ALL)` 从窗口内全部研报里挖短语簇（旧口径只从「没命中任何旧主题」的研报里挖）。仍排除已被注册主题拥有的词项，所以旧主题不会重新冒出来。
- 每个候选簇带 `relation`：与哪些注册主题共享多少篇证据，按份额分三档（`≥60%` possible_split of X / `20–60%` adjacent / `<20%` distinct，常量 `SPLIT_SHARE`/`ADJACENT_SHARE`）。
- 命名时把重叠的邻近主题（id、关键问题、词项、共享篇数）放进模型提示，要求明确给出 `relation` 与 `rationale`（驱动是否相同、验证条件是否相同、是否依赖同一事件/现金流/风险）。注册行新增 `relation / rationale / split_from / evidence_doc_ids`。
- **归并 = 别名，不开新主题**：模型判「同一争论」并给出新叫法时，追加到 `themes/aliases.jsonl`（append-only，带 as_of），`lexicon.all_themes(as_of)` 只并入 `as_of ≤ 打分日` 的别名——更早的期回放时看不到晚加的别名，和 `registered_d` 同一条规则。
- **冻结当期定义集**：`themes.snapshot(as_of)` 产出 `theme_set_sha`，周跑在筛选 A 之前写 `A_theme_set.json`，并记进 journal 与所有 topic_scorer verdict 的 meta；面板透传到每期。
- 面板主题抽屉「这是什么」一格显示：与邻近主题的关系、归并/拆分理由原文、拆自哪个主题、后来归并进来的别名（含生效日）。
- 事后补跑中命名的主题，注册行 provenance 写明「于 YYYY-MM-DD 事后补跑中命名（模型已见过该期之后的世界）」。

**改在哪**：`ideagen/themes.py`、`ideagen/lexicon.py`、`ideagen/orchestrator.py`（发现块）、`ideagen/review.py`（weekly_block 透传）、`ideagen/cli.py`（`theme-candidates --all-docs/--unmatched-only`、`theme-alias`）、`themes/aliases.jsonl`、`web/dash.html`（`themeIdentity`）。方法与案例：[`主题形成_从当期研报出发.md`](主题形成_从当期研报出发.md)。

**怎么验的**：`tests/test_theme_formation.py` 31 条（全窗口能在已命中旧主题的研报里挖出新簇；relation 三档；别名 as-of 钳制；偷词被拒；snapshot 稳定性；假模型判 same_debate → 不注册、写别名、journal 有 merge note；判 split → 注册行含 split_from）。真实库两期对照：

| 期 | 窗口篇数 | 已命中/未命中 | 旧口径候选 | 新口径候选 |
|---|---|---|---|---|
| 2026-08-26 | 623 | 408/215 | 0 | 4 |
| 2026-09-02 | 1109 | 938/171 | 0 | 14 |

例：09-02「杰克逊霍尔会议」簇 33 篇/15 家机构，31 篇已被 POLICY-PATH 认领——旧口径永远看不见；「沃什」24 篇是 POLICY-PATH 的新叫法（词表里只有英文 Warsh）→ 该走别名；「中国房地/预售」25 篇全在 CHINA-POLICY 的研报里，但驱动（取消预售制度）与验证条件（政策落地、开发商现金流）不同 → 该拆分。

**还没解决的**：候选簇里公司名与 n-gram 碎片仍占多数，靠模型 skip；模型对归并/拆分的判定没在真实端口上验；别名只做精确冲突检查；`of` 会被词表宽的主题吃掉，所以提示给前 3 个邻居而不是 1 个。

**关于「主题变了要不要重跑历史」**：旧记录不改写（各期封存自己当时的定义集，回放看不到新定义），但要评价新的主题形成法，必须把 A→B→C 整条链在历史期上按新方法重跑——这是并行、带标签（`backfill`）的重跑，用 `weekly --supersede` 让新运行成为该期记录、旧运行改记 `weekly_superseded`（判决/日志保留，面板计为尝试）。见文末「全链重跑」。

---

## 2. G 分歧按对象归属观点，不再整篇一个正负号

**改了什么**

- 新模块 `ideagen/claims.py`：把标题/摘要/正文前 3000 字按中英标点切子句；子句归属它提到词项的主题（没提到则继承上一子句的对象）；方向只看子句内的词表。「黄金看多，白银看空」→ 黄金主题 +1、白银主题 −1。
- **方向依赖对象的词不判向**：上调/下调/cut/raise/加息/降息… 当对象是利率/收益率/央行/通胀/汇率等时标 `unresolved`，不套「降息一定利好」的规则；「盈利上调」仍 +1；显式立场词（看多/减持/利好…）优先。
- 模型路径（有推理端口时）：一篇一请求抽多个对象的观点（对象、对哪个预注册问题、方向、条件、期限、原文引用），允许弃判；缓存表 `claim_cache`（键 = 文本 sha256 + 抽取器版本 + 主题集合指纹），命中不再调用；解析失败回退机械路径并标 `clause:fallback`；token 用量记入 verdict meta。
- `topic_hgep`：G 基于归属到该主题的 claims，同机构同方向只计一次；公式不变；旧算法保留为 `G_keyword` 作对照；`g_detail`（正/负/中性/未判向计数、来源、≤8 条带原文的观点）随分数落盘并在面板显示。

**改在哪**：`ideagen/claims.py`、`ideagen/strategies/topic_hgep.py`、`ideagen/schema.py`（claim_cache）、`ideagen/strategy.py`（RunContext.claim_cache）、`web/dash.html`（`hgepFactorDetail`）。

**怎么验的**：`tests/test_claims.py`（黄金/白银；盈利上调 +1 而利率下调 unresolved，并断言旧法 `stance_of("利率下调") == -1`；条件句/hedge；同机构去重；假模型归属正确、缓存命中 0 调用、坏 JSON 回退；单对象文本上 G == G_keyword 回归）。真实库 2026-09-02：858 篇、589 篇命中主题，1.9 秒；EARNINGS-QUALITY G=79.2 vs G_keyword=27.1（两者分开可比）。

**还没解决的**：模型路径没在真实端口上实跑，token 成本只有记录机制没有实测数；模型路径默认开启且每期上限 300 篇串行调用，重跑时用 `--param claims_model=0` 关掉；摘要覆盖率（feed 84/84、ib 0/76）的问题没动。

---

## 3. E 实据：只修已核实的实现缺陷，权重与角色留给讨论

**改了什么**

- E 改按因果深度取最强三条（100 利润实现/政策落地/签约、75 订单/营收/出货、50 价格已动、25 叙事），并加**未实现/否定降级**：同一子句里出现「尚未/并未/缺乏/预计/拟/有待/pending…」时降为 25。三个示例现在是：政策尚未落地 = 25、盈利预测缺乏依据 = 25、订单已签署并完成交付 = 100（旧法分别是 100/75/25，排反了）。
- 同一事实多篇转述不重复抬高：进 top-3 前按机构 / 标题签名去重。
- `e_detail`（用了哪三条、命中词、降级原因、机构）与 `E_category_legacy`（旧类别法）一起落盘；面板显示。
- 代码注释写明：0.25 权重和「E 应如何进入筛选」未定论，两种口径都保留是为后续对照实验。

**改在哪**：`ideagen/lexicon.py`（`UNREALISED_MARKERS`、`depth_detail`）、`ideagen/strategies/topic_hgep.py`。

**怎么验的**：`tests/test_evidence_depth.py`。真实库 09-02：E 从旧法全 100 变为 41.7–100，有了区分度。

**还没解决的**：`DEPTH_TERMS` 词表本身没改（例如「自由现金流被高估」仍命中 100）；E 是否提供 H/G/P 之外的增量、是否系统性偏好成熟主题、权重是否合理——按 Jon 的意见，需要同一批主题上做对照实验，尚未做。

---

## 4. P 入价：真算、真接、真用

**改了什么**

- `_prices` 抽到 `ideagen/pricing.py`；`orchestrator.weekly` 在没有注入价格时自动按 `lexicon.all_indicators(as_of)` + `clamp_dates(as_of)` 构建（`prices = prices or {}` 那一行删除并有源码闸门测试）；journal 新增 `prices` 步（codes / measured / defaulted / clamp）。
- `return_percentile_detail` 返回样本量与截止日；每个主题记录 `p_detail`（指示标的、截止日、样本量、值、方法、来源）与 `P_measured`。默认 50 仍进公式，但「实测得到的 50」与「缺数填的 50」一路区分到面板：实测显示「实测 n=253 · 截止 2026-09-01」，缺数显示「缺数 · 默认 50，未测量」。
- `_ranking_note` 在 P 全默认时写「缺数默认值，全部未测量」而不是「有区分度」。

**怎么验的**：`tests/test_priced_in.py`。真实库 2026-09-02：18 个有效主题、57 个指示标的（含 related），clamp 2026-09-01，**57 个全部实测，0 缺数**，样本量 253。例：AI-CAPEX US.SMH → 28.1，ENERGY-SUPPLY US.XLE → 84.2，TERM-PREMIUM US.TLT → 56.1。此前全是 50 纯粹因为没接。

**还没解决的**：云端若状态库是 MySQL 且没有 K 线表，`_price_inputs` 会返回带 error 的空视图（journal 明说），P 仍全默认——生产节点没有 OpenD，价格腿要另接；收益率百分位作为 P 的代理本身的局限照 Jon 原文，不扩大。

---

## 5. 生成方法框：主读数是不同标的数，想法条数退小字

**改了什么**：流水线画布每种方法的框改为「AI 端到端 / **54 只标的** / 由 100 条想法合并 · 拒 3」；合并框「73 只候选标的 / 由 400 条想法合并 · 方法间按标的再次去重」；网格尾栏、焦点头、浮层同口径；图例写明各方法的标的数不能相加当作候选池规模。全部从数据现算（`poolCountBy('method', m)` 按 `proposed_by` 反算，想法条数取 `generators[].n`）。

**怎么验的**：`tests/test_pool_topics.py::RealPeriodNumbersAreTwoNumbers`（真实库 09-02：每方法去重标的数 < 想法数；ai_native 54 / carl_constraint 52 / chain 55 / gap 54，各 100 条想法，合并后 73 只）；`test_no_period_numbers_are_hard_coded_in_panel_strings`（剥掉注释后扫所有字符串字面量，不允许 54/73/52/100 + 只/条）。

---

## 6. 候选池抽屉：可展开、可拖宽

**改了什么**：抽屉头部加「⤢ 展开 / ⤡ 收起」（全宽 = min(1200px, 96vw)），左边缘拖拽把手（480px ～ 96vw，≥900px 自动进宽档），宽度存 localStorage，轮询重画不回弹；宽档下候选池表标的列 sticky、名称列不再省略号、表高跟视口走。期次 / 筛选 / 排序 / 面包屑 / ESC 逐层关闭不变。<1024px 沿用 100vw。

**怎么验的**：`test_drawer_can_be_widened`（按钮、把手、四个函数、`.drawer.wide` 规则）、`test_script_parses`（node --check）。已在本地面板实测。

---

## 7. 删除候选池的统一 1.5 赔率门槛提示

**改了什么**：`stagePoolBody` 那句「赔率低于 1.5 的想法不进入候选池」删除。全仓 grep 后改正三处把 1.5 写成全局门槛的文案：策略口径表里赔率两条策略的描述（现在写清：概率归一 → 扣月 0.28% 现金门槛 → 赚÷亏；宽松 = max(本批中位数, 1.5)，6～14 条；严格 = 前 40% 且 ≥1.5，6～8 条；并注明「候选池本身没有统一的赔率门槛」）、开放问题里「赚亏比下限」一条、名词表「赔率」一条；`review.py ASSUMPTIONS` 加注「仅赔率排序两条策略」。核对了 spread / calib：各自复制 `_omega` 但没有 1.5 下限，不需要改。`report.py` 的「保守赔率≥1.5→S」是 PM40 评级规则，不是候选池门槛，未动。

**怎么验的**：`test_the_pool_no_longer_claims_a_universal_odds_floor`；`test_omega_thresholds_on_the_panel_match_the_strategy_module`（从 `strat.spec()` 读 floor / n_min / n_max、从 `DEFAULT_HURDLE_M` 算 0.28%、从模块源码读 0.40，逐个对面板文字，防再次漂移）。

---

## 8. 多主题完整展示 + 表级主题筛选

**改了什么**

- 后端 `_merge_pool` 保留完整 `proposals`（每条：原候选 id、topic_id、method、thesis、上下行、三概率、horizon、vehicle/exposure、citations）与 `topic_counts`；`topic_id/topics/theses` 原样保留。`review.weekly_block` 透传 `topics / n_methods / topic_counts / proposals`。旧期次 payload 没有 `proposals` 的，从生成器裁决的 id（方法:主题:标的）还原方法×主题，标 `proposals_partial=true`，论点/赔率留空不填中位数。
- 候选池全表「主题」列列出**全部**来源主题标签（×N 为该主题下想法条数，可点即筛）；行详情按 **主题 → 生成方法 → 原始提案** 分组阅读；合并气泡、持仓抽屉里的主题也显示全部来源主题。
- 表顶主题筛选条：主题多选芯片（带标的只数）+ 附加条件 + 清除 + 计数「N / 73 只标的 · M 条想法」（去重口径与多选并集写在 ⓘ）。`poolMatch` 的 `topic`/`mt` 分支按全部来源主题与逐条提案匹配。所有既有的点击入口（点主题、点方法、点共识档）都落到同一个 `poolFilter` 状态、同一条筛选条上，没有第二套入口。

**怎么验的**：`MergePoolKeepsTopics`、`WeeklyBlockPassesProposalsThrough`、`test_pool_match_reads_every_source_topic`、`test_the_table_has_one_filter_entry_and_shows_every_topic`、`test_position_and_bubble_show_all_topics`。**GLD 核对**（真实库 09-02，run `20260904T140244Z-8a233074`）：14 条提案来自 **4 个主题 × 4 种方法**——POLICY-PATH 4、TERM-PREMIUM 4、INFLATION 3、DOLLAR-FX 3；ai_native 3（缺 DOLLAR-FX）、carl_constraint 4、chain 4、gap 3（缺 INFLATION）。主主题 TERM-PREMIUM 是与 POLICY-PATH 4:4 平票后按 id 取大的结果，和截图一致。本期 73 只里 50 只来自多个主题。

**还没解决的**：详情行展开状态在整段重画后关闭（原有行为）；窄宽下多主题标签会让行变高——这正是第 6 条「展开」要解的。

---

## 9. 历史回测明细显示全部策略

**改了什么**：`backtestCard` 删除 `.slice(0,4)`；表默认按胜率降序并写「按胜率降序 · 显示全部 N/N 个策略」（N 从数据来，折叠计数同源）；可按胜率 / 平均收益 / 名称换排序；表下「策略名单对照」按 key 对照模拟账户名单（10 + 2 − 1 = 11），缺席原因只认 `skipped_need_model` / `excluded_arms` 的记录，没有就写「原因未记录」；「来源限定·AI 端到端」与「AI 端到端选取」分列，不按名字相似合并。

**怎么验的**：`tests/test_dash_perf.py::BacktestDetailShowsEveryStrategy`（4 条）。真实库：11/11 行；缺席 AI 端到端选取（`summary.disclaimer`：未参与，需调用模型会使复算不可重复）。

---

## 10.【重点】业绩分析页：共用结构、分开数据、对齐交易口径

**改了什么**

*数据层 `ideagen/performance.py`（新）*——一份 `PerfView` 契约，两种模式各自独立构建，前端整份替换、绝不拼接：

- `paper_view(subset=live|backfill|all)`：每个仓位沿 持仓 → 想法 → 批次 → `orch_runs.data_classification` 判定按时 / 补跑。**`live` 子集按明确口径重建账户**：从资本起步只重放按时仓位的现金流，现金利息按账上记过的当天利率对重建后的余额重算（不是拿原全账户 INT 记录）；`backfill` 为补跑仓位的对称视图；`all` 为库里原样账户。重建口径写在 `disclosures`。
- 自然周 × 策略 PnL：ISO 周分桶，恒等式 `pnl = realized + unrealized_chg + cash_income + fees + flows`，每格带 `reconciled`/`residual`；不含任何「某批想法未来一个月回报」。
- 汇总表：全部策略（注册表里有而没组合的 → `未运行` + 原因）；累计收益、超额 vs SPY / 全量基准（同一起止日）、最大回撤、现金占比（期末 + 期间平均，`cash_share_basis` 说明主用哪个）、期数 / 天数、状态；`roster_diff` 对照另一模式名单，缺席原因只来自记录。
- 归因：按主题 / 标的 / 生成方法，`full_credit`（完整计入，各行不可相加）与 `split_equal`（按来源数均分，可相加）两列并给，规则原文随数据下发；多主题多方法的来源关系完整保留。
- `backtest_view`：读 `backtest_*` 表；现有 30 天持有简化回测标为 `stock-picking-study-30d`（页面写「选股能力研究」并警示不是完整账户表现），其与模拟运行规则的差异按 `outcome_for`/`tranche_curve` 代码核实后写进 `disclosures`（首个收盘无条件成交、固定 30 天、无止损、往返成本一次、¼ tranche、现金 0%）。
- **正式回测引擎 `ideagen/backtest_formal.py`（新）**：对每个已完成周跑、每个选取策略，把选中的候选建成 `BT-` 前缀批次（generated_at = 该期 07:23 HKT 钳制时间）、下到 `bt:` 前缀的回测专用组合，然后沿真实交易日逐日调用 `paper.step`——成交、止损、止盈、到期、现金计息、成本全部和模拟运行同一套代码。`paper.all_books` 显式排除回测组合；所有按期读 `ideas/batches` 的查询排除 `BT-` 批次（11 处，有测试钉住）。结果写 `backtest_runs / points / positions`，同 id 重跑幂等。CLI：`ideagen backtest-formal [--from --to --arms --backtest-id --dry-run]`。
- API：`GET /api/perf?mode=paper|backtest&subset=live|backfill|all&source=<backtest_id>`；`state.perf_index`（便宜的模式索引，实盘条目 `available:false` + 原因）。

*页面 `web/dash.html`*——新顶层 tab「业绩 · 赚了还是亏了」：页首模式切换 [模拟运行 | 历史回测 | 实盘（未接入）]，模拟运行内再分 [按时运行（默认）| 事后补跑 | 全部]；页首写清数据类型、方法学、区间、资本、样本；「口径与差异」折叠。四块主内容：净值曲线（全部策略可勾、统一起点归一化 100、回撤小图、不可用策略灰显带原因、受影响区间斜纹）、自然周 × 策略损益（金额 / 收益率 / 并排，点格展开分解与对账状态）、全部策略汇总（全部行、每列可排序、名单对照一行）、收益归因（主题 / 标的 / 方法切换，两列并给，合计行写「不可相加」）。「研究检验」与「数据与运行记录」收进折叠。证据页保留（回答「它管用吗」）并链接到业绩页（回答「赚了还是亏了」）。

**怎么验的**：`tests/test_performance.py`、`tests/test_backtest_formal.py`、`tests/test_dash_perf.py`（契约键名双方共用；`node --check`）。真实库副本（盯市至 09-04）：

- 按时子集只有 08-26 一期：spread +0.63%、buy_all +0.52、random_pick +0.46、omega_loose +0.42、omega_strict +0.41、calib +0.36、left_tail +0.36；两个「来源限定」组合在按时子集里没有仓位 → 缺数据；期末现金占比 ≈75%，同窗 SPY +5.58%。三个子集 6 周 × 10 组合全部对账。
- `all` 子集暴露账上事实：omega_strict 期末现金 −5.9%、spread −2.3%（09-04 同日五批各按同一现金定额 → 透支），视图以 disclosure 报出，未抹平。
- 正式回测（`bt-formal-20260902-…`）：6 期 × 10 组合，285 点、1,168 仓位、0 错误、0 未成交、0 模型调用。累计：random_pick +3.86%、generated_ai_native +2.35、buy_all +2.05、ai_native +1.19、spread +1.16、omega_strict +1.07、carl +1.07、omega_loose +0.99、calib +0.87、left_tail −0.02；退出：止盈 28、到期 ~240、**止损 0**。名次与选股能力研究明显不同（random_pick 一边第一、一边倒数第二）——这就是「名单一致 ≠ 业绩可比」的数字版。

**还没解决的**：正式回测只写本机 SQLite 状态库，没接云端 MySQL；同日多批次透支是 `paper.open_batch` 的问题，本层只报不修；事件退出在正式回测里永不触发（无 alerts），已披露；模拟运行的统计检验在样本够之前只给样本说明。

---

## 全链重跑：让面板显示新链路的最终结果

按 PM 的决定，六期历史（2026-07-29 → 2026-09-02）全部用新链路重跑：当期研报 → 主题形成（全窗口发现 + 模型命名 + 归并/拆分）→ 新 G/E/P 打分 → 写想法 → 选取 → 正式回测。要点：

- 语料与日历用本机冻结的库，不重新拉取；模型只用于命名主题与写想法。
- 每期标 `backfill`（模型见过该期之后的世界，这一点代码消不掉，只能标注），`--supersede` 让新运行成为该期记录，旧运行改记 `weekly_superseded`（判决/日志保留，面板计为尝试）。
- 选股能力研究改为直接读该期最新完成周跑的候选池（不再依赖建仓批次），正式回测按新判决重走。
- 命令：`python3 scripts/backfill_weeks.py --no-trade --supersede --param claims_model=0 <六个日期>`，随后 `ideagen backtest-formal` 与 `scripts/run_real_backtest.py`。

重跑结果在跑完后追加到本文末尾。

---

## 云端：查清的两处问题与本轮的修法

1. **生产节点的例行周跑曾是 POC 瘦模式**：`deploy/compose.yaml` 给 scheduler 的 `IDEAGEN_POC_WEEKLY_MODE=wisburg-auto` 对应只跑 2 种生成方法、2 个选取策略、3 个主题，且 `skip_theme_discovery=True`——不改配置，周三云端那期不会跑主题发现，也不是面板描述的那条链。**已修**：compose 新增 `IDEAGEN_WEEKLY_FULL_CHAIN` 默认 `1`，`poc_workflow.weekly_kwargs` 在开关打开时跑与本机同一条链（全部注册的生成方法与选取策略、5 个主题、每主题 20 条、主题发现开）；要回到瘦模式显式设 0。
2. **主题发现在云端读的是空表**：`themes.candidates` 读本机 `documents` 表，而云端研报在状态库的 `corpus_documents` 里、只以 `corpus` 行的形式进入运行。**已修**：发现流程改为读运行自己拿到的语料；云端没有窗口前的历史（RDS 语料只有 09-05 起三天）时，lift 闸门明确记为 `skipped: no documents before the window` 而不是静默放行或静默拒绝。
3. **镜像没拷 `themes/`**：云端只认 16 个种子主题，08-08 后发现的都看不见。**已修**：`COPY themes`；容器里注册表与别名写到 `IDEAGEN_DB` 旁的持久目录（读 seed + 持久两份并集，写持久那份），换容器不丢。
4. **本机重跑的结果怎么到云端**：状态库走 `seed_cloud_state export → TOS → 节点启动时 import`。导入是按主键「已有的赢」，而 MySQL 对「一期只能完成一次」有唯一键，所以**已改为先把种子里标为 `weekly_superseded` 的旧运行改掉，再插入新运行**，否则重跑结果会被静默丢弃。主题注册表与别名随 git 进镜像。

仍未做：云端节点没有 K 线表（无 OpenD），P 在云端仍是带标注的默认值；正式回测只写本机状态库。

