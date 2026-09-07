"""cli/chunk_stats.py — 薄封装：分片字数统计（§1.6 口径，K15 降级）。

用法：python cli\\chunk_stats.py --novel <名> [--chunk-start N] [--chunk-end M]
- 字数 = 去除空白后的 Unicode 字符数（§1.6，core/timeline.count_chars 唯一实现）；
- 缺省范围 = 全部已 init 分片；失败退出码非 0（renderer 据此降级填「未知」，K15）。
纪律（§0.3）：≤120 行；只做「解析 argv → 调 core → 打印 KEY=VALUE → 退出码」。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from _common import map_error

import core.paths as paths
import core.state as statemod
import core.timeline as timelinemod


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="分片字数统计（§1.6）")
    p.add_argument("--novel", required=True)
    p.add_argument("--chunk-start", type=int)
    p.add_argument("--chunk-end", type=int)
    a = p.parse_args(argv)

    base = paths.base_dir()
    root = None
    try:
        root = paths.root_of(base, a.novel)
        meta_path = root / "metadata.json"
        doc = statemod.read_metadata(meta_path, base=base)
        start = a.chunk_start if a.chunk_start is not None else 1
        end = a.chunk_end if a.chunk_end is not None else doc.total_chunks
        if not (1 <= start <= end <= doc.total_chunks):
            raise statemod.StateError(
                f"范围非法: [{start},{end}] 须 ⊆ [1,{doc.total_chunks}]")
        total = 0
        for num in range(start, end + 1):
            f = root / "chunks" / f"part_{num:0{doc.chunk_padding}d}.txt"
            if not f.exists():
                raise statemod.StateError(f"分片缺失: {f.name}")
            chars = timelinemod.count_chars(f.read_text(encoding="utf-8"))
            total += chars
            print(f"part_{num:0{doc.chunk_padding}d}={chars}")
        print(f"STATUS=OK total_chars={total}")
        return 0
    except Exception as exc:
        return map_error(exc,
                         meta_path=(root / "metadata.json") if root else None,
                         base=base)


if __name__ == "__main__":
    sys.exit(main())
