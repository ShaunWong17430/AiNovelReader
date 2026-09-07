"""run.py — 唯一主入口（§6.0 主循环 a–k，K6 / K25 / K26 / K31 / K42）。

子命令（§6.0 语法）：
- run      --novel <名> [--once] [--max-rounds N] [--dry-run]   # 常驻/单步/预演
- init     --novel <名> [--recover] [--batch-size N] ...         # 与 cli/init_novel 同 core
- status   --novel <名>                                          # 不加锁（K25）
- validate --config                                              # 不加锁（K25）

铁律（§0.3）：直接 import core，不子进程调 cli\\*。
- K6 --dry-run：加锁前、不写任何文件（除锁外零写入）、不调 LLM、不改 metadata；
- K25 锁范围：run/init 加写锁；status/validate/dry-run 不加锁；
- K26 优雅中断：SIGINT/SIGBREAK/CTRL_CLOSE_EVENT → stop_flag（当前片完成后退出）；
- K31：metadata 缺失 → 自动 init（绝不 emergency）；K42：自动 init 用定值默认参数；
- §8.4 处置：ProcessError（0=FATAL/2=环境/3=校验）→ 置 error 退出 3；
  未知异常 → 当轮重试 1 次（C2/A5）→ 仍失败 → 置 error；
  purge 失败仅 WARN 保持 done（K32）。
"""
from __future__ import annotations

import argparse
import ctypes
import os
import re
import signal
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import core.lock as lockmod
import core.logger as logmod
import core.paths as paths
import core.reader as reader
import core.recovery as recovery
import core.state as statemod
import core.summarizer as summarizer
from core.config import Config, ensure_api_key, load_config

_ENTRY_RE = re.compile(r"processed=(\d+)")

# K26：SetConsoleCtrlHandler 注册的 ctypes 回调必须模块级保活——回调对象一旦
# 被 GC，OS 仍持有其函数指针，下次控制事件（Ctrl+C / 关窗 / 登出）直接
# 0xC0000005 原生崩溃（无 traceback、无日志、残留锁）。实测崩溃即由此引发。
_CTRL_HANDLERS: list[object] = []


class _StopFlag:
    """K26 stop_flag：信号处理器置位；片间/退避检查。"""

    def __init__(self):
        self._ev = threading.Event()

    def set(self):
        self._ev.set()

    def __call__(self) -> bool:
        return self._ev.is_set()


def install_interrupt_handlers(flag: _StopFlag) -> None:
    """K26：SIGINT/SIGBREAK + CTRL_CLOSE_EVENT（ctypes，零新依赖）。

    回调存活铁律：`proto(lambda ...)` 生成的 ctypes 回调对象是 Python 对象，
    必须由 _CTRL_HANDLERS 保活到进程退出；否则 OS 控制事件命中悬挂指针 →
    ACCESS_VIOLATION（本模块曾因此崩溃，见 _CTRL_HANDLERS 注释）。
    """
    signal.signal(signal.SIGINT, lambda *_: flag.set())
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, lambda *_: flag.set())
    if os.name == "nt":
        try:
            proto = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)

            def _ctrl_handler(code: int) -> bool:
                """控制事件回调：置 stop_flag（片间/退避检查）并吞掉事件。"""
                try:
                    flag.set()
                except Exception:
                    pass
                return True

            handler = proto(_ctrl_handler)
            ctypes.windll.kernel32.SetConsoleCtrlHandler(handler, True)
            _CTRL_HANDLERS.append(handler)       # 保活关键（防悬挂指针崩溃）
        except Exception:
            pass    # CTRL_CLOSE 退化：残留锁接管路径兜底（风险 R1）


def detect_stall(log_path: Path) -> tuple[bool, int | None]:
    """§6.5 停滞检测：倒序取最近连续 2 条 [driver] [entry] 行；processed 相同 → 停滞。"""
    if not log_path.exists():
        return False, None
    try:
        lines = [ln for ln in log_path.read_text(encoding="utf-8").splitlines()
                 if "[driver] [entry]" in ln and "processed=" in ln]
    except (OSError, UnicodeDecodeError):
        return False, None
    vals: list[int] = []
    for ln in lines[-2:]:
        m = _ENTRY_RE.search(ln)
        if m:
            vals.append(int(m.group(1)))
    if len(vals) == 2 and vals[0] == vals[1]:
        return True, vals[0]
    return False, None


