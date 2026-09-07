"""阶段 4 出口测试 14/32/34：续跑、优雅中断、停滞检测（§12.3/§12.4）。

- 14. 片级续跑：part_003 后中断 → 重启只发 part_004/005（llm.log 验证）；
- 32. K26 优雅中断：stop_flag 置位 → 当前片完成后退出、status 保持 running、
      .batch 已产出片保留、重启续跑；信号处理器安装冒烟（Windows CTRL_CLOSE）；
- 34. 停滞检测：run.log 尾部连续 2 条 entry processed 相同 → WARN 不 error
      （§6.5 倒序取 2 条）。
"""
from __future__ import annotations

import json

import pytest

import core.reader as readermod
import core.state as statemod
from conftest import DEFAULT_NOVEL_NAME
from helpers import init_novel_direct, write_config
from core.config import load_config

N = DEFAULT_NOVEL_NAME


@pytest.fixture(autouse=True)
def _fake_isolate():
    import core.llm_client as llm
    llm.clear_faults()
    yield
    llm.reset_fake()


def _env(base, config, monkeypatch, n=12):
    init_novel_direct(base, config, n_chunks=n)
    monkeypatch.setenv("NOVEL_BASE", str(base))
    monkeypatch.setenv("NOVEL_CONFIG", str(config))
    return load_config()


def _meta(base):
    return json.loads((base / N / "metadata.json").read_text(encoding="utf-8"))


def _llm_chunks(base):
    log = base / N / "llm.log"
    if not log.exists():
        return []
    return [json.loads(ln)["chunk"] for ln in
            log.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _run_once(base, cfg, *, once=True, max_rounds=None, flag=None):
    import run as runmod
    if flag is None:
        flag = runmod._StopFlag()
    return runmod._run_loop(base, cfg, N, once=once, max_rounds=max_rounds,
                            flag=flag)


def test_14_chunk_level_resume(base_dir, monkeypatch):
    """part_003 后中断 → 重启只发 part_004/005（已产出片不重调，K5）。"""
    import run as runmod
    cfg = write_config(base_dir, backend="fake", retry_backoff_s=[0, 0, 0])
    _env(base_dir, cfg, monkeypatch)
    flag = runmod._StopFlag()
    real_chat = readermod.chat
    state = {"n": 0}

    def spy(**kw):
        state["n"] += 1
        if state["n"] >= 3:                       # part_003 调用后置位
            flag.set()
        return real_chat(**kw)

    monkeypatch.setattr(readermod, "chat", spy)
    rc = _run_once(base_dir, cfg, flag=flag)      # 中断轮
    assert rc == 0
    meta = _meta(base_dir)
    assert meta["status"] == "running"            # 优雅中断保持 running
    assert meta["processed_chunks"] == 0          # 未提交
    root = base_dir / N
    batch_files = sorted(p.name for p in (root / ".batch").glob("*.json"))
    assert batch_files == ["part_001.json", "part_002.json", "part_003.json"]
    # 重启：只补 part_004/005
    rc2 = _run_once(base_dir, cfg, max_rounds=1)  # 非 once：最多 1 轮
    assert rc2 == 0
    assert _meta(base_dir)["processed_chunks"] == 5
    chunks = _llm_chunks(base_dir)
    assert chunks == ["part_001", "part_002", "part_003",
                      "part_004", "part_005"]     # 第二轮只发 4/5


def test_32_elegant_interrupt_and_resume(base_dir, monkeypatch):
    """K26：stop_flag → 当前片完成退出；重启续 part_004（run --once 语义）。"""
    import run as runmod
    cfg = write_config(base_dir, backend="fake", retry_backoff_s=[0, 0, 0])
    _env(base_dir, cfg, monkeypatch)
    flag = runmod._StopFlag()
    real_chat = readermod.chat
    state = {"n": 0}

    def spy(**kw):
        state["n"] += 1
        if state["n"] == 2:                       # part_002 完成后置位
            flag.set()
        return real_chat(**kw)

    monkeypatch.setattr(readermod, "chat", spy)
    rc = _run_once(base_dir, cfg, flag=flag)
    assert rc == 0
    meta = _meta(base_dir)
    assert meta["status"] == "running"            # 不置 error、不推进
    assert meta["processed_chunks"] == 0
    # 重启（--once 一轮）→ 批 1 补齐 part_003..005 完成
    rc2 = _run_once(base_dir, cfg)
    assert rc2 == 0
    assert _meta(base_dir)["processed_chunks"] == 5


def test_32_install_interrupt_handlers_smoke():
    """K26 处理器安装冒烟（SIGINT/SIGBREAK + CTRL_CLOSE，Windows 不抛）。"""
    import run as runmod
    flag = runmod._StopFlag()
    runmod.install_interrupt_handlers(flag)       # 不抛异常即可
    assert not flag()


def test_34_stall_detection_from_runlog(base_dir):
    """§6.5：倒序最近连续 2 条 entry processed 相同 → 停滞。"""
    import run as runmod
    log = base_dir / N / "run.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    entry = ("[2026-01-01T12:00:00+08:00] [driver] [entry] [batch=null] [OK] "
             "processed={} status=running\n")
    log.write_text(entry.format(5) + entry.format(5), encoding="utf-8",
                   newline="\n")
    stalled, val = runmod.detect_stall(log)
    assert stalled is True and val == 5
    # 不同 → 不停滞
    log.write_text(entry.format(4) + entry.format(5), encoding="utf-8",
                   newline="\n")
    assert runmod.detect_stall(log)[0] is False
    # 不足 2 条 → 不停滞
    log.write_text(entry.format(5), encoding="utf-8", newline="\n")
    assert runmod.detect_stall(log)[0] is False


def test_34_stall_warns_but_not_error(base_dir, monkeypatch):
    """连续 2 轮 processed 不变 → WARN 不 error（集成）。"""
    cfg = write_config(base_dir, backend="fake", retry_backoff_s=[0, 0, 0])
    _env(base_dir, cfg, monkeypatch)
    statemod.update_metadata(base_dir / N / "metadata.json", base=base_dir,
                             processed=5)          # 预置 processed=5
    state = {"n": 0}

    def fake_pb(*a, **kw):                        # 恒谎报不推进（停滞模拟）
        state["n"] += 1
        return {"kind": "committed", "processed": 5}

    monkeypatch.setattr(readermod, "process_batch", fake_pb)
    rc = _run_once(base_dir, cfg, once=False, max_rounds=3)
    assert rc == 0
    assert state["n"] >= 2                        # 至少两轮谎报
    runlog = (base_dir / N / "run.log").read_text(encoding="utf-8")
    assert "可能停滞" in runlog                   # WARN
    assert _meta(base_dir)["status"] != "error"   # 不置 error
