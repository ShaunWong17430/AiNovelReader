"""出口测试 3 / 5 与 §3 配置：core/config.py（§3.2 / §3.3 / §3.4 / K28 / K11）。

覆盖：
- §3.3 校验表违规 → ConfigError（→ 退出码 2）；
- §3.2 api_key 三级解析（env > config.api_key[${ENV}] > 无 → openai 拒）；
- §3.4 脱敏快照（不含 api_key、base_url 只留 scheme+host）与指纹稳定性；
- K28 init 硬校验（summary_max vs max_output_tokens.summarizer×0.6、
  chunk_max_chars 上限 30000）+ K2 参数前置。
"""
from __future__ import annotations

import json

import pytest

import core.config as cfgmod

from helpers import write_config


def _load(tmp, **kw):
    p = write_config(tmp, **kw)
    return cfgmod.load_config(p)


# ---------------------------------------------------------------------------
# §3.3 校验表
# ---------------------------------------------------------------------------

def test_load_default_shipped_config():
    cfg = cfgmod.load_config()      # 仓库自带 config.json（阶段 0）
    assert cfg.backend == "openai"
    assert cfg.max_output_tokens["summarizer"] >= 8334   # K28 默认参数兼容


def test_invalid_backend(base_dir):
    with pytest.raises(cfgmod.ConfigError, match="backend"):
        _load(base_dir, backend="claude")


def test_invalid_base_url(base_dir):
    for url in ("", "ftp://x", "openai.com/v1", "https://a b"):
        with pytest.raises(cfgmod.ConfigError, match="base_url"):
            _load(base_dir, base_url=url)


def test_invalid_models(base_dir):
    with pytest.raises(cfgmod.ConfigError, match="models"):
        _load(base_dir, models={"reader": "", "summarizer": "x"})


def test_invalid_numeric_params(base_dir):
    with pytest.raises(cfgmod.ConfigError, match="timeout_s"):
        _load(base_dir, timeout_s=0)
    with pytest.raises(cfgmod.ConfigError, match="timeout_s"):
        _load(base_dir, timeout_s=601)
    with pytest.raises(cfgmod.ConfigError, match="max_retries"):
        _load(base_dir, max_retries=6)
    with pytest.raises(cfgmod.ConfigError, match="retry_backoff_s"):
        _load(base_dir, retry_backoff_s=[2, 6])          # 长度 != max_retries
    with pytest.raises(cfgmod.ConfigError, match="retry_backoff_s"):
        _load(base_dir, retry_backoff_s=[2, -6, 18])     # 元素 < 0
    with pytest.raises(cfgmod.ConfigError, match="max_output_tokens"):
        _load(base_dir, max_output_tokens_summarizer=0)
    with pytest.raises(cfgmod.ConfigError, match="response_format"):
        _load(base_dir, response_format="xml")


def test_config_json_parse_failure(base_dir):
    (base_dir / "config.json").write_text("{ not json", encoding="utf-8")
    with pytest.raises(cfgmod.ConfigError, match="解析失败"):
        cfgmod.load_config(base_dir / "config.json")


# ---------------------------------------------------------------------------
# §3.2 api_key 三级解析
# ---------------------------------------------------------------------------

def test_api_key_env_wins(monkeypatch, base_dir):
    monkeypatch.setenv("NOVEL_LLM_API_KEY", "env-key-123")
    cfg = _load(base_dir, backend="openai", api_key="cfg-key")
    assert cfgmod.resolve_api_key(cfg) == "env-key-123"
    assert cfgmod.ensure_api_key(cfg) == "env-key-123"


def test_api_key_env_name_from_config(monkeypatch, base_dir):
    monkeypatch.setenv("MY_CUSTOM_KEY", "custom-456")
    cfg = _load(base_dir, backend="openai", api_key_env="MY_CUSTOM_KEY")
    assert cfgmod.resolve_api_key(cfg) == "custom-456"


def test_api_key_plain_config_value(monkeypatch, base_dir):
    monkeypatch.delenv("NOVEL_LLM_API_KEY", raising=False)
    cfg = _load(base_dir, backend="openai", api_key="plain-789")
    assert cfgmod.resolve_api_key(cfg) == "plain-789"


def test_api_key_env_expansion(monkeypatch, base_dir):
    monkeypatch.setenv("SUPER_KEY", "expanded-key")
    monkeypatch.delenv("NOVEL_LLM_API_KEY", raising=False)
    cfg = _load(base_dir, backend="openai", api_key="${SUPER_KEY}")
    assert cfgmod.resolve_api_key(cfg) == "expanded-key"


