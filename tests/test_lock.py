"""出口测试 33 / 50 + §1.8：core/lock.py（K17 / K25 / K26 / K41，--force）。

覆盖：
- 33. K26：taskkill /F 残留锁（死 pid）→ 接管 WARN；--force 跳过探测；
      并发同书（进程存活持锁）→ 后到者退出码 3；锁名 casefold（K26）；
- 50. K41：伪造「存活但进程创建时间早于锁 create_time」的残留锁
      → 判 PID 复用、接管并 log WARN；
- 锁内容结构 {pid, create_time, ts}；finally 释放仅删自己持有的锁。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import core.lock as lockmod


def _spawn_sleeper() -> subprocess.Popen:
    """派生一个存活 60s 的辅助进程（供存活探测用）。"""
    return subprocess.Popen([sys.executable, "-c",
                             "import time; time.sleep(60)"])


def test_acquire_release_lifecycle(base_dir):
    lock = lockmod.acquire(base_dir, "示例书名")
    path = lockmod.lock_path_of(base_dir, "示例书名")
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["pid"] == os.getpid()
    assert isinstance(data["create_time"], float)
    assert data["ts"]
    lock.release()
    assert not path.exists()


def test_lock_name_casefold_k26(base_dir):
    assert lockmod.lock_path_of(base_dir, "Foo Book") == \
        base_dir / ".foo book.run.lock"
    assert lockmod.lock_path_of(base_dir, "示例书名") == \
        base_dir / ".示例书名.run.lock"


def test_concurrent_holder_gets_exit3(base_dir):
    """K17/K26：进程存活持锁 → 后到者 LockError（→ 退出码 3）。"""
    lock = lockmod.acquire(base_dir, "示例书名")
    try:
        with pytest.raises(lockmod.LockError, match="存活"):
            lockmod.acquire(base_dir, "示例书名")
    finally:
        lock.release()


def test_dead_pid_residual_lock_takeover_warn(base_dir):
    """33（taskkill /F 场景模拟）：残留锁（死 pid）→ 接管 + WARN。"""
    path = lockmod.lock_path_of(base_dir, "示例书名")
    path.write_text(json.dumps({"pid": 99999999, "create_time": 0.0,
                                "ts": "2026-01-01T00:00:00+08:00"}),
                    encoding="utf-8")
    warns: list[str] = []
    lock = lockmod.acquire(base_dir, "示例书名", warn_sink=warns.append)
    try:
        assert lock.took_over is True
        assert warns and "接管" in warns[0]
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["pid"] == os.getpid()          # 已重写为我的锁
    finally:
        lock.release()


def test_garbage_lock_taken_over(base_dir):
    path = lockmod.lock_path_of(base_dir, "示例书名")
    path.write_bytes(b"\xff\xfe not json")
    lock = lockmod.acquire(base_dir, "示例书名", warn_sink=lambda m: None)
    assert lock.took_over
    lock.release()


def test_force_skips_liveness_probe(base_dir):
    """33：--force 跳过存活探测强制接管（与 backup --force 独立旗标）。"""
    path = lockmod.lock_path_of(base_dir, "示例书名")
    lock = lockmod.acquire(base_dir, "示例书名")   # 自己持锁（存活）
    try:
        lock2 = lockmod.acquire(base_dir, "示例书名", force=True)
        assert lock2.took_over
        lock2.release()
    finally:
        lock.release()                             # 已被 force 覆写，释放为 no-op


def test_release_only_removes_own_lock(base_dir):
    path = lockmod.lock_path_of(base_dir, "示例书名")
    lock = lockmod.acquire(base_dir, "示例书名")
    # 模拟他人接管：覆写为其它 pid
    path.write_text(json.dumps({"pid": 12345, "create_time": 0.0,
                                "ts": "x"}), encoding="utf-8")
    lock.release()
    assert path.exists()                           # 非我持有，不删


def test_k41_pid_reuse_creation_time_mismatch_takeover(base_dir):
    """50：伪造「存活但进程创建时间早于锁 create_time」→ PID 复用接管 + WARN。"""
    proc = _spawn_sleeper()
    try:
        creation = lockmod.process_creation_epoch(proc.pid)
        assert creation is not None
        path = lockmod.lock_path_of(base_dir, "示例书名")
        path.write_text(json.dumps({
            "pid": proc.pid,
            "create_time": creation + 3600,        # 早于锁内 create_time
            "ts": "2026-01-01T00:00:00+08:00",
        }), encoding="utf-8")
        warns: list[str] = []
        lock = lockmod.acquire(base_dir, "示例书名", warn_sink=warns.append)
        try:
            assert lock.took_over is True
            assert warns and "接管" in warns[0]
            assert json.loads(path.read_text(encoding="utf-8"))["pid"] == os.getpid()
        finally:
            lock.release()
    finally:
        proc.kill()
        proc.wait()


def test_k41_same_process_creation_blocks(base_dir):
    """同进程创建时间与锁内 create_time 一致 → 判存活持锁（不接管）。"""
    proc = _spawn_sleeper()
    try:
        creation = lockmod.process_creation_epoch(proc.pid)
        path = lockmod.lock_path_of(base_dir, "示例书名")
        path.write_text(json.dumps({"pid": proc.pid, "create_time": creation,
                                    "ts": "x"}), encoding="utf-8")
        with pytest.raises(lockmod.LockError, match="存活"):
            lockmod.acquire(base_dir, "示例书名")
    finally:
        proc.kill()
        proc.wait()


def test_case_collision_detection(base_dir):
    (base_dir / "Foo 书").mkdir()
    assert lockmod.case_collision(base_dir, "foo 书") == "Foo 书"
    assert lockmod.case_collision(base_dir, "Foo 书") is None
    assert lockmod.case_collision(base_dir, "别的书") is None
