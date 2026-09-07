"""core/recovery.py — init / --recover / 快照与回滚（§6.4 / §1.5 / K2 / K4 / K11 / K18 / K19 / K22 / K23 / K24 / K32 / K37 / K42）。

- `init_novel`：§6.4 全 11 步（K37 带数据时间线守卫、K2 参数前置、B3 编码探测、
  K19 快照先行、I4 幂等分支、D2 失败兜底——任一失败不写 metadata.json）；
- `recover`：--recover 子流程（E4 连续前缀推导、K4 恒等式、K11 快照恢复、
  K18 上界、K1 current_batch 从 .rollback 最大 id 推导）；
- `backup`：K23 三态（不存在→写；完好→幂等覆写；损坏→2 需 --force）；
- `rollback`：read/summarize 两相（K22 不删 .batch\\、快照只恢复不删）；
- `purge_rollback`：K32 例外——失败仅 WARN 不阻塞（done 终态保持）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import config as cfgmod
from .config import Config, build_snapshot, compute_fingerprint, ensure_api_key, validate_init_params
from .lock import case_collision
from .logger import Logger
from .paths import InvalidName, base_dir, validate_novel_name
from .state import (DEFAULT_RUN_PARAMS, MetadataCorrupt, MetadataMissing,
                    RootDirMismatch, create_metadata, read_metadata,
                    set_error)
from .timeline import (TimelineError, count_chars, derive_processed,
                       file_check, has_data_rows, skeleton)

_PART_FILE_RE = re.compile(r"^part_(\d+)\.txt$")
_READ_SNAP_RE = re.compile(r"^batch_(\d+)_read_timeline\.md$")
_SUM_SNAP_RE = re.compile(r"^batch_(\d+)_summarize_summary\.md$")
_NOTES_RE = re.compile(r"^part_(\d+)~part_(\d+)\.md$")


class InitError(Exception):
    """init/--recover 失败；code ∈ 退出码 {2,3,4}（§8.1）。"""

    def __init__(self, msg: str, code: int):
        super().__init__(msg)
        self.code = code


class BackupError(Exception):
    """backup 失败；code = 2（损坏未 --force，K23）或 3（数据缺失）。"""

    def __init__(self, msg: str, code: int):
        super().__init__(msg)
        self.code = code


class RollbackError(Exception):
    """rollback 失败；code = 2（参数）或 3（无快照/恢复失败，K24 零容忍）。"""

    def __init__(self, msg: str, code: int):
        super().__init__(msg)
        self.code = code


# ---------------------------------------------------------------------------
# 分片扫描（§6.4 步骤 4–7，init/recover 共用）
# ---------------------------------------------------------------------------

def scan_chunks(root: Path, chunk_max_chars: int) -> tuple[int, int]:
    """步骤 4–7：chunks\\ 存在非空 → 命名连续/零填充一致 → B3 编码探测 →
    单片字数校验。返回 (total_chunks, chunk_padding)；违规抛 InitError。
    """
    chunks = root / "chunks"
    if not chunks.is_dir():
        raise InitError("chunks\\ 不存在（§6.4 步骤 4）", 2)
    files = sorted(chunks.glob("part_*.txt"))
    if not files:
        raise InitError("chunks\\ 为空（步骤 4）", 2)
    nums: list[int] = []
    bad_names: list[str] = []
    for f in files:
        m = _PART_FILE_RE.match(f.name)
        if m:
            nums.append(int(m.group(1)))
        else:
            bad_names.append(f.name)
    if bad_names:
        raise InitError(f"chunks\\ 命名非法（须 part_\\d+.txt）: {bad_names}", 3)
    nums.sort()
    if nums != list(range(1, len(nums) + 1)):
        missing = sorted(set(range(1, max(nums) + 1)) - set(nums))
        raise InitError(
            f"分片命名不连续/有缺口（步骤 5）: 缺 {missing[:10]}", 3)
    # 零填充位数 = 文件名数字串宽度（part_001 → 3），非 int 值宽度
    padding = len(_PART_FILE_RE.match(files[0].name).group(1))
    if any(len(_PART_FILE_RE.match(f.name).group(1)) != padding
           for f in files[1:]):
        raise InitError(
            f"分片零填充不一致（步骤 5）: 须一致 {padding} 位", 3)
    # B3 编码探测（步骤 6）：严格 UTF-8，任一失败或含 BOM → 4（只读不改写）
    bad_enc: list[str] = []
    for f in files:
        raw = f.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            bad_enc.append(f"{f.name}（含 BOM）")
            continue
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            bad_enc.append(f.name)
    if bad_enc:
        raise InitError(f"分片编码探测失败（步骤 6，B3）: {bad_enc}", 4)
    # 步骤 7：单片字数 > chunk_max_chars → 3
    over = [(f.name, count_chars(f.read_text(encoding="utf-8")))
            for f in files
            if count_chars(f.read_text(encoding="utf-8")) > chunk_max_chars]
    if over:
        raise InitError(
            f"单片字数超限 > {chunk_max_chars}（步骤 7）: {over[:10]}", 3)
    return len(nums), padding


def _build_snapshot_file(root: Path, config: Config, *,
                         batch_size: int, timeline_window: int,
                         summary_max: int, chunk_max_chars: int,
                         chunk_padding: int, prompt_version: str) -> None:
    """§3.4 K19：脱敏快照先行；写失败抛 InitError(2)（→ D2 不写 metadata）。"""
    snap = build_snapshot(config, batch_size=batch_size,
                          timeline_window=timeline_window,
                          summary_max=summary_max,
                          chunk_max_chars=chunk_max_chars,
                          chunk_padding=chunk_padding,
                          prompt_version=prompt_version)
    try:
        from .state import atomic_write_json
        atomic_write_json(root / "config.snapshot.json", snap)
    except OSError as exc:
        raise InitError(f"K19: config.snapshot.json 写失败 → 整体 init 失败（D2）: {exc}", 2) from exc


def _load_snapshot(root: Path) -> dict:
    """读 config.snapshot.json（K11 运行参数）；缺失/损坏 → 空 dict。"""
    p = root / "config.snapshot.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _summary_result(root: Path, doc=None, *, idempotent: bool = False) -> dict:
    from .state import MetaDoc
    d = doc or MetaDoc()
    return {
        "novel": d.novel_name, "root_dir": str(root),
        "total_chunks": d.total_chunks, "processed_chunks": d.processed_chunks,
        "summary_chunks": d.summary_chunks, "current_batch": d.current_batch,
        "status": d.status, "batch_size": d.batch_size,
        "timeline_window": d.timeline_window, "summary_max": d.summary_max,
        "chunk_max_chars": d.chunk_max_chars, "chunk_padding": d.chunk_padding,
        "prompt_version": d.prompt_version,
        "config_fingerprint": d.config_fingerprint,
        "idempotent": idempotent,
    }


# ---------------------------------------------------------------------------
# init（§6.4 全 11 步）
# ---------------------------------------------------------------------------

def init_novel(base: Path, novel_name: str, config: Config, *,
               batch_size: int = 5, timeline_window: int = 10,
               summary_max: int = 5000, chunk_max_chars: int = 20000,
               prompt_version: str = "v1", logger: Logger | None = None) -> dict:
    """步骤 1–11；D2：任一失败不写 metadata.json。返回摘要 dict。"""
    # 1. validate_novel_name
    try:
        name = validate_novel_name(novel_name)
    except InvalidName as exc:
        raise InitError(str(exc), 2) from exc
    # 2. K2 参数前置 + K28 硬校验（任何落盘之前）；缺 key → 2
    try:
        validate_init_params(config, batch_size=batch_size,
                             timeline_window=timeline_window,
                             summary_max=summary_max,
                             chunk_max_chars=chunk_max_chars)
        ensure_api_key(config)
    except cfgmod.ConfigError as exc:
        raise InitError(str(exc), 2) from exc
    coll = case_collision(base, name)
    if coll:
        raise InitError(
            f"K26: 大小写碰撞——{coll!r} 已存在（Windows 两锁管一套 metadata）", 3)

    root = base / name
    meta_path = root / "metadata.json"

    # I4 幂等分支：metadata 已存在
    if meta_path.exists():
        try:
            doc = read_metadata(meta_path, base=base)
        except RootDirMismatch as exc:
            raise InitError(str(exc), 3) from exc
        except MetadataCorrupt as exc:
            set_error(meta_path, f"init 遇损坏 metadata: {exc}", base=base)
            raise InitError("metadata 损坏，已 emergency；请按 §10.1 A 恢复", 3) from exc
        if doc.status == "init" and doc.processed_chunks == 0:
            tl, sm = root / "plot_timeline.md", root / "summary.md"
            if not tl.exists() or not sm.exists():
                return _summary_result(root, doc, idempotent=True)   # 缺失 → 0 不重建
            try:
                if has_data_rows(tl.read_text(encoding="utf-8")):
                    raise InitError("I4: status=init 但时间线含数据行", 2)
            except (OSError, UnicodeDecodeError) as exc:
                raise InitError(f"时间线读取失败: {exc}", 3) from exc
            return _summary_result(root, doc, idempotent=True)
        raise InitError(
            f"I4: metadata 已存在（status={doc.status}, processed={doc.processed_chunks}）", 2)

    # 3. K37 带数据时间线守卫（metadata 缺失）
    tl_path = root / "plot_timeline.md"
    if tl_path.exists():
        try:
            if has_data_rows(tl_path.read_text(encoding="utf-8")):
                raise InitError(
                    "K37: 时间线已有数据，禁普通 init 重建（避免状态与内容分叉）；"
                    "请改用 init --recover", 3)
        except (OSError, UnicodeDecodeError) as exc:
            raise InitError(f"时间线读取失败: {exc}", 3) from exc

    # 4–7. 分片扫描（chunks 存在/命名/编码/字数）
    total, padding = scan_chunks(root, chunk_max_chars)

    # 8. 建目录 + 骨架（已存在跳过——K37 已保证至多骨架）
    for sub in ("notes", ".rollback", ".batch"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    if not tl_path.exists():
        from .state import atomic_write_text
        atomic_write_text(tl_path, skeleton())
    sm_path = root / "summary.md"
    if not sm_path.exists():
        from .state import atomic_write_text
        atomic_write_text(sm_path, "")

    # 9. K19 快照先行（失败 → 不写 metadata，整体失败走 D2）
    _build_snapshot_file(root, config, batch_size=batch_size,
                         timeline_window=timeline_window,
                         summary_max=summary_max,
                         chunk_max_chars=chunk_max_chars,
                         chunk_padding=padding, prompt_version=prompt_version)

    # 10. metadata（最后一步）
    fingerprint = compute_fingerprint(config)
    try:
        doc = create_metadata(meta_path, novel_name=name, root_dir=str(root),
                              total_chunks=total,
                              timeline_window=timeline_window,
                              batch_size=batch_size, summary_max=summary_max,
                              chunk_padding=padding,
                              chunk_max_chars=chunk_max_chars,
                              prompt_version=prompt_version,
                              config_fingerprint=fingerprint, base=base)
    except OSError as exc:
        raise InitError(f"metadata 写失败（D2）: {exc}", 2) from exc

    # 11. log + 摘要
    if logger is not None:
        logger.ok("driver", "init", None,
                  f"novel={name} total={total} processed=0 current_batch=0")
    return _summary_result(root, doc)


# ---------------------------------------------------------------------------
# --recover（§6.4）
# ---------------------------------------------------------------------------

def recover(base: Path, novel_name: str, config: Config, *,
            batch_size: int | None = None, timeline_window: int | None = None,
            summary_max: int | None = None, chunk_max_chars: int | None = None,
            summary_chunks: int | None = None, current_batch: int | None = None,
            prompt_version: str | None = None,
            logger: Logger | None = None) -> dict:
    """--recover：从时间线/快照/.rollback 重建 metadata（K1/K4/K11/K18）。"""
    try:
        name = validate_novel_name(novel_name)
    except InvalidName as exc:
        raise InitError(str(exc), 2) from exc
    root = base / name
    meta_path = root / "metadata.json"
    if meta_path.exists():
        raise InitError("--recover 仅用于 metadata 缺失；已存在请走 init（I4）", 2)

    snap = _load_snapshot(root)
    snap_run = snap.get("run") or {}

    def sp(key: str, default):
        v = snap_run.get(key)
        return v if v is not None else default

    batch_size = batch_size if batch_size is not None else int(sp("batch_size", DEFAULT_RUN_PARAMS["batch_size"]))
    timeline_window = timeline_window if timeline_window is not None else int(sp("timeline_window", DEFAULT_RUN_PARAMS["timeline_window"]))
    summary_max = summary_max if summary_max is not None else int(sp("summary_max", DEFAULT_RUN_PARAMS["summary_max"]))
    chunk_max_chars = chunk_max_chars if chunk_max_chars is not None else int(sp("chunk_max_chars", DEFAULT_RUN_PARAMS["chunk_max_chars"]))
    prompt_version = prompt_version or str(sp("prompt_version", DEFAULT_RUN_PARAMS["prompt_version"]))

    try:
        validate_init_params(config, batch_size=batch_size,
                             timeline_window=timeline_window,
                             summary_max=summary_max,
                             chunk_max_chars=chunk_max_chars)
    except cfgmod.ConfigError as exc:
        raise InitError(str(exc), 2) from exc

    derived_batch = derive_current_batch(root)
    # K11：显式传参与快照不一致 → 3，除非 derived_batch==0（无已提交批）
    if derived_batch != 0 and snap_run:
        for key, val in (("batch_size", batch_size),
                         ("timeline_window", timeline_window),
                         ("summary_max", summary_max),
                         ("chunk_max_chars", chunk_max_chars),
                         ("prompt_version", prompt_version)):
            if key not in snap_run:
                continue
            snap_val = snap_run[key]
            if key == "prompt_version":
                conflict = str(snap_val) != str(val)
            else:
                conflict = int(snap_val) != int(val)
            if conflict:
                raise InitError(
                    f"K11: 显式传参 {key}={val} 与快照 {snap_val} 不一致"
                    "（已提交批受影响）；如确要改请清 .rollback 或 metadata 重建", 3)

    total, padding = scan_chunks(root, chunk_max_chars)

    tl_path = root / "plot_timeline.md"
    if tl_path.exists():
        errs = file_check(tl_path, total_chunks=total, chunk_padding=padding)
        if errs:
            raise InitError("--recover: 时间线硬校验失败（§9.3 子集）: "
                            + "; ".join(errs), 3)
        processed = derive_processed(tl_path)          # E4 连续前缀
    else:
        processed = 0

    # K4 恒等式：processed - summary ≤ window + batch_size
    s_chunks = 0 if summary_chunks is None else summary_chunks
    if not (0 <= s_chunks <= processed):
        raise InitError(
            f"K4: --summary-chunks {s_chunks} 须 ∈ [0, processed={processed}]", 3)
    if processed - s_chunks > timeline_window + batch_size:
        raise InitError(
            f"K4 恒等式违例: processed({processed}) - summary_chunks({s_chunks})"
            f" = {processed - s_chunks} > window+batch"
            f"({timeline_window}+{batch_size})；三出口：① 传 --summary-chunks N "
            "② 调大 --timeline-window ③ 分卷", 3)

    # K1/K18：current_batch 推导 + 上界
    cb = derived_batch if current_batch is None else current_batch
    if current_batch is None and cb > _ceil_div(processed, batch_size):
        raise InitError(
            f"K18: 推导 current_batch={cb} > ceil(processed/batch_size)="
            f"{_ceil_div(processed, batch_size)}；清理孤快照或显式 --current-batch N", 3)
    if cb < 0:
        raise InitError("current_batch 须 ≥ 0", 2)

    # 恢复后 summary.md 字数 > 新 summary_max → 仅警告（§10.1 A 步骤 4）
    sm_path = root / "summary.md"
    if sm_path.exists():
        try:
            if count_chars(sm_path.read_text(encoding="utf-8")) > summary_max:
                if logger is not None:
                    logger.warn("driver", "recover", None,
                                f"summary.md 字数 > 新 summary_max({summary_max})，仅警告")
        except (OSError, UnicodeDecodeError):
            pass
    # notes\\ 命名范围一致性（缺失仅警告）
    notes_dir = root / "notes"
    if notes_dir.is_dir():
        for f in notes_dir.glob("*.md"):
            m = _NOTES_RE.match(f.name)
            if not m or not (1 <= int(m.group(1)) <= int(m.group(2)) <= total):
                if logger is not None:
                    logger.warn("driver", "recover", None,
                                f"notes 命名越界（仅警告）: {f.name}")

    # 落盘：目录/骨架 → 快照（K11 刷新）→ metadata（含推导值）
    for sub in ("notes", ".rollback", ".batch"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    from .state import atomic_write_text
    if not tl_path.exists():
        atomic_write_text(tl_path, skeleton())
    if not sm_path.exists():
        atomic_write_text(sm_path, "")
    _build_snapshot_file(root, config, batch_size=batch_size,
                         timeline_window=timeline_window,
                         summary_max=summary_max,
                         chunk_max_chars=chunk_max_chars,
                         chunk_padding=padding, prompt_version=prompt_version)
    fingerprint = compute_fingerprint(config)
    try:
        doc = create_metadata(meta_path, novel_name=name, root_dir=str(root),
                              total_chunks=total,
                              timeline_window=timeline_window,
                              batch_size=batch_size, summary_max=summary_max,
                              chunk_padding=padding,
                              chunk_max_chars=chunk_max_chars,
                              prompt_version=prompt_version,
                              config_fingerprint=fingerprint, base=base,
                              processed_chunks=processed,
                              summary_chunks=s_chunks, current_batch=cb)
    except OSError as exc:
        raise InitError(f"metadata 写失败（D2）: {exc}", 2) from exc
    if logger is not None:
        logger.ok("driver", "recover", None,
                  f"novel={name} processed={processed} current_batch={cb}")
    return _summary_result(root, doc)


def _ceil_div(a: int, b: int) -> int:
    return -(-a // b) if b > 0 else 0


# ---------------------------------------------------------------------------
# 快照 / 回滚（§1.5，K22 / K23 / K24 / K32）
# ---------------------------------------------------------------------------

def derive_current_batch(root: Path) -> int:
    """K1：从 .rollback\\batch_<id>_read_* 最大 id 推导；失败/无 → 0。"""
    rb = root / ".rollback"
    if not rb.is_dir():
        return 0
    ids = [int(m.group(1)) for f in rb.glob("batch_*_read_timeline.md")
           if (m := _READ_SNAP_RE.match(f.name))]
    return max(ids) if ids else 0


def _snapshot_target(root: Path, batch_id: int, phase: str) -> Path:
    return root / ".rollback" / (f"batch_{batch_id}_read_timeline.md"
                                 if phase == "read"
                                 else f"batch_{batch_id}_summarize_summary.md")


def backup(base: Path, novel_name: str, *, batch_id: int, phase: str,
           force: bool = False, logger: Logger | None = None) -> dict:
    """K23 三态：不存在→写(0)；完好→幂等覆写(0)；损坏→2（需 --force）。"""
    if phase not in ("read", "summarize"):
        raise BackupError(f"--phase 须 read|summarize，实际 {phase!r}", 2)
    if batch_id < 1:
        raise BackupError(f"batch_id 须 ≥ 1，实际 {batch_id}", 2)
    try:
        name = validate_novel_name(novel_name)
    except InvalidName as exc:
        raise BackupError(str(exc), 2) from exc
    root = base / name
    if not root.is_dir():
        raise BackupError(f"ROOT 不存在（先 init）: {root}", 3)
    source = root / ("plot_timeline.md" if phase == "read" else "summary.md")
    if not source.exists():
        raise BackupError(f"快照源缺失: {source.name}", 3)
    try:
        content = source.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise BackupError(f"快照源不可读: {source.name}（{exc}）", 3) from exc

    target = _snapshot_target(root, batch_id, phase)
    target.parent.mkdir(parents=True, exist_ok=True)
    state = "written"
    if target.exists():
        try:
            target.read_text(encoding="utf-8")        # 完好判定：严格 UTF-8 可读
            state = "overwritten"                     # K23 幂等覆写
        except (OSError, UnicodeDecodeError):
            if not force:
                raise BackupError(
                    f"K23: 快照损坏 {target.name}，需 --force", 2) from None
            state = "forced"
    from .state import atomic_write_text
    atomic_write_text(target, content)
    if logger is not None:
        logger.ok("driver", "backup", batch_id,
                  f"phase={phase} {state} snapshot={target.name}")
    return {"status": state, "batch_id": batch_id, "phase": phase,
            "snapshot": str(target)}


def rollback(base: Path, novel_name: str, *, phase: str,
             logger: Logger | None = None) -> dict:
    """K22/K24：从最新快照恢复（read→timeline，summarize→summary）。

    - 快照只恢复不删；不删 .batch\\；不碰 notes；
    - 无快照/恢复失败 → RollbackError（调用方置 error，禁止自动重试）。
    """
    if phase not in ("read", "summarize"):
        raise RollbackError(f"--phase 须 read|summarize，实际 {phase!r}", 2)
    try:
        name = validate_novel_name(novel_name)
    except InvalidName as exc:
        raise RollbackError(str(exc), 2) from exc
    root = base / name
    rb = root / ".rollback"
    if not rb.is_dir():
        raise RollbackError(f"无 .rollback\\ 目录（无快照可恢复）: {root}", 3)
    pattern = _READ_SNAP_RE if phase == "read" else _SUM_SNAP_RE
    snaps = [f for f in rb.glob("batch_*.md") if pattern.match(f.name)]
    if not snaps:
        raise RollbackError(f"无 {phase} 快照可恢复（{rb}）", 3)
    snap = max(snaps, key=lambda f: int(pattern.match(f.name).group(1)))
    try:
        content = snap.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RollbackError(f"快照不可读: {snap.name}（{exc}）", 3) from exc
    target = root / ("plot_timeline.md" if phase == "read" else "summary.md")
    try:
        from .state import atomic_write_text
        atomic_write_text(target, content)
    except OSError as exc:
        raise RollbackError(f"回滚写失败: {target.name}（{exc}）", 3) from exc
    batch_id = int(pattern.match(snap.name).group(1))
    if logger is not None:
        logger.ok("driver", "rollback", batch_id,
                  f"phase={phase} restored={snap.name}")
    return {"status": "restored", "batch_id": batch_id, "phase": phase,
            "snapshot": str(snap)}


def purge_rollback(root: Path, *, logger: Logger | None = None) -> None:
    """K32：--purge 例外——失败仅 log WARN 不阻塞（done 终态保持）。"""
    rb = root / ".rollback"
    if not rb.is_dir():
        return
    for f in rb.glob("batch_*.md"):
        try:
            f.unlink()
        except OSError as exc:
            if logger is not None:
                logger.warn("driver", "purge", None,
                            f"快照清理失败（仅警告，K32）: {f.name}（{exc}）")
