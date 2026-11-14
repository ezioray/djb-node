# SPDX-License-Identifier: Apache-2.0
"""C3(Windows) / C4(macOS) 托盘壳：把 :mod:`djb_node.tray.model` 渲染成托盘 / 菜单栏，**只发 intent**。

设计边界
========

* **零第三方依赖**：只用标准库 ``tkinter`` + 本包已有模块；不引入 pystray / rumps / PyQt / wx。
  图标用 ``PhotoImage`` 现画 16×16 纯色块（``iconphoto``），**不下载任何图片资源**。
* **壳不含口径**：六项菜单、暂停/恢复二选一、角标、缺字段「—」全部由
  :meth:`~djb_node.tray.model.TrayModel.build` 与
  :func:`~djb_node.tray.notifications.summarize` 决定；本模块只做
  「取数 → 建模型 → 交给后端渲染」，不读 ``status`` 字段、不算金额、不数未读。
* **不执行动作**：点击只把 :class:`~djb_node.tray.model.TrayIntent` 交给 ``intent_sink``
  （默认把建议命令写进结构化事件日志 + 终端），壳不跑命令、不改 daemon 状态；
  只有 ``QUIT`` 会停止事件循环。
* **GUI 后端可注入**：:class:`Backend` 是七方法协议，tkinter 实现是 :class:`TkBackend`。
  测试注入替身即可断言全部渲染行为，**不需要创建 Tk**，因此无头 CI 也能跑。
* **无主窗口**：macOS 走 ``withdraw()`` + 菜单栏菜单（``LSUIElement`` 由打包脚本设，不出 Dock 图标）；
  Windows / Linux 走 ``iconify()`` + 右键弹出同一份菜单，关窗只收回不退出。
* **无头降级**：没有 tkinter 或没有图形会话时 :func:`main` 打印一行中文说明并返回
  ``EXIT_HEADLESS``(3)，**不抛 traceback**（``import tkinter`` 延迟到函数内）。

数据来源（只读）
================

:func:`main` 的 status 走 ``djb status --json`` 的等价只读路径：子进程
``[sys.executable, "-m", "djb_node", "status", "--json"]``，任何失败（非零退出 / 超时 /
非 JSON）一律降级 ``{"state": "stopped"}``，由模型保守显示「已停止」——不给假「运行中」。
消息同理走只读 ``djb notify list --json``，只取标题口径（正文留在本地库）。
壳自己**不开库、不联网、不写状态**。

刷新节奏
========

:meth:`TrayApp.run` 先同步刷一次，再 ``Backend.after(interval_ms, ...)`` 自循环，
默认 5000ms、可注入。渲染顺序固定 title → menu items → badge，便于快照断言。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable

from ..errors import UsageError
from ..platform.no_window import subprocess_window_kwargs
from ..logging_setup import event
from .model import TrayAction, TrayIntent, TrayMenuItem, TrayModel
from .notifications import FETCH_LIMIT, summarize
from ..gui.theme import COLORS, menu_options
from .actions import apply_control, control_enabled
from .panels import DesktopPanel, project_pages

__all__ = [
    "Backend",
    "HeadlessError",
    "TkBackend",
    "TrayApp",
    "cli_notifications",
    "cli_status",
    "main",
    "CLI_TIMEOUT_S",
    "DEFAULT_INTERVAL_MS",
    "DEFAULT_TITLE",
    "EXIT_HEADLESS",
    "EXIT_OK",
    "HEADLESS_HINT",
]

#: 周期刷新间隔（毫秒）
DEFAULT_INTERVAL_MS = 5000
#: 窗口 / 菜单栏标题前缀
DEFAULT_TITLE = "DJB 联脑"
#: 只读 CLI 子进程超时（秒）：cli 内部 status 调用 5s，留出解释器启动余量
CLI_TIMEOUT_S = 8.0
#: 正常退出
EXIT_OK = 0
#: 无图形环境（与 facts 的退出码表不冲突：3 只在托盘壳入口使用）
EXIT_HEADLESS = 3
#: 无头降级时打印的唯一一行说明（前面拼原因）
HEADLESS_HINT = "托盘壳需要图形环境（tkinter + 桌面会话），当前无法启动，已跳过"


class HeadlessError(RuntimeError):
    """无图形环境（无 DISPLAY / 无 tkinter / 窗口打不开）：``main`` 据此返回 3 而不是抛栈。"""


@runtime_checkable
class Backend(Protocol):
    """GUI 后端协议：``TrayApp`` 只通过这些方法碰界面，因此替身可在无 Tk 环境下测全流程。"""

    def set_activate_handler(self, handler: Callable[[str], None]) -> None:
        """登记点击回调（参数是菜单项 ``key``，路由由 :meth:`TrayApp.activate` 负责）。"""

    def set_title(self, text: str) -> None:
        """窗口 / 菜单栏标题（壳传 :meth:`TrayModel.status_line`）。"""

    def set_menu_items(self, items: Sequence[TrayMenuItem]) -> None:
        """整份菜单（顺序即展示顺序；禁用的项必须置灰，不能隐藏）。"""

    def set_badge(self, text: str | None) -> None:
        """角标（壳传消息项的 badge，``None`` = 清除）。"""

    def after(self, delay_ms: int, callback: Callable[[], None]) -> Any:
        """延迟回调（对应 tkinter ``after``）；返回句柄，壳不解释。"""

    def run(self) -> None:
        """阻塞跑事件循环，直到 :meth:`stop`。"""

    def stop(self) -> None:
        """停止事件循环（幂等）。"""


class TrayApp:
    """托盘壳逻辑：``provider → TrayModel → Backend``，点击只发 intent。

    * provider 都是旁路：抛异常时降级为空输入（模型自身会保守显示「已停止」「—」），壳不崩；
    * 渲染顺序固定 title → items → badge；
    * 只有 ``QUIT`` 停止事件循环，其余动作只交给 ``intent_sink``（不执行任何命令）；
    * 周期刷新走 :meth:`Backend.after`，默认 5000ms，可注入。
    """

    def __init__(self, backend: Backend,
                 status_provider: Callable[[], Mapping[str, Any] | None],
                 notifications_provider: Callable[[], Sequence[Mapping[str, Any]] | None],
                 intent_sink: Callable[[TrayIntent], None], *,
                 interval_ms: int = DEFAULT_INTERVAL_MS,
                 bus: Any | None = None) -> None:
        if isinstance(interval_ms, bool) or not isinstance(interval_ms, int) or interval_ms <= 0:
            raise UsageError("interval_ms 必须是正整数（毫秒）")
        self._backend = backend
        self._status_provider = status_provider
        self._notifications_provider = notifications_provider
        self._intent_sink = intent_sink
        self._interval_ms = int(interval_ms)
        self._status_known = False
        self._notifications_known = False
        self._model: TrayModel | None = None
        self._timer: Any = None
        self._closed = False
        #: 与主窗口共享的暂停/让路状态总线（可选；None = 只做托盘自己的刷新）
        self._bus = bus
        self._refreshing = False
        self._unsubscribe: Callable[[], None] | None = None
        if bus is not None:
            self._unsubscribe = bus.subscribe(self._on_external_state)
        backend.set_activate_handler(self.activate)

    # ─────────────────────────────────────────────────── 状态查询
    @property
    def model(self) -> TrayModel | None:
        """最近一次刷新得到的模型（未刷新过为 ``None``）。"""
        return self._model

    @property
    def interval_ms(self) -> int:
        return self._interval_ms

    @property
    def closed(self) -> bool:
        return self._closed

    # ─────────────────────────────────────────────────── 采集 / 渲染
    def refresh(self) -> TrayModel:
        """拉一次 provider 并渲染（唯一写界面的入口；顺序固定 title → items → badge）。"""
        status = self._collect(self._status_provider, {})
        # Legacy model still consumes an empty fallback; UI keeps failure metadata.
        unavailable = object()
        notification_result = self._collect(self._notifications_provider, unavailable)
        notifications = () if notification_result is unavailable else notification_result
        notification_context = {"known": False} if notification_result is unavailable else notification_result
        model = TrayModel.build(status if isinstance(status, Mapping) else {},
                                self._summary(notifications))
        self._model = model
        context = getattr(self._backend, "set_context", None)
        if callable(context):
            context(status, notification_context)
        self._backend.set_title(model.status_line())
        self._backend.set_menu_items(model.items)
        self._backend.set_badge(self._badge_of(model))
        self._publish_state(status)
        return model

    def _publish_state(self, status: Any) -> None:
        """把本次回读结果广播给主窗口（暂停/让路一致性）；失败只记事件，不拖垮托盘。"""
        bus = self._bus
        if bus is None or not isinstance(status, Mapping):
            return
        self._refreshing = True
        try:
            bus.publish_status(status, source="tray")
        except Exception as exc:
            event("tray.bus_publish_failed", level="warning", error=type(exc).__name__)
        finally:
            self._refreshing = False

    def _on_external_state(self, _snapshot: Any) -> None:
        """别的界面（主窗口）回读到新状态：立即重画菜单，不再等下一个 5s 周期。"""
        if self._closed or self._refreshing:
            return
        try:
            self.refresh()
        except Exception as exc:                       # 托盘是旁路，刷新失败不能崩
            event("tray.bus_refresh_failed", level="warning", error=type(exc).__name__)

    def _collect(self, provider: Callable[[], Any], default: Any) -> Any:
        """旁路采集：provider 抛异常时降级为 ``default``（托盘不因 status / 消息库异常停）。"""
        try:
            value = provider()
        except Exception as exc:                       # 托盘是旁路：provider 崩了也不能拖垮壳
            event("tray.provider_failed", level="warning",
                  error=type(exc).__name__)
            return default
        return default if value is None else value

    @staticmethod
    def _summary(value: Any) -> Mapping[str, Any]:
        """通知投影：序列 → :func:`summarize`；已是投影的映射原样用；其余一律按 0 处理。"""
        if isinstance(value, Mapping):
            return value
        return summarize(_rows(value))

    @staticmethod
    def _badge_of(model: TrayModel) -> str | None:
        """角标直接取模型的「消息中心」项，壳不重新计数。"""
        item = model.item("messages")
        return item.badge if item is not None else None

    # ─────────────────────────────────────────────────── 点击 → intent
    def activate(self, key: str) -> TrayIntent | None:
        """点击路由：只发 intent；禁用项 / 未知 key / 已关闭 → ``None``（不发任何动作）。"""
        if self._closed or self._model is None:
            return None
        intent = self._model.intent_for(str(key or ""))
        if intent is None:
            event("tray.intent_skipped", reason="item_unavailable", item=str(key or ""))
            return None
        try:
            self._intent_sink(intent)
        except Exception as exc:                       # sink 写日志失败也不能让托盘崩
            event("tray.intent_sink_failed", level="warning", error=type(exc).__name__)
        if intent.action is TrayAction.QUIT:
            self.close()
        return intent

    # ─────────────────────────────────────────────────── 事件循环
    def run(self) -> None:
        """先同步渲染一次，再挂周期刷新，最后进后端事件循环（阻塞）。"""
        self.refresh()
        self._schedule()
        self._backend.run()

    def close(self) -> None:
        """停止事件循环（幂等）：之后的定时器回调一律空转，不会再刷界面。"""
        if self._closed:
            return
        self._closed = True
        self._timer = None
        if self._unsubscribe is not None:
            try:
                self._unsubscribe()
            except Exception:
                pass
            self._unsubscribe = None
        self._backend.stop()

    def _schedule(self) -> None:
        if self._closed:
            return
        self._timer = self._backend.after(self._interval_ms, self._tick)

    def _tick(self) -> None:
        if self._closed:
            return
        self.refresh()
        self._schedule()


def _menu_label(item: TrayMenuItem) -> str:
    """菜单项文案：角标只作展示后缀（无角标不加括号）。"""
    return item.label + (" (" + item.badge + ")" if item.badge else "")


class TkBackend:
    """tkinter 后端：无主窗口（macOS 菜单栏 / Windows·Linux 最小化窗口 + 右键菜单）。

    ``tk`` 可注入（默认延迟 import），因此「窗口打不开」的分支也能在无 Tk 环境下测。
    """

    def __init__(self, *, tk: Any | None = None, title: str = DEFAULT_TITLE) -> None:
        self._tk = tk if tk is not None else _import_tk()
        try:
            self._root = self._tk.Tk()
        except Exception as exc:                       # TclError：没有 DISPLAY / 会话不可用
            raise HeadlessError("窗口创建失败：" + str(exc)) from None
        self._root.withdraw()                          # 先隐藏：不给用户一个空白主窗口
        self._title = str(title)
        self._badge: str | None = None
        self._handler: Callable[[str], None] | None = None
        self._menu = self._tk.Menu(self._root, tearoff=0, **menu_options())
        self._context = ({}, ())
        self._panel = None
        self._refresh_handler = lambda: None
        self._control_enabled = lambda: False
        self._root.configure(background=COLORS["bg"])
        self._root.bind("<F10>", self._keyboard_popup)
        self._root.bind("<Shift-F10>", self._keyboard_popup)
        self._root.bind("<Escape>", lambda _e: self._hide())
        self._icon = self._make_icon()
        self._attach()
        self.set_title(self._title)

    # ─────────────────────────────────────────────────── Backend 协议
    def set_activate_handler(self, handler: Callable[[str], None]) -> None:
        self._handler = handler

    def set_title(self, text: str) -> None:
        self._title = str(text)
        self._apply_title()

    def set_badge(self, text: str | None) -> None:
        self._badge = str(text) if text else None
        self._apply_title()

    def set_menu_items(self, items: Sequence[TrayMenuItem]) -> None:
        self._menu.delete(0, "end")
        for item in items:
            self._menu.add_command(
                label=self._visible_label(item), command=self._command(item.key),
                state="normal" if item.enabled else "disabled")

    def after(self, delay_ms: int, callback: Callable[[], None]) -> Any:
        return self._root.after(int(delay_ms), callback)

    def run(self) -> None:
        self._root.mainloop()

    def stop(self) -> None:
        try:
            self._root.quit()
            self._root.destroy()
        except Exception:                              # 已销毁 / 解释器已退出：忽略
            pass

    def set_context(self, status, notifications) -> None:
        self._context = (status, notifications)
        if self._panel is not None:
            self._render_panel()

    def set_refresh_handler(self, handler) -> None:
        self._refresh_handler = handler

    def set_control_enabled(self, enabled) -> None:
        self._control_enabled = enabled

    def _visible_label(self, item):
        label = _menu_label(item)
        status, notifications = self._context
        known = isinstance(status, Mapping) and status.get("state") in (
            "online", "starting", "paused", "updating", "stopped", "error")
        if item.key == "status":
            if not known:
                return "? 状态未知 / 打开只读面板"
            labels = {"online": "运行中", "starting": "启动中", "paused": "已暂停",
                      "updating": "更新中", "stopped": "已停止", "error": "错误"}
            return "状态 / " + labels[status["state"]] + " / 只读面板"
        if item.key == "messages":
            if not isinstance(notifications, (list, tuple)):
                return "! 消息读取失败 / 请刷新"
            return label + (" / P0 重要·静默" if item.badge and item.badge.startswith("!") else " / 只读")
        if item.action in (TrayAction.PAUSE, TrayAction.RESUME):
            return label + (" / 确认后执行并回读" if self._control_enabled() else " / 命令指南·未执行")
        if item.action in (TrayAction.DIAGNOSE, TrayAction.OPEN_LOGS):
            return label + " / 命令指南·未执行"
        if item.action is TrayAction.BIND:
            return label + " / 一次性码"
        return label

    def _render_panel(self):
        status, notifications = self._context
        self._panel.render(project_pages(
            status if isinstance(status, Mapping) else {},
            notifications if isinstance(notifications, (list, tuple)) else None,
            status_known=isinstance(status, Mapping) and status.get("state") in (
                "online", "starting", "paused", "updating", "stopped", "error"),
            notifications_known=isinstance(notifications, (list, tuple))))

    def show_page(self, page):
        if self._panel is None:
            self._panel = DesktopPanel(self._root, self._tk, refresh=self._refresh_handler)
        self._render_panel()
        self._panel.show(page)

    def show_feedback(self, title, message):
        # Nonmodal, silent, no bell or default system messagebox sounds.
        from tkinter import ttk
        window = self._tk.Toplevel(self._root)
        from ..gui.theme import apply_theme
        apply_theme(window, ttk)
        window.title("DJB / " + title)
        window.geometry("640x300")
        body = ttk.Frame(window, padding=24)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="◢ " + title, style="Signal.TLabel").pack(anchor="w")
        ttk.Label(body, text=message, wraplength=580).pack(fill="x", pady=20)
        close = ttk.Button(body, text="关闭 / Esc", command=window.destroy)
        close.pack(anchor="e")
        close.focus_set()
        window.bind("<Escape>", lambda _e: window.destroy())

    def confirm_control(self, message):
        from tkinter import ttk
        from ..gui.theme import apply_theme
        window = self._tk.Toplevel(self._root)
        apply_theme(window, ttk)
        window.title("DJB / 确认本机控制")
        window.geometry("640x300")
        choice = [False]
        body = ttk.Frame(window, padding=24)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="! 影响确认 / 可取消", style="Warning.TLabel").pack(anchor="w")
        ttk.Label(body, text=message, wraplength=580).pack(fill="x", pady=24)
        def accept():
            choice[0] = True
            window.destroy()
        ttk.Button(body, text="确认执行并回读", command=accept).pack(side="right")
        cancel = ttk.Button(body, text="取消 / Esc", command=window.destroy)
        cancel.pack(side="left")
        cancel.focus_set()
        window.bind("<Escape>", lambda _e: window.destroy())
        window.grab_set()
        self._root.wait_window(window)
        return choice[0]

    def ask_bind_code(self) -> str | None:
        """一次性绑定码输入框（模态；取消 / 空串返回 None，不执行任何命令）。"""
        from tkinter import simpledialog
        from .. import facts
        message = ("在网页端 → 账户/节点 → 生成绑定码，然后粘贴到这里。\n\n"
                   + facts.WEB_CONSOLE_URL)
        value = simpledialog.askstring("DJB / 绑定网页端", message, parent=self._root)
        text = str(value).strip() if value else ""
        return text or None

    def _keyboard_popup(self, _event):
        self._menu.tk_popup(self._root.winfo_rootx() + 20, self._root.winfo_rooty() + 20)
        self._menu.grab_release()
        return "break"

    # ─────────────────────────────────────────────────── 窗口细节
    def _attach(self) -> None:
        """把菜单挂到系统菜单栏（macOS）或右键（Windows / Linux）。"""
        if sys.platform == "darwin":
            self._root.config(menu=self._menu)          # 菜单栏；LSUIElement 由打包脚本设
            return
        self._root.iconify()                            # 最小化到任务栏，不占桌面
        self._root.bind("<Button-3>", self._popup)
        self._root.protocol("WM_DELETE_WINDOW", self._hide)   # 关窗 = 收回托盘，不退出

    def _make_icon(self) -> Any:
        """16×16 纯色图标（不下载图片资源；窗口管理器不支持时静默省略）。"""
        icon = self._tk.PhotoImage(width=16, height=16)
        icon.put(COLORS["bg"], to=(0, 0, 16, 16))
        icon.put(COLORS["mint"], to=(2, 4, 5, 14))
        icon.put(COLORS["mint2"], to=(5, 11, 12, 14))
        icon.put(COLORS["text"], to=(10, 2, 14, 10))
        try:
            self._root.iconphoto(True, icon)
        except Exception:                              # 部分 WM 不支持 iconphoto：省略图标
            pass
        return icon

    def _apply_title(self) -> None:
        suffix = " [" + self._badge + "]" if self._badge else ""
        status, notifications = self._context
        if not isinstance(status, Mapping) or status.get("state") not in (
                "online", "starting", "paused", "updating", "stopped", "error"):
            self._root.title("DJB / ? 状态未知 · 请刷新" + suffix)
        else:
            labels = {"online": "运行中", "starting": "启动中", "paused": "已暂停",
                      "updating": "更新中", "stopped": "已停止", "error": "错误"}
            self._root.title("DJB / " + labels[status["state"]] + suffix)

    def _popup(self, event: Any) -> None:
        try:
            self._menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._menu.grab_release()

    def _hide(self) -> None:
        self._root.withdraw()

    def _command(self, key: str) -> Callable[[], None]:
        def _dispatch() -> None:
            handler = self._handler
            if handler is not None:
                handler(key)
        return _dispatch


# ─────────────────────────────────────────────────────────── 延迟导入 / 无头判定
def _import_tk() -> Any:
    """延迟 import tkinter：模块被 import 时（CI / 服务端）绝不碰 GUI。"""
    import tkinter
    return tkinter


def _has_display() -> bool:
    """Windows / macOS 总有图形会话语义；Linux 需要 X11 或 Wayland。"""
    if sys.platform in ("win32", "darwin"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _make_backend(*, title: str) -> TkBackend:
    """创建真实 GUI 后端；无图形会话 / 无 tkinter 一律抛 :class:`HeadlessError`。"""
    if not _has_display():
        raise HeadlessError("没有图形会话（DISPLAY / WAYLAND_DISPLAY 均未设置）")
    try:
        tk = _import_tk()
    except ImportError:
        raise HeadlessError("本机 Python 没有 tkinter（精简镜像常见，装 python3-tk 即可）") \
            from None
    return TkBackend(tk=tk, title=title)


# ─────────────────────────────────────────────────────────── 只读数据来源（子进程）
def _run_cli_json(cli_args: Sequence[str], *, timeout: float = CLI_TIMEOUT_S) -> Any:
    """跑一次**只读** CLI 子命令并解析 JSON；任何失败返回 ``None``（壳不抛、不重试）。"""
    command = [sys.executable, "-m", "djb_node", *cli_args]
    label = " ".join(cli_args)
    # 与桌面端同一条规矩：子进程的 home 必须显式等于本进程解析出的 home
    # （不显式传就会跟着外部环境漂，读到别人的库）
    env = dict(os.environ)
    try:
        from ..config import resolve_home
        env["DJB_HOME"] = str(resolve_home())
    except Exception:                                # pragma: no cover - 配置坏了也别炸壳
        pass
    try:
        # 子进程是我们自己的 CLI（stdout 强制 UTF-8），必须显式按 UTF-8 解；
        # 中文 Windows 上沿用 cp936 会 UnicodeDecodeError（见 desktop/datasource.py）
        completed = subprocess.run(command, capture_output=True,
                                   encoding="utf-8", errors="replace",
                                   timeout=timeout, check=False, env=env,
                                   **subprocess_window_kwargs())
    except (OSError, subprocess.SubprocessError) as exc:
        event("tray.cli_failed", level="warning", command=label,
              error=type(exc).__name__)
        return None
    if completed.returncode != 0:
        event("tray.cli_failed", level="warning", command=label,
              exit_code=completed.returncode)
        return None
    try:
        return json.loads(completed.stdout or "")
    except (TypeError, ValueError):
        event("tray.cli_failed", level="warning", command=label, error="bad_json")
        return None


def cli_status() -> dict[str, Any]:
    """``djb status --json`` 的只读等价采集；失败降级 ``{"state": "stopped"}``。"""
    payload = _run_cli_json(("status", "--json"))
    return dict(payload) if isinstance(payload, Mapping) else {"state": "stopped"}


def cli_notifications() -> tuple[Mapping[str, Any], ...]:
    """``djb notify list --json`` 的只读行（只供标题/计数投影，正文不进入托盘）。"""
    payload = _run_cli_json(("notify", "list", "--json", "--limit", str(FETCH_LIMIT)))
    rows = payload.get("items") if isinstance(payload, Mapping) else payload
    return _rows(rows)


def _rows(value: Any) -> tuple[Mapping[str, Any], ...]:
    """容错投影：字符串 / 映射 / 不可迭代 → 空；非映射元素直接丢（不展开正文）。"""
    if value is None or isinstance(value, (str, bytes, Mapping)):
        return ()
    try:
        items = tuple(value)
    except TypeError:
        return ()
    return tuple(item for item in items if isinstance(item, Mapping))


def desktop_status():
    """Strict UI provider; legacy cli_status fallback remains API-compatible."""
    payload = _run_cli_json(("status", "--json"))
    return dict(payload) if isinstance(payload, Mapping) else {"state": "unknown"}


def _run_cli_command(cli_args: Sequence[str], *,
                     timeout: float = 30.0) -> tuple[int, str, str]:
    """跑一次**会改本机状态**的 CLI（auth --bind）：返回 (rc, stdout, stderr)，不抛异常。"""
    command = [sys.executable, "-m", "djb_node", *cli_args]
    env = dict(os.environ)
    try:
        from ..config import resolve_home
        env["DJB_HOME"] = str(resolve_home())
    except Exception:                                  # 配置坏了也别炸壳
        pass
    try:
        completed = subprocess.run(command, capture_output=True,
                                   encoding="utf-8", errors="replace",
                                   timeout=timeout, check=False, env=env,
                                   stdin=subprocess.DEVNULL,
                                   **subprocess_window_kwargs())
    except (OSError, subprocess.SubprocessError) as exc:
        event("tray.cli_action_failed", level="warning", command=" ".join(cli_args[:2]),
              error=type(exc).__name__)
        return 127, "", type(exc).__name__
    return completed.returncode, completed.stdout or "", completed.stderr or ""


def desktop_notifications():
    payload = _run_cli_json(("notify", "list", "--json", "--limit", str(FETCH_LIMIT)))
    if not isinstance(payload, Mapping) or not isinstance(payload.get("items"), list):
        return {"known": False}
    rows = payload["items"]
    if any(not isinstance(row, Mapping) for row in rows):
        return {"known": False}
    return _rows(rows)


def desktop_intent_sink(backend, *, refresh):
    """Bind real read-only panels and guarded local control; guides are explicit."""
    def enabled():
        # Existing daemon IPC is AF_UNIX/POSIX; do not pretend Windows is supported.
        if sys.platform == "win32":
            return False
        try:
            from ..config import Paths, load_config, resolve_home
            return control_enabled(load_config(Paths(resolve_home())))
        except Exception:
            return False

    if hasattr(backend, "set_control_enabled"):
        backend.set_control_enabled(enabled)

    def sink(intent):
        if intent.action is TrayAction.QUIT:
            return
        if intent.action in (TrayAction.OPEN_STATUS, TrayAction.OPEN_MESSAGES):
            refresh()
            backend.show_page("运行" if intent.action is TrayAction.OPEN_STATUS else "通知")
            event("tray.panel_opened", action=intent.action.value)
            return
        if intent.action in (TrayAction.PAUSE, TrayAction.RESUME) and enabled():
            from ..config import Paths, resolve_home
            from ..daemon import ControlClient
            def call(command, *, timeout):
                return ControlClient(Paths(resolve_home())).call(command, timeout=timeout)
            result = apply_control(intent.action, call=call,
                                   enabled=enabled, confirm=backend.confirm_control)
            refresh()
            backend.show_feedback("本机控制回读", result.message)
            event("tray.control_result", action=intent.action.value, verified=result.verified)
            return
        if intent.action is TrayAction.BIND:
            ask = getattr(backend, "ask_bind_code", None)
            code = ask() if callable(ask) else None
            code = str(code or "").strip()
            if not code:
                backend.show_feedback("绑定网页端 / 已取消",
                                      "没有输入绑定码，未执行任何命令。\n\n"
                                      "在网页端 → 账户/节点 → 生成绑定码。")
                event("tray.bind_cancelled")
                return
            rc, out, err = _run_cli_command(("auth", "--bind", code, "--json"))
            refresh()
            if rc == 0:
                message = "绑定成功：本机已保存节点凭据，daemon 会用新凭据重新上线。"
                ok = True
            else:
                first = next((line.strip() for line in (err or out).splitlines()
                              if line.strip()), "退出码 " + str(rc))
                # 绑定码只进子进程参数：错误消息里若回显，替换掉再上屏
                detail = first.removeprefix("djb: ").replace(code, "***")[:160]
                message = "绑定未成功（退出码 " + str(rc) + "）：" + detail
                ok = False
            backend.show_feedback("绑定网页端 / " + ("成功" if ok else "失败"), message)
            event("tray.bind_result", ok=ok)
            return
        backend.show_feedback("命令指南 / 未执行", "此动作未自动执行。\n\n"
                              + " ".join(intent.command) + "\n\n" + intent.description
                              + ("\nWindows 本机控制 IPC 尚未通过支持验证；不会执行托盘控制。"
                                 if sys.platform == "win32" and intent.action in (TrayAction.PAUSE, TrayAction.RESUME) else "")
                              + "\n执行后请刷新核对实际状态。")
        event("tray.command_guide", action=intent.action.value)
    return sink


# ─────────────────────────────────────────────────────────── 默认 sink / 日志 / 入口
def _default_intent_sink(intent: TrayIntent) -> None:
    """默认 intent_sink：把建议命令写日志和终端，**不执行**（危险动作交给用户或 CLI）。"""
    command = " ".join(intent.command) if intent.command else "（无命令）"
    event("tray.intent", action=intent.action.value, command=command,
          requires_daemon=intent.requires_daemon, description=intent.description)
    stream = sys.stdout
    if stream is not None:                             # Windows pythonw 没有控制台
        print("托盘动作 " + intent.action.value + "：建议命令 " + command
              + "（" + intent.description + "；壳不自动执行）", file=stream, flush=True)


def _setup_logging() -> None:
    """尽力把托盘事件写进客户端日志（与 CLI 同一文件）；失败不影响起停。"""
    try:
        from ..config import Paths, load_config, resolve_home
        from ..logging_setup import setup_logging
        paths = Paths(resolve_home())
        setup_logging(paths.log_file, level=load_config(paths).log_level)
    except Exception as exc:                           # 日志只是旁路：失败不影响起停
        event("tray.logging_setup_failed", level="warning", error=type(exc).__name__)


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("必须是正整数") from None
    if number <= 0:
        raise argparse.ArgumentTypeError("必须是正整数")
    return number


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="djb-tray",
        description="DJB 联脑托盘壳（Windows / macOS）：只发动作建议，不执行命令")
    parser.add_argument("--interval-ms", type=_positive_int, default=DEFAULT_INTERVAL_MS,
                        help="刷新间隔（毫秒，默认 " + str(DEFAULT_INTERVAL_MS) + "）")
    parser.add_argument("--title", default=DEFAULT_TITLE, help="窗口 / 菜单栏标题前缀")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Sequence[str] | None = None) -> int:
    """真实入口：无头环境打印一行中文说明并返回 3；否则进事件循环，正常退出返回 0。"""
    args = _parse_args(argv)
    try:
        backend: Backend = _make_backend(title=args.title)
    except HeadlessError as exc:
        print(HEADLESS_HINT + "：" + str(exc))
        return EXIT_HEADLESS
    _setup_logging()
    # Old injected backends preserve the seven-method protocol and intent-only contract.
    interactive = callable(getattr(backend, "show_page", None))
    app = TrayApp(backend, desktop_status if interactive else cli_status,
                  desktop_notifications if interactive else cli_notifications,
                  _default_intent_sink, interval_ms=args.interval_ms)
    if interactive:
        backend.set_refresh_handler(app.refresh)
        app._intent_sink = desktop_intent_sink(backend, refresh=app.refresh)
    try:
        app.run()
    except KeyboardInterrupt:                          # 从终端启动时 Ctrl-C = 退出托盘
        app.close()
    return EXIT_OK


if __name__ == "__main__":                             # pragma: no cover - 入口
    raise SystemExit(main())