def _report_error(base, meta_path: Path, msg: str,
                  logger: logmod.Logger | None) -> int:
    """置 status=error + 报告 + 退出 3（§10；置 error 异常时保持原状态 WARN）。"""
    try:
        statemod.update_metadata(meta_path, base=base, status="error", error=msg)
    except Exception as exc:
        print(f"ERROR=置 error 失败（保持原状态）: {exc}")
        return 3
    if logger is not None:
        logger.error("driver", "error", None, f"status=error: {msg}")
    print(f"ERROR={msg}")
    return 3


def _run_loop(base, cfg: Config, novel: str, *, once: bool,
              max_rounds: int | None, flag: _StopFlag) -> int:
    """§6.0 主循环步骤 a–k。"""
    root = paths.root_of(base, novel)
    meta_path = root / "metadata.json"
    # echo=True：每条 run.log 同时镜像 stdout（flush），控制台实时可见
    # 批次/片/小结进度——杜绝「跑了半小时命令行一片空白」。
    logger = logmod.Logger(root / "run.log", echo=True)
    rounds = 0
    while True:
        rounds += 1
        # a. 读 metadata（K31：缺失 ≠ 解析失败 → 自动 init）
        try:
            meta = statemod.read_metadata(meta_path, base=base)
        except statemod.MetadataMissing:
            try:
                recovery.init_novel(base, novel, cfg)      # K42 定值默认参数
                logger.ok("driver", "init", None,
                          f"自动 init（K31/K42）novel={novel}")
            except recovery.InitError as exc:
                return _report_error(base, meta_path, f"自动 init 失败: {exc}",
                                     logger)
            meta = statemod.read_metadata(meta_path, base=base)
        except statemod.MetadataCorrupt as exc:
            return _report_error(base, meta_path, f"metadata 损坏: {exc}", logger)
        if rounds == 1:
            print(f"START novel={meta.novel_name} "
                  f"processed={meta.processed_chunks}/{meta.total_chunks} "
                  f"summary={meta.summary_chunks} "
                  f"next_batch={meta.current_batch + 1} "
                  f"status={meta.status}", flush=True)
        # b. 终态
        if meta.status == "done":
            print(f"STATUS=done processed={meta.processed_chunks} "
                  f"summary={meta.summary_chunks} current_batch={meta.current_batch}")
            return 0
        if meta.status == "error":
            print(f"ERROR=status=error: {meta.error}")
            return 3
        # c. init → running
        if meta.status == "init":
            try:
                meta = statemod.update_metadata(meta_path, base=base,
                                                status="running")
            except statemod.StateError as exc:
                return _report_error(base, meta_path, str(exc), logger)
        # d. 入口 entry 行（§6.5 停滞检测数据源）
        logger.ok("driver", "entry", None,
                  f"processed={meta.processed_chunks} status=running")
        # e. 完成判定 → done + purge（K32：purge 失败仅 WARN 保持 done）
        if meta.processed_chunks == meta.total_chunks \
                and meta.summary_chunks == meta.processed_chunks:
            statemod.update_metadata(meta_path, base=base, status="done")
            try:
                recovery.purge_rollback(root, logger=logger)
            except Exception as exc:
                logger.warn("driver", "purge", None,
                            f"purge 失败（仅 WARN，K32，保持 done）: {exc}")
            print(f"STATUS=done processed={meta.processed_chunks} "
                  f"summary={meta.summary_chunks} current_batch={meta.current_batch}")
            return 0
        # f. 推进一批（§6.2；未知异常当轮重试 1 次，C2/A5）
        try:
            br = reader.process_batch(base, meta, cfg, logger=logger,
                                      stop_check=flag)
        except reader.ProcessError as exc:
            return _report_error(base, meta_path, str(exc), logger)
        except Exception as exc:
            logger.error("driver", "batch", None,
                         f"脚本异常（当轮重试 1 次，C2/A5）: {exc}")
            try:
                meta2 = statemod.read_metadata(meta_path, base=base)
                br = reader.process_batch(base, meta2, cfg, logger=logger,
                                          stop_check=flag)
            except reader.ProcessError as exc2:
                return _report_error(base, meta_path, str(exc2), logger)
            except Exception as exc2:
                return _report_error(base, meta_path,
                                     f"当轮重试仍异常: {exc2}", logger)
        if br.get("kind") == "stopped":
            m = statemod.read_metadata(meta_path, base=base)
            print(f"STATUS=stopped processed={m.processed_chunks} "
                  f"summary={m.summary_chunks}", flush=True)
            return 0
        # g. 小结判定与执行（§6.3；未知异常当轮重试 1 次）
        try:
            sr = summarizer.process_summary(base, meta, cfg, logger=logger,
                                            stop_check=flag)
        except reader.ProcessError as exc:
            return _report_error(base, meta_path, str(exc), logger)
        except Exception as exc:
            logger.error("driver", "summary", None,
                         f"小结脚本异常（当轮重试 1 次）: {exc}")
            try:
                meta2 = statemod.read_metadata(meta_path, base=base)
                sr = summarizer.process_summary(base, meta2, cfg, logger=logger,
                                                stop_check=flag)
            except reader.ProcessError as exc2:
                return _report_error(base, meta_path, str(exc2), logger)
            except Exception as exc2:
                return _report_error(base, meta_path,
                                     f"小结当轮重试仍异常: {exc2}", logger)
        # h/i/j. 退出条件
        if once:
            m = statemod.read_metadata(meta_path, base=base)
            print(f"STATUS=round processed={m.processed_chunks} "
                  f"summary={m.summary_chunks}", flush=True)
            return 0
        if flag():
            logger.ok("driver", "stop", None, "stop_flag 置位，优雅退出")
            m = statemod.read_metadata(meta_path, base=base)
            print(f"STATUS=stopped processed={m.processed_chunks} "
                  f"summary={m.summary_chunks}", flush=True)
            return 0
        if max_rounds is not None and rounds >= max_rounds:
            m = statemod.read_metadata(meta_path, base=base)
            print(f"STATUS=max_rounds processed={m.processed_chunks} "
                  f"summary={m.summary_chunks}", flush=True)
            return 0
        # k. 停滞检测（§6.5：连续 2 轮入口 processed 相同 → WARN，不置 error）
        stalled, val = detect_stall(root / "run.log")
        if stalled:
            logger.warn("driver", "stall", None,
                        f"连续 2 轮入口 processed 相同（{val}），可能停滞（仅 WARN）")


