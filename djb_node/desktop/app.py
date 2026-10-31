# SPDX-License-Identifier: Apache-2.0
"""桌面主窗口（Tk）：侧栏 12 视图 + 内容区 + 状态栏；逻辑全在 controller。

约束
====

* **纯 stdlib**：只用 ``tkinter`` / ``ttk``；``tkinter`` 延迟到实例化才 import，
  因此没有图形环境的机器上 import 本模块、跑逻辑测试都不受影响。
* **无头语义不变**：窗口打不开 → :class:`~djb_node.gui.window.HeadlessError` →
  :func:`main` 返回 ``EXIT_HEADLESS``(3)，不打印 traceback。
* **颜色只来自主题**：所有样式名在 :func:`djb_node.gui.theme.apply_theme` 里定义，
  本文件不写十六进制。
* **不执行危险动作**：暂停/恢复要二次确认并回读；保存走 controller 的校验+回读；
  云端没接线的按钮置灰并写原因，点了也只回一句「暂不可用」。
* **可截图**：:meth:`DesktopApp.show_view` 只切视图并 ``update_idletasks``，
  外部脚本可用 ``import -window`` 逐视图取证（见 ``scripts/shot-desktop.py``）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Mapping, Sequence

from .access import ACCESS_FILE, RELOAD_DAEMON_DOWN, RELOAD_FAILED, RELOAD_OK, RELOAD_UNSUPPORTED
from .access import AccessService, FileAccessStore
from .controller import DEFAULT_MESSAGE, DesktopController, LocalControlChannel
from .datasource import FakeDataSource, LocalDataSource, ProviderError, Snapshot
from .views import VIEW_KEYS, VIEW_TITLES, Action, Card, Field, Row, Table, View

__all__ = ["DesktopApp", "EXIT_HEADLESS", "EXIT_OK", "HEADLESS_HINT", "HeadlessError",
           "demo_snapshot", "main", "make_controller"]

#: 正常退出
EXIT_OK = 0
#: 无图形环境（与托盘 / gui.window 同一语义）
EXIT_HEADLESS = 3
HEADLESS_HINT = "桌面客户端需要图形环境（tkinter + 桌面会话），当前无法启动，已跳过"

TOP_NAV_KEYS = VIEW_KEYS


class HeadlessError(RuntimeError):
    """无图形环境：``main`` 据此返回 3 而不是抛栈。"""


def _import_tk():
    import tkinter
    return tkinter


def _has_display() -> bool:
    if sys.platform in ("win32", "darwin"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


class DesktopApp:
    """主窗口：一个 sidebar + 一个内容区（视图切换只重画内容区，不重建侧栏）。"""

    def __init__(self, controller: DesktopController, *, root: Any = None,
                 tk: Any = None, title: str = "DJB 联脑 · 桌面控制台",
                 bus: Any | None = None, on_quit: Any = None) -> None:
        self._tk = tk if tk is not None else _import_tk()
        try:
            self._root = root if root is not None else self._tk.Tk()
        except Exception as exc:                    # TclError：没有 DISPLAY / 会话不可用
            raise HeadlessError("窗口创建失败：" + type(exc).__name__) from None
        from tkinter import ttk
        from ..gui.theme import apply_theme
        self._ttk = ttk
        self._controller = controller
        if getattr(controller, "save_path", None) is None:
            controller.save_path = _make_save_path(lambda: self._root)
        self._bus = bus
        self._on_quit = on_quit
        self._unsubscribe = None
        self._nav_buttons: dict[str, Any] = {}
        self._fields: dict[str, Any] = {}
        self._row_notes: dict[str, str] = {}
        self._tree: Any = None
        self._detail: Any = None
        self._detail_columns: tuple[str, ...] = ()
        self._vars: dict[str, Any] = {}
        self._root.title(title)
        self._root.geometry("1180x780")
        self._root.minsize(900, 620)
        apply_theme(self._root, ttk)
        self._build_shell()
        self._bind_keys()
        if self._bus is not None:
            self._unsubscribe = self._bus.subscribe(self._on_bus)
        self.show_view(controller.view)

    # ── 骨架
    def _build_shell(self) -> None:
        tk, ttk = self._tk, self._ttk
        self._root.rowconfigure(1, weight=1)
        self._root.columnconfigure(1, weight=1)
        top = ttk.Frame(self._root, style="Topbar.TFrame", padding=(16, 12))
        top.grid(row=0, column=0, columnspan=2, sticky="ew")
        top.columnconfigure(1, weight=1)
        ttk.Label(top, text="DJB 联脑", style="TopH2.TLabel").grid(row=0, column=0, sticky="w")
        self._view_title = ttk.Label(top, text=VIEW_TITLES[self._controller.view],
                                     style="TopH3.TLabel")
        self._view_title.grid(row=0, column=1, sticky="w", padx=(16, 0))
        # 演示数据必须在窗口上贴标签：截图/录屏不许被误当成真机数据
        self._demo_badge = None
        if getattr(self._controller.snapshot, "source", "") == "demo":
            self._demo_badge = ttk.Label(top, text="· 演示数据（--demo）", style="PillWait.TLabel")
            self._demo_badge.grid(row=0, column=2, sticky="e", padx=(0, 8))
        self._state_pill = ttk.Label(top, text="状态未知", style="PillMuted.TLabel")
        self._state_pill.grid(row=0, column=3, sticky="e", padx=(0, 12))
        ttk.Button(top, text="刷新 / F5", command=self.refresh).grid(row=0, column=4, sticky="e")
        side = ttk.Frame(self._root, style="Sidebar.TFrame", padding=(10, 12))
        side.grid(row=1, column=0, sticky="nsew")
        ttk.Label(side, text="导航", style="Meta.TLabel").pack(anchor="w", padx=6, pady=(0, 8))
        for index, key in enumerate(VIEW_KEYS, start=1):
            button = ttk.Button(side, text=str(index) + " · " + VIEW_TITLES[key],
                                style="Nav.TButton",
                                takefocus=True,
                                command=lambda name=key: self.show_view(name))
            button.pack(fill="x", pady=1)
            self._nav_buttons[key] = button
        wrapper = ttk.Frame(self._root, style="App.TFrame")
        wrapper.grid(row=1, column=1, sticky="nsew")
        wrapper.rowconfigure(0, weight=1)
        wrapper.columnconfigure(0, weight=1)
        from ..gui.theme import TOKENS
        self._canvas = tk.Canvas(wrapper, background=TOKENS["bg"], highlightthickness=0,
                                 borderwidth=0)
        self._canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(wrapper, orient="vertical", command=self._canvas.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self._canvas.configure(yscrollcommand=scrollbar.set)
        self._content = ttk.Frame(self._canvas, style="App.TFrame", padding=(20, 16))
        self._content_id = self._canvas.create_window((0, 0), window=self._content, anchor="nw")
        self._content.bind("<Configure>", self._on_content_resize)
        self._canvas.bind("<Configure>", self._on_canvas_resize)
        status = ttk.Frame(self._root, style="Topbar.TFrame", padding=(16, 8))
        status.grid(row=2, column=0, columnspan=2, sticky="ew")
        self._status = ttk.Label(status, text=DEFAULT_MESSAGE, style="TopMeta.TLabel")
        self._status.pack(side="left")

    def _on_content_resize(self, _event=None) -> None:
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_canvas_resize(self, event) -> None:
        self._canvas.itemconfigure(self._content_id, width=max(1, event.width))

    def _bind_keys(self) -> None:
        self._root.bind("<F5>", lambda _e: self.refresh())
        for index, key in enumerate(VIEW_KEYS, start=1):
            if index <= 9:
                self._root.bind("<Control-Key-" + str(index) + ">",
                                lambda _e, name=key: self.show_view(name))
        self._root.bind("<Control-r>", lambda _e: self.refresh())
        self._root.bind("<Escape>", lambda _e: self._canvas.yview_moveto(0))

    # ── 渲染
    def show_view(self, key: str) -> View:
        """切视图并立即完成布局（截图脚本依赖这一点）。"""
        try:
            view = self._controller.show(key)
        except ValueError:
            return self._controller.current()
        self._render(view)
        self._sync_nav(key)
        self._update_status()
        try:
            self._canvas.yview_moveto(0)
            self._root.update_idletasks()
        except Exception:
            pass
        return view

    def refresh(self) -> View:
        view = self._controller.refresh()
        self._render(view)
        self._update_status()
        return view

    def _sync_nav(self, key: str) -> None:
        for name, button in self._nav_buttons.items():
            button.configure(style="NavOn.TButton" if name == key else "Nav.TButton")

    def _update_status(self) -> None:
        self._status.configure(text=self._controller.status_line())
        pill = self._pill_text()
        self._state_pill.configure(text=pill[0], style=pill[1])

    def _pill_text(self) -> tuple[str, str]:
        status = self._controller.snapshot.get("status")
        if not isinstance(status, Mapping):
            return "? 状态未知", "PillMuted.TLabel"
        state = str(status.get("state") or "unknown")
        if state in ("online", "starting"):
            return "✓ 运行中", "Pill.TLabel"
        if state in ("paused", "updating"):
            return "◐ 已暂停", "PillWait.TLabel"
        if state == "error":
            return "✕ 错误", "PillFail.TLabel"
        return "· 已停止", "PillMuted.TLabel"

    def _clear(self) -> None:
        for child in self._content.winfo_children():
            child.destroy()
        self._fields.clear()
        self._vars.clear()
        self._row_notes.clear()
        self._tree = None
        self._detail = None

    def _render(self, view: View) -> None:
        ttk = self._ttk
        self._clear()
        self._view_title.configure(text=view.title)
        ttk.Label(self._content, text=view.subtitle, style="Muted.TLabel").pack(
            anchor="w", pady=(0, 10))
        for text in view.unavailable:
            ttk.Label(self._content, text=text, style="Warning.TLabel",
                      wraplength=760).pack(anchor="w", pady=(0, 4))
        if view.cards:
            row = ttk.Frame(self._content, style="App.TFrame")
            row.pack(fill="x", pady=(4, 12))
            for index, card in enumerate(view.cards):
                self._render_card(row, card, index)
        if view.actions:
            bar = ttk.Frame(self._content, style="App.TFrame")
            bar.pack(fill="x", pady=(0, 12))
            for action in view.actions:
                self._render_action(bar, action)
        if view.table is not None:
            self._render_table(view.table)
        if view.fields:
            self._render_fields(view.fields, view.key)
        for note in view.notes:
            if not note:
                continue
            ttk.Label(self._content, text="· " + note, style="Meta.TLabel",
                      wraplength=760).pack(anchor="w", pady=(6, 0))

    def _render_card(self, parent, card: Card, index: int, *, per_row: int = 3) -> None:
        """指标卡：每行 3 张（窄窗口也不横向溢出），取值按 tone 上色。"""
        ttk = self._ttk
        row, column = divmod(index, per_row)
        frame = ttk.Frame(parent, style="Card.TFrame", padding=(14, 12))
        frame.grid(row=row, column=column, sticky="nsew", padx=(0, 10), pady=(0, 10))
        parent.columnconfigure(column, weight=1, uniform="card")
        ttk.Label(frame, text=card.title, style="CardMuted.TLabel").pack(anchor="w")
        value_style = {"mint": "ValueMint.TLabel", "amber": "ValueWait.TLabel",
                       "red": "ValueFail.TLabel", "muted": "ValueMuted.TLabel",
                       "default": "ValueDefault.TLabel"}.get(card.tone, "ValueDefault.TLabel")
        ttk.Label(frame, text=card.value, style=value_style, wraplength=240).pack(
            anchor="w", pady=(4, 0))
        if card.meta:
            ttk.Label(frame, text=card.meta, style="CardMuted.TLabel", wraplength=240).pack(
                anchor="w", pady=(4, 0))

    def _ttk_family(self) -> str:
        from ..gui.theme import FONT
        return FONT[0]

    def _render_action(self, parent, action: Action) -> None:
        ttk = self._ttk
        button = ttk.Button(parent, text=action.label, takefocus=True,
                            style="Primary.TButton" if action.primary else "TButton",
                            command=lambda key=action.key: self._on_action(key))
        if not action.enabled:
            button.state(["disabled"])
        button.pack(side="left", padx=(0, 8))
        if not action.enabled and action.reason:
            ttk.Label(parent, text=action.reason, style="Meta.TLabel").pack(
                side="left", padx=(0, 12))

    def _render_table(self, table: Table) -> None:
        tk, ttk = self._tk, self._ttk
        frame = ttk.Frame(self._content, style="Card.TFrame", padding=(1, 1))
        frame.pack(fill="both", expand=True, pady=(0, 10))
        tree = ttk.Treeview(frame, columns=list(table.columns), show="headings",
                            height=min(12, max(3, len(table.rows) + 1)), selectmode="browse")
        for column in table.columns:
            tree.heading(column, text=column, anchor="w")
            tree.column(column, anchor="w", width=max(90, 620 // max(1, len(table.columns))),
                        stretch=True)
        from ..gui.theme import MUTED_TEXT, TOKENS, MINT_TEXT, AMBER_TEXT, RED_TEXT
        tree.tag_configure("disabled", foreground=MUTED_TEXT)
        tag_colors = {"mint": MINT_TEXT, "amber": AMBER_TEXT, "red": RED_TEXT,
                      "muted": MUTED_TEXT, "default": TOKENS["ink"]}
        for name, color in tag_colors.items():
            tree.tag_configure("tone-" + name, foreground=color)
        self._row_notes = {}
        for row in table.rows:
            tags = ["tone-" + (row.tone or "default")]
            if row.disabled:
                tags.append("disabled")          # 标灰优先于语义色
            iid = row.key or ""
            tree.insert("", "end", iid=iid or None, values=row.cells, tags=tuple(tags))
            if row.note:
                self._row_notes[iid] = row.note
        tree.pack(fill="both", expand=True)
        if not table.rows:
            ttk.Label(frame, text=table.empty, style="CardMuted.TLabel",
                      padding=(12, 16)).pack(anchor="w")
        # 详情抽屉：选中一行就在表格下面展开字段（键盘 ↑↓ 也生效）
        self._detail = ttk.Frame(self._content, style="CardAlt.TFrame", padding=(14, 10))
        self._detail_columns = tuple(table.columns)
        self._tree = tree
        if table.rows:
            tree.bind("<<TreeviewSelect>>", self._on_row_select)
            ttk.Label(self._content, text="选中一行看详情（↑↓ 或点击）",
                      style="Meta.TLabel").pack(anchor="w")
        if table.note:
            ttk.Label(self._content, text=table.note, style="Meta.TLabel").pack(anchor="w")

    def _on_row_select(self, _event=None) -> None:
        tree = getattr(self, "_tree", None)
        detail = getattr(self, "_detail", None)
        if tree is None or detail is None:
            return
        selection = tree.selection()
        for child in detail.winfo_children():
            child.destroy()
        if not selection:
            detail.pack_forget()
            return
        values = tree.item(selection[0], "values")
        try:
            self._controller.select_row(self._controller.view, values[0] if values else "")
        except Exception:
            pass
        detail.pack(fill="x", pady=(0, 10))
        for column, value in zip(self._detail_columns, values):
            line = self._ttk.Frame(detail, style="CardAlt.TFrame")
            line.pack(fill="x", pady=1)
            self._ttk.Label(line, text=column, style="CardMuted.TLabel", width=14,
                            anchor="w").pack(side="left")
            self._ttk.Label(line, text=str(value), style="Card.TLabel").pack(side="left")
        note = self._row_notes.get(selection[0], "")
        if note:
            self._ttk.Label(detail, text="原因：" + note, style="CardMuted.TLabel").pack(
                anchor="w", pady=(4, 0))

    def _render_fields(self, fields: Sequence[Field], view_key: str) -> None:
        """字段区：标签 + 输入框 + 下一行提示（保存按钮在上方动作条，不重复）。"""
        ttk = self._ttk
        tk = self._tk
        box = ttk.Frame(self._content, style="Card.TFrame", padding=(16, 14))
        box.pack(fill="x", pady=(4, 10))
        ttk.Label(box, text="可编辑字段" if any(f.editable for f in fields) else "详情",
                  style="H3.TLabel").pack(anchor="w", pady=(0, 8))
        for field in fields:
            block = ttk.Frame(box, style="Card.TFrame")
            block.pack(fill="x", pady=(0, 8))
            block.columnconfigure(1, weight=1)
            ttk.Label(block, text=field.label, style="CardMuted.TLabel", width=18,
                      anchor="w").grid(row=0, column=0, sticky="w", padx=(0, 12))
            if field.editable:
                variable = tk.StringVar(value="" if field.secret else field.value)
                entry = ttk.Entry(block, textvariable=variable, width=48)
                if field.secret:
                    entry.configure(show="•")
                entry.grid(row=0, column=1, sticky="ew")
                hint = field.hint or ""
                if field.secret:
                    hint = ("当前 " + field.value + "｜" + hint).strip("｜")
                ttk.Label(block, text=hint, style="CardMuted.TLabel", wraplength=620).grid(
                    row=1, column=1, sticky="w", pady=(2, 0))
                self._fields[field.key] = field
                self._vars[field.key] = variable
            else:
                ttk.Label(block, text=field.value, style="Card.TLabel").grid(
                    row=0, column=1, sticky="w")
                if field.hint:
                    ttk.Label(block, text=field.hint, style="CardMuted.TLabel",
                              wraplength=620).grid(row=1, column=1, sticky="w", pady=(2, 0))

    # ── 动作
    def _on_action(self, key: str) -> None:
        for name, variable in self._vars.items():
            self._controller.set_draft(name, variable.get())
        result = self._controller.action(key, confirm=self._confirm)
        if result.effect == "copy":
            self._copy(result.payload)
        elif result.effect == "open_dir":
            self._open_dir(result.payload)
        elif result.effect in ("refresh", "access", "bench", "control", "reload"):
            self._render(self._controller.current())
        self._update_status()
        if result.effect in ("save", "access", "reload", "control", "export", "verify") and result.message:
            self._toast(result.message, ok=result.ok)

    def _copy(self, text: str) -> None:
        try:
            self._root.clipboard_clear()
            self._root.clipboard_append(text)
        except Exception:
            pass

    def _open_dir(self, path: str) -> None:
        """只做「显示路径」：不自动调用系统文件管理器（避免在无桌面环境弹错）。"""
        if path:
            self._toast("日志目录：" + path, ok=True)

    def _toast(self, message: str, *, ok: bool = True) -> None:
        self._status.configure(text=("✓ " if ok else "! ") + message)

    def _confirm(self, message: str) -> bool:
        """二次确认（模态，键盘可达：Esc 取消，Enter 确认）。"""
        tk, ttk = self._tk, self._ttk
        from ..gui.theme import apply_theme
        window = tk.Toplevel(self._root)
        apply_theme(window, ttk)
        window.title("DJB / 确认本机控制")
        window.geometry("560x260")
        choice = [False]
        body = ttk.Frame(window, padding=20)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="! 影响确认 / 可取消（Esc）", style="Warning.TLabel").pack(anchor="w")
        ttk.Label(body, text=message, wraplength=500).pack(fill="x", pady=18)
        bar = ttk.Frame(body)
        bar.pack(fill="x", side="bottom")

        def accept() -> None:
            choice[0] = True
            window.destroy()

        ttk.Button(bar, text="确认执行并回读", style="Primary.TButton",
                   command=accept).pack(side="right")
        cancel = ttk.Button(bar, text="取消 / Esc", command=window.destroy)
        cancel.pack(side="left")
        cancel.focus_set()
        window.bind("<Escape>", lambda _e: window.destroy())
        window.bind("<Return>", lambda _e: accept())
        window.grab_set()
        self._root.wait_window(window)
        return choice[0]

    def _on_bus(self, snapshot) -> None:
        """托盘那边改了状态：立刻重画（不重新取数，避免抖动）。"""
        try:
            self._update_status()
        except Exception:
            pass

    # ── 生命周期
    def run(self) -> None:
        self._root.mainloop()

    def stop(self) -> None:
        if self._unsubscribe is not None:
            try:
                self._unsubscribe()
            except Exception:
                pass
            self._unsubscribe = None
        try:
            self._root.destroy()
        except Exception:
            pass
        if self._on_quit is not None:
            try:
                self._on_quit()
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────── 演示 / 入口
def demo_snapshot() -> Snapshot:
    """确定性的演示数据（截图取证用；不连网络、不读用户目录）。"""
    return FakeDataSource(_DEMO_SECTIONS, source="demo").snapshot()


_DEMO_SECTIONS = {
        "status": {"state": "online", "accepting": True, "daemon_running": True,
                   "balance": "128.40", "today_earnings": "6.12", "reason": None,
                   "inflight": [{"id": "t-8821", "state": "running", "elapsed_s": 12}]},
        "bench": {"cpu_count": 8, "cpu_load_1m": 0.42, "mem_total_bytes": 32 * 1024 ** 3,
                  "mem_available_bytes": 21 * 1024 ** 3, "disk_total_bytes": 2 * 1024 ** 4,
                  "disk_free_bytes": 1_400 * 1024 ** 3, "platform": "Linux", "machine": "x86_64",
                  "python": "3.12.15",
                  "checks": [{"id": "system", "level": "ok", "summary": "系统满足最低要求",
                              "next_step": "无需处理"},
                             {"id": "acceleration", "level": "warn",
                              "summary": "未检测到 GPU 加速",
                              "next_step": "CPU 模式可运行，收益按 CPU 计"}]},
        "gpu": {"present": False, "devices": [],
                "note": "本机无可用 GPU（没有 nvidia-smi；没有 rocm-smi）"},
        "models": {"catalog": [
            {"id": "qwen2.5-7b-instruct", "context": 32768, "offered": True},
            {"id": "llama-3.1-8b-instruct", "context": 131072, "offered": True},
            {"id": "mixtral-8x7b", "context": 32768, "offered": False,
             "reason": "需要 ≥24GB 显存，本机不满足"}],
            "local": [{"id": "qwen2.5-7b-instruct", "state": "applied"}],
            "online": True, "note": ""},
        "tasks": {"rows": [
            {"id": "t-8821", "state": "running", "elapsed_s": 12, "tokens": 1536},
            {"id": "t-8820", "state": "done", "elapsed_s": 43, "tokens": 8192,
             "receipt_id": "rcpt-77a1"}], "origin": "local", "note": "云端任务列表暂不可用"},
        "receipts": {"rows": [
            {"receipt_id": "rcpt-77a1", "created_at": "2026-11-01T03:12:00Z", "tokens": 8192,
             "status": "verified"},
            {"receipt_id": "rcpt-77a0", "created_at": "2026-11-01T02:44:00Z", "tokens": 2048,
             "status": "pending"}], "origin": "local", "note": ""},
        "earnings": {"balance": "128.40", "today": "6.12", "currency": "USDT",
                     "pending": "1.80", "settled": "4.32",
                     "payouts": [{"id": "po-31", "amount": "4.32", "currency": "USDT",
                                  "status": "settled", "created_at": "2026-10-31"}],
                     "online": True, "note": ""},
        "agent": {"upstream": {"openai": "http://127.0.0.1:1717/v1",
                               "anthropic": "http://127.0.0.1:1717"},
                  "downlink": {"url": "http://127.0.0.1:1717/agent", "token": "djb_sc_…9f2c"},
                  "note": "下行令牌只读展示；轮换请到服务端控制台。"},
        "access": {"valid": True, "saved": True,
                   "config": {"node_id": "node-7c31", "node_secret": "djb_ns_…4e11",
                              "api_key": "djb_sk_…abcd", "price_mode": "auto",
                              "price_mode_label": "跟随平台建议价",
                              "price_note": "—", "usdt_address": "TQn9Xa…7f3d",
                              "openai_base_url": "http://127.0.0.1:1717/v1",
                              "anthropic_base_url": "http://127.0.0.1:1717",
                              "agent_downlink_url": "http://127.0.0.1:1717/agent",
                              "scoped_token": "djb_sc_…9f2c"},
                   "valid": True, "binding": {"state": "bound", "node_id": "node-7c31"},
                   "note": "", "path": "~/.djb/desktop/access-config.json"},
        "yield": {"state": "online", "reason": None, "reason_label": None, "accepting": True,
                  "mode": "balanced", "yield_enabled": True},
        "account": {"profile": {"email": "node@example.com", "role": "共享方",
                                "twofa_enabled": True},
                    "security": {"node_id": "node-7c31", "storage": "file"},
                    "note": ""},
        "logs": {"rows": [
            {"ts": "2026-11-01T04:31:02Z", "level": "info", "event": "daemon.started"},
            {"ts": "2026-11-01T04:31:09Z", "level": "info", "event": "channel.claimed"},
            {"ts": "2026-11-01T04:32:44Z", "level": "warning",
             "event": "yield.detected"},
            {"ts": "2026-11-01T04:33:10Z", "level": "error", "event": "upstream.probe_failed"}],
            "path": "~/.djb/logs/djb.log", "levels": ["all", "debug", "info", "warning",
                                                      "error"], "note": ""},
}


def _make_save_path(root_getter):
    """导出用保存对话框：返回用户选的路径；取消/无对话框 → None（绝不自己编路径）。"""
    def ask(default_name: str) -> str | None:
        try:
            from tkinter import filedialog

            root = root_getter()
            path = filedialog.asksaveasfilename(
                parent=root, title="导出收益明细", defaultextension=".csv",
                initialfile=default_name,
                filetypes=(("CSV 文件", "*.csv"), ("所有文件", "*.*")))
        except Exception:
            return None
        return str(path) if path else None
    return ask


def _make_cli_runner(home):
    """本机命令通道：显式带上 ``DJB_HOME``，避免子进程读到别的 home。"""
    def run(argv):
        import subprocess
        import sys

        env = dict(os.environ)
        env["DJB_HOME"] = str(home)
        command = [sys.executable, "-m", "djb_node", *argv]
        if "--json" not in argv:
            command.append("--json")
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=20,
                                       env=env, stdin=subprocess.DEVNULL, check=False)
        except subprocess.TimeoutExpired:
            raise ProviderError("本地命令超时（20s）") from None
        except OSError as exc:
            raise ProviderError("本地命令无法执行（" + type(exc).__name__ + "）") from None
        if completed.returncode != 0:
            lines = [line.strip() for line in (completed.stderr or completed.stdout or "").splitlines()
                     if line.strip() and not line.strip().startswith("details=")]
            detail = lines[0].removeprefix("djb: ") if lines else "退出码 " + str(completed.returncode)
            raise ProviderError(detail[:160])
        try:
            return json.loads(completed.stdout or "{}")
        except ValueError:
            raise ProviderError("本地命令输出不是 JSON") from None
    return run


def _make_reloader(home: str | os.PathLike | None):
    """接 daemon 的热重载：与 `djb daemon reload` **同一条控制命令**。

    返回 ``(状态, 说明)``：成功给新配置的变更摘要，失败给 daemon 的原话（可读原因），
    没接上/旧版本 → 对应状态，绝不谎报「已生效」。
    """
    def reload() -> tuple[str, str]:
        try:
            from ..daemon import ControlClient
            from ..config import Paths
            from ..errors import NotRunningError, UsageError
        except ImportError:                       # pragma: no cover - 防御
            return RELOAD_UNSUPPORTED, ""
        paths = Paths(str(home) if home else os.path.expanduser("~/.djb"))
        try:
            response = ControlClient(paths).call({"cmd": "reload"}, timeout=15.0)
        except NotRunningError:
            return RELOAD_DAEMON_DOWN, ""
        except UsageError:
            # 旧 daemon 不认识 reload：这是「没有热重载能力」，不是保存失败
            return RELOAD_UNSUPPORTED, ""
        except Exception as exc:
            return RELOAD_FAILED, type(exc).__name__
        if response.get("ok") is True:
            changed = response.get("changed") or []
            detail = ("已重载：" + ", ".join(str(item) for item in changed)) if changed \
                else "配置无变化"
            return RELOAD_OK, detail
        return RELOAD_FAILED, str(response.get("error") or "原因未知")
    return reload


def demo_source() -> FakeDataSource:
    """演示数据源（``--demo`` / 截图脚本共用，内容确定）。"""
    return FakeDataSource(_DEMO_SECTIONS, source="demo")


def make_controller(*, home: str | os.PathLike | None = None, demo: bool = False,
                    cloud: Any | None = None, bus: Any | None = None) -> DesktopController:
    """装配：数据源 + 接入配置服务 + 控制通道 + 状态总线。"""
    from ..gui.state import ControlBus
    bus = bus if bus is not None else ControlBus()
    if demo:
        source: Any = demo_source()
    else:
        if cloud is None:
            # 没显式注入就按 ~/.djb 自己装：没登录节点时是 None（视图照常显示「暂不可用」）
            from .nodecloud import NodeCloudClient
            cloud = NodeCloudClient.from_home(home)
        source = LocalDataSource(home=home, cloud=cloud)
    store = FileAccessStore(None if home is None else
                            os.path.join(str(home), ACCESS_FILE))
    access = AccessService(store, reloader=_make_reloader(home))
    return DesktopController(source, access=access, control=LocalControlChannel(home), bus=bus,
                             cli=_make_cli_runner(home) if not demo else None)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="djb desktop", description="DJB 联脑桌面控制台（12 视图）")
    parser.add_argument("--home", help="本机数据目录（默认 $DJB_HOME 或 ~/.djb）")
    parser.add_argument("--view", default="overview", choices=VIEW_KEYS,
                        help="启动时打开的视图")
    parser.add_argument("--demo", action="store_true", help="用确定性演示数据（截图/培训用）")
    parser.add_argument("--shot", metavar="DIR", help="构造后逐视图截图到目录（需要 ImageMagick import）")
    args = parser.parse_args(list(argv) if argv is not None else None)
    controller = make_controller(home=args.home, demo=args.demo)
    try:
        app = DesktopApp(controller, on_quit=lambda: None)
    except HeadlessError:
        print(HEADLESS_HINT)
        return EXIT_HEADLESS
    except Exception as exc:                        # tkinter 缺失 / 无显示：一律 3
        print(HEADLESS_HINT + "（" + type(exc).__name__ + "）")
        return EXIT_HEADLESS
    app.show_view(args.view)
    if args.shot:
        _shoot(app, args.shot)
        app.stop()
        return EXIT_OK
    app.run()
    return EXIT_OK


def _shoot(app: DesktopApp, directory: str) -> list[str]:
    """逐视图截图（调用外部 ``import``；失败只记录，不影响退出码）。"""
    import subprocess
    os.makedirs(directory, exist_ok=True)
    written: list[str] = []
    for key in VIEW_KEYS:
        app.show_view(key)
        try:
            app._root.update()          # noqa: SLF001 - 截图脚本需要一次完整重绘
            app._root.update_idletasks()
        except Exception:
            pass
        shots = [(os.path.join(directory, "view-" + key + ".png"), None)]
        if getattr(app, "_tree", None) is not None:
            shots.append((os.path.join(directory, "view-" + key + "-detail.png"), "select"))
        for target, mode in shots:
            if mode == "select":
                try:
                    children = app._tree.get_children()   # noqa: SLF001 - 取证脚本
                    if not children:
                        continue
                    app._tree.selection_set(children[0])
                    app._tree.focus(children[0])
                    app._root.update()
                except Exception:
                    continue
            try:
                window_id = str(app._root.winfo_id())
                subprocess.run(["import", "-window", window_id, target], check=False, timeout=30)
            except (OSError, subprocess.SubprocessError):
                continue
            if os.path.exists(target):
                written.append(target)
    return written


if __name__ == "__main__":                          # pragma: no cover
    sys.exit(main())
