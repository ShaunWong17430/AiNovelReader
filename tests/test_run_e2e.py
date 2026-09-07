"""阶段 4 出口测试 13/20/42/43/44：run.py 端到端（§12.3）。

- 13.  FakeLLM 跑通 12 片（batch_size=5）→ 末轮小结后立即 done；13 片收尾小结
      正常；--purge 清快照（done 后 .rollback 无 batch_*）；
- 20.  批次号解耦（J1+K1+K18）：processed 推导范围、快照 id = current_batch+1、
      recover 后从 .rollback 最大 id 推导；
- 42.  K31：全新 ROOT（无 metadata）首次 run --once → 自动 init 成功并推进，
      全程无 metadata.corrupt_* 产生；
- 43.  K33：timeline_window=10、首片（processed=0）→ reader 正常调用
      （chunk-start 钳制为 1、tail 空 → prompt 时间线段为空、summary「（无前情）」）；
- 44.  K42：自动 init 落盘运行参数 == §1.3 示例默认值。
"""
from __future__ import annotations

import json

from conftest import DEFAULT_NOVEL_NAME
from helpers import init_novel_direct, make_chunks, run_cli, write_config

N = DEFAULT_NOVEL_NAME


def test_13_full_book_12chunks_to_done(base_dir):
    """12 片 batch=5 → 三批（5/5/2）→ 收尾小结 → 立即 done + purge。"""
    cfg = write_config(base_dir, backend="fake", retry_backoff_s=[0, 0, 0])
    root = base_dir / N
    make_chunks(root / "chunks", 12)
    r = run_cli(["run.py", "run", "--novel", N], base=base_dir, config=cfg)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "STATUS=done" in r.stdout
    meta = json.loads((root / "metadata.json").read_text(encoding="utf-8"))
    assert meta["status"] == "done"
    assert meta["processed_chunks"] == 12
    assert meta["summary_chunks"] == 12
    assert meta["current_batch"] == 3
    # done 后 purge 清空快照（K32）
    rb = root / ".rollback"
    assert not list(rb.glob("batch_*.md")), "done 后应 purge 全部快照"
    # 时间线 12 条数据行（每片 1 事件）
    tl = (root / "plot_timeline.md").read_text(encoding="utf-8")
    data_rows = [ln for ln in tl.splitlines()
                 if ln.startswith("| ") and not ln.startswith("|--")
                 and not ln.startswith("| 顺序")]
    assert len(data_rows) == 12
    # notes 三个批次文件
    assert sorted(p.name for p in (root / "notes").glob("*.md")) == [
        "part_001~part_005.md", "part_006~part_010.md",
        "part_011~part_012.md"]
    # summary.md 已写（收尾小结）
    assert (root / "summary.md").read_text(encoding="utf-8").strip()


def test_13_thirteen_chunks_tail_summary(base_dir):
    """13 片：批 5/5/3 → 收尾小结正常 done。"""
    cfg = write_config(base_dir, backend="fake", retry_backoff_s=[0, 0, 0])
    root = base_dir / N
    make_chunks(root / "chunks", 13)
    r = run_cli(["run.py", "run", "--novel", N], base=base_dir, config=cfg)
    assert r.returncode == 0, r.stdout + r.stderr
    meta = json.loads((root / "metadata.json").read_text(encoding="utf-8"))
    assert meta["status"] == "done" and meta["processed_chunks"] == 13
    assert meta["summary_chunks"] == 13


