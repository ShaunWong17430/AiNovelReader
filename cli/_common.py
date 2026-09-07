"""cli/_common.py — cli 薄封装共用件（§0.3 纪律：无业务规则，仅错误映射与打印）。

- sys.path 前置 CODE_ROOT，使 `import core` 可用（调用约定：直接运行
  `python cli\\xxx.py`，cwd 固定 CODE_ROOT）；
- `map_error(exc, ...)`：已知异常 → 打印 ERROR= 行 + 退出码；
  未知异常不捕获（traceback 退出码 1，供驱动器按 §8.4「非 0/2/3/4」处置）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.config as cfgmod          # noqa: E402
import core.lock as lockmod           # noqa: E402
import core.paths as paths            # noqa: E402
import core.recovery as recovery      # noqa: E402
import core.renderer as rendermod     # noqa: E402
import core.state as statemod         # noqa: E402
import core.timeline as timelinemod   # noqa: E402


def warn_stderr(msg: str) -> None:
    """锁接管等 WARN 提示 → stderr（§1.8 警告接管 log WARN；ROOT 未建时无 run.log）。"""
    print(f"WARN {msg}", file=sys.stderr)


def map_error(exc, *, meta_path: Path | None = None,
              base: Path | None = None) -> int:
    """已知异常 → (打印 ERROR 行, 退出码)；未知异常原样上抛（退出码 1）。"""
    if isinstance(exc, cfgmod.ConfigError):
        print(f"ERROR={exc}")
        return 2
    if isinstance(exc, paths.InvalidName):
        print(f"ERROR={exc}")
        return 2
    if isinstance(exc, lockmod.LockError):
        print(f"ERROR={exc}")
        return 3
    if isinstance(exc, recovery.InitError):
        print(f"ERROR={exc}")
        return exc.code
    if isinstance(exc, recovery.BackupError):
        print(f"ERROR={exc}")
        return exc.code
    if isinstance(exc, recovery.RollbackError):
        print(f"ERROR={exc}")
        return exc.code
    if isinstance(exc, statemod.MetadataMissing):
        print(f"ERROR=metadata.json 缺失（先 init）: {exc}")
        return 2
    if isinstance(exc, statemod.RootDirMismatch):
        print(f"ERROR={exc}")
        return 3
    if isinstance(exc, statemod.MetadataCorrupt):
        return _emergency(meta_path, base, exc)
    if isinstance(exc, statemod.StateError):
        print(f"ERROR={exc}")
        return 3
    if isinstance(exc, timelinemod.TimelineError):
        print(f"ERROR={exc}")
        return 3
    if isinstance(exc, rendermod.RenderError):
        print(f"ERROR={exc}")
        return 3
    raise exc


def _emergency(meta_path, base, exc) -> int:
    """② schema 校验失败 → emergency：改名保留现场 + 最小 error metadata。"""
    if meta_path is None:
        print(f"ERROR=metadata 损坏（无路径可 emergency）: {exc}")
        return 3
    try:
        res = statemod.set_error(meta_path, f"metadata 损坏: {exc}", base=base)
        if res.renamed:
            print(f"ERROR=metadata 损坏已 emergency（改名 {res.corrupt_path.name}）: {exc}")
        else:
            print(f"ERROR=metadata 损坏已 emergency（D2 未改名）: {exc}")
    except Exception as e2:
        print(f"ERROR=emergency 失败: {e2}")
    return 3
