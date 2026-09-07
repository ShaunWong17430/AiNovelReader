"""阶段 3 DoD 静态断言（IMPLEMENTATION_PLAN 阶段 3 DoD / G2）：
- `cli\\*` 全部 ≤120 行（含新增 5 命令，回归断言）；
- FakeLLM 全流程无网络可复现：backend=fake 时屏蔽 openai 包 chat 仍成功；
- llm.log 每行合法 NDJSON 且无 api_key / 完整 prompt（grep 源码 + 实测）；
- 业务规则只准在 core\\*：cli 不泄漏 LLM 键控/解析/校验关键词；
- core 阶段 3 模块全部可导入。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from conftest import CODE_ROOT, DEFAULT_NOVEL_NAME
from helpers import init_novel_direct, write_config

MAX_CLI_LINES = 120


def _line_count(p: Path) -> int:
    return len(p.read_text(encoding="utf-8").splitlines())


def test_cli_files_all_within_120_lines():
    cli_dir = CODE_ROOT / "cli"
    files = sorted(cli_dir.glob("*.py"))
    over = [(f.name, _line_count(f)) for f in files
            if _line_count(f) > MAX_CLI_LINES]
    assert not over, f"cli 超 120 行: {over}"


def test_cli_new_commands_within_120_lines():
    for name in ("render_notes.py", "verify_notes.py", "write_summary.py",
                 "verify_summary.py", "update_metadata.py"):
        p = CODE_ROOT / "cli" / name
        assert p.is_file()
        assert _line_count(p) <= MAX_CLI_LINES, f"{name} 超 120 行"


def test_cli_contains_no_llm_business_keywords():
    """LLM 键控/解析/收敛逻辑只准在 core：cli 不得出现其判定关键词。"""
    banned = ("fixture_key", "parse_json_three_tier", "sanitize_reader_output",
              "validate_reader_output", "retry_on_status", "FATAL_CODES",
              "finish_reason", "PAYLOAD_TOO_LARGE")
    for f in sorted((CODE_ROOT / "cli").glob("*.py")):
        if f.name == "_common.py":
            continue
        src = f.read_text(encoding="utf-8")
        for kw in banned:
            assert kw not in src, f"{f.name} 泄漏业务关键词 {kw}"


def test_llm_log_payload_never_logged_in_source():
    """llm.log 绝不记完整 prompt：req_fp 用 sha256 哈希（§8.5）。"""
    src = (CODE_ROOT / "core" / "llm_client.py").read_text(encoding="utf-8")
    assert "llm_req_fp(model, phase, chunk_id, prompt)" in src
    # log_line 的 entry 不含 prompt 键
    assert '"prompt"' not in src.replace('"prompt": prompt', "")


def test_fake_llm_works_without_openai_package(base_dir, monkeypatch):
    """G2/DoD：FakeLLM 全流程无网络可复现——openai 不可导入时 chat 仍成功。"""
    monkeypatch.setitem(sys.modules, "openai", None)
    monkeypatch.setitem(sys.modules, "openai.types.chat", None)
    config = write_config(base_dir, backend="fake", retry_backoff_s=[0, 0, 0])
    init_novel_direct(base_dir, config, n_chunks=3)
    monkeypatch.setenv("NOVEL_BASE", str(base_dir))
    monkeypatch.setenv("NOVEL_CONFIG", str(config))
    import core.llm_client as llm
    r = llm.chat(phase="reader", novel_name=DEFAULT_NOVEL_NAME,
                 chunk_id="part_001", prompt="p", expect="json")
    assert r.ok, r
    llm.reset_fake()


def test_llm_log_each_line_ndjson_no_secrets(base_dir, monkeypatch):
    """llm.log 每行合法 NDJSON、无 api_key/完整 prompt/响应（实测）。"""
    config = write_config(base_dir, backend="fake", retry_backoff_s=[0, 0, 0])
    init_novel_direct(base_dir, config, n_chunks=3)
    monkeypatch.setenv("NOVEL_BASE", str(base_dir))
    monkeypatch.setenv("NOVEL_CONFIG", str(config))
    import core.llm_client as llm
    prompt = "超长 prompt 不应进入日志：机密正文 XYZ123"
    llm.inject_response("reader:part_002",
                        {"chunk": "part_002", "events": [{"event": "e",
                                                          "impact": "i"}],
                         "notes": {"characters": "c", "plots": ["p"],
                                   "questions": [], "abstract": "a"}})
    r = llm.chat(phase="reader", novel_name=DEFAULT_NOVEL_NAME,
                 chunk_id="part_002", prompt=prompt, expect="json")
    assert r.ok
    llm.reset_fake()
    log_text = (base_dir / DEFAULT_NOVEL_NAME / "llm.log").read_text(
        encoding="utf-8")
    for ln in log_text.splitlines():
        assert ln.strip()
        entry = json.loads(ln)                       # 每行合法 NDJSON
        assert set(entry) <= {"ts", "novel", "batch", "phase", "chunk",
                              "model", "attempt", "prompt_tokens",
                              "completion_tokens", "latency_ms",
                              "finish_reason", "ok", "error_code", "req_fp"}
    assert "api_key" not in log_text
    assert "XYZ123" not in log_text                  # 不记完整 prompt
    assert "{\"chunk\"" not in log_text              # 不记完整响应


def test_phase3_core_modules_importable():
    import core.llm_client  # noqa: F401
    import core.prompts     # noqa: F401
    import core.renderer    # noqa: F401
    import core.sanitize    # noqa: F401
    import core.validator   # noqa: F401


def test_prompts_contract_files_exist():
    """§4 契约文件齐全（§0.3 + summary_output_v1.json）。"""
    for rel in ("prompts/reader_v1.md", "prompts/summarizer_v1.md",
                "prompts/schema/reader_output_v1.json",
                "prompts/schema/summary_output_v1.json"):
        assert (CODE_ROOT / rel).is_file(), f"缺失 {rel}"
