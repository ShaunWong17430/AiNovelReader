"""core/summarizer.py — 小结阶段（§6.3，C5 / K7 / K21 / K35 / K36）。

步骤编排（§6.3 1–7）：
1.  幂等预检：.summary_applied 内容 == processed →
    - summary_chunks 已 == processed → 跳步骤 6 verify+删草稿（K21，避免撞严格单调）；
    - 否则 update_metadata --summary <processed>（K21/K36 补推进）→ 转步骤 6；
    内容不匹配 → 陈旧标记（log 警告）继续；
2.  backup --phase summarize（快照 id = current_batch，C5；零容忍）；
3.  切片 tail_timeline [summary_chunks+1, processed]（空范围回退 --lines
    window+5）；行数上限 (window+batch)×5（C7）→ 超限 → 3；
4.  调用前先删陈旧 summary_draft.txt（K35）→ chat(summarizer) → 写草稿；
    - FATAL 类（AUTH/NOT_FOUND/BAD_REQUEST/PAYLOAD_TOO_LARGE）→ 立即 error（同 reader）；
    - 其余（NETWORK 耗尽/EMPTY/TRUNCATED/PARSE 等）→ 步骤 5 失败路径：
      rollback summarize → summary_retries+1 → ==1 回步骤 2 / >=2 置 error；
5.  write_summary 校验（单段/≤max/无 BOM）→ 不满足 → 重生成 1 次（P2-6）→
    仍失败 → 失败路径（同上）→ 成功写 .summary_applied（内容=processed）；
6.  verify_summary → 通过 → update_metadata --summary（summary_retries 原子清零）
    → 删草稿与标记（P2-9）；失败（K7）→ 删标记 → rollback → retries+1 → 同上；
7.  收尾：processed==total 且 summary_chunks==processed → 返回 done（run.py 置
    done + purge）。

触发（§6.3）：processed - summary_chunks > timeline_window，或收尾强制
（processed==total 且 summary_chunks<processed）。
"""
from __future__ import annotations

from pathlib import Path

from . import recovery as recovery_mod
from . import timeline as timelinemod
from . import validator as validatormod
from .llm_client import FATAL_CODES, chat
from .logger import Logger
from .paths import root_of
from .prompts import build_summarizer_prompt
from .reader import ProcessError
from .state import atomic_write_text, read_metadata, update_metadata


def should_summarize(meta) -> bool:
    """§6.3 触发判定：窗口超限或收尾强制。"""
    if meta.processed_chunks == meta.total_chunks:
        return meta.summary_chunks < meta.processed_chunks      # 收尾强制
    return meta.processed_chunks - meta.summary_chunks > meta.timeline_window


def _read_marker(root: Path) -> str | None:
    p = root / ".summary_applied"
    try:
        return p.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None


def _summary_failure(base: Path, meta, logger: Logger | None, why: str) -> None:
    """§6.3 步骤 4/5 失败路径：rollback summarize → retries+1 → ==1 回步骤 2；>=2 error。

    返回 None 表示回循环顶部重来；抛 ProcessError 表示终局。
    """
    meta_path = root_of(base, meta.novel_name) / "metadata.json"
    try:
        rb = recovery_mod.rollback(base, meta.novel_name, phase="summarize",
                                   logger=logger)
    except recovery_mod.RollbackError as exc:
        raise ProcessError(
            3, f"K24 回滚失败（summarize，禁止自动重试，请人工核对"
               f" .rollback\\batch_* 与 summary.md）: {exc}") from exc
    new_retries = meta.summary_retries + 1
    update_metadata(meta_path, base=base, summary_retries=new_retries)  # 绝对赋值
    if logger is not None:
        logger.warn("summarizer", "retry", meta.current_batch,
                    f"小结失败（summary_retries→{new_retries}）: {why}；"
                    f"rollback 恢复（快照 {rb.get('snapshot')}）")
    if new_retries >= 2:
        raise ProcessError(3, f"小结第 {new_retries} 次失败（K8）: {why}")


def _verify_and_finish(base: Path, meta, logger: Logger | None,
                       draft: Path, marker: Path) -> dict:
    """步骤 6 verify_summary + P2-9 清理。失败 → K7 失败路径。"""
    root = root_of(base, meta.novel_name)
    meta_path = root / "metadata.json"
    sm = root / "summary.md"
    if not sm.exists():
        return _summary_failure(base, meta, logger, "summary.md 缺失")
    try:
        raw = sm.read_bytes()
    except OSError as exc:
        return _summary_failure(base, meta, logger, f"summary.md 读取失败: {exc}")
    if raw.startswith(b"\xef\xbb\xbf"):
        return _summary_failure(base, meta, logger, "summary.md 含 BOM（§9.4）")
    text = raw.decode("utf-8")
    errs = validatormod.validate_summary_text(text, meta.summary_max)
    if errs:
        # K7：删 .summary_applied → rollback → retries+1 → 失败路径
        try:
            marker.unlink(missing_ok=True)
        except OSError:
            pass
        return _summary_failure(base, meta, logger,
                                f"verify_summary 失败: {'; '.join(errs)}")
    update_metadata(meta_path, base=base, summary=meta.processed_chunks)
    for p in (draft, marker):                      # P2-9：删草稿与标记
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
    return {"kind": "done" if meta.processed_chunks == meta.total_chunks
            else "ok"}


