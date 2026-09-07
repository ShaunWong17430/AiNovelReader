"""core/state.py — metadata.json 唯一读写口（§1.3 / §6.1 / K1 / K13 / K31 / K34 / K36）。

职责（阶段 2 交付）：
- schema 校验唯一清单（§1.3 字段表）——`validate_doc`；
- 原子写：临时文件 + `os.replace`（§11 不变量 6）；
- emergency（B2/E3/J5/D6）：改名保留现场 + 最小 error metadata
  （进度字段 -1、status=error、error 用调用方 MSG、timestamp 禁冒号、
  emergency 文件须能过自身 schema）；原文件不存在（D2）跳过改名；
- K1：`current_batch` 单调计数器，每次 `--processed` 成功提交 +1，绝不重算；
- K13：`total_chunks=-1`/`processed_chunks=-1` 下 `--status running` 一律拒（3）；
- K31：metadata.json 缺失（FileNotFoundError）返回「缺失」标记，
  不属解析失败、不触发 emergency、绝不写文件；
- K36：`--summary` 仅「等于」no-op；目标 < 当前 → 拒（3，严格单调 §11）；
- J11：`root_dir` 不一致降级退出码 3 + 提示，不销毁、不重置进度；
- 最小读取集（§6.1，含 K34 `chunk_max_chars`）：驱动侧字段读齐。
"""
from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass, field, fields
from datetime import datetime
from pathlib import Path

from .paths import InvalidName, validate_novel_name

# §8.2 LLM 错误分类码全集（llm_last_error 取值域）
LLM_ERROR_CODES = frozenset({
    "NETWORK", "TIMEOUT", "RATE_LIMIT", "SERVER_5XX",
    "AUTH", "NOT_FOUND", "BAD_REQUEST", "PAYLOAD_TOO_LARGE",
    "EMPTY", "TRUNCATED", "PARSE", "SCHEMA", "ABORTED", "UNKNOWN",
})

# K42 / §1.3 示例默认运行参数（自动 init 钉死；emergency 兜底亦用）
DEFAULT_RUN_PARAMS = {
    "batch_size": 5, "timeline_window": 10, "summary_max": 5000,
    "chunk_max_chars": 20000, "chunk_padding": 3, "prompt_version": "v1",
}

_STATUSES = ("init", "running", "done", "error")
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")

# 未设置哨兵：llm_last_error 仅显式赋值才写（成功调用不覆盖）
_UNSET = object()


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------

class MetadataMissing(FileNotFoundError):
    """K31 缺失标记：metadata.json 不存在 ≠ 解析失败；调用方走自动 init。"""


class MetadataCorrupt(Exception):
    """①JSON 解析失败 / ②字段缺失 / ③类型不符 → emergency（改名+最小 error 文档）。"""


class RootDirMismatch(MetadataCorrupt):
    """J11：root_dir 不一致 → 降级退出码 3 + 提示；不销毁、不重置进度。"""


class StateError(Exception):
    """③ 数据校验失败（字段合法但参数与状态冲突）→ 退出码 3，不 emergency。"""


# ---------------------------------------------------------------------------
# 原子写（§11 不变量 6）
# ---------------------------------------------------------------------------

def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """临时文件 + os.replace；同目录，UTF-8 无 BOM。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp{uuid.uuid4().hex[:8]}"
    try:
        tmp.write_text(text, encoding=encoding, newline="\n")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def atomic_write_json(path: Path, data: dict) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False,
                                        indent=2, sort_keys=True) + "\n")


def now_iso() -> str:
    """本地时区 ISO 8601 含 UTC 偏移（不转 UTC，§1.7）。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 文档模型与 schema 校验（§1.3 字段表 = 唯一清单）
# ---------------------------------------------------------------------------

