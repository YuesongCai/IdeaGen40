#!/usr/bin/env python3
"""Refuse to publish a payload carrying data that is not ours to republish.

The published site is a deliberate choice: the blotter, the equity curve and the
theses are this project's own output, and publishing them was decided knowingly.
Partner data was not part of that decision, and it reached the payload by a route
nobody chose. `.gitignore` excludes the raw shelf snapshots, but the product codes
survive inside each idea's description and sources, so they flow through the report
into a public, indexable URL — the exclusion looked like protection while the data
went out anyway.

That is the specific failure this check exists to stop: a boundary enforced on the
input while the derived output is unguarded. It runs on what is actually about to be
published, not on the sources, because that is the only place the question can be
answered honestly.

Exit 0 to publish, 1 to refuse. It never edits the payload — deciding what to
redact is a judgement about someone else's licence terms, and it belongs to a
person, not to a pre-push hook.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

#: Shelf identifiers. The `L#####` form is the partner's product code; a match is
#: not a guess about what the string means, it is the partner's own key.
PARTNER_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bL0\d{4}\b", "合作方货架产品代码"),
    (r"Nexus\s*(?:Wealth|Capital|资本|财富)", "合作方机构名"),
)

#: Verbatim third-party research. Any long run of subscription body text is a
#: licence problem regardless of which document it came from, so the check is on
#: the field that would carry it rather than on the words inside it.
BODY_FIELDS = ("body", "full_text", "raw_text", "正文")

#: How much partner branding is incidental (a vehicle's own name in a holding row)
#: versus pervasive. A single legitimate fund name should not block a publish; a
#: payload where the partner is everywhere is a different thing.
BRAND_LIMIT = 20


def scan(text: str, where: str) -> list[str]:
    out: list[str] = []
    for pat, label in PARTNER_PATTERNS:
        hits = sorted(set(re.findall(pat, text)))
        if hits:
            out.append(f"{where}: {label} {len(hits)} 个 — {', '.join(hits[:6])}")

    brand = len(re.findall(r"\bOlive\b", text))
    if brand > BRAND_LIMIT:
        out.append(f"{where}: 出现合作方名称 Olive {brand} 次"
                   f"（阈值 {BRAND_LIMIT}），已不是个别持仓名")

    for f in BODY_FIELDS:
        # A body field with real content, not merely the key appearing somewhere.
        for m in re.finditer(rf'"{f}"\s*:\s*"((?:[^"\\]|\\.){{400,}})"', text):
            out.append(f"{where}: 字段 {f!r} 带有 {len(m.group(1))} 字符的原文，"
                       f"疑似订阅研究正文，不可转载")
            break
    return out


def main(argv: list[str]) -> int:
    targets = [Path(a) for a in argv[1:]] or [
        Path("web/report.json"), Path("web/index.html"), Path("web/artifact.html")]
    checked, problems = 0, []
    for p in targets:
        if not p.exists():
            continue
        checked += 1
        problems += scan(p.read_text(encoding="utf-8", errors="replace"), p.as_posix())

    if not checked:
        # Nothing found to inspect is not a pass. A path that silently matches no
        # file would turn this guard into a no-op exactly when it is most needed.
        print("发布前检查：没有找到任何待发布文件，拒绝发布（无法确认内容）",
              file=sys.stderr)
        return 1

    if problems:
        print(f"\n拒绝发布：{len(problems)} 处内容不属于我们，无权公开转载\n",
              file=sys.stderr)
        for x in problems:
            print(f"  · {x}", file=sys.stderr)
        print("\n公开发布自己的持仓与论点是已经做过的决定；合作方货架数据不在其中。"
              "\n处理办法（需人工判断）："
              "\n  1) 在生成报告时把货架代码与合作方名称替换为内部代号；"
              "\n  2) 或把这些标的的明细从公开产物里剔除，只留聚合数字；"
              "\n  3) 确认无误后再发布。"
              "\n注意：历史提交里已经发布过，删除当前版本不会移除历史——"
              "需要单独决定是否重写公开分支历史。", file=sys.stderr)
        return 1

    print(f"发布前检查通过（检查了 {checked} 个文件）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
