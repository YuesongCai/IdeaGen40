"""The audit bundle: one downloadable zip that reconstructs a run end to end.

The dashboard already shows every stage of a run, but showing is not the same
as handing over. A reader who wants to check the system — a PM, an allocator,
an auditor — needs the material in their own hands, in an order that reads
without the UI: what went in, what each step decided, what came out, and every
question anyone later asked about it together with the answer given.

What the bundle deliberately does NOT contain: the research-report bodies. Those
are partner data. The bundle carries the citation ids the run actually used, so
any claim can be traced back to a specific document, but the documents
themselves stay behind the source's own licence.

Machine identity (bucket names, home paths, cloud account ids) is scrubbed on
the way out, the same discipline as `/api/journal` — provenance is timestamps
and step structure, not the machine that ran it.
"""
from __future__ import annotations

import hashlib
import io
import json
import zipfile
from typing import Any

from . import ask, config

#: Selector and generator ids carry English keys in the artifacts; the bundle is
#: read by people, so filenames say what the thing is.
GEN_ZH = {"ai_native": "AI端到端", "carl_constraint": "约束边界",
          "chain": "传导链", "gap": "共识缺口"}


def _sel_name(key: str) -> str:
    """`omega_strict` means nothing to a reader; the registry's own label does.

    Read from the strategy registry rather than a second hand-kept table — a
    new arm should appear in the bundle under its real name without anyone
    remembering to update this file.
    """
    try:
        from . import strategy  # noqa: PLC0415
        for entry in strategy.available("idea_selector"):
            if entry.get("name") == key:
                label = (entry.get("label") or "").split(". ", 1)[-1].strip()
                if label and label != key:
                    return f"{key}_{label}".replace("/", "／")
    except Exception:  # noqa: BLE001 — a nameless file beats a failed export
        pass
    return key


def _dumps(obj: Any) -> bytes:
    return json.dumps(ask.scrub(obj), ensure_ascii=False, indent=1,
                      default=str).encode("utf-8")


def _ask_entries(run_id: str) -> list[dict]:
    """Every question asked about this run, with the answer that was given."""
    out: list[dict] = []
    try:
        with ask.ASK_LOG.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("run_id") == run_id:
                    out.append(row)
    except OSError:
        pass
    return out


def _readme(run: dict, journal: dict | None, files: list[tuple[str, int]],
            n_asks: int) -> str:
    """The bundle's own explanation, in the order a person reads it."""
    steps = {s.get("step"): s for s in ((journal or {}).get("steps") or [])}
    topics = (steps.get("topics") or {}).get("chosen") or []
    gen = steps.get("generators") or {}
    pool = steps.get("pool") or {}
    sel = steps.get("selectors") or {}
    picks = sel.get("chosen") or {}
    lines = [
        f"# 运行审计包 · {run.get('as_of')} 期",
        "",
        f"- 运行编号：`{run.get('run_id')}`",
        f"- 类型：{run.get('kind')}　结果：{'完成' if run.get('ok') else '失败'}",
        f"- 起止：{run.get('started_at')} → {run.get('ended_at')}（UTC）",
        f"- 模型调用次数：{run.get('calls')}",
        f"- 输入指纹：`{run.get('inputs_sha') or (steps.get('inputs') or {}).get('sha') or '—'}`",
        "",
        "## 这一期发生了什么",
        "",
    ]
    inp = steps.get("inputs") or {}
    if inp:
        lines.append(
            f"1. **封存材料**：{inp.get('corpus')} 篇研报、{inp.get('calendar')} 条宏观日历。"
            "封存之后所有判断只能用这一份——包里的每个结论都能回到这个指纹上。")
    if topics:
        lines.append(
            f"2. **选主题**：打分后选出 {len(topics)} 个：{'、'.join(topics)}。"
            "落选主题的分数与短板见 `02_选主题.json` 的 `scores` 与 `rejected`。")
    if gen:
        prod = gen.get("produced") or {}
        detail = "、".join(f"{GEN_ZH.get(k, k)} {v} 条" for k, v in prod.items())
        lines.append(f"3. **写想法**：4 种写法互不通气，共写出 {gen.get('pool')} 条（{detail}）。")
    if pool:
        lines.append(
            f"4. **合并候选池**：{pool.get('raw')} 条合并成 {pool.get('merged')} 条，一个标的一条。")
    if picks:
        lines.append(
            f"5. **挑持仓**：{len(picks)} 种挑法从同一个 {sel.get('pool')} 条池子里各挑各的。"
            "唯一的变量是挑法本身，这是它们之间的胜负日后能算数的前提。")
    lines += [
        "",
        "## 包里有什么",
        "",
        "| 文件 | 是什么 |",
        "| --- | --- |",
        "| `01_运行日志.json` | 真实时钟的逐步日志：每一步的时间、耗时、产出、端口自检 |",
        "| `02_选主题.json` | 语义打分臂：18 个主题的完整打分、入选 5 个、落选各差多少 |",
        "| `02_选主题_纯数数对照.json` | 不做语义判断、只数提及次数的对照臂——两者的分歧是本系统最大赌注的读数 |",
        "| `03_写想法/*.json` | 4 种写法各自写出的想法原文，含论点、赔率、引用的研报编号 |",
        "| `04_候选池.json` | 合并后的候选池，一标一条 |",
        "| `05_挑持仓/*.json` | 每种挑法从同池里挑了什么，以及它自己的打分 |",
        "| `06_追问记录.jsonl` | 事后向「当时的它」提过的问题与回答"
        f"（本期 {n_asks} 条）——答案只依据上面这些封存材料 |",
        "| `manifest.json` | 每个文件的字节数与 SHA-256，用来验证包没被改过 |",
        "",
        "## 这个包不包含什么",
        "",
        "- **研报正文**。那是数据源的版权内容。包里保留的是引用编号"
        "（`citations` 字段，如 `ib:103758`），任何一条论点都能指回具体某一篇，"
        "但正文需要在数据源侧查阅。",
        "- **机器身份**。对象存储的桶名、主机路径、云账号编号在导出时已抹掉。",
        "",
        "## 怎么核对",
        "",
        "1. 先读 `01_运行日志.json` 的 `steps`：时间戳是真实时钟，不是事后补的。",
        "2. 挑一条你关心的持仓，在 `05_挑持仓/` 里找到它，看是哪种挑法选的；",
        "3. 拿它的 `id` 回到 `04_候选池.json` 和 `03_写想法/`，看当时写的论点和赔率；",
        "4. 顺着 `citations` 回到研报编号，确认论点有出处而不是凭空生成；",
        "5. `manifest.json` 里的 SHA-256 可以验证这几个文件从导出到现在没被动过。",
        "",
        f"导出文件 {len(files)} 个，合计 {sum(n for _, n in files)} 字节。",
    ]
    return "\n".join(lines) + "\n"


