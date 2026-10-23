# SPDX-License-Identifier: Apache-2.0
"""python -m djb_node -> djb CLI（等价于安装后的 djb 入口脚本）。"""
from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
