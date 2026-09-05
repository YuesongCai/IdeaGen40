#!/usr/bin/env python3
"""把库里已存的回测摘要文案改成新词表，和 run_real_backtest.py 未来产出的措辞对齐。

只动 backtest_runs.summary 里的**展示文案**：不碰任何数字、不碰 inputs_sha、
不碰 orch_runs.error（那是历史事件记录，改它等于篡改日志）。
每行原值先备份进 backtest_runs_summary_backup。

用法： retag_backtest_summary.py <db路径> [--apply]
"""
import sys, sqlite3, json

PAIRS = [
    # 回测语境里的「臂」= 被并排比较的组合
    ("两个来源限定臂", "两个来源限定组合"),
    ("同时进入两本账", "同时进入两个组合"),
    ("（各臂 ", "（各组合 "),
    ("低估每条臂一个建仓成本", "低估每个组合一个建仓成本"),
    ("而这些臂并不满足", "而这些组合并不满足"),
    ("它拿每条臂自己实现的", "它拿每个组合自己实现的"),
    ("ev_rank 臂按 exploratory 注册", "ev_rank 按 exploratory 注册"),
    ("各臂的满窗口占比还不相同", "各组合的满窗口占比还不相同"),
    ("「两臂被同样截断、截断会抵消」", "「两个组合被同样截断、截断会抵消」"),
    ("有的臂从正翻到负", "有的组合从正翻到负"),
    ("相对对照臂 ", "相对对照组合 "),
    ("对照臂 buy_all 不做任何挑选", "对照组合 buy_all 不做任何挑选"),
    ("各臂的超额都含有这一部分", "各组合的超额都含有这一部分"),
    ("判定该臂 powered", "判定该组合 powered"),
    ("忽略了同臂持仓的相关性", "忽略了同一组合内持仓的相关性"),
    ("算不出门槛的臂，标为 underpowered", "算不出门槛的组合，标为 underpowered"),
    ("忽略了同臂持仓之间的相关性", "忽略了同一组合内持仓之间的相关性"),
    ("越过它的臂标为 not_ruled_out", "越过它的组合标为 not_ruled_out"),
    ("没有任何臂", "没有任何组合"),
    ("为 false 的臂，其结论", "为 false 的组合，其结论"),
    ("关于该臂的判断", "关于该组合的判断"),
    ("各臂 30", "各组合 30"),
    ("每条臂", "每个组合"), ("各臂", "各组合"), ("该臂", "该组合"),
    ("两臂", "两个组合"), ("的臂", "的组合"), ("条臂", "个组合"),
    # 语料 → 研报
    ("从当周语料里发现并命名", "从当周研报里发现并命名"),
    ("语料发布日", "研报发布日"),
    ("语料", "研报"),
]

def main():
    if len(sys.argv) < 2:
        print(__doc__); return 2
    db, apply = sys.argv[1], "--apply" in sys.argv
    con = sqlite3.connect(db); con.row_factory = sqlite3.Row
    rows = con.execute("SELECT backtest_id, summary FROM backtest_runs").fetchall()
    changed, samples = [], []
    for r in rows:
        s0 = r["summary"] or ""
        s = s0
        for o, n in PAIRS:
            s = s.replace(o, n)
        if s == s0:
            continue
        json.loads(s)                     # 改完必须仍是合法 JSON
        a, b = json.loads(s0), json.loads(s)
        def nums(o, out):                 # 数字一个都不能动
            if isinstance(o, dict):
                [nums(v, out) for v in o.values()]
            elif isinstance(o, list):
                [nums(v, out) for v in o]
            elif isinstance(o, (int, float)) and not isinstance(o, bool):
                out.append(o)
        na, nb = [], []
        nums(a, na); nums(b, nb)
        assert na == nb, f"{r['backtest_id']} 的数字被动了"
        assert a.get("inputs_sha") == b.get("inputs_sha")
        changed.append((r["backtest_id"], s))
        for w in ("臂", "语料", "本账"):
            i = s0.find(w)
            if i >= 0 and len(samples) < 4:
                samples.append((s0[max(0, i-45):i+35], s[max(0, i-45):i+35]))
    print(f"{len(rows)} 行摘要，{len(changed)} 行需要改。数字/inputs_sha 逐行核对：未变。")
    for o, n in samples:
        print(f"  旧 …{o}…\n  新 …{n}…\n")
    left = sum((s.count("臂") + s.count("语料") + s.count("本账")) for _, s in changed)
    print(f"改完残留旧词：{left}")
    if not apply:
        print("（空跑，未写库。加 --apply 才写）"); return 0
    con.execute("CREATE TABLE IF NOT EXISTS backtest_runs_summary_backup"
                "(backtest_id TEXT PRIMARY KEY, summary TEXT, saved_at TEXT)")
    for bid, s in changed:
        con.execute("INSERT OR REPLACE INTO backtest_runs_summary_backup "
                    "SELECT backtest_id, summary, datetime('now') FROM backtest_runs "
                    "WHERE backtest_id=?", (bid,))
        con.execute("UPDATE backtest_runs SET summary=? WHERE backtest_id=?", (s, bid))
    con.commit()
    print(f"已写库 {len(changed)} 行；原值在 backtest_runs_summary_backup 里，"
          "回滚：UPDATE backtest_runs SET summary=(SELECT summary FROM "
          "backtest_runs_summary_backup b WHERE b.backtest_id=backtest_runs.backtest_id) "
          "WHERE backtest_id IN (SELECT backtest_id FROM backtest_runs_summary_backup);")
    return 0

sys.exit(main())
