"""cli/append_timeline.py — 薄封装：时间线原子追加（§1.4，J3 禁重试）。

用法：python cli\\append_timeline.py --novel <名> [--batch <id>]
- 本批范围由 processed_chunks 推导（§1.2，J1）；batch 缺省 = current_batch+1；
- C12（batch == current_batch+1）、A4（快照行数）、K5（覆盖全）、
  J3（时间线处于快照状态）在 core 内校验；失败退出码 3（禁自动重试）。
- K25：cli 写命令复用 core/lock.py 同一把锁。
纪律（§0.3）：≤120 行；只做「解析 argv → 调 core → 打印 KEY=VALUE → 退出码」。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from _common import map_error, warn_stderr

import core.lock as lockmod
import core.paths as paths
import core.state as statemod
import core.timeline as timelinemod


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="时间线原子追加（§1.4）")
    p.add_argument("--novel", required=True)
    p.add_argument("--batch", type=int)
    a = p.parse_args(argv)

    base = paths.base_dir()
    root = None
    try:
        novel = paths.validate_novel_name(a.novel)   # 锁路径安全（InvalidName → 2）
        lock = lockmod.acquire(base, novel, warn_sink=warn_stderr)
    except lockmod.LockError as exc:
        return map_error(exc)
    except paths.InvalidName as exc:
        return map_error(exc)
    try:
        root = paths.root_of(base, a.novel)
        meta_path = root / "metadata.json"
        doc = statemod.read_metadata(meta_path, base=base)
        start = doc.processed_chunks + 1
        end = min(doc.total_chunks, start + doc.batch_size - 1)
        if start > end:
            raise timelinemod.TimelineError(
                f"本批范围为空（processed={doc.processed_chunks} ≥ total={doc.total_chunks}）")
        batch_id = a.batch if a.batch is not None else doc.current_batch + 1
        res = timelinemod.append_batch(
            root, batch_id=batch_id, current_batch=doc.current_batch,
            chunk_padding=doc.chunk_padding, start=start, end=end)
    except Exception as exc:
        return map_error(exc,
                         meta_path=(root / "metadata.json") if root else None,
                         base=base)
    finally:
        lock.release()
    print(f"STATUS=OK batch={batch_id} appended={res['appended']} "
          f"seq_end={res['seq_end']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