def test_missing_key_openai_fails(base_dir, monkeypatch):
    monkeypatch.delenv("NOVEL_LLM_API_KEY", raising=False)
    cfg = _load(base_dir, backend="openai", api_key=None)
    assert cfgmod.resolve_api_key(cfg) is None
    with pytest.raises(cfgmod.ConfigError, match="api_key"):
        cfgmod.ensure_api_key(cfg)


def test_fake_backend_no_key_needed(base_dir, monkeypatch):
    monkeypatch.delenv("NOVEL_LLM_API_KEY", raising=False)
    cfg = _load(base_dir, backend="fake")
    assert cfgmod.ensure_api_key(cfg) == ""              # fake 不需 key


# ---------------------------------------------------------------------------
# §3.4 脱敏快照与指纹（K11）+ 测试 4 快照不含 key
# ---------------------------------------------------------------------------

def test_snapshot_sanitized_no_api_key(base_dir, monkeypatch):
    monkeypatch.setenv("NOVEL_LLM_API_KEY", "sk-super-secret-xyz")
    cfg = _load(base_dir, backend="openai", api_key="sk-cfg-secret")
    snap = cfgmod.build_snapshot(cfg, batch_size=5, timeline_window=10,
                                 summary_max=5000, chunk_max_chars=20000,
                                 chunk_padding=3, prompt_version="v1")
    blob = json.dumps(snap, ensure_ascii=False)
    assert "sk-super-secret-xyz" not in blob
    assert "sk-cfg-secret" not in blob
    assert snap["llm"]["base_url"] == "https://api.openai.com"   # scheme+host
    assert "/v1" not in snap["llm"]["base_url"]
    assert snap["run"]["batch_size"] == 5                        # K11 运行参数
    assert snap["run"]["prompt_version"] == "v1"


def test_fingerprint_deterministic_and_sensitive_to_config(base_dir, monkeypatch):
    monkeypatch.setenv("NOVEL_LLM_API_KEY", "whatever")
    cfg_a = _load(base_dir, backend="openai")
    cfg_b = _load(base_dir, backend="openai")
    assert cfgmod.compute_fingerprint(cfg_a) == cfgmod.compute_fingerprint(cfg_b)
    assert cfgmod.compute_fingerprint(cfg_a).startswith("sha256:")
    cfg_c = _load(base_dir, backend="openai", timeout_s=30)
    assert cfgmod.compute_fingerprint(cfg_c) != cfgmod.compute_fingerprint(cfg_a)
    # 指纹不含 api_key（key 变化不改变指纹）
    cfg_key = _load(base_dir, backend="openai", api_key="secret-A")
    assert cfgmod.compute_fingerprint(cfg_key) == cfgmod.compute_fingerprint(cfg_a)


# ---------------------------------------------------------------------------
# K2 / K28 init 硬校验（§6.4 步骤 2，任何落盘之前）
# ---------------------------------------------------------------------------

def test_k28_summary_max_vs_output_tokens(base_dir):
    cfg = _load(base_dir, max_output_tokens_summarizer=8000)   # ×0.6 = 4800
    with pytest.raises(cfgmod.ConfigError, match="K28"):
        cfgmod.validate_init_params(cfg, batch_size=5, timeline_window=10,
                                    summary_max=20000, chunk_max_chars=20000)
    cfg2 = _load(base_dir, max_output_tokens_summarizer=10000)  # ×0.6 = 6000
    cfgmod.validate_init_params(cfg2, batch_size=5, timeline_window=10,
                                summary_max=5000, chunk_max_chars=20000)


def test_k28_chunk_max_chars_upper_bound(base_dir):
    cfg = _load(base_dir)
    with pytest.raises(cfgmod.ConfigError, match="K28"):
        cfgmod.validate_init_params(cfg, batch_size=5, timeline_window=10,
                                    summary_max=5000, chunk_max_chars=200000)
    cfgmod.validate_init_params(cfg, batch_size=5, timeline_window=10,
                                summary_max=5000, chunk_max_chars=30000)


def test_k2_param_ranges(base_dir):
    cfg = _load(base_dir)
    with pytest.raises(cfgmod.ConfigError, match="timeline_window"):
        cfgmod.validate_init_params(cfg, batch_size=5, timeline_window=0,
                                    summary_max=5000, chunk_max_chars=20000)
    with pytest.raises(cfgmod.ConfigError, match="batch_size"):
        cfgmod.validate_init_params(cfg, batch_size=21, timeline_window=10,
                                    summary_max=5000, chunk_max_chars=20000)
    with pytest.raises(cfgmod.ConfigError, match="summary_max"):
        cfgmod.validate_init_params(cfg, batch_size=5, timeline_window=10,
                                    summary_max=999, chunk_max_chars=20000)
    with pytest.raises(cfgmod.ConfigError, match="chunk_max_chars"):
        cfgmod.validate_init_params(cfg, batch_size=5, timeline_window=10,
                                    summary_max=5000, chunk_max_chars=999)
