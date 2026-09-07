"""core/paths.py — 路径与命名校验（§2，共享唯一实现）。

- `validate_novel_name(raw) -> str`：§2 全 7 步（strip → 正则 → 禁 '.'/'..'
  → basename → commonpath 禁 startswith（C8）→ Windows 保留名/结尾点/长度
  （D12）→ 返回规范名）。任何失败抛 `InvalidName`（调用方映射退出码 2，
  root_dir 不可信 → 跳过写 error，直接报告）。
- BASE 单常量（§2 铁律）：D:\\data\\dsh_wz1\\novel_reading。
  **测试支持（非需求偏离）**：环境变量 `NOVEL_BASE` 可覆盖 BASE，
  仅供测试/沙箱隔离；生产调用不设该变量，行为与常量完全一致。
- 铁律：调用方必须用返回值构造 ROOT（`root_of`），不收绝对路径参数。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

DEFAULT_BASE = Path(r"D:\data\dsh_wz1\novel_reading")   # §2 单常量

# §2 步骤 2：^[\w\u4e00-\u9fa5\-\. ]+$
_NAME_RE = re.compile(r"^[\w\u4e00-\u9fa5\-\. ]+$")

# §2 步骤 6（D12）：Windows 保留名（任何带扩展名形式亦命中）
_WIN_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

MAX_NAME_LEN = 100                                        # D12


class InvalidName(Exception):
    """novel_name 未过 §2 校验 → 退出码 2；文件一律不动。"""


def base_dir() -> Path:
    """BASE 单常量；仅测试经 NOVEL_BASE 覆盖（生产不设该变量）。"""
    env = os.environ.get("NOVEL_BASE")
    return Path(env) if env else DEFAULT_BASE


def validate_novel_name(raw: str) -> str:
    """§2 全 7 步校验；通过返回 trim 后规范名，失败抛 InvalidName。"""
    # 1. trim
    name = raw.strip()
    if not name:
        raise InvalidName("novel_name 为空（strip 后）")
    # 2. 正则
    if not _NAME_RE.match(name):
        raise InvalidName(f"novel_name 含非法字符（须 [\\w 中文 - . 空格]）: {name!r}")
    # 3. 禁 "." / ".." 及 ".." 子串
    if name in (".", "..") or ".." in name:
        raise InvalidName(f"novel_name 非法: {name!r}")
    # 4. basename 恒等（禁路径分隔符）
    if os.path.basename(name) != name:
        raise InvalidName(f"novel_name 含路径分隔符: {name!r}")
    # 5. commonpath 校验（C8：禁字符串 startswith）
    base = base_dir()
    try:
        common = os.path.commonpath(
            [os.path.realpath(base / name), os.path.realpath(base)])
    except ValueError as exc:
        raise InvalidName(f"novel_name 无法解析为 BASE 子路径: {name!r}") from exc
    if common != os.path.realpath(base):
        raise InvalidName(f"novel_name 越出 BASE 范围: {name!r}")
    # 6. Windows 合法性（D12）
    if name.endswith("."):
        raise InvalidName("novel_name 不能以点结尾")
    if len(name) > MAX_NAME_LEN:
        raise InvalidName(f"novel_name 长度 {len(name)} > {MAX_NAME_LEN}")
    stem = name.upper().split(".", 1)[0]      # 任何带扩展名形式均命中
    if stem in _WIN_RESERVED:
        raise InvalidName(f"novel_name 命中 Windows 保留名: {name!r}")
    # 7. 返回 trim 后规范名
    return name


def root_of(base: Path, name: str) -> Path:
    """ROOT = BASE / validate_novel_name(name)（调用方必须用返回值构造 ROOT）。"""
    return base / validate_novel_name(name)
