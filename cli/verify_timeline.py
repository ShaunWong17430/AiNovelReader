"""cli/verify_timeline.py — 薄封装：时间线校验（§9.2 check-only 三态 / §9.3 / K40）。

用法：python cli\\verify_timeline.py --novel <名> [--batch <id>] [--check-only]
- --check-only：三态 APPLIED/NOT_FOUND/PARTIAL（退出码 0）+ 健全性
  G12–G14 + K40（失败退出码 3）；退出码 0 时 stdout 仅一行三态（I3）。
- 批模式：§9.3 硬校验总表（C12/A4/K5/C1/覆盖/格式），失败退出码 3。
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
    p = argparse.ArgumentParser(description="时间线校验（§9.2 / §9.3）")
    p.add_argument("--novel", required=True)
    p.add_argument("--batch", type=int)
    p.add_argument("--check-only", action="store_true")
    a = p.parse_args(argv)

    base = paths.base_dir()
    root = None
    try:
        root = paths.root_of(base, a.novel)
        meta_path = root / "metadata.json"
        doc = statemod.read_metadata(meta_path, base=base)
        start = doc.processed_chunks + 1
        end = min(doc.total_chunks, start + doc.batch_size - 1)
        tl = root / "plot_timeline.md"
        if a.check_only:
            state, errs = timelinemod.check_only(
                tl, start=start, end=end, total_chunks=doc.total_chunks,
                chunk_padding=doc.chunk_padding,
                processed_chunks=doc.processed_chunks,
                chunks_dir=root / "chunks")
            for e in errs:
                print(f"ERROR={e}")
            if errs:
                return 3                        # I3：退出码优先于 stdout
            print(state)                        # 仅一行 APPLIED/NOT_FOUND/PARTIAL
            return 0
        batch_id = a.batch if a.batch is not None else doc.current_batch + 1
        errs, warns = timelinemod.verify_batch(
            root, batch_id=batch_id, current_batch=doc.current_batch,
            chunk_padding=doc.chunk_padding, start=start, end=end)
        for w in warns:
            print(f"WARN={w}")
        for e in errs:
            print(f"ERROR={e}")
        if errs:
            return 3
        print(f"STATUS=OK timeline 校验通过 batch={batch_id}")
        return 0
    except Exception as exc:
        return map_error(exc,
                         meta_path=(root / "metadata.json") if root else None,
                         base=base)


if __name__ == "__main__":
    sys.exit(main())
