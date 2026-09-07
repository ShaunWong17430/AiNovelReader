"""gui/main_window.py — 主窗口（PyQt6）。

布局：
- 顶部：小说路径选择（浏览目录 → 自动推导 base + novel，不再依赖传入小说名）；
- 中部左：reader / summarizer 双套配置（url + 模型名 + api key，彻底分离）；
- 中部右：元数据观察窗（中文化）+ 日志显示；
- 底部：保存配置 / 初始化 / 开始 / 停止（等价 Ctrl+C）。

线程模型：RunWorker(QThread) 跑 init/run；停止 = 置 stop_flag 优雅退出。
"""
from __future__ import annotations

import os
import re
import sys
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QTextCharFormat, QTextCursor
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
    QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

from . import config_io, secrets as secretsmod
from .worker import RunWorker

CODE_ROOT = Path(__file__).resolve().parents[1]
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

from core.paths import InvalidName, base_dir, validate_novel_name  # noqa: E402

# 元数据字段 → 中文标签（观察窗）
_META_LABELS = [
    ("novel_name", "小说名"), ("root_dir", "根目录"),
    ("status", "状态"), ("total_chunks", "总片数"),
    ("processed_chunks", "已读片数"), ("summary_chunks", "已小结片数"),
    ("current_batch", "当前批次"), ("batch_size", "批大小"),
    ("batch_retries", "批重试"), ("summary_retries", "小结重试"),
    ("timeline_window", "时间线窗口"), ("summary_max", "小结上限字数"),
    ("chunk_max_chars", "单片上限字数"), ("chunk_padding", "片号位数"),
    ("prompt_version", "提示词版本"), ("config_fingerprint", "配置指纹"),
    ("llm_calls", "调用次数"), ("llm_prompt_tokens", "输入 tokens"),
    ("llm_completion_tokens", "输出 tokens"),
    ("llm_last_error", "最近 LLM 错误"), ("error", "错误信息"),
    ("recovery_required", "需恢复"), ("created_at", "创建时间"),
    ("updated_at", "更新时间"),
]

_STATUS_CN = {"init": "待初始化", "running": "运行中",
              "done": "已完成", "error": "出错"}

_LOG_LEVEL_RE = re.compile(r"^\[.*?\]\s*\[(\w+)\]\s+")     # 日志行级别提取