@dataclass
class MetaDoc:
    novel_name: str = ""
    root_dir: str = ""
    total_chunks: int = -1
    processed_chunks: int = -1
    summary_chunks: int = -1
    timeline_window: int = 10
    batch_size: int = 5
    summary_max: int = 5000
    chunk_padding: int = 3
    chunk_max_chars: int = 20000
    prompt_version: str = "v1"
    config_fingerprint: str = ""
    llm_calls: int = 0
    llm_prompt_tokens: int = 0
    llm_completion_tokens: int = 0
    llm_last_error: str | None = None
    status: str = "init"
    current_batch: int = 0
    batch_retries: int = 0
    summary_retries: int = 0
    error: str | None = None
    recovery_required: bool = False
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_dict(cls, data: dict) -> "MetaDoc":
        return cls(**{f.name: data.get(f.name, getattr(cls, f.name, None))
                      for f in fields(cls)})


def validate_doc(doc: dict, *, base: Path | None = None) -> None:
    """§1.3 字段表校验唯一清单；违规抛 MetadataCorrupt / RootDirMismatch。

    - 字段缺失/类型不符/值越界 → MetadataCorrupt（→ emergency）；
    - root_dir != BASE/novel_name → RootDirMismatch（→ 3，不 emergency）。
    """
    errs: list[str] = []
    need = {f.name for f in fields(MetaDoc)}
    missing = need - set(doc)
    if missing:
        raise MetadataCorrupt(f"metadata 字段缺失: {sorted(missing)}")

    def bad(field_name: str, why: str) -> None:
        errs.append(f"{field_name}: {why}")

    novel_name = doc["novel_name"]
    if not isinstance(novel_name, str) or not novel_name.strip():
        bad("novel_name", "非空字符串")
    else:
        try:
            validate_novel_name(novel_name)
        except InvalidName as exc:
            bad("novel_name", str(exc))

    root_dir = doc["root_dir"]
    if not isinstance(root_dir, str) or not root_dir:
        bad("root_dir", "非空字符串")
    elif base is not None:
        expect = os.path.normcase(str(base / novel_name))
        if os.path.normcase(root_dir) != expect:
            raise RootDirMismatch(
                f"root_dir 与 BASE/novel_name 不一致: {root_dir!r} != {expect!r}；"
                "按 J11 降级退出码 3，不销毁、不重置进度")

    total = doc["total_chunks"]
    if not isinstance(total, int) or isinstance(total, bool):
        bad("total_chunks", "须为 int")
    elif total != -1 and total <= 0:
        bad("total_chunks", f"须 > 0 或 -1（emergency），实际 {total}")

    processed = doc["processed_chunks"]
    if not isinstance(processed, int) or isinstance(processed, bool):
        bad("processed_chunks", "须为 int")
    elif processed != -1 and not (isinstance(total, int) and total > 0
                                  and 0 <= processed <= total):
        bad("processed_chunks", f"须 -1 或 0≤p≤total，实际 {processed}/{total}")

    summary = doc["summary_chunks"]
    if not isinstance(summary, int) or isinstance(summary, bool):
        bad("summary_chunks", "须为 int")
    elif summary != -1 and not (isinstance(processed, int) and processed >= 0
                                and 0 <= summary <= processed):
        bad("summary_chunks", f"须 -1 或 0≤s≤processed，实际 {summary}/{processed}")

    if not isinstance(doc["timeline_window"], int) or not (1 <= doc["timeline_window"] <= 50):
        bad("timeline_window", "须 ∈ [1,50]")
    if not isinstance(doc["batch_size"], int) or not (1 <= doc["batch_size"] <= 20):
        bad("batch_size", "须 ∈ [1,20]")
    if not isinstance(doc["summary_max"], int) or not (1000 <= doc["summary_max"] <= 20000):
        bad("summary_max", "须 ∈ [1000,20000]")
    if not isinstance(doc["chunk_padding"], int) or doc["chunk_padding"] < 1:
        bad("chunk_padding", "须 ≥ 1")
    if not isinstance(doc["chunk_max_chars"], int) or not (1000 <= doc["chunk_max_chars"] <= 30000):
        bad("chunk_max_chars", "须 ∈ [1000,30000]（K28 上限）")
    if not isinstance(doc["prompt_version"], str) or not doc["prompt_version"].strip():
        bad("prompt_version", "非空")
    if not isinstance(doc["config_fingerprint"], str) or not doc["config_fingerprint"]:
        bad("config_fingerprint", "非空")
    for key in ("llm_calls", "llm_prompt_tokens", "llm_completion_tokens"):
        if not isinstance(doc[key], int) or doc[key] < 0:
            bad(key, "须 ≥ 0")
    le = doc["llm_last_error"]
    if le is not None and (not isinstance(le, str) or le not in LLM_ERROR_CODES):
        bad("llm_last_error", f"须 null 或 ∈ §8.2 码集，实际 {le!r}")
    if doc["status"] not in _STATUSES:
        bad("status", f"须 ∈ {_STATUSES}，实际 {doc['status']!r}")
    if not isinstance(doc["current_batch"], int) or doc["current_batch"] < 0:
        bad("current_batch", "须 ≥ 0（K1 单调计数器）")
    for key in ("batch_retries", "summary_retries"):
        if not isinstance(doc[key], int) or not (0 <= doc[key] <= 2):
            bad(key, "须 ∈ [0,2]（K8）")
    if doc["error"] is not None and not isinstance(doc["error"], str):
        bad("error", "须 null 或字符串")
    if not isinstance(doc["recovery_required"], bool):
        bad("recovery_required", "须 bool")
    for key in ("created_at", "updated_at"):
        ts = doc[key]
        if not isinstance(ts, str) or not _ISO_RE.match(ts):
            bad(key, "须 ISO 8601 含 UTC 偏移")

    if errs:
        raise MetadataCorrupt("metadata schema 校验失败: " + "; ".join(errs))


# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------

def create_metadata(path: Path, *, novel_name: str, root_dir: str,
                    total_chunks: int, timeline_window: int, batch_size: int,
                    summary_max: int, chunk_padding: int, chunk_max_chars: int,
                    prompt_version: str, config_fingerprint: str,
                    base: Path | None = None,
                    processed_chunks: int = 0, summary_chunks: int = 0,
                    current_batch: int = 0) -> MetaDoc:
    """init 创建初始 metadata（唯一创建入口；status=init、current_batch=0）。

    --recover 场景可传 processed_chunks/summary_chunks/current_batch
    （K1 推导值必须写入 metadata，禁止遗留 current_batch=0 重排覆盖旧快照）。
    """
    ts = now_iso()
    doc = MetaDoc(
        novel_name=novel_name, root_dir=root_dir, total_chunks=total_chunks,
        processed_chunks=processed_chunks, summary_chunks=summary_chunks,
        timeline_window=timeline_window,
        batch_size=batch_size, summary_max=summary_max,
        chunk_padding=chunk_padding, chunk_max_chars=chunk_max_chars,
        prompt_version=prompt_version, config_fingerprint=config_fingerprint,
        status="init", current_batch=current_batch, batch_retries=0,
        summary_retries=0, error=None, recovery_required=False,
        created_at=ts, updated_at=ts,
    )
    validate_doc(doc.to_dict(), base=base)
    atomic_write_json(path, doc.to_dict())
    return doc


