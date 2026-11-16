#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Windows 启动冒烟：**先证明能起来、13 个视图能切**，再谈别的。

为什么单独一个脚本
==================

Linux CI 有 xvfb，能跑投影和窗口测试，但**抓不到 Windows Tk 的坑**：
`8b345e5` 给 Shift+Tab 绑了 X11 专有的 `<ISO_Left_Tab>`，Linux 一直全绿，Windows 上
`TclError` 把 `DesktopApp.__init__` 炸掉——客户端双击打不开。这类问题只有真机、真 Tk 能抓。

**每一步都返回 `(ok, detail)`，`ok=False` 就是这一步失败**（2026-11-16 复核 MUST-FIX #2：
早先的版本只看「函数没抛异常」，`window-exists`/`a11y-provider`/`still-alive` 恒 ok，等于假绿）。

两种跑法
========

* ``--mode inproc``（默认，venv 里跑）：进程内建窗口 → 断言主窗口存在且标题对 → 断言 a11y
  provider 真的挂上且 ``accChildCount>0`` → 依次切 13 个视图 → 逐个开/关弹窗 → 写 JSON。
* ``--mode exe --exe <djb-desktop.exe>``（冻结包）：起进程 → **按标题找窗口**（Tk 窗口属于
  python 子进程，按启动 pid 找不到）→ ``SetForegroundWindow`` 抢前台 → 发 Ctrl+1..9 →
  结束断言「进程活着 **且** 窗口还在」。

自测（证明脚本会红）
====================

    python tools/win_smoke.py --self-test-fail      # 故意把窗口标题写错 → 必须退出码 1

输出：``win_smoke_<版本>.json``（每步 ok/fail + 耗时 + detail），退出码 0 = 全 ok，1 = 有失败。

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
TITLE = "DJB 联脑 · 桌面控制台"
BAD_TITLE = "这个标题故意不存在 · self-test"


def step(name: str, ok: bool, detail: str = "", ms: float = 0.0) -> None:
    RESULT.append({"step": name, "status": "ok" if ok else "fail",
                   "detail": detail, "ms": round(ms, 1)})
    print("[%s] %s %s" % ("ok" if ok else "FAIL", name, detail), flush=True)


def run_step(name: str, fn) -> bool:
    """跑一步：``fn`` 必须返回 ``(ok, detail)``；抛异常算失败。**不返回 bool 也算失败**。"""
    start = time.perf_counter()
    try:
        result = fn()
        if not (isinstance(result, tuple) and len(result) == 2):
            raise AssertionError("这一步没返回 (ok, detail)，返回值=%r" % (result,))
        ok, detail = result
        step(name, bool(ok), str(detail), (time.perf_counter() - start) * 1000)
        return bool(ok)
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
def smoke_inproc(out: Path, views: list[str], self_test_fail: bool = False) -> int:
    import tkinter as tk

    from djb_node.desktop.app import DesktopApp, make_controller
    from djb_node.desktop.views import VIEW_KEYS

    expect_title = BAD_TITLE if self_test_fail else TITLE
    started = time.perf_counter()
    controller = make_controller(home=None, demo=True, boot=None)
    app = DesktopApp(controller, on_quit=lambda: None)
    root = app._root
    step("launch", True, "Tk %s" % tk.TkVersion, (time.perf_counter() - started) * 1000)

    ok_all = True

    def window_exists():
        alive = bool(root.winfo_exists())
        title = ""
        try:
            title = root.title()
        except Exception:
            pass
        return alive and title == expect_title, "exists=%s title=%r" % (alive, title)

    ok_all &= run_step("window-exists", window_exists)

    def a11y_provider():
        provider = getattr(app, "_a11y", None)
        if provider is None:
            return False, "provider=None（读屏没挂上）"
        kids = provider.children("")
        if not kids:
            return False, "provider 在，但 accChildCount=0"
        return True, "childCount=%d" % len(kids)

    ok_all &= run_step("a11y-provider", a11y_provider)

    wanted = VIEW_KEYS if (not views or views == ["all"]) else views
    for key in wanted:
        def switch(key=key):
            app.show_view(key)
            root.update_idletasks()
            root.update()
            current = getattr(app._last_view, "key", "")
            if current != key:
                return False, "视图没切过去：期望 %s 实际 %s" % (key, current)
            count = _widget_count(root)
            return count > 1, "控件 %d 个" % count
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
            left = _toplevels(root)
            return not left, ("弹窗开 → 关" if not left else "还剩 %d 个弹窗" % len(left))
        ok_all &= run_step(name, open_and_close)

    def stop():
        app.stop()
        return True, "已收场"

    ok_all &= run_step("stop", stop)
    payload = _payload(out, "inproc", ok_all)
    payload["self_test_fail"] = bool(self_test_fail)
    payload["expect_title"] = expect_title
    _write(out, payload)
    return 0 if ok_all else 1