def _now_ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("小说阅读自动化 · 控制台")
        self.resize(1180, 780)
        self._worker: RunWorker | None = None
        self._base: Path | None = None
        self._novel: str = ""
        self._build_ui()
        self._timer = QTimer(self)                  # 元数据观察窗刷新
        self._timer.timeout.connect(self._refresh_meta)
        self._timer.start(2000)
        self._apply_state_from_disk()
        self._update_button_state()

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        central = QWidget(self)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)

        # 1. 小说路径
        path_box = QGroupBox("小说路径（自动推导基地 base 与小说名，无需手输小说名）")
        pb = QHBoxLayout(path_box)
        self.ed_path = QLineEdit()
        self.ed_path.setPlaceholderText(
            "选择小说根目录（含 chunks\\ 或 metadata.json），或小说库根目录")
        btn_browse = QPushButton("浏览…")
        btn_browse.clicked.connect(self._browse_novel)
        pb.addWidget(self.ed_path, 1)
        pb.addWidget(btn_browse)
        self.lbl_derived = QLabel("尚未选择")
        self.lbl_derived.setStyleSheet("color:#555;")
        pb.addWidget(self.lbl_derived)
        outer.addWidget(path_box)

        # 2. 配置区（双套）
        cfg_row = QHBoxLayout()
        cfg_row.setSpacing(8)
        self._reader_edits = self._make_phase_box("阅读模型（Reader）", cfg_row)
        self._sum_edits = self._make_phase_box("小结模型（Summarizer）", cfg_row)
        outer.addLayout(cfg_row)

        # 3. 按钮行
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.btn_save = QPushButton("保存配置")
        self.btn_save.clicked.connect(self._save_config)
        self.btn_init = QPushButton("初始化")
        self.btn_init.setToolTip("扫描 chunks\\ 并生成 metadata.json / 骨架")
        self.btn_init.clicked.connect(self._do_init)
        self.btn_start = QPushButton("开始")
        self.btn_start.setStyleSheet(
            "QPushButton{background:#2e7d32;color:#fff;font-weight:bold;"
            "padding:6px 22px;} QPushButton:disabled{background:#aaa;}")
        self.btn_start.clicked.connect(self._do_start)
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setToolTip("等价 Ctrl+C：当前片完成后优雅退出")
        self.btn_stop.setStyleSheet(
            "QPushButton{background:#b71c1c;color:#fff;font-weight:bold;"
            "padding:6px 22px;} QPushButton:disabled{background:#aaa;}")
        self.btn_stop.clicked.connect(self._do_stop)
        for b in (self.btn_save, self.btn_init, self.btn_start, self.btn_stop):
            btn_row.addWidget(b)
        btn_row.addStretch(1)
        outer.addLayout(btn_row)

        # 4. 元数据 + 日志
        split = QSplitter(Qt.Orientation.Vertical)
        self.meta_table = QTableWidget(len(_META_LABELS), 2)
        self.meta_table.setHorizontalHeaderLabels(["字段", "值"])
        self.meta_table.horizontalHeader().setStretchLastSection(True)
        self.meta_table.setColumnWidth(0, 150)
        self.meta_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers)
        self.meta_table.verticalHeader().setVisible(False)
        for row, (_key, label) in enumerate(_META_LABELS):
            self.meta_table.setItem(row, 0, QTableWidgetItem(label))
        meta_wrap = QWidget()
        mv = QVBoxLayout(meta_wrap)
        mv.setContentsMargins(0, 0, 0, 0)
        mv.addWidget(QLabel("元数据观察（每 2 秒刷新）"))
        mv.addWidget(self.meta_table)
        split.addWidget(meta_wrap)

        log_wrap = QWidget()
        lv = QVBoxLayout(log_wrap)
        lv.setContentsMargins(0, 0, 0, 0)
        head = QHBoxLayout()
        head.addWidget(QLabel("日志"))
        head.addStretch(1)
        btn_clear = QPushButton("清空")
        btn_clear.clicked.connect(self._clear_log)
        head.addWidget(btn_clear)
        lv.addLayout(head)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        self.log_view.setStyleSheet(
            "QPlainTextEdit{font-family:'Consolas','Microsoft YaHei UI',"
            "monospace;font-size:12px;background:#fafafa;}")
        lv.addWidget(self.log_view)
        split.addWidget(log_wrap)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 2)
        outer.addWidget(split, 1)

        self.setCentralWidget(central)
        self.statusBar().showMessage("就绪")

    def _make_phase_box(self, title: str, row: QHBoxLayout) -> dict:
        box = QGroupBox(title)
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        url = QLineEdit()
        url.setPlaceholderText("http://127.0.0.1:1235/v1")
        model = QLineEdit()
        model.setPlaceholderText("Qwen / gpt-4o-mini…")
        key = QLineEdit()
        key.setEchoMode(QLineEdit.EchoMode.Password)
        key.setPlaceholderText("留空 = 沿用已保存密钥")
        show = QCheckBox("显示")
        show.toggled.connect(
            lambda on, k=key: k.setEchoMode(
                QLineEdit.EchoMode.Normal if on
                else QLineEdit.EchoMode.Password))
        kh = QHBoxLayout()
        kh.addWidget(key, 1)
        kh.addWidget(show)
        form.addRow("接口 URL", url)
        form.addRow("模型名称", model)
        form.addRow("API Key", kh)
        row.addWidget(box, 1)
        return {"url": url, "model": model, "key": key}

    # ------------------------------------------------------------- 路径推导

    def _browse_novel(self) -> None:
        start = str(self._base or base_dir())
        chosen = QFileDialog.getExistingDirectory(
            self, "选择小说根目录或小说库根目录", start)
        if chosen:
            self.ed_path.setText(chosen)
            self._derive_path()

    def _derive_path(self) -> bool:
        """路径 → (base, novel)。支持两种选择：
        ① 小说根目录（含 chunks\\ 或 metadata.json）→ base=parent, novel=dirname；
        ② 小说库根目录 → novel 待选（下拉候选由 _update_derived 罗列）。
        """
        raw = self.ed_path.text().strip()
        if not raw:
            return False
        p = Path(raw).resolve()
        if not p.is_dir():
            self.lbl_derived.setText("✗ 路径不存在")
            return False
        is_root = (p / "chunks").is_dir() or (p / "metadata.json").exists()
        if is_root:
            base, novel = p.parent, p.name
        else:
            base, novel = p, ""
        try:
            if novel:
                novel = validate_novel_name(novel)
        except InvalidName as exc:
            self.lbl_derived.setText(f"✗ {exc}")
            return False
        self._base, self._novel = base, novel
        if novel:
            os.environ["NOVEL_BASE"] = str(base)   # 供 core.paths.base_dir()
            self.lbl_derived.setText(f"base: {base.name} · 小说: {novel}")
        else:
            os.environ["NOVEL_BASE"] = str(base)
            self.lbl_derived.setText(f"base: {base.name} · 小说: 待选（见日志）")
            self._log(f"[提示] 所选为小说库根目录；请在库内选择小说子目录",
                      level="SKIP")
        self._update_button_state()
        return True

    # ------------------------------------------------------------- 配置落盘

    def _collect_phase(self, edits: dict) -> dict:
        return {"base_url": edits["url"].text().strip(),
                "model": edits["model"].text().strip(),
                "api_key": edits["key"].text().strip()}

    def _save_config(self) -> bool:
        try:
            path = config_io.save_gui_state(
                self._collect_phase(self._reader_edits),
                self._collect_phase(self._sum_edits))
        except RuntimeError as exc:
            QMessageBox.critical(self, "保存失败", str(exc))
            self._log(f"[错误] {exc}", level="ERROR")
            return False
        self._log(f"[配置] 已保存 → {path.name}"
                  "（api key 已 DPAPI 加密，config.json 不含明文）")
        return True

    def _apply_state_from_disk(self) -> None:
        state = config_io.load_gui_state()
        for phase, edits in (("reader", self._reader_edits),
                             ("summarizer", self._sum_edits)):
            st = state.get(phase) or {}
            edits["url"].setText(st.get("base_url") or "")
            edits["model"].setText(st.get("model") or "")
            edits["key"].setText(st.get("api_key") or "")

    # ------------------------------------------------------------- 按钮动作

    def _do_init(self) -> None:
        if not self._prepare_run():
            return
        self._launch("init")

    def _do_start(self) -> None:
        if not self._prepare_run():
            return
        self._launch("run")

    def _prepare_run(self) -> bool:
        if not self.ed_path.text().strip():
            QMessageBox.information(self, "提示", "请先选择小说路径")
            return False
        if not self._derive_path():
            return False
        if not self._novel:
            QMessageBox.information(
                self, "提示",
                "所选为小说库根目录；请改为选择具体小说目录（含 chunks\\）")
            return False
        if not self._save_config():
            return False
        return True

    def _launch(self, mode: str) -> None:
        reader = self._collect_phase(self._reader_edits)
        summ = self._collect_phase(self._sum_edits)
        self._worker = RunWorker(
            self._base, self._novel, mode,
            reader_key=reader["api_key"] or None,
            summarizer_key=summ["api_key"] or None)
        self._worker.sig_log.connect(self._log)
        self._worker.sig_done.connect(self._on_worker_done)
        self._worker.sig_state.connect(
            lambda s: self.statusBar().showMessage(s))
        self._worker.start()
        self._log(f"[GUI] 已启动{mode == 'init' and '初始化' or '运行'}线程…",
                  level="OK")
        self._update_button_state()

    def _do_stop(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.stop()
            self._log("[GUI] 已请求停止（stop_flag 置位，当前片完成后退出）…",
                      level="WARN")
            self.statusBar().showMessage("正在停止…")

    def _on_worker_done(self, code: int, summary: str) -> None:
        if summary:
            self._log(f"[完成] {summary}", level="OK")
        if code == 0:
            self.statusBar().showMessage(
                f"操作完成（{_now_ts()}）{(' · ' + summary) if summary else ''}")
        else:
            self.statusBar().showMessage(f"操作结束，退出码 {code}")
        self._update_button_state()
        self._refresh_meta()

    def _update_button_state(self) -> None:
        busy = self._worker is not None and self._worker.isRunning()
        ready = bool(self._novel) and not busy
        self.btn_init.setEnabled(ready)
        self.btn_start.setEnabled(ready)
        self.btn_stop.setEnabled(busy)
        self.btn_save.setEnabled(not busy)

    # ------------------------------------------------------------- 元数据窗

    def _refresh_meta(self) -> None:
        if not (self._base and self._novel):
            return
        meta_path = self._base / self._novel / "metadata.json"
        doc = {}
        if meta_path.exists():
            try:
                import json
                doc = json.loads(meta_path.read_text(encoding="utf-8"))
                if not isinstance(doc, dict):
                    doc = {}
            except (OSError, ValueError):
                doc = {}
        for row, (key, label) in enumerate(_META_LABELS):
            val = doc.get(key, "—")
            if key == "status" and isinstance(val, str):
                val = _STATUS_CN.get(val, val)
            if key == "recovery_required":
                val = "是" if val is True else ("否" if val is False else val)
            item = QTableWidgetItem(str(val))
            if key == "status" and doc.get("status") == "error":
                item.setForeground(QColor("#b71c1c"))
            elif key == "status" and doc.get("status") == "done":
                item.setForeground(QColor("#2e7d32"))
            self.meta_table.setItem(row, 1, item)

    # ------------------------------------------------------------- 日志

    def _log(self, line: str, level: str | None = None) -> None:
        m = _LOG_LEVEL_RE.match(line)
        if level is None and m:
            level = m.group(1)
        color = {None: "#000", "OK": "#2e7d32", "WARN": "#e65100",
                 "SKIP": "#888", "ERROR": "#b71c1c"}.get(level, "#000")
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        cur = self.log_view.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.End)
        cur.insertText(f"[{_now_ts()}] {line}\n", fmt)
        self.log_view.setTextCursor(cur)
        self.log_view.ensureCursorVisible()

    def _clear_log(self) -> None:
        self.log_view.clear()

    # ------------------------------------------------------------- 收尾

    def closeEvent(self, event) -> None:       # noqa: N802（Qt 命名）
        if self._worker is not None and self._worker.isRunning():
            self._worker.stop()
            self._worker.wait(8000)
        event.accept()