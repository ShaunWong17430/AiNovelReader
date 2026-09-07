"""cli/backup.py — 薄封装：批快照（§1.5，K23 三态）。

用法：python cli\\backup.py --novel <名> --batch <id> --phase read|summarize
      [--force]
- 不存在→写(0)；存在且完好→幂等覆写(0)；损坏→2（需 --force，K23）。
- K25：cli 写命令复用 core/lock.py 同一把锁。
纪律（§0.3）：≤120 行；只做「解析 argv → 调 core → 打印 KEY=VALUE → 退出码」。
"""
from __future__ import annotations

import argparse
import sys

from _common import map_error, warn_stderr

import core.lock as lockmod
import core.paths as paths
import core.recovery as recovery


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="批快照（K23 三态）")
    p.add_argument("--novel", required=True)
    p.add_argument("--batch", type=int, required=True)
    p.add_argument("--phase", choices=("read", "summarize"), required=True)
    p.add_argument("--force", action="store_true")
    a = p.parse_args(argv)

    base = paths.base_dir()
    try:
        novel = paths.validate_novel_name(a.novel)   # 锁路径安全（InvalidName → 2）
        lock = lockmod.acquire(base, novel, warn_sink=warn_stderr)
    except lockmod.LockError as exc:
        return map_error(exc)
    except paths.InvalidName as exc:
        return map_error(exc)
    try:
        res = recovery.backup(base, a.novel, batch_id=a.batch,
                              phase=a.phase, force=a.force)
    except recovery.BackupError as exc:
        return map_error(exc)
    finally:
        lock.release()
    print(f"STATUS=OK batch={res['batch_id']} phase={res['phase']} "
          f"state={res['status']} snapshot={res['snapshot']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
