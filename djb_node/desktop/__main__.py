# SPDX-License-Identifier: Apache-2.0
"""``python -m djb_node.desktop`` 入口：等价于 ``djb_node.desktop.app.main()``。"""
from __future__ import annotations

import sys

from .app import main

if __name__ == "__main__":                              # pragma: no cover
    sys.exit(main())
