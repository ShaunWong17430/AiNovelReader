"""core/renderer.py — notes.md Python 渲染（§5 / B7 / K15）。

- 模板五段（§5）：H1 + 阅读范围/角色发展/关键情节/疑问/摘要（B7 标题关键词恒在）；
- 多片合并：`characters` 空行拼接；`plots`/`questions` 展平（questions 全局截断
  前 3）；`abstract` 空格拼接（可达 batch_size×300，不在渲染二次截断）；
- 「约 XXXX 字」由 §1.6 实算（chunks\\part_*.txt 去空白字符数）；
- K15 降级：chunks 字数统计失败 → 字数「未知」+ 警告，不阻塞；
  部分片缺失 → 警告 + 用已有片渲染；完全无可用片 → RenderError（验收失败转
  §6.2 步骤 7）；
- 写入原子（§11 不变量 6/7：临时文件 + os.replace，notes 只由本模块创建/覆写）。
"""
from __future__ import annotations

import json
from pathlib import Path

from .state import atomic_write_text
from .timeline import count_chars


class RenderError(Exception):
    """notes 完全无法生成 → 验收失败（§6.2 步骤 6b → 步骤 7）。"""


def merge_notes(batch: list[dict]) -> dict:
    """§5 多片合并规则。batch 元素为 .batch\\*.json 完整 reader 输出（§4.4）。"""
    def notes_of(n: dict) -> dict:
        nd = n.get("notes")
        return nd if isinstance(nd, dict) else {}

    characters = "\n\n".join(
        str(notes_of(n).get("characters", "")).strip() for n in batch
        if str(notes_of(n).get("characters", "")).strip())
    plots = [str(p).strip() for n in batch
             for p in (notes_of(n).get("plots") or [])
             if p and str(p).strip()]
    questions = [str(q).strip() for n in batch
                 for q in (notes_of(n).get("questions") or [])
                 if q and str(q).strip()][:3]
    abstract = " ".join(
        str(notes_of(n).get("abstract", "")).strip() for n in batch
        if str(notes_of(n).get("abstract", "")).strip())
    return {"characters": characters, "plots": plots,
            "questions": questions, "abstract": abstract}


def render_notes_md(*, start: int, end: int, padding: int, merged: dict,
                    char_count: int | None) -> str:
    """§5 模板五段渲染。char_count=None → 字数「未知」（K15 降级）。"""
    def pad(n: int) -> str:
        return f"part_{n:0{padding}d}"

    lines = [
        f"# 读书笔记：{pad(start)}~{pad(end)}",
        "## 📖 阅读范围",
        f"- 分片：{pad(start)}.txt~{pad(end)}.txt",
        (f"- 字数：约 {char_count} 字" if char_count is not None
         else "- 字数：未知（chunk_stats 降级，K15）"),
        "## 👥 角色发展（基于全局状态 + 新内容）",
        merged["characters"],
        "## 📍 关键情节（每个情节梗概并控制在 100 字以内）",
    ]
    lines += [f"- {p}" for p in merged["plots"]]
    lines += ["## ❓ 疑问（控制在 3 个问题以内）"]
    lines += [f"- {q}" for q in merged["questions"]]
    lines += ["## 📝 摘要（≤300 字，单片口径）", merged["abstract"]]
    return "\n".join(lines) + "\n"


def chunk_char_count(root: Path, start: int, end: int,
                     padding: int) -> tuple[int | None, list[str]]:
    """§1.6 实算 chunks 字数；任一片读取失败 → (None, 警告)（K15「未知」）。"""
    total = 0
    for num in range(start, end + 1):
        f = root / "chunks" / f"part_{num:0{padding}d}.txt"
        try:
            total += count_chars(f.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            return None, [f"chunks/{f.name} 字数统计失败（K15 降级「未知」）: {exc}"]
    return total, []


def render_notes_file(root: Path, *, start: int, end: int,
                      padding: int) -> dict:
    """读 .batch\\part_XXX..YYY.json → 合并渲染 → 原子写 notes\\part_XXX~part_YYY.md。

    返回 {"path": str, "warnings": list, "chunks": int}。
    """
    batch: list[dict] = []
    warns: list[str] = []
    missing: list[str] = []
    for num in range(start, end + 1):
        f = root / ".batch" / f"part_{num:0{padding}d}.json"
        if not f.exists():
            missing.append(f.name)
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            warns.append(f".batch/{f.name} 读取失败，跳过: {exc}")
            continue
        batch.append(data)
    if missing:
        warns.append(f".batch 缺 {len(missing)} 片: {missing}（K15 部分渲染）")
    if not batch:
        raise RenderError("本批 .batch 无任何可用片，notes 完全无法生成")
    merged = merge_notes(batch)
    char_count, cwarns = chunk_char_count(root, start, end, padding)
    warns += cwarns
    text = render_notes_md(start=start, end=end, padding=padding,
                           merged=merged, char_count=char_count)
    notes_dir = root / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    target = notes_dir / f"part_{start:0{padding}d}~part_{end:0{padding}d}.md"
    atomic_write_text(target, text)                 # §11 不变量 6/7
    return {"path": str(target), "warnings": warns, "chunks": len(batch)}
