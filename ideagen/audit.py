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
    return _arm_name("idea_selector", key)


def _gen_name(key: str) -> str:
    """A generator's readable name, GEN_ZH first for the four founding arms.

    The four keep their hand-written short names because they are what the
    methodology document and every review deck call them. Anything else — a
    PM rule grafted onto one of them, say — takes its name from the registry,
    which is the only table that grows on its own.
    """
    if key in GEN_ZH:
        return GEN_ZH[key]
    return _arm_name("idea_generator", key)


def _arm_name(kind: str, key: str) -> str:
    """`omega_strict` means nothing to a reader; the registry's own label does.

    Read from the strategy registry rather than a second hand-kept table — a
    new arm should appear in the bundle under its real name without anyone
    remembering to update this file.
    """
    try:
        from . import strategy  # noqa: PLC0415
        for entry in strategy.available(kind):
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


class ManifestUnavailable(RuntimeError):
    """The corpus manifest could not be built, and the reader must be told."""


def _corpus_manifest(run: dict, con=None) -> bytes | None:
    """One row per report the run froze: where it came from, and how.

    The Nexus audit package the team wants to match records what the AI used
    and what it fetched. For this system that is the retrieval receipt already
    stored beside every document — the exact call that pulled it — plus the
    content hash, so a reader can confirm the report behind a citation is the
    same text the run scored. Titles and receipts only; the licensed body
    stays with its source, which is why `body_len` is here and `body` is not.
    """
    try:
        from . import db, config  # noqa: PLC0415
        from datetime import date as _date, timedelta as _td  # noqa: PLC0415
        # The caller's connection when it has one: reaching for a global here
        # would read a different database than the rest of the bundle.
        con = con if con is not None else db.init()
        as_of = _date.fromisoformat(str(run["as_of"]))
        days = [(as_of - _td(days=i)).isoformat()
                for i in range(config.OBSERVATION_WINDOW_DAYS)]
        rows = db.q(con,
                    "SELECT doc_id, published_d, tier, "
                    "COALESCE(institution, line) AS institution, title, "
                    "retrieval, content_hash, ingested_at, "
                    "length(COALESCE(body,'')) AS body_len "
                    "FROM documents WHERE published_d IN (%s) "
                    "ORDER BY published_d DESC, tier, doc_id"
                    % ",".join("?" * len(days)), days)
    except Exception as e:  # noqa: BLE001 — a missing manifest is reported, not fatal
        raise ManifestUnavailable(f"{type(e).__name__}: {e}") from e
    started = str(run.get("started_at") or "")
    out = []
    for r in rows:
        d = dict(r)
        if started and str(d.get("ingested_at") or "") > started:
            continue          # ingested after the run began: it never saw this
        out.append(json.dumps(ask.scrub(d), ensure_ascii=False,
                              default=str).encode() + b"\n")
    return b"".join(out) if out else None


