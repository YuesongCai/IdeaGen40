# 初心（IdeaGen40 最高准则）

> 用户 2026-08-26 要求「一定要时刻提醒自己回归初心」并原文记录。
> 这份文件是机器可读的基准：任何 PM 语义注入（`ideagen/philosophy.py`）在生效前
> 都要拿它做体检，冲突的注入不予登记。

## Overall philosophy

- 相信 AI 的语义分析，而不是再做一个 quant，ai-native strategy
- 语义分析做 momentum，tactical trade，1 个月，钉死的
- 目标：ai-native、macro driven、语义分析 led、momentum trade、perpetual portfolio、25% annual

## Process

1. 1 个月 4 周，每周三 run 一次；数据源：wisburg 日报（周一–周三前三天）+ 宏观日历；
   标的：olive mcp、iark mcp、futu opend；run 在字节云上（agentkit 沙箱），数据存字节云
2. 筛选A：run 出 5 个 macro topics，打分精简 MECE；
   筛选B：4 种出 idea 方式（ai 端到端 / carl / 2 个自设计），每 topic 20 个一个月 horizon，
   universe 限公募 + ETF + 私募（流动性好，UCITS 日度申赎）；
   筛选C：20 筛成 10，4 种方法（ai 端到端 + 3 个自设计）
3. 自动 rebalance：每周占组合 25%，第五周换第一周
4. take profit / stop loss 要有，剩余资金买 JPST

## Iteration 共识

1 个月是对的；信息源 credible/stable 够了不用多；打分A 热度为主 MECE；打分B 最 TBD 持续迭代
