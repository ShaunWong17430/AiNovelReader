"""D7 交付物：core/prompts.py 模板管理（§4.1/§4.2/§4.3）测试。

- string.Template 加载（prompt_version 锁定、运行期不变）；
- build_reader_prompt / build_summarizer_prompt 渲染契约变量；
- 变量值中的 `$` 转义（$ → $$，防止用户内容被误当模板变量）；
- 未知 prompt_version → PromptError（数据校验失败 3）。
"""
from __future__ import annotations

import pytest

from conftest import CODE_ROOT

import core.prompts as prompts


def test_load_templates_v1():
    tpl = prompts.load_templates("v1")
    assert set(tpl) == {"reader", "summarizer"}
    assert "$novel_name" in tpl["reader"].template
    assert "$max_chars" in tpl["summarizer"].template


def test_build_reader_prompt_contract_vars():
    text = prompts.build_reader_prompt(
        "v1", novel_name="示例书名", chunk_id="part_003",
        summary_text="（无前情）", timeline_tail="",
        chunk_text="正文内容", event_min=1, event_max=5)
    for token in ("示例书名", "part_003", "（无前情）", "正文内容"):
        assert token in text
    assert '"required"' in text and '"events"' in text    # schema 全文嵌入
    assert "json_schema" not in text              # 无未替换变量
    assert "$novel_name" not in text and "$chunk_id" not in text
    assert "$summary_text" not in text and "$timeline_tail" not in text
    assert "$chunk_text" not in text              # 无残留 $var 模板变量


def test_build_summarizer_prompt_contract_vars():
    text = prompts.build_summarizer_prompt(
        "v1", summary_text="旧摘要", timeline_slice="| 1 | part_001 | e | i |",
        chunk_start=6, chunk_end=10, max_chars=5000)
    for token in ("旧摘要", "6", "10", "5000", "| 1 | part_001 |"):
        assert token in text
    assert "timeline_slice" not in text


def test_dollar_in_user_content_is_escaped():
    """$ 转义：summary_text 含 $var 不应触发 substitute KeyError，字面保留。"""
    text = prompts.build_reader_prompt(
        "v1", novel_name="书名$X", chunk_id="part_001",
        summary_text="价格 $99 与 $var 字面", timeline_tail="",
        chunk_text="正文", event_min=1, event_max=5)
    assert "价格 $99 与 $var 字面" in text
    assert "书名$X" in text


def test_unknown_prompt_version_raises():
    with pytest.raises(prompts.PromptError):
        prompts.build_reader_prompt("v99", novel_name="n", chunk_id="part_001",
                                    summary_text="", timeline_tail="",
                                    chunk_text="c", event_min=1, event_max=5)


def test_reader_schema_text_available():
    schema = prompts.reader_schema_text("v1")
    assert '"chunk"' in schema and '"events"' in schema
    assert (CODE_ROOT / "prompts" / "schema" / "reader_output_v1.json").is_file()
