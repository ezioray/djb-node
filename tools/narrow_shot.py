"""窄窗取证：把窗口压到 390 宽（E10 口径）看表格列会不会被挤出可视区。

用法::

    python tools/narrow_shot.py <out-dir> [--width 390] [--view receipts] [--demo]

只读：不联网时用 ``--demo``，要真数据就在 ``xvfb-run`` 里跑并注入 key（同 live_shot.py）。
输出：PNG + 一行 JSON（可视宽 / 各列宽 / 是否溢出），便于原样落盘当证据。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("out")
    parser.add_argument("--width", type=int, default=390)
    parser.add_argument("--height", type=int, default=780)
    parser.add_argument("--view", default="receipts")
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--base", default="http://127.0.0.1:18090")
    parser.add_argument("--key-file", default="~/.djb/secrets/testnode-client-key")
    args = parser.parse_args(argv)

    from djb_node.desktop.app import DesktopApp, demo_snapshot, make_controller

    cloud = None
    if not args.demo:
        from djb_node.desktop.nodecloud import NodeCloudClient

        key = open(os.path.expanduser(args.key_file), encoding="utf-8").read().strip()
        cloud = NodeCloudClient(args.base, key)          # key 只在内存里
    home = Path(os.environ.get("DJB_HOME", "/tmp/djb-narrow"))
    controller = make_controller(home=home, demo=args.demo, cloud=cloud)
    app = DesktopApp(controller)
    root = app._root                                  # noqa: SLF001 - 取证脚本
    root.minsize(1, 1)                                # 强制压到 390（产品本身 minsize 900）
    root.geometry(str(args.width) + "x" + str(args.height))
    app.show_view(args.view)
    root.update_idletasks()
    root.update()
    tree = getattr(app, "_tree", None)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    png = out / ("narrow-" + str(args.width) + "-" + args.view + ".png")
    try:
        subprocess.run(["import", "-window", str(root.winfo_id()), str(png)],
                       check=False, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass
    shown = (list(tree["displaycolumns"]) if tree is not None
             and tree["displaycolumns"] not in (("#all",), "#all") else
             (list(tree["columns"]) if tree is not None else []))
    widths = [int(tree.column(c, "width")) for c in shown] if tree is not None else []
    info = {"view": args.view, "width": args.width, "window": root.winfo_width(),
            "tree_width": int(tree.winfo_width()) if tree is not None else None,
            "columns": list(tree["columns"]) if tree is not None else [],
            "shown": shown,
            "hidden": [c for c in (tree["columns"] if tree is not None else [])
                       if c not in shown],
            "column_widths": widths,
            "fits": (sum(widths) <= int(tree.winfo_width())) if tree is not None else None,
            "png": str(png) if png.exists() else ""}
    print(json.dumps(info, ensure_ascii=False))
    return 0


if __name__ == "__main__":                            # pragma: no cover
    sys.exit(main())
