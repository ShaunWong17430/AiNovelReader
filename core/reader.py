"""core/reader.py — 阅读阶段（§6.2 推进一批，K5 / K8 / K14 / K22 / K29 / K33 / K39）。

步骤编排（§6.2 1–8，与 IMPLEMENTATION_PLAN D9/D11）：
1.  本批范围由 processed 推导（§1.2；J1：严禁 batch_id 反推）；
2.  幂等预检 check_only 三态（§9.2；APPLIED 快路径 / PARTIAL 置 error 不改文件）；
3.  片级续跑：扫描 .batch\\，已存在且过 §4.4 校验的片跳过（K5）；
4.  backup --phase read（K23 三态；非零 → 立即 error，零容忍）；
5.  逐片：读 chunk（BOM 剔除+警告）→ 字数超限该片 FATAL（K29，不调 LLM）→
    chat（§7.5 内部重试）→ FATAL（K14：不落盘/不回滚/不消耗 retries）→
    非致命失败 → 整批失败走步骤 7 → 写 .batch\\part_XXX.json；
6.  整批产出：append_batch → render_notes → verify_batch + verify_notes
    （任一失败 → 步骤 7）；
7.  失败处置：rollback --phase read（K22 不删 .batch\\；K24 非零立即 error）
    → --retries <batch_retries+1>（绝对赋值）→ ==1 回步骤 3 重建队列
    （.batch 已产出片自动跳过，不重复调 LLM）→ >=2 置 error（K8）；
8.  通过 → --processed（K1 current_batch+1、batch_retries 原子清零）→ 清理 .batch\\。

异常契约（run.py 统一置 error）：
- ProcessError(0) = FATAL（K14，不回滚不重试）；
- ProcessError(2) = 环境/参数（K39 backup/verify 退出码 2 → 立即 error 不回滚）；
- ProcessError(3) = 数据校验失败（PARTIAL / verify 3 / 回滚失败 K24 / 批双重失败 K8）。
"""
from __future__ import annotations

import json
from pathlib import Path

from . import recovery as recovery_mod
from . import renderer as rendermod
from . import timeline as timelinemod
from . import validator as validatormod
from .llm_client import FATAL_CODES, chat
from .logger import Logger
from .paths import root_of
from .prompts import build_reader_prompt
from .state import MetaDoc, atomic_write_text, read_metadata, update_metadata


class ProcessError(Exception):
    """批处理失败；code ∈ {0, 2, 3}（§8.4 处置语义）。"""

    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code


def compute_batch_range(meta: MetaDoc) -> tuple[int, int, int]:
    """§1.2 本批范围唯一权威口径（J1/K1：由 processed 推导）。"""
    start = meta.processed_chunks + 1
    end = min(meta.total_chunks, start + meta.batch_size - 1)
    return start, end, end - start + 1


def build_call_queue(root: Path, meta: MetaDoc, start: int,
                     end: int) -> list[int]:
    """§6.2 步骤 3：待调队列（.batch 已有且过 §4.4 校验的片跳过，K5）。"""
    queue: list[int] = []
    for num in range(start, end + 1):
        chunk_id = f"part_{num:0{meta.chunk_padding}d}"
        f = root / ".batch" / f"{chunk_id}.json"
        if f.exists():
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if not validatormod.validate_reader_output(data, chunk_id):
                    continue                       # 过校验 → 跳过（续跑复用）
            except (OSError, ValueError):
                pass                               # 损坏/不可读 → 重调
        queue.append(num)
    return queue


def _read_chunk(root: Path, meta: MetaDoc, num: int,
                logger: Logger | None) -> str:
    """步骤 5a：读 chunk（UTF-8；BOM → 剔除 + log 警告）。"""
    f = root / "chunks" / f"part_{num:0{meta.chunk_padding}d}.txt"
    try:
        raw = f.read_bytes()
    except OSError as exc:
        raise ProcessError(3, f"chunk 读取失败: {f.name}（{exc}）") from exc
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
        if logger is not None:
            logger.warn("reader", "chunk", None, f"{f.name} 含 BOM，已剔除")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProcessError(3, f"{f.name} 无法 UTF-8 解码: {exc}") from exc


def _clean_ref(text: object) -> str:
    """K43：注入参考文本清洗——折叠空白、半角 `|` 换全角（模板/schema 禁半角 `|`）。"""
    s = " ".join(str(text).split())
    return s.replace("|", "｜")


