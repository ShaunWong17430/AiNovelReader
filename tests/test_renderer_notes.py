"""阶段 3 CLI 回归：render_notes / verify_notes / write_summary / verify_summary
（§5 / §9.4 / §9.5 / B7 / K15），为阶段 4 事务闭环铺路。

覆盖：
- render_notes：本批 .batch → 多片合并渲染 → notes\\part_XXX~part_YYY.md；
  空 .batch → 3（K15 验收失败）；缺片 → 警告 + 部分渲染（K15 降级）；
- verify_notes：通过 → VERIFY=OK；篡改标题 → 3；范围不一致 → 仅警告（0）；
- write_summary：草稿 → summary.md + .summary_applied（内容=processed）；
  单段/字数校验失败 → 3 且草稿保留（§6.3 步骤 5）；
- verify_summary：通过 → VERIFY=OK；含 BOM / 多段 → 3（§9.4）。
"""
from __future__ import annotations

from conftest import DEFAULT_NOVEL_NAME
from helpers import (init_novel_cli, make_chunks, make_reader_json, run_cli,
                     write_config)

N = DEFAULT_NOVEL_NAME
RENDER = "cli/render_notes.py"
VNOTES = "cli/verify_notes.py"
WSUM = "cli/write_summary.py"
VSUM = "cli/verify_summary.py"


def _book_ready(base, config, n=5):
    """init + backup(batch 1 read) + 本批 .batch 片产物，可 append/render。"""
    root = base / N
    make_chunks(root / "chunks", n)               # init 步骤 4：chunks 非空
    init_novel_cli(base, config)
    r = run_cli(["cli/backup.py", "--novel", N, "--batch", "1",
                 "--phase", "read"], base=base, config=config)
    assert r.returncode == 0, r.stdout
    for i in range(1, n + 1):
        make_reader_json(root, i)
    return root


def test_render_notes_produces_file(base_dir):
    cfg = write_config(base_dir)
    root = _book_ready(base_dir, cfg)
    r = run_cli([RENDER, "--novel", N, "--start", "1", "--end", "5"],
                base=base_dir, config=cfg)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "NOTES=part_001~part_005.md" in r.stdout
    notes = root / "notes" / "part_001~part_005.md"
    assert notes.is_file()
    text = notes.read_text(encoding="utf-8")
    # §5 五段标题按序（B7）
    for kw in ("# 读书笔记：part_001~part_005", "## 📖 阅读范围",
               "## 👥 角色发展", "## 📍 关键情节", "## ❓ 疑问",
               "## 📝 摘要"):
        assert kw in text, f"缺 {kw}"
    assert "part_001.txt~part_005.txt" in text
    assert "字数：约" in text
    # 多片合并：5 片 characters 空行拼接 / plots 展平
    assert text.count("主角") == 5
    assert text.count("- 情节") == 5


def test_render_notes_empty_batch_fails(base_dir):
    """K15：.batch 完全无可用片 → 3（验收失败）。"""
    cfg = write_config(base_dir)
    root = base_dir / N
    make_chunks(root / "chunks", 5)
    init_novel_cli(base_dir, cfg)
    r = run_cli([RENDER, "--novel", N, "--start", "1", "--end", "5"],
                base=base_dir, config=cfg)
    assert r.returncode == 3
    assert "无法生成" in r.stdout


def test_render_notes_missing_chunks_warns_k15(base_dir):
    """K15 降级：缺片 → 警告 + 用已有片渲染；chunks 缺失 → 字数「未知」+ 警告。"""
    cfg = write_config(base_dir)
    root = _book_ready(base_dir, cfg)
    (root / ".batch" / "part_003.json").unlink()
    (root / "chunks").rename(root / "chunks_backup")     # 字数统计失败
    r = run_cli([RENDER, "--novel", N, "--start", "1", "--end", "5"],
                base=base_dir, config=cfg)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "WARN" in r.stderr and "K15" in r.stderr
    text = (root / "notes" / "part_001~part_005.md").read_text(encoding="utf-8")
    assert "未知" in text
    assert "字数" in text