def process_summary(base: Path, meta, cfg, *, logger: Logger | None = None,
                    stop_check=None) -> dict:
    """§6.3 步骤 1–7 小结全链；返回 {"kind": "ok"|"done"|"noop"}；ProcessError 上抛。"""
    root = root_of(base, meta.novel_name)
    meta_path = root / "metadata.json"
    draft = root / "summary_draft.txt"
    marker = root / ".summary_applied"

    while True:
        meta = read_metadata(meta_path, base=base)
        if not should_summarize(meta):
            return {"kind": "noop"}

        # 步骤 1 幂等预检（K21）
        marker_val = _read_marker(root)
        if marker_val == str(meta.processed_chunks):
            if meta.summary_chunks == meta.processed_chunks:
                if logger is not None:
                    logger.skip("summarizer", "precheck", meta.current_batch,
                                "已落位（K21），跳过 --summary 直接 verify")
                return _verify_and_finish(base, meta, logger, draft, marker)
            update_metadata(meta_path, base=base, summary=meta.processed_chunks)
            return _verify_and_finish(base, meta, logger, draft, marker)
        if marker_val is not None:
            if logger is not None:
                logger.warn("summarizer", "precheck", meta.current_batch,
                            f"陈旧 .summary_applied 标记 {marker_val!r} != "
                            f"processed {meta.processed_chunks}（继续）")

        # 步骤 2 backup summarize（快照 id = current_batch，C5；零容忍）
        try:
            recovery_mod.backup(base, meta.novel_name,
                                batch_id=meta.current_batch, phase="summarize",
                                logger=logger)
        except recovery_mod.BackupError as exc:
            raise ProcessError(2, f"backup(summarize) 失败（零容忍）: {exc}") from exc

        # 步骤 3 切片（K33 空范围回退 --lines window+5；行数上限 C7）
        slice_lines = timelinemod.tail(root / "plot_timeline.md",
                                       meta.summary_chunks + 1,
                                       meta.processed_chunks)
        if not slice_lines:
            slice_lines = timelinemod.tail_lines(root / "plot_timeline.md",
                                                 meta.timeline_window + 5)
        cap = (meta.timeline_window + meta.batch_size) * 5     # C7
        if len(slice_lines) > cap:
            raise ProcessError(3, f"小结切片行数 {len(slice_lines)} > "
                               f"(window+batch)×5={cap}（C7）")

        # 步骤 4：删陈旧草稿（K35）→ chat → 草稿（生成-校验最多 2 次，P2-6）
        summary_text = ""
        sm = root / "summary.md"
        if sm.exists():
            try:
                summary_text = sm.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError):
                summary_text = ""
        if not summary_text:
            summary_text = "（无前情）"
        prompt = build_summarizer_prompt(
            meta.prompt_version, summary_text=summary_text,
            timeline_slice="\n".join(slice_lines),
            chunk_start=meta.summary_chunks + 1,
            chunk_end=meta.processed_chunks, max_chars=meta.summary_max)

        ok_text: str | None = None
        gen_err: str | None = None
        for attempt in range(2):                   # P2-6：不合格重生成 1 次
            if logger is not None:
                logger.ok("summarizer", "start", meta.current_batch,
                          f"range={meta.summary_chunks + 1}-{meta.processed_chunks}"
                          f" 小结 LLM 调用中（等待响应…）")
            try:
                draft.unlink(missing_ok=True)      # K35 每次调用前删陈旧草稿
            except OSError:
                pass
            res = chat(phase="summarizer", novel_name=meta.novel_name,
                       chunk_id=None, prompt=prompt, expect="text",
                       chunk_start=meta.summary_chunks + 1,
                       chunk_end=meta.processed_chunks, stop_check=stop_check)
            if logger is not None:
                logger.log("summarizer", "llm", meta.current_batch,
                           "OK" if res.ok else "ERROR",
                           f"range={meta.summary_chunks + 1}-{meta.processed_chunks}"
                           f" attempt={res.attempts} pt={res.prompt_tokens}"
                           f" ct={res.completion_tokens} lat={res.latency_ms}ms")
            if not res.ok:
                if res.error_code in FATAL_CODES:  # K35 失败分支：FATAL 立即 error
                    raise ProcessError(0, f"FATAL {res.error_code}（summarizer，"
                                       f"K35）: {res.error_detail}")
                gen_err = f"summarizer 调用失败: {res.error_code}"
                break                              # 非 FATAL → 失败路径
            errs = validatormod.validate_summary_text(res.text, meta.summary_max)
            if not errs:
                ok_text = res.text
                break
            gen_err = f"生成小结不合格（{attempt + 1} 次）: {'; '.join(errs)}"
        if ok_text is None:
            _summary_failure(base, meta, logger, gen_err or "小结生成失败")
            continue                               # retries==1 回步骤 2
        atomic_write_text(draft, ok_text)
        # 步骤 5 write_summary：draft → 校验 → 原子写 summary.md + .summary_applied
        atomic_write_text(root / "summary.md", ok_text)      # §11 不变量 3/6
        atomic_write_text(marker, str(meta.processed_chunks))
        # 步骤 6 verify（文件层复验 + K7 分支）
        return _verify_and_finish(base, meta, logger, draft, marker)