def _batch_preview_section(root: Path, meta: MetaDoc, chunk_id: str,
                           stats: dict | None = None) -> str:
    """K43 批内接力：读 .batch 中本批已读前片（序号 < 当前片）的 events，
    格式化为参考段；缺失/损坏/字段缺失 → 跳过该片（仅影响参考，不判失败）。

    返回完整段落（含标题）或空串（首片无前片 / 全部跳过 / batch_size=1）。
    可复现性依赖 K22：已成功片不重调、.batch 结果原样保留。

    stats（可选出参，§15 v3.2.3）：回填 {"parts": 实际贡献前片数, "rows": 注入
    事件行数}，供 [window] 日志显示批内接力注入量；返回契约不变（恒为 str），
    故既有调用方（含测试）无需改动。
    """
    def _report(n_parts: int, n_rows: int) -> None:
        if stats is not None:
            stats["parts"] = n_parts
            stats["rows"] = n_rows

    try:
        num = int(chunk_id.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        _report(0, 0)
        return ""
    start = meta.processed_chunks + 1              # 本批首片（批范围由 processed 推导）
    if num <= start:
        _report(0, 0)
        return ""
    lines: list[str] = []
    n_parts = 0
    for i in range(start, num):
        p = root / ".batch" / f"part_{i:0{meta.chunk_padding}d}.json"
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            continue                                # 缺失/损坏 → 跳过（K43 失败隔离）
        if not isinstance(data, dict):
            continue
        evs = data.get("events")
        if not isinstance(evs, list):
            continue
        before = len(lines)
        for e in evs:
            if not isinstance(e, dict) or "event" not in e or "impact" not in e:
                continue
            lines.append(f"[part_{i:0{meta.chunk_padding}d}] 事件："
                         f"{_clean_ref(e['event'])} 影响：{_clean_ref(e['impact'])}")
        if len(lines) > before:                     # 该片有合法事件行 → 计一片
            n_parts += 1
    if not lines:
        _report(0, 0)
        return ""
    _report(n_parts, len(lines))
    return "## 本批已读前片事件（批内接力，仅作背景参考）\n\n" + "\n".join(lines)


def _build_reader_prompt(root: Path, meta: MetaDoc, chunk_id: str,
                         chunk_text: str,
                         logger: Logger | None = None) -> str:
    """§4.2 + K33/K43：summary_text（空则「（无前情）」）+ timeline_tail 窗口钳制
    + K20 行数上限（整片裁剪，保最新事件）+ batch_section（K43 批内接力）。

    [window] 日志（§15 v3.2.3）：`scope=pre-batch` + `inj=Np/Mr`——时间线窗口止于
    上次提交进度，批内即时前情走 K43 注入段，两个量分列显示。"""
    sm = root / "summary.md"
    summary_text = ""
    if sm.exists():
        try:
            summary_text = sm.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            summary_text = ""
    if not summary_text:
        summary_text = "（无前情）"
    tl_start = max(1, meta.processed_chunks - meta.timeline_window + 1)  # K33
    tl_end = meta.processed_chunks
    # K20：时间线段行数上限 = window×5；超限整片裁剪（丢最旧片）保最新
    max_rows = meta.timeline_window * 5
    timeline_lines, dropped = timelinemod.tail_capped(
        root / "plot_timeline.md", tl_start, tl_end, max_rows=max_rows)
    timeline_tail = "\n".join(timeline_lines)
    inj: dict = {}
    batch_section = _batch_preview_section(root, meta, chunk_id, inj)      # K43
    if logger is not None:
        # §15 v3.2.3（可观测性）：scope=pre-batch 明示时间线窗口止于「上次提交
        # 进度」（批内不推进，§6.2/K20/K33）；inj=Np/Mr 显示批内接力段实际注入
        # 的前片数/事件行数（K43，批内第 2..N 片的即时前情）。两段合起来才是
        # 本片真正看到的全部前情——只看 tail 会误判为「窗口没跟上」。
        dropped_s = (f" dropped=part_{dropped:0{meta.chunk_padding}d}"
                     if dropped else "")
        logger.skip("reader", "window", meta.current_batch + 1,
                    f"scope=pre-batch tail=[{tl_start},{tl_end}]"
                    f" rows={len(timeline_lines)} cap={max_rows}"
                    f"{dropped_s} inj={inj['parts']}p/{inj['rows']}r")
    return build_reader_prompt(
        meta.prompt_version, novel_name=meta.novel_name, chunk_id=chunk_id,
        summary_text=summary_text, timeline_tail=timeline_tail,
        chunk_text=chunk_text, event_min=1, event_max=5,
        batch_section=batch_section)


def _call_reader(root: Path, meta: MetaDoc, cfg, num: int,
                 logger: Logger | None, stop_check=None) -> None:
    """步骤 5b–5g：字数校验（K29）→ chat（K14 FATAL 分支）→ 写 .batch json。"""
    chunk_id = f"part_{num:0{meta.chunk_padding}d}"
    text = _read_chunk(root, meta, num, logger)
    n_chars = timelinemod.count_chars(text)
    if n_chars > meta.chunk_max_chars:
        raise ProcessError(0,                              # K29：该片 FATAL，不调 LLM
                           f"FATAL: {chunk_id} 字数 {n_chars} > chunk_max_chars"
                           f" {meta.chunk_max_chars}（K29）")
    prompt = _build_reader_prompt(root, meta, chunk_id, text, logger=logger)
    if logger is not None:
        logger.ok("reader", "start", meta.current_batch + 1,
                  f"chunk={chunk_id} LLM 调用中（等待响应…）")
    res = chat(phase="reader", novel_name=meta.novel_name, chunk_id=chunk_id,
               prompt=prompt, expect="json", stop_check=stop_check)
    if logger is not None:
        logger.log("reader", "llm", meta.current_batch + 1, "OK" if res.ok else "ERROR",
                   f"chunk={chunk_id} attempt={res.attempts} pt={res.prompt_tokens}"
                   f" ct={res.completion_tokens} lat={res.latency_ms}ms")
    if not res.ok:
        if res.error_code in FATAL_CODES:                  # K14：立即 error 分支
            raise ProcessError(0, f"FATAL {res.error_code}: {chunk_id}"
                               f"（{res.error_detail}，K14）")
        raise ProcessError(3, f"{chunk_id} 调用失败: {res.error_code}"
                           f"（{res.error_detail}）")
    data = json.loads(res.text)
    target = root / ".batch" / f"{chunk_id}.json"
    atomic_write_text(target, json.dumps(data, ensure_ascii=False) + "\n")
    if logger is not None:
        logger.ok("reader", "write", meta.current_batch + 1,
                  f"chunk={chunk_id} bytes={target.stat().st_size}")


def _cleanup_batch(root: Path, logger: Logger | None) -> None:
    """§1.5：--processed 成功时整目录清理（幂等；失败仅警告）。"""
    d = root / ".batch"
    if not d.is_dir():
        return
    for f in d.glob("*.json"):
        try:
            f.unlink()
        except OSError as exc:
            if logger is not None:
                logger.warn("reader", "cleanup", None,
                            f".batch/{f.name} 清理失败（幂等，下轮再清）: {exc}")


def _handle_batch_failure(base: Path, meta: MetaDoc, logger: Logger | None,
                          why: str) -> None:
    """§6.2 步骤 7：rollback → retries+1 → ==1 继续（回步骤 3）；>=2 置 error。

    返回 None 表示继续重跑；抛 ProcessError 表示终局失败。
    """
    meta_path = root_of(base, meta.novel_name) / "metadata.json"
    try:
        rb = recovery_mod.rollback(base, meta.novel_name, phase="read",
                                   logger=logger)
    except recovery_mod.RollbackError as exc:
        # K24 零容忍中的零容忍：立即 error，禁止自动重试
        raise ProcessError(
            3, f"K24 回滚失败（禁止自动重试，请人工核对 .rollback\\batch_*"
               f" 与 timeline）: {exc}") from exc
    new_retries = meta.batch_retries + 1
    update_metadata(meta_path, base=base, retries=new_retries)   # 绝对赋值
    if logger is not None:
        logger.warn("reader", "retry", meta.current_batch + 1,
                    f"批失败（batch_retries→{new_retries}）: {why}；"
                    f"rollback 恢复（快照 {rb.get('snapshot')}），重建队列续跑")
    if new_retries >= 2:                                       # K8：到 2 置 error
        raise ProcessError(3, f"批第 {new_retries} 次失败（K8）: {why}")


def process_batch(base: Path, meta: MetaDoc, cfg, *, logger: Logger | None = None,
                  stop_check=None) -> dict:
    """§6.2 步骤 1–8 推进一批；内部处理步骤 7 的重试循环。

    返回 {"kind": "committed"|"fastpath"|"no_batch"|"stopped", "processed": int}；
    终局失败抛 ProcessError。
    """
    root = root_of(base, meta.novel_name)
    meta_path = root / "metadata.json"

    while True:
        meta = read_metadata(meta_path, base=base)             # 重读最新状态
        if meta.status == "error":
            raise ProcessError(3, f"metadata 已置 error: {meta.error}")
        start, end, n = compute_batch_range(meta)
        if start > meta.total_chunks:
            return {"kind": "no_batch", "processed": meta.processed_chunks}
        batch_id = meta.current_batch + 1                      # K1 待派批

        # 步骤 2 幂等预检（§9.2 三态 + G12–G14 + K40）
        try:
            state, errs = timelinemod.check_only(
                root / "plot_timeline.md", start=start, end=end,
                total_chunks=meta.total_chunks,
                chunk_padding=meta.chunk_padding,
                processed_chunks=meta.processed_chunks,
                chunks_dir=root / "chunks")
        except timelinemod.TimelineError as exc:
            raise ProcessError(3, f"预检时间线解析失败: {exc}") from exc
        if errs:
            raise ProcessError(3, "预检失败（G12–G14/K40）: " + "; ".join(errs))
        if state == "PARTIAL":
            raise ProcessError(
                3, f"PARTIAL: 本批 [{start},{end}] 时间线部分命中，"
                   "报告命中/缺失清单，不改文件，置 error")
        if state == "APPLIED":
            # 快路径：补 notes + 轻量 verify（失败仅警告）→ 提交（P2-10）
            notes_path = root / "notes" / \
                f"part_{start:0{meta.chunk_padding}d}~part_{end:0{meta.chunk_padding}d}.md"
            if not notes_path.exists():
                try:
                    rendermod.render_notes_file(root, start=start, end=end,
                                                padding=meta.chunk_padding)
                    if logger is not None:
                        logger.ok("reader", "render", batch_id, "补渲染 notes（P2-10）")
                except Exception as exc:                       # 无法补渲染 → 仅警告
                    if logger is not None:
                        logger.warn("reader", "render", batch_id,
                                    f"补渲染失败（仅警告）: {exc}")
            else:
                n_errs, _n_warns = validatormod.validate_notes_file(
                    notes_path, start=start, end=end, padding=meta.chunk_padding)
                if n_errs and logger is not None:
                    logger.warn("reader", "verify", batch_id,
                                f"verify_notes 失败（仅警告）: {'; '.join(n_errs)}")
            doc = update_metadata(meta_path, base=base,
                                  processed=meta.processed_chunks + n)  # K1 +1
            _cleanup_batch(root, logger)
            return {"kind": "fastpath", "processed": doc.processed_chunks}

        # 步骤 3 片级续跑队列
        queue = build_call_queue(root, meta, start, end)
        if logger is not None:
            logger.skip("reader", "queue", batch_id,
                        f"range=[{start},{end}] pending={len(queue)}"
                        f" reuse={n - len(queue)}")

        # 步骤 4 backup（零容忍：非零 → 立即 error）
        try:
            recovery_mod.backup(base, meta.novel_name, batch_id=batch_id,
                                phase="read", logger=logger)
        except recovery_mod.BackupError as exc:
            raise ProcessError(2, f"backup 失败（零容忍，立即 error）: {exc}") from exc

        # 步骤 5 逐片（串行升序）
        try:
            for num in queue:
                if stop_check is not None and stop_check():
                    return {"kind": "stopped",
                            "processed": meta.processed_chunks}
                _call_reader(root, meta, cfg, num, logger, stop_check)
        except ProcessError as exc:
            if exc.code == 0:                                  # K14 FATAL：不回滚
                raise
            _handle_batch_failure(base, meta, logger, str(exc))
            continue                                           # retries==1 回步骤 3

        # 步骤 6 整批产出（append → render → verify）
        try:
            timelinemod.append_batch(root, batch_id=batch_id,
                                     current_batch=meta.current_batch,
                                     chunk_padding=meta.chunk_padding,
                                     start=start, end=end)
            rendermod.render_notes_file(root, start=start, end=end,
                                        padding=meta.chunk_padding)  # K15：内部降级
            v_errs, v_warns = timelinemod.verify_batch(
                root, batch_id=batch_id, current_batch=meta.current_batch,
                chunk_padding=meta.chunk_padding, start=start, end=end)
            if v_errs:
                raise ProcessError(3, "verify_timeline 失败: " + "; ".join(v_errs))
            notes_path = root / "notes" / \
                f"part_{start:0{meta.chunk_padding}d}~part_{end:0{meta.chunk_padding}d}.md"
            n_errs, n_warns = validatormod.validate_notes_file(
                notes_path, start=start, end=end, padding=meta.chunk_padding)
            for w in (v_warns + n_warns):
                if logger is not None:
                    logger.warn("reader", "verify", batch_id, w)
            if n_errs:
                raise ProcessError(3, "verify_notes 失败: " + "; ".join(n_errs))
        except (timelinemod.TimelineError, rendermod.RenderError,
                ProcessError) as exc:
            if isinstance(exc, ProcessError) and exc.code == 0:
                raise
            _handle_batch_failure(base, meta, logger, str(exc))
            continue                                           # 重跑

        # 步骤 8 提交（K1 +1、batch_retries 原子清零）→ 清理 .batch
        doc = update_metadata(meta_path, base=base,
                              processed=meta.processed_chunks + n)
        _cleanup_batch(root, logger)
        if logger is not None:
            logger.ok("reader", "commit", batch_id,
                      f"processed={doc.processed_chunks} current_batch={doc.current_batch}")
        return {"kind": "committed", "processed": doc.processed_chunks}
