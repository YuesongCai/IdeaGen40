"""策略停用入库：一个选取策略或生成方式可以「放进库房」，记原因、日期、谁。

Allspring：因子可以按时间关掉、放进 inventory，不是删掉。对我们来说有三件事必须同时成立：

1. **新的周跑跳过它。** 入库的选取策略不再建新仓，入库的生成方式不再出想法。
2. **历史不动。** 已有组合的仓位、净值、判决一行不删——业绩页照常能看它入库前的表现，
   这正是日后决定要不要取出来的依据。
3. **决定有记录。** 追加式 `strategy_inventory.jsonl`（仿 `themes/aliases.jsonl`）：
   每行一个动作（入库 / 取出），谁、哪天、为什么。当前状态是每个策略最后一行动作；
   文件本身就是这件事的完整历史，不存在「悄悄改回来」。

默认什么都不入库：文件不存在时一切照旧，`tests/test_strategy_inventory.py` 守着这一点。

两条刻意的限制：
* 对照类（role=control，如全量基准、随机基准）不许入库。它们是所有比较的零点，停掉
  其中一个，业绩页上「超额 vs 全量基准」一整列会静默变空，而那一列是判断其余策略的尺子。
* 入库只影响**不点名**的周跑。操作者显式用 `--selectors` 点名跑某个策略时照跑：显式
  指令优先，这样入库前后做一次对照补跑不需要先把它取出来。

写到哪：与主题别名同一套持久目录规则（`lexicon.durable_themes_dir`）。云端容器每次
部署重建仓库副本，写在仓库里的入库记录会在下一次代码同步时消失。
"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from typing import Any

from . import config

FILENAME = "strategy_inventory.jsonl"
KINDS = {"selector": "idea_selector", "generator": "idea_generator",
         "idea_selector": "idea_selector", "idea_generator": "idea_generator"}
ACTIONS = ("shelve", "restore")
_FIELDS = {"kind", "name", "action", "reason", "by", "at", "recorded_at"}
_REPO = Path(__file__).resolve().parent.parent / "themes" / FILENAME


class InventoryError(ValueError):
    pass


def paths() -> list[Path]:
    """Files read, in order. `IDEAGEN_STRATEGY_INVENTORY` pins one (tests, replays)."""
    env = os.environ.get("IDEAGEN_STRATEGY_INVENTORY")
    if env:
        return [Path(env)]
    from . import lexicon
    d = lexicon.durable_themes_dir()
    return [_REPO] + ([d / FILENAME] if d else [])


def write_path() -> Path:
    env = os.environ.get("IDEAGEN_STRATEGY_INVENTORY")
    if env:
        return Path(env)
    from . import lexicon
    d = lexicon.durable_themes_dir()
    return (d / FILENAME) if d else _REPO


def read(files: list[Path] | None = None) -> list[dict[str, Any]]:
    """Every recorded action, oldest first. A malformed line is an error, not a skip:
    a silently dropped 「入库」 line would put a stopped strategy back into the run."""
    out: list[dict[str, Any]] = []
    for p in (files if files is not None else paths()):
        if not p.exists():
            continue
        for n, raw in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise InventoryError(f"{p}:{n} 不是合法 JSON：{exc}") from exc
            _validate(row, where=f"{p}:{n}")
            out.append(row)
    out.sort(key=lambda r: (str(r.get("recorded_at") or r["at"])))
    return out


def _validate(row: dict[str, Any], *, where: str) -> None:
    unknown = set(row) - _FIELDS
    if unknown:
        raise InventoryError(f"{where} 有未知字段 {sorted(unknown)}")
    missing = [k for k in ("kind", "name", "action", "at", "by", "reason") if not row.get(k)]
    if missing:
        raise InventoryError(f"{where} 缺少 {missing}（入库与取出都要写谁、哪天、为什么）")
    if row["kind"] not in ("idea_selector", "idea_generator"):
        raise InventoryError(f"{where} kind 必须是 idea_selector / idea_generator")
    if row["action"] not in ACTIONS:
        raise InventoryError(f"{where} action 必须是 {ACTIONS}")


def shelved(kind: str, rows: list[dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    """`name -> the shelve row` for every strategy of `kind` whose last action is shelve."""
    kind = KINDS.get(kind, kind)
    last: dict[str, dict[str, Any]] = {}
    for r in (rows if rows is not None else read()):
        if r["kind"] == kind:
            last[r["name"]] = r
    return {n: r for n, r in last.items() if r["action"] == "shelve"}


def active(kind: str, names: list[str]) -> list[str]:
    """The names a default weekly run should use: registered minus shelved, order kept."""
    off = shelved(kind)
    return [n for n in names if n not in off]


def _registry(kind: str) -> dict[str, dict[str, Any]]:
    from . import strategy as strat
    return {r["name"]: r for r in strat.available(kind)}


def record(kind: str, name: str, action: str, *, reason: str, by: str,
           at: str | None = None, path: Path | None = None) -> dict[str, Any]:
    kind = KINDS.get(kind)
    if not kind:
        raise InventoryError("kind 必须是 selector 或 generator")
    reg = _registry(kind)
    if name not in reg:
        raise InventoryError(f"没有叫 {name!r} 的{'选取策略' if kind == 'idea_selector' else '生成方式'}；"
                             f"已注册：{sorted(reg)}")
    if action == "shelve" and reg[name].get("role") == "control":
        raise InventoryError(f"{name} 是对照（全量基准 / 随机基准一类），是其余策略比较的零点，不能入库")
    rows = read()
    is_off = name in shelved(kind, rows)
    if action == "shelve" and is_off:
        raise InventoryError(f"{name} 已在库中")
    if action == "restore" and not is_off:
        raise InventoryError(f"{name} 不在库中，无需取出")
    row = {"kind": kind, "name": name, "action": action,
           "reason": (reason or "").strip(), "by": (by or "").strip(),
           "at": at or config.today_hkt().isoformat(),
           "recorded_at": config.now_hkt().isoformat()}
    _validate(row, where="新记录")
    p = path or write_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return row


def state_block() -> dict[str, Any]:
    """What the method page lists: every registered strategy, 在用 or 入库, with the record."""
    try:
        rows = read()
        err = None
    except InventoryError as e:
        rows, err = [], str(e)
    out: dict[str, Any] = {"file": str(write_path().name), "error": err, "kinds": {}}
    for kind in ("idea_selector", "idea_generator"):
        off = shelved(kind, rows)
        items = []
        for name, spec in sorted(_registry(kind).items()):
            r = off.get(name)
            items.append({"name": name, "label": spec.get("label") or name,
                          "role": spec.get("role"),
                          "status": "shelved" if r else "active",
                          "reason": r["reason"] if r else None,
                          "at": r["at"] if r else None, "by": r["by"] if r else None,
                          "history": [x for x in rows if x["kind"] == kind and x["name"] == name]})
        out["kinds"][kind] = {"items": items, "n_shelved": len(off),
                              "n_active": len(items) - sum(1 for i in items if i["status"] == "shelved")}
    return out


def cmd_strategy_inventory(args) -> int:
    if args.action == "list":
        b = state_block()
        if b["error"]:
            print("读入库记录失败：", b["error"])
            return 1
        for kind, blk in b["kinds"].items():
            print(f"== {kind}  在用 {blk['n_active']} · 入库 {blk['n_shelved']}")
            for it in blk["items"]:
                tag = "入库" if it["status"] == "shelved" else "在用"
                extra = f"  {it['at']} {it['by']}：{it['reason']}" if it["status"] == "shelved" else ""
                print(f"  [{tag}] {it['name']}{extra}")
        return 0
    if not args.kind or not args.name or not args.reason or not args.by:
        print("add / remove 需要 --kind --name --reason --by")
        return 2
    try:
        row = record(args.kind, args.name, "shelve" if args.action == "add" else "restore",
                     reason=args.reason, by=args.by, at=getattr(args, "as_of", None))
    except InventoryError as e:
        print("拒绝：", e)
        return 1
    print(json.dumps(row, ensure_ascii=False))
    return 0
