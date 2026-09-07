"""出口测试 1–5、18、19、33、40 + §6.4 / §1.5 / §8.1：init / --recover / backup / rollback / purge。

CLI 级断言（退出码 §8.1：0/2/3/4）：
- 1.  12 片假小说 init 成功；GBK 片→4；".."/CON/结尾点/>100→2；" abc "→trim 通过；
- 2.  单片超限 → init 3（不落 metadata，D2）；
- 3.  配置非法/缺 key → 2；
- 4.  快照不含 key；run.log grep 不到 key；
- 5.  K28：summary_max=20000+summarizer 8000 → 2；chunk_max_chars=200000 → 2；
- 18. backup K23 三态（写/幂等覆写/损坏需 --force）；
- 19. --recover（K4 恒等式三出口、--summary-chunks、E4 前缀、顺序断裂、K11 快照、K18 上界）；
- 33. 并发 init 同书 → 后到者 3（锁）；K26 大小写碰撞 → 3；
- 40. K37：metadata 缺失但时间线含数据 → 普通 init 3 提示 --recover、不落任何文件；
      init --recover 正常恢复；
- 其余：I4 幂等分支、rollback 两相（K22 快照只恢复不删）、--purge（K32）、
  tail 空范围退出码 0、chunk_stats、append→verify CLI 闭环。
"""
from __future__ import annotations

import json

import core.lock as lockmod
import core.state as statemod
import core.timeline as tl

from conftest import DEFAULT_NOVEL_NAME
from core.state import atomic_write_json, atomic_write_text
from helpers import (init_novel_cli, make_chunks, make_reader_json, run_cli,
                     write_config)

NAME = DEFAULT_NOVEL_NAME
INIT = ["cli/init_novel.py", "--novel", NAME]
BACKUP = ["cli/backup.py", "--novel", NAME, "--batch", "1", "--phase", "read"]


def _init_ok(base, cfg, chunks: int = 5, extra=None) -> tuple:
    root = base / NAME
    make_chunks(root / "chunks", chunks)
    r = init_novel_cli(base, cfg, extra=extra)
    assert r.returncode == 0, f"init 失败: {r.stdout}{r.stderr}"
    return root, r


# ---------------------------------------------------------------------------
# 测试 1：init 成功 / 各失败档
# ---------------------------------------------------------------------------

def test_init_12_chunks_success(base_dir):
    cfg = write_config(base_dir)
    root, r = _init_ok(base_dir, cfg, chunks=12)
    assert "STATUS=OK" in r.stdout and "total=12" in r.stdout
    doc = statemod.read_metadata(root / "metadata.json", base=base_dir)
    assert (doc.total_chunks, doc.status, doc.chunk_padding) == (12, "init", 3)
    for sub in ("notes", ".rollback", ".batch"):
        assert (root / sub).is_dir()
    assert (root / "plot_timeline.md").read_text(encoding="utf-8") == tl.skeleton()
    assert (root / "summary.md").read_text(encoding="utf-8") == ""
    assert (root / "config.snapshot.json").exists()
    assert (root / "run.log").exists()


def test_init_gbk_chunk_exit4(base_dir):
    cfg = write_config(base_dir)
    root = base_dir / NAME
    make_chunks(root / "chunks", 3)
    (root / "chunks" / "part_002.txt").write_bytes("中文内容".encode("gbk"))
    r = init_novel_cli(base_dir, cfg)
    assert r.returncode == 4, r.stdout + r.stderr
    assert "编码" in r.stdout
    assert not (root / "metadata.json").exists()      # D2：不落 metadata


def test_init_bom_chunk_exit4(base_dir):
    cfg = write_config(base_dir)
    root = base_dir / NAME
    make_chunks(root / "chunks", 3, bom=True)
    r = init_novel_cli(base_dir, cfg)
    assert r.returncode == 4 and "BOM" in r.stdout
    assert not (root / "metadata.json").exists()


