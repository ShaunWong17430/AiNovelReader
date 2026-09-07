"""小说阅读自动化系统 · 测试底座（阶段 0 交付物，REQUIREMENTS v3.2.1）。

两大职责：

1. BASE 沙箱隔离
   每用例独立临时 BASE/ROOT（自建：普通 mkdir + uuid，teardown rmtree 清理）。
   autouse 守卫在用例前后审计真实 BASE（D:\\data\\dsh_wz1\\novel_reading），
   出现任何新增条目即判用例失败，防止测试污染真实目录（§2 铁律：BASE 单常量）。

   环境约束（阶段 0 实测，禁用清单）：
   - 本执行沙箱中，带 mode=0o700 的 mkdir 会使新建目录事后完全不可访问
     （不可枚举/不可写子项/不可删除），而 pytest 的 tmp_path / mktemp /
     getbasetemp 一律使用 0o700 → 全部禁用；pytest.ini 亦禁用 cacheprovider。
   - 符号链接创建被拒（WinError 1314，无特权），不得依赖。

2. FakeLLM fixture 键控约定（§7.6，K10 / K30，后续所有阶段统一遵守）：
       reader:     (phase, chunk_id)      -> "reader:<chunk_id>"        如 reader:part_001
       summarizer: (phase, "start-end")   -> "summarizer:<start>-<end>" 如 summarizer:1-5
   故障注入同键控：同一键可再出现于 "faults" 段，kind ∈ §8.2 全错误码集。
   详见 tests\\fixtures\\README.md。

   注意 phase 命名映射（§8.5）：fixture 键用 chat() 形参名 reader/summarizer；
   llm.log/backup/rollback 阶段码用 read/summarize，两者不得混用。
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import uuid
from pathlib import Path

import pytest

CODE_ROOT = Path(__file__).resolve().parents[1]             # novel_skill\
TESTS_DIR = CODE_ROOT / "tests"
FIXTURES_DIR = TESTS_DIR / "fixtures"
REAL_BASE = Path(r"D:\data\dsh_wz1\novel_reading")           # §2 单常量
SANDBOX_ROOT = Path(r"D:\data\dsh_wz1\.test_sandboxes")      # 自建沙箱根（工作区内）
DEFAULT_NOVEL_NAME = "示例书名"                                # §1.3 示例

if str(CODE_ROOT) not in sys.path:                           # 使 `import core` 可用
    sys.path.insert(0, str(CODE_ROOT))

# ---------------------------------------------------------------------------
# FakeLLM 键控约定（§7.6 / K10 / K30）
# ---------------------------------------------------------------------------

READER = "reader"
SUMMARIZER = "summarizer"
_PHASES = (READER, SUMMARIZER)

# §8.2 LLM 错误分类码全集（= 故障注入 kind 取值域）
ERROR_CODES = frozenset({
    "NETWORK", "TIMEOUT", "RATE_LIMIT", "SERVER_5XX",
    "AUTH", "NOT_FOUND", "BAD_REQUEST", "PAYLOAD_TOO_LARGE",
    "EMPTY", "TRUNCATED", "PARSE", "SCHEMA", "ABORTED", "UNKNOWN",
})

_KEY_RE = re.compile(r"^(reader|summarizer):(.+)$")
_SUMMARIZER_RANGE_RE = re.compile(r"^(\d+)-(\d+)$")


def fixture_key(phase: str,
                chunk_id: str | None = None, *,
                chunk_start: int | None = None,
                chunk_end: int | None = None) -> str:
    """按 K10/K30 约定构造 fixture 键（reader/summarizer 与故障注入共用）。

    reader     -> fixture_key("reader", "part_001")                     == "reader:part_001"
    summarizer -> fixture_key("summarizer", chunk_start=1, chunk_end=5) == "summarizer:1-5"
    """
    if phase == READER:
        if chunk_id is None or not str(chunk_id):
            raise ValueError("reader 键需要 chunk_id")
        return f"{READER}:{chunk_id}"
    if phase == SUMMARIZER:
        if chunk_start is None or chunk_end is None:
            raise ValueError("summarizer 键需要 chunk_start 与 chunk_end")
        if not (1 <= chunk_start <= chunk_end):
            raise ValueError(f"非法范围: {chunk_start}-{chunk_end}")
        return f"{SUMMARIZER}:{chunk_start}-{chunk_end}"
    raise ValueError(f"未知 phase: {phase!r}（应为 {_PHASES}）")


def split_fixture_key(key: str) -> tuple[str, str]:
    """校验并分解 fixture 键 -> (phase, suffix)；格式非法抛 ValueError。"""
    m = _KEY_RE.match(key)
    if not m:
        raise ValueError(
            f"fixture 键格式错误: {key!r}（应为 reader:<chunk_id> 或 summarizer:<start>-<end>）")
    phase, suffix = m.group(1), m.group(2)
    if phase == READER:
        if not suffix.strip():
            raise ValueError(f"reader 键 chunk_id 不能为空白: {key!r}")
    elif not _SUMMARIZER_RANGE_RE.match(suffix):
        raise ValueError(f"summarizer 键后缀须为整数 <start>-<end>: {key!r}")
    return phase, suffix


class FixtureStore:
    """加载 tests/fixtures/*.json，校验键控与故障 kind，提供查找。

    fixture 文件格式（键控约定详见 tests/fixtures/README.md）：
        {
          "responses": { "reader:part_001": {...reader 完整 JSON（§4.4）...},
                         "summarizer:1-5":  "纯文本单段（§4.3）" },
          "faults":    { "reader:part_003": {"kind": "NETWORK", "attempts": 2} }
        }
    """

    def __init__(self, fixtures_dir: Path):
        self.dir = fixtures_dir
        self.responses: dict[str, object] = {}
        self.faults: dict[str, dict] = {}
        for path in sorted(fixtures_dir.glob("*.json")):
            self._load(path)

    def _load(self, path: Path) -> None:
        data = json.loads(path.read_text(encoding="utf-8"))
        for key, value in (data.get("responses") or {}).items():
            split_fixture_key(key)
            if key in self.responses:
                raise ValueError(f"fixture 文件间 response 键重复: {key}（{path.name}）")
            self.responses[key] = value
        for key, spec in (data.get("faults") or {}).items():
            split_fixture_key(key)
            if key in self.faults:
                raise ValueError(f"fixture 文件间 fault 键重复: {key}（{path.name}）")
            if not isinstance(spec, dict) or spec.get("kind") not in ERROR_CODES:
                raise ValueError(f"fault kind 必须 ∈ §8.2 全码集: {key} -> {spec!r}")
            attempts = spec.get("attempts", 1)
            if not isinstance(attempts, int) or attempts < 1:
                raise ValueError(f"fault attempts 必须为正整数: {key}")
            self.faults[key] = spec

    # -- 查找（fixture_key 的糖封装） ----------------------------------------

    def response(self, phase, chunk_id=None, *, chunk_start=None, chunk_end=None):
        key = fixture_key(phase, chunk_id, chunk_start=chunk_start, chunk_end=chunk_end)
        if key not in self.responses:
            raise KeyError(f"无预设响应: {key}")
        return self.responses[key]

    def fault(self, phase, chunk_id=None, *, chunk_start=None, chunk_end=None):
        """故障注入与 responses 同键控；无注入返回 None。"""
        key = fixture_key(phase, chunk_id, chunk_start=chunk_start, chunk_end=chunk_end)
        return self.faults.get(key)


# ---------------------------------------------------------------------------
# pytest fixtures
# ---------------------------------------------------------------------------

def pytest_configure(config):
    """会话开始：尽力清理上次异常退出遗留的沙箱目录（忽略删不掉者）。"""
    if SANDBOX_ROOT.exists():
        for child in list(SANDBOX_ROOT.iterdir()):
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)


@pytest.fixture(scope="session")
def fixture_store() -> FixtureStore:
    """全量加载 tests/fixtures/*.json（加载期即校验键控约定）。"""
    return FixtureStore(FIXTURES_DIR)


@pytest.fixture
def base_dir():
    """每用例独立临时 BASE（对应真实 D:\\data\\dsh_wz1\\novel_reading）。

    禁用 pytest tmp_path（其 mkdir 恒带 mode=0o700，在本沙箱中会毒化目录）；
    此处普通 mkdir（不传 mode）+ uuid 自建，teardown 整树清理。
    """
    case_dir = SANDBOX_ROOT / f"case_{uuid.uuid4().hex}"
    base = case_dir / "base"
    case_dir.mkdir(parents=True)
    base.mkdir()
    yield base
    shutil.rmtree(case_dir, ignore_errors=True)


@pytest.fixture
def root_dir(base_dir: Path) -> Path:
    """ROOT = BASE / novel_name（§1.1），预建 chunks\\ 子目录。"""
    root = base_dir / DEFAULT_NOVEL_NAME
    (root / "chunks").mkdir(parents=True)
    return root


@pytest.fixture(autouse=True)
def _guard_real_base():
    """污染防线：每用例前后审计真实 BASE 无新增（含目录与文件）。"""
    def snapshot() -> set[Path]:
        return set(REAL_BASE.rglob("*")) if REAL_BASE.exists() else set()

    before = snapshot()
    yield
    created = snapshot() - before
    assert not created, (
        f"测试污染了真实 BASE {REAL_BASE}: {sorted(map(str, created))[:5]}")
