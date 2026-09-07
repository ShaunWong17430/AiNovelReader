"""阶段 4 出口测试 36/37/38/47 + 阶段 5 勘误测试 51：锁范围、K15 降级、
分片集合变更、K32 purge、K43 批内接力。

- 36. K25：status / validate / --dry-run 不加锁可运行（残留锁存在亦不受影响）；
      run 加锁（残留锁接管）；
- 37. K15：chunk 字数统计失败 → notes 仍生成（字数「未知」+ 警告）；
      render_notes 完全无法生成 → 验收失败（回滚重跑 → 仍失败 → error）；
- 38. 分片集合运行期变更（chunks 数量 != total）→ 预检 3 → error 报告；
- 47. K32：done 后 purge 失败 → log WARN、退出码 0、status 保持 done；
- 51. K43 批内接力（v3.2.2 勘误）：第 N 片 prompt 注入本批已读前片事件；
      .batch 损坏/缺 events → 跳过注入不判失败（§15 条款 4）。
"""
from __future__ import annotations

import json

import pytest

import core.recovery as recovery
import core.renderer as rendermod
import core.state as statemod
from conftest import DEFAULT_NOVEL_NAME
from helpers import init_novel_direct, make_chunks, run_cli, write_config
from core.config import load_config

N = DEFAULT_NOVEL_NAME


@pytest.fixture(autouse=True)
def _fake_isolate():
    import core.llm_client as llm
    llm.clear_faults()
    yield
    llm.reset_fake()


def _env(base, config, monkeypatch, n=5):
    init_novel_direct(base, config, n_chunks=n)
    monkeypatch.setenv("NOVEL_BASE", str(base))
    monkeypatch.setenv("NOVEL_CONFIG", str(config))
    return load_config()


def _meta(base):
    return json.loads((base / N / "metadata.json").read_text(encoding="utf-8"))


def _run_once(base, cfg, *, once=True, max_rounds=None):
    import run as runmod
    return runmod._run_loop(base, cfg, N, once=once, max_rounds=max_rounds,
                            flag=runmod._StopFlag())


# ---------------------------------------------------------------------------
# 测试 36：K25 锁范围
# ---------------------------------------------------------------------------

def test_36_status_validate_dryrun_no_lock(base_dir):
    """status/validate/--dry-run 不加锁：残留锁存在也能运行。"""
    cfg = write_config(base_dir, backend="fake", retry_backoff_s=[0, 0, 0])
    root = base_dir / N
    make_chunks(root / "chunks", 5)
    r = run_cli(["run.py", "init", "--novel", N], base=base_dir, config=cfg)
    assert r.returncode == 0, r.stdout
    # 放置残留锁（pid 不存在）
    import json as _json
    lock = base_dir / f".{N}.run.lock"
    lock.write_text(_json.dumps({"pid": 999999999, "create_time": 0.0,
                                 "ts": "2026-01-01T00:00:00+08:00"}),
                    encoding="utf-8")
    # status / validate / dry-run 不加锁（K25）→ 可运行
    r = run_cli(["run.py", "status", "--novel", N], base=base_dir, config=cfg)
    assert r.returncode == 0 and "STATUS=init" in r.stdout
    r = run_cli(["run.py", "validate"], base=base_dir, config=cfg)
    assert r.returncode == 0 and "CONFIG=OK" in r.stdout
    r = run_cli(["run.py", "run", "--dry-run", "--novel", N], base=base_dir,
                config=cfg)
    assert r.returncode == 0 and "PLAN=start" in r.stdout   # init 态 → 预计启动
    # run 加锁：残留锁 pid 不存在 → 接管 WARN → 正常推进
    r = run_cli(["run.py", "run", "--once", "--novel", N], base=base_dir,
                config=cfg)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "WARN" in r.stderr                     # 接管警告
    assert _meta(base_dir)["processed_chunks"] == 5


def test_36_dry_run_writes_nothing(base_dir):
    """K6：--dry-run 不写任何文件（除锁外零写入）。"""
    cfg = write_config(base_dir, backend="fake", retry_backoff_s=[0, 0, 0])
    root = base_dir / N
    make_chunks(root / "chunks", 5)
    r = run_cli(["run.py", "run", "--dry-run", "--novel", N], base=base_dir,
                config=cfg)
    assert r.returncode == 0
    assert "PLAN=auto_init" in r.stdout           # metadata 缺失 → 预计自动 init
    assert not (root / "metadata.json").exists()  # 未写 metadata
    assert not list(base_dir.glob(f".{N}.run.lock"))  # 未加锁
    assert not (root / "run.log").exists()        # 未写日志