def test_init_bad_names_exit2(base_dir):
    cfg = write_config(base_dir)
    for name in ("..", "CON", "CON.txt", "book.", "x" * 101, "a/b", ""):
        r = init_novel_cli(base_dir, cfg, novel=name)
        assert r.returncode == 2, f"{name!r} -> {r.returncode}: {r.stdout}"


def test_init_trimmed_name(base_dir):
    cfg = write_config(base_dir)
    make_chunks(base_dir / "abc" / "chunks", 3)       # trim 后 root = BASE/abc
    r = init_novel_cli(base_dir, cfg, novel=" abc ")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (base_dir / "abc" / "metadata.json").exists()


def test_init_naming_gap_exit3(base_dir):
    cfg = write_config(base_dir)
    root = base_dir / NAME
    make_chunks(root / "chunks", 4)
    (root / "chunks" / "part_003.txt").unlink()       # 缺 part_003
    r = init_novel_cli(base_dir, cfg)
    assert r.returncode == 3 and "缺口" in r.stdout


def test_init_padding_inconsistent_exit3(base_dir):
    cfg = write_config(base_dir)
    root = base_dir / NAME
    make_chunks(root / "chunks", 9)                       # part_001..part_009
    (root / "chunks" / "part_10.txt").write_text("两位宽", encoding="utf-8")
    r = init_novel_cli(base_dir, cfg)
    assert r.returncode == 3 and "零填充" in r.stdout


# ---------------------------------------------------------------------------
# 测试 2 / 5：超限与 K28（init 硬校验，任何落盘之前）
# ---------------------------------------------------------------------------

def test_init_chunk_over_limit_exit3(base_dir):
    cfg = write_config(base_dir)
    root = base_dir / NAME
    make_chunks(root / "chunks", 3, over_chars=5000)
    r = init_novel_cli(base_dir, cfg, extra=["--chunk-max-chars", "1000"])
    assert r.returncode == 3 and "超限" in r.stdout
    assert not (root / "metadata.json").exists()
    assert not (root / "config.snapshot.json").exists()


def test_k28_summary_max_exit2(base_dir):
    cfg = write_config(base_dir, max_output_tokens_summarizer=8000)   # ×0.6=4800
    root = base_dir / NAME
    make_chunks(root / "chunks", 3)
    r = init_novel_cli(base_dir, cfg, extra=["--summary-max", "20000"])
    assert r.returncode == 2 and "K28" in r.stdout
    assert not (root / "metadata.json").exists()


def test_k28_chunk_max_200000_exit2(base_dir):
    cfg = write_config(base_dir)
    root = base_dir / NAME
    make_chunks(root / "chunks", 3)
    r = init_novel_cli(base_dir, cfg, extra=["--chunk-max-chars", "200000"])
    assert r.returncode == 2 and "K28" in r.stdout
    assert not (root / "metadata.json").exists()


# ---------------------------------------------------------------------------
# 测试 3 / 4：配置非法 / 缺 key / 快照与 log 脱敏
# ---------------------------------------------------------------------------

def test_init_invalid_config_exit2(base_dir):
    cfg = write_config(base_dir, backend="claude")
    make_chunks(base_dir / NAME / "chunks", 3)
    r = init_novel_cli(base_dir, cfg)
    assert r.returncode == 2 and "backend" in r.stdout


def test_init_missing_api_key_exit2(base_dir, monkeypatch):
    monkeypatch.delenv("NOVEL_LLM_API_KEY", raising=False)
    cfg = write_config(base_dir, backend="openai", api_key=None)
    make_chunks(base_dir / NAME / "chunks", 3)
    r = init_novel_cli(base_dir, cfg)
    assert r.returncode == 2 and "api_key" in r.stdout
    assert not (base_dir / NAME / "metadata.json").exists()


