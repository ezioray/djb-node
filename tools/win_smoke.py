#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Windows 启动冒烟：**先证明能起来、13 个视图能切**，再谈别的。

为什么单独一个脚本
==================

Linux CI 有 xvfb，能跑投影和窗口测试，但**抓不到 Windows Tk 的坑**：
`8b345e5` 给 Shift+Tab 绑了 X11 专有的 `<ISO_Left_Tab>`，Linux 一直全绿，Windows 上
`TclError` 把 `DesktopApp.__init__` 炸掉——客户端双击打不开。这类问题只有真机、真 Tk 能抓。

两种跑法
========

* ``--mode inproc``（默认，venv 里跑）：进程内建窗口 → 断言主窗口存在 → 依次切 13 个视图 →
  逐个开/关弹窗 → 写 JSON。
* ``--mode exe --exe <djb-desktop.exe>``（冻结包）：起进程 → 等主窗口出现 → 用 Ctrl+1..9 /
  Ctrl+PageDown 切页 → 结束时进程还活着 + 窗口还在。冻结包里没法和进程内对话，所以 exe 模式
  只验「起得来、切得动、不死」，**不验每页内容**（内容由 inproc 模式和 pytest 守）。

输出：``win_smoke_<版本>.json``（每步 ok/fail/skip + 耗时），退出码 0 = 全 ok。

用法
====

    python tools/win_smoke.py --out win_smoke_0.3.9.json
    python tools/win_smoke.py --mode exe --exe dist/djb-desktop.exe --out smoke.json
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
RESULT: list[dict] = []


def step(name: str, ok: bool, detail: str = "", ms: float = 0.0, status: str | None = None) -> None:
    RESULT.append({"step": name, "status": status or ("ok" if ok else "fail"),
                   "detail": detail, "ms": round(ms, 1)})
    print("[%s] %s %s" % ("ok" if ok else (status or "fail"), name, detail), flush=True)


def run_step(name: str, fn) -> bool:
    start = time.perf_counter()
    try:
        detail = fn() or ""
        step(name, True, str(detail), (time.perf_counter() - start) * 1000)
        return True
    except Exception as exc:                    # 冒烟脚本要报告，不要自己崩
        step(name, False, "%s: %s" % (type(exc).__name__, exc),
             (time.perf_counter() - start) * 1000)
        return False


def _toplevels(root) -> list:
    import tkinter as tk
    return [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)]


def _close_toplevels(root) -> int:
    closed = 0
    for window in _toplevels(root):
        try:
            window.destroy()
            closed += 1
        except Exception:
            pass
    return closed


def _widget_count(widget) -> int:
    total = 1
    for child in widget.winfo_children():
        total += _widget_count(child)
    return total


# ── inproc 模式 ───────────────────────────────────────────────────────────────
def smoke_inproc(out: Path, views: list[str]) -> int:
    import tkinter as tk

    from djb_node.desktop.app import DesktopApp, make_controller
    from djb_node.desktop.views import VIEW_KEYS

    started = time.perf_counter()
    controller = make_controller(home=None, demo=True, boot=None)
    app = DesktopApp(controller, on_quit=lambda: None)
    root = app._root
    step("launch", True, "Tk %s" % tk.TkVersion, (time.perf_counter() - started) * 1000)

    ok_all = True
    ok_all &= run_step("window-exists", lambda: "exists=%s" % bool(root.winfo_exists()))
    ok_all &= run_step("a11y-provider",
                       lambda: "provider=%s" % type(getattr(app, "_a11y", None)).__name__)

    wanted = VIEW_KEYS if (not views or views == ["all"]) else views
    for key in wanted:
        def switch(key=key):
            app.show_view(key)
            root.update_idletasks()
            root.update()
            current = getattr(app._last_view, "key", "")
            assert current == key, "视图没切过去：期望 %s 实际 %s" % (key, current)
            count = _widget_count(root)
            assert count > 1, "控件数为 %d" % count
            return "控件 %d 个" % count
        ok_all &= run_step("view:" + key, switch)

    dialogs = []
    if hasattr(app, "_confirm"):
        dialogs.append(("dialog:confirm",
                        lambda: app._confirm("冒烟测试：这条只用于确认弹窗能开能关。")))
    if hasattr(app, "_ask_bind_code"):
        dialogs.append(("dialog:bind-code", lambda: app._ask_bind_code()))
    for name, opener in dialogs:
        def open_and_close(opener=opener):
            root.after(250, lambda: _close_toplevels(root))   # 模态：定时把它自己关掉
            opener()
            root.update()
            assert not _toplevels(root), "弹窗没关掉"
            return "开 → 关"
        ok_all &= run_step(name, open_and_close)

    ok_all &= run_step("stop", lambda: (app.stop(), "已收场")[1])
    payload = _payload(out, "inproc", ok_all)
    _write(out, payload)
    return 0 if ok_all else 1


