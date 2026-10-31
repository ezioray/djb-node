# SPDX-License-Identifier: Apache-2.0
"""S3 共享偏好窗口（tkinter 最小实现；无第三方依赖）。

* :class:`ShareWindow` 只负责「设置 → View → Backend」与保存 intent，不落盘；
* :class:`TkBackend` 是唯一 import tkinter 的地方，且延迟到实例化；
* 无图形环境时 :func:`main` 返回 ``EXIT_HEADLESS``(3)，不抛 traceback。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
from dataclasses import asdict, replace
from pathlib import Path
from typing import Callable, Protocol, Sequence, runtime_checkable

from .share import ShareSettings, ShareView, build_share_view
from .theme import apply_theme

__all__ = ["EXIT_HEADLESS", "EXIT_OK", "HeadlessError", "ShareBackend", "ShareWindow",
           "TkBackend", "main"]

DRAFT_NOTICE = "草稿，尚未生效；不会修改运行中的共享规则。"

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
        self.status_message = DRAFT_NOTICE
        backend.set_handlers(on_change=self._changed, on_save=self.save)
        self.refresh()

    @property
    def settings(self) -> ShareSettings:
        return self._settings

    def _changed(self) -> None:
        self._settings = self._backend.current_settings()
        self.status_message = "有未保存的草稿更改。" + DRAFT_NOTICE
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
        feedback = getattr(self._backend, "set_status", None)
        if callable(feedback):
            feedback(self.status_message)

    def save(self) -> None:
        """校验后只把设置交给 sink；不写 store、不改 daemon。"""
        try:
            self._settings = self._backend.current_settings()
            self._settings.validate()
            self._save_sink(self._settings)
        except Exception:
            self.status_message = "草稿保存失败或回读未确认；请检查目录权限后重试。尚未生效。"
        else:
            self.status_message = "草稿已保存。" + DRAFT_NOTICE
        self.refresh()

    def run(self) -> None:
        self._backend.run()

    def stop(self) -> None:
        self._backend.stop()


class TkBackend:
    """Stable widgets: projection updates never destroy keyboard focus."""

    def __init__(self, settings: ShareSettings) -> None:
        try:
            import tkinter as tk
            from tkinter import ttk
            self._root = tk.Tk()
        except Exception as exc:
            raise HeadlessError(type(exc).__name__) from None
        self._tk, self._ttk, self._settings = tk, ttk, settings
        self._root.title("DJB / NIGHT SIGNAL · 共享草稿")
        self._root.geometry("840x850")
        self._root.minsize(700, 700)
        apply_theme(self._root, ttk)
        self._preset = tk.StringVar(value=settings.preset)
        self._realtime = tk.BooleanVar(value=settings.realtime)
        self._batch = tk.BooleanVar(value=settings.batch)
        self._network = tk.BooleanVar(value=settings.network)
        self._on_change = lambda: None
        self._on_save = lambda: None
        self._body = ttk.Frame(self._root, padding=24)
        self._body.pack(fill="both", expand=True, padx=2, pady=2)
        ttk.Label(self._body, text="◢ DJB / NIGHT SIGNAL     // LOCAL DRAFT",
                  style="Signal.TLabel").pack(anchor="w")
        ttk.Label(self._body, text="你想怎么共享？", font=("Microsoft YaHei UI", 22, "bold")).pack(anchor="w", pady=(12, 4))
        ttk.Label(self._body, text="最终有效规则 = 本地规则与平台策略中更严格者", style="Muted.TLabel").pack(anchor="w")
        self._notice = ttk.Label(self._body, text=DRAFT_NOTICE, style="Warning.TLabel", wraplength=740)
        self._notice.pack(fill="x", pady=12)
        ttk.Label(self._body, text="01 / 共享预设 · 仅编辑意向", style="Signal.TLabel").pack(anchor="w")
        self._options = {}
        for key in ("saver", "balanced", "full"):
            button = ttk.Radiobutton(self._body, variable=self._preset, value=key,
                                     command=self._changed, takefocus=True)
            button.pack(fill="x", pady=2)
            self._options[key] = button
        ttk.Separator(self._body).pack(fill="x", pady=12)
        ttk.Label(self._body, text="02 / 能力草稿 · 不是运行状态", style="Signal.TLabel").pack(anchor="w")
        self._checks = []
        for variable in (self._realtime, self._batch, self._network):
            button = ttk.Checkbutton(self._body, variable=variable, command=self._changed, takefocus=True)
            button.pack(fill="x")
            self._checks.append(button)
        self._estimates = ttk.Label(self._body, style="Muted.TLabel", wraplength=740)
        self._estimates.pack(anchor="w", pady=8)
        self._warnings = ttk.Label(self._body, style="Warning.TLabel", wraplength=740)
        self._warnings.pack(anchor="w")
        self._details = ttk.Label(self._body, style="Muted.TLabel", wraplength=740)
        self._details.pack(fill="x", pady=6)
        footer = ttk.Frame(self._body)
        footer.pack(fill="x", side="bottom", pady=(12, 0))
        self._save_button = ttk.Button(footer, text="保存草稿 / Ctrl+S", command=lambda: self._on_save(), takefocus=True)
        self._save_button.pack(side="right")
        ttk.Button(footer, text="关闭 / Esc", command=self.stop, takefocus=True).pack(side="left")
        self._root.bind("<Control-s>", self._save_shortcut)
        self._root.bind("<Command-s>", self._save_shortcut)
        self._root.bind("<Escape>", lambda _e: self.stop())
        self._options[settings.preset].focus_set()

    def _save_shortcut(self, _event=None):
        if "disabled" not in self._save_button.state():
            self._on_save()
        return "break"

    def set_handlers(self, *, on_change, on_save) -> None:
        self._on_change, self._on_save = on_change, on_save

    def current_settings(self) -> ShareSettings:
        return replace(self._settings, preset=self._preset.get(),
                       realtime=bool(self._realtime.get()), batch=bool(self._batch.get()),
                       network=bool(self._network.get()))

    def _changed(self) -> None:
        self._settings = self.current_settings()
        self._on_change()

    def set_status(self, text: str) -> None:
        self._notice.configure(text=text)

    def set_draft_source(self, path: Path, message: str) -> None:
        self._details.configure(text="03 / 本地证据\n" + message + "\n草稿路径：" + str(path)
                                + "\n价格模式保留原值；不上传凭据，不调用 daemon reload。")

    def render(self, view: ShareView) -> None:
        for option in view.options:
            self._options[option.key].configure(text=f"{option.label}  /  {option.summary}")
        for button, label in zip(self._checks, (view.realtime_label, view.batch_label,
                                               view.network_label + "  " + view.network_badge)):
            button.configure(text="草稿 · " + label)
        self._estimates.configure(text="收益预估 / " + "  ·  ".join(view.estimates)
                                  + "\n— 表示无服务端数据，不代表零收益。")
        self._warnings.configure(text="\n".join(view.warnings)
                                 + "\n组网仅记录意向；未接运行时，不会开启组网。")
        self._save_button.state(["!disabled"] if view.save_enabled else ["disabled"])

    def run(self) -> None:
        self._root.mainloop()

    def stop(self) -> None:
        try:
            self._root.destroy()
        except Exception:
            pass


def draft_path(home: str | None = None) -> Path:
    # Keep historical default; explicit --home / DJB_HOME is honored.
    return Path(home or os.environ.get("DJB_HOME") or (Path.home() / ".djb")).expanduser() / "share-preferences.json"


def load_local(path: Path | None = None) -> ShareSettings:
    target = Path(path) if path is not None else draft_path()
    if target.stat().st_size > 16384:
        raise ValueError("草稿过大")
    data = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("草稿格式错误")
    values = {key: data[key] for key in asdict(ShareSettings()) if key in data}
    for key in ("realtime", "batch", "network"):
        if key in values and type(values[key]) is not bool:
            raise ValueError("草稿开关类型错误")
    for key in ("preset", "price_mode"):
        if key in values and not isinstance(values[key], str):
            raise ValueError("草稿文本类型错误")
    settings = ShareSettings(**values)
    settings.validate()
    return settings


def save_local(settings: ShareSettings, path: Path | None = None) -> Path:
    """原子写本机偏好（不含凭据）；daemon 重载由后续接线完成，不假装已生效。"""
    settings.validate()
    target = Path(path) if path is not None else draft_path()
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
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    if load_local(target) != settings:
        raise ValueError("草稿回读不一致")
    return target


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="djb gui", description="共享偏好草稿（尚未生效）")
    parser.add_argument("--home", help="本机草稿目录；不会修改运行配置")
    parser.add_argument("--preset", choices=("saver", "balanced", "full"), default=None)
    parser.add_argument("--no-realtime", action="store_true")
    parser.add_argument("--no-batch", action="store_true")
    parser.add_argument("--network", action="store_true", help="记录组网草稿意向（不实际开启）")
    parser.add_argument("--upload-mbps", type=float, default=0.0, help="手动输入上行 Mbps，非实时检测")
    args = parser.parse_args(list(argv) if argv is not None else None)
    path = draft_path(args.home)
    source_message = "未找到本地草稿，使用编辑默认值；不是运行配置。"
    blocked = False
    try:
        settings = load_local(path)
        source_message = "已回读本地草稿；不是运行配置。"
    except FileNotFoundError:
        settings = ShareSettings()
    except (OSError, ValueError, TypeError):
        settings = ShareSettings()
        source_message = "原草稿不可读，已阻止覆盖。请先备份并修复草稿，再重新打开。"
        blocked = True
    settings = replace(settings, preset=args.preset or settings.preset,
                       realtime=False if args.no_realtime else settings.realtime,
                       batch=False if args.no_batch else settings.batch,
                       network=True if args.network else settings.network)
    try:
        settings.validate()
        if not math.isfinite(args.upload_mbps) or args.upload_mbps < 0:
            raise ValueError("上行 Mbps 必须为非负有限数")
    except ValueError as exc:
        print(f"参数不合法：{exc}", file=sys.stderr)
        return 2
    try:
        backend = TkBackend(settings)
    except HeadlessError:
        print(HEADLESS_HINT)
        return EXIT_HEADLESS
    if hasattr(backend, "set_draft_source"):
        backend.set_draft_source(path, source_message)

    def _save(value: ShareSettings) -> None:
        if blocked:
            raise ValueError("原草稿不可读，禁止覆盖")
        save_local(value, path)
        print("草稿已保存并回读，尚未生效；未调用 daemon reload。")

    window = ShareWindow(backend, settings, save_sink=_save,
                         network_ok=args.upload_mbps >= 50.0, upload_mbps=args.upload_mbps)
    window.run()
    return EXIT_OK