def test_snapshot_and_log_have_no_api_key(base_dir, monkeypatch):
    monkeypatch.setenv("NOVEL_LLM_API_KEY", "sk-top-secret-987")
    cfg = write_config(base_dir, backend="openai", api_key="sk-cfg-secret-111")
    root, _ = _init_ok(base_dir, cfg, chunks=3)
    for f in ("config.snapshot.json", "run.log", "metadata.json"):
        blob = (root / f).read_text(encoding="utf-8")
        assert "sk-top-secret-987" not in blob, f
        assert "sk-cfg-secret-111" not in blob, f
    snap = json.loads((root / "config.snapshot.json").read_text(encoding="utf-8"))
    assert snap["llm"]["base_url"] == "https://api.openai.com"


# ---------------------------------------------------------------------------
# 测试 18：backup K23 三态（CLI）
# ---------------------------------------------------------------------------

def test_backup_k23_three_states(base_dir):
    cfg = write_config(base_dir)
    root, _ = _init_ok(base_dir, cfg, chunks=5)
    snap = root / ".rollback" / "batch_1_read_timeline.md"
    # ① 不存在 → 写(0)
    r = run_cli(BACKUP, base=base_dir, config=cfg)
    assert r.returncode == 0, r.stdout + r.stderr
    assert snap.exists()
    # ② 存在且完好 → 幂等覆写(0)（批级重跑能跑第二次）
    r2 = run_cli(BACKUP, base=base_dir, config=cfg)
    assert r2.returncode == 0 and "overwritten" in r2.stdout
    # ③ 损坏 → 2（需 --force）
    snap.write_bytes(b"\xff\xfe corrupt")
    r3 = run_cli(BACKUP, base=base_dir, config=cfg)
    assert r3.returncode == 2 and "--force" in r3.stdout
    # --force → 0 且覆写
    r4 = run_cli(BACKUP + ["--force"], base=base_dir, config=cfg)
    assert r4.returncode == 0
    assert snap.read_text(encoding="utf-8").startswith("# 情节时间线")


def test_backup_bad_phase_exit2(base_dir):
    cfg = write_config(base_dir)
    _init_ok(base_dir, cfg, chunks=3)
    r = run_cli(["cli/backup.py", "--novel", NAME, "--batch", "1",
                 "--phase", "bogus"], base=base_dir, config=cfg)
    assert r.returncode == 2


# ---------------------------------------------------------------------------
# 测试 40：K37 带数据时间线守卫 + --recover 恢复
# ---------------------------------------------------------------------------

def test_k37_guard_blocks_plain_init_and_recover_works(base_dir):
    cfg = write_config(base_dir)
    root = base_dir / NAME
    make_chunks(root / "chunks", 5)
    tl_text = tl.skeleton() + "| 1 | part_001 | 事件 | 影响 |\n"
    atomic_write_text(root / "plot_timeline.md", tl_text)
    r = init_novel_cli(base_dir, cfg)
    assert r.returncode == 3
    assert "--recover" in r.stdout
    # 不落任何文件
    assert not (root / "metadata.json").exists()
    assert not (root / "config.snapshot.json").exists()
    assert not (root / "notes").exists()
    assert (root / "plot_timeline.md").read_text(encoding="utf-8") == tl_text
    # init --recover 正常恢复
    r2 = init_novel_cli(base_dir, cfg, extra=["--recover"])
    assert r2.returncode == 0, r2.stdout + r2.stderr
    doc = statemod.read_metadata(root / "metadata.json", base=base_dir)
    assert doc.processed_chunks == 1


# ---------------------------------------------------------------------------
# 测试 19：--recover（K4 / E4 / K11 / K18）
# ---------------------------------------------------------------------------

def _timeline_rows(root, nums):
    lines = [f"| {i} | part_{n:03d} | 事件 | 影响 |"
             for i, n in enumerate(nums, start=1)]
    atomic_write_text(root / "plot_timeline.md",
                      tl.skeleton() + "\n".join(lines) + "\n")


def test_recover_processed_12_ok(base_dir):
    cfg = write_config(base_dir)
    root = base_dir / NAME
    make_chunks(root / "chunks", 12)
    _timeline_rows(root, range(1, 13))
    r = init_novel_cli(base_dir, cfg, extra=["--recover"])
    assert r.returncode == 0, r.stdout + r.stderr
    doc = statemod.read_metadata(root / "metadata.json", base=base_dir)
    assert doc.processed_chunks == 12                 # 12-0=12 ≤ window+batch(15)
    assert doc.summary_chunks == 0


