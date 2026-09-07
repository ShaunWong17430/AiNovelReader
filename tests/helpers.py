"""tests/helpers.py — 阶段 2 测试工具：临时 config / 分片 / .batch json / CLI 子进程。

全部写入沙箱（base_dir fixture），绝不触碰真实 BASE（conftest autouse 守卫兜底）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from conftest import CODE_ROOT, DEFAULT_NOVEL_NAME

DEFAULT_SUMMARIZER_TOKENS = 10000      # K28 兼容（0.6×10000=6000 ≥ 默认 summary_max 5000）


def write_config(dir_path: Path, *, backend: str = "fake",
                 api_key: str | None = None,
                 api_key_env: str = "NOVEL_LLM_API_KEY",
                 max_output_tokens_summarizer: int = DEFAULT_SUMMARIZER_TOKENS,
                 max_output_tokens_reader: int = 4096,
                 response_format: str = "json_object",
                 **llm_overrides) -> Path:
    """写一份合法 config.json（默认 backend=fake，无需 key）。"""
    llm = {
        "backend": backend,
        "base_url": "https://api.openai.com/v1",
        "api_key_env": api_key_env,
        "api_key": api_key,
        "models": {"reader": "gpt-4o-mini", "summarizer": "gpt-4o-mini"},
        "timeout_s": 120, "max_retries": 3,
        "timeout_fatal_threshold": 3,
        "retry_backoff_s": [2, 6, 18],
        "retry_on_status": [408, 409, 429, 500, 502, 503, 504],
        "temperature": {"reader": 0.3, "summarizer": 0.3},
        "max_output_tokens": {"reader": max_output_tokens_reader,
                              "summarizer": max_output_tokens_summarizer},
        "response_format": response_format,
    }
    llm.update(llm_overrides)
    cfg = {"llm": llm,
           "run": {"sanitize_output": True, "budget_prompt_tokens": None,
                   "log_level": "INFO"}}
    p = dir_path / "config.json"
    p.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return p


def make_chunks(root: Path, n: int, *, padding: int = 3,
                text: str = "章", enc: str = "utf-8", bom: bool = False,
                over_chars: int | None = None) -> list[Path]:
    """写 part_001.txt..part_00N.txt（默认 UTF-8 无 BOM）。

    over_chars：每片正文（去除空白后）字数 ≥ over_chars（用于超限用例）。
    """
    root.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    for i in range(1, n + 1):
        content = (text * (over_chars // len(text) + 1)) if over_chars \
            else f"{text}{i}"
        data = content.encode(enc)
        if bom:
            data = b"\xef\xbb\xbf" + data
        f = root / f"part_{i:0{padding}d}.txt"
        f.write_bytes(data)
        files.append(f)
    return files


def make_reader_json(root: Path, num: int, *, padding: int = 3,
                     events: list | None = None,
                     chunk: str | None = None) -> Path:
    """写 .batch\\part_XXX.json（reader 输出，§4.4）。"""
    chunk_id = chunk or f"part_{num:0{padding}d}"
    if events is None:
        events = [{"event": f"事件{num}", "impact": f"影响{num}"}]
    data = {"chunk": chunk_id, "events": events,
            "notes": {"characters": "主角", "plots": ["情节"],
                      "questions": [], "abstract": "摘要"}}
    d = root / ".batch"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{chunk_id}.json"
    f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return f


def run_cli(args: list[str], *, base: Path, config: Path | None = None,
            extra_env: dict | None = None) -> subprocess.CompletedProcess:
    """子进程运行 cli 命令（NOVEL_BASE / NOVEL_CONFIG 沙箱覆盖，§0.3 调用约定）。"""
    env = dict(os.environ)
    env["NOVEL_BASE"] = str(base)
    env["PYTHONIOENCODING"] = "utf-8"          # 子进程 stdout 恒 UTF-8（防 GBK 乱码）
    env["PYTHONUTF8"] = "1"
    if config is not None:
        env["NOVEL_CONFIG"] = str(config)
    if extra_env:
        env.update(extra_env)
    return subprocess.run([sys.executable, *args], cwd=CODE_ROOT,
                          capture_output=True, text=True, env=env,
                          encoding="utf-8", errors="replace", timeout=120)


def init_novel_cli(base: Path, config: Path, novel: str = DEFAULT_NOVEL_NAME,
                   extra: list[str] | None = None) -> subprocess.CompletedProcess:
    """便捷：跑一次 cli/init_novel.py。"""
    return run_cli(["cli/init_novel.py", "--novel", novel, *(extra or [])],
                   base=base, config=config)


def init_novel_direct(base: Path, config: Path, novel: str = DEFAULT_NOVEL_NAME,
                      n_chunks: int = 5, **kwargs) -> dict:
    """进程内初始化（llm_client 等进程内调用测试用）：写分片 + core.recovery.init_novel。

    返回 recovery.init_novel 的摘要 dict（含 root_dir）。kwargs 透传运行参数。
    """
    from core.config import load_config
    from core.recovery import init_novel
    root = base / novel
    make_chunks(root / "chunks", n_chunks)
    cfg = load_config(config)
    return init_novel(base, novel, cfg, **kwargs)
