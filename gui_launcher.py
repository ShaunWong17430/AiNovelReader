"""GUI 入口。

用法（任一）：
    python gui_launcher.py
    python -m gui.launcher

要求：PyQt6 环境（项目约定 D:\\data\\dsh_wz1\\pyfordsh\\python.exe）。
"""
from __future__ import annotations

import sys
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parent
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

from PyQt6.QtWidgets import QApplication  # noqa: E402

from gui.main_window import MainWindow    # noqa: E402


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Novel Reader GUI")
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())