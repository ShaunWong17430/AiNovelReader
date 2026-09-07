"""core/sanitize.py — 输出净化三档（§7.4 / K29）。

v3.0 三档：
- ① 自动净化：`event`/`impact`/笔记字段含首尾空白、内部换行、半角 `|`、
  `\\ufeff`、孤立 `\\r` → trim + 折叠 + `|`→`｜` + 剔除；log WARN，不算失败；
- ② 自动收敛：`notes.questions`>3、`notes.abstract`>300、`events`>5（K29）、
  `notes.characters`>500（K29）、`events[].event/impact`>100、`plots` 每条>100
  → 截断到上限 + 警告，不算失败；
- ③ 不可救（`chunk` 不匹配、`events` 为空、必填缺失/类型错、净化后仍禁则、
  JSON 三级解析全失败）由 `core/validator.py` 判定（chat 内先净化后校验）。

字数口径一律 §1.6（`core/timeline.count_chars`：去除空白后的 Unicode 字符数）。
"""
from __future__ import annotations

from .timeline import count_chars

# ② 档上限（§4.4 / K29）
_MAX_CHARACTERS = 500          # notes.characters
_MAX_ABSTRACT = 300            # notes.abstract
_MAX_EVENTS = 5                # events 数组长度（K29 收敛）
_MAX_CELL = 100                # event / impact / plots 每条
_MAX_QUESTIONS = 3             # notes.questions 条数


class SanitizeError(Exception):
    """③ 不可救标记（供 chat 内部区分「收敛失败」与「结构不可救」）。"""


def clean_text(value: object) -> str:
    """① 档净化：剔除 `\\ufeff`/孤立 `\\r`，任意空白折叠为单空格，半角 `|` → 全角 `｜`，trim。"""
    s = str(value)
    s = s.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", " ")
    s = " ".join(s.split())
    return s.strip().replace("|", "｜")


def truncate_chars(s: str, n: int) -> str:
    """截断到 ≤ n 个非空白字符（§1.6 口径）；保留既有空白结构，尾部 trim。"""
    cnt = 0
    for i, ch in enumerate(s):
        if not ch.isspace():
            cnt += 1
            if cnt > n:
                return s[:i].rstrip()
    return s


def _clean_field(name: str, value: object, warns: list[str]) -> str:
    """净化单个字符串字段；发生任何替换 → 记 ① 档警告。"""
    if not isinstance(value, str):
        return str(value)
    cleaned = clean_text(value)
    if cleaned != value:
        warns.append(f"{name} 已净化（① 档：折叠空白/剔除 BOM/半角 |→｜）")
    return cleaned


def _cap_field(name: str, value: str, cap: int, warns: list[str]) -> str:
    """② 档收敛：超出 cap（§1.6 字数）→ 截断 + 警告。"""
    if count_chars(value) > cap:
        warns.append(f"{name} 超 {cap} 字（② 档收敛截断，K29）")
        return truncate_chars(value, cap)
    return value


def sanitize_reader_output(data: dict, *, chunk_id: str,
                           warn_sink=None) -> tuple[dict, list[str]]:
    """① 净化 + ② 收敛；返回 (净化后 dict, 警告列表)。

    仅处理结构合法的部分；结构性问题（events 非数组 / notes 非对象等）
    原样保留，由 validator 判 ③ 不可救。
    """
    warns: list[str] = []
    out = dict(data)

    events = out.get("events")
    if isinstance(events, list):
        cleaned_events: list[object] = []
        for i, ev in enumerate(events):
            if not isinstance(ev, dict):
                cleaned_events.append(ev)
                continue
            e2 = dict(ev)
            for key in ("event", "impact"):
                if key in e2:
                    e2[key] = _clean_field(f"events[{i}].{key}", e2[key], warns)
            cleaned_events.append(e2)
        out["events"] = cleaned_events

    notes = out.get("notes")
    if isinstance(notes, dict):
        n2 = dict(notes)
        for key in ("characters", "abstract"):
            if key in n2:
                n2[key] = _clean_field(f"notes.{key}", n2[key], warns)
        for key in ("plots", "questions"):
            lst = n2.get(key)
            if isinstance(lst, list):
                n2[key] = [_clean_field(f"notes.{key}[{i}]", x, warns)
                           for i, x in enumerate(lst)]
        out["notes"] = n2

    # --- ② 收敛（仅对净化后字符串生效） ---
    if isinstance(out.get("events"), list):
        evs = [e for e in out["events"] if isinstance(e, dict)]
        if len(evs) > _MAX_EVENTS:
            warns.append(f"events 共 {len(evs)} 条 > {_MAX_EVENTS}（② 档收敛截断前"
                         f" {_MAX_EVENTS} 条，K29）")
            out["events"] = out["events"][:_MAX_EVENTS]
        for i, ev in enumerate(out["events"]):
            if not isinstance(ev, dict):
                continue
            for key in ("event", "impact"):
                if isinstance(ev.get(key), str):
                    ev[key] = _cap_field(f"events[{i}].{key}", ev[key],
                                         _MAX_CELL, warns)
    if isinstance(out.get("notes"), dict):
        n2 = out["notes"]
        for key, cap in (("characters", _MAX_CHARACTERS),
                         ("abstract", _MAX_ABSTRACT)):
            if isinstance(n2.get(key), str):
                n2[key] = _cap_field(f"notes.{key}", n2[key], cap, warns)
        if isinstance(n2.get("plots"), list):
            n2["plots"] = [_cap_field(f"notes.plots[{i}]", x, _MAX_CELL, warns)
                           if isinstance(x, str) else x
                           for i, x in enumerate(n2["plots"])]
        if isinstance(n2.get("questions"), list):
            if len(n2["questions"]) > _MAX_QUESTIONS:
                warns.append(f"notes.questions 共 {len(n2['questions'])} 条 > "
                             f"{_MAX_QUESTIONS}（② 档收敛截断前 3）")
                n2["questions"] = n2["questions"][:_MAX_QUESTIONS]

    if warn_sink is not None:
        for w in warns:
            warn_sink(w)
    return out, warns
