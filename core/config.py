"""core/config.py — config.json 加载/校验/快照（§3，K27 / K28 / K11）。

- `load_config()`：§3.3 校验表，违规抛 `ConfigError` → 退出码 2（不写文件）。
- `ensure_api_key(cfg)`：§3.2 三级解析（① 环境变量 api_key_env（默认
  NOVEL_LLM_API_KEY）② config.json `llm.api_key`（"${ENV}" 展开）③ 都无 →
  ConfigError 2，绝不带空 key）；backend==fake 不需 key。
- `validate_init_params(cfg, ...)`：K2 参数前置（§6.4 步骤 2）+ K28 init 硬校验
  （summary_max ≤ max_output_tokens.summarizer×0.6、chunk_max_chars ≤ 30000），
  违规 → ConfigError 2，在任何落盘之前。
- `build_snapshot` / `compute_fingerprint`：§3.4 脱敏快照与指纹（K11）——
  剔除 api_key、base_url 只留 scheme+host；快照同时记运行参数；
  指纹对脱敏 LLM 配置取 sha256（每轮入口比对，不一致仅 log 警告）。

测试支持（非需求偏离）：环境变量 `NOVEL_CONFIG` 覆盖 config.json 路径
（默认 CODE_ROOT/config.json），仅测试/沙箱使用。
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

from . import CODE_ROOT

DEFAULT_CONFIG_PATH = CODE_ROOT / "config.json"
DEFAULT_API_KEY_ENV = "NOVEL_LLM_API_KEY"

VALID_BACKENDS = ("openai", "fake")
VALID_SCHEMES = ("http", "https")
VALID_RESPONSE_FORMATS = ("json_object", "text")


class ConfigError(Exception):
    """配置非法/缺 api_key → 退出码 2；不写任何文件、不发请求。"""


def default_config_path() -> Path:
    """默认 CODE_ROOT/config.json；仅测试经 NOVEL_CONFIG 覆盖。"""
    env = os.environ.get("NOVEL_CONFIG")
    return Path(env) if env else DEFAULT_CONFIG_PATH


@dataclass
class Config:
    """config.json 解析结果（§3.1）。api_key 保持原文，解析见 resolve_api_key。

    GUI 双套分离（reader/summarizer 各自 base_url + api_key）：
    - `profiles`：`{"reader": {...}, "summarizer": {...}}`，每项可含
      base_url / api_key_env / api_key / model；缺省回退顶层字段；
    - 旧结构（无 profiles）完全兼容：两 phase 共用顶层 base_url/api_key。
    """
    backend: str
    base_url: str
    api_key_env: str
    api_key: str | None
    models: dict = field(default_factory=dict)
    profiles: dict = field(default_factory=dict)
    timeout_s: int = 120
    max_retries: int = 3
    timeout_fatal_threshold: int = 3
    retry_backoff_s: list = field(default_factory=list)
    retry_on_status: list = field(default_factory=list)
    temperature: dict = field(default_factory=dict)
    max_output_tokens: dict = field(default_factory=dict)
    response_format: str = "json_object"
    sanitize_output: bool = True
    budget_prompt_tokens: int | None = None
    log_level: str = "INFO"

    @classmethod
    def from_dict(cls, data: dict) -> "Config":
        llm = data.get("llm") or {}
        run = data.get("run") or {}
        return cls(
            backend=llm.get("backend", "openai"),
            base_url=llm.get("base_url", ""),
            api_key_env=llm.get("api_key_env", DEFAULT_API_KEY_ENV),
            api_key=llm.get("api_key"),
            models=llm.get("models") or {},
            # GUI 双套：reader/summarizer 各自 base_url + api_key（缺省回退顶层）
            profiles=llm.get("profiles") or {},
            timeout_s=llm.get("timeout_s", 120),
            max_retries=llm.get("max_retries", 3),
            timeout_fatal_threshold=llm.get("timeout_fatal_threshold", 3),
            retry_backoff_s=llm.get("retry_backoff_s") or [],
            retry_on_status=llm.get("retry_on_status") or [],
            temperature=llm.get("temperature") or {},
            max_output_tokens=llm.get("max_output_tokens") or {},
            response_format=llm.get("response_format", "json_object"),
            sanitize_output=bool(run.get("sanitize_output", True)),
            budget_prompt_tokens=run.get("budget_prompt_tokens"),
            log_level=run.get("log_level", "INFO"),
        )

    # -- GUI 双套生效值（§3.1 扩展：profiles 覆盖顶层，缺省回退） -------------

    def profile(self, phase: str) -> dict:
        """phase（reader/summarizer）的 profile dict（不存在 → 空 dict）。"""
        prof = self.profiles.get(phase)
        return prof if isinstance(prof, dict) else {}

    def profile_base_url(self, phase: str) -> str:
        """生效 base_url：profile 优先，否则顶层（旧结构共用）。"""
        return (self.profile(phase).get("base_url")
                or self.base_url or "")

    def profile_api_key_env(self, phase: str) -> str:
        """生效 api_key_env：profile 优先，否则顶层。"""
        return (self.profile(phase).get("api_key_env")
                or self.api_key_env or DEFAULT_API_KEY_ENV)

    def profile_model(self, phase: str) -> str:
        """生效 model：profile 优先，否则顶层 models（旧结构共用）。"""
        return (self.profile(phase).get("model")
                or (self.models.get(phase) or "") or "")


def _fail(rule: str, detail: str) -> "ConfigError":
    return ConfigError(f"config 非法（{rule}）: {detail}")


def validate_config(cfg: Config) -> None:
    """§3.3 配置校验表（启动一次性）；违规抛 ConfigError（→ 退出码 2）。"""
    if cfg.backend not in VALID_BACKENDS:
        raise _fail("backend", f"{cfg.backend!r} ∉ {VALID_BACKENDS}")
    for key in ("reader", "summarizer"):
        url = cfg.profile_base_url(key)
        if not url or any(ch.isspace() for ch in url):
            raise _fail("base_url", f"{key} base_url 非空且无空白")
        scheme = urllib.parse.urlsplit(url).scheme.lower()
        if scheme not in VALID_SCHEMES:
            raise _fail("base_url", f"{key} scheme {scheme!r} ∉ {VALID_SCHEMES}")
        if not cfg.profile_model(key).strip():
            raise _fail("models", f"models.{key} 非空")
    if not isinstance(cfg.timeout_s, int) or not (1 <= cfg.timeout_s <= 600):
        raise _fail("timeout_s", f"{cfg.timeout_s!r} ∉ [1,600]")
    if not isinstance(cfg.max_retries, int) or not (0 <= cfg.max_retries <= 5):
        raise _fail("max_retries", f"{cfg.max_retries!r} ∉ [0,5]")
    if not isinstance(cfg.timeout_fatal_threshold, int) \
            or not (1 <= cfg.timeout_fatal_threshold <= 5):
        raise _fail("timeout_fatal_threshold",
                    f"{cfg.timeout_fatal_threshold!r} ∉ [1,5]")
    backoff = cfg.retry_backoff_s
    if (not isinstance(backoff, list) or len(backoff) != cfg.max_retries
            or any(not isinstance(x, (int, float)) or x < 0 for x in backoff)):
        raise _fail("retry_backoff_s",
                    f"长度须 == max_retries({cfg.max_retries}) 且元素 ≥ 0")
    for key in ("reader", "summarizer"):
        mot = cfg.max_output_tokens.get(key)
        if not isinstance(mot, int) or mot <= 0:
            raise _fail("max_output_tokens", f"max_output_tokens.{key} > 0")
    if cfg.response_format not in VALID_RESPONSE_FORMATS:
        raise _fail("response_format",
                    f"{cfg.response_format!r} ∉ {VALID_RESPONSE_FORMATS}")


def load_config(path: Path | None = None) -> Config:
    """加载并校验 config.json（§3.3）；失败抛 ConfigError（→ 2）。"""
    path = Path(path) if path else default_config_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"config.json 读取失败: {path}（{exc}）") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ConfigError(f"config.json JSON 解析失败: {exc}") from exc
    if not isinstance(data, dict):
        raise _fail("结构", "顶层须为对象")
    cfg = Config.from_dict(data)
    validate_config(cfg)
    return cfg


# ---------------------------------------------------------------------------
# §3.2 api_key 三级解析
# ---------------------------------------------------------------------------

def resolve_api_key(cfg: Config, phase: str | None = None) -> str | None:
    """三级解析：env(api_key_env) → config.api_key（${ENV} 展开）→ None。

    phase 非空（reader/summarizer）时优先读该 phase 的 profile（GUI 双套
    分离）：env(profile.api_key_env) → profile.api_key → 顶层 api_key。
    """
    prof = cfg.profile(phase) if phase in ("reader", "summarizer") else {}
    env_name = (prof.get("api_key_env")
                or cfg.api_key_env or DEFAULT_API_KEY_ENV)
    raw = prof.get("api_key") or cfg.api_key
    val = os.environ.get(env_name)
    if val:
        return val
    if raw:
        if isinstance(raw, str) and raw.startswith("${") and raw.endswith("}"):
            return os.environ.get(raw[2:-1])        # ${ENV} 展开；缺失 → None
        return raw
    return None


def ensure_api_key(cfg: Config, phase: str | None = None) -> str:
    """backend==openai 时必须有 key；fake 不需 key。缺失 → ConfigError 2。

    phase 指定 → 只校验该 phase（llm_client 建 client 时用）；
    phase 省略 → 逐个 phase（reader/summarizer）校验，任一缺失即 2。
    """
    if cfg.backend != "openai":
        return ""
    if phase is not None and phase in ("reader", "summarizer"):
        return _ensure_one(cfg, phase)
    for ph in ("reader", "summarizer"):
        _ensure_one(cfg, ph)
    return _ensure_one(cfg, "reader")


def _ensure_one(cfg: Config, phase: str) -> str:
    key = resolve_api_key(cfg, phase=phase)
    if not key:
        raise ConfigError(
            f"缺少 api_key（phase={phase}，env:{cfg.profile_api_key_env(phase)} "
            "或 config.json llm.api_key），绝不带空 key 发请求")
    return key


# ---------------------------------------------------------------------------
# K2 / K28 init 硬校验（§6.4 步骤 2，在任何落盘之前）
# ---------------------------------------------------------------------------

def validate_init_params(cfg: Config, *, batch_size: int,
                         timeline_window: int, summary_max: int,
                         chunk_max_chars: int) -> None:
    """K2 参数前置 + K28 init 硬校验；违规抛 ConfigError（→ 退出码 2）。"""
    if not (1 <= timeline_window <= 50):
        raise _fail("K2 timeline_window", f"{timeline_window} ∉ [1,50]")
    if not (1 <= batch_size <= 20):
        raise _fail("K2 batch_size", f"{batch_size} ∉ [1,20]")
    if not (1000 <= summary_max <= 20000):
        raise _fail("K2 summary_max", f"{summary_max} ∉ [1000,20000]")
    if not (1000 <= chunk_max_chars <= 30000):          # K28 上限 30000
        raise _fail("K28 chunk_max_chars", f"{chunk_max_chars} ∉ [1000,30000]")
    mot = cfg.max_output_tokens.get("summarizer")
    if not isinstance(mot, int) or mot <= 0:
        raise _fail("K28", "max_output_tokens.summarizer 须 > 0")
    cap = mot * 0.6
    if summary_max > cap:                               # K28 硬校验
        raise _fail(
            "K28",
            f"summary_max={summary_max} > max_output_tokens.summarizer×0.6"
            f"（{mot}×0.6={cap:g}）；请调小 summary_max 或调大"
            " max_output_tokens.summarizer")


# ---------------------------------------------------------------------------
# §3.4 脱敏快照与指纹（K11）
# ---------------------------------------------------------------------------

def sanitize_base_url(url: str) -> str:
    """base_url 只记 scheme+host（§1.7 脱敏铁律）；空 → 空串。"""
    if not url:
        return ""
    p = urllib.parse.urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


def _sanitized_llm(cfg: Config) -> dict:
    """脱敏 LLM 配置：剔除 api_key；base_url 只留 scheme+host（含 profiles 双套）。"""
    profs: dict = {}
    for key in ("reader", "summarizer"):
        prof = cfg.profile(key)
        if not prof:
            continue
        p: dict = {
            "base_url": sanitize_base_url(
                prof.get("base_url") or cfg.base_url),
        }
        if prof.get("model"):
            p["model"] = prof["model"]
        if prof.get("api_key_env"):
            p["api_key_env"] = prof["api_key_env"]
        profs[key] = p
    return {
        "backend": cfg.backend,
        "base_url": sanitize_base_url(cfg.base_url),
        "profiles": profs,
        "models": dict(cfg.models),
        "timeout_s": cfg.timeout_s,
        "max_retries": cfg.max_retries,
        "retry_backoff_s": list(cfg.retry_backoff_s),
        "retry_on_status": list(cfg.retry_on_status),
        "temperature": dict(cfg.temperature),
        "max_output_tokens": dict(cfg.max_output_tokens),
        "response_format": cfg.response_format,
    }


def build_snapshot(cfg: Config, *, batch_size: int, timeline_window: int,
                   summary_max: int, chunk_max_chars: int,
                   chunk_padding: int, prompt_version: str) -> dict:
    """§3.4 K11：脱敏快照 = 脱敏 LLM 配置 + 运行参数（绝不含 api_key）。"""
    return {
        "llm": _sanitized_llm(cfg),
        "run": {
            "batch_size": batch_size,
            "timeline_window": timeline_window,
            "summary_max": summary_max,
            "chunk_max_chars": chunk_max_chars,
            "chunk_padding": chunk_padding,
            "prompt_version": prompt_version,
        },
    }


def compute_fingerprint(cfg: Config) -> str:
    """对脱敏 LLM 配置取 sha256 → "sha256:<hex>"（每轮入口比对）。"""
    blob = json.dumps(_sanitized_llm(cfg), sort_keys=True,
                      ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()
