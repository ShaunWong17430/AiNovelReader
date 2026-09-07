"""阶段 4 出口测试 15/16/17/21/22/23/24/35/45/48/49：故障注入（§12.3/§12.4）。

进程内驱动 run._run_loop（once），monkeypatch core 函数/注入 FakeLLM 故障：
- 15. 预检三态（NOT_FOUND 正常 / APPLIED 快路径不调 LLM / PARTIAL → error 不改文件）；
- 16. K21：.summary_applied==processed 且 summary_chunks<processed → 续跑补推进；
- 17. K22：批内第 3 片 PARSE 失败 → rollback 后回步骤 3 重建队列，前 2 片不重调；
- 21. backup 非零 → 立即 error 且无新增 LLM（零容忍）；
- 22. append「成功落盘但返回非零」→ 先 rollback 再重跑，无重复行；
- 23. K24：rollback 非零 → 置 error、不继续重跑、报告含快照/phase；
- 24. 批双重失败 → error、batch_retries>=2（K8）；
- 35. 脚本崩溃（RuntimeError，非 0/2/3/4）→ 当轮重试 1 次 → 成功；仍失败 → error；
- 45. K40：时间线「未来行」→ 预检 3 → error 报告，不走 APPLIED 快路径；
- 48. K35：summarizer NETWORK 耗尽 → summary_retries 路径 + 陈旧草稿先删；
      summarizer AUTH → 立即 error 且 summary_retries 仍 0；
- 49. K39：verify 退出码 2 → 立即 error、不计入批级重试、不回滚。
"""
from __future__ import annotations

import json

import pytest

import core.llm_client as llm
import core.reader as readermod
import core.recovery as recovery
import core.state as statemod
import core.summarizer as summarizermod
import core.timeline as timelinemod
from conftest import DEFAULT_NOVEL_NAME
from helpers import init_novel_direct, make_chunks, write_config
from core.config import load_config

N = DEFAULT_NOVEL_NAME


@pytest.fixture(autouse=True)
def _fake_isolate():
    llm.clear_faults()
    yield
    llm.reset_fake()


def _env(base, config, monkeypatch, n=12):
    init_novel_direct(base, config, n_chunks=n)
    monkeypatch.setenv("NOVEL_BASE", str(base))
    monkeypatch.setenv("NOVEL_CONFIG", str(config))
    return load_config()


def _run_once(base, cfg, novel=N):
    import run as runmod
    return runmod._run_loop(base, cfg, novel, once=True, max_rounds=None,
                            flag=runmod._StopFlag())


def _meta(base):
    return json.loads((base / N / "metadata.json").read_text(encoding="utf-8"))


def _llm_log(base):
    log = base / N / "llm.log"
    if not log.exists():
        return []
    return [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines()
            if ln.strip()]


def _llm_counts(base):
    return [e["chunk"] for e in _llm_log(base)]


def _llm_phases(base):
    return [e["phase"] for e in _llm_log(base)]


def _tl_rows(base):
    tl = (base / N / "plot_timeline.md").read_text(encoding="utf-8")
    return [ln for ln in tl.splitlines()
            if ln.startswith("| ") and not ln.startswith("|--")
            and not ln.startswith("| 顺序")]


# ---------------------------------------------------------------------------
# 测试 15：预检三态
# ---------------------------------------------------------------------------

def test_15_precheck_not_found_runs_normally(base_dir, monkeypatch):
    cfg = write_config(base_dir, backend="fake", retry_backoff_s=[0, 0, 0])
    _env(base_dir, cfg, monkeypatch)
    rc = _run_once(base_dir, cfg)
    assert rc == 0
    assert _meta(base_dir)["processed_chunks"] == 5


def test_15_precheck_applied_fastpath_no_llm(base_dir, monkeypatch):
    """APPLIED → 快路径补 notes/commit，不调 LLM。"""
    cfg = write_config(base_dir, backend="fake", retry_backoff_s=[0, 0, 0])
    _env(base_dir, cfg, monkeypatch, n=5)
    root = base_dir / N
    # 构造时间线含批 1 全部片（模拟已 APPLIED）
    rows = [f"| {i} | part_{i:03d} | 事件{i} | 影响{i} |" for i in range(1, 6)]
    tl_body = ("# 情节时间线\n\n| 顺序 | 分片 | 事件 | 影响 |\n"
               "|------|------|------|------|\n" + "\n".join(rows) + "\n")
    (root / "plot_timeline.md").write_text(tl_body, encoding="utf-8", newline="\n")
    statemod.update_metadata(root / "metadata.json", base=base_dir,
                             status="running")
    rc = _run_once(base_dir, cfg)
    assert rc == 0
    assert _meta(base_dir)["processed_chunks"] == 5
    assert "read" not in _llm_phases(base_dir)   # 快路径不调 reader（K5/P2-10）


