"""core/lock.py — BASE 级单实例锁（§1.8，K17 / K25 / K26 / K41）。

- 锁位置（K17）：`BASE\\.<novel_name.casefold()>.run.lock`（与 metadata 同级，
  首次运行 ROOT 未建亦可建锁）；
- 原子创建：`O_CREAT|O_EXCL`（Python `open('x')`）；
- 存活探测（K26）：`ctypes.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` +
  `GetExitCodeProcess`（零新依赖）；
- K41 防 PID 复用：`GetProcessTimes`（同一句柄）取进程创建时间，与锁内
  `create_time`（= 持锁进程创建时间）比对——±1s 容差判「同一进程」；
  不等（含「早于」）→ 判 PID 复用 → 按「进程已死」接管，log WARN；
- `--force`：跳过存活探测强制接管（驱动器自动流程从不带 --force）；
- finally 释放：仅删除自己持有（pid 匹配）的锁。
- 适用范围（K25）：run/init 与 cli\\* 写命令加锁；status/validate/
  --dry-run 不加锁（metadata 写是 os.replace 原子，读侧安全）。
"""
from __future__ import annotations

import ctypes
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from .logger import now_iso

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259
ERROR_ACCESS_DENIED = 5
_FILETIME_EPOCH_DIFF = 116444736000000000      # 1601-01-01 → 1970-01-01（100ns）
_SAME_PROCESS_TOLERANCE_S = 1.0                # K41 ±1s 容差


class LockError(Exception):
    """获锁失败（进程存活持锁）→ 退出码 3，提示 PID，如确无见 --force。"""


@dataclass
class Lock:
    path: Path
    pid: int
    create_time: float
    owned: bool = True
    took_over: bool = False

    def release(self) -> None:
        """finally 释放：仅删除 pid 匹配的锁（防误删他人锁）。"""
        if not self.owned:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if int(data.get("pid", -1)) == self.pid:
                self.path.unlink()
        except FileNotFoundError:
            pass
        except Exception:
            pass
        finally:
            self.owned = False


def lock_path_of(base: Path, novel_name: str) -> Path:
    """K26：锁名用 novel_name.casefold()（Windows 大小写不敏感）。"""
    return base / f".{novel_name.casefold()}.run.lock"


def case_collision(base: Path, novel_name: str) -> str | None:
    """K26：init 时检测大小写碰撞——BASE 下已有 casefold 同名不同写法的目录。

    返回碰撞目录名；无碰撞返回 None。（Windows 大小写不敏感致两锁管一套
    metadata，故 init 必须拒绝。）
    """
    folded = novel_name.casefold()
    if not base.is_dir():
        return None
    for child in base.iterdir():
        if (child.is_dir() and child.name.casefold() == folded
                and child.name != novel_name):
            return child.name
    return None


# ---------------------------------------------------------------------------
# Windows 存活探测（ctypes 零新依赖，K26 / K41）
# ---------------------------------------------------------------------------

def process_creation_epoch(pid: int) -> float | None:
    """取 pid 进程创建时间（epoch 秒）；无法取得返回 None。

    仅内部使用；OpenProcess 失败视为进程不存在（None 由 probe_pid 判死）。
    """
    status, creation = probe_pid(pid)
    return creation


def probe_pid(pid: int) -> tuple[str, float | None]:
    """存活探测 → (状态, 进程创建时间 epoch | None)。

    状态 ∈ {"alive", "dead", "denied"}：
    - OpenProcess 失败：ERROR_ACCESS_DENIED → "denied"（存在但不可查，按存活）；
      其余 → "dead"；
    - OpenProcess 成功：GetExitCodeProcess != STILL_ACTIVE → "dead"；
      GetProcessTimes 成功 → 附创建时间；失败 → "alive"（保守，无时间）。
    """
    if pid <= 0:
        return "dead", None
    try:
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    except (AttributeError, OSError):
        return "dead", None                       # 非 Windows / 调用失败
    if not handle:
        err = ctypes.get_last_error()
        return ("denied", None) if err == ERROR_ACCESS_DENIED else ("dead", None)
    try:
        exit_code = ctypes.c_ulong()
        if not ctypes.windll.kernel32.GetExitCodeProcess(
                handle, ctypes.byref(exit_code)):
            return "alive", None
        if exit_code.value != STILL_ACTIVE:
            return "dead", None
        creation = _FILETIME()
        exit_ft = _FILETIME()
        kernel = _FILETIME()
        user = _FILETIME()
        if not ctypes.windll.kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation), ctypes.byref(exit_ft),
                ctypes.byref(kernel), ctypes.byref(user)):
            return "alive", None
        return "alive", (creation.value - _FILETIME_EPOCH_DIFF) / 1e7
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


class _FILETIME(ctypes.Structure):
    """FILETIME：dwLowDateTime + dwHighDateTime → 100ns 计数。"""
    _fields_ = [("dwLowDateTime", ctypes.c_ulong),
                ("dwHighDateTime", ctypes.c_ulong)]

    @property
    def value(self) -> int:
        return (self.dwHighDateTime << 32) | self.dwLowDateTime


# ---------------------------------------------------------------------------
# 获取 / 释放
# ---------------------------------------------------------------------------

def _write_payload(path: Path, payload: dict) -> None:
    """原子覆写锁内容（临时文件 + os.replace，防截断竞态）。"""
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    tmp.write_text(json.dumps(payload, ensure_ascii=False),
                   encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def acquire(base: Path, novel_name: str, *, force: bool = False,
            warn_sink=None) -> Lock:
    """获锁；失败抛 LockError（→ 3）。force 跳过存活探测强制接管。

    warn_sink: callable(msg) 用于接管 WARN 提示（run.log 或 stderr）。
    """
    path = lock_path_of(base, novel_name)
    self_creation = process_creation_epoch(os.getpid()) or time.time()
    payload = {"pid": os.getpid(), "create_time": self_creation,
               "ts": now_iso()}
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        return Lock(path=path, pid=os.getpid(), create_time=self_creation)
    except FileExistsError:
        pass

    # 已存在 → 读现有锁
    epid, ecreate = -1, 0.0
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
        epid = int(existing.get("pid", -1))
        ecreate = float(existing.get("create_time", 0.0))
    except Exception:
        epid, ecreate = -1, 0.0                     # 不可读 → 视为残留

    takeover = bool(force)
    if not takeover and epid > 0:
        status, creation = probe_pid(epid)
        if status == "dead":
            takeover = True                         # 进程不存在/已死 → 接管
        elif status == "denied":
            raise LockError(
                f"锁被进程 {epid} 持有（存活但不可探测）；如确无见 --force")
        else:  # alive
            if creation is not None and abs(creation - ecreate) > _SAME_PROCESS_TOLERANCE_S:
                takeover = True                     # K41：创建时间不符 → PID 复用
            else:
                raise LockError(
                    f"进程 {epid} 存活持锁（{path.name}）；如确无见 --force")
    elif not takeover:
        takeover = True                             # 无 pid 可查 → 残留接管

    if takeover:
        if warn_sink is not None:
            warn_sink(
                f"锁 {path.name} 残留/被复用（pid={epid}），接管（K26/K41）；"
                "若系另一运行实例请用 --force 核对")
        _write_payload(path, payload)
        return Lock(path=path, pid=os.getpid(),
                    create_time=self_creation, took_over=True)
    raise LockError("获锁失败（未接管）")            # 理论不可达
