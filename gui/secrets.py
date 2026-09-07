"""gui/secrets.py — api key 脱密保存在后台（Windows DPAPI，零新依赖）。

- 加密：`CryptProtectData`（当前用户级，LocalMachine 不可见）；密文 hex 存
  `<CODE_ROOT>/gui/.secrets.json`，绝不落盘明文。
- 解密失败/非 Windows（无 DPAPI）→ 返回 None，绝不抛异常、绝不回显明文。
- 明文仅在 GUI 输入框与发送给 core 的瞬间存在于内存。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import os
from pathlib import Path

_SECRETS_FILE = Path(__file__).resolve().parent / ".secrets.json"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1

_DPAPI_AVAILABLE = os.name == "nt"

# 显式签名：64 位地址不会按 32 位 c_int 截断（windll 默认转换的经典坑）
if _DPAPI_AVAILABLE:
    _crypt32 = ctypes.windll.crypt32
    _crypt32.CryptProtectData.argtypes = [
        ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.wintypes.DWORD, ctypes.c_void_p]
    _crypt32.CryptProtectData.restype = ctypes.wintypes.BOOL
    _crypt32.CryptUnprotectData.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p), ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.wintypes.DWORD,
        ctypes.c_void_p]
    _crypt32.CryptUnprotectData.restype = ctypes.wintypes.BOOL
    _kernel32 = ctypes.windll.kernel32
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.wintypes.DWORD),
                ("pbData", ctypes.c_void_p)]


def _load_existing() -> dict:
    try:
        data = json.loads(_SECRETS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    try:
        _SECRETS_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        if os.name != "nt":
            try:
                _SECRETS_FILE.chmod(0o600)
            except OSError:
                pass
    except OSError:
        pass                     # 保存失败不抛：密钥仅当晚生效，绝不阻塞界面


def _protect(plain: str) -> bytes | None:
    """DPAPI 加密：仅当前用户可解；失败返回 None（不抛）。"""
    if not _DPAPI_AVAILABLE:
        return None
    try:
        utf16 = plain.encode("utf-16-le")
        buf = ctypes.create_string_buffer(len(utf16))
        ctypes.memmove(buf, utf16, len(utf16))
        blob_in = _DATA_BLOB(len(utf16), ctypes.cast(buf, ctypes.c_void_p))
        blob_out = _DATA_BLOB()
        ok = _crypt32.CryptProtectData(
            ctypes.byref(blob_in), None, None, None, None,
            _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
        if not ok:
            return None
        try:
            return bytes(ctypes.string_at(blob_out.pbData, blob_out.cbData))
        finally:
            _kernel32.LocalFree(blob_out.pbData)
    except Exception:
        return None


def _unprotect(blob: bytes) -> str | None:
    """DPAPI 解密；失败返回 None（不回显）。"""
    if not _DPAPI_AVAILABLE:
        return None
    try:
        buf = ctypes.create_string_buffer(len(blob))
        ctypes.memmove(buf, blob, len(blob))
        blob_in = _DATA_BLOB(len(blob), ctypes.cast(buf, ctypes.c_void_p))
        blob_out = _DATA_BLOB()
        ok = _crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None,
            _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
        if not ok:
            return None
        try:
            return bytes(ctypes.string_at(blob_out.pbData,
                                          blob_out.cbData)).decode(
                "utf-16-le", errors="replace").rstrip("\x00")
        finally:
            _kernel32.LocalFree(blob_out.pbData)
    except Exception:
        return None


def save_secret(slot: str, plain: str) -> bool:
    """按槽位（reader/summarizer）加密保存；成功返回 True。"""
    if not plain or not isinstance(plain, str):
        return False
    blob = _protect(plain)
    if blob is None:
        return False
    data = _load_existing()
    data[slot] = blob.hex()
    _save(data)
    return True


def load_secret(slot: str) -> str | None:
    """读槽位密文并解密；无/失败返回 None（绝不回显明文到日志）。"""
    data = _load_existing()
    hex_blob = data.get(slot)
    if not hex_blob:
        return None
    try:
        blob = bytes.fromhex(hex_blob)
    except ValueError:
        return None
    return _unprotect(blob)


def delete_secret(slot: str) -> None:
    """删除槽位密文（换 key 前清旧值用）。"""
    data = _load_existing()
    if slot in data:
        data.pop(slot, None)
        _save(data)
