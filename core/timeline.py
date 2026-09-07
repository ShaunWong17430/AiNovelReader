"""core/timeline.py — plot_timeline.md 文件事务组件（§1.4 / §1.5 / §1.6 / §9.2 / §9.3 / K33 / K40）。

core 包内部组织（§0.3 未逐一列名）：时间线 解析/归一化/原子追加/校验/
窗口截取 与 §1.6 字数口径 在此唯一实现（业务规则仍在 core\\* 内）。

- §1.4：标题 + 空行 + 表头 + 分隔行 + 数据行（4 列）；顺序 1..N 严格连续；
  分片 = part_ + 恰好 chunk_padding 位；写入原子（临时文件 + os.replace）；
  追加前归一化 \\r\\n / 孤立 \\r / BOM / 多余空行。
- §1.6：字数 = 去除空白后的 Unicode 字符数（str.isspace() 覆盖全角）。
- §9.2：check-only 三态（APPLIED/NOT_FOUND/PARTIAL）+ 健全性 G12–G14 + K40。
- §9.3：硬校验总表（append/verify 共用）。
- K33：--chunk-start 钳制 max(1, ·)；钳制后空范围返回空 + 退出码 0。
- K40：时间线最大分片序号 ∈ {processed_chunks, end}，防「未来行」假推进。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .state import atomic_write_text

TITLE = "# 情节时间线"
HEADER = "| 顺序 | 分片 | 事件 | 影响 |"
SEPARATOR = "|------|------|------|------|"

_PART_RE = re.compile(r"^part_(\d+)$")


class TimelineError(Exception):
    """时间线解析/校验失败 → 退出码 3（I3：退出码优先于 stdout）。"""


@dataclass(frozen=True)
class Row:
    seq: int
    chunk: str            # "part_001"
    chunk_num: int
    event: str
    impact: str

    def line(self) -> str:
        return f"| {self.seq} | {self.chunk} | {self.event} | {self.impact} |"


# ---------------------------------------------------------------------------
# §1.6 字数口径
# ---------------------------------------------------------------------------

def count_chars(text: str) -> int:
    """字数 = 去除空白后的 Unicode 字符数（str.isspace()，覆盖全角/不间断空格）。"""
    return sum(1 for ch in text if not ch.isspace())


# ---------------------------------------------------------------------------
# 结构 / 解析 / 归一化
# ---------------------------------------------------------------------------

def skeleton() -> str:
    return f"{TITLE}\n\n{HEADER}\n{SEPARATOR}\n"


def normalize(text: str) -> str:
    """追加前归一化：剔除 BOM、\\r\\n/孤立 \\r → \\n、折叠节间多余空行。

    分隔行之前的多余空行折叠为单个空行（标题后一个空行）；
    分隔行之后的空行一律剔除（数据区不允许空行，§1.4）。
    """
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = []
    after_separator = False
    prev_blank = True
    for ln in text.split("\n"):
        blank = not ln.strip()
        if after_separator:
            if not blank:
                out.append(ln)
            continue
        if blank:
            if prev_blank:
                continue
            out.append("")
            prev_blank = True
        else:
            out.append(ln)
            prev_blank = False
            if ln.strip() == SEPARATOR:
                after_separator = True
    while out and not out[-1].strip():
        out.pop()
    return ("\n".join(out) + "\n") if out else ""


def _parse_row(line: str, lineno: int) -> Row:
    s = line.strip()
    if not (s.startswith("|") and s.endswith("|")):
        raise TimelineError(f"第 {lineno} 行不是表格行: {line!r}")
    cells = [c.strip() for c in s[1:-1].split("|")]
    if len(cells) != 4:
        raise TimelineError(f"第 {lineno} 行须恰 4 列，实际 {len(cells)}: {line!r}")
    seq_s, chunk, event, impact = cells
    try:
        seq = int(seq_s)
    except ValueError:
        raise TimelineError(f"第 {lineno} 行顺序列非整数: {seq_s!r}") from None
    m = _PART_RE.match(chunk)
    if not m:
        raise TimelineError(
            f"第 {lineno} 行分片列非法（禁 part_001-002 式跨片）: {chunk!r}")
    if not event:
        raise TimelineError(f"第 {lineno} 行事件列非空")
    return Row(seq=seq, chunk=chunk, chunk_num=int(m.group(1)),
               event=event, impact=impact)


def parse(text: str) -> list[Row]:
    """严格解析：标题/空行/表头/分隔行原样存在 + 数据行；失败抛 TimelineError。"""
    text = text.lstrip("\ufeff")
    lines = text.split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    if len(lines) < 4:
        raise TimelineError("时间线行数不足（标题/空行/表头/分隔行）")
    if lines[0].strip() != TITLE:
        raise TimelineError(f"首行须为标题 {TITLE!r}，实际 {lines[0].strip()!r}")
    if lines[1].strip() != "":
        raise TimelineError("第 2 行须为空行")
    if lines[2].strip() != HEADER:
        raise TimelineError(f"第 3 行须为表头 {HEADER!r}")
    if lines[3].strip() != SEPARATOR:
        raise TimelineError(f"第 4 行须为分隔行 {SEPARATOR!r}")
    rows: list[Row] = []
    for i, line in enumerate(lines[4:], start=5):
        if not line.strip():
            raise TimelineError(f"第 {i} 行空行（数据区不允许空行）")
        rows.append(_parse_row(line, i))
    return rows


def read_timeline(path: Path) -> list[Row]:
    """读文件并解析；文件缺失 → 空列表；解析失败抛 TimelineError。"""
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise TimelineError(f"时间线读取失败: {path.name}（{exc}）") from exc
    return parse(text)


def has_data_rows(text: str) -> bool:
    """K37：表头之后是否有任何 `|` 数据行。"""
    try:
        return bool(parse(text))
    except TimelineError:
        return False


def append_rows(path: Path, rows: list[Row]) -> int:
    """归一化现有内容 + 原子追加；返回追加后数据行总数（§1.4 / 不变量 6）。"""
    existing = read_timeline(path)
    seq = len(existing)
    new_lines = []
    for row in rows:
        seq += 1
        new_lines.append(f"| {seq} | {row.chunk} | {row.event} | {row.impact} |")
    body = normalize(path.read_text(encoding="utf-8")) if path.exists() else skeleton()
    atomic_write_text(path, body + ("\n".join(new_lines) + "\n"
                                    if new_lines else ""))
    return len(existing) + len(rows)


# ---------------------------------------------------------------------------
# 校验（§9.2 / §9.3）
# ---------------------------------------------------------------------------

def file_check(path: Path, *, total_chunks: int,
               chunk_padding: int) -> list[str]:
    """§9.3 文件级硬校验（结构/连续/分片格式/上界）；返回错误清单（空=通过）。"""
    if not path.exists():
        return [f"时间线缺失: {path.name}"]
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        return ["时间线含 BOM（EF BB BF）"]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return [f"时间线无法 UTF-8 解码: {exc}"]
    if "\r" in text:
        return ["时间线未 LF 归一（含 \\r）"]
    try:
        rows = parse(text)
    except TimelineError as exc:
        return [str(exc)]
    errs: list[str] = []
    for i, row in enumerate(rows, start=1):
        if row.seq != i:
            errs.append(f"顺序列须 1..N 严格连续（G12）: 第 {i} 行为 {row.seq}")
            break
        if not re.fullmatch(rf"part_\d{{{chunk_padding}}}", row.chunk):
            errs.append(
                f"分片零填充须恰 {chunk_padding} 位（G13）: {row.chunk}")
        if row.chunk_num > total_chunks:
            errs.append(
                f"分片序号上界 ≤ total_chunks({total_chunks})（G13）: {row.chunk}")
    return errs


def _sanitize_cell(text: str) -> str:
    """写前行清洗：禁换行与半角 |（用全角 ｜），trim。"""
    return " ".join(text.replace("\r", "\n").splitlines()).strip().replace("|", "｜")


def _load_batch_json(root: Path, num: int, padding: int) -> dict:
    f = root / ".batch" / f"part_{num:0{padding}d}.json"
    if not f.exists():
        raise TimelineError(f"本批 .batch 覆盖不全（缺 {f.name}，K5）")
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TimelineError(f".batch/{f.name} 读取失败: {exc}") from exc
    return data


def _batch_rows(root: Path, *, chunk_padding: int, start: int, end: int,
                batch_id: int) -> tuple[list[Row], list[str], int]:
    """读本批 .batch\\*.json → (待追加行, 警告, 事件总数)。校验 C1/覆盖/格式。"""
    if batch_id < 1:
        raise TimelineError(f"batch_id 非法: {batch_id}")
    rows: list[Row] = []
    warns: list[str] = []
    events_total = 0
    for num in range(start, end + 1):
        data = _load_batch_json(root, num, chunk_padding)
        expect_chunk = f"part_{num:0{chunk_padding}d}"
        if data.get("chunk") != expect_chunk:
            raise TimelineError(
                f".batch json chunk 不匹配: {data.get('chunk')!r} != {expect_chunk!r}")
        events = data.get("events")
        if not isinstance(events, list) or not (1 <= len(events) <= 5):
            raise TimelineError(
                f"{expect_chunk} events 须 ∈ [1,5]（C1），实际 {len(events) if isinstance(events, list) else '非数组'}")
        for ev in events:
            event = _sanitize_cell(str(ev.get("event", "")))
            impact = _sanitize_cell(str(ev.get("impact", "")))
            if not event:
                raise TimelineError(f"{expect_chunk} 存在空 event")
            if count_chars(event) > 100 or count_chars(impact) > 100:
                warns.append(f"{expect_chunk} event/impact 超 100 字（仅警告）")
            rows.append(Row(seq=0, chunk=expect_chunk, chunk_num=num,
                            event=event, impact=impact))
            events_total += 1
    return rows, warns, events_total


def append_batch(root: Path, *, batch_id: int, current_batch: int,
                 chunk_padding: int, start: int, end: int) -> dict:
    """§6.2 步骤 6a：合并本批 .batch 一次性原子追加（J3 禁重试语义）。

    前置：C12（batch_id == current_batch+1）、A4 快照可读、
    J3 时间线处于快照状态、K5 覆盖全、C1 每片 1..5 events。
    返回 {appended, seq_end, warnings}。
    """
    if batch_id != current_batch + 1:
        raise TimelineError(
            f"C12: batch_id({batch_id}) != current_batch+1({current_batch + 1})")
    snapshot = root / ".rollback" / f"batch_{batch_id}_read_timeline.md"
    if not snapshot.exists():
        raise TimelineError(f"A4: 快照缺失 {snapshot.name}（先 backup）")
    try:
        snap_rows = parse(snapshot.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, TimelineError) as exc:
        raise TimelineError(f"A4: 快照不可读 {snapshot.name}: {exc}") from exc

    tl_path = root / "plot_timeline.md"
    tl_rows = read_timeline(tl_path)
    if len(tl_rows) != len(snap_rows):
        raise TimelineError(
            f"J3: 时间线不在快照状态（当前 {len(tl_rows)} 行 != 快照 "
            f"{len(snap_rows)} 行），应先 rollback")

    rows, warns, events_total = _batch_rows(
        root, chunk_padding=chunk_padding, start=start, end=end,
        batch_id=batch_id)
    seq_end = len(tl_rows) + len(rows)
    if not tl_path.exists():
        atomic_write_text(tl_path, skeleton())
    body = normalize(tl_path.read_text(encoding="utf-8"))
    base_seq = len(tl_rows)
    tail_lines = "\n".join(
        f"| {base_seq + i + 1} | {row.chunk} | {row.event} | {row.impact} |"
        for i, row in enumerate(rows))
    atomic_write_text(tl_path, body + tail_lines + "\n")
    return {"appended": events_total, "seq_end": seq_end, "warnings": warns}


def verify_batch(root: Path, *, batch_id: int, current_batch: int,
                 chunk_padding: int, start: int, end: int) -> tuple[list[str], list[str]]:
    """§9.3 硬校验总表（append 后验收）；返回 (错误清单, 警告清单)。"""
    if batch_id != current_batch + 1:
        return ([f"C12: batch_id({batch_id}) != current_batch+1({current_batch + 1})"], [])
    tl_path = root / "plot_timeline.md"
    errs = file_check(tl_path, total_chunks=end, chunk_padding=chunk_padding)
    snapshot = root / ".rollback" / f"batch_{batch_id}_read_timeline.md"
    if not snapshot.exists():
        errs.append(f"A4: 快照缺失 {snapshot.name}")
        return errs, []
    try:
        snap_rows = parse(snapshot.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, TimelineError) as exc:
        errs.append(f"A4: 快照不可读: {exc}")
        return errs, []
    try:
        tl_rows = read_timeline(tl_path)
        rows, warns, events_total = _batch_rows(
            root, chunk_padding=chunk_padding, start=start, end=end,
            batch_id=batch_id)
    except TimelineError as exc:
        errs.append(str(exc))
        return errs, []
    this_batch = tl_rows[len(snap_rows):]
    if len(this_batch) != len(rows):
        errs.append(
            f"A4/覆盖: 本批行数 {len(this_batch)} != 快照+events {len(snap_rows) + events_total}（快照 {len(snap_rows)} + events {events_total}）")
    for row in this_batch:
        if not (start <= row.chunk_num <= end):
            errs.append(f"分片须 ∈ 本批范围 [{start},{end}]: {row.chunk}")
    return errs, warns


def check_applied(path: Path, start: int, end: int) -> str:
    """§9.2 check-only 三态：本批范围「分片」列存在性（退出码均 0）。"""
    if start > end or not path.exists():
        return "NOT_FOUND"
    try:
        present = {row.chunk_num for row in parse(path.read_text(encoding="utf-8"))}
    except TimelineError:
        raise
    want = set(range(start, end + 1))
    hit = want & present
    if not hit:
        return "NOT_FOUND"
    if hit == want:
        return "APPLIED"
    return "PARTIAL"


def check_only(path: Path, *, start: int, end: int, total_chunks: int,
               chunk_padding: int, processed_chunks: int,
               chunks_dir: Path) -> tuple[str, list[str]]:
    """§9.2 check-only：三态 + 健全性 G12–G14 + K40；返回 (状态, 错误)。"""
    errs = file_check(path, total_chunks=total_chunks,
                      chunk_padding=chunk_padding)
    # G13 ③：chunks\\ part_*.txt 数量/最大序号/零填充 == total/chunk_padding
    nums: list[int] = []
    pads: list[int] = []
    if chunks_dir.is_dir():
        for f in chunks_dir.glob("part_*.txt"):
            m = re.fullmatch(r"part_(\d+)\.txt", f.name)
            if m:
                nums.append(int(m.group(1)))
                pads.append(len(m.group(1)))
    if len(nums) != total_chunks or (nums and max(nums) != total_chunks):
        errs.append(f"G13: chunks\\ part_*.txt 数量/最大序号 != total_chunks({total_chunks})")
    if pads and any(p != chunk_padding for p in pads):
        errs.append(f"G13: chunks\\ 零填充 != chunk_padding({chunk_padding})")
    # K40：最大分片序号 ∈ {processed_chunks, end}
    try:
        max_seq = max((row.chunk_num for row in
                       parse(path.read_text(encoding="utf-8"))), default=0)
    except TimelineError:
        max_seq = 0
    if max_seq not in (processed_chunks, end):
        errs.append(
            f"K40: 时间线最大分片序号 {max_seq} ∉ {{processed={processed_chunks}, "
            f"end={end}}}（疑似「未来行」外部篡改）")
    state = check_applied(path, start, end)
    return state, errs


# ---------------------------------------------------------------------------
# 窗口截取（K33）与推导
# ---------------------------------------------------------------------------

def tail(path: Path, chunk_start: int, chunk_end: int) -> list[str]:
    """K33：--chunk-start 钳制 max(1, ·)；钳制后空范围返回空（退出码 0）。"""
    start = max(1, chunk_start)
    if start > chunk_end:
        return []
    rows = read_timeline(path)
    return [row.line() for row in rows if start <= row.chunk_num <= chunk_end]


def tail_capped(path: Path, chunk_start: int, chunk_end: int, *,
                max_rows: int) -> tuple[list[str], int | None]:
    """K20 行数上限裁剪：窗口按分片取全量后，若行数 > max_rows → 从最旧
    整片裁剪（丢最旧片，保最新事件），直到 ≤ max_rows。

    返回 (行列表, 被裁剪掉的最旧分片号 | None)。max_rows<1 → 全部裁剪。

    关键语义：窗口的量尺是「片」，行上限只在「该片超密、总行数爆表」时
    触发整片丢弃——同一事件线若 part_007 独占 5 行，5 行都在、是最新的
    其它分片被丢，而不是把 part_007 的行砍一半、制造残缺事件。
    """
    rows = read_timeline(path)
    keep = [row for row in rows if max(1, chunk_start) <= row.chunk_num <= chunk_end]
    dropped: int | None = None
    # 从最旧整片裁剪；窗口仅剩一片时保底保留（宁全勿空，reader 时间线段非空）
    while len(keep) > max_rows and len({r.chunk_num for r in keep}) > 1:
        first = keep[0].chunk_num
        dropped = first
        keep = [row for row in keep if row.chunk_num != first]
    return [row.line() for row in keep], dropped


def tail_lines(path: Path, n: int) -> list[str]:
    """末尾 n 条数据行（n<1 → 空）。"""
    if n < 1:
        return []
    return [row.line() for row in read_timeline(path)[-n:]]


def max_chunk_seq(path: Path) -> int:
    """时间线最大分片序号；无数据行 → 0（K40 用）。"""
    return max((row.chunk_num for row in read_timeline(path)), default=0)


def derive_processed(path: Path) -> int:
    """E4：按分片列 part_001 起连续前缀推导 processed（禁最大序号）。"""
    nums = {row.chunk_num for row in read_timeline(path)}
    k = 1
    while k in nums:
        k += 1
    return k - 1
