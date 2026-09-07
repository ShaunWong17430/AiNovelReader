"""core/llm_client.py — LLM 调用层（§7 / §8，K10 / K14 / K16 / K27 / K30 / K38）。

阶段 3 交付（IMPLEMENTATION_PLAN D6）：
- §7.1 `LLMResult` + `chat()` 接口（K30 扩展：summarizer 传
  chunk_start/chunk_end 供键控——§7.1 最小接口的规范化扩展）；
- §7.2 调用构造：model/temperature/max_output_tokens 按 phase 取；
  expect=="json" 且 response_format=="json_object" → 传
  response_format={"type":"json_object"}；不传 stream；请求体不含本地绝对路径；
- §7.3 三级解析：json.loads → 剥 Markdown 围栏 → 截首 `{` 到末 `}`，均失败 → PARSE；
- §7.5 线性重试状态机（K3/K16/K27）：
  阶段 A：网络/协议类退避 `max_retries` 次（NETWORK/TIMEOUT/RATE_LIMIT/SERVER_5XX；
  401/403→AUTH、404→NOT_FOUND、400→BAD_REQUEST、413→PAYLOAD_TOO_LARGE（K38）、
  422→BAD_REQUEST（K27）、408/409 按 retry_on_status 归类）；
  成功但 finish_reason=="length"→TRUNCATED / 空→EMPTY / PARSE / SCHEMA → break 进阶段 B；
  阶段 B：内容类固定重发 1 次（与 max_retries 无关；TRUNCATED 调高 max_tokens，
  受模型输出上限钳制）；K16 总调用上限：纯网络类 ≤max_retries+1、内容类 ≤2。
  退避期间可被 stop_check 中断 → 返回 ABORTED（不置 error）；
- §7.6 FakeLLM：backend=="fake" 从 tests/fixtures/*.json 读预设响应
  （reader 键 `reader:<chunk_id>`；summarizer 键 `summarizer:<start>-<end>`，K30），
  故障注入同键控（kind ∈ §8.2 全码集，attempts=先失败次数）；接口与 openai 一致；
- §8.5 llm.log NDJSON 一行一次调用（phase 映射 reader→read / summarizer→summarize；
  req_fp = sha256(model+phase+chunk_id+prompt) 前 12 位，绝不记完整 prompt/响应）；
- metadata `llm_*` 计数累加：每次实际请求（attempt）计 calls/tokens；失败写
  `llm_last_error`（成功调用不覆盖，§1.3）；ABORTED 不写 metadata。

K14 隔离：本层只返回分类结果——不落盘 .batch、不 rollback、不碰
batch_retries；FATAL 置 status=error 是驱动器（§6.2 步骤 5c / §6.3 步骤 4）职责。
"""
from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import CODE_ROOT
from .config import Config, ensure_api_key, load_config
from .logger import llm_req_fp, log_llm, now_iso, phase_to_stage
from .paths import base_dir, root_of
from .sanitize import sanitize_reader_output
from .state import update_metadata
from .validator import validate_reader_output

FIXTURES_DIR = CODE_ROOT / "tests" / "fixtures"

# §8.2 FATAL 码（K14：立即 error，不重试、不落盘、不 rollback、不消耗 retries）
FATAL_CODES = frozenset({"AUTH", "NOT_FOUND", "BAD_REQUEST", "PAYLOAD_TOO_LARGE"})
# §8.2 可重试（阶段 A 退避预算）
RETRYABLE_CODES = frozenset({"NETWORK", "TIMEOUT", "RATE_LIMIT", "SERVER_5XX"})
# §8.2 内容类（阶段 B 固定 1 次）
CONTENT_CODES = frozenset({"EMPTY", "TRUNCATED", "PARSE", "SCHEMA"})

# TRUNCATED 阶段 B 调高 max_tokens 的上限钳制（§7.5「受模型上限钳制」；
# 取 gpt-4o-mini 级 16384 为模型输出上限——阶段 3 实现假设，README 阶段 5 提示
# 按模型容量选择 chunk_max_chars，超出部分由 K29 单片超限 FATAL 兜底）。
_MODEL_OUTPUT_CAP = 16384

