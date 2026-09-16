"""策略停用入库（WS-E）。

守的三件事：默认什么都不入库（不改现有行为）；入库的策略被不点名的周跑跳过、
取出后回来；记录只追加、对照类不许入库。不联网。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

os.environ.setdefault("WISBURG_MCP_URL", "https://research.example/mcp")
os.environ.setdefault("OLIVE_MCP_URL", "https://catalog.example/mcp")

from ideagen import strategy as strat, strategy_inventory as inv  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def journal(tmp_path, monkeypatch):
    p = tmp_path / "strategy_inventory.jsonl"
    monkeypatch.setenv("IDEAGEN_STRATEGY_INVENTORY", str(p))
    return p


def _names(kind):
    return [r["name"] for r in strat.available(kind)]


def test_default_shelves_nothing(journal):
    assert not journal.exists()
    for kind in ("idea_selector", "idea_generator"):
        assert inv.active(kind, _names(kind)) == _names(kind)
    # and the repository does not ship a journal that would shelve something
    shipped = ROOT / "themes" / inv.FILENAME
    if shipped.exists():
        rows = inv.read([shipped])
        assert not inv.shelved("idea_selector", rows) and not inv.shelved("idea_generator", rows)


def test_shelve_then_restore(journal):
    inv.record("selector", "spread", "shelve", reason="逆风期分散约束无效", by="测试", at="2026-09-17")
    assert "spread" not in inv.active("idea_selector", _names("idea_selector"))
    assert inv.active("idea_generator", _names("idea_generator")) == _names("idea_generator")
    inv.record("selector", "spread", "restore", reason="样本够了再看", by="测试", at="2026-09-18")
    assert "spread" in inv.active("idea_selector", _names("idea_selector"))
    lines = [json.loads(x) for x in journal.read_text(encoding="utf-8").splitlines()]
    assert [x["action"] for x in lines] == ["shelve", "restore"]      # append-only history
    blk = inv.state_block()["kinds"]["idea_selector"]
    item = next(i for i in blk["items"] if i["name"] == "spread")
    assert item["status"] == "active" and len(item["history"]) == 2


def test_generators_can_be_shelved(journal):
    inv.record("generator", "gap", "shelve", reason="连续四期零入选", by="测试")
    assert "gap" not in inv.active("idea_generator", _names("idea_generator"))


def test_controls_and_unknown_names_are_refused(journal):
    with pytest.raises(inv.InventoryError):
        inv.record("selector", "buy_all", "shelve", reason="x", by="y")
    with pytest.raises(inv.InventoryError):
        inv.record("selector", "no_such_arm", "shelve", reason="x", by="y")
    with pytest.raises(inv.InventoryError):
        inv.record("selector", "spread", "restore", reason="x", by="y")    # not shelved
    with pytest.raises(inv.InventoryError):
        inv.record("selector", "spread", "shelve", reason="", by="y")      # reason required
    assert not journal.exists()


def test_a_malformed_line_is_an_error_not_a_skip(journal):
    journal.write_text('{"kind": "idea_selector", "name": "spread"\n', encoding="utf-8")
    with pytest.raises(inv.InventoryError):
        inv.active("idea_selector", ["spread"])
    assert inv.state_block()["error"]


def test_the_weekly_run_reads_the_inventory_for_default_lists():
    """Named runs keep running what they name; only the default lists are filtered.
    Three places build a default list — the model check, stage B and stage C."""
    src = (ROOT / "ideagen" / "orchestrator.py").read_text(encoding="utf-8")
    assert len(re.findall(r'_inv\.active\(\s*"idea_selector"', src)) == 2
    assert len(re.findall(r'_inv\.active\(\s*"idea_generator"', src)) == 2
    assert not re.search(r'else \\\n\s*\[r\["name"\] for r in strat\.available\("idea_(selector|generator)"\)\]', src)