def _readme(run: dict, journal: dict | None, files: list[tuple[str, int]],
            n_asks: int, gaps: list[tuple[str, str]] | None = None) -> str:
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
            f"1. **输入封存**：{inp.get('corpus')} 篇研报、{inp.get('calendar')} 条宏观日历。"
            "封存后所有判断只能取用这一份材料，包内每一项结论都可回溯到该输入指纹。")
    if topics:
        lines.append(
            f"2. **筛选A 选主题**：打分后入选 {len(topics)} 个：{'、'.join(topics)}。"
            "落选主题的得分与失分项见 `02_选主题.json` 的 `scores` 与 `rejected`。")
    if gen:
        prod = gen.get("produced") or {}
        detail = "、".join(f"{GEN_ZH.get(k, k)} {v} 条" for k, v in prod.items())
        lines.append(f"3. **筛选B 生成候选**：4 种生成方式并行且互不可见，共生成 "
                     f"{gen.get('pool')} 条（{detail}）。")
    if pool:
        lines.append(
            f"4. **合并候选池**：{pool.get('raw')} 条归并为 {pool.get('merged')} 条，同一标的一条。")
    if picks:
        lines.append(
            f"5. **筛选C 选取持仓**：{len(picks)} 种选取策略自同一 {sel.get('pool')} 条候选池独立选取。"
            "唯一变量为选取策略本身，这是后续配对比较成立的前提。")
    lines += [
        "",
        "## 包里有什么",
        "",
        "| 文件 | 是什么 |",
        "| --- | --- |",
        "| `01_运行日志.json` | 真实时钟的逐步日志：每步的时间、耗时、产出与端口自检 |",
        "| `02_选主题.json` | 语义打分：全部主题的完整打分、入选名单、落选主题与入选线的分差 |",
        "| `02_选主题_纯数数对照.json` | 不做语义判断、仅统计提及次数的计数对照打分；两种打分的分歧是「语义打分是否有增量」的实时读数 |",
        "| `03_写想法/*.json` | 每种生成方式各自产出的候选原文，含论点、赔率与引用的研报编号 |",
        "| `04_候选池.json` | 合并后的候选池，一标一条 |",
        "| `05_挑持仓/*.json` | 每种选取策略自同一候选池选中了什么，及其自身打分 |",
        "| `06_追问记录.jsonl` | 事后对该次运行提出的问题与回答"
        f"（本期 {n_asks} 条）；回答仅依据上述封存材料 |",
        "| `07_研报清单.jsonl` | 本期封存的每一篇研报：标题、机构、层级、"
        "**取回它的检索调用**（`retrieval`）与内容哈希。"
        "`03_写想法/` 里的 `citations` 编号在这里能查到对应的那一篇 |",
        "| `08_当时生效的准则.json` | 本期生效的 PM 准则（若有）：原话、它被蒸馏成的"
        "指令、以及每条想法因此必须回答的字段。派生生成方式的产出在 `03_写想法/` 里 |",
        "| `manifest.json` | 每个文件的字节数与 SHA-256，用来验证包没被改过 |",
        "",
    ]
    # The table above lists what a complete bundle holds. When this one is not
    # complete, that has to be said here rather than left for the reader to
    # discover by opening the zip and counting — a shorter file list reads as
    # "the run produced less", which is a claim about the run and may be false.
    if gaps:
        lines += [
            f"> ⚠ **这个包少了上表里的 {len(gaps)} 项**，逐项原因见 "
            "`00_读取缺口.txt`。「没写过」是记录的空缺；「读不到」是打包这台"
            "机器的故障，产物可能仍然存在于别处。",
            "",
        ]
    lines += [
        "## 这个包不包含什么",
        "",
        "- **研报正文**。属数据源版权内容。包内保留引用编号"
        "（`citations` 字段，如 `ib:103758`），任一论点均可回溯到具体文献，"
        "正文需在数据源侧查阅。",
        "- **机器身份**。对象存储桶名、主机路径、云账号编号在导出时已移除。",
        "",
        "## 怎么核对",
        "",
        "1. 读 `01_运行日志.json` 的 `steps`：时间戳为真实时钟，非事后补写。",
        "2. 选定一个关注的持仓，在 `05_挑持仓/` 中定位它由哪种选取策略选中；",
        "3. 以其 `id` 回到 `04_候选池.json` 与 `03_写想法/`，查阅当时的论点与赔率；",
        "4. 沿 `citations` 回溯到研报编号，在 `07_研报清单.jsonl` 里查到那一篇，"
        "看它由哪个检索调用取回、内容哈希是多少；",
        "5. 用 `manifest.json` 中的 SHA-256 验证文件自导出以来未被修改。",
        "",
        f"导出文件 {len(files)} 个，合计 {sum(n for _, n in files)} 字节。",
    ]
    return "\n".join(lines) + "\n"


