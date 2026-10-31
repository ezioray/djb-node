#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""F16 真联调脚本：桌面客户端 → 现役测试网 cloud（只读）。

用法::

    python tools/e2e_nodecloud.py --base http://127.0.0.1:18090 \
        --key-file ~/.djb/secrets/testnode-client-key

行为约定（都是硬要求）：

* **凭据只进内存**：密钥文件只读内容到内存，绝不打印、绝不写日志、绝不进输出文件；
  输出里只有长度/是否以换行结尾这类元信息。
* **只读**：四个端点全是 GET，本脚本不改任何服务端状态。
* **取不到就说取不到**：每个域打印 ``ok`` / ``unavailable`` + 原因，退出码非 0（有域失败时），
  绝不把失败写成 0 条。
* 输出可 `--json`（默认），便于原样落盘成证据。

退出码：0 全部成功；5 有域不可用；2 参数/凭据文件问题。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from djb_node.desktop.datasource import ProviderError          # noqa: E402
from djb_node.desktop.nodecloud import NodeCloudClient, micro_text  # noqa: E402

EXIT_USAGE = 2
EXIT_UNAVAILABLE = 5

DOMAINS = ("tasks", "receipts", "earnings", "profile")


def read_key(path: Path) -> str:
    """读密钥到内存；只校验形态，不返回任何可用于识别的片段。"""
    if not path.exists():
        raise SystemExit("凭据文件不存在：" + str(path) + "（等 lead 放好再跑）")
    mode = os.stat(path).st_mode & 0o777
    if mode & 0o077:
        raise SystemExit("凭据文件权限过宽：" + oct(mode) + "（应为 0600）")
    key = path.read_text(encoding="utf-8").strip()
    if not key:
        raise SystemExit("凭据文件是空的")
    return key


def summarize(kind: str, data) -> dict:
    """把结果压成「有多少条 + 关键字段是否齐全」，不回显正文。"""
    if kind == "earnings":
        return {"settled": data.get("settled"), "pending": data.get("pending"),
                "currency": data.get("currency"), "note": data.get("note"),
                "rows": len(data.get("payouts") or [])}
    if kind == "profile":
        return {key: data.get(key) for key in
                ("node_id", "state", "region", "platform", "bound", "last_seen_at")}
    rows = data.get("items") if isinstance(data, dict) else data
    rows = rows or []
    first = rows[0] if rows else {}
    return {"rows": len(rows),
            "state_field": first.get("state") or first.get("status") or first.get("receipt_id"),
            "truncated": bool((data or {}).get("truncated")) if isinstance(data, dict) else False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="F16 真联调（只读）")
    parser.add_argument("--base", default="http://127.0.0.1:18090")
    parser.add_argument("--key-file", default="~/.djb/secrets/testnode-client-key")
    parser.add_argument("--json", action="store_true", help="输出 JSON（默认也是 JSON）")
    args = parser.parse_args(argv)

    key = read_key(Path(os.path.expanduser(args.key_file)))
    client = NodeCloudClient(args.base, key, timeout=10.0)
    result: dict = {"base": args.base, "key_len": len(key), "domains": {}}
    failures = 0
    for kind in DOMAINS:
        try:
            if kind == "earnings":
                data = client.earnings()
            elif kind == "profile":
                data = client.profile()
            else:
                data = client.fetch(kind)
        except ProviderError as exc:
            failures += 1
            result["domains"][kind] = {"ok": False, "reason": exc.reason, "hint": exc.hint}
        else:
            result["domains"][kind] = {"ok": True, **summarize(kind, data)}
    result["ok"] = failures == 0
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if failures == 0 else EXIT_UNAVAILABLE


if __name__ == "__main__":
    raise SystemExit(main())
