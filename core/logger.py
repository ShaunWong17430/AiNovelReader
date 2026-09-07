"""core/logger.py — run.log / llm.log（§1.7 / §8.5）。

- run.log 行格式：`[<ts>] [<role>] [<action>] [batch=<id|null>] [<level>] <msg>`
  方括号间单空格；恒单行（msg 内换行归一为空格）；本地时区 ISO 含偏移；
  level ∈ OK/WARN/SKIP/ERROR（J 修订）。
- 脱敏铁律：run.log/llm.log 绝不记 api_key；base_url 只记 scheme+host。
- llm.log：NDJSON 一行一次调用（§8.5）；req_fp =
  sha256(model+phase+chunk_id+prompt) 前 12 位；绝不记 api_key/完整
  prompt/响应。phase 映射：reader→read、summarizer→summarize（§8.5 订正）。
- logger 失败仅 stderr，不触错误路径（§8.4）。
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

from .config import sanitize_base_url

LEVELS = ("OK", "WARN", "SKIP", "ERROR")

_PHASE_TO_STAGE = {"reader": "read", "summarizer": "summarize"}


def now_iso() -> str:
    """本地时区 ISO 8601 含 UTC 偏移（不转 UTC，§1.7）。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sanitize_msg(msg: str) -> str:
    """msg 恒单行：任意空白（含换行/回车）折叠为单空格，首尾 trim。"""
    return " ".join(str(msg).split())


def redact(text: str, *secrets: str) -> str:
    """脱敏：将已知密钥（api_key 等）替换为 ***；用于日志前预处理。"""
    out = text
    for secret in secrets:
        if secret:
            out = out.replace(secret, "***")
    return out


class Logger:
    """run.log 追加写入器（§1.7）。失败仅 stderr，不抛异常。

    echo=True 时每行同时镜像到 stdout（flush=True，控制台实时运行状态，
    run.py 驱动器默认开启；cli\\* 保持默认静默）。
    """

    def __init__(self, path: Path | None, *, echo: bool = False):
        self.path = Path(path) if path else None
        self.echo = echo

    def log(self, role: str, action: str, batch: int | None, level: str,
            msg: str, *, ts: str | None = None) -> None:
        if level not in LEVELS:
            level = "WARN"
        line = (f"[{ts or now_iso()}] [{role}] [{action}] "
                f"[batch={batch if batch is not None else 'null'}] "
                f"[{level}] {sanitize_msg(msg)}")
        if self.echo:
            print(line, flush=True)              # 控制台实时镜像（含 chunk 级进度）
        try:
            if self.path is None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(line + "\n")
        except OSError as exc:
            print(f"[logger] 写入失败（仅 stderr）: {exc}", file=sys.stderr)

    def ok(self, role, action, batch, msg, **kw):
        self.log(role, action, batch, "OK", msg, **kw)

    def warn(self, role, action, batch, msg, **kw):
        self.log(role, action, batch, "WARN", msg, **kw)

    def skip(self, role, action, batch, msg, **kw):
        self.log(role, action, batch, "SKIP", msg, **kw)

    def error(self, role, action, batch, msg, **kw):
        self.log(role, action, batch, "ERROR", msg, **kw)


# ---------------------------------------------------------------------------
# llm.log（§8.5 NDJSON，一行一次调用）
# ---------------------------------------------------------------------------

def llm_req_fp(model: str, phase: str, chunk_id: str | None, prompt: str) -> str:
    """req_fp = sha256(model+phase+chunk_id+prompt) 前 12 位（§8.5）。"""
    blob = f"{model}{phase}{chunk_id or ''}{prompt}".encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:12]


def phase_to_stage(phase: str) -> str:
    """reader→read / summarizer→summarize（§8.5 订正；非法 phase 原样返回）。"""
    return _PHASE_TO_STAGE.get(phase, phase)


def log_llm(path: Path, entry: dict) -> None:
    """写一行 NDJSON（llm.log）；失败仅 stderr。entry 须已含 §8.5 字段。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(f"[logger] llm.log 写入失败（仅 stderr）: {exc}", file=sys.stderr)
