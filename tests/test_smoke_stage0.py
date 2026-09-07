"""阶段 0 冒烟测试 —— 出口：pytest 可收集、可运行；目录结构与 §0.3 一致。

覆盖：
1. 运行解释器 == Python 3.10（§0.1 锁定 pyfordsh）；
2. 三方依赖可导入（openai / tiktoken / pytest，§0.4）；
3. 目录结构与 §0.3 一致；
4. BASE 沙箱隔离（每用例独立临时 BASE/ROOT，互不串扰；真实 BASE 由
   conftest autouse 守卫兜底）；
5. FakeLLM fixture 键控约定（reader (phase, chunk_id) / summarizer
   (phase, "start-end")，K10/K30；故障注入同键控）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from conftest import (CODE_ROOT, REAL_BASE, SANDBOX_ROOT, READER, SUMMARIZER,
                      fixture_key, split_fixture_key)

# §0.3 代码布局（相对 CODE_ROOT）
EXPECTED_LAYOUT = (
    "config.json", "run.py",
    # core\
    "core/paths.py", "core/config.py", "core/state.py", "core/logger.py",
    "core/lock.py", "core/llm_client.py", "core/prompts.py",
    "core/validator.py", "core/sanitize.py", "core/reader.py",
    "core/summarizer.py", "core/renderer.py", "core/recovery.py",
    # cli\
    "cli/init_novel.py", "cli/backup.py", "cli/append_timeline.py",
    "cli/verify_timeline.py", "cli/tail_timeline.py", "cli/chunk_stats.py",
    "cli/render_notes.py", "cli/verify_notes.py", "cli/write_summary.py",
    "cli/verify_summary.py", "cli/update_metadata.py", "cli/rollback.py",
    # prompts\
    "prompts/reader_v1.md", "prompts/summarizer_v1.md",
    "prompts/schema/reader_output_v1.json",
    "prompts/schema/summary_output_v1.json",
)


def test_python_runtime_is_3_10():
    assert sys.version_info[:2] == (3, 10), f"要求 Python 3.10，实际 {sys.version}"
    assert "pyfordsh" in Path(sys.executable).parts, (
        f"应使用 pyfordsh 解释器（§0.1），实际 {sys.executable}")


def test_third_party_deps_importable():
    import openai
    import tiktoken
    assert hasattr(openai, "OpenAI")
    assert hasattr(tiktoken, "get_encoding")


def test_code_layout_matches_spec_0_3():
    for rel in EXPECTED_LAYOUT:
        assert (CODE_ROOT / rel).is_file(), f"缺失 §0.3 文件: {rel}"
    for d in ("core", "cli", "prompts", "prompts/schema",
              "tests", "tests/fixtures"):
        assert (CODE_ROOT / d).is_dir(), f"缺失 §0.3 目录: {d}"
    import core  # noqa: F401  core 包可导入（run.py 直接 import core，§0.3）


def test_fake_llm_key_convention():
    # reader: (phase, chunk_id)
    assert fixture_key(READER, "part_001") == "reader:part_001"
    # summarizer: (phase, "start-end")（K30）
    assert fixture_key(SUMMARIZER, chunk_start=1, chunk_end=5) == "summarizer:1-5"
    assert split_fixture_key("summarizer:6-10") == (SUMMARIZER, "6-10")
    # 非法输入一律拒绝
    with pytest.raises(ValueError):
        fixture_key("unknown_phase", "part_001")
    with pytest.raises(ValueError):
        fixture_key(SUMMARIZER, chunk_start=5, chunk_end=1)
    with pytest.raises(ValueError):
        split_fixture_key("reader:")
    with pytest.raises(ValueError):
        split_fixture_key("summarizer:abc")


def test_fixture_store_smoke(fixture_store):
    # reader 预设 = 完整 JSON（§4.4），chunk 严格等于 chunk_id
    resp = fixture_store.response(READER, "part_001")
    assert isinstance(resp, dict) and resp["chunk"] == "part_001"
    assert 1 <= len(resp["events"]) <= 5
    assert set(resp["notes"]) == {"characters", "plots", "questions", "abstract"}
    # summarizer 预设 = 纯文本单段（§4.3）
    summary = fixture_store.response(SUMMARIZER, chunk_start=1, chunk_end=5)
    assert isinstance(summary, str) and summary.strip() and "\n" not in summary
    # 故障注入同键控，kind ∈ §8.2
    fault = fixture_store.fault(READER, "part_002")
    assert fault is not None and fault["kind"] == "NETWORK"
    assert fault["attempts"] == 2
    assert fixture_store.fault(READER, "part_001") is None      # 无注入
    assert fixture_store.fault(SUMMARIZER, chunk_start=6, chunk_end=10)["kind"] == "TRUNCATED"
    with pytest.raises(KeyError):
        fixture_store.response(READER, "part_999")


def test_sandbox_isolation_case_a(root_dir, base_dir):
    # 临时 BASE/ROOT 必须完全位于自建沙箱根下，与真实 BASE 无关
    assert SANDBOX_ROOT in base_dir.parents
    assert REAL_BASE not in base_dir.parents
    assert (root_dir / "chunks").is_dir()
    (root_dir / "chunks" / "case_a_marker.txt").write_text("A", encoding="utf-8")
    assert (root_dir / "chunks" / "case_a_marker.txt").is_file()


def test_sandbox_isolation_case_b(root_dir):
    # 每用例独立临时 ROOT：上一用例的产物不可见（防止跨用例污染）
    assert not (root_dir / "chunks" / "case_a_marker.txt").exists()