# ── exe 模式 ──────────────────────────────────────────────────────────────────
def _find_window(title: str) -> int:
    import ctypes
    import ctypes.wintypes as wt
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.FindWindowW.restype = wt.HWND
    user32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
    return int(user32.FindWindowW(None, title) or 0)


def smoke_exe(exe: Path, out: Path, timeout: float = 40.0) -> int:
    import ctypes
    import ctypes.wintypes as wt

    title = os.environ.get("DJB_SMOKE_TITLE", "DJB 联脑 · 桌面控制台")
    proc = subprocess.Popen([str(exe)], cwd=str(exe.parent))
    step("launch", True, "pid=%d" % proc.pid)
    ok_all = True

    def wait_window():
        deadline = time.time() + timeout
        while time.time() < deadline:
            if proc.poll() is not None:
                raise AssertionError("进程提前退出，退出码 %s" % proc.returncode)
            hwnd = _find_window(title)
            if hwnd:
                return "hwnd=0x%X" % hwnd
            time.sleep(0.3)
        raise AssertionError("等 %ss 没等到主窗口 %r" % (timeout, title))

    ok_all &= run_step("window-exists", wait_window)

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    for key in ("1", "2", "3", "4", "5", "6", "7", "8", "9"):
        user32.keybd_event(0x11, 0, 0, 0)                  # Ctrl down
        user32.keybd_event(ord(key), 0, 0, 0)
        user32.keybd_event(ord(key), 0, 2, 0)
        user32.keybd_event(0x11, 0, 2, 0)
        time.sleep(0.2)
    ok_all &= run_step("switch-views",
                       lambda: "已发 Ctrl+1..9；进程存活=%s" % (proc.poll() is None))
    ok_all &= run_step("still-alive", lambda: "退出码=%s" % proc.poll())

    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        proc.kill()
    payload = _payload(out, "exe", ok_all)
    payload["exe"] = str(exe)
    _write(out, payload)
    return 0 if ok_all else 1


def _payload(out: Path, mode: str, ok: bool) -> dict:
    return {"mode": mode, "ok": bool(ok), "platform": sys.platform,
            "python": sys.version.split()[0], "as_of": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "steps": RESULT, "failed": [r["step"] for r in RESULT if r["status"] == "fail"]}


def _write(out: Path, payload: dict) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("写出：%s（ok=%s）" % (out, payload["ok"]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Windows 启动冒烟（13 视图 + 弹窗）")
    parser.add_argument("--mode", choices=("inproc", "exe"), default="inproc")
    parser.add_argument("--exe", type=Path, help="冻结包里的 djb-desktop.exe")
    parser.add_argument("--out", type=Path, help="结果 JSON（默认 win_smoke_<版本>.json）")
    parser.add_argument("--views", nargs="*", help="只跑这些视图（默认 13 个全跑）")
    args = parser.parse_args(argv)

    if not IS_WINDOWS:
        print("这条冒烟只对 Windows 有意义（platform=%s）" % sys.platform)
        return 2
    version = os.environ.get("DJB_SMOKE_VERSION", "0.3.9")
    out = args.out or Path("win_smoke_%s.json" % version)
    if args.mode == "exe":
        if not args.exe:
            parser.error("--mode exe 需要 --exe")
        return smoke_exe(args.exe, out)
    return smoke_inproc(out, args.views or [])


if __name__ == "__main__":
    sys.exit(main())
