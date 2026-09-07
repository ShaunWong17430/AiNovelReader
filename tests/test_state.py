"""出口测试 25/26/27 + §1.3/§6.1：core/state.py（schema / 原子写 / K1 / K13 / K31 / K36 / J11）。

覆盖：
- 25. emergency：metadata.corrupt_<ts>（无冒号）+ 最小 error metadata 过自身 schema；
- 26. D2：原文件不存在 → 跳过改名、不输出「已改名」，error 字段=具体原因；
- 27. J11：root_dir 不一致 → RootDirMismatch → 3，不改名不重置；
- K31：缺失 = MetadataMissing 标记（不 emergency、不写文件）；
- K1：current_batch 单调 +1（--processed 提交）；
- K13：占位态（-1）拒 --status running（提示 §10.1 A）；
- K36：--summary 仅「等于」no-op；< 当前拒（严格单调）；
- B6 状态机白名单 + done 终态 + J6 + D15/G1 人工恢复清 error。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import core.state as statemod

from helpers import write_config


def _mk_root(base: Path, name: str = "示例书名") -> Path:
    root = base / name
    root.mkdir(parents=True, exist_ok=True)
    return root


def _create(base: Path, *, total: int = 12, name: str = "示例书名",
            batch_size: int = 5, **kw) -> tuple[Path, Path]:
    root = _mk_root(base, name)
    meta = root / "metadata.json"
    statemod.create_metadata(
        meta, novel_name=name, root_dir=str(root), total_chunks=total,
        timeline_window=kw.get("timeline_window", 10), batch_size=batch_size,
        summary_max=5000, chunk_padding=3, chunk_max_chars=20000,
        prompt_version="v1", config_fingerprint="sha256:test", base=base)
    return meta, root


# ---------------------------------------------------------------------------
# 基本读写 / 原子写 / K31 缺失标记
# ---------------------------------------------------------------------------

def test_create_read_roundtrip(base_dir):
    meta, root = _create(base_dir)
    doc = statemod.read_metadata(meta, base=base_dir)
    assert doc.status == "init" and doc.processed_chunks == 0
    assert doc.current_batch == 0 and doc.total_chunks == 12
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$",
                    doc.created_at)
    # 原子写：无临时文件残留
    assert not list(root.glob(".metadata.json.tmp*"))
    assert not list(root.glob("metadata.json.tmp*"))


def test_k31_missing_is_marker_not_emergency(base_dir):
    meta, root = _create(base_dir)
    missing = root / "metadata.json"
    missing.unlink()
    with pytest.raises(statemod.MetadataMissing):
        statemod.read_metadata(missing)
    assert issubclass(statemod.MetadataMissing, FileNotFoundError)
    # 不触发 emergency、不写任何文件
    assert not list(root.glob("metadata.corrupt_*"))
    with pytest.raises(statemod.StateError):
        statemod.update_metadata(missing, base=base_dir, status="running")


def test_corrupt_json_raises_corrupt(base_dir):
    meta, root = _create(base_dir)
    meta.write_text("{ not json", encoding="utf-8")
    with pytest.raises(statemod.MetadataCorrupt, match="解析失败"):
        statemod.read_metadata(meta)


def test_missing_field_raises_corrupt(base_dir):
    meta, root = _create(base_dir)
    data = json.loads(meta.read_text(encoding="utf-8"))
    del data["timeline_window"]
    meta.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(statemod.MetadataCorrupt, match="字段缺失"):
        statemod.read_metadata(meta)


def test_wrong_type_raises_corrupt(base_dir):
    meta, root = _create(base_dir)
    data = json.loads(meta.read_text(encoding="utf-8"))
    data["total_chunks"] = "12"
    meta.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(statemod.MetadataCorrupt, match="total_chunks"):
        statemod.read_metadata(meta)


# ---------------------------------------------------------------------------
# K1 / 严格单调 / 单批增量（测试 30 语义在阶段 3 出口，此处实现级验证）
# ---------------------------------------------------------------------------

def test_processed_commit_k1_monotonic_counter(base_dir):
    meta, _ = _create(base_dir)
    doc = statemod.update_metadata(meta, base=base_dir, processed=5)
    assert doc.processed_chunks == 5 and doc.current_batch == 1
    doc = statemod.update_metadata(meta, base=base_dir, processed=10)
    assert doc.processed_chunks == 10 and doc.current_batch == 2
    doc = statemod.read_metadata(meta, base=base_dir)
    assert doc.current_batch == 2                 # 绝不重算


def test_processed_rejects_old_equal_and_two_batches(base_dir):
    meta, _ = _create(base_dir, total=20)
    statemod.update_metadata(meta, base=base_dir, processed=5)
    for bad in (5, 4, 0):                         # 旧值 / +0
        with pytest.raises(statemod.StateError, match="严格递增"):
            statemod.update_metadata(meta, base=base_dir, processed=bad)
    with pytest.raises(statemod.StateError, match="单批增量"):   # +2n（5→15 > batch 5）
        statemod.update_metadata(meta, base=base_dir, processed=15)
    with pytest.raises(statemod.StateError, match="total"):
        statemod.update_metadata(meta, base=base_dir, processed=21)   # > total


def test_processed_clears_batch_retries(base_dir):
    meta, _ = _create(base_dir)
    statemod.update_metadata(meta, base=base_dir, retries=2)
    doc = statemod.update_metadata(meta, base=base_dir, processed=5)
    assert doc.batch_retries == 0                 # 成功验收原子清零


# ---------------------------------------------------------------------------
# K36 --summary：仅「等于」no-op；< 当前拒；≤ processed
# ---------------------------------------------------------------------------

def test_summary_equal_noop(base_dir):
    meta, _ = _create(base_dir)
    doc = statemod.update_metadata(meta, base=base_dir, summary=0)
    assert doc.summary_chunks == 0                # == 当前 → no-op 成功
    doc2 = statemod.read_metadata(meta, base=base_dir)
    assert doc2.summary_chunks == 0


def test_summary_strict_monotonic(base_dir):
    meta, _ = _create(base_dir)
    statemod.update_metadata(meta, base=base_dir, processed=5)
    statemod.update_metadata(meta, base=base_dir, summary=5)
    with pytest.raises(statemod.StateError, match="严格单调"):
        statemod.update_metadata(meta, base=base_dir, summary=3)   # < 当前 → 拒
    with pytest.raises(statemod.StateError, match="processed"):
        statemod.update_metadata(meta, base=base_dir, summary=6)   # > processed → 拒
    doc = statemod.update_metadata(meta, base=base_dir, summary=5)  # == 当前 → no-op
    assert doc.summary_chunks == 5


def test_summary_clears_summary_retries(base_dir):
    meta, _ = _create(base_dir)
    statemod.update_metadata(meta, base=base_dir, processed=5,
                             summary_retries=2)
    doc = statemod.update_metadata(meta, base=base_dir, summary=5)
    assert doc.summary_retries == 0


# ---------------------------------------------------------------------------
# B6 状态机 + done 终态 + J6 + D15/G1 人工恢复
# ---------------------------------------------------------------------------

def test_status_machine_whitelist(base_dir):
    meta, _ = _create(base_dir)
    statemod.update_metadata(meta, base=base_dir, status="running")
    statemod.update_metadata(meta, base=base_dir, status="running")  # 幂等
    statemod.update_metadata(meta, base=base_dir, status="error", error="原因")
    statemod.update_metadata(meta, base=base_dir, status="error")    # 幂等
    statemod.update_metadata(meta, base=base_dir, status="running")  # 人工恢复
    doc = statemod.update_metadata(meta, base=base_dir, status="done")
    assert doc.status == "done"


def test_done_terminal_rejects_all(base_dir):
    meta, _ = _create(base_dir)
    statemod.update_metadata(meta, base=base_dir, status="running")
    statemod.update_metadata(meta, base=base_dir, status="done")
    with pytest.raises(statemod.StateError, match="done"):
        statemod.update_metadata(meta, base=base_dir, status="running")   # J6/§1.3
    with pytest.raises(statemod.StateError, match="done"):
        statemod.update_metadata(meta, base=base_dir, status="error")     # J6
    with pytest.raises(statemod.StateError, match="done"):
        statemod.update_metadata(meta, base=base_dir, processed=12)
    with pytest.raises(statemod.StateError, match="done"):
        statemod.update_metadata(meta, base=base_dir, summary=12)


def test_error_to_running_manual_recovery_clears_state(base_dir):
    meta, _ = _create(base_dir)
    statemod.update_metadata(meta, base=base_dir, status="running")
    statemod.update_metadata(meta, base=base_dir, status="error",
                             error="LLM 耗尽")
    statemod.update_metadata(meta, base=base_dir, retries=2,
                             summary_retries=2)
    doc = statemod.update_metadata(meta, base=base_dir, status="running")
    assert doc.error is None and doc.recovery_required is False
    assert doc.batch_retries == 0 and doc.summary_retries == 0     # D15/G1


def test_init_to_error_allowed(base_dir):
    meta, _ = _create(base_dir)
    doc = statemod.update_metadata(meta, base=base_dir, status="error",
                                   error="init 失败原因")
    assert doc.status == "error" and doc.error == "init 失败原因"


def test_illegal_transition_rejected(base_dir):
    meta, _ = _create(base_dir)
    with pytest.raises(statemod.StateError, match="白名单"):
        statemod.update_metadata(meta, base=base_dir, status="done")  # init→done 不在白名单


# ---------------------------------------------------------------------------
# K13 占位态（-1）拒 --status running
# ---------------------------------------------------------------------------

def test_k13_placeholder_rejects_running(base_dir):
    meta, root = _create(base_dir)
    statemod.set_error(meta, "测试 emergency", base=base_dir)
    doc = statemod.read_metadata(meta, base=base_dir)
    assert doc.total_chunks == -1 and doc.processed_chunks == -1
    with pytest.raises(statemod.StateError, match="K13"):
        statemod.update_metadata(meta, base=base_dir, status="running")


# ---------------------------------------------------------------------------
# 25. emergency 全流程 / 26. D2 跳过改名 / 27. J11 root_dir 不一致
# ---------------------------------------------------------------------------

def test_emergency_renames_and_writes_valid_minimal_doc(base_dir):
    meta, root = _create(base_dir)
    meta.write_text("{ broken json", encoding="utf-8")
    res = statemod.set_error(meta, "现场原因 XYZ", base=base_dir)
    assert res.renamed is True
    assert res.corrupt_path is not None
    assert ":" not in res.corrupt_path.name          # D6：timestamp 禁冒号
    assert res.corrupt_path.name.startswith("metadata.corrupt_")
    assert res.corrupt_path.exists()
    # 新 error metadata：进度 -1、status=error、error=MSG、recovery_required
    doc = statemod.read_metadata(meta, base=base_dir)
    assert doc.status == "error" and doc.error == "现场原因 XYZ"
    assert (doc.total_chunks, doc.processed_chunks,
            doc.summary_chunks) == (-1, -1, -1)
    assert doc.recovery_required is True
    # emergency 文件须能过自身 schema（防死循环）
    statemod.validate_doc(doc.to_dict(), base=base_dir)


def test_d2_missing_original_skips_rename(base_dir):
    meta, root = _create(base_dir)
    meta.unlink()                                    # D2：原文件不存在
    res = statemod.set_error(meta, "具体原因：分片超限", base=base_dir)
    assert res.renamed is False and res.corrupt_path is None
    assert not list(root.glob("metadata.corrupt_*"))
    doc = statemod.read_metadata(meta, base=base_dir)
    assert doc.status == "error"
    assert doc.error == "具体原因：分片超限"           # 26：error=具体原因
    assert "corrupted" not in (doc.error or "").lower()


def test_j11_root_dir_mismatch_no_rename_no_reset(base_dir):
    meta, root = _create(base_dir)
    data = json.loads(meta.read_text(encoding="utf-8"))
    data["root_dir"] = str(root.parent / "其它目录")
    meta.write_text(json.dumps(data), encoding="utf-8")
    before = meta.read_bytes()
    with pytest.raises(statemod.RootDirMismatch, match="root_dir"):
        statemod.read_metadata(meta, base=base_dir)
    assert meta.read_bytes() == before               # 不改名不重置（J11）
    assert not list(root.glob("metadata.corrupt_*"))


def test_llm_last_error_validation(base_dir):
    meta, _ = _create(base_dir)
    doc = statemod.update_metadata(meta, base=base_dir, llm_last_error="AUTH")
    assert doc.llm_last_error == "AUTH"
    with pytest.raises(statemod.StateError, match="llm_last_error"):
        statemod.update_metadata(meta, base=base_dir, llm_last_error="BOGUS")


def test_retries_bounds_k8(base_dir):
    meta, _ = _create(base_dir)
    assert statemod.update_metadata(meta, base=base_dir, retries=2).batch_retries == 2
    with pytest.raises(statemod.StateError, match="batch_retries"):
        statemod.update_metadata(meta, base=base_dir, retries=3)
    with pytest.raises(statemod.StateError, match="summary_retries"):
        statemod.update_metadata(meta, base=base_dir, summary_retries=-1)


def test_llm_counters_accumulate(base_dir):
    meta, _ = _create(base_dir)
    statemod.update_metadata(meta, base=base_dir, llm={"calls": 2,
                                                       "prompt_tokens": 100,
                                                       "completion_tokens": 50})
    doc = statemod.update_metadata(meta, base=base_dir, llm={"calls": 1,
                                                             "prompt_tokens": 30,
                                                             "completion_tokens": 20})
    assert (doc.llm_calls, doc.llm_prompt_tokens,
            doc.llm_completion_tokens) == (3, 130, 70)
