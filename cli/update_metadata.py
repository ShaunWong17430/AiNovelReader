"""cli/update_metadata.py — 薄封装：metadata 推进（§1.3，B6 状态机 / J6 / K1 / K36）。

用法：python cli\\update_metadata.py --novel <名>
      [--processed N] [--summary N] [--status S] [--error MSG]
      [--retries N] [--summary-retries N]
- 业务规则唯一实现于 core/state.update_metadata（严格单调、单批增量、
  白名单 B6、K13 占位态、K36 no-op、done 终态拒一切）；
- 打印推进后关键字段 KEY=VALUE；退出码 2（参数）/3（状态冲突）。
纪律（§0.3）：≤120 行；写命令复用同锁（K25）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from _common import map_error, warn_stderr

import core.lock as lockmod
import core.paths as paths
import core.state as statemod


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="metadata 推进（§1.3）")
    p.add_argument("--novel", required=True)
    p.add_argument("--processed", type=int)
    p.add_argument("--summary", type=int)
    p.add_argument("--status")
    p.add_argument("--error")
    p.add_argument("--retries", type=int)
    p.add_argument("--summary-retries", type=int)
    a = p.parse_args(argv)

    base = paths.base_dir()
    root = None
    lock = None
    try:
        novel = paths.validate_novel_name(a.novel)
        root = paths.root_of(base, novel)
        lock = lockmod.acquire(base, novel, warn_sink=warn_stderr)
        doc = statemod.update_metadata(
            root / "metadata.json", base=base,
            processed=a.processed, summary=a.summary, status=a.status,
            error=a.error, retries=a.retries, summary_retries=a.summary_retries)
    except Exception as exc:
        return map_error(exc, meta_path=(root / "metadata.json") if root else None,
                         base=base)
    finally:
        if lock is not None:
            lock.release()
    print(f"STATUS={doc.status} PROCESSED={doc.processed_chunks} "
          f"SUMMARY={doc.summary_chunks} CURRENT_BATCH={doc.current_batch} "
          f"BATCH_RETRIES={doc.batch_retries} SUMMARY_RETRIES={doc.summary_retries}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
