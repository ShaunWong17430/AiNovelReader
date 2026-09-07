"""core/validator.py — 输出校验（§4.4 / §9.4 / §9.5）。

- `validate_reader_output(data, chunk_id)`：§4.4 reader 输出 schema；
  `chunk` 严格等于本次 chunk_id，不匹配 → SCHEMA；`events` ∈ [1,5]
  （>5 由 sanitize ② 档收敛，K29；0 → ③ 不可救）；笔记四段约束；
- `validate_summary_text(text, max_chars)`：§9.4 小结校验（单段 / ≤ max_chars /
  §1.6 字数口径）；BOM 属文件层检查（verify_summary 读 bytes）；
- `validate_notes_file(path, start, end, padding)`：§9.5 notes 校验——
  文件名/H1 无空格（B11/D13）、5 个二级标题按序（B7 关键词匹配，失败）；
  章节范围与文件名一致（自愈路径失败仅警告）。

校验失败 = 退出码 3（数据校验失败）；「仅警告」项随返回值带出。
"""
from __future__ import annotations

import re
from pathlib import Path

from .timeline import count_chars

# §4.4 上限（与 sanitize ② 档一致，此处为收敛后的兜底防线）
_MAX_CHARACTERS = 500
_MAX_ABSTRACT = 300
_MAX_CELL = 100
_MAX_QUESTIONS = 3
_MAX_EVENTS = 5

_NOTES_NAME_RE = re.compile(r"^part_(\d+)~part_(\d+)\.md$")
_TITLES = ("阅读范围", "角色发展", "关键情节", "疑问", "摘要")   # B7 按序关键词


def validate_reader_output(data: object, chunk_id: str) -> list[str]:
    """§4.4 reader 输出 schema 校验；返回错误清单（空 = 通过）。"""
    errs: list[str] = []
    if not isinstance(data, dict):
        return ["输出顶层须为 JSON 对象"]
    if data.get("chunk") != chunk_id:
        errs.append(f"chunk 须严格等于 {chunk_id}，实际 {data.get('chunk')!r}")

    events = data.get("events")
    if not isinstance(events, list):
        errs.append("events 须为数组")
    else:
        if len(events) == 0:
            errs.append("events 长度须 ∈ [1,5]，实际 0（③ 不可救）")
        elif len(events) > _MAX_EVENTS:
            errs.append(f"events 长度须 ∈ [1,5]，实际 {len(events)}（② 档收敛失败）")
        for i, ev in enumerate(events):
            if not isinstance(ev, dict):
                errs.append(f"events[{i}] 须为对象")
                continue
            for key in ("event", "impact"):
                v = ev.get(key)
                if not isinstance(v, str) or not v.strip():
                    errs.append(f"events[{i}].{key} 须非空字符串")
                elif "|" in v:
                    errs.append(f"events[{i}].{key} 含半角 |（① 档应已替换，净化后仍禁则）")
                elif count_chars(v) > _MAX_CELL:
                    errs.append(f"events[{i}].{key} 超 {_MAX_CELL} 字（② 档收敛失败）")

    notes = data.get("notes")
    if not isinstance(notes, dict):
        errs.append("notes 须为对象")
    else:
        c = notes.get("characters")
        if not isinstance(c, str) or not c.strip():
            errs.append("notes.characters 须非空字符串")
        elif count_chars(c) > _MAX_CHARACTERS:
            errs.append(f"notes.characters 超 {_MAX_CHARACTERS} 字（② 档收敛失败，K29）")
        plots = notes.get("plots")
        if not isinstance(plots, list) or not plots:
            errs.append("notes.plots 须为非空数组（长度 ≥1）")
        else:
            for i, p in enumerate(plots):
                if not isinstance(p, str) or not p.strip():
                    errs.append(f"notes.plots[{i}] 须非空字符串")
                elif count_chars(p) > _MAX_CELL:
                    errs.append(f"notes.plots[{i}] 超 {_MAX_CELL} 字（② 档收敛失败）")
        q = notes.get("questions")
        if not isinstance(q, list):
            errs.append("notes.questions 须为数组")
        elif len(q) > _MAX_QUESTIONS:
            errs.append(f"notes.questions 长度须 ≤{_MAX_QUESTIONS}（② 档收敛失败）")
        a = notes.get("abstract")
        if not isinstance(a, str) or not a.strip():
            errs.append("notes.abstract 须非空字符串")
        elif count_chars(a) > _MAX_ABSTRACT:
            errs.append(f"notes.abstract 超 {_MAX_ABSTRACT} 字（② 档收敛失败）")
    return errs


def validate_summary_text(text: object, max_chars: int) -> list[str]:
    """§9.4 summary 校验：单段（内部无 `\\n`、末尾至多一 `\\n`）；≤ max_chars。"""
    errs: list[str] = []
    if not isinstance(text, str):
        return ["summary 须为字符串"]
    body = text.rstrip("\n")
    if "\n" in body:
        errs.append("summary 须单段（内部无换行）")
    if text.count("\n") > 1:
        errs.append("summary 末尾至多一个换行")
    n = count_chars(text)
    if n > max_chars:
        errs.append(f"summary 字数 {n} > summary_max {max_chars}")
    return errs


def validate_notes_file(path: Path, *, start: int, end: int,
                        padding: int) -> tuple[list[str], list[str]]:
    """§9.5 notes 校验：文件名/H1/标题按序（失败）；范围一致（仅警告，自愈路径）。

    返回 (错误清单, 警告清单)；错误非空 → 退出码 3（验收失败）。
    """
    errs: list[str] = []
    warns: list[str] = []
    if not path.exists():
        return [f"notes 缺失: {path.name}"], []
    m = _NOTES_NAME_RE.match(path.name)
    if not m:
        errs.append(f"notes 文件名须 part_XXX~part_YYY.md 且无空格: {path.name!r}")
    else:
        fs, fe = int(m.group(1)), int(m.group(2))
        if (fs, fe) != (start, end):
            warns.append(f"notes 范围 {fs}~{fe} 与本批 [{start},{end}] 不一致"
                         "（自愈路径失败仅警告）")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [f"notes 读取失败: {exc}"], []

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    h1s = [ln for ln in lines if ln.startswith("# ")]
    if len(h1s) != 1:
        errs.append(f"notes 须恰一个 H1，实际 {len(h1s)}")
    else:
        h1_body = h1s[0][2:].strip()
        if " " in h1_body:
            errs.append(f"notes H1 无空格（B11/D13）: {h1_body!r}")

    h2s = [ln for ln in lines if ln.startswith("## ")]
    idx = 0
    for want in _TITLES:
        found = False
        while idx < len(h2s):
            if want in h2s[idx]:
                found = True
                idx += 1
                break
            idx += 1
        if not found:
            errs.append(f"缺二级标题「{want}」（B7 须按序含 "
                        "阅读范围/角色发展/关键情节/疑问/摘要）")
    return errs, warns