# ── exe 模式 ──────────────────────────────────────────────────────────────────
def _user32():
    import ctypes
    return ctypes.WinDLL("user32", use_last_error=True)


def _find_window(title: str) -> int:
    import ctypes.wintypes as wt
    user32 = _user32()
    user32.FindWindowW.restype = wt.HWND
    user32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
    return int(user32.FindWindowW(None, title) or 0)


def _window_title(hwnd: int) -> str:
    from ctypes import create_unicode_buffer
    user32 = _user32()
    buffer = create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buffer, 512)
    return buffer.value


def _window_present(hwnd: int) -> bool:
    return bool(hwnd) and bool(_user32().IsWindow(hwnd))


def smoke_exe(exe: Path, out: Path, timeout: float = 40.0, self_test_fail: bool = False) -> int:
    title = BAD_TITLE if self_test_fail else os.environ.get("DJB_SMOKE_TITLE", TITLE)
    user32 = _user32()
    proc = subprocess.Popen([str(exe)], cwd=str(exe.parent))
    step("launch", True, "pid=%d" % proc.pid)
    ok_all = True
    state: dict[str, int] = {"hwnd": 0}

    def wait_window():
        deadline = time.time() + timeout
        while time.time() < deadline:
            if proc.poll() is not None:
                return False, "进程提前退出，退出码 %s" % proc.returncode
            hwnd = _find_window(title)
            if hwnd:
                state["hwnd"] = hwnd
                return True, "hwnd=0x%X title=%r" % (hwnd, _window_title(hwnd))
            time.sleep(0.3)
        return False, "等 %ss 没等到主窗口 %r" % (timeout, title)

    ok_all &= run_step("window-exists", wait_window)
    hwnd = state["hwnd"]

    def focus_window():
        if not _window_present(hwnd):
            return False, "窗口不在了，没法抢前台"
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.3)
        foreground = int(user32.GetForegroundWindow() or 0)
        return foreground == hwnd, "foreground=0x%X hwnd=0x%X" % (foreground, hwnd)

    if hwnd:
        ok_all &= run_step("foreground", focus_window)

    def switch_views():
        if not _window_present(hwnd):
            return False, "窗口不在了，没法切页"
        for key in "123456789":
            user32.keybd_event(0x11, 0, 0, 0)                  # Ctrl down
            user32.keybd_event(ord(key), 0, 0, 0)
            user32.keybd_event(ord(key), 0, 2, 0)
            user32.keybd_event(0x11, 0, 2, 0)
            time.sleep(0.2)
            if proc.poll() is not None:
                return False, "发到 Ctrl+%s 时进程死了，退出码 %s" % (key, proc.returncode)
        return True, "已发 Ctrl+1..9，进程存活"

    ok_all &= run_step("switch-views", switch_views)

    def still_alive():
        alive = proc.poll() is None
        present = _window_present(hwnd)
        return alive and present, "进程存活=%s 窗口还在=%s 退出码=%s" % (
            alive, present, proc.poll())

    ok_all &= run_step("still-alive", still_alive)

    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        proc.kill()
    payload = _payload(out, "exe", ok_all)
    payload["exe"] = str(exe)
    payload["self_test_fail"] = bool(self_test_fail)
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
    parser.add_argument("--self-test-fail", action="store_true",
                        help="自测：故意让 window-exists 失败（标题写错），退出码必须是 1")
    args = parser.parse_args(argv)

    if not IS_WINDOWS:
        print("这条冒烟只对 Windows 有意义（platform=%s）" % sys.platform)
        return 2
    version = os.environ.get("DJB_SMOKE_VERSION", "0.3.9")
    out = args.out or Path("win_smoke_%s.json" % version)
    if args.mode == "exe":
        if not args.exe:
            parser.error("--mode exe 需要 --exe")
        return smoke_exe(args.exe, out, self_test_fail=args.self_test_fail)
    return smoke_inproc(out, args.views or [], self_test_fail=args.self_test_fail)


if __name__ == "__main__":
    sys.exit(main())
