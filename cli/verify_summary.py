"""cli/verify_summary.py — 薄封装：小结验收（§6.3 步骤 6 / §9.4）。

用法：python cli\\verify_summary.py --novel <名>
- 校验 summary.md：UTF-8 无 BOM、单段（内部无 \\n、末尾至多一 \\n）、
  ≤ summary_max（§1.6 字数口径，上限从 metadata 取）；
- 通过 → VERIFY=OK（0）；失败 → 3（K7 失败分支由驱动器处置）。
纪律（§0.3）：≤120 行；只读命令不加锁（K25）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from _common import map_error

import core.paths as paths
import core.state as statemod
import core.validator as validatormod


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="小结验收（§9.4）")
    p.add_argument("--novel", required=True)
    a = p.parse_args(argv)

    base = paths.base_dir()
    root = None
    try:
        novel = paths.validate_novel_name(a.novel)
        root = paths.root_of(base, novel)
        meta = statemod.read_metadata(root / "metadata.json", base=base)
        f = root / "summary.md"
        if not f.exists():
            print(f"ERROR=summary.md 缺失: {f}")
            return 3
        raw = f.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            print("ERROR=summary.md 含 BOM（EF BB BF）")
            return 3
        text = raw.decode("utf-8")
        errs = validatormod.validate_summary_text(text, meta.summary_max)
        if errs:
            print(f"ERROR=summary 校验失败: {'; '.join(errs)}")
            return 3
    except Exception as exc:
        return map_error(exc, meta_path=(root / "metadata.json") if root else None,
                         base=base)
    print("VERIFY=OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