def test_recover_k4_identity_three_exits(base_dir):
    cfg = write_config(base_dir)
    root = base_dir / NAME
    make_chunks(root / "chunks", 100)
    _timeline_rows(root, range(1, 101))
    extra = ["--recover", "--timeline-window", "10", "--batch-size", "5"]
    r = init_novel_cli(base_dir, cfg, extra=extra)
    assert r.returncode == 3
    for kw in ("--summary-chunks", "--timeline-window", "分卷"):
        assert kw in r.stdout, f"三出口缺 {kw}: {r.stdout}"
    # --summary-chunks 越界（> processed）→ 3
    r3 = init_novel_cli(base_dir, cfg,
                        extra=extra + ["--summary-chunks", "101"])
    assert r3.returncode == 3
    # 传 --summary-chunks 88 → 100−88=12 ≤ 15 → 成功
    r2 = init_novel_cli(base_dir, cfg,
                        extra=extra + ["--summary-chunks", "88"])
    assert r2.returncode == 0, r2.stdout + r2.stderr
    doc = statemod.read_metadata(root / "metadata.json", base=base_dir)
    assert doc.summary_chunks == 88


def test_recover_missing_part006_derives_5_not_12(base_dir):
    cfg = write_config(base_dir)
    root = base_dir / NAME
    make_chunks(root / "chunks", 12)
    _timeline_rows(root, list(range(1, 6)) + list(range(7, 13)))   # 缺 part_006
    r = init_novel_cli(base_dir, cfg, extra=["--recover"])
    assert r.returncode == 0, r.stdout + r.stderr
    doc = statemod.read_metadata(root / "metadata.json", base=base_dir)
    assert doc.processed_chunks == 5                  # E4 连续前缀，非最大序号 12


def test_recover_seq_break_exit3(base_dir):
    cfg = write_config(base_dir)
    root = base_dir / NAME
    make_chunks(root / "chunks", 12)
    atomic_write_text(root / "plot_timeline.md",
                      tl.skeleton() + "| 1 | part_001 | 事件 | 影响 |\n"
                      "| 2 | part_002 | 事件 | 影响 |\n"
                      "| 3 | part_003 | 事件 | 影响 |\n"
                      "| 5 | part_004 | 事件 | 影响 |\n")
    r = init_novel_cli(base_dir, cfg, extra=["--recover"])
    assert r.returncode == 3
    assert not (root / "metadata.json").exists()


def test_recover_k11_snapshot_conflict(base_dir):
    cfg = write_config(base_dir)
    root = base_dir / NAME
    make_chunks(root / "chunks", 12)
    _timeline_rows(root, range(1, 13))
    (root / ".rollback").mkdir(parents=True, exist_ok=True)
    atomic_write_text(root / ".rollback" / "batch_1_read_timeline.md",
                      tl.skeleton() + "| 1 | part_001 | 事件 | 影响 |\n")
    atomic_write_json(root / "config.snapshot.json", {
        "llm": {},
        "run": {"batch_size": 5, "timeline_window": 10, "summary_max": 5000,
                "chunk_max_chars": 20000, "chunk_padding": 3,
                "prompt_version": "v1"}})
    # 显式传参与快照不一致（derived current_batch=1 ≠ 0）→ 3
    r = init_novel_cli(base_dir, cfg,
                       extra=["--recover", "--batch-size", "8"])
    assert r.returncode == 3 and "K11" in r.stdout
    # 一致 → 成功
    r2 = init_novel_cli(base_dir, cfg,
                        extra=["--recover", "--batch-size", "5"])
    assert r2.returncode == 0, r2.stdout + r2.stderr
    doc = statemod.read_metadata(root / "metadata.json", base=base_dir)
    assert doc.batch_size == 5 and doc.current_batch == 1   # K1 推导值写入


