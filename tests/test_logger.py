"""出口测试 4（log 部分）+ §1.7 / §8.5：core/logger.py 行格式与脱敏铁律。

覆盖：
- §1.7 行格式 `[ts] [role] [action] [batch=id|null] [LEVEL] msg`、单行、
  msg 换行归一、本地 ISO 含偏移；
- 脱敏铁律：run.log/llm.log 绝不出现 api_key；base_url 只记 scheme+host；
- §8.5：llm.log NDJSON 一行一次调用、req_fp 稳定、phase→stage 映射。
"""
from __future__ import annotations

import json
import re

import core.config as cfgmod
import core.logger as logmod
from core.state import atomic_write_text

LINE_RE = re.compile(
    r"^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}\] "
    r"\[(\w+)\] \[(\w+)\] \[batch=(null|\d+)\] \[(OK|WARN|SKIP|ERROR)\] (.+)$")


def test_run_log_line_format(base_dir):
    log = logmod.Logger(base_dir / "run.log")
    log.ok("driver", "init", None, "novel=示例书名 total=12")
    log.warn("reader", "llm", 1, "chunk=part_001 attempt=2")
    lines = (base_dir / "run.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    m1 = LINE_RE.match(lines[0])
    assert m1 and m1.group(1) == "driver" and m1.group(2) == "init"
    assert m1.group(3) == "null" and m1.group(4) == "OK"
    m2 = LINE_RE.match(lines[1])
    assert m2 and m2.group(3) == "1" and m2.group(4) == "WARN"


def test_msg_newlines_normalized_to_single_line(base_dir):
    log = logmod.Logger(base_dir / "run.log")
    log.log("driver", "act", None, "WARN", "第一行\n第二行\r\n第三行")
    lines = (base_dir / "run.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert "第一行 第二行 第三行" in lines[0]


def test_redact_secret_not_in_log(base_dir):
    secret = "sk-super-secret-42"
    log = logmod.Logger(base_dir / "run.log")
    log.ok("driver", "init", None, logmod.redact(f"key={secret}", secret))
    content = (base_dir / "run.log").read_text(encoding="utf-8")
    assert secret not in content
    assert "***" in content


def test_logger_echo_mirrors_to_stdout(base_dir, capsys):
    """echo=True：每条日志同时镜像 stdout（flush），run.py 控制台实时状态。"""
    log = logmod.Logger(base_dir / "run.log", echo=True)
    log.ok("reader", "llm", 1, "chunk=part_001 attempt=1 lat=90000ms")
    out = capsys.readouterr().out
    assert "chunk=part_001 attempt=1 lat=90000ms" in out
    assert "[reader] [llm] [batch=1] [OK]" in out
    # 文件照常写入
    lines = (base_dir / "run.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1


def test_logger_echo_default_off(base_dir, capsys):
    """默认 echo=False：cli\\* 静默，不污染 stdout。"""
    log = logmod.Logger(base_dir / "run.log")
    log.ok("driver", "init", None, "novel=示例 进度=1/10")
    assert capsys.readouterr().out == ""


def test_sanitize_base_url_only_scheme_host():
    assert cfgmod.sanitize_base_url("https://api.openai.com/v1") == "https://api.openai.com"
    assert cfgmod.sanitize_base_url("http://localhost:8000/anything") == "http://localhost:8000"


def test_llm_log_ndjson_and_req_fp(base_dir):
    path = base_dir / "llm.log"
    entry = {
        "ts": logmod.now_iso(), "novel": "示例书名", "batch": 1,
        "phase": logmod.phase_to_stage("reader"), "chunk": "part_001",
        "model": "gpt-4o-mini", "attempt": 1, "prompt_tokens": 1200,
        "completion_tokens": 320, "latency_ms": 4321,
        "finish_reason": "stop", "ok": True, "error_code": None,
        "req_fp": logmod.llm_req_fp("gpt-4o-mini", "reader", "part_001", "prompt"),
    }
    logmod.log_llm(path, entry)
    logmod.log_llm(path, entry)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    for ln in lines:
        obj = json.loads(ln)                       # 每行合法 NDJSON
        assert obj["phase"] == "read"              # §8.5 映射 reader→read
        assert obj["req_fp"] == logmod.llm_req_fp(
            "gpt-4o-mini", "reader", "part_001", "prompt")   # 同片 req_fp 稳定
    assert logmod.phase_to_stage("summarizer") == "summarize"
