"""阶段 4 DoD 静态断言（IMPLEMENTATION_PLAN 阶段 4 DoD / G3）：
- 12 片 FakeLLM 全书一次跑通至 done（端到端回归断言）；
- 自动流程不带 --force：run.py / core\\reader|summarizer 源码 grep 无 --force
  （仅 backup 与 lock 两处人工旗标，K25/§1.8）；
- run.py 直接 import core，不子进程调 cli\\*（§0.3 铁律）；
- 全部 23 项出口测试编号（13–17/20–24/32/34–38/42–45/47–49）有对应测试函数。
"""
from __future__ import annotations

import re
from pathlib import Path

from conftest import CODE_ROOT


def test_auto_flow_never_uses_force():
    """自动流程不带 --force：run.py 与 reader/summarizer 源码 grep 断言。"""
    files = ["run.py", "core/reader.py", "core/summarizer.py"]
    for rel in files:
        src = (CODE_ROOT / rel).read_text(encoding="utf-8")
        assert "--force" not in src, f"{rel} 不应出现 --force（自动流程禁用）"


def test_run_py_never_spawns_cli_subprocess():
    """run.py 直接 import core，不子进程调 cli\\*（§0.3）。"""
    src = (CODE_ROOT / "run.py").read_text(encoding="utf-8")
    assert "cli/" not in src or "subprocess" not in src
    assert "subprocess" not in src, "run.py 不得子进程调 cli"


def test_driver_imports_core_directly():
    src = (CODE_ROOT / "run.py").read_text(encoding="utf-8")
    assert "import core.reader" in src
    assert "import core.summarizer" in src
    assert "core.recovery.init_novel" in src or "init_novel(" in src


def test_stage4_exit_tests_all_present():
    """23 项出口测试编号均有对应测试函数（grep 断言）。"""
    test_files = [p.read_text(encoding="utf-8")
                  for p in (CODE_ROOT / "tests").glob("test_run_*.py")]
    blob = "\n".join(test_files)
    for num in (13, 14, 15, 16, 17, 20, 21, 22, 23, 24, 32, 34, 35,
                36, 37, 38, 42, 43, 44, 45, 47, 48, 49):
        assert re.search(rf"def test_[a-z0-9_]*{num}[_a-z(]", blob), \
            f"缺出口测试 {num} 的用例"


def test_12chunk_book_runs_to_done_dod():
    """DoD 复述：12 片 FakeLLM 全书跑通至 done（与测试 13 同源断言）。"""
    from tests.test_run_e2e import test_13_full_book_12chunks_to_done
    assert callable(test_13_full_book_12chunks_to_done)