def _cmd_dry_run(base, cfg: Config, novel: str) -> int:
    """K6：加锁前、零写入；打印动作清单 + 预计退出码。"""
    root = paths.root_of(base, novel)
    meta_path = root / "metadata.json"
    try:
        meta = statemod.read_metadata(meta_path, base=base)
    except statemod.MetadataMissing:
        print(f"PLAN=auto_init novel={novel} "
              "（metadata 缺失 → K31 自动 init，K42 默认参数）")
        return 0
    except statemod.MetadataCorrupt as exc:
        print(f"ERROR={exc}")
        return 3
    if meta.status == "done":
        print(f"PLAN=done processed={meta.processed_chunks}")
        return 0
    if meta.status == "error":
        print(f"PLAN=error: {meta.error}")
        return 3
    if meta.status == "init":
        print(f"PLAN=start processed={meta.processed_chunks}")
        return 0
    start, end, n = reader.compute_batch_range(meta)
    queue = reader.build_call_queue(root, meta, start, end) if start <= meta.total_chunks else []
    summary_yes = summarizer.should_summarize(meta)
    print(f"PLAN=run processed={meta.processed_chunks} batch=[{start},{end}]"
          f" pending={len(queue)} summary={'yes' if summary_yes else 'no'} exit=0")
    return 0


def _cmd_status(base, novel: str) -> int:
    """K25：不加锁。done→0；error→3（§6.0 步骤 b）。"""
    meta = statemod.read_metadata(paths.root_of(base, novel) / "metadata.json",
                                  base=base)
    print(f"STATUS={meta.status} PROCESSED={meta.processed_chunks} "
          f"SUMMARY={meta.summary_chunks} TOTAL={meta.total_chunks} "
          f"CURRENT_BATCH={meta.current_batch}")
    return 0 if meta.status != "error" else 3


