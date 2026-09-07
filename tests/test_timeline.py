"""出口测试 18（时间线侧）/ 45（K40 实现级）+ §1.4 / §1.6 / §9.2 / §9.3 / K33：
core/timeline.py 文件事务组件（解析/归一化/追加/校验/截取）。

覆盖：
- §1.6 字数口径（全角空格/制表符剔除；中文标点计入；英文逐字符）；
- §1.4 结构解析失败（标题/表头/分隔行/4 列/跨片写法/顺序连续）；
- 归一化（\\r\\n/孤立 \\r/BOM/多余空行）与原子追加（顺序续号）；
- K5 覆盖 / C1 events∈[1,5] / C12 / A4 / J3（快照状态守卫）；
- §9.2 check-only 三态（APPLIED/NOT_FOUND/PARTIAL）+ G12–G14 + K40；
- K33 钳制与空范围（退出码 0 语义）；
- E4 连续前缀推导（--recover 用）。
"""
from __future__ import annotations

import json

import pytest

import core.recovery as recovery
import core.state as statemod
import core.timeline as tl

from core.state import atomic_write_text
from helpers import make_chunks, make_reader_json, write_config

NAME = "示例书名"


# ---------------------------------------------------------------------------
# §1.6 字数口径
# ---------------------------------------------------------------------------

def test_count_chars_spec_1_6():
    assert tl.count_chars("Hello") == 5
    assert tl.count_chars("中文 测试") == 4
    assert tl.count_chars("a b\tc\nd") == 4
    assert tl.count_chars("全\u3000角\u00a0空格") == 4      # 全角/不间断空格剔除
    assert tl.count_chars("，。！？") == 4                   # 中文标点计入


# ---------------------------------------------------------------------------
# 结构 / 归一化 / 解析失败
# ---------------------------------------------------------------------------

def test_skeleton_parse_empty():
    rows = tl.parse(tl.skeleton())
    assert rows == []


def test_parse_structure_failures():
    good = tl.skeleton()
    with pytest.raises(tl.TimelineError, match="标题"):
        tl.parse(good.replace("# 情节时间线", "# 别的"))
    with pytest.raises(tl.TimelineError, match="表头"):
        tl.parse(good.replace("| 顺序 | 分片 | 事件 | 影响 |",
                              "| a | b | c | d |"))
    with pytest.raises(tl.TimelineError, match="分隔行"):
        tl.parse(good.replace("|------|------|------|------|",
                              "|--|--|--|--|"))
    bad_row = good + "| 1 | part_001 | 事件 |\n"             # 3 列
    with pytest.raises(tl.TimelineError, match="4 列"):
        tl.parse(bad_row)
    cross = good + "| 1 | part_001-002 | 事件 | 影响 |\n"    # 跨片写法
    with pytest.raises(tl.TimelineError, match="part_001-002|分片"):
        tl.parse(cross)


def test_normalize_bom_crlf_extra_blanks():
    raw = "\ufeff# 情节时间线\r\n\r\n\r\n| 顺序 | 分片 | 事件 | 影响 |\r\n" \
          "|------|------|------|------|\r\n\r\n| 1 | part_001 | 事件 | 影响 |\r\n\r\n"
    norm = tl.normalize(raw)
    assert "\ufeff" not in norm and "\r" not in norm
    assert norm == tl.skeleton() + "| 1 | part_001 | 事件 | 影响 |\n"
    assert tl.parse(norm)[0].chunk == "part_001"


def test_append_rows_continues_seq(base_dir):
    path = base_dir / "tl.md"
    atomic_write_text(path, tl.skeleton())
    n = tl.append_rows(path, [
        tl.Row(0, "part_001", 1, "事件A", "影响A"),
        tl.Row(0, "part_002", 2, "事件B", "影响B")])
    assert n == 2
    rows = tl.parse(path.read_text(encoding="utf-8"))
    assert [r.seq for r in rows] == [1, 2]
    assert rows[0].line() == "| 1 | part_001 | 事件A | 影响A |"


