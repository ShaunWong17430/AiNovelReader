"""core — 业务逻辑（唯一实现源），规格 REQUIREMENTS v3.2.1 §0.3。

铁律：任何业务规则只准在 core\\* 实现一次；cli\\* 仅薄封装。
"""
from pathlib import Path

# CODE_ROOT 常量（§0.3）：模块内一律绝对路径，cwd 无关。
CODE_ROOT = Path(__file__).resolve().parents[1]