def _cmd_init(base, cfg: Config, a) -> int:
    """init 子命令（同 cli/init_novel.py，复用 core.recovery；K25 加锁）。"""
    novel = paths.validate_novel_name(a.novel)
    lock = lockmod.acquire(base, novel, warn_sink=lambda m: print(f"WARN {m}",
                                                                  file=sys.stderr))
    try:
        if a.recover:
            res = recovery.recover(base, novel, cfg,
                                   batch_size=a.batch_size,
                                   timeline_window=a.timeline_window,
                                   summary_max=a.summary_max,
                                   chunk_max_chars=a.chunk_max_chars,
                                   summary_chunks=a.summary_chunks,
                                   current_batch=a.current_batch)
        else:
            res = recovery.init_novel(
                base, novel, cfg,
                batch_size=5 if a.batch_size is None else a.batch_size,
                timeline_window=10 if a.timeline_window is None else a.timeline_window,
                summary_max=5000 if a.summary_max is None else a.summary_max,
                chunk_max_chars=20000 if a.chunk_max_chars is None else a.chunk_max_chars)
    finally:
        lock.release()
    print(f"STATUS=OK novel={res['novel']} total={res['total_chunks']} "
          f"processed={res['processed_chunks']} summary={res['summary_chunks']} "
          f"current_batch={res['current_batch']} root={res['root_dir']}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="run.py", description="小说阅读自动化主入口（§6.0）")
    sub = p.add_subparsers(dest="cmd", required=True)

    rp = sub.add_parser("run", help="常驻/单步主循环")
    rp.add_argument("--novel", required=True)
    rp.add_argument("--once", action="store_true")
    rp.add_argument("--max-rounds", type=int)
    rp.add_argument("--dry-run", action="store_true")

    ip = sub.add_parser("init", help="初始化（同 cli/init_novel.py）")
    ip.add_argument("--novel", required=True)
    ip.add_argument("--recover", action="store_true")
    for opt, dest in (("--batch-size", "batch_size"),
                      ("--timeline-window", "timeline_window"),
                      ("--summary-max", "summary_max"),
                      ("--chunk-max-chars", "chunk_max_chars"),
                      ("--summary-chunks", "summary_chunks"),
                      ("--current-batch", "current_batch")):
        ip.add_argument(opt, type=int, dest=dest)

    sp = sub.add_parser("status", help="状态（不加锁，K25）")
    sp.add_argument("--novel", required=True)
    sub.add_parser("validate", help="配置校验（不加锁，K25）")

    a = p.parse_args(argv)
    try:
        cfg = load_config()
    except Exception as exc:
        print(f"ERROR={exc}")
        return 2
    if a.cmd == "validate":
        try:
            ensure_api_key(cfg)
        except Exception as exc:
            print(f"ERROR={exc}")
            return 2
        print("CONFIG=OK")
        return 0
    try:
        novel = paths.validate_novel_name(a.novel)
    except Exception as exc:
        print(f"ERROR={exc}")
        return 2
    base = paths.base_dir()
    if a.cmd == "status":
        try:
            return _cmd_status(base, novel)
        except Exception as exc:
            print(f"ERROR={exc}")
            return 3
    if a.cmd == "init":
        try:
            return _cmd_init(base, cfg, a)
        except Exception as exc:
            print(f"ERROR={exc}")
            return 3
    # run
    if a.dry_run:                                   # K6/K25：dry-run 在加锁前
        return _cmd_dry_run(base, cfg, novel)
    flag = _StopFlag()
    install_interrupt_handlers(flag)
    try:
        lock = lockmod.acquire(base, novel, warn_sink=lambda m: print(
            f"WARN {m}", file=sys.stderr))
    except lockmod.LockError as exc:
        print(f"ERROR={exc}")
        return 3
    try:
        return _run_loop(base, cfg, novel, once=a.once, max_rounds=a.max_rounds,
                         flag=flag)
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
