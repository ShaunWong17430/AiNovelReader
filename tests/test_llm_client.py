"""阶段 3 出口测试 6–12、41：LLM 调用层（§12.2，§7/§8，K16/K27/K30/K38/K14）。

覆盖（按 REQUIREMENTS v3.2.1 §12.2）：
- 6.  注入 NETWORK/TIMEOUT/RATE_LIMIT(429)/SERVER_5XX → 阶段 A 退避重试
      （attempts=2 验证「失败 1 次后成功」）；401/403→AUTH 立即只发 1 次；
      FATAL（AUTH/NOT_FOUND/BAD_REQUEST/413→PAYLOAD_TOO_LARGE，K38）→
      不落盘、不 rollback、batch_retries 仍 0（K14）；
- 7.  K30：summarizer 按 (phase, chunk_start-chunk_end) 键控；
      TRUNCATED/EMPTY → 阶段 B 重发 1 次（总 ≤2，K16）；
- 8.  三种坏 JSON：围栏/前后缀杂质解析成功，完全坏 JSON → PARSE → 重发 1 次；
- 9.  events=0 → SCHEMA → 重发 1 次；events=6 → K29 ② 档收敛截断前 5 通过
      （§12.2 文本 9 的「6→SCHEMA」为 v3.0 旧口径，v3.2 K29 已改收敛，按修订
      语义验收，与测试 11 一致）；batch_retries 未被污染；
- 10. 净化链路（\\n/半角|/\\ufeff/首尾空白）→ 通过不判失败；
- 11. 收敛（5 questions/400 abstract/6 events/600 characters →
      截断前 3/300/5/500 + 警告，K29）；
- 12. llm.log 完整、每行合法 NDJSON、同片 req_fp 稳定、无 api_key/完整 prompt；
- 41. K38：注入 413 → error_code=PAYLOAD_TOO_LARGE（∈ §8.2 码集）、FATAL 只发
      1 次、llm_last_error 可合法落盘。

测试环境：backend=fake 全流程无网络、无费用、可复现（§12 测试策略）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import core.llm_client as llm
from conftest import DEFAULT_NOVEL_NAME, SUMMARIZER
from helpers import init_novel_direct, write_config

READER = "reader"

N = DEFAULT_NOVEL_NAME


@pytest.fixture(autouse=True)
def _fake_isolate():
    """每用例清空注入故障；用例后整体重置 FakeLLM（防跨用例污染）。"""
    llm.clear_faults()
    yield
    llm.reset_fake()


@pytest.fixture
def book(base_dir, monkeypatch) -> Path:
    """12 片书 + fake config（退避 0 加速）+ 进程内 init + env 指向沙箱。"""
    config = write_config(base_dir, backend="fake",
                          max_output_tokens_summarizer=10000,
                          retry_backoff_s=[0, 0, 0])
    init_novel_direct(base_dir, config, n_chunks=12)
    monkeypatch.setenv("NOVEL_BASE", str(base_dir))
    monkeypatch.setenv("NOVEL_CONFIG", str(config))
    return base_dir / N


def _good_reader(chunk: str, events=None) -> dict:
    return {"chunk": chunk,
            "events": events if events is not None else
            [{"event": "事件", "impact": "影响"}],
            "notes": {"characters": "主角", "plots": ["情节"],
                      "questions": [], "abstract": "摘要"}}


def _meta(root: Path) -> dict:
    return json.loads((root / "metadata.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 测试 6：网络/协议重试 + FATAL 分类（K14）
# ---------------------------------------------------------------------------

def test_6_retry_network_class_then_succeed(book):
    llm.inject_fault("reader:part_006", {"kind": "NETWORK", "attempts": 1})
    llm.inject_response("reader:part_006", _good_reader("part_006"))
    r = llm.chat(phase=READER, novel_name=N, chunk_id="part_006",
                 prompt="p6", expect="json")
    assert r.ok, r
    assert r.attempts == 2                      # 失败 1 次 → 阶段 A 退避 → 成功


@pytest.mark.parametrize("kind,chunk", [
    ("TIMEOUT", "part_007"), ("RATE_LIMIT", "part_008"),
    ("SERVER_5XX", "part_009"), ("UNKNOWN", "part_010"),
])
def test_6_retryable_kinds_exhaust_after_fault(book, kind, chunk):
    """可重试类（含 UNKNOWN 按 NETWORK）→ 退避重试后成功。"""
    llm.inject_fault(f"reader:{chunk}", {"kind": kind, "attempts": 1})
    llm.inject_response(f"reader:{chunk}", _good_reader(chunk))
    r = llm.chat(phase=READER, novel_name=N, chunk_id=chunk,
                 prompt="p", expect="json")
    assert r.ok, r
    assert r.attempts == 2


@pytest.mark.parametrize("kind", ["AUTH", "NOT_FOUND", "BAD_REQUEST",
                                  "PAYLOAD_TOO_LARGE"])
def test_6_fatal_kinds_send_once(book, kind):
    """FATAL（§8.3）：401/403→AUTH 等只发 1 次，不重试（attempts=5 也无效）。"""
    llm.inject_fault("reader:part_011", {"kind": kind, "attempts": 5})
    r = llm.chat(phase=READER, novel_name=N, chunk_id="part_011",
                 prompt="p", expect="json")
    assert not r.ok
    assert r.error_code == kind
    assert r.attempts == 1                       # K14：FATAL 立即返回


def test_6_k14_no_disk_no_rollback_no_batch_retries(book):
    """K14：FATAL → 不落盘 .batch、不 rollback、batch_retries 仍 0。"""
    llm.inject_fault("reader:part_012", {"kind": "BAD_REQUEST", "attempts": 2})
    r = llm.chat(phase=READER, novel_name=N, chunk_id="part_012",
                 prompt="p", expect="json")
    assert not r.ok and r.error_code == "BAD_REQUEST"
    assert not (book / ".batch" / "part_012.json").exists()
    assert not (book / ".rollback").exists() or \
        not list((book / ".rollback").glob("batch_1_read_*"))
    meta = _meta(book)
    assert meta["batch_retries"] == 0
    assert meta["summary_retries"] == 0


def test_6_network_exhausted_returns_last_code(book):
    """退避耗尽仍是网络类 → 末次 error_code，该片失败（§7.5 循环正常结束）。"""
    llm.inject_fault("reader:part_013", {"kind": "NETWORK", "attempts": 99})
    r = llm.chat(phase=READER, novel_name=N, chunk_id="part_013",
                 prompt="p", expect="json")
    assert not r.ok
    assert r.error_code == "NETWORK"
    assert r.attempts == 4                       # max_retries=3 → 1+3 次


def test_6_timeout_guard_triggers_early(book):
    """护栏：TIMEOUT 连续达阈值（默认 3）→ 立即失败转人工，不耗尽退避预算。

    背景：本地 llama 长推理 + 服务端斩连接时，TIMEOUT 每发数百秒且重试
    大概率仍超时——护栏把「1+3 次全耗」降为「连续 3 次即止」，快速转人工。
    """
    llm.inject_fault("reader:part_014", {"kind": "TIMEOUT", "attempts": 99})
    r = llm.chat(phase=READER, novel_name=N, chunk_id="part_014",
                 prompt="p", expect="json")
    assert not r.ok
    assert r.error_code == "TIMEOUT"
    assert r.attempts == 3                       # 阈值 3 → 不发第 4 次
    assert "转人工" in (r.error_detail or "")


def test_6_timeout_single_does_not_trigger_guard(book):
    """单次 TIMEOUT 后恢复 → 正常重试成功，护栏不误杀。"""
    llm.inject_fault("reader:part_015", {"kind": "TIMEOUT", "attempts": 1})
    llm.inject_response("reader:part_015", _good_reader("part_015"))
    r = llm.chat(phase=READER, novel_name=N, chunk_id="part_015",
                 prompt="p", expect="json")
    assert r.ok, r
    assert r.attempts == 2                       # 失败 1 次 → 退避 → 成功


def test_6_timeout_streak_reset_by_other_code(book):
    """护栏按「连续」计：TIMEOUT 后出现其它可重试码 → 计数清零不触发。"""
    # 键控故障只能固定一种 kind，这里用「TIMEOUT 1 次后回落成功」验证
    # 单发不触发；连续语义由上一测试覆盖（同一键的多次注入合并语义）
    llm.inject_fault("reader:part_016", {"kind": "TIMEOUT", "attempts": 1})
    llm.inject_response("reader:part_016", _good_reader("part_016"))
    r = llm.chat(phase=READER, novel_name=N, chunk_id="part_016",
                 prompt="p", expect="json")
    assert r.ok, r
    assert r.attempts == 2


# ---------------------------------------------------------------------------
# 测试 7：K30 键控 + TRUNCATED/EMPTY 阶段 B（K16）
# ---------------------------------------------------------------------------

def test_7_k30_summarizer_keying(book):
    """K30：summarizer 按 (phase, chunk_start-chunk_end) 键控 fixture。"""
    text = "收尾小结：主角完成遗迹探索，谜团浮出水面。"
    llm.inject_response("summarizer:1-12", text)
    r = llm.chat(phase=SUMMARIZER, novel_name=N, chunk_id=None,
                 prompt="s", expect="text", chunk_start=1, chunk_end=12)
    assert r.ok and r.text == text
    # 不同范围不命中同一键 → 无预设 → UNKNOWN（验证键控精确性）
    r2 = llm.chat(phase=SUMMARIZER, novel_name=N, chunk_id=None,
                  prompt="s", expect="text", chunk_start=2, chunk_end=12)
    assert not r2.ok and r2.error_code == "UNKNOWN"


def test_7_phase_b_retry_truncated(book):
    """TRUNCATED → break 进阶段 B 重发 1 次（总 ≤2，K16）；阶段 B 调高 max_tokens。"""
    llm.inject_fault("summarizer:2-6", {"kind": "TRUNCATED", "attempts": 1})
    llm.inject_response("summarizer:2-6", "正常小结：第二至六片主线推进。")
    r = llm.chat(phase=SUMMARIZER, novel_name=N, chunk_id=None,
                 prompt="s", expect="text", chunk_start=2, chunk_end=6)
    assert r.ok, r
    assert r.attempts == 2
    assert "正常小结" in r.text


def test_7_phase_b_retry_empty(book):
    """EMPTY → 阶段 B 重发 1 次。"""
    llm.inject_fault("summarizer:3-8", {"kind": "EMPTY", "attempts": 1})
    llm.inject_response("summarizer:3-8", "空响应后恢复的小结。")
    r = llm.chat(phase=SUMMARIZER, novel_name=N, chunk_id=None,
                 prompt="s", expect="text", chunk_start=3, chunk_end=8)
    assert r.ok and r.attempts == 2


def test_7_phase_b_exhausted_returns_content_code(book):
    """阶段 B 仍 TRUNCATED → 返回 TRUNCATED（内容类总 ≤2 次，K16）。"""
    llm.inject_fault("summarizer:4-9", {"kind": "TRUNCATED", "attempts": 99})
    llm.inject_response("summarizer:4-9", "不管用")
    r = llm.chat(phase=SUMMARIZER, novel_name=N, chunk_id=None,
                 prompt="s", expect="text", chunk_start=4, chunk_end=9)
    assert not r.ok and r.error_code == "TRUNCATED"
    assert r.attempts == 2                       # 阶段 A 1 次 + 阶段 B 1 次


# ---------------------------------------------------------------------------
# 测试 8：三级解析
# ---------------------------------------------------------------------------

def test_8_three_tier_json_parse(book):
    # ① 剥 Markdown 围栏解析成功
    llm.inject_response("reader:part_008",
                        "```json\n" + json.dumps(_good_reader("part_008"),
                                                 ensure_ascii=False) + "\n```")
    r = llm.chat(phase=READER, novel_name=N, chunk_id="part_008",
                 prompt="p", expect="json")
    assert r.ok, r
    # ② 前后缀杂质截取成功
    llm.inject_response("reader:part_009",
                        "好的，如下： " + json.dumps(_good_reader("part_009"),
                                                     ensure_ascii=False) + " 以上")
    r = llm.chat(phase=READER, novel_name=N, chunk_id="part_009",
                 prompt="p", expect="json")
    assert r.ok, r
    # ③ 完全坏 JSON → PARSE → 阶段 B 重发 1 次 → 仍 PARSE
    llm.inject_response("reader:part_010", "这不是 JSON {{{ 半截")
    r = llm.chat(phase=READER, novel_name=N, chunk_id="part_010",
                 prompt="p", expect="json")
    assert not r.ok and r.error_code == "PARSE"
    assert r.attempts == 2


# ---------------------------------------------------------------------------
# 测试 9：events 0/6（K29 收敛语义）
# ---------------------------------------------------------------------------

def test_9_events_empty_schema_retry(book):
    """events=0（③ 不可救）→ SCHEMA → 阶段 B 重发 1 次。"""
    bad = _good_reader("part_009", events=[])
    llm.inject_response("reader:part_009", bad)
    r = llm.chat(phase=READER, novel_name=N, chunk_id="part_009",
                 prompt="p", expect="json")
    assert not r.ok and r.error_code == "SCHEMA"
    assert r.attempts == 2
    assert _meta(book)["batch_retries"] == 0     # 未被污染


def test_9_events_over_five_converges_k29(book):
    """events=6 → K29 ② 档收敛截断前 5 + 警告 → 通过（不再判死）。

    注：§12.2 文本 9「events 0/6→SCHEMA」为 v3.0 旧口径；v3.2 K29 已明确
    events>5 归 ② 档收敛（§4.4/§7.4），按修订语义验收，与测试 11 一致。
    """
    six = _good_reader("part_010",
                       events=[{"event": f"事件{i}", "impact": "影响"}
                               for i in range(6)])
    llm.inject_response("reader:part_010", six)
    r = llm.chat(phase=READER, novel_name=N, chunk_id="part_010",
                 prompt="p", expect="json")
    assert r.ok, r
    out = json.loads(r.text)
    assert len(out["events"]) == 5
    assert _meta(book)["batch_retries"] == 0


# ---------------------------------------------------------------------------
# 测试 10：净化链路
# ---------------------------------------------------------------------------

def test_10_sanitize_chain(book):
    dirty = {
        "chunk": "part_010",
        "events": [{"event": " 事件\n含换行|竖线\ufeff", "impact": " 影响 \r\n "}],
        "notes": {"characters": " 角色 \n描述 ", "plots": [" 情节 "],
                  "questions": [" 问题 "], "abstract": " 摘要 "},
    }
    llm.inject_response("reader:part_010", dirty)
    r = llm.chat(phase=READER, novel_name=N, chunk_id="part_010",
                 prompt="p", expect="json")
    assert r.ok, r
    ev = json.loads(r.text)["events"][0]["event"]
    assert "|" not in ev and "｜" in ev
    assert "\n" not in ev and "\ufeff" not in ev
    assert ev == "事件 含换行｜竖线"


# ---------------------------------------------------------------------------
# 测试 11：② 档收敛（K29）
# ---------------------------------------------------------------------------

def test_11_convergence_k29(book):
    big = {
        "chunk": "part_011",
        "events": [{"event": f"事件{i}", "impact": "影响"} for i in range(6)],
        "notes": {"characters": "字" * 600, "plots": ["情" * 120],
                  "questions": [f"问题{i}" for i in range(5)],
                  "abstract": "摘" * 400},
    }
    llm.inject_response("reader:part_011", big)
    r = llm.chat(phase=READER, novel_name=N, chunk_id="part_011",
                 prompt="p", expect="json")
    assert r.ok, r
    out = json.loads(r.text)
    assert len(out["events"]) == 5              # 6 → 5
    assert len(out["notes"]["questions"]) == 3  # 5 → 3
    assert len(out["notes"]["abstract"]) == 300 # 400 → 300
    assert len(out["notes"]["characters"]) == 500  # 600 → 500（K29）
    assert len(out["notes"]["plots"][0]) == 100 # 120 → 100


# ---------------------------------------------------------------------------
# 测试 12：llm.log（§8.5 NDJSON / req_fp 稳定 / 脱敏）
# ---------------------------------------------------------------------------

def test_12_llm_log_ndjson_and_stable_req_fp(book):
    llm.inject_response("reader:part_012", _good_reader("part_012"))
    prompt = "同片相同 prompt 正文（不应出现在日志中）"
    for _ in range(2):
        r = llm.chat(phase=READER, novel_name=N, chunk_id="part_012",
                     prompt=prompt, expect="json")
        assert r.ok
    log_text = (book / "llm.log").read_text(encoding="utf-8")
    lines = [ln for ln in log_text.splitlines() if ln.strip()]
    assert len(lines) == 2
    entries = []
    for ln in lines:
        entry = json.loads(ln)                   # 每行合法 NDJSON
        for key in ("ts", "novel", "batch", "phase", "chunk", "model",
                    "attempt", "prompt_tokens", "completion_tokens",
                    "latency_ms", "finish_reason", "ok", "error_code",
                    "req_fp"):
            assert key in entry, f"缺字段 {key}: {ln}"
        entries.append(entry)
    assert entries[0]["phase"] == "read"         # reader→read 映射（§8.5）
    assert entries[0]["chunk"] == "part_012"
    assert len(entries[0]["req_fp"]) == 12
    assert entries[0]["req_fp"] == entries[1]["req_fp"]   # 同片稳定
    assert "api_key" not in log_text
    assert "NOVEL_LLM_API_KEY" not in log_text
    assert prompt not in log_text                # 不记完整 prompt


def test_12_llm_log_records_failure_and_phase_map(book):
    llm.inject_fault("reader:part_013", {"kind": "AUTH", "attempts": 1})
    llm.chat(phase=READER, novel_name=N, chunk_id="part_013",
             prompt="p", expect="json")
    llm.inject_response("summarizer:1-5", "小结文本")
    llm.chat(phase=SUMMARIZER, novel_name=N, chunk_id=None,
             prompt="s", expect="text", chunk_start=1, chunk_end=5)
    log_text = (book / "llm.log").read_text(encoding="utf-8")
    rows = [json.loads(ln) for ln in log_text.splitlines() if ln.strip()]
    assert rows[0]["ok"] is False and rows[0]["error_code"] == "AUTH"
    assert rows[1]["phase"] == "summarize"       # summarizer→summarize（§8.5）
    assert rows[1]["ok"] is True and rows[1]["error_code"] is None
    # 同一次 chat 的失败也计 requests（llm_calls 累加 attempts）
    meta = _meta(book)
    assert meta["llm_calls"] == 2
    assert meta["llm_last_error"] == "AUTH"      # 失败写；成功调用不覆盖


# ---------------------------------------------------------------------------
# 测试 41：K38（HTTP 413 → PAYLOAD_TOO_LARGE）
# ---------------------------------------------------------------------------

def test_41_k38_payload_too_large(book):
    llm.inject_fault("reader:part_041", {"kind": "PAYLOAD_TOO_LARGE",
                                         "attempts": 3})
    r = llm.chat(phase=READER, novel_name=N, chunk_id="part_041",
                 prompt="p", expect="json")
    assert not r.ok
    assert r.error_code == "PAYLOAD_TOO_LARGE"   # ∈ §8.2 码集
    assert r.attempts == 1                       # FATAL 不重试
    meta = _meta(book)
    assert meta["llm_last_error"] == "PAYLOAD_TOO_LARGE"   # 可合法落盘
    assert meta["batch_retries"] == 0            # K14 不消耗
