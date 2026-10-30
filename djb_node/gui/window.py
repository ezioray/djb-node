# SPDX-License-Identifier: Apache-2.0
"""S3 共享偏好窗口（tkinter 最小实现；无第三方依赖）。

* :class:`ShareWindow` 只负责「设置 → View → Backend」与保存 intent，不落盘；
* :class:`TkBackend` 是唯一 import tkinter 的地方，且延迟到实例化；
* 无图形环境时 :func:`main` 返回 ``EXIT_HEADLESS``(3)，不抛 traceback。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Callable, Protocol, Sequence, runtime_checkable

from .share import ShareSettings, ShareView, build_share_view

__all__ = ["EXIT_HEADLESS", "EXIT_OK", "HeadlessError", "ShareBackend", "ShareWindow",
           "TkBackend", "main"]

EXIT_OK = 0
EXIT_HEADLESS = 3
HEADLESS_HINT = "共享设置窗口需要图形环境（tkinter + 桌面会话），当前无法启动，已跳过"


class HeadlessError(RuntimeError):
    """无图形环境（无 tkinter / 无 DISPLAY / 窗口打不开）。"""


@runtime_checkable
class ShareBackend(Protocol):
    """渲染后端协议；测试注入替身即可断言，无需创建 Tk。"""

    def set_handlers(self, *, on_change: Callable[[], None],
                     on_save: Callable[[], None]) -> None: ...
    def current_settings(self) -> ShareSettings: ...
    def render(self, view: ShareView) -> None: ...
    def run(self) -> None: ...
    def stop(self) -> None: ...


class ShareWindow:
    """共享偏好窗口逻辑：设置 → View → Backend；保存只发 intent。"""

    def __init__(self, backend: ShareBackend, settings: ShareSettings, *,
                 save_sink: Callable[[ShareSettings], None],
                 network_ok: bool, upload_mbps: float,
                 min_upload_mbps: float = 50.0,
                 estimates: dict[str, str] | None = None) -> None:
        self._backend = backend
        self._settings = settings
        self._save_sink = save_sink
        self._network_ok = bool(network_ok)
        self._upload_mbps = float(upload_mbps)
        self._min_upload_mbps = float(min_upload_mbps)
        self._estimates = dict(estimates or {})
        self._advanced = False
        backend.set_handlers(on_change=self._changed, on_save=self.save)
        self.refresh()

    @property
    def settings(self) -> ShareSettings:
        return self._settings

    def _changed(self) -> None:
        self._settings = self._backend.current_settings()
        self.refresh()

    def toggle_advanced(self) -> None:
        self._advanced = not self._advanced
        self.refresh()

    def view(self) -> ShareView:
        view = build_share_view(
            self._settings, network_ok=self._network_ok,
            upload_mbps=self._upload_mbps, min_upload_mbps=self._min_upload_mbps,
            estimates=self._estimates, advanced=self._advanced)
        try:
            self._settings.validate()
        except ValueError as exc:
            return replace(view, warnings=(str(exc),) + view.warnings,
                           save_enabled=False)
        return view

    def refresh(self) -> None:
        self._backend.render(self.view())

    def save(self) -> None:
        """校验后只把设置交给 sink；不写 store、不改 daemon。"""
        self._settings.validate()
        self._save_sink(self._settings)

    def run(self) -> None:
        self._backend.run()

    def stop(self) -> None:
        self._backend.stop()


class TkBackend:
    """tkinter 渲染：只有本类 import tkinter；控件状态与 :class:`ShareSettings` 同步。"""

    def __init__(self, settings: ShareSettings) -> None:
        try:
            import tkinter as tk
            from tkinter import ttk
        except Exception as exc:  # pragma: no cover - 平台相关
            raise HeadlessError(str(exc)) from exc
        self._tk = tk
        try:
            self._root = tk.Tk()
        except Exception as exc:  # pragma: no cover - 无 DISPLAY
            raise HeadlessError(str(exc)) from exc
        self._root.title("DJB 联脑 · 共享设置")
        self._root.geometry("560x420")
        self._settings = settings
        self._preset = tk.StringVar(value=settings.preset)
        self._realtime = tk.BooleanVar(value=settings.realtime)
        self._batch = tk.BooleanVar(value=settings.batch)
        self._network = tk.BooleanVar(value=settings.network)
        self._on_change: Callable[[], None] = lambda: None
        self._on_save: Callable[[], None] = lambda: None
        self._ttk = ttk
        self._body = ttk.Frame(self._root, padding=12)
        self._body.pack(fill="both", expand=True)

    def set_handlers(self, *, on_change: Callable[[], None],
                     on_save: Callable[[], None]) -> None:
        self._on_change = on_change
        self._on_save = on_save

    def current_settings(self) -> ShareSettings:
        return ShareSettings(preset=self._preset.get(),
                             realtime=bool(self._realtime.get()),
                             batch=bool(self._batch.get()),
                             network=bool(self._network.get()))

    def _changed(self) -> None:
        self._settings = self.current_settings()
        self._on_change()

    def render(self, view: ShareView) -> None:
        ttk = self._ttk
        for child in self._body.winfo_children():
            child.destroy()
        ttk.Label(self._body, text=view.title, font=("", 14, "bold")).pack(anchor="w")
        for option in view.options:
            ttk.Radiobutton(self._body, text=f"{option.label}  {option.summary}",
                            value=option.key, variable=self._preset,
                            command=self._changed).pack(anchor="w", pady=2)
        ttk.Separator(self._body).pack(fill="x", pady=8)
        ttk.Checkbutton(self._body, text=view.realtime_label, variable=self._realtime,
                        command=self._changed).pack(anchor="w")
        ttk.Checkbutton(self._body, text=view.batch_label, variable=self._batch,
                        command=self._changed).pack(anchor="w")
        ttk.Checkbutton(self._body, text=f"{view.network_label}  {view.network_badge}",
                        variable=self._network, command=self._changed).pack(anchor="w")
        for line in view.estimates:
            ttk.Label(self._body, text=line).pack(anchor="w", pady=1)
        for warning in view.warnings:
            ttk.Label(self._body, text="⚠️ " + warning, foreground="#b45309").pack(anchor="w")
        ttk.Button(self._body, text="保存偏好", command=self._on_save,
                   state=("normal" if view.save_enabled else "disabled")).pack(anchor="e", pady=10)

    def run(self) -> None:
        self._root.mainloop()

    def stop(self) -> None:
        try:
            self._root.destroy()
        except Exception:  # pragma: no cover
            pass


def save_local(settings: ShareSettings, path: Path | None = None) -> Path:
    """原子写本机偏好（不含凭据）；daemon 重载由后续接线完成，不假装已生效。"""
    target = Path(path) if path is not None else Path.home() / ".djb" / "share-preferences.json"
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = json.dumps({
        "preset": settings.preset, "realtime": settings.realtime,
        "batch": settings.batch, "network": settings.network,
        "price_mode": settings.price_mode,
    }, ensure_ascii=False, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=".share-", dir=str(target.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return target


def main(argv: Sequence[str] | None = None) -> int:
    """打开共享设置窗口；无图形环境返回 3 并打印一行说明。"""
    parser = argparse.ArgumentParser(prog="djb gui", description="S3 共享设置窗口")
    parser.add_argument("--preset", choices=("saver", "balanced", "full"), default="balanced")
    parser.add_argument("--no-realtime", action="store_true")
    parser.add_argument("--no-batch", action="store_true")
    parser.add_argument("--network", action="store_true", help="开启组网参与（默认关）")
    parser.add_argument("--upload-mbps", type=float, default=0.0, help="实测上行 Mbps")
    args = parser.parse_args(list(argv) if argv is not None else None)
    settings = ShareSettings(preset=args.preset, realtime=not args.no_realtime,
                             batch=not args.no_batch, network=args.network)
    try:
        settings.validate()
    except ValueError as exc:
        print(f"参数不合法：{exc}", file=sys.stderr)
        return 2
    try:
        backend = TkBackend(settings)
    except HeadlessError as exc:
        print(f"{HEADLESS_HINT}（{exc}）")
        return EXIT_HEADLESS
    def _save(value: ShareSettings) -> None:
        target = save_local(value)
        print(f"已保存共享偏好：{target}（daemon 重载待接线）")

    window = ShareWindow(backend, settings, save_sink=_save,
                         network_ok=args.upload_mbps >= 50.0,
                         upload_mbps=args.upload_mbps)
    window.run()
    return EXIT_OK
