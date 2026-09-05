#!/usr/bin/env python3
"""Do the tests still pass with only what the container image ships?

The self-updater gates on the suite passing inside the image, and the image
carries code without `data/` or most of `deploy/`. A test that reaches for the
live database is green here and red there — and when it goes red it blocks the
deploy of everything else in the same push, invisibly, because the image is the
only place it fails. That happened: ten errors from a consistency suite whose
`setUpClass` opened the database, sitting on the gate while a dozen unrelated
fixes waited behind it.

This builds the image's file tree from the Dockerfile's own COPY list — reading
it rather than restating it, so adding a COPY line does not silently invalidate
the check — and runs pytest there.

    python3 scripts/check_image_tree.py

Exit code is pytest's. A test with nothing to say in this tree should skip; a
test that errors here will stop a deploy.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCKERFILE = os.path.join(ROOT, "deploy", "Dockerfile")


def copy_list() -> list[str]:
    """Sources from the Dockerfile's COPY lines, in order."""
    out: list[str] = []
    with open(DOCKERFILE, encoding="utf-8") as fh:
        for line in fh:
            m = re.match(r"\s*COPY\s+(?:--\S+\s+)*(\S+)\s+(\S+)", line)
            if m and not m.group(1).startswith("--"):
                out.append(m.group(1))
    return out


def build_tree(dest: str) -> list[str]:
    copied = []
    for src in copy_list():
        s = os.path.join(ROOT, src)
        d = os.path.join(dest, src)
        if not os.path.exists(s):
            continue
        os.makedirs(os.path.dirname(d) or dest, exist_ok=True)
        if os.path.isdir(s):
            shutil.copytree(s, d, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(s, d)
        copied.append(src)
    return copied


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ideagen-imgtree-") as tmp:
        copied = build_tree(tmp)
        print(f"按 Dockerfile 的 COPY 列表建树：{', '.join(copied)}")
        for absent in ("data", "deploy/compose.yaml", "deploy/Caddyfile"):
            here = os.path.exists(os.path.join(tmp, absent))
            print(f"  {absent:<22}{'⚠ 意外存在' if here else '不在树里（与镜像一致）'}")
        print()
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/", "-q", "--no-header"],
            cwd=tmp, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        print()
        if proc.returncode == 0:
            print("✓ 镜像树上通过：自更新闸门不会被测试挡下。")
        else:
            print("✗ 镜像树上失败：这会拦下同一次推送里所有人的改动。"
                  "\n  在这棵树上没有可断言对象的测试应当 skip，而不是 error。")
        return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
