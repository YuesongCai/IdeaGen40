# 一次性分析：回测设计的依据

`docs/回测设计_如何回测才能服务于25%目标.md` 里的每个数字都出自这里。
全部只读 `data/ideagen.db` 的 `prices` / `candidates`，不写库。

| 脚本 | 回答什么 | 模型调用 |
|---|---|---|
| `envelope.py` | 这个货架 + 这个结构里最多有多少收益（oracle / 随机零假设 / 机械臂） | 0 |
| `envelope2.py` | 分行情（SPY 涨 76 期 vs 跌 29 期）与四档滚动净值的真实回撤 | 0 |
| `mom_vs_semantic.py` | 同一批候选池里，一条动量线 vs 语义排序 | 0 |
| `partial.py` | 控制住 60 日波动之后，ev 还排不排得动收益 | 0 |
| `probe_cutoff.py` | 生成模型的知识截止在哪（用库里的价格反查它的记忆） | 1 |

`probe_cutoff.py` 是唯一要调模型的，它从 `~/.ideagen.env` 读被注释掉的 key，
只在进程内使用、不写回——同 `scripts/backfill_weeks.py` 的做法。

输出落在 `scripts/analysis/_out/`（未入库）。