# TIMEOUT 连续阈值护栏（config.timeout_fatal_threshold 可配，默认 3）：
# 本地 llama 推理动辄数分钟，TIMEOUT 往往意味着「服务端在斩连接/槽位被占」，
# 重试大概率仍是超时且每次代价数百秒——连续达阈值后立即失败转人工，
# 不再耗尽 (max_retries+1) 次退避预算（防「看起来卡死」的慢循环）。


class LLMError(Exception):
    """llm_client 配置/环境错误（→ 退出码 2）。"""


@dataclass
class LLMResult:
    """§7.1 单次 chat() 结果（含内部重试）。"""
    text: str
    ok: bool
    error_code: str | None
    error_detail: str | None
    attempts: int
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    finish_reason: str | None
    model: str


@dataclass
class _RawResp:
    """传输层统一响应（openai / fake 两后端同构）。"""
    ok: bool
    content: str = ""
    finish_reason: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""
    error_code: str | None = None
    error_detail: str | None = None


# ---------------------------------------------------------------------------
# §8.2 错误分类（K27 / K38）
# ---------------------------------------------------------------------------

def classify_status(code: int, retry_on_status: list) -> str:
    """HTTP 状态 → §8.2 错误码（K27：408/409 按 retry_on_status 归类；K38：413）。"""
    if code in (401, 403):
        return "AUTH"
    if code == 404:
        return "NOT_FOUND"
    if code == 400:
        return "BAD_REQUEST"
    if code == 413:
        return "PAYLOAD_TOO_LARGE"          # K38
    if code == 422:
        return "BAD_REQUEST"                # K27
    if code == 429:
        return "RATE_LIMIT"
    if code in (500, 502, 503, 504):
        return "SERVER_5XX"
    if code == 408:
        return "TIMEOUT" if code in retry_on_status else "UNKNOWN"
    if code == 409:
        return "NETWORK" if code in retry_on_status else "UNKNOWN"   # 409 归可重试类
    if 500 <= code <= 599:
        return "SERVER_5XX"
    if code in retry_on_status:
        return "NETWORK"
    return "BAD_REQUEST"


def classify_exception(exc: Exception, retry_on_status: list) -> str:
    """openai SDK 异常 → §8.2 错误码（无码按 NETWORK，§8.3）。"""
    import openai
    if isinstance(exc, openai.RateLimitError):
        return "RATE_LIMIT"
    if isinstance(exc, openai.APITimeoutError):
        return "TIMEOUT"
    if isinstance(exc, openai.APIConnectionError):
        return "NETWORK"
    if isinstance(exc, openai.APIStatusError):
        return classify_status(exc.status_code, retry_on_status)
    return "UNKNOWN"


# ---------------------------------------------------------------------------
# §7.3 三级解析
# ---------------------------------------------------------------------------

def parse_json_three_tier(text: str) -> object | None:
    """三级解析：json.loads → 剥 Markdown 围栏 → 截首 `{` 到末 `}`；均失败 → None。"""
    stripped = text.strip()
    candidates = [text]
    if stripped.startswith("```"):
        body = re.sub(r"^```[a-zA-Z]*\s*", "", stripped)
        body = re.sub(r"\s*```$", "", body)
        candidates.append(body)
    if "{" in text:
        i, j = text.find("{"), text.rfind("}")
        if i < j:
            candidates.append(text[i:j + 1])
    for cand in candidates:
        try:
            data = json.loads(cand)
            if isinstance(data, dict):
                return data
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# openai 后端（§7.2 调用构造 + 异常分类）
# ---------------------------------------------------------------------------