def test_20_batch_id_decoupled_from_processed(base_dir):
    """J1+K1+K18：范围由 processed 推导（start=8,end=10），快照 id = current_batch+1=3。"""
    cfg = write_config(base_dir, backend="fake", retry_backoff_s=[0, 0, 0])
    root = base_dir / N
    make_chunks(root / "chunks", 12)
    # init（batch_size=3）
    r = run_cli(["run.py", "init", "--novel", N, "--batch-size", "3"],
                base=base_dir, config=cfg)
    assert r.returncode == 0, r.stdout
    # 构造 processed=7 / current_batch=2 的现场：7 行时间线 + .rollback/batch_2_read
    tl = root / "plot_timeline.md"
    rows = []
    for i in range(1, 8):
        rows.append(f"| {i} | part_{i:03d} | 事件{i} | 影响{i} |")
    tl_body = ("# 情节时间线\n\n| 顺序 | 分片 | 事件 | 影响 |\n"
               "|------|------|------|------|\n" + "\n".join(rows) + "\n")
    tl.write_text(tl_body, encoding="utf-8", newline="\n")     # 强制 LF（§1.4）
    rb = root / ".rollback"
    rb.mkdir(exist_ok=True)
    (rb / "batch_2_read_timeline.md").write_text(tl_body, encoding="utf-8",
                                                 newline="\n")
    (root / "metadata.json").unlink()          # recover 前置：metadata 缺失
    r = run_cli(["run.py", "init", "--novel", N, "--recover"],
                base=base_dir, config=cfg)
    assert r.returncode == 0, r.stdout
    meta = json.loads((root / "metadata.json").read_text(encoding="utf-8"))
    assert meta["processed_chunks"] == 7 and meta["current_batch"] == 2
    # run --once：批 3 = [8,10]，batch_id=3（K1 current_batch+1，与 processed 解耦）
    r = run_cli(["run.py", "run", "--once", "--novel", N], base=base_dir,
                config=cfg)
    assert r.returncode == 0, r.stdout + r.stderr
    meta = json.loads((root / "metadata.json").read_text(encoding="utf-8"))
    assert meta["processed_chunks"] == 10 and meta["current_batch"] == 3
    assert (rb / "batch_3_read_timeline.md").exists()   # 快照 id=3
    tl_text = (root / "plot_timeline.md").read_text(encoding="utf-8")
    assert "part_008" in tl_text and "part_009" in tl_text and "part_010" in tl_text
    assert "part_011" not in tl_text                    # 未越批


def test_42_k31_first_run_auto_init(base_dir):
    """K31：无 metadata 首跑 run --once → 自动 init + 推进，无 corrupt 文件。"""
    cfg = write_config(base_dir, backend="fake", retry_backoff_s=[0, 0, 0])
    root = base_dir / N
    make_chunks(root / "chunks", 5)
    r = run_cli(["run.py", "run", "--once", "--novel", N], base=base_dir,
                config=cfg)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not list(root.glob("metadata.corrupt_*")), "K31 不得 emergency"
    meta = json.loads((root / "metadata.json").read_text(encoding="utf-8"))
    assert meta["status"] == "running"          # once 一轮：批 1 提交后 running
    assert meta["processed_chunks"] == 5
    assert "自动 init" in (root / "run.log").read_text(encoding="utf-8")


def test_44_k42_auto_init_defaults(base_dir):
    """K42：自动 init 落盘运行参数 == §1.3 示例默认值。"""
    cfg = write_config(base_dir, backend="fake", retry_backoff_s=[0, 0, 0])
    root = base_dir / N
    make_chunks(root / "chunks", 5)
    r = run_cli(["run.py", "run", "--once", "--novel", N], base=base_dir,
                config=cfg)
    assert r.returncode == 0, r.stdout + r.stderr
    meta = json.loads((root / "metadata.json").read_text(encoding="utf-8"))
    assert meta["batch_size"] == 5
    assert meta["timeline_window"] == 10
    assert meta["summary_max"] == 5000
    assert meta["chunk_max_chars"] == 20000
    assert meta["chunk_padding"] == 3
    assert meta["prompt_version"] == "v1"


def test_43_k33_first_chunk_tail_empty(base_dir, monkeypatch):
    """K33：首片（processed=0）→ chunk-start 钳制 1、tail 空、「（无前情）」。"""
    import run as runmod
    import core.reader as readermod
    cfg = write_config(base_dir, backend="fake", retry_backoff_s=[0, 0, 0])
    root = base_dir / N
    make_chunks(root / "chunks", 5)
    init_novel_direct(base_dir, cfg, n_chunks=5)   # 进程内 init
    monkeypatch.setenv("NOVEL_BASE", str(base_dir))
    monkeypatch.setenv("NOVEL_CONFIG", str(cfg))
    captured: list[str] = []
    real_chat = readermod.chat

    def spy(**kw):
        captured.append(kw["prompt"])
        return real_chat(**kw)

    monkeypatch.setattr(readermod, "chat", spy)
    flag = runmod._StopFlag()
    rc = runmod._run_loop(base_dir, cfg, N, once=True, max_rounds=None, flag=flag)
    assert rc == 0
    assert captured, "应产生 reader prompt"
    first = captured[0]
    assert "（无前情）" in first                 # summary.md 空 → 占位
    assert "## 时间线（前情事件）" in first
    tail_section = first.split("## 时间线（前情事件）")[1].split("## 本片正文")[0]
    assert "| 1 |" not in tail_section           # processed=0 → tail 空（K33 钳制后空范围）