def test_recover_k18_upper_bound(base_dir):
    cfg = write_config(base_dir)
    root = base_dir / NAME
    make_chunks(root / "chunks", 12)
    _timeline_rows(root, range(1, 13))
    (root / ".rollback").mkdir(parents=True, exist_ok=True)
    atomic_write_text(root / ".rollback" / "batch_9_read_timeline.md",
                      tl.skeleton() + "| 1 | part_001 | 事件 | 影响 |\n")
    r = init_novel_cli(base_dir, cfg,
                       extra=["--recover", "--batch-size", "5"])
    assert r.returncode == 3 and "K18" in r.stdout
    # 显式 --current-batch 3 可恢复（K18 出口）
    r2 = init_novel_cli(base_dir, cfg, extra=["--recover", "--batch-size", "5",
                                              "--current-batch", "3"])
    assert r2.returncode == 0, r2.stdout + r2.stderr
    doc = statemod.read_metadata(root / "metadata.json", base=base_dir)
    assert doc.current_batch == 3


# ---------------------------------------------------------------------------
# 33 / K26：并发 init / 大小写碰撞
# ---------------------------------------------------------------------------

def test_concurrent_init_same_book_exit3(base_dir):
    cfg = write_config(base_dir)
    make_chunks(base_dir / NAME / "chunks", 3)
    lock = lockmod.acquire(base_dir, NAME)
    try:
        r = init_novel_cli(base_dir, cfg)
        assert r.returncode == 3
        assert "锁" in r.stdout or "存活" in r.stdout
    finally:
        lock.release()
    r2 = init_novel_cli(base_dir, cfg)                # 释放后成功
    assert r2.returncode == 0, r2.stdout + r2.stderr


def test_init_case_collision_exit3(base_dir):
    cfg = write_config(base_dir)
    make_chunks(base_dir / "Foo 书" / "chunks", 3)
    r = init_novel_cli(base_dir, cfg, novel="foo 书")
    assert r.returncode == 3 and "碰撞" in r.stdout


# ---------------------------------------------------------------------------
# I4 幂等分支 / 缺失 metadata（K31）CLI 语义
# ---------------------------------------------------------------------------

def test_init_idempotent_i4(base_dir):
    cfg = write_config(base_dir)
    root, _ = _init_ok(base_dir, cfg, chunks=3)
    r = init_novel_cli(base_dir, cfg)                 # 已 init（init/0）→ 幂等 0
    assert r.returncode == 0
    atomic_write_text(root / "plot_timeline.md",
                      tl.skeleton() + "| 1 | part_001 | 事件 | 影响 |\n")
    r2 = init_novel_cli(base_dir, cfg)                # status=init 但时间线有数据 → 2
    assert r2.returncode == 2


def test_cli_missing_metadata_exit2(base_dir):
    cfg = write_config(base_dir)
    r = run_cli(["cli/verify_timeline.py", "--novel", NAME, "--check-only"],
                base=base_dir, config=cfg)
    assert r.returncode == 2 and "metadata" in r.stdout   # K31：不 emergency、不写文件


# ---------------------------------------------------------------------------
# append → verify → tail → chunk_stats → rollback CLI 闭环
# ---------------------------------------------------------------------------

