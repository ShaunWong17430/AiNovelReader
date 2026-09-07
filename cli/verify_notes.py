"""cli/verify_notes.py — 薄封装：笔记验收（§6.2 步骤 6c / §9.5）。

用法：python cli\\verify_notes.py --novel <名> --start <N> --end <M>
- 校验 notes\\part_XXX~part_YYY.md：文件名/H1 无空格（B11/D13）、
  5 个二级标题按序出现（B7 关键词匹配）、章节范围与文件名一致
  （自愈路径失败仅警告）；
- 失败 → 3（验收失败，计入 batch_retries 由驱动器处置）；通过 → VERIFY=OK。
纪律（§0.3）：≤120 行；只读命令不加锁（K25）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from _common import map_error, warn_stderr

import core.paths as paths
import core.state as statemod
import core.validator as validatormod


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="笔记验收（§9.5）")
    p.add_argument("--novel", required=True)
    p.add_argument("--start", type=int, required=True)
    p.add_argument("--end", type=int, required=True)
    a = p.parse_args(argv)

    base = paths.base_dir()
    root = None
    try:
        novel = paths.validate_novel_name(a.novel)
        root = paths.root_of(base, novel)
        meta = statemod.read_metadata(root / "metadata.json", base=base)
        target = root / "notes" / \
            f"part_{a.start:0{meta.chunk_padding}d}~part_{a.end:0{meta.chunk_padding}d}.md"
        errs, warns = validatormod.validate_notes_file(
            target, start=a.start, end=a.end, padding=meta.chunk_padding)
    except Exception as exc:
        return map_error(exc, meta_path=(root / "metadata.json") if root else None,
                         base=base)
    for w in warns:
        warn_stderr(w)
    if errs:
        print(f"ERROR=notes 校验失败: {'; '.join(errs)}")
        return 3
    print("VERIFY=OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