class _OpenAIBackend:
    """openai 传输层：**reader/summarizer 各自独立 client**（GUI 双套分离）。

    GUI 需求「彻底分开为两套」：reader 用 profile(reader) 的 base_url+api_key，
    summarizer 用 profile(summarizer) 的——即使 url 相同也各自建客户端实例，
    互不共享连接/凭据；旧 config（无 profiles）两套共用顶层字段，行为不变。
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._clients: dict[str, object] = {}

    def _client_instance(self, phase: str):
        if phase not in self._clients:
            import openai
            key = ensure_api_key(self.cfg, phase=phase)   # §3.2 phase 生效键
            url = self.cfg.profile_base_url(phase)        # phase 生效 url
            self._clients[phase] = openai.OpenAI(
                api_key=key, base_url=url, timeout=self.cfg.timeout_s)
        return self._clients[phase]

    def call(self, *, model, phase, chunk_id, chunk_start, chunk_end,
             prompt, expect, max_tokens, attempt) -> _RawResp:
        cfg = self.cfg
        kwargs: dict = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": cfg.temperature.get(phase, 0.3),
            "max_tokens": max_tokens,
        }
        if expect == "json" and cfg.response_format == "json_object":
            kwargs["response_format"] = {"type": "json_object"}
        try:
            resp = self._client_instance(phase).chat.completions.create(**kwargs)
        except Exception as exc:                     # 网络/协议层失败
            code = classify_exception(exc, cfg.retry_on_status)
            return _RawResp(ok=False, error_code=code,
                            error_detail=str(exc)[:300], model=model)
        content = resp.choices[0].message.content or ""
        finish = resp.choices[0].finish_reason
        usage = resp.usage
        return _RawResp(
            ok=True, content=content, finish_reason=finish,
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
            model=model)


# ---------------------------------------------------------------------------
# §7.6 FakeLLM（K10 / K30 键控 + 故障注入）
# ---------------------------------------------------------------------------

def _estimate_tokens(s: str) -> int:
    """tiktoken 缺失降级「字符数 ÷ 2」（§0.4 降级口径），至少 1。"""
    return max(1, len(s) // 2)


class FakeLLM:
    """backend=="fake" 的传输层：fixtures 预设 + 进程内注入，键控与 openai 一致。"""

    def __init__(self, fixtures_dir: Path):
        self.fixtures_dir = fixtures_dir
        self.responses: dict[str, object] = {}
        self.faults: dict[str, dict] = {}
        self._remaining: dict[str, int] = {}
        for path in sorted(fixtures_dir.glob("*.json")):
            self._load(path)

    def _load(self, path: Path) -> None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise LLMError(f"FakeLLM fixture 加载失败 {path.name}: {exc}") from exc
        for key, value in (data.get("responses") or {}).items():
            self.responses[key] = value
        for key, spec in (data.get("faults") or {}).items():
            self._set_fault(key, spec)

    def _set_fault(self, key: str, spec: dict) -> None:
        self.faults[key] = spec
        self._remaining[key] = int(spec.get("attempts", 1))

    # -- 进程内注入（测试用；reset() 恢复文件预设） -------------------------

    def inject_response(self, key: str, value: object) -> None:
        self.responses[key] = value

    def inject_fault(self, key: str, spec: dict) -> None:
        self._set_fault(key, spec)

    def clear_faults(self) -> None:
        self.faults.clear()
        self._remaining.clear()

    # -- 键控（K10 / K30） --------------------------------------------------

    @staticmethod
    def key_of(phase: str, chunk_id: str | None,
               chunk_start: int | None, chunk_end: int | None) -> str:
        if phase == "reader":
            if not chunk_id:
                raise LLMError("reader 调用须传 chunk_id（§7.1）")
            return f"reader:{chunk_id}"
        if chunk_start is None or chunk_end is None:
            raise LLMError(
                "summarizer 调用须传 chunk_start/chunk_end（K30 键控，§7.1 扩展）")
        return f"summarizer:{chunk_start}-{chunk_end}"

    # -- 传输层调用 ----------------------------------------------------------

    def call(self, *, model, phase, chunk_id, chunk_start, chunk_end,
             prompt, expect, max_tokens, attempt) -> _RawResp:
        key = self.key_of(phase, chunk_id, chunk_start, chunk_end)
        spec = self.faults.get(key)
        if spec:
            left = self._remaining.get(key, 0)
            if left > 0:
                self._remaining[key] = left - 1
                return self._fault_resp(spec, model)
            self.faults.pop(key, None)             # attempts 耗尽 → 回落正常响应
        if key not in self.responses:
            return _RawResp(ok=False, error_code="UNKNOWN",
                            error_detail=f"FakeLLM 无预设响应: {key}", model=model)
        value = self.responses[key]
        if isinstance(value, str):
            content = value
        else:
            content = json.dumps(value, ensure_ascii=False)
        return _RawResp(
            ok=True, content=content, finish_reason="stop",
            prompt_tokens=_estimate_tokens(prompt),
            completion_tokens=_estimate_tokens(content), model=model)

    def _fault_resp(self, spec: dict, model: str) -> _RawResp:
        kind = spec["kind"]
        detail = f"注入故障 {kind}"
        if kind in FATAL_CODES or kind in RETRYABLE_CODES or kind == "UNKNOWN":
            return _RawResp(ok=False, error_code=kind, error_detail=detail,
                            model=model)
        if kind == "ABORTED":
            return _RawResp(ok=False, error_code="ABORTED", error_detail=detail,
                            model=model)
        if kind == "EMPTY":
            return _RawResp(ok=True, content="", finish_reason="stop", model=model)
        if kind == "TRUNCATED":
            return _RawResp(ok=True, content=spec.get("payload", "（输出被截断）"),
                            finish_reason="length", model=model)
        if kind == "PARSE":
            return _RawResp(ok=True, content=spec.get("payload", "这不是 JSON"),
                            finish_reason="stop", model=model)
        if kind == "SCHEMA":
            payload = spec.get("payload")
            if payload is not None:
                content = payload if isinstance(payload, str) \
                    else json.dumps(payload, ensure_ascii=False)
            else:
                content = json.dumps(
                    {"chunk": "part_999", "events": [],
                     "notes": {"characters": "", "plots": [], "questions": [],
                               "abstract": ""}}, ensure_ascii=False)
            return _RawResp(ok=True, content=content, finish_reason="stop",
                            model=model)
        return _RawResp(ok=False, error_code="UNKNOWN", error_detail=detail,
                        model=model)


_FAKE: FakeLLM | None = None


def _get_fake() -> FakeLLM:
    global _FAKE
    if _FAKE is None:
        _FAKE = FakeLLM(FIXTURES_DIR)
    return _FAKE


def inject_response(key: str, value: object) -> None:
    """测试注入预设响应（进程内）。"""
    _get_fake().inject_response(key, value)


def inject_fault(key: str, spec: dict) -> None:
    """测试注入故障（同键控，kind ∈ §8.2 全码集）。"""
    _get_fake().inject_fault(key, spec)


def clear_faults() -> None:
    """清除全部注入故障（恢复文件预设）。"""
    _get_fake().clear_faults()


def reset_fake() -> None:
    """整体重置 FakeLLM（重新加载 fixtures 文件），供测试隔离。"""
    global _FAKE
    _FAKE = None


# ---------------------------------------------------------------------------
# metadata 计数 / llm.log
# ---------------------------------------------------------------------------

def _read_current_batch(meta_path: Path | None) -> int:
    """llm.log batch 字段 = current_batch + 1（待派批，§8.5 示例一致）。"""
    if meta_path is None or not meta_path.exists():
        return 1
    try:
        from .state import read_metadata
        return read_metadata(meta_path).current_batch + 1
    except Exception:
        return 1


def _commit_meta(meta_path: Path | None, *, ok: bool, code: str | None,
                 attempts: int, pt: int, ct: int) -> None:
    """metadata llm_* 累加：成功计 requests；失败写 llm_last_error（成功不覆盖）。

    失败仅 stderr 警告，不阻断 LLM 结果（§8.4 update_metadata 异常不置 error）。
    """
    if meta_path is None or not meta_path.exists():
        return
    try:
        kwargs: dict = {"llm": {"calls": attempts, "prompt_tokens": pt,
                                "completion_tokens": ct}}
        if not ok and code not in (None, "ABORTED"):
            kwargs["llm_last_error"] = code
        update_metadata(meta_path, **kwargs)
    except Exception as exc:
        print(f"[llm_client] metadata 计数累加失败（仅警告，不影响结果）: {exc}",
              file=sys.stderr)


# ---------------------------------------------------------------------------
# §7.5 线性重试状态机（K16 计数边界）
# ---------------------------------------------------------------------------

def chat(*, phase: str, novel_name: str, chunk_id: str | None = None,
         prompt: str, expect: str, chunk_start: int | None = None,
         chunk_end: int | None = None, config_path: Path | None = None,
         stop_check=None) -> LLMResult:
    """§7.1 接口。K30：summarizer 调用传 chunk_start/chunk_end 供 FakeLLM 键控。

    config_path：测试经 NOVEL_CONFIG 覆盖，正常不传。
    stop_check：阶段 4 驱动器置 stop_flag 回调（退避期间中断 → ABORTED）。
    """
    if phase not in ("reader", "summarizer"):
        raise LLMError(f"phase 须为 reader/summarizer，实际 {phase!r}")
    if expect not in ("json", "text"):
        raise LLMError(f"expect 须为 json/text，实际 {expect!r}")
    cfg = load_config(config_path) if config_path is not None else load_config()
    root = root_of(base_dir(), novel_name)
    meta_path = root / "metadata.json"
    llm_log_path = root / "llm.log"
    if cfg.backend == "fake":
        backend = _get_fake()
    else:
        backend = _OpenAIBackend(cfg)
    return _run_chain(cfg, backend, phase=phase, novel_name=novel_name,
                      chunk_id=chunk_id, chunk_start=chunk_start,
                      chunk_end=chunk_end, prompt=prompt, expect=expect,
                      meta_path=meta_path, llm_log_path=llm_log_path,
                      stop_check=stop_check)


def _run_chain(cfg: Config, backend, *, phase, novel_name, chunk_id,
               chunk_start, chunk_end, prompt, expect,
               meta_path, llm_log_path, stop_check) -> LLMResult:
    model = cfg.profile_model(phase)                 # GUI 双套：phase 生效 model
    max_tokens = cfg.max_output_tokens.get(phase, 1024)
    started = time.monotonic()
    total_pt = total_ct = 0
    last_code: str | None = None
    last_detail: str | None = None
    last_finish: str | None = None

    def do_call(attempt: int, mt: int) -> tuple[_RawResp, int]:
        t0 = time.monotonic()
        raw = backend.call(model=model, phase=phase, chunk_id=chunk_id,
                           chunk_start=chunk_start, chunk_end=chunk_end,
                           prompt=prompt, expect=expect, max_tokens=mt,
                           attempt=attempt)
        return raw, int((time.monotonic() - t0) * 1000)

    def log_line(attempt, raw, ok, code, lat):
        if llm_log_path is None:
            return
        entry = {
            "ts": now_iso(), "novel": novel_name,
            "batch": _read_current_batch(meta_path),
            "phase": phase_to_stage(phase),
            "chunk": chunk_id, "model": model, "attempt": attempt,
            "prompt_tokens": raw.prompt_tokens,
            "completion_tokens": raw.completion_tokens,
            "latency_ms": lat, "finish_reason": raw.finish_reason,
            "ok": ok, "error_code": code,
            "req_fp": llm_req_fp(model, phase, chunk_id, prompt),
        }
        log_llm(llm_log_path, entry)

    def finish(ok, code, detail, text, attempts, pt, ct, finish_reason) -> LLMResult:
        _commit_meta(meta_path, ok=ok, code=code, attempts=attempts, pt=pt, ct=ct)
        return LLMResult(
            text=text, ok=ok, error_code=code, error_detail=detail,
            attempts=attempts, prompt_tokens=pt, completion_tokens=ct,
            latency_ms=int((time.monotonic() - started) * 1000),
            finish_reason=finish_reason, model=model)

    def handle_success(raw, attempts, pt, ct):
        """成功响应 → 解析/净化/校验；返回 (LLMResult|None, 内容类失败码|None)。"""
        if raw.finish_reason == "length":
            return None, "TRUNCATED"
        if not raw.content.strip():
            return None, "EMPTY"
        if expect == "json":
            parsed = parse_json_three_tier(raw.content)
            if parsed is None:
                return None, "PARSE"
            cleaned, _warns = sanitize_reader_output(parsed, chunk_id=chunk_id)
            errs = validate_reader_output(cleaned, chunk_id)
            if errs:
                return None, "SCHEMA"
            return finish(True, None, None,
                          json.dumps(cleaned, ensure_ascii=False),
                          attempts, pt, ct, raw.finish_reason), None
        return finish(True, None, None, raw.content, attempts, pt, ct,
                      raw.finish_reason), None

    def phase_b(code: str, attempts_a: int, pt: int, ct: int) -> LLMResult:
        """阶段 B：内容类固定重发 1 次（K16，与 max_retries 无关）。"""
        mt = max_tokens
        if code == "TRUNCATED":                      # 调高 max_tokens，受上限钳制
            mt = min(int(max_tokens * 1.5), _MODEL_OUTPUT_CAP)
        attempt = attempts_a + 1
        raw, lat = do_call(attempt, mt)
        pt += raw.prompt_tokens
        ct += raw.completion_tokens
        if not raw.ok:
            code2 = raw.error_code or "UNKNOWN"
            log_line(attempt, raw, False, code2, lat)
            return finish(False, code2, raw.error_detail, "", attempt,
                          pt, ct, raw.finish_reason)
        log_line(attempt, raw, True, None, lat)
        if raw.finish_reason == "length":
            return finish(False, "TRUNCATED", "阶段 B 重发仍被截断", "",
                          attempt, pt, ct, raw.finish_reason)
        if not raw.content.strip():
            return finish(False, "EMPTY", "阶段 B 重发仍为空", "",
                          attempt, pt, ct, raw.finish_reason)
        if expect == "json":
            parsed = parse_json_three_tier(raw.content)
            if parsed is None:
                return finish(False, "PARSE", "阶段 B 重发仍无法解析", "",
                              attempt, pt, ct, raw.finish_reason)
            cleaned, _warns = sanitize_reader_output(parsed, chunk_id=chunk_id)
            errs = validate_reader_output(cleaned, chunk_id)
            if errs:
                return finish(False, "SCHEMA", "; ".join(errs), "",
                              attempt, pt, ct, raw.finish_reason)
            return finish(True, None, None,
                          json.dumps(cleaned, ensure_ascii=False),
                          attempt, pt, ct, raw.finish_reason)
        return finish(True, None, None, raw.content, attempt, pt, ct,
                      raw.finish_reason)

    # ---- 阶段 A：传输/协议重试（退避预算 = max_retries 次） ----
    attempts = 0
    timeout_streak = 0                               # 连续 TIMEOUT 计数（护栏）
    for attempt in range(1, cfg.max_retries + 2):
        attempts = attempt
        raw, lat = do_call(attempt, max_tokens)
        total_pt += raw.prompt_tokens
        total_ct += raw.completion_tokens
        if not raw.ok:
            code = raw.error_code or "UNKNOWN"
            log_line(attempt, raw, False, code, lat)
            if code == "ABORTED":
                return finish(False, "ABORTED", raw.error_detail, "",
                              attempts, total_pt, total_ct, raw.finish_reason)
            if code in FATAL_CODES:                  # K14：FATAL 立即返回
                return finish(False, code, raw.error_detail, "",
                              attempts, total_pt, total_ct, raw.finish_reason)
            last_code, last_detail = code, raw.error_detail
            last_finish = raw.finish_reason
            # 连续 TIMEOUT 护栏：达阈值 → 立即失败转人工，不耗尽退避预算
            timeout_streak = timeout_streak + 1 if code == "TIMEOUT" \
                else 0
            if code == "TIMEOUT" \
                    and timeout_streak >= cfg.timeout_fatal_threshold:
                return finish(False, "TIMEOUT",
                              f"连续 {timeout_streak} 次超时（阈值 "
                              f"{cfg.timeout_fatal_threshold}）——大概率服务端"
                              "斩连接/槽位被占，转人工处理（调大 timeout_s、"
                              "重启 llama-server 或降低并发）",
                              "", attempts, total_pt, total_ct, raw.finish_reason)
            if attempt <= cfg.max_retries:           # 退避预算内继续
                if stop_check is not None and stop_check():
                    return finish(False, "ABORTED",
                                  "退避期间 stop_flag 置位（K16）", "",
                                  attempts, total_pt, total_ct, raw.finish_reason)
                backoff = cfg.retry_backoff_s
                if backoff and 0 <= attempt - 1 < len(backoff):
                    time.sleep(float(backoff[attempt - 1]))
            continue
        # 成功响应
        log_line(attempt, raw, True, None, lat)
        res, fail_code = handle_success(raw, attempts, total_pt, total_ct)
        if res is not None:
            return res
        # 内容类失败（TRUNCATED/EMPTY/PARSE/SCHEMA）→ 进阶段 B
        return phase_b(fail_code, attempts, total_pt, total_ct)

    # 退避耗尽仍是网络/协议类 → 末次 error_code（该片失败）
    return finish(False, last_code or "NETWORK", last_detail, "",
                  attempts, total_pt, total_ct, last_finish)