# ---------------------------------------------------------------------------
# 测试 37：K15 降级
# ---------------------------------------------------------------------------

def test_37_chunk_stats_failure_notes_still_generated(base_dir, monkeypatch):
    """chunk_stats 失败 → 字数「未知」+ 警告，notes 仍生成、批正常提交。"""
    cfg = write_config(base_dir, backend="fake", retry_backoff_s=[0, 0, 0])
    _env(base_dir, cfg, monkeypatch, n=5)

    def broken_count(*a, **kw):
        return None, ["K15 注入：字数统计失败"]

    monkeypatch.setattr(rendermod, "chunk_char_count", broken_count)
    rc = _run_once(base_dir, cfg)
    assert rc == 0
    assert _meta(base_dir)["processed_chunks"] == 5
    notes = (base_dir / N / "notes" / "part_001~part_005.md")
    assert notes.exists()
    assert "未知" in notes.read_text(encoding="utf-8")


def test_37_render_totally_fails_is_acceptance_failure(base_dir, monkeypatch):
    """render_notes 完全无法生成 → 验收失败（回滚重跑 → 仍失败 → error，K8）。"""
    cfg = write_config(base_dir, backend="fake", retry_backoff_s=[0, 0, 0])
    _env(base_dir, cfg, monkeypatch, n=5)

    def boom(*a, **kw):
        raise rendermod.RenderError("注入 render 完全无法生成")

    monkeypatch.setattr(rendermod, "render_notes_file", boom)
    rc = _run_once(base_dir, cfg)
    assert rc == 3
    meta = _meta(base_dir)
    assert meta["status"] == "error"
    assert meta["batch_retries"] == 2             # 重跑仍失败 → K8


# ---------------------------------------------------------------------------
# 测试 38：分片集合运行期变更
# ---------------------------------------------------------------------------

def test_38_chunks_collection_changed(base_dir, monkeypatch):
    """chunks 数量 != total_chunks（G13）→ 预检 3 → error 报告。"""
    cfg = write_config(base_dir, backend="fake", retry_backoff_s=[0, 0, 0])
    _env(base_dir, cfg, monkeypatch, n=5)
    (base_dir / N / "chunks" / "part_005.txt").unlink()   # 删一片
    rc = _run_once(base_dir, cfg)
    assert rc == 3
    meta = _meta(base_dir)
    assert meta["status"] == "error"
    assert "G13" in (meta["error"] or "")
    assert _meta(base_dir)["processed_chunks"] == 0


# ---------------------------------------------------------------------------
# 测试 47：K32 purge 例外
# ---------------------------------------------------------------------------

def test_47_k32_purge_failure_keeps_done(base_dir, monkeypatch):
    """done 后 purge 失败 → log WARN、退出码 0、status 保持 done（不得置 error）。"""
    cfg = write_config(base_dir, backend="fake", retry_backoff_s=[0, 0, 0])
    _env(base_dir, cfg, monkeypatch, n=5)
    root = base_dir / N
    statemod.update_metadata(root / "metadata.json", base=base_dir,
                             processed=5)
    statemod.update_metadata(root / "metadata.json", base=base_dir, summary=5)

    def boom(root, **kw):
        raise OSError("注入 purge 失败")

    monkeypatch.setattr(recovery, "purge_rollback", boom)
    rc = _run_once(base_dir, cfg)
    assert rc == 0
    meta = _meta(base_dir)
    assert meta["status"] == "done"               # K32：保持 done
    runlog = (root / "run.log").read_text(encoding="utf-8")
    assert "purge 失败（仅 WARN" in runlog


# ---------------------------------------------------------------------------
# K26 回归：ctypes 控制事件回调保活（防悬挂指针 0xC0000005 原生崩溃）
# ---------------------------------------------------------------------------

