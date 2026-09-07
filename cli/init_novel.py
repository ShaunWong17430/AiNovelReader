"""cli/init_novel.py — 薄封装：初始化小说（§6.4 / §6.0 init，K2/K19/K37，I4 幂等）。

用法：python cli\\init_novel.py --novel <名> [--recover] [--batch-size N]
      [--timeline-window N] [--summary-max N] [--chunk-max-chars N]
      [--summary-chunks N] [--current-batch N]
纪律（§0.3）：≤120 行；只做「解析 argv → 调 core → 打印 KEY=VALUE → 退出码」。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from _common import map_error, warn_stderr

import core.config as cfgmod
import core.lock as lockmod
import core.logger as logmod
import core.paths as paths
import core.recovery as recovery

_DEFAULTS = {"batch_size": 5, "timeline_window": 10,
             "summary_max": 5000, "chunk_max_chars": 20000}   # K42/§1.3 示例


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="初始化小说（§6.4）")
    p.add_argument("--novel", required=True)
    p.add_argument("--recover", action="store_true")
    p.add_argument("--batch-size", type=int)
    p.add_argument("--timeline-window", type=int)
    p.add_argument("--summary-max", type=int)
    p.add_argument("--chunk-max-chars", type=int)
    p.add_argument("--summary-chunks", type=int)
    p.add_argument("--current-batch", type=int)
    a = p.parse_args(argv)

    base = paths.base_dir()
    try:
        cfg = cfgmod.load_config()
        novel = paths.validate_novel_name(a.novel)   # 锁路径安全（InvalidName → 2）
        lock = lockmod.acquire(base, novel, warn_sink=warn_stderr)
    except cfgmod.ConfigError as exc:
        return map_error(exc)
    except paths.InvalidName as exc:
        return map_error(exc)
    except lockmod.LockError as exc:
        return map_error(exc)
    try:
        if a.recover:
            res = recovery.recover(
                base, a.novel, cfg,
                batch_size=a.batch_size, timeline_window=a.timeline_window,
                summary_max=a.summary_max, chunk_max_chars=a.chunk_max_chars,
                summary_chunks=a.summary_chunks, current_batch=a.current_batch)
        else:
            res = recovery.init_novel(
                base, a.novel, cfg,
                batch_size=_DEFAULTS["batch_size"] if a.batch_size is None else a.batch_size,
                timeline_window=_DEFAULTS["timeline_window"] if a.timeline_window is None else a.timeline_window,
                summary_max=_DEFAULTS["summary_max"] if a.summary_max is None else a.summary_max,
                chunk_max_chars=_DEFAULTS["chunk_max_chars"] if a.chunk_max_chars is None else a.chunk_max_chars)
    except recovery.InitError as exc:
        return map_error(exc)
    finally:
        lock.release()

    logmod.Logger(Path(res["root_dir"]) / "run.log").ok(
        "driver", "recover" if a.recover else "init", None,
        f"novel={res['novel']} total={res['total_chunks']} "
        f"processed={res['processed_chunks']} current_batch={res['current_batch']}")
    print(f"STATUS=OK novel={res['novel']} total={res['total_chunks']} "
          f"processed={res['processed_chunks']} summary={res['summary_chunks']} "
          f"current_batch={res['current_batch']} root={res['root_dir']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
