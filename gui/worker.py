"""gui/worker.py — QThread 运行核心逻辑（初始化 / 主循环）。

- 在独立线程跑 `recovery.init_novel`（初始化）或 `run.py._run_loop`
  （开始/常驻主循环），不阻塞 GUI；
- 捕获 core 经 stdout/stderr 输出的日志行（Logger echo=True 的镜像），
  经 Qt 信号转发到界面日志窗；
- 停止按钮 = 置 `_StopFlag`（等价 Ctrl+C 的 stop_flag，片间/退避优雅退出）；
- 运行前进程内设 `NOVEL_BASE`（小说路径推导的 base）+ 两套 phase 的
  API key 环境变量（DPAPI 解密后只存在于本进程 env，绝不分发日志）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal

from core.logger import sanitize_msg

# CODE_ROOT 前置（gui\\ 与 cli\\ 同约定：cwd 固定 novel_skill\\ 或已注入 sys.path）
CODE_ROOT = Path(__file__).resolve().parents[1]
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

import run as runmod                      # noqa: E402  仅复用 _run_loop/_StopFlag
import core.lock as lockmod               # noqa: E402
import core.recovery as recovery          # noqa: E402

# GUI 双套 API key 的独立环境变量名（profile.api_key_env 指向，config.json
# 不落明文；worker 运行前由 GUI 按 DPAPI 解密结果设置）
ENV_READER_KEY = "NOVEL_LLM_READER_KEY"
ENV_SUMMARIZER_KEY = "NOVEL_LLM_SUMMARIZER_KEY"


class _EmitStream:
    """把 write() 按行拆分为 (strip 空行) 回调；GUI 日志与原始流双写。"""

    def __init__(self, emit, fallback):
        self._emit = emit
        self._fallback = fallback
        self._buf = ""

    def write(self, text: str) -> None:
        try:
            if self._fallback is not None:
                self._fallback.write(text)
        except Exception:
            pass
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.rstrip("\r")
            if line.strip():
                try:
                    self._emit(line)
                except Exception:
                    pass

    def flush(self) -> None:
        if self._buf.strip():
            try:
                self._emit(self._buf.rstrip("\r\n"))
            except Exception:
                pass
            self._buf = ""
        try:
            if self._fallback is not None:
                self._fallback.flush()
        except Exception:
            pass


class RunWorker(QThread):
    """跑一次操作（init 或 run）。信号：log 行 / 完成(exit_code, 摘要)。"""

    sig_log = pyqtSignal(str)
    sig_done = pyqtSignal(int, str)
    sig_state = pyqtSignal(str)

    def __init__(self, base: Path, novel: str, mode: str,
                 reader_key: str | None = None,
                 summarizer_key: str | None = None,
                 parent=None):
        super().__init__(parent)
        self._base = Path(base)
        self._novel = novel
        self._mode = mode                    # "init" | "run"
        self._reader_key = reader_key
        self._summarizer_key = summarizer_key
        self._flag = runmod._StopFlag()
        self._finished_signal_sent = False

    # -- 供 GUI 调用 --------------------------------------------------------

    def stop(self) -> None:
        """停止按钮：置 stop_flag（等价 Ctrl+C；片间/退避优雅退出）。"""
        self._flag.set()

    # -- 线程体 --------------------------------------------------------------

    def run(self) -> None:                   # QThread.run
        # 1. 进程内环境：NOVEL_BASE（小说路径推导）+ 双套 phase key
        os.environ["NOVEL_BASE"] = str(self._base)
        if self._reader_key is not None:
            os.environ[ENV_READER_KEY] = self._reader_key
        if self._summarizer_key is not None:
            os.environ[ENV_SUMMARIZER_KEY] = self._summarizer_key

        old_out, old_err = sys.stdout, sys.stderr
        try:
            sys.stdout = _EmitStream(self.sig_log.emit, old_out)
            sys.stderr = _EmitStream(self.sig_log.emit, old_err)
            code = self._do()
        except Exception as exc:             # 未知异常 → 日志 + 退出码 1
            code = 1
            try:
                self.sig_log.emit(f"[异常] {exc}")
            except Exception:
                print(f"[异常] {exc}", file=old_err)
        finally:
            sys.stdout, sys.stderr = old_out, old_err
        if not self._finished_signal_sent:
            self._finished_signal_sent = True
            self.sig_done.emit(code, self._summarize_result() if code == 0
                               else "")

    def _do(self) -> int:
        try:
            from core.config import Config, load_config
            cfg = load_config()              # CODE_ROOT/config.json（GUI 已写入）
        except Exception as exc:
            self.sig_log.emit(f"[配置错误] {exc}")
            return 2

        # 加锁（run/init 均加写锁，K25；与 cli 行为一致）
        try:
            lock = lockmod.acquire(
                self._base, self._novel,
                warn_sink=lambda m: self.sig_log.emit(f"[锁警告] {m}"))
        except lockmod.LockError as exc:
            self.sig_log.emit(f"[锁错误] {exc}")
            return 3
        try:
            return self._locked_run(cfg)
        finally:
            lock.release()

    def _locked_run(self, cfg) -> int:
        root = self._base / self._novel
        if self._mode == "init":
            self.sig_log.emit(f"开始初始化小说：{self._novel}…")
            try:
                res = recovery.init_novel(self._base, self._novel, cfg)
                self.sig_log.emit(
                    sanitize_msg(
                        f"[初始化完成] 总片数={res['total_chunks']} "
                        f"已读={res['processed_chunks']} "
                        f"小结={res['summary_chunks']} "
                        f"批次={res['current_batch']} 根目录={res['root_dir']}"))
                return 0
            except recovery.InitError as exc:
                self.sig_log.emit(f"[初始化失败] {exc}")
                return exc.code
        # run：常驻主循环（K31 缺失 metadata 自动 init；stop_flag 优雅退出）
        self.sig_log.emit(f"开始运行小说：{self._novel}（停止按钮 = Ctrl+C）…")
        try:
            return runmod._run_loop(self._base, cfg, self._novel,
                                    once=False, max_rounds=None,
                                    flag=self._flag)
        except Exception as exc:
            self.sig_log.emit(f"[运行异常] {exc}")
            return 1

    def _summarize_result(self) -> str:
        try:
            from core.state import MetadataMissing, read_metadata
            meta = read_metadata(self._base / self._novel / "metadata.json")
            return sanitize_msg(
                f"状态={meta.status} 已读={meta.processed_chunks}/"
                f"{meta.total_chunks} 小结={meta.summary_chunks}")
        except (MetadataMissing, Exception):
            return ""