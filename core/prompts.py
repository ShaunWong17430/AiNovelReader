"""core/prompts.py — 模板管理（§4.1 / §4.2 / §4.3）。

- `prompts\\{reader|summarizer}_{prompt_version}.md` 用 `string.Template`
  （`$var`）；`metadata.prompt_version` 决定加载，init 锁定、运行期不变。
- 模板内禁任何流程指令（§4.1 铁律）；本模块只做「模板 + 变量 → 文本」。
- reader prompt 变量（§4.2 + K43）：novel_name / chunk_id / summary_text /
  timeline_tail / chunk_text / event_min / event_max / json_schema /
  batch_section（K43 批内接力：本批已读前片事件参考段，可空串）；
- summarizer prompt 变量（§4.3）：summary_text / timeline_slice /
  chunk_start / chunk_end / max_chars。
- 正文/摘要等变量值中的 `$` 先转义为 `$$`（string.Template 语义），
  保证用户内容不会被误当作模板变量。
"""
from __future__ import annotations

import json
from pathlib import Path
from string import Template

from . import CODE_ROOT

PROMPTS_DIR = CODE_ROOT / "prompts"
SCHEMA_DIR = PROMPTS_DIR / "schema"

_cache: dict[str, dict[str, Template]] = {}


class PromptError(Exception):
    """模板/schema 缺失或不可读 → 数据校验失败（退出码 3）。"""


def _load_template(rel: str) -> Template:
    path = PROMPTS_DIR / rel
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PromptError(f"prompt 模板缺失: {path}（{exc}）") from exc
    return Template(text)


def _load_schema_text(name: str, prompt_version: str) -> str:
    path = SCHEMA_DIR / f"{name}_{prompt_version}.json"
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PromptError(f"schema 缺失: {path}（{exc}）") from exc


def load_templates(prompt_version: str) -> dict[str, Template]:
    """按 prompt_version 加载 reader/summarizer 模板（进程内缓存）。"""
    if prompt_version not in _cache:
        _cache[prompt_version] = {
            "reader": _load_template(f"reader_{prompt_version}.md"),
            "summarizer": _load_template(f"summarizer_{prompt_version}.md"),
        }
    return _cache[prompt_version]


def _esc(value: object) -> str:
    """模板值原样字符串化。

    string.Template 对「替换值」不做二次解析（仅模板文本内的 $var/$$ 生效），
    故用户内容中的 `$` 直接保留，无需转义。
    """
    return str(value)


def build_reader_prompt(prompt_version: str, *, novel_name: str, chunk_id: str,
                        summary_text: str, timeline_tail: str, chunk_text: str,
                        event_min: int = 1, event_max: int = 5,
                        batch_section: str = "") -> str:
    """§4.2 + K43 reader prompt：模板 + 前情/正文/时间线/batch_section/schema。"""
    tpl = load_templates(prompt_version)["reader"]
    schema = _load_schema_text("reader_output", prompt_version)
    return tpl.substitute(
        novel_name=_esc(novel_name), chunk_id=_esc(chunk_id),
        summary_text=_esc(summary_text), timeline_tail=_esc(timeline_tail),
        chunk_text=_esc(chunk_text), event_min=_esc(event_min),
        event_max=_esc(event_max), json_schema=schema,
        batch_section=_esc(batch_section))


def build_summarizer_prompt(prompt_version: str, *, summary_text: str,
                            timeline_slice: str, chunk_start: int,
                            chunk_end: int, max_chars: int) -> str:
    """§4.3 summarizer prompt：当前摘要 + 窗口时间线 + 字数上限。"""
    tpl = load_templates(prompt_version)["summarizer"]
    return tpl.substitute(
        summary_text=_esc(summary_text), timeline_slice=_esc(timeline_slice),
        chunk_start=_esc(chunk_start), chunk_end=_esc(chunk_end),
        max_chars=_esc(max_chars))


def reader_schema_text(prompt_version: str) -> str:
    """§4.4 reader 输出 schema 原文（供 prompt 注入 json_schema 变量）。"""
    return _load_schema_text("reader_output", prompt_version)


def dump_json(value: object) -> str:
    """JSON 序列化（供 prompt 变量使用）；失败抛 PromptError。"""
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except (TypeError, ValueError) as exc:
        raise PromptError(f"prompt 变量 JSON 序列化失败: {exc}") from exc
