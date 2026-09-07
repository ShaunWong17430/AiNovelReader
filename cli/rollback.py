"""cli/rollback.py — 薄封装：回滚（read/summarize 两相 + --purge，K22 / K24 / K32）。

用法：python cli\\rollback.py --novel <名> --phase read|summarize
      python cli\\rollback.py --novel <名> --purge
- read 相恢复 plot_timeline.md；summarize 相恢复 summary.md；
- 快照只恢复不删（仅 --purge 清理）；rollback 不删 .batch\\（K22）；
- 失败退出码 3（K24：调用方零容忍，禁止自动重试）；
- --purge 失败仅 WARN、退出码 0（K32：done 终态保持，不适用 K24）；
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
import core.recovery as recovery


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="回滚 / 快照清理（K22/K24/K32）")
    p.add_argument("--novel", required=True)
    p.add_argument("--phase", choices=("read", "summarize"))
    p.add_argument("--purge", action="store_true")
    a = p.parse_args(argv)
    if a.purge and a.phase is not None:
        print("ERROR=--purge 与 --phase 互斥")
        return 2
    if not a.purge and a.phase is None:
        print("ERROR=须提供 --phase read|summarize 或 --purge")
        return 2

    base = paths.base_dir()
    try:
        novel = paths.validate_novel_name(a.novel)   # 锁路径安全（InvalidName → 2）
        lock = lockmod.acquire(base, novel, warn_sink=warn_stderr)
    except lockmod.LockError as exc:
        return map_error(exc)
    except paths.InvalidName as exc:
        return map_error(exc)
    try:
        root = paths.root_of(base, a.novel)
        if a.purge:
            recovery.purge_rollback(root)
            print("STATUS=OK purge 完成（失败仅 WARN，K32）")
            return 0
        res = recovery.rollback(base, a.novel, phase=a.phase)
    except recovery.RollbackError as exc:
        return map_error(exc)
    finally:
        lock.release()
    print(f"STATUS=OK phase={res['phase']} batch={res['batch_id']} "
          f"restored={res['snapshot']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
