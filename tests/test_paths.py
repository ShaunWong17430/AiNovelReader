"""出口测试 1（路径部分）/ 12.1：core/paths.py validate_novel_name 全 7 步（§2 / C8 / D12）。

覆盖：trim 通过；空 / "." / ".." / 含 ".." / 分隔符 / 正则外字符 /
Windows 保留名（含带扩展名形式）/ 结尾点 / 长度>100 → InvalidName（→ 退出码 2）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

import core.paths as paths


def test_trim_and_valid_names():
    assert paths.validate_novel_name(" 示例书名 ") == "示例书名"
    assert paths.validate_novel_name("Three-Body 3") == "Three-Body 3"
    assert paths.validate_novel_name("book_1 中文") == "book_1 中文"
    assert paths.validate_novel_name("a.b-c d") == "a.b-c d"


def test_empty_and_dots():
    for bad in ("", "   ", ".", "..", "a..b", "..evil", "x/../y"):
        with pytest.raises(paths.InvalidName, match="novel_name"):
            paths.validate_novel_name(bad)


def test_separators_and_regex():
    for bad in ("a/b", "a\\b", "a:b", "a*b", "a?b", "a\"b", "a<b", "a>b", "a|b",
                "哈利·波特"):          # 中点 U+00B7 不在 §2 白名单
        with pytest.raises(paths.InvalidName):
            paths.validate_novel_name(bad)


def test_windows_reserved_names_with_any_extension():
    for bad in ("CON", "con", "CON.txt", "con.foo", "PRN", "AUX", "NUL",
                "COM1", "com9.txt", "LPT1", "lpt8.log"):
        with pytest.raises(paths.InvalidName, match="保留名"):
            paths.validate_novel_name(bad)


def test_trailing_dot_and_length():
    with pytest.raises(paths.InvalidName, match="结尾"):
        paths.validate_novel_name("book.")
    with pytest.raises(paths.InvalidName, match="长度"):
        paths.validate_novel_name("x" * 101)
    assert paths.validate_novel_name("x" * 100) == "x" * 100


def test_base_constant_and_env_override(monkeypatch):
    assert paths.base_dir() == paths.DEFAULT_BASE
    assert paths.DEFAULT_BASE == Path(r"D:\data\dsh_wz1\novel_reading")
    sandbox = Path(r"D:\data\dsh_wz1\.test_sandboxes\fake_base")
    monkeypatch.setenv("NOVEL_BASE", str(sandbox))
    assert paths.base_dir() == sandbox
    # NOVEL_BASE 指向的沙箱同样执行 commonpath 守卫（C8）
    monkeypatch.setenv("NOVEL_BASE", str(sandbox.parent))
    assert paths.validate_novel_name("示例书名") == "示例书名"


def test_root_of_uses_validated_name(base_dir):
    root = paths.root_of(base_dir, "  示例书名  ")
    assert root == base_dir / "示例书名"