def test_15_precheck_partial_errors_no_change(base_dir, monkeypatch):
    """PARTIAL → 置 error、不改文件。"""
    cfg = write_config(base_dir, backend="fake", retry_backoff_s=[0, 0, 0])
    _env(base_dir, cfg, monkeypatch, n=5)
    root = base_dir / N
    tl_body = ("# 情节时间线\n\n| 顺序 | 分片 | 事件 | 影响 |\n"
               "|------|------|------|------|\n"
               "| 1 | part_001 | 事件 | 影响 |\n")      # 仅 1 片 → 部分命中
    (root / "plot_timeline.md").write_text(tl_body, encoding="utf-8", newline="\n")
    statemod.update_metadata(root / "metadata.json", base=base_dir,
                             status="running")
    rc = _run_once(base_dir, cfg)
    assert rc == 3
    meta = _meta(base_dir)
    assert meta["status"] == "error"
    err = meta["error"] or ""
    assert ("PARTIAL" in err) or ("预检失败" in err)   # 部分命中 → 预检异常 → error
    assert "read" not in _llm_phases(base_dir)   # 不改文件、不调 LLM
    assert len(_tl_rows(base_dir)) == 1          # 时间线未被改动


# ---------------------------------------------------------------------------
# 测试 16：K21 自愈（.summary_applied==processed 且 summary_chunks<processed）
# ---------------------------------------------------------------------------

def test_16_k21_marker_self_heal(base_dir, monkeypatch):
    cfg = write_config(base_dir, backend="fake", retry_backoff_s=[0, 0, 0])
    _env(base_dir, cfg, monkeypatch, n=5)
    root = base_dir / N
    # processed=5（提交批 1）
    statemod.update_metadata(root / "metadata.json", base=base_dir,
                             processed=5)
    # 构造 K21 现场：summary.md 已写 + .summary_applied==processed，但 summary_chunks=0
    (root / "summary.md").write_text("已落盘的小结文本，单段不超过上限。",
                                     encoding="utf-8", newline="\n")
    (root / ".summary_applied").write_text("5", encoding="utf-8")
    rc = _run_once(base_dir, cfg)
    assert rc == 0
    meta = _meta(base_dir)
    assert meta["summary_chunks"] == 5           # K21 补推进，不撞严格单调
    assert meta["status"] == "running"
    assert _llm_counts(base_dir) == []           # 不重调 summarizer
    assert not (root / ".summary_applied").exists()   # P2-9 清理


# ---------------------------------------------------------------------------
# 测试 17：K22 重建队列（批内片失败 → 回步骤 3，已产出片不重调）
# ---------------------------------------------------------------------------

def test_17_k22_requeue_after_chunk_failure(base_dir, monkeypatch):
    cfg = write_config(base_dir, backend="fake", retry_backoff_s=[0, 0, 0])
    _env(base_dir, cfg, monkeypatch, n=12)
    # part_003：阶段 A + 阶段 B 都坏 JSON → 该片失败 → 整批失败 → 重跑恢复
    llm.inject_fault("reader:part_003", {"kind": "PARSE", "attempts": 2})
    rc = _run_once(base_dir, cfg)
    assert rc == 0, _meta(base_dir).get("error")
    assert _meta(base_dir)["processed_chunks"] == 5
    counts = _llm_counts(base_dir)
    assert counts.count("part_001") == 1
    assert counts.count("part_002") == 1
    assert counts.count("part_003") == 3        # 阶段A+B 失败 2 次 + 重跑 1 次
    assert counts.count("part_004") == 1
    assert counts.count("part_005") == 1
    # 时间线无重复行（rollback 恢复后重新 append）
    assert len(_tl_rows(base_dir)) == 5


# ---------------------------------------------------------------------------
# 测试 21：backup 零容忍
# ---------------------------------------------------------------------------