def read_metadata(path: Path, *, base: Path | None = None) -> MetaDoc:
    """读取并全量 schema 校验。

    - 文件不存在 → 抛 MetadataMissing（K31「缺失」标记，不 emergency）；
    - 解析/字段/类型失败 → MetadataCorrupt（→ emergency）；
    - root_dir 不一致 → RootDirMismatch（J11 → 3）。
    最小读取集（§6.1，含 K34 chunk_max_chars）为上述字段的子集，
    驱动侧按需取用。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise MetadataMissing(f"metadata.json 不存在: {path}") from None
    except OSError as exc:
        raise MetadataCorrupt(f"metadata.json 读取失败: {exc}") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise MetadataCorrupt(f"metadata.json JSON 解析失败: {exc}") from exc
    if not isinstance(data, dict):
        raise MetadataCorrupt("metadata.json 顶层不是对象")
    validate_doc(data, base=base)
    return MetaDoc.from_dict(data)


def _load_for_update(path: Path, *, base: Path | None) -> MetaDoc:
    try:
        return read_metadata(path, base=base)
    except MetadataMissing:
        raise StateError(f"metadata.json 缺失，先 init（{path}）") from None


# 状态机白名单（B6）：init→running、running→running、running→done、
# running→error、error→error（幂等）、error→running（仅人工）、init→error（I1）。
# done = 唯一终态，拒绝任何转换（含 --error）。
_ALLOWED_STATUS = {
    ("init", "running"), ("running", "running"), ("running", "done"),
    ("running", "error"), ("error", "error"), ("error", "running"),
    ("init", "error"),
}


def update_metadata(path: Path, *, base: Path | None = None,
                    processed: int | None = None,
                    summary: int | None = None,
                    status: str | None = None,
                    error: str | None = None,
                    retries: int | None = None,
                    summary_retries: int | None = None,
                    llm: dict | None = None,
                    llm_last_error: object = _UNSET) -> MetaDoc:
    """唯一写入口（init 创建除外）：校验 → 原子写。

    - processed：严格递增 + 单批增量（≤ batch_size）+ ≤ total；提交时
      current_batch +1（K1）、batch_retries 原子清零；
    - summary：== 当前 → no-op（K36，退出码 0 值不变）；< 当前 → StateError；
      > 当前须 ≤ processed，summary_retries 原子清零；
    - status：白名单（B6）；done 终态拒一切；error→running 清 error/
      recovery_required/双重试（D15/G1）；K13 占位态拒 running；
    - retries/summary_retries：绝对赋值（0..2，K8）；
    - llm：计数累加（llm_client 用）；llm_last_error 仅显式赋值才写。
    """
    doc = _load_for_update(path, base=base)
    if doc.status == "done" and any(x is not None for x in (
            processed, summary, status, error, retries, summary_retries, llm)):
        raise StateError("done 为唯一终态，拒绝任何转换（含 --error，J6）")

    if processed is not None:
        if doc.total_chunks == -1:
            raise StateError("total_chunks=-1（emergency 占位态），禁止推进（K13）")
        if not isinstance(processed, int):
            raise StateError("--processed 须为整数")
        if processed > doc.total_chunks:
            raise StateError(f"--processed {processed} > total {doc.total_chunks}")
        if processed <= doc.processed_chunks:
            raise StateError(
                f"processed 严格递增（§11）：{processed} ≤ 当前 {doc.processed_chunks}")
        if processed - doc.processed_chunks > doc.batch_size:
            raise StateError(
                f"单批增量越界：{processed - doc.processed_chunks} > batch_size"
                f" {doc.batch_size}")
        doc.processed_chunks = processed
        doc.current_batch += 1                        # K1 单调计数器
        doc.batch_retries = 0                         # 成功验收原子清零

    if summary is not None:
        if not isinstance(summary, int):
            raise StateError("--summary 须为整数")
        if summary == doc.summary_chunks:
            return doc                              # K36：仅「等于」no-op（值不变）
        if summary < doc.summary_chunks:
            raise StateError(
                f"--summary 严格单调（§11/K36）：{summary} < 当前 {doc.summary_chunks}")
        if doc.processed_chunks == -1 or summary > doc.processed_chunks:
            raise StateError(
                f"--summary {summary} > processed {doc.processed_chunks}")
        doc.summary_chunks = summary
        doc.summary_retries = 0

    if status is not None:
        if status not in _STATUSES:
            raise StateError(f"未知 status: {status!r}")
        if (doc.status, status) not in _ALLOWED_STATUS:
            raise StateError(
                f"非法状态转换: {doc.status} → {status}（白名单 B6）")
        if status == "running" and (doc.total_chunks == -1
                                    or doc.processed_chunks == -1):
            raise StateError(
                "K13：占位态（total/processed=-1，emergency）下禁 --status "
                "running；请按 §10.1 A 恢复")
        if status == "running" and doc.status == "error":
            doc.error = None                          # D15/G1 人工恢复
            doc.recovery_required = False
            doc.batch_retries = 0
            doc.summary_retries = 0
        doc.status = status

    if error is not None:
        if not isinstance(error, str):
            raise StateError("error 须为字符串")
        doc.error = error

    if retries is not None:
        if not isinstance(retries, int) or not (0 <= retries <= 2):
            raise StateError(f"batch_retries 须 ∈ [0,2]（K8），实际 {retries!r}")
        doc.batch_retries = retries

    if summary_retries is not None:
        if not isinstance(summary_retries, int) or not (0 <= summary_retries <= 2):
            raise StateError(f"summary_retries 须 ∈ [0,2]（K8），实际 {summary_retries!r}")
        doc.summary_retries = summary_retries

    if llm:
        doc.llm_calls += int(llm.get("calls", 0))
        doc.llm_prompt_tokens += int(llm.get("prompt_tokens", 0))
        doc.llm_completion_tokens += int(llm.get("completion_tokens", 0))

    if llm_last_error is not _UNSET:
        if llm_last_error is not None and llm_last_error not in LLM_ERROR_CODES:
            raise StateError(f"llm_last_error 须 ∈ §8.2 码集: {llm_last_error!r}")
        doc.llm_last_error = llm_last_error

    doc.updated_at = now_iso()                        # 每次写操作刷新
    validate_doc(doc.to_dict(), base=base)
    atomic_write_json(path, doc.to_dict())
    return doc


# ---------------------------------------------------------------------------
# emergency（B2/E3/J5/D6）与 K31 缺失标记
# ---------------------------------------------------------------------------

@dataclass
class EmergencyResult:
    doc: MetaDoc
    renamed: bool                     # 原文件是否改名保留现场
    corrupt_path: Path | None = None  # metadata.corrupt_<ts> 路径


def set_error(path: Path, reason: str, *, base: Path | None = None,
              novel_name: str | None = None, root_dir: str | None = None,
              defaults: dict | None = None) -> EmergencyResult:
    """emergency：改名保留现场 + 最小 error metadata（进度字段 -1）。

    - 原文件存在 → 改名 metadata.corrupt_<ts>（D6：timestamp 禁冒号）；
    - 原文件不存在（D2）→ 跳过改名、不输出「已改名」；
    - 最小 error metadata 用 novel_name/root_dir（缺省由路径推导）+
      DEFAULT_RUN_PARAMS 兜底，必须能过自身 schema（防死循环）；
    - K13：置 -1 后 --status running 自然被拒（提示 §10.1 A）。
    """
    renamed = False
    corrupt_path: Path | None = None
    if path.exists():
        ts = now_iso().replace(":", "-")              # D6 禁冒号
        corrupt_path = path.with_name(f"metadata.corrupt_{ts}")
        try:
            os.replace(path, corrupt_path)
            renamed = True
        except OSError as exc:
            raise StateError(f"emergency 改名失败，无法保留现场: {exc}") from exc

    d = dict(DEFAULT_RUN_PARAMS)
    if defaults:
        d.update(defaults)
    name = novel_name or path.parent.name
    try:
        validate_novel_name(name)
    except InvalidName:
        name = "unknown_novel"
    ts = now_iso()
    doc = MetaDoc(
        novel_name=name,
        root_dir=root_dir or str(path.parent),
        total_chunks=-1, processed_chunks=-1, summary_chunks=-1,
        timeline_window=int(d["timeline_window"]), batch_size=int(d["batch_size"]),
        summary_max=int(d["summary_max"]), chunk_padding=int(d["chunk_padding"]),
        chunk_max_chars=int(d["chunk_max_chars"]),
        prompt_version=str(d["prompt_version"]),
        config_fingerprint=str(d.get("config_fingerprint", "sha256:emergency")),
        status="error",
        error=reason or "metadata 状态损坏，需人工恢复（§10.1 A）",
        recovery_required=True,
        created_at=ts, updated_at=ts,
    )
    validate_doc(doc.to_dict(), base=base)
    atomic_write_json(path, doc.to_dict())
    return EmergencyResult(doc=doc, renamed=renamed, corrupt_path=corrupt_path)