def test_append_verify_tail_cli_cycle(base_dir):
    cfg = write_config(base_dir)
    root, _ = _init_ok(base_dir, cfg, chunks=5)
    assert run_cli(BACKUP, base=base_dir, config=cfg).returncode == 0
    for i in range(1, 6):
        make_reader_json(root, i)
    r = run_cli(["cli/append_timeline.py", "--novel", NAME, "--batch", "1"],
                base=base_dir, config=cfg)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "appended=5" in r.stdout
    # check-only：APPLIED
    rv = run_cli(["cli/verify_timeline.py", "--novel", NAME, "--check-only"],
                 base=base_dir, config=cfg)
    assert rv.returncode == 0 and rv.stdout.strip() == "APPLIED"
    # 批模式校验
    rv2 = run_cli(["cli/verify_timeline.py", "--novel", NAME, "--batch", "1"],
                  base=base_dir, config=cfg)
    assert rv2.returncode == 0
    # tail：窗口内事件行原文
    rt = run_cli(["cli/tail_timeline.py", "--novel", NAME,
                  "--chunk-start", "1", "--chunk-end", "3"],
                 base=base_dir, config=cfg)
    assert rt.returncode == 0
    assert len(rt.stdout.strip().splitlines()) == 3
    assert rt.stdout.splitlines()[0].startswith("| 1 | part_001 |")
    # 空范围 → 空输出 + 退出码 0（K33）
    re_ = run_cli(["cli/tail_timeline.py", "--novel", NAME,
                   "--chunk-start", "5", "--chunk-end", "3"],
                  base=base_dir, config=cfg)
    assert re_.returncode == 0 and re_.stdout.strip() == ""
    # rollback --phase read：恢复骨架，快照只恢复不删（K22）
    rr = run_cli(["cli/rollback.py", "--novel", NAME, "--phase", "read"],
                 base=base_dir, config=cfg)
    assert rr.returncode == 0, rr.stdout + rr.stderr
    assert (root / "plot_timeline.md").read_text(encoding="utf-8") == tl.skeleton()
    assert (root / ".rollback" / "batch_1_read_timeline.md").exists()
    # 无快照 → 3
    (root / ".rollback" / "batch_1_read_timeline.md").unlink()
    r3 = run_cli(["cli/rollback.py", "--novel", NAME, "--phase", "read"],
                 base=base_dir, config=cfg)
    assert r3.returncode == 3


def test_rollback_summarize_phase(base_dir):
    cfg = write_config(base_dir)
    root, _ = _init_ok(base_dir, cfg, chunks=3)
    atomic_write_text(root / "summary.md", "第一版小结内容")
    r = run_cli(["cli/backup.py", "--novel", NAME, "--batch", "1",
                 "--phase", "summarize"], base=base_dir, config=cfg)
    assert r.returncode == 0
    atomic_write_text(root / "summary.md", "被外部篡改")
    r2 = run_cli(["cli/rollback.py", "--novel", NAME, "--phase", "summarize"],
                 base=base_dir, config=cfg)
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert (root / "summary.md").read_text(encoding="utf-8") == "第一版小结内容"
    assert (root / ".rollback" / "batch_1_summarize_summary.md").exists()


def test_purge_k32_keeps_dir_exit0(base_dir):
    cfg = write_config(base_dir)
    root, _ = _init_ok(base_dir, cfg, chunks=3)
    assert run_cli(BACKUP, base=base_dir, config=cfg).returncode == 0
    assert run_cli(["cli/backup.py", "--novel", NAME, "--batch", "1",
                    "--phase", "summarize"], base=base_dir,
                   config=cfg).returncode == 0
    r = run_cli(["cli/rollback.py", "--novel", NAME, "--purge"],
                base=base_dir, config=cfg)
    assert r.returncode == 0
    rb = root / ".rollback"
    assert rb.is_dir() and not list(rb.glob("batch_*.md"))


def test_chunk_stats_cli(base_dir):
    cfg = write_config(base_dir)
    root, _ = _init_ok(base_dir, cfg, chunks=5)       # 内容 "章1".."章5"（各 2 字）
    r = run_cli(["cli/chunk_stats.py", "--novel", NAME,
                 "--chunk-start", "1", "--chunk-end", "2"],
                base=base_dir, config=cfg)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "part_001=2" in r.stdout and "part_002=2" in r.stdout
    assert "total_chars=4" in r.stdout
    (root / "chunks" / "part_003.txt").unlink()
    r2 = run_cli(["cli/chunk_stats.py", "--novel", NAME,
                  "--chunk-start", "1", "--chunk-end", "3"],
                 base=base_dir, config=cfg)
    assert r2.returncode == 3 and "缺失" in r2.stdout
