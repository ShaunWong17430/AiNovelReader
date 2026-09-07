"""阶段 3 出口测试 28–31、46：update_metadata CLI（§12.3/§12.4，B6/J6/K1/K36）。

覆盖（REQUIREMENTS v3.2.1 §12.4/§12.3）：
- 28. done 后 --status running / --error → 3（J6；done 唯一终态）；
- 29. --processed abc → 2 且不 emergency（无 metadata.corrupt_*）；
      合法但 N>total → 3；
- 30. 严格单调：旧值 / +0（等于当前）→ 3；单批增量越界（+2n）→ 3；
- 31. 状态机 init→running→done 全程 0；init→error（I1）成功；
- 46. K36：--summary 目标<当前 → 3 拒；==当前 → no-op（退出码 0，值不变）。

业务规则全部实现于 core/state.py；本文件只验收 CLI 行为（退出码 + KEY=VALUE）。
"""
from __future__ import annotations

from conftest import DEFAULT_NOVEL_NAME
from helpers import init_novel_cli, make_chunks, run_cli, write_config

N = DEFAULT_NOVEL_NAME
META = "cli/update_metadata.py"


def _init(base, config, n=5, extra=None):
    root = base / N
    make_chunks(root / "chunks", n)               # init 步骤 4：chunks 非空
    init_novel_cli(base, config, extra=extra)


def test_28_done_rejects_status_and_error(base_dir):
    cfg = write_config(base_dir)
    _init(base_dir, cfg)
    # init → running → processed=total(5) → done（白名单 B6）
    r = run_cli([META, "--novel", N, "--status", "running"], base=base_dir,
                config=cfg)
    assert r.returncode == 0, r.stdout
    r = run_cli([META, "--novel", N, "--processed", "5"], base=base_dir,
                config=cfg)
    assert r.returncode == 0, r.stdout
    r = run_cli([META, "--novel", N, "--status", "done"], base=base_dir,
                config=cfg)
    assert r.returncode == 0, r.stdout
    assert "STATUS=done" in r.stdout
    # done 终态拒绝任何转换（J6）
    r = run_cli([META, "--novel", N, "--status", "running"], base=base_dir,
                config=cfg)
    assert r.returncode == 3
    r = run_cli([META, "--novel", N, "--error", "x"], base=base_dir, config=cfg)
    assert r.returncode == 3


def test_29_processed_abc_exit2_no_emergency(base_dir):
    cfg = write_config(base_dir)
    _init(base_dir, cfg)
    r = run_cli([META, "--novel", N, "--processed", "abc"], base=base_dir,
                config=cfg)
    assert r.returncode == 2                       # 参数错误（argparse）
    root = base_dir / N
    assert not list(root.glob("metadata.corrupt_*")), "不得 emergency 改名"
    meta = (root / "metadata.json").read_text(encoding="utf-8")
    assert "corrupt" not in meta.lower()


def test_29_processed_over_total_exit3(base_dir):
    cfg = write_config(base_dir)
    _init(base_dir, cfg)                           # total=5
    r = run_cli([META, "--novel", N, "--processed", "99"], base=base_dir,
                config=cfg)
    assert r.returncode == 3
    assert "99 > total 5" in r.stdout or "> total" in r.stdout


def test_30_strict_monotonic_and_single_batch(base_dir):
    """严格单调拒旧值/+0；单批增量越界（+2n）→ 3。"""
    cfg = write_config(base_dir)
    _init(base_dir, cfg, extra=["--batch-size", "2"])   # total=5, batch=2
    r = run_cli([META, "--novel", N, "--processed", "2"], base=base_dir,
                config=cfg)
    assert r.returncode == 0, r.stdout
    # 旧值 → 3
    r = run_cli([META, "--novel", N, "--processed", "1"], base=base_dir,
                config=cfg)
    assert r.returncode == 3
    # +0（等于当前）→ 3（严格递增 §11）
    r = run_cli([META, "--novel", N, "--processed", "2"], base=base_dir,
                config=cfg)
    assert r.returncode == 3
    # 单批增量越界：2→5 增量 3 > batch_size 2 → 3
    r = run_cli([META, "--novel", N, "--processed", "5"], base=base_dir,
                config=cfg)
    assert r.returncode == 3
    assert "单批增量" in r.stdout
    # 合法路径：2→4（增量 2 ≤ 2）→ 4→5（增量 1 ≤ 2）
    r = run_cli([META, "--novel", N, "--processed", "4"], base=base_dir,
                config=cfg)
    assert r.returncode == 0, r.stdout
    r = run_cli([META, "--novel", N, "--processed", "5"], base=base_dir,
                config=cfg)
    assert r.returncode == 0, r.stdout
    assert "PROCESSED=5" in r.stdout


def test_31_state_machine_init_running_done(base_dir):
    cfg = write_config(base_dir)
    _init(base_dir, cfg)
    # init → running
    r = run_cli([META, "--novel", N, "--status", "running"], base=base_dir,
                config=cfg)
    assert r.returncode == 0 and "STATUS=running" in r.stdout
    # running → done
    r = run_cli([META, "--novel", N, "--status", "done"], base=base_dir,
                config=cfg)
    assert r.returncode == 0 and "STATUS=done" in r.stdout


def test_31_init_to_error_allowed(base_dir):
    """init→error 在白名单（I1）。"""
    cfg = write_config(base_dir)
    _init(base_dir, cfg)
    r = run_cli([META, "--novel", N, "--status", "error"], base=base_dir,
                config=cfg)
    assert r.returncode == 0 and "STATUS=error" in r.stdout


def test_46_k36_summary_noop_and_strict(base_dir):
    cfg = write_config(base_dir)
    _init(base_dir, cfg)
    # processed=5 → current_batch=1（K1）
    r = run_cli([META, "--novel", N, "--processed", "5"], base=base_dir,
                config=cfg)
    assert r.returncode == 0 and "CURRENT_BATCH=1" in r.stdout
    # --summary 5（== processed）→ 0
    r = run_cli([META, "--novel", N, "--summary", "5"], base=base_dir,
                config=cfg)
    assert r.returncode == 0 and "SUMMARY=5" in r.stdout
    # == 当前 → no-op（退出码 0，值不变，K36）
    r = run_cli([META, "--novel", N, "--summary", "5"], base=base_dir,
                config=cfg)
    assert r.returncode == 0
    assert "SUMMARY=5" in r.stdout
    # 目标 < 当前 → 3 拒（严格单调 §11/K36）
    r = run_cli([META, "--novel", N, "--summary", "3"], base=base_dir,
                config=cfg)
    assert r.returncode == 3
    assert "严格单调" in r.stdout or "summary" in r.stdout
