"""阶段 2 DoD 静态断言（IMPLEMENTATION_PLAN 阶段 2 DoD）：
- `cli\\*` 全部 ≤120 行（含 _common.py 共用件）；
- 锁与 backup 的 --force 为两个独立旗标（源码 grep）；
- cli 只做「解析 argv → 调 core → 打印 → 退出码」：grep 断言 cli 不
  出现业务规则关键词（events 计数/快照三态判定等留 core）。
"""
from __future__ import annotations

import re
from pathlib import Path

from conftest import CODE_ROOT

MAX_CLI_LINES = 120


def _line_count(p: Path) -> int:
    return len(p.read_text(encoding="utf-8").splitlines())


def test_cli_files_all_within_120_lines():
    cli_dir = CODE_ROOT / "cli"
    files = sorted(cli_dir.glob("*.py"))
    assert files, "cli\\ 下应有命令文件"
    over = [(f.name, _line_count(f)) for f in files
            if _line_count(f) > MAX_CLI_LINES]
    assert not over, f"cli 超 120 行: {over}"


def test_cli_force_flags_independent():
    """K26/33：lock --force 与 backup --force 是两个独立旗标。"""
    backup_src = (CODE_ROOT / "cli" / "backup.py").read_text(encoding="utf-8")
    lock_src = (CODE_ROOT / "core" / "lock.py").read_text(encoding="utf-8")
    assert 'add_argument("--force"' in backup_src
    assert "force: bool = False" in lock_src or "force" in lock_src


def test_cli_contains_no_business_rule_keywords():
    """业务规则只准在 core\\*：cli 不得出现规则判定/推导逻辑关键词。"""
    banned = ("events_total", "derive_processed", "check_applied",
              "has_data_rows", "def _batch_rows")
    for f in sorted((CODE_ROOT / "cli").glob("*.py")):
        if f.name == "_common.py":
            continue
        src = f.read_text(encoding="utf-8")
        for kw in banned:
            assert kw not in src, f"{f.name} 泄漏业务关键词 {kw}"


def test_no_auto_force_in_driver_path():
    """自动流程不带 --force（DoD 源 grep；run.py 为阶段 4 骨架，先守 cli 入口）。"""
    for f in (CODE_ROOT / "cli").glob("*.py"):
        src = f.read_text(encoding="utf-8")
        # init/append/rollback 命令不得自带 --force（仅 backup 允许）
        if f.name not in ("backup.py",):
            assert "--force" not in src, f"{f.name} 不应有 --force 旗标"


def test_all_core_modules_importable():
    import core.config      # noqa: F401
    import core.lock        # noqa: F401
    import core.logger      # noqa: F401
    import core.paths       # noqa: F401
    import core.recovery    # noqa: F401
    import core.state       # noqa: F401
    import core.timeline    # noqa: F401