def build(p, run_id: str | None) -> tuple[bytes, str] | tuple[None, str]:
    """Return (zip bytes, filename), or (None, error message)."""
    run = ask._run_row(p, run_id)
    if not run:
        return None, "没有找到这次运行的记录"
    journal = ask._journal(p, run)
    members: list[tuple[str, bytes]] = []

    def add(name: str, payload: Any) -> None:
        if payload is None:
            return
        members.append((name, _dumps(payload)))

    add("01_运行日志.json", journal)
    add("02_选主题.json", ask._artifact(p, run, "A_topics.json"))
    add("02_选主题_纯数数对照.json",
        ask._artifact(p, run, "A_topics_counting.json"))
    for key, zh in GEN_ZH.items():
        add(f"03_写想法/{zh}.json",
            ask._artifact(p, run, f"B_generators/{key}.json"))
    add("04_候选池.json", ask._artifact(p, run, "B_pool.json"))
    # Selector artifacts are discovered rather than listed: the set of arms is
    # allowed to grow, and a bundle that silently omitted a new arm would be
    # the one place the growth is invisible.
    prefix = f"runs/{run['as_of']}/{run['run_id']}/C_selectors/"
    try:
        keys = sorted(p.blobs.list(prefix))
    except Exception:  # noqa: BLE001
        keys = []
    for k in keys:
        name = k.rsplit("/", 1)[-1]
        if not name.endswith(".json"):
            continue
        add(f"05_挑持仓/{_sel_name(name[:-5])}.json", ask._artifact(
            p, run, f"C_selectors/{name}"))

    asks = _ask_entries(run["run_id"])
    if asks:
        members.append(("06_追问记录.jsonl", b"".join(
            json.dumps(ask.scrub(a), ensure_ascii=False).encode() + b"\n"
            for a in asks)))

    manifest = {
        "run_id": run["run_id"],
        "as_of": run["as_of"],
        "kind": run["kind"],
        "ok": bool(run["ok"]),
        "inputs_sha": run.get("inputs_sha"),
        "exported_files": [
            {"name": n, "bytes": len(b),
             "sha256": hashlib.sha256(b).hexdigest()}
            for n, b in members],
        "excluded": ["研报正文（数据源版权内容，包内只保留引用编号）",
                     "机器身份（桶名 / 主机路径 / 云账号编号）"],
    }
    readme = _readme(run, journal, [(n, len(b)) for n, b in members], len(asks))

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.md", readme)
        for name, blob in members:
            z.writestr(name, blob)
        z.writestr("manifest.json",
                   json.dumps(manifest, ensure_ascii=False, indent=1))
    fname = f"IdeaGen40_审计包_{run['as_of']}_{run['run_id'][:16]}.zip"
    return buf.getvalue(), fname
