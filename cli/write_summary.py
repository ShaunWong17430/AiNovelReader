"""cli/write_summary.py — 薄封装：写 summary.md（§6.3 步骤 4 / §9.4 校验）。

用法：python cli\\write_summary.py --novel <名> --processed <N> [--max <summary_max>]
- 从 summary_draft.txt 读草稿 → §9.4 校验（单段 / ≤ max 字 / UTF-8 无 BOM）；
- 不满足 → 退出码 3，草稿保留（§6.3 步骤 5：回到步骤 4 重新生成）；
- 成功 → 原子写 summary.md + 写 .summary_applied（内容 = processed 值，§1.1）。
- --max 缺省从 metadata.summary_max 取。
纪律（§0.3）：≤120 行；写命令复用同锁（K25）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from _common import map_error, warn_stderr

import core.lock as lockmod
import core.paths as paths
import core.state as statemod
import core.timeline as timelinemod
import core.validator as validatormod


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="写 summary.md（§9.4）")
    p.add_argument("--novel", required=True)
    p.add_argument("--processed", type=int, required=True)
    p.add_argument("--max", type=int)
    a = p.parse_args(argv)

    base = paths.base_dir()
    root = None
    lock = None
    try:
        novel = paths.validate_novel_name(a.novel)
        root = paths.root_of(base, novel)
        lock = lockmod.acquire(base, novel, warn_sink=warn_stderr)
        meta = statemod.read_metadata(root / "metadata.json", base=base)
        max_chars = a.max if a.max is not None else meta.summary_max
        draft = root / "summary_draft.txt"
        if not draft.exists():
            print(f"ERROR=summary_draft.txt 缺失: {draft}")
            return 3
        text = draft.read_text(encoding="utf-8")
        errs = validatormod.validate_summary_text(text, max_chars)
        if errs:
            print(f"ERROR=summary 校验失败（草稿保留）: {'; '.join(errs)}")
            return 3
        statemod.atomic_write_text(root / "summary.md", text)       # 不变量 6/3
        statemod.atomic_write_text(root / ".summary_applied", str(a.processed))
    except Exception as exc:
        return map_error(exc, meta_path=(root / "metadata.json") if root else None,
                         base=base)
    finally:
        if lock is not None:
            lock.release()
    print(f"SUMMARY=OK processed={a.processed} chars="
          f"{timelinemod.count_chars(text)} max={max_chars}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