def test_verify_notes_ok_and_failure(base_dir):
    cfg = write_config(base_dir)
    root = _book_ready(base_dir, cfg)
    run_cli([RENDER, "--novel", N, "--start", "1", "--end", "5"],
            base=base_dir, config=cfg)
    r = run_cli([VNOTES, "--novel", N, "--start", "1", "--end", "5"],
                base=base_dir, config=cfg)
    assert r.returncode == 0 and "VERIFY=OK" in r.stdout
    # 篡改 H1 → 3（B11/D13）
    notes = root / "notes" / "part_001~part_005.md"
    text = notes.read_text(encoding="utf-8")
    notes.write_text(text.replace("读书笔记：", "读书笔记： 带空格 "),
                     encoding="utf-8")
    r = run_cli([VNOTES, "--novel", N, "--start", "1", "--end", "5"],
                base=base_dir, config=cfg)
    assert r.returncode == 3
    # 缺失 → 3
    notes.unlink()
    r = run_cli([VNOTES, "--novel", N, "--start", "1", "--end", "5"],
                base=base_dir, config=cfg)
    assert r.returncode == 3


def test_write_summary_ok_and_applied_marker(base_dir):
    cfg = write_config(base_dir)
    root = _book_ready(base_dir, cfg)
    draft = "单段小结：主角进入遗迹，发现石碑铭文，开启主线调查。"
    (root / "summary_draft.txt").write_text(draft, encoding="utf-8")
    r = run_cli([WSUM, "--novel", N, "--processed", "5"], base=base_dir,
                config=cfg)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "SUMMARY=OK" in r.stdout and "processed=5" in r.stdout
    assert (root / "summary.md").read_text(encoding="utf-8") == draft
    assert (root / ".summary_applied").read_text(encoding="utf-8") == "5"
    # verify_summary 通过
    r = run_cli([VSUM, "--novel", N], base=base_dir, config=cfg)
    assert r.returncode == 0 and "VERIFY=OK" in r.stdout


def test_write_summary_validation_failure_keeps_draft(base_dir):
    """§6.3 步骤 5：单段/字数校验不满足 → 3，草稿保留（回到步骤 4）。"""
    cfg = write_config(base_dir)
    root = _book_ready(base_dir, cfg)
    (root / "summary_draft.txt").write_text("第一行\n第二行", encoding="utf-8")
    r = run_cli([WSUM, "--novel", N, "--processed", "5"], base=base_dir,
                config=cfg)
    assert r.returncode == 3
    assert "单段" in r.stdout
    assert (root / "summary_draft.txt").exists(), "草稿必须保留"
    # 超长：> summary_max(5000) → 3
    (root / "summary_draft.txt").write_text("字" * 6000, encoding="utf-8")
    r = run_cli([WSUM, "--novel", N, "--processed", "5"], base=base_dir,
                config=cfg)
    assert r.returncode == 3
    assert "summary_max" in r.stdout
    # 校验失败不得覆写 summary.md（init 步骤 8 的骨架空文件保持原样）
    assert (root / "summary.md").read_text(encoding="utf-8").strip() == "", \
        "校验失败不得覆写 summary.md（init 骨架保持原样）"


def test_write_summary_missing_draft(base_dir):
    cfg = write_config(base_dir)
    root = base_dir / N
    make_chunks(root / "chunks", 5)
    init_novel_cli(base_dir, cfg)
    r = run_cli([WSUM, "--novel", N, "--processed", "1"], base=base_dir,
                config=cfg)
    assert r.returncode == 3
    assert "summary_draft.txt 缺失" in r.stdout


def test_verify_summary_rejects_bom_and_multi_paragraph(base_dir):
    cfg = write_config(base_dir)
    root = _book_ready(base_dir, cfg)
    good = "正常小结文本。"
    (root / "summary.md").write_text(good, encoding="utf-8")
    assert run_cli([VSUM, "--novel", N], base=base_dir,
                   config=cfg).returncode == 0
    # 含 BOM → 3（§9.4 UTF-8 无 BOM）
    (root / "summary.md").write_bytes(b"\xef\xbb\xbf" + good.encode("utf-8"))
    r = run_cli([VSUM, "--novel", N], base=base_dir, config=cfg)
    assert r.returncode == 3 and "BOM" in r.stdout
    # 多段 → 3
    (root / "summary.md").write_text("第一段\n第二段", encoding="utf-8")
    r = run_cli([VSUM, "--novel", N], base=base_dir, config=cfg)
    assert r.returncode == 3
    # 缺失 → 3
    (root / "summary.md").unlink()
    r = run_cli([VSUM, "--novel", N], base=base_dir, config=cfg)
    assert r.returncode == 3