# ---------------------------------------------------------------------------
# 批追加 / 校验（backup → append → verify 闭环）
# ---------------------------------------------------------------------------

def _root(base_dir) -> Path:
    root = base_dir / NAME
    for sub in ("chunks", ".rollback", ".batch"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    atomic_write_text(root / "plot_timeline.md", tl.skeleton())
    atomic_write_text(root / "summary.md", "")
    return root


def _backup(root, base_dir, batch_id=1, phase="read"):
    return recovery.backup(base_dir, NAME, batch_id=batch_id, phase=phase)


def test_append_verify_cycle(base_dir):
    root = _root(base_dir)
    make_chunks(root / "chunks", 12)               # G13 ③ 需要真实分片集合
    _backup(root, base_dir, 1, "read")
    for i in range(1, 6):
        make_reader_json(root, i)
    res = tl.append_batch(root, batch_id=1, current_batch=0,
                          chunk_padding=3, start=1, end=5)
    assert res["appended"] == 5 and res["seq_end"] == 5
    # verify：批模式全绿
    errs, warns = tl.verify_batch(root, batch_id=1, current_batch=0,
                                  chunk_padding=3, start=1, end=5)
    assert errs == [] and warns == []
    # check-only：APPLIED + K40 通过
    state, errs = tl.check_only(root / "plot_timeline.md", start=1, end=5,
                                total_chunks=12, chunk_padding=3,
                                processed_chunks=0, chunks_dir=root / "chunks")
    assert state == "APPLIED" and errs == []
    # E4 连续前缀
    assert tl.derive_processed(root / "plot_timeline.md") == 5
    assert tl.max_chunk_seq(root / "plot_timeline.md") == 5


def test_append_then_second_batch(base_dir):
    root = _root(base_dir)
    make_chunks(root / "chunks", 12)
    for i in range(1, 6):
        make_reader_json(root, i)
    _backup(root, base_dir, 1, "read")
    tl.append_batch(root, batch_id=1, current_batch=0, chunk_padding=3,
                    start=1, end=5)
    for i in range(6, 11):
        make_reader_json(root, i)
    _backup(root, base_dir, 2, "read")             # 快照 2 = 当前时间线（5 行）
    tl.append_batch(root, batch_id=2, current_batch=1, chunk_padding=3,
                    start=6, end=10)
    rows = tl.parse((root / "plot_timeline.md").read_text(encoding="utf-8"))
    assert [r.seq for r in rows] == list(range(1, 11))   # 顺序 1..N 连续
    state, errs = tl.check_only(root / "plot_timeline.md", start=6, end=10,
                                total_chunks=12, chunk_padding=3,
                                processed_chunks=5,
                                chunks_dir=root / "chunks")
    assert state == "APPLIED" and errs == []


def test_j3_timeline_not_at_snapshot_state(base_dir):
    root = _root(base_dir)
    _backup(root, base_dir, 1, "read")
    make_reader_json(root, 1)
    tl.append_batch(root, batch_id=1, current_batch=0, chunk_padding=3,
                    start=1, end=1)
    _backup(root, base_dir, 2, "read")             # 快照 2 = 当前时间线（1 行）
    # 篡改时间线（外部追加一行）→ 不在快照状态
    p = root / "plot_timeline.md"
    atomic_write_text(p, p.read_text(encoding="utf-8")
                      + "| 2 | part_002 | 外插 | 影响 |\n")
    make_reader_json(root, 2)
    with pytest.raises(tl.TimelineError, match="快照状态"):
        tl.append_batch(root, batch_id=2, current_batch=1, chunk_padding=3,
                        start=2, end=2)


def test_c12_batch_id_must_be_current_plus_one(base_dir):
    root = _root(base_dir)
    _backup(root, base_dir, 3, "read")
    make_reader_json(root, 1)
    with pytest.raises(tl.TimelineError, match="C12"):
        tl.append_batch(root, batch_id=3, current_batch=0, chunk_padding=3,
                        start=1, end=1)


def test_a4_snapshot_missing(base_dir):
    root = _root(base_dir)
    make_reader_json(root, 1)
    with pytest.raises(tl.TimelineError, match="A4"):
        tl.append_batch(root, batch_id=1, current_batch=0, chunk_padding=3,
                        start=1, end=1)


def test_k5_coverage_and_c1_events_bounds(base_dir):
    root = _root(base_dir)
    _backup(root, base_dir, 1, "read")
    make_reader_json(root, 1)
    make_reader_json(root, 2)
    make_reader_json(root, 3)                     # 缺 part_004/005
    with pytest.raises(tl.TimelineError, match="覆盖不全"):
        tl.append_batch(root, batch_id=1, current_batch=0, chunk_padding=3,
                        start=1, end=5)
    # C1：0 事件 → 拒
    root2 = _root(base_dir)
    _backup(root2, base_dir, 1, "read")
    for i in range(1, 6):
        make_reader_json(root2, i, events=[] if i == 3 else
                         [{"event": f"e{i}", "impact": f"i{i}"}])
    with pytest.raises(tl.TimelineError, match="C1"):
        tl.append_batch(root2, batch_id=1, current_batch=0, chunk_padding=3,
                        start=1, end=5)


def test_verify_batch_detects_coverage_gap(base_dir):
    root = _root(base_dir)
    _backup(root, base_dir, 1, "read")
    for i in range(1, 6):
        make_reader_json(root, i)
    tl.append_batch(root, batch_id=1, current_batch=0, chunk_padding=3,
                    start=1, end=5)
    (root / ".batch" / "part_003.json").unlink()
    errs, _ = tl.verify_batch(root, batch_id=1, current_batch=0,
                              chunk_padding=3, start=1, end=5)
    assert any("覆盖不全" in e for e in errs)


# ---------------------------------------------------------------------------
# check-only 三态 / K40 / G13
# ---------------------------------------------------------------------------

def test_check_only_states(base_dir):
    root = _root(base_dir)
    make_chunks(root / "chunks", 12)
    p = root / "plot_timeline.md"
    assert tl.check_only(p, start=1, end=5, total_chunks=12, chunk_padding=3,
                         processed_chunks=0,
                         chunks_dir=root / "chunks")[0] == "NOT_FOUND"
    _backup(root, base_dir, 1, "read")
    for i in range(1, 6):
        make_reader_json(root, i)
    tl.append_batch(root, batch_id=1, current_batch=0, chunk_padding=3,
                    start=1, end=5)
    assert tl.check_only(p, start=1, end=5, total_chunks=12, chunk_padding=3,
                         processed_chunks=0,
                         chunks_dir=root / "chunks")[0] == "APPLIED"
    # PARTIAL：删一行（part_003）
    rows = tl.parse(p.read_text(encoding="utf-8"))
    keep = [r for r in rows if r.chunk_num != 3]
    atomic_write_text(p, tl.skeleton()
                      + "\n".join(r.line() for r in keep) + "\n")
    state, errs = tl.check_only(p, start=1, end=5, total_chunks=12,
                                chunk_padding=3, processed_chunks=0,
                                chunks_dir=root / "chunks")
    assert state == "PARTIAL"
    # G13 顺序连续被破坏 → 3
    assert any("连续" in e for e in errs)


def test_k40_future_row_rejected(base_dir):
    """45（实现级）：篡改时间线添入序号 > end 的未来行 → check-only 3。"""
    root = _root(base_dir)
    make_chunks(root / "chunks", 12)
    _backup(root, base_dir, 1, "read")
    for i in range(1, 6):
        make_reader_json(root, i)
    tl.append_batch(root, batch_id=1, current_batch=0, chunk_padding=3,
                    start=1, end=5)
    p = root / "plot_timeline.md"
    atomic_write_text(p, p.read_text(encoding="utf-8")
                      + "| 6 | part_007 | 未来行 | 影响 |\n")
    state, errs = tl.check_only(p, start=6, end=10, total_chunks=12,
                                chunk_padding=3, processed_chunks=5,
                                chunks_dir=root / "chunks")
    assert any("K40" in e for e in errs)


def test_g13_chunks_collection_change(base_dir):
    root = _root(base_dir)
    make_chunks(root / "chunks", 12)
    (root / "chunks" / "part_013.txt").write_text("多出的一片", encoding="utf-8")
    _, errs = tl.check_only(root / "plot_timeline.md", start=1, end=5,
                            total_chunks=12, chunk_padding=3,
                            processed_chunks=0, chunks_dir=root / "chunks")
    assert any("G13" in e for e in errs)


# ---------------------------------------------------------------------------
# K33 tail 钳制 / 空范围
# ---------------------------------------------------------------------------

def test_tail_k33_clamp_and_empty_range(base_dir):
    root = _root(base_dir)
    _backup(root, base_dir, 1, "read")
    for i in range(1, 6):
        make_reader_json(root, i)
    tl.append_batch(root, batch_id=1, current_batch=0, chunk_padding=3,
                    start=1, end=5)
    p = root / "plot_timeline.md"
    assert tl.tail(p, -5, 3) == tl.tail(p, 1, 3)          # 钳制 max(1,·)
    assert len(tl.tail(p, -5, 3)) == 3
    assert tl.tail(p, 6, 5) == []                          # 钳制后空范围 → 空
    assert tl.tail(p, 9, 99) == []                         # 越界 → 空
    assert len(tl.tail_lines(p, 2)) == 2
    assert tl.tail_lines(p, 0) == []


# ---------------------------------------------------------------------------
# K20 行数上限：tail_capped 整片裁剪（丢最旧片、保最新事件）
# ---------------------------------------------------------------------------

def _tl_with_dense_rows(base_dir, per_chunk: dict[int, int]) -> Path:
    """构造时间线：per_chunk = {片号: 事件行数}，顺序 1..N 连续。"""
    root = _root(base_dir)
    p = root / "plot_timeline.md"
    lines: list[str] = []
    seq = 1
    for num, n in sorted(per_chunk.items()):
        for _ in range(n):
            lines.append(f"| {seq} | part_{num:03d} | 事件{seq} | 影响{seq} |")
            seq += 1
    atomic_write_text(p, tl.skeleton() + "\n".join(lines) + "\n")
    return p


def test_tail_capped_no_trim_when_under_cap(base_dir):
    """行数 ≤ 上限 → 全量返回、dropped=None（窗口=片数语义不被行数干扰）。"""
    p = _tl_with_dense_rows(base_dir, {1: 3, 2: 3, 3: 3})   # 9 行 / 3 片
    lines, dropped = tl.tail_capped(p, 1, 3, max_rows=15)
    assert dropped is None
    assert len(lines) == 9
    assert "part_001" in lines[0] and "part_003" in lines[-1]


def test_tail_capped_trims_oldest_whole_chunks(base_dir):
    """超限 → 从最旧整片裁剪：part_001 的 5 行整片丢弃，part_002/003 完整保留。"""
    p = _tl_with_dense_rows(base_dir, {1: 5, 2: 3, 3: 3})   # 11 行 / 3 片
    lines, dropped = tl.tail_capped(p, 1, 3, max_rows=7)
    assert dropped == 1
    assert len(lines) == 6                                 # 2+3 = 6 ≤ 7
    assert not any("part_001" in ln for ln in lines)       # 最旧片整片丢弃
    assert "part_002" in lines[0] and "part_003" in lines[-1]


def test_tail_capped_single_chunk_kept_even_over_cap(base_dir):
    """保底：窗口仅剩一片时即使超限也保留（宁全勿空）。"""
    p = _tl_with_dense_rows(base_dir, {1: 5})
    lines, dropped = tl.tail_capped(p, 1, 1, max_rows=3)
    assert dropped is None
    assert len(lines) == 5


def test_tail_capped_empty_and_clamp(base_dir):
    """空范围/越界 → 空；chunk-start 钳制 max(1,·)。"""
    p = _tl_with_dense_rows(base_dir, {1: 2, 2: 2})
    assert tl.tail_capped(p, 6, 5, max_rows=10) == ([], None)
    lines, dropped = tl.tail_capped(p, -5, 2, max_rows=10)
    assert dropped is None and len(lines) == 4
