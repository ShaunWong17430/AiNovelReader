"""gui/config_io.py — GUI 与 config.json / .secrets.json 之间的读写。

- `load_gui_state()`：读 config.json（缺省 CODE_ROOT/config.json）→ 回填
  界面字段；api key 从 DPAPI 密文解密（无明文落盘、无明文进 config.json）；
- `save_gui_state(...)`：url/model 写 config.json（保留其余 llm/run 字段）；
  api key 只写 gui/.secrets.json（DPAPI 加密），config.json 仅记
  api_key_env（worker 运行前解密注入环境变量）。
"""
from __future__ import annotations

import json
from pathlib import Path

from . import secrets as secretsmod

CODE_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = CODE_ROOT / "config.json"

# phase → 独立环境变量名（profile.api_key_env；绝不落明文 key 到 config.json）
PHASE_ENV = {
    "reader": "NOVEL_LLM_READER_KEY",
    "summarizer": "NOVEL_LLM_SUMMARIZER_KEY",
}


def _read_config() -> dict:
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load_gui_state() -> dict:
    """读当前配置：返回 {"reader": {...}, "summarizer": {...}} 供界面回填。

    每项含 base_url / model / api_key（api_key 为空串表示无密钥）。
    """
    data = _read_config()
    llm = data.get("llm") or {}
    profiles = llm.get("profiles") or {}
    models = llm.get("models") or {}
    out: dict = {}
    for phase in ("reader", "summarizer"):
        prof = profiles.get(phase) if isinstance(profiles, dict) else None
        prof = prof if isinstance(prof, dict) else {}
        base_url = prof.get("base_url") or llm.get("base_url") or ""
        model = prof.get("model") or models.get(phase) or ""
        key = secretsmod.load_secret(phase) or ""
        out[phase] = {"base_url": base_url, "model": model, "api_key": key}
    return out


def save_gui_state(reader: dict, summarizer: dict) -> Path:
    """写 config.json（url/model/api_key_env）+ .secrets.json（加密 key）。

    保留 config.json 中其它 llm/run 字段（backend/timeout/温度/上限等）；
    返回 config.json 路径。
    """
    data = _read_config()
    llm = data.get("llm") or {}
    llm = dict(llm)
    if "backend" not in llm:
        llm["backend"] = "openai"
    if "base_url" not in llm:
        llm["base_url"] = reader.get("base_url") or ""

    profiles: dict = {}
    models: dict = {}
    for phase, cfg in (("reader", reader), ("summarizer", summarizer)):
        url = (cfg.get("base_url") or "").strip()
        model = (cfg.get("model") or "").strip()
        prof: dict = {"api_key_env": PHASE_ENV[phase]}
        if url:
            prof["base_url"] = url
        if model:
            prof["model"] = model
        profiles[phase] = prof
        if model:
            models[phase] = model
        key = (cfg.get("api_key") or "").strip()
        if key:
            secretsmod.save_secret(phase, key)
        else:
            secretsmod.delete_secret(phase)      # 清空输入 = 清除旧密文

    llm["profiles"] = profiles
    if models:
        llm["models"] = models
    data["llm"] = llm
    try:
        CONFIG_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"config.json 写入失败: {CONFIG_PATH}（{exc}）") from exc
    return CONFIG_PATH


def config_exists() -> bool:
    return CONFIG_PATH.exists()


def get_phase_env(phase: str) -> str:
    return PHASE_ENV[phase]