def test_21_backup_failure_immediate_error_no_llm(base_dir, monkeypatch):
    cfg = write_config(base_dir, backend="fake", retry_backoff_s=[0, 0, 0])
    _env(base_dir, cfg, monkeypatch)

    def boom(base, novel_name, **kw):
        raise recovery.BackupError("注入 backup 失败（K23 损坏）", 2)

    monkeypatch.setattr(recovery, "backup", boom)
    rc = _run_once(base_dir, cfg)
    assert rc == 3
    meta = _meta(base_dir)
    assert meta["status"] == "error"
    assert "backup" in (meta["error"] or "")
    assert _llm_counts(base_dir) == []           # 无新增 LLM


# ---------------------------------------------------------------------------
# 测试 22：append 禁重试（成功落盘但返回非零 → 先 rollback 再重跑，无重复行）
# ---------------------------------------------------------------------------

def test_22_append_failure_rollback_then_rerun(base_dir, monkeypatch):
    cfg = write_config(base_dir, backend="fake", retry_backoff_s=[0, 0, 0])
    _env(base_dir, cfg, monkeypatch)
    real_append = timelinemod.append_batch
    state = {"n": 0}

    def flaky_append(*a, **kw):
        real_append(*a, **kw)                   # 成功落盘
        state["n"] += 1
        if state["n"] == 1:
            raise timelinemod.TimelineError("注入：成功落盘但返回非零")

    monkeypatch.setattr(timelinemod, "append_batch", flaky_append)
    rc = _run_once(base_dir, cfg)
    assert rc == 0, _meta(base_dir).get("error")
    meta = _meta(base_dir)
    assert meta["processed_chunks"] == 5
    # 重跑不重调 LLM（.batch 已产出片跳过），时间线无重复行
    assert len(_llm_counts(base_dir)) == 5
    assert len(_tl_rows(base_dir)) == 5


# ---------------------------------------------------------------------------
# 测试 23：K24 rollback 零容忍
# ---------------------------------------------------------------------------

def test_23_k24_rollback_failure_immediate_error(base_dir, monkeypatch):
    cfg = write_config(base_dir, backend="fake", retry_backoff_s=[0, 0, 0])
    _env(base_dir, cfg, monkeypatch)
    llm.inject_fault("reader:part_003", {"kind": "PARSE", "attempts": 2})

    def boom(base, novel_name, **kw):
        raise recovery.RollbackError("注入 rollback 失败", 3)

    monkeypatch.setattr(recovery, "rollback", boom)
    rc = _run_once(base_dir, cfg)
    assert rc == 3
    meta = _meta(base_dir)
    assert meta["status"] == "error"
    assert "K24" in (meta["error"] or "")       # 报告含快照/phase 语义
    assert meta["batch_retries"] == 0           # 不回滚则不计 retries
    counts = _llm_counts(base_dir)
    # K24：不继续重跑——part_003 仅阶段 A+B 两次调用，无第 3 次重跑
    assert counts.count("part_003") == 2
    assert "part_004" not in counts             # 立即中止，未继续逐片


# ---------------------------------------------------------------------------
# 测试 24：批双重失败（K8）
# ---------------------------------------------------------------------------

def test_24_batch_double_failure_error(base_dir, monkeypatch):
    cfg = write_config(base_dir, backend="fake", retry_backoff_s=[0, 0, 0])
    _env(base_dir, cfg, monkeypatch)
    llm.inject_fault("reader:part_003", {"kind": "PARSE", "attempts": 99})
    rc = _run_once(base_dir, cfg)
    assert rc == 3
    meta = _meta(base_dir)
    assert meta["status"] == "error"
    assert meta["batch_retries"] == 2           # K8：到 2 置 error
    assert "K8" in (meta["error"] or "")


# ---------------------------------------------------------------------------
# 测试 35：脚本崩溃当轮重试 1 次
# ---------------------------------------------------------------------------

def test_35_crash_retry_once_then_succeed(base_dir, monkeypatch):
    cfg = write_config(base_dir, backend="fake", retry_backoff_s=[0, 0, 0])
    _env(base_dir, cfg, monkeypatch)
    real_pb = readermod.process_batch
    state = {"n": 0}

    def flaky(*a, **kw):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("注入脚本崩溃")
        return real_pb(*a, **kw)

    monkeypatch.setattr(readermod, "process_batch", flaky)
    rc = _run_once(base_dir, cfg)
    assert rc == 0
    assert _meta(base_dir)["processed_chunks"] == 5
    runlog = (base_dir / N / "run.log").read_text(encoding="utf-8")
    assert "当轮重试 1 次" in runlog