def test_k26_ctrl_handler_kept_alive(monkeypatch):
    """install_interrupt_handlers 后回调必须由 _CTRL_HANDLERS 保活。

    曾因回调对象被 GC 而崩溃：SetConsoleCtrlHandler 注册的函数指针悬挂，
    下次 Ctrl+C/关窗 → ACCESS_VIOLATION（无 traceback、残留锁）。此测试
    防回归：Windows 上安装后 _CTRL_HANDLERS 非空。
    """
    import os
    import run as runmod
    runmod._CTRL_HANDLERS.clear()
    flag = runmod._StopFlag()
    runmod.install_interrupt_handlers(flag)
    if os.name == "nt":
        assert runmod._CTRL_HANDLERS, "ctypes 回调必须保活（防悬挂指针崩溃）"
        # 保活的回调可被调用且置位 stop_flag（不抛异常）
        handler = runmod._CTRL_HANDLERS[0]
        assert handler(0) is True
        assert flag()
    else:
        assert runmod._CTRL_HANDLERS == []


# ---------------------------------------------------------------------------
# 测试 51：K43 批内接力（v3.2.2 勘误，§15 条款）
# ---------------------------------------------------------------------------

def test_51_batch_preview_injected(base_dir, monkeypatch):
    """K43 端到端：批 [1,5] 第 3/5 片 prompt 含前片事件行；首片无该段。"""
    import run as runmod
    import core.reader as readermod
    cfg = write_config(base_dir, backend="fake", retry_backoff_s=[0, 0, 0])
    root = base_dir / N
    make_chunks(root / "chunks", 5)
    _env(base_dir, cfg, monkeypatch, n=5)
    captured: list[tuple[str, str]] = []
    real_chat = readermod.chat

    def spy(**kw):
        captured.append((kw.get("chunk_id"), kw["prompt"]))
        return real_chat(**kw)

    monkeypatch.setattr(readermod, "chat", spy)
    rc = runmod._run_loop(base_dir, cfg, N, once=True, max_rounds=None,
                          flag=runmod._StopFlag())
    assert rc == 0
    ids = [cid for cid, _ in captured]
    assert ids[:5] == [f"part_{i:03d}" for i in range(1, 6)], ids
    # 首片：无批内接力段
    assert "本批已读前片事件" not in captured[0][1]
    # 第 3 片：含 part_001/part_002 事件行（K43 注入）
    p3 = captured[2][1]
    assert "## 本批已读前片事件（批内接力，仅作背景参考）" in p3
    sec3 = p3.split("## 本批已读前片事件")[1].split("## 本片正文")[0]
    assert "[part_001]" in sec3 and "[part_002]" in sec3
    # 第 5 片：含 part_001..004 全部前片
    p5 = captured[4][1]
    sec5 = p5.split("## 本批已读前片事件")[1].split("## 本片正文")[0]
    for i in range(1, 5):
        assert f"[part_{i:03d}]" in sec5


def test_51_batch_preview_skip_bad(base_dir):
    """K43 单元（§15 条款 4）：.batch 坏 JSON/缺 events → 跳过该片注入不抛异常；
    首片无注入；清洗半角 | → 全角。"""
    import core.reader as readermod
    cfg = write_config(base_dir, backend="fake", retry_backoff_s=[0, 0, 0])
    root = base_dir / N
    make_chunks(root / "chunks", 5)
    init_novel_direct(base_dir, cfg, n_chunks=5)
    (root / ".batch").mkdir(exist_ok=True)
    (root / ".batch" / "part_001.json").write_text(
        json.dumps({"chunk": "part_001",
                    "events": [{"event": "A 发生", "impact": "B 受影响"}],
                    "notes": {}}, ensure_ascii=False), encoding="utf-8")
    (root / ".batch" / "part_002.json").write_text("{bad json", encoding="utf-8")
    (root / ".batch" / "part_003.json").write_text(
        json.dumps({"chunk": "part_003", "notes": {}}, ensure_ascii=False),
        encoding="utf-8")
    meta = statemod.read_metadata(root / "metadata.json", base=base_dir)
    sec = readermod._batch_preview_section(root, meta, "part_004")
    assert "[part_001]" in sec                      # 合法 events 注入
    assert "part_002" not in sec                    # 坏 JSON 跳过
    assert "part_003" not in sec                    # 缺 events 跳过
    assert readermod._batch_preview_section(root, meta, "part_001") == ""
    # 清洗：半角 | → 全角 ｜
    (root / ".batch" / "part_001.json").write_text(
        json.dumps({"chunk": "part_001",
                    "events": [{"event": "A|B", "impact": "C"}],
                    "notes": {}}, ensure_ascii=False), encoding="utf-8")
    sec2 = readermod._batch_preview_section(root, meta, "part_002")
    assert "A｜B" in sec2
