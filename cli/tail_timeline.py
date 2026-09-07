"""cli/tail_timeline.py — 薄封装：时间线窗口截取（§1.5，K33 钳制 + 空范围退出码 0）。

用法：python cli\\tail_timeline.py --novel <名> (--chunk-start N --chunk-end M | --lines N)
- K33：--chunk-start <1 钳制为 1；钳制后 start>end 空范围 → 空输出 + 退出码 0；
- stdout 为事件行原文（供 reader/summarizer prompt 输入，§4.2/§6.3）。
纪律（§0.3）：≤120 行；只做「解析 argv → 调 core → 打印 → 退出码」。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from _common import map_error

import core.paths as paths
import core.timeline as timelinemod


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="时间线窗口截取（K33）")
    p.add_argument("--novel", required=True)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--chunk-start", type=int)
    g.add_argument("--lines", type=int)
    p.add_argument("--chunk-end", type=int)
    a = p.parse_args(argv)
    if a.chunk_start is not None and a.chunk_end is None:
        print("ERROR=--chunk-start 需配合 --chunk-end")
        return 2

    base = paths.base_dir()
    root = None
    try:
        root = paths.root_of(base, a.novel)
        tl = root / "plot_timeline.md"
        if a.lines is not None:
            rows = timelinemod.tail_lines(tl, a.lines)
        else:
            rows = timelinemod.tail(tl, a.chunk_start, a.chunk_end)
        for row in rows:
            print(row)
        return 0
    except Exception as exc:
        return map_error(exc,
                         meta_path=(root / "metadata.json") if root else None,
                         base=base)


if __name__ == "__main__":
    sys.exit(main())