def build(p, run_id: str | None, con=None) -> tuple[bytes, str] | tuple[None, str]:
    """Return (zip bytes, filename), or (None, error message)."""
    run = ask._run_row(p, run_id)
    if not run:
        return None, "没有找到这次运行的记录"
    journal, journal_why = ask.journal_or_reason(p, run)
    members: list[tuple[str, bytes]] = []
    #: Files the README promises that are not in this zip, each with the reason.
    #: Without this, a bundle built where the artifact store is unreachable is a
    #: README listing eight files and a zip containing one — and it reads as
    #: "this run produced nothing", which is the opposite of what happened.
    gaps: list[tuple[str, str]] = []

    def add(name: str, payload: Any, why: str | None = None) -> None:
        if payload is None:
            gaps.append((name, why or "产物不在包里，原因未记录"))
            return
        members.append((name, _dumps(payload)))

    def add_artifact(name: str, key: str) -> None:
        obj, why = ask.artifact_or_reason(p, run, key)
        add(name, obj, why)

    add("01_运行日志.json", journal, journal_why)
    add_artifact("02_选主题.json", "A_topics.json")
    add_artifact("02_选主题_纯数数对照.json", "A_topics_counting.json")
    # Discovered, not listed — same reason as the selectors below. The four
    # founding arms are no longer the whole set: a PM rule grafted onto one of
    # them runs as its own arm, and a bundle built from a hand-kept list would
    # omit exactly the arm someone added on purpose.
    gen_prefix = f"runs/{run['as_of']}/{run['run_id']}/B_generators/"
    try:
        gen_keys = sorted(p.blobs.list(gen_prefix))
    except Exception as e:  # noqa: BLE001
        # Falling back to the four founding generators is a guess, and one that
        # silently drops a fifth somebody added on purpose. Say it is a guess.
        gen_keys = [gen_prefix + f"{k}.json" for k in GEN_ZH]
        gaps.append(("03_写想法/（目录）",
                     f"列不出产物目录，只能按四种初始生成方式猜，"
                     f"后来新增的会漏掉：{type(e).__name__}: {e}"))
    for k in gen_keys:
        name = k.rsplit("/", 1)[-1]
        if not name.endswith(".json"):
            continue
        add_artifact(f"03_写想法/{_gen_name(name[:-5])}.json",
                     f"B_generators/{name}")
    add_artifact("04_候选池.json", "B_pool.json")
    # Selector artifacts are discovered rather than listed: the set of arms is
    # allowed to grow, and a bundle that silently omitted a new arm would be
    # the one place the growth is invisible.
    prefix = f"runs/{run['as_of']}/{run['run_id']}/C_selectors/"
    try:
        keys = sorted(p.blobs.list(prefix))
    except Exception as e:  # noqa: BLE001
        keys = []
        gaps.append(("05_挑持仓/（目录）",
                     f"列不出产物目录，本包没有任何选取策略产物："
                     f"{type(e).__name__}: {e}"))
    for k in keys:
        name = k.rsplit("/", 1)[-1]
        if not name.endswith(".json"):
            continue
        add_artifact(f"05_挑持仓/{_sel_name(name[:-5])}.json",
                     f"C_selectors/{name}")

    try:
        manifest_rows = _corpus_manifest(run, con)
    except ManifestUnavailable as e:
        manifest_rows, gaps = None, gaps + [("07_研报清单.jsonl", str(e))]
    if manifest_rows:
        members.append(("07_研报清单.jsonl", manifest_rows))

    # The rules in force when this run happened. Without them a replay on
    # another machine rebuilds four arms where the run had five, and the extra
    # arm's positions have no explanation anywhere in the bundle.
    try:
        from . import philosophy  # noqa: PLC0415
        from datetime import date as _date  # noqa: PLC0415
        cards = philosophy.cards(as_of=_date.fromisoformat(str(run["as_of"])))
    except Exception:  # noqa: BLE001
        cards = []
    if cards:
        add("08_当时生效的准则.json", cards)

    asks = _ask_entries(run["run_id"])
    if asks:
        members.append(("06_追问记录.jsonl", b"".join(
            json.dumps(ask.scrub(a), ensure_ascii=False).encode() + b"\n"
            for a in asks)))

    if gaps:
        members.insert(0, ("00_读取缺口.txt", (
            "这个包少了下面这些文件。少的原因写在每一行后面——\n"
            "「没写过」是记录本身的空缺，「读不到」是打包这台机器的故障，\n"
            "后者意味着产物可能仍然存在，只是这台机器够不着。\n\n"
            + "\n".join(f"- {n}\n  {why}" for n, why in gaps)
            + "\n").encode()))

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
        # Distinct from `excluded`: those are left out on purpose, these could
        # not be got. Collapsing them would let a read failure pass for policy.
        "gaps": [{"name": n, "reason": ask._scrub_text(w)} for n, w in gaps],
    }
    readme = _readme(run, journal, [(n, len(b)) for n, b in members],
                     len(asks), gaps)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.md", readme)
        for name, blob in members:
            z.writestr(name, blob)
        z.writestr("manifest.json",
                   json.dumps(manifest, ensure_ascii=False, indent=1))
    fname = f"IdeaGen40_审计包_{run['as_of']}_{run['run_id'][:16]}.zip"
    return buf.getvalue(), fname
