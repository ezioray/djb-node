#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""把 ``--gateway`` / ``--update-source`` 安全地写进 profile 的 config.json。

为什么不用 ``sed``（2026-11-15 审计 P2）：URL 里的 ``&`` 在 sed 替换串里会展开成
"整个匹配"，``#`` 会撞上 ``s#...#`` 的分隔符，``"`` / ``\\`` 会直接把 JSON 写坏。
结果不是报错，而是**写出一个 daemon 起不来的 config**（或悄悄写进别的字段）——
安装脚本"看起来成功了"。

用 Python 渲染就没有转义问题：JSON 由 json 库负责，URL 是值、不是语法。
只改这两个字段，其它字段（edition / extra.feature_flags / quotes.*）原样保留。

用法：``render_config.py <源 config.json> <目标路径> <gateway> <update_source>``
"""

from __future__ import annotations

import json
import sys


def render(source: str, target: str, gateway: str, update_source: str) -> dict:
    with open(source, encoding="utf-8") as handle:
        config = json.load(handle)
    if not isinstance(config, dict):                       # pragma: no cover - 配置坏文件
        raise SystemExit("render_config.py: 源配置不是 JSON 对象：" + source)
    config["gateway_url"] = gateway
    config["update_source"] = update_source
    with open(target, "w", encoding="utf-8") as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return config


def main(argv: list[str]) -> int:
    if len(argv) != 5:
        sys.stderr.write("用法：render_config.py <源> <目标> <gateway> <update_source>\n")
        return 2
    config = render(argv[1], argv[2], argv[3], argv[4])
    # 回读断言：写进去的就是调用方给的（防止将来有人改回拼字符串）
    assert config["gateway_url"] == argv[3]
    assert config["update_source"] == argv[4]
    return 0


if __name__ == "__main__":                                 # pragma: no cover
    raise SystemExit(main(sys.argv))