def test_35_crash_retry_then_error(base_dir, monkeypatch):
    cfg = write_config(base_dir, backend="fake", retry_backoff_s=[0, 0, 0])
    _env(base_dir, cfg, monkeypatch)

    def always_crash(*a, **kw):
        raise RuntimeError("恒崩")

    monkeypatch.setattr(readermod, "process_batch", always_crash)
    rc = _run_once(base_dir, cfg)
    assert rc == 3
    meta = _meta(base_dir)
    assert meta["status"] == "error"
    assert "当轮重试仍异常" in (meta["error"] or "")


# ---------------------------------------------------------------------------
# 测试 45：K40 未来行端到端
# ---------------------------------------------------------------------------

def test_45_k40_future_row_errors(base_dir, monkeypatch):
    cfg = write_config(base_dir, backend="fake", retry_backoff_s=[0, 0, 0])
    _env(base_dir, cfg, monkeypatch, n=5)
    root = base_dir / N
    # 篡改：时间线加入序号 > end 的「未来行」（part_099）
    tl_body = ("# 情节时间线\n\n| 顺序 | 分片 | 事件 | 影响 |\n"
               "|------|------|------|------|\n"
               "| 1 | part_099 | 未来行 | 篡改 |\n")
    (root / "plot_timeline.md").write_text(tl_body, encoding="utf-8", newline="\n")
    statemod.update_metadata(root / "metadata.json", base=base_dir,
                             status="running")
    rc = _run_once(base_dir, cfg)
    assert rc == 3
    meta = _meta(base_dir)
    assert meta["status"] == "error"
    err = meta["error"] or ""
    assert ("K40" in err) or ("未来行" in err) or ("99" in err)
    assert meta["processed_chunks"] == 0        # 不走 APPLIED 快路径


# ---------------------------------------------------------------------------
# 测试 48：K35 小结失败分支
# ---------------------------------------------------------------------------

def _reach_tail_summary(base, cfg):
    """跑批 1+2（processed=10），为收尾小结失败做准备。"""
    rc = _run_once(base, cfg)
    assert rc == 0
    rc = _run_once(base, cfg)
    assert rc == 0
    assert _meta(base)["processed_chunks"] == 10


def test_48_k35_network_exhausted_summary_retries(base_dir, monkeypatch):
    cfg = write_config(base_dir, backend="fake", retry_backoff_s=[0, 0, 0])
    _env(base_dir, cfg, monkeypatch, n=12)
    _reach_tail_summary(base_dir, cfg)
    root = base_dir / N
    (root / "summary_draft.txt").write_text("陈旧草稿（K35 应先删）",
                                            encoding="utf-8")
    llm.inject_fault("summarizer:1-12", {"kind": "NETWORK", "attempts": 99})
    rc = _run_once(base_dir, cfg)
    assert rc == 3
    meta = _meta(base_dir)
    assert meta["status"] == "error"
    assert meta["summary_retries"] == 2         # K8：小结到 2 置 error
    assert not (root / "summary_draft.txt").exists()   # K35：陈旧草稿已先删


def test_48_k35_auth_fatal_no_retries(base_dir, monkeypatch):
    cfg = write_config(base_dir, backend="fake", retry_backoff_s=[0, 0, 0])
    _env(base_dir, cfg, monkeypatch, n=12)
    _reach_tail_summary(base_dir, cfg)
    llm.inject_fault("summarizer:1-12", {"kind": "AUTH", "attempts": 1})
    rc = _run_once(base_dir, cfg)
    assert rc == 3
    meta = _meta(base_dir)
    assert meta["status"] == "error"
    assert meta["summary_retries"] == 0         # FATAL：不消耗 retries
    assert "FATAL AUTH" in (meta["error"] or "")


# ---------------------------------------------------------------------------
# 测试 49：K39 verify 退出码 2 → 立即 error
# ---------------------------------------------------------------------------

def test_49_k39_verify_exit2_immediate_error(base_dir, monkeypatch):
    cfg = write_config(base_dir, backend="fake", retry_backoff_s=[0, 0, 0])
    _env(base_dir, cfg, monkeypatch)

    def env_error(**kw):
        raise readermod.ProcessError(2, "注入 verify_timeline 环境错误（K39）")

    monkeypatch.setattr(timelinemod, "check_only", env_error)
    rc = _run_once(base_dir, cfg)
    assert rc == 3
    meta = _meta(base_dir)
    assert meta["status"] == "error"
    assert meta["batch_retries"] == 0           # 不计入批级重试
    assert not list((base_dir / N / ".rollback").glob("batch_1_read_*.md")), \
        "K39：不回滚"
    assert _llm_counts(base_dir) == []          # 未开始逐片
