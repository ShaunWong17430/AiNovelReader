"""cli/render_notes.py — 薄封装：渲染读书笔记（§5，K15 降级）。

用法：python cli\\render_notes.py --novel <名> --start <N> --end <M>
- 读本批 .batch\\part_XXX..YYY.json → 多片合并 → 原子写
  notes\\part_XXX~part_YYY.md（§11 不变量 6/7）；
- K15 降级：缺片 → 警告 + 用已有片渲染；无任何片 → 3（验收失败）；
  chunks 字数统计失败 → 字数「未知」+ 警告，不阻塞。
纪律（§0.3）：≤120 行；写命令复用同锁（K25）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from _common import map_error, warn_stderr

import core.lock as lockmod
import core.paths as paths
import core.renderer as rendermod
import core.state as statemod


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="渲染读书笔记（§5）")
    p.add_argument("--novel", required=True)
    p.add_argument("--start", type=int, required=True)
    p.add_argument("--end", type=int, required=True)
    a = p.parse_args(argv)

    base = paths.base_dir()
    root = None
    lock = None
    try:
        novel = paths.validate_novel_name(a.novel)
        root = paths.root_of(base, novel)
        lock = lockmod.acquire(base, novel, warn_sink=warn_stderr)
        meta = statemod.read_metadata(root / "metadata.json", base=base)
        res = rendermod.render_notes_file(root, start=a.start, end=a.end,
                                          padding=meta.chunk_padding)
    except Exception as exc:
        return map_error(exc, meta_path=(root / "metadata.json") if root else None,
                         base=base)
    finally:
        if lock is not None:
            lock.release()
    for w in res["warnings"]:
        warn_stderr(w)
    print(f"NOTES={Path(res['path']).name} chunks={res['chunks']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
