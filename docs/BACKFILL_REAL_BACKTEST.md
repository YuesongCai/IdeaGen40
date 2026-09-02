# 真实回测补齐方案（Jon 诉求①②的落地路径）

状态：**待执行**（生产实例就绪后跑）。写于 2026-09-03。

## 为什么需要

Jon 的两条硬要求：①回测必须是真实数据，合成 fixture（`bt-synth-*`）不算数；
②严格卡时间，不许信息泄露。当前库里只有 **一期**（2026-08-26）真实候选池 ——
一期出不了胜率，`poc-backtest` 出的又是合成回放。

## 数据现状

- 语料 `documents`：2026-07-25 → 至今，1.1 万+ 篇（wisburg 持久化窗口）。
- 真实周产：仅 2026-08-26（candidates 42 条、verdicts 全套）。
- 价格 `prices`：全 universe 日线 2026-07-24 起（US.SPY 等 295 天）。

## 方案：模型补生成历史周

在生产实例上，对语料覆盖的历史周三逐周跑完整三段（A 打分 → B 出想法 → C 选择）：

```bash
# 每期一次，as-of 卡在当期周三；顺序无关但建议正序
ideagen run --as-of 2026-07-29
ideagen run --as-of 2026-08-05
ideagen run --as-of 2026-08-12
ideagen run --as-of 2026-08-19
# 2026-08-26 已存在（真实首期，勿覆盖）；2026-09-03 由周程序自产
```

然后对全部期做 C 阶段配对扫描（零模型、可复算）：

```python
backtest.sweep(con, periods, stage="idea_selector", allow_model=False, strict=True)
```

已内建的 as-of 钳制（补跑时自动生效，无需另写代码）：
- 语料按 `published_d` 观察窗过滤；
- 主题 `registered_d` 钳制 —— 晚注册的主题不能给早期打分；
- universe 上架日期 as-of 资格检查；
- 结果按 30 天持有期用**后来**的价格计——这是 outcome，不是泄露。

## 必须披露的局限（不许藏）

补生成的历史想法有一类**无法用代码消除**的前视风险：模型权重本身见过
2026 年 7–8 月之后的世界。文档层面卡死了输入，但 DeepSeek 权重里的
「后见之明」无法审计。所以：

1. 补跑期的 `data_classification` 记为 `backfill`（区别于 08-26 起的 `live`）；
2. 面板与任何给 Jon/PM 的展示必须标注哪些期是 backfill、哪些是 live；
3. 结论性的胜率以 live 期为准，backfill 期作为参考样本扩充，两者分开汇报；
4. 随时间推移 live 期数每周 +1，backfill 的权重自然衰减到零。

## 验收

- `backtest_runs` 出现非 `bt-synth-` 的真实 sweep 记录，TOS 不可变归档；
- 面板回测区 hit_rate（胜率）列有数，live/backfill 分开可见；
- ask-the-run 能对任一 backfill 期回答「当时读了什么、为什么选它」。
