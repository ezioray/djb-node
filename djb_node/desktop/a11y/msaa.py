# SPDX-License-Identifier: Apache-2.0
"""MSAA / IAccessible 提供方（**只在 Windows 生效**）。

对外三件事：:func:`attach` / :meth:`Provider.refresh` / :meth:`Provider.detach`。
渲染层（``app.py``）只调这三个，不碰 COM 细节；本模块**在模块级不 import comtypes**，
所以 Linux 上 import 它永远是安全的。

四条硬规矩（复核清单）
======================

1. **COM 边界绝不抛异常**：每个 ``acc*`` 方法最外层 try/except，出错一律返回 ``E_FAIL`` 并留一条
   限流日志——Python traceback 穿过 COM 边界会直接崩掉客户端进程。
2. **优雅降级**：``comtypes`` 缺失 / ``SetWindowLongPtrW`` 失败 / 初始化任一步出错 →
   provider 不装，界面照常跑，日志留 ``desktop.a11y.disabled reason=…``。
   ``DJB_DESKTOP_A11Y=0`` 是**故障逃生阀**（不是特性开关）：设了就不装 provider。
3. **默认不落盘**：只有 ``DJB_DESKTOP_A11Y_TRACE=<路径>`` 时才写调用日志；日志里
   ``accValue`` 只写长度（**不写原文**，避免金额/地址/节点 ID 进磁盘）。
4. **刷新一般不发事件，但焦点/选中移位要补发**：:meth:`Provider.refresh` 只换数据；唯一的例外是
   被聚焦/被选中的业务行位置变了——那时用它的**新 childId 重发** ``EVENT_OBJECT_FOCUS`` /
   ``EVENT_OBJECT_SELECTION``，否则读屏手里那个号会指向另一行。childId 是**位置**
   （``children`` 下标 + 1，密集 1..N），身份由 ``node_id`` 路径承担，两者别混。

实现要点（都是踩过的坑，别删）
==============================

* ``comtypes.client.GetModule("oleacc.dll")`` 给的是 **dispinterface 壳**，vtable 全是
  ``E_NOTIMPL`` —— 必须**手写 vtable 声明**（GUID 用 IAccessible 真 IID，方法按 7..27 槽）。
* ``IDispatch`` 的 ``GetTypeInfo/GetTypeInfoCount`` 要交回 oleacc 的 IAccessible ``ITypeInfo``，
  否则 ``LresultFromObject`` 的句柄跨进程换不回来。
* 出参字符串必须是 **``SysAllocString`` 的真 BSTR**，给 Python 缓冲区会让客户端桩崩溃
  （``RPC_S_CALL_FAILED``）。
* ``WM_GETOBJECT`` 的 ``lParam`` 是 32 位，``-4`` 会以 ``0xFFFFFFFC`` 到达，必须先归一化。
* ``accLocation``：**宁可返回错误也不返回假坐标**（错误的焦点框比没有框更糟）。
"""
from __future__ import annotations

import logging
import os
import sys
from dataclasses import replace
from typing import Any

from ..views import View
from .tree import (STATE_FOCUSABLE, STATE_SELECTABLE, STATE_SELECTED, Node, build_tree,
                   flatten_for_provider)

__all__ = ["ENV_DISABLE", "ENV_TRACE", "Provider", "attach", "available"]

log = logging.getLogger("djb_node.desktop.a11y")

#: 故障逃生阀：置 ``0`` 就不装 provider（不是特性开关，是「读屏把界面拖死」时的应急阀）
ENV_DISABLE = "DJB_DESKTOP_A11Y"
#: 调试调用日志路径；不设就**一行都不写**
ENV_TRACE = "DJB_DESKTOP_A11Y_TRACE"

_E_FAIL = -2147467259
S_FALSE = 1
E_INVALIDARG = -2147024809            # 0x80004005
_E_NOTIMPL = -2147467263         # 0x80004001
_DISP_E_BADINDEX = -2147352572   # 0x80020004
_OBJID_CLIENT = -4
_WM_GETOBJECT = 0x003D
_EVENT_OBJECT_FOCUS = 0x8005
_EVENT_OBJECT_SELECTION = 0x8006
_SELFLAG_TAKEFOCUS = 0x1
_SELFLAG_TAKESELECTION = 0x2

#: 一次刷新里最多报几条异常（防止读屏狂刷把日志撑爆）
_MAX_ERROR_LOGS = 20


#: 逃生阀的「关」取值。以前只认 ``"0"``，客户写 ``false``/``off`` 关不掉（复核 SHOULD-FIX 1）。
_DISABLE_VALUES = frozenset({"0", "false", "no", "off", "disable", "disabled"})


def _disabled_by_env() -> bool:
    """``DJB_DESKTOP_A11Y`` 是否要求关掉 provider（大小写/空白都不敏感）。"""
    raw = os.environ.get(ENV_DISABLE)
    if raw is None:
        return False                                  # 没设 = 默认开（默认值即「开」）
    return str(raw).strip().lower() in _DISABLE_VALUES


def available() -> bool:
    """当前环境能不能装 provider（不 import comtypes，只看平台与逃生阀）。"""
    if _disabled_by_env():
        return False
    return sys.platform == "win32"


class _Runtime:
    """COM 侧的一堆东西（接口声明、oleacc/oleaut32、窗口过程钩子）。只构造一次。"""

    def __init__(self) -> None:  # pragma: no cover - Windows only
        import ctypes
        import ctypes.wintypes as wt

        import comtypes
        from comtypes import COMMETHOD, GUID, HRESULT, IUnknown
        from comtypes import COMObject
        from comtypes.automation import IDispatch, VARIANT
        from comtypes.typeinfo import LoadRegTypeLib
        from ctypes import POINTER, c_int, c_long, c_ulong, c_void_p, c_wchar_p

        self.ctypes = ctypes
        self.wt = wt
        self.VARIANT = VARIANT
        self.COMObject = COMObject
        self.POINTER = POINTER
        self.c_long = c_long
        self.c_ulong = c_ulong
        self.c_void_p = c_void_p
        self.c_wchar_p = c_wchar_p
        self.IUnknown = IUnknown
        self.IDispatch = IDispatch
        self.HRESULT = HRESULT

        class IAccessibleV(IDispatch):
            """IAccessible 的 vtable 声明（IID 用真值；顺序 = MSAA 规范 7..27 槽）。"""

            _iid_ = GUID("{618736E0-3C3D-11CF-810C-00AA00389B71}")
            _methods_ = [
                COMMETHOD([], HRESULT, "get_accParent",
                          ([], POINTER(POINTER(IDispatch)), "ppdispParent")),
                COMMETHOD([], HRESULT, "get_accChildCount", ([], POINTER(c_long), "pcount")),
                COMMETHOD([], HRESULT, "get_accChild",
                          ([], VARIANT, "varChild"),
                          ([], POINTER(POINTER(IDispatch)), "ppdispChild")),
                COMMETHOD([], HRESULT, "get_accName",
                          ([], VARIANT, "varChild"), ([], POINTER(c_void_p), "pszName")),
                COMMETHOD([], HRESULT, "get_accValue",
                          ([], VARIANT, "varChild"), ([], POINTER(c_void_p), "pszValue")),
                COMMETHOD([], HRESULT, "get_accDescription",
                          ([], VARIANT, "varChild"), ([], POINTER(c_void_p), "pszDescription")),
                COMMETHOD([], HRESULT, "get_accRole",
                          ([], VARIANT, "varChild"), ([], POINTER(VARIANT), "pvarRole")),
                COMMETHOD([], HRESULT, "get_accState",
                          ([], VARIANT, "varChild"), ([], POINTER(VARIANT), "pvarState")),
                COMMETHOD([], HRESULT, "get_accHelp",
                          ([], VARIANT, "varChild"), ([], POINTER(c_void_p), "pszHelp")),
                COMMETHOD([], HRESULT, "get_accHelpTopic",
                          ([], POINTER(c_void_p), "pszHelpFile"), ([], VARIANT, "varChild"),
                          ([], POINTER(c_long), "pidTopic")),
                COMMETHOD([], HRESULT, "get_accKeyboardShortcut",
                          ([], VARIANT, "varChild"), ([], POINTER(c_void_p), "psz")),
                COMMETHOD([], HRESULT, "get_accFocus", ([], POINTER(VARIANT), "pvarChild")),
                COMMETHOD([], HRESULT, "get_accSelection", ([], POINTER(VARIANT), "pvarChildren")),
                COMMETHOD([], HRESULT, "get_accDefaultAction",
                          ([], VARIANT, "varChild"), ([], POINTER(c_void_p), "psz")),
                COMMETHOD([], HRESULT, "accSelect",
                          ([], c_long, "flagsSelect"), ([], VARIANT, "varChild")),
                COMMETHOD([], HRESULT, "accLocation",
                          ([], POINTER(c_long), "pxLeft"), ([], POINTER(c_long), "pyTop"),
                          ([], POINTER(c_long), "pcxWidth"), ([], POINTER(c_long), "pcyHeight"),
                          ([], VARIANT, "varChild")),
                COMMETHOD([], HRESULT, "accNavigate",
                          ([], c_long, "navDir"), ([], VARIANT, "varStart"),
                          ([], POINTER(VARIANT), "pvarEndUpAt")),
                COMMETHOD([], HRESULT, "accHitTest",
                          ([], c_long, "xLeft"), ([], c_long, "yTop"),
                          ([], POINTER(VARIANT), "pvarChild")),
                COMMETHOD([], HRESULT, "accDoDefaultAction", ([], VARIANT, "varChild")),
                COMMETHOD([], HRESULT, "put_accName",
                          ([], VARIANT, "varChild"), ([], c_wchar_p, "szName")),
                COMMETHOD([], HRESULT, "put_accValue",
                          ([], VARIANT, "varChild"), ([], c_wchar_p, "szValue")),
            ]

        self.IAccessibleV = IAccessibleV
        self.typelib = LoadRegTypeLib(GUID("{1EA4DBF0-3C3B-11CF-810C-00AA00389B71}"), 1, 1, 0)
        self.typeinfo = self.typelib.GetTypeInfoOfGuid(IAccessibleV._iid_)

        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.oleacc = ctypes.WinDLL("oleacc", use_last_error=True)
        self.oleaut32 = ctypes.WinDLL("oleaut32", use_last_error=True)
        self.LRESULT = ctypes.c_ssize_t
        self.user32.SetWindowLongPtrW.restype = self.LRESULT
        self.user32.SetWindowLongPtrW.argtypes = [wt.HWND, c_int, c_void_p]
        self.user32.CallWindowProcW.restype = self.LRESULT
        self.user32.CallWindowProcW.argtypes = [c_void_p, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
        self.user32.GetParent.restype = wt.HWND
        self.user32.GetParent.argtypes = [wt.HWND]
        self.oleacc.LresultFromObject.restype = self.LRESULT
        self.oleacc.LresultFromObject.argtypes = [c_void_p, wt.WPARAM, c_void_p]
        self.oleaut32.SysAllocString.restype = c_void_p
        self.oleaut32.SysAllocString.argtypes = [c_wchar_p]

        comtypes.CoInitialize()

    def bstr(self, text: str) -> int:
        """真 BSTR（由 oleaut32 分配，桩/客户端用 ``SysFreeString`` 释放）。"""
        return self.oleaut32.SysAllocString(text or "")

    def empty_variant(self, target: Any) -> None:
        target[0].vt = 0                      # VT_EMPTY

    def int_variant(self, target: Any, value: int) -> None:
        target[0].vt = 3                      # VT_I4
        target[0].value = int(value)


def _process_by_exe(name: str) -> int:
    """按可执行文件名找进程（给 trace 用：证明读屏软件当时在跑）。找不到就 0。"""
    try:
        import ctypes
        import ctypes.wintypes as wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        count = 4096
        while True:
            buffer = (wintypes.DWORD * count)()
            needed = wintypes.DWORD()
            if not psapi.EnumProcesses(buffer, ctypes.sizeof(buffer), ctypes.byref(needed)):
                return 0
            got = needed.value // ctypes.sizeof(wintypes.DWORD)
            if got < count:
                break
            count *= 2
        for index in range(got):
            handle = kernel32.OpenProcess(0x1000, False, buffer[index])
            if not handle:
                continue
            try:
                path = ctypes.create_unicode_buffer(1024)
                size = wintypes.DWORD(len(path))
                if kernel32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
                    if path.value.replace("/", "\\").rsplit("\\", 1)[-1].lower() == name.lower():
                        return int(buffer[index])
            finally:
                kernel32.CloseHandle(handle)
    except Exception:
        return 0
    return 0


def _foreground_exe() -> str:
    """trace 里记「调用发生时前台进程的可执行文件名」。

    注意口径：这是**前台窗口**的进程，不是发起 COM 调用的进程。读屏软件调 acc* 时它自己是
    前台，所以这一列才会是 ``nvda.exe``；反过来不能拿它当「谁调用的」证据。
    """
    try:
        import ctypes
        import ctypes.wintypes as wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return "-"
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        handle = kernel32.OpenProcess(0x1000, False, pid)     # QUERY_LIMITED_INFORMATION
        if not handle:
            return "-"
        try:
            buffer = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buffer))
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return "-"
            return buffer.value.replace("/", "\\").rsplit("\\", 1)[-1] or "-"
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return "-"


def _pointer_keys(obj: Any) -> list[str]:
    """诊断用：COMObject 里已经 QI 出来的接口名（不打印指针值）。"""
    try:
        return [getattr(iface, "__name__", str(iface))
                for iface in getattr(obj, "_com_pointers_", {})]
    except Exception:
        return []


def _dispatch_pointer(obj: Any) -> Any:
    """取这个小对象的 ``IDispatch*``。

    ``IAccessible`` 是 **dual interface**：同一个 vtable 既能当 IAccessible 也能当 IDispatch 用，
    所以拿 IAccessible 的指针当 IDispatch 交出去是合法的。**不要**在这里
    ``QueryInterface(IDispatch)``：comtypes 的 Python 侧 COM 对象对自身 IDispatch 的 QI
    会回 ``E_NOINTERFACE``，客户端就只能看到「有名字、点不进去」的平树。
    """
    pointers = getattr(obj, "_com_pointers_", {}) or {}
    fallback = None
    for iface, pointer in pointers.items():
        if pointer is None:
            continue
        if getattr(iface, "__name__", "") == "IDispatch":
            return pointer
        if fallback is None:
            fallback = pointer
    return fallback


def _child_index(var: Any) -> int:
    """``CHILDID_SELF`` 是 0；其余取整数（comtypes 的 VARIANT 有时给对象、有时给值）。"""
    try:
        return int(var)
    except Exception:
        try:
            return int(var.value)
        except Exception:
            return 0


class Provider:
    """把一个 Tk 窗口变成读屏可见的 MSAA 提供方。"""

    def __init__(self, root: Any) -> None:
        self.root = root
        self.rt: _Runtime | None = None
        self.hwnd = 0
        self.outer = 0
        self._hooks: list[Any] = []
        self._procs: list[Any] = []
        self._root_object: Any = None
        self._node_class: Any = None
        self._objects: dict[str, Any] = {}
        self._node_by_path: dict[str, Node] = {}
        self._children_by_path: dict[str, tuple[str, ...]] = {}
        self._parent_by_path: dict[str, str] = {}
        self._focused_path = ""
        self._selection_path = ""
        self._root_path = ""
        # childId 必须是**密集**的 1..N（MSAA 客户端按 1..accChildCount 遍历，稀疏 id 必撞空，
        # 复核 MF-2）。所以 id = 当前 children 顺序里的位置 + 1；**身份稳定性由 path 承担**：
        # 焦点/选中记住的是 path，刷新后位置变了就用新 childId **重发**焦点/选中事件。
        self._focus_event_id = 0
        self._selection_event_id = 0
        # 发过的事件（event, childId, path）：真机取证与单测都用它，不发事件也给记账
        self._event_log: list[tuple[int, int, str]] = []
        self._tree: Node | None = None
        self._error_count = 0
        self._trace_path = (os.environ.get(ENV_TRACE) or "").strip()

    # ── 生命周期 ──────────────────────────────────────────────────────────────
    def attach(self) -> bool:
        """装到 Tk 顶层窗口上；失败返回 ``False``（调用方什么都不用做）。"""
        try:
            self.rt = _Runtime()
        except Exception as exc:                      # pragma: no cover - Windows only
            log.warning("desktop.a11y.disabled reason=comtypes_unavailable error=%r", exc)
            return False
        try:
            import tkinter  # noqa: F401  （确保 root 已经真实创建）
            self.root.update_idletasks()
            self.hwnd = int(self.root.winfo_id())
            outer = self.rt.user32.GetParent(self.rt.wt.HWND(self.hwnd))
            self.outer = int(outer or 0)
            self._node_class = _build_node_object(self.rt)
            self._root_object = self._node_class(self, "")
            punk = self._root_object.QueryInterface(self.rt.IUnknown)
            self._punk = punk
            self._install(self.hwnd, "inner", punk)
            if self.outer:
                self._install(self.outer, "outer", punk)
            log.info("desktop.a11y.enabled hwnd=%s outer=%s", self.hwnd, self.outer)
            return True
        except Exception as exc:                      # pragma: no cover - Windows only
            log.warning("desktop.a11y.disabled reason=attach_failed error=%r", exc)
            self._uninstall()
            return False

    def _install(self, hwnd: int, tag: str, punk: Any) -> None:  # pragma: no cover
        rt = self.rt
        old = rt.user32.SetWindowLongPtrW(rt.wt.HWND(hwnd), -4, None)   # GWLP_WNDPROC 读旧值
        if not old:
            raise OSError("SetWindowLongPtrW 读旧窗口过程失败 hwnd=%s" % hwnd)
        rt.user32.SetWindowLongPtrW(rt.wt.HWND(hwnd), -4, rt.ctypes.cast(old, rt.c_void_p))

        def handler(h, msg, wparam, lparam):
            if msg == _WM_GETOBJECT:
                raw = int(lparam) & 0xFFFFFFFF
                objid = raw - 0x100000000 if raw >= 0x80000000 else raw
                if objid in (_OBJID_CLIENT, 0):
                    try:
                        self._trace("WM_GETOBJECT@" + tag, objid, "hwnd=%d" % int(h))
                        return rt.oleacc.LresultFromObject(
                            rt.ctypes.byref(rt.IAccessibleV._iid_), wparam, punk)
                    except Exception as exc:
                        self._error("WM_GETOBJECT@" + tag, exc)
                        return 0
            return rt.user32.CallWindowProcW(old, h, msg, wparam, lparam)

        proc = rt.ctypes.WINFUNCTYPE(rt.LRESULT, rt.wt.HWND, rt.wt.UINT, rt.wt.WPARAM,
                                     rt.wt.LPARAM)(handler)
        rt.user32.SetWindowLongPtrW(rt.wt.HWND(hwnd), -4, rt.ctypes.cast(proc, rt.c_void_p))
        self._procs.append((hwnd, old, proc))

    def _uninstall(self) -> None:                     # pragma: no cover - Windows only
        for hwnd, old, _proc in reversed(getattr(self, "_procs", [])):
            try:
                self.rt.user32.SetWindowLongPtrW(self.rt.wt.HWND(hwnd), -4, old)
            except Exception:
                pass
        self._procs = []

    def detach(self) -> None:
        self._uninstall()
        self._objects.clear()
        self._root_object = None
        self.rt = None

    # ── 数据 ──────────────────────────────────────────────────────────────────
    def refresh(self, view: View, *, max_rows: int | None = None) -> None:
        """换一页/一版数据。**不发任何焦点事件**（自动刷新不能把读屏的焦点顶走）。

        没有 COM 运行时也照常建树（纯数据），这样 Linux 上能单测节点解析。
        """
        try:
            tree = build_tree(view) if max_rows is None else build_tree(view, max_rows=max_rows)
        except Exception as exc:                      # pragma: no cover - 防御
            self._error("build_tree", exc)
            return
        old_focus_id = self.child_id(self._focused_path) if self._focused_path else 0
        old_selection_id = self.child_id(self._selection_path) if self._selection_path else 0
        self._tree = tree
        self._root_path = tree.node_id
        self._node_by_path = {}
        self._children_by_path = {}
        self._parent_by_path = {}
        # provider 只暴露「根 + 一层简单子元素」：跨进程没有编组，子对象回不来（见 tree 里的说明）
        flat = flatten_for_provider(tree)
        paths: list[str] = []
        seen: dict[str, int] = {}
        for node in flat:
            path = node.node_id
            if path in seen:
                seen[path] += 1
                path = "%s#%d" % (path, seen[path])
                node = replace(node, node_id=path)
            else:
                seen[path] = 0
            paths.append(path)
            self._node_by_path[path] = node
            self._parent_by_path[path] = ""
            self._children_by_path[path] = ()
        self._children_by_path[""] = tuple(paths)
        self._children_by_path[self._root_path] = tuple(paths)
        self._node_by_path[""] = tree
        self._node_by_path[self._root_path] = tree
        self._parent_by_path[""] = ""
        self._root_children = tuple(paths)
        # 焦点/选中可能被筛掉了：掉了就清空
        if self._focused_path and self._focused_path not in self._node_by_path:
            self._focused_path = ""
        if self._selection_path and self._selection_path not in self._node_by_path:
            self._selection_path = ""
        # **位置变了就重发**（复核 MF-2 契约）：读屏手里记的是 childId，
        # 行往上/往下挪了必须让它知道「同一个业务行现在叫几号」，否则它会读成另一行。
        if self._focused_path:
            new_id = self.child_id(self._focused_path)
            if old_focus_id and new_id and new_id != old_focus_id:
                self._refire(_EVENT_OBJECT_FOCUS, self._focused_path, new_id)
        if self._selection_path:
            new_id = self.child_id(self._selection_path)
            if old_selection_id and new_id and new_id != old_selection_id:
                self._refire(_EVENT_OBJECT_SELECTION, self._selection_path, new_id)

    def _index(self, node: Node, parent_path: str) -> None:
        path = node.node_id
        self._node_by_path[path] = node
        self._parent_by_path[path] = parent_path
        self._children_by_path[path] = tuple(child.node_id for child in node.children)
        for child in node.children:
            self._index(child, path)

    def node(self, path: str) -> Node | None:
        return self._node_by_path.get(path or self._root_path)

    def children(self, path: str) -> tuple[str, ...]:
        """直接子元素，**按界面顺序**；childId 就是这里的下标 + 1（密集，1..N）。"""
        if path not in ("", self._root_path):
            return ()
        return self._root_children

    # ── 事件 / 日志 ───────────────────────────────────────────────────────────
    def _window_rect(self) -> "tuple[int, int, int, int] | None":
        """窗口屏幕矩形 ``(left, top, right, bottom)``；没有真窗口（Linux/单测）给 None。"""
        rt = self.rt
        if rt is None:
            return None
        try:
            rect = rt.wt.RECT()
            if not rt.user32.GetWindowRect(rt.wt.HWND(self.outer or self.hwnd),
                                           ctypes.byref(rect)):
                return None
            return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
        except Exception:                              # pragma: no cover - 句柄失效
            return None

    def contains_point(self, x: int, y: int) -> bool:
        """这个屏幕坐标是否落在本窗口矩形内（``accHitTest`` 用；拿不到矩形就保守放行）。"""
        rect = self._window_rect()
        if rect is None:
            return True
        left, top, right, bottom = rect
        return left <= x < right and top <= y < bottom

    def focusable_paths(self) -> tuple[str, ...]:
        """可以拿焦点的节点（按读屏遍历顺序）——app 用 Tab 走位时按位置映射。"""
        return tuple(path for path in self._root_children
                     if self._node_by_path[path].state & (STATE_FOCUSABLE | STATE_SELECTABLE))

    def notify_focus(self, path: str) -> None:
        """把焦点挪到某个节点并**发** ``EVENT_OBJECT_FOCUS``（键盘路径 / 客户端 accSelect）。"""
        self._focus(path, _EVENT_OBJECT_FOCUS)

    def notify_focus_at(self, index: int) -> None:
        """按**可聚焦节点的顺序**给第 ``index`` 个发焦点事件。

        app 的 Tab 顺序（``_tab_order``）与 a11y 树的可聚焦顺序都由同一个 ``View`` 的视觉顺序
        生成，所以这里用位置对应；越界就夹到边界，不抛——焦点事件是「锦上添花」，不许影响界面。
        """
        paths = self.focusable_paths()
        if not paths:
            return
        self.notify_focus(paths[max(0, min(int(index), len(paths) - 1))])

    def selectable_paths(self) -> tuple[str, ...]:
        """可被选中的行（表格行/列表项），按读屏遍历顺序。"""
        return tuple(path for path in self._root_children
                     if self._node_by_path[path].state & STATE_SELECTABLE)

    def notify_selection_at(self, index: int) -> None:
        """按**可选中节点的顺序**给第 ``index`` 个发选中事件（↑↓ 换行时调）。"""
        paths = self.selectable_paths()
        if not paths:
            return
        self.select(paths[max(0, min(int(index), len(paths) - 1))])

    def _refire(self, event: int, path: str, child_id: int) -> None:
        """位置变了以后**重发**事件（只发事件，不改任何状态、不动 Tk 焦点）。"""
        self._record_event(event, child_id, path)
        if self.rt is None:
            self._focus_event_id = child_id
            return
        try:
            self.rt.user32.NotifyWinEvent(event, self.rt.wt.HWND(self.outer or self.hwnd),
                                          _OBJID_CLIENT, int(child_id))
        except Exception as exc:                      # pragma: no cover
            self._error("NotifyWinEvent", exc)
        if event == _EVENT_OBJECT_FOCUS:
            self._focus_event_id = child_id
        else:
            self._selection_event_id = child_id

    def select(self, path: str) -> None:
        """选中某行（表格行/列表项）并发 ``EVENT_OBJECT_SELECTION``。"""
        self._selection_path = path
        self._focus(path, _EVENT_OBJECT_SELECTION, keep_focus=False)

    def _record_event(self, event: int, child_id: int, path: str) -> None:
        self._event_log.append((int(event), int(child_id), path))
        del self._event_log[:-200]
        self._trace("NotifyWinEvent", child_id, "event=0x%04X path=%s" % (int(event), path))

    def _focus(self, path: str, event: int, *, keep_focus: bool = True) -> None:
        if path not in self._node_by_path:
            return
        if keep_focus:
            self._focused_path = path
        else:
            self._selection_path = path
        self._record_event(event, self.child_id(path), path)
        if self.rt is None:                 # 没有 COM 也要记账：Linux 上可单测，语义也一致
            return
        try:
            self.rt.user32.NotifyWinEvent(event, self.rt.wt.HWND(self.outer or self.hwnd),
                                          _OBJID_CLIENT, self.child_id(path))
        except Exception as exc:                      # pragma: no cover
            self._error("NotifyWinEvent", exc)

    def child_id(self, path: str) -> int:
        """路径 → childId = 当前 children 顺序里的位置 + 1（根给 0 = CHILDID_SELF）。

        **密集编号**：1..N 一个不空。位置会变，所以「同一行还是同一个号」不是契约——
        契约是「焦点/选中跟着 **path** 走，位置变了就用新号重发事件」（见 ``refresh``）。
        """
        try:
            return self._root_children.index(path) + 1
        except ValueError:
            return 0

    def path_for_id(self, child_id: int) -> str:
        """childId → 路径；越界/非正给空串（与 ``get_accChild`` 的 S_FALSE 一致）。"""
        try:
            index = int(child_id)
        except (TypeError, ValueError):
            return ""
        if 1 <= index <= len(self._root_children):
            return self._root_children[index - 1]
        return ""

    def _nvda_pid(self) -> int:
        """NVDA 进程号（5 秒缓存）——trace 里用它证明「读屏软件当时在跑」。"""
        now = __import__("time").time()
        if now - getattr(self, "_nvda_checked_at", 0.0) > 5.0:
            self._nvda_checked_at = now
            self._nvda_cached = _process_by_exe("nvda.exe")
        return getattr(self, "_nvda_cached", 0)

    def _trace(self, method: str, child: Any, extra: str = "") -> None:
        if not self._trace_path:
            return
        try:
            with open(self._trace_path, "a", encoding="utf-8") as fh:
                fh.write("%s pid=%d fg_exe=%s nvda=%d method=%s child=%s %s\n"
                         % (__import__("time").strftime("%H:%M:%S"), os.getpid(),
                            _foreground_exe(), self._nvda_pid(), method, child, extra))
        except Exception:
            pass

    def _error(self, method: str, exc: BaseException) -> None:
        self._error_count += 1
        if self._error_count <= _MAX_ERROR_LOGS:
            where = ""
            tb = getattr(exc, "__traceback__", None)
            if tb is not None:
                frames = __import__("traceback").extract_tb(tb)
                if frames:
                    last = frames[-1]
                    where = " at %s:%s" % (last.filename.rsplit("\\", 1)[-1], last.lineno)
            log.warning("desktop.a11y.error method=%s error=%r%s", method, exc, where)


def attach(root: Any) -> Provider | None:
    """装上 provider；任何失败都返回 ``None``（界面照常跑）。"""
    if not available():
        log.info("desktop.a11y.disabled reason=platform_or_escape_hatch platform=%s",
                 sys.platform)
        return None
    provider = Provider(root)
    return provider if provider.attach() else None


# ── COM 对象 ──────────────────────────────────────────────────────────────────
def _build_node_object(rt: _Runtime) -> Any:  # pragma: no cover - Windows only
    """按运行时构造 COM 对象类（每个节点一个实例，按 ``node_id`` 复用）。"""
    E_FAIL, E_NOTIMPL, BADINDEX = _E_FAIL, _E_NOTIMPL, _DISP_E_BADINDEX

    class _NodeObject(rt.COMObject):
        _com_interfaces_ = [rt.IAccessibleV, rt.IDispatch]

        def __init__(self, provider: Provider, path: str) -> None:
            super().__init__()
            self._provider = provider
            self._path = path
            # IAccessible 是 dual interface：同一个指针既是 IAccessible 也是 IDispatch。
            # comtypes 按 **IID** 记接口指针，这里把 IAccessible 的指针也登记到 IDispatch 的
            # IID 下——否则 ``QueryInterface(IDispatch)`` 会回 E_NOINTERFACE（_com_pointers_
            # 里根本没有那个 IID），``get_accChild`` 就交不出子对象。
            try:
                pointer = self._com_pointers_.get(rt.IAccessibleV._iid_)
                if pointer is not None:
                    self._com_pointers_[rt.IDispatch._iid_] = pointer
            except Exception:
                pass

        # ── 内部 ──
        def _node(self):
            return self._provider.node(self._path)

        def _child_path(self, var_child) -> str:
            index = _child_index(var_child)
            if index <= 0:
                return self._path
            return self._provider.path_for_id(index)       # 身份映射，不是位置

        def _child_node(self, var_child):
            path = self._child_path(var_child)
            return self._provider.node(path) if path else None

        def _child_object(self, path: str):
            provider = self._provider
            obj = provider._objects.get(path)
            if obj is None:
                obj = provider._node_class(provider, path)
                provider._objects[path] = obj
            return obj

        # ── IAccessible ──
        def get_accParent(self, ppdispParent):
            try:
                ppdispParent[0] = None
                return 0
            except Exception:
                return E_FAIL

        def get_accChildCount(self, pcount):
            try:
                count = len(self._provider.children(self._path))    # 密集：1..count 一个不空
                pcount[0] = count
                self._provider._trace("accChildCount", 0, "count=%d" % count)
                return 0
            except Exception as exc:
                self._provider._error("accChildCount", exc)
                return E_FAIL

        def get_accChild(self, varChild, ppdispChild):
            """本版全是**简单子元素**：交回 NULL + ``S_FALSE``，客户端用 childId 取属性。

            ``S_FALSE``（不是 E_FAIL！）是 MSAA 的约定：E_FAIL 会让客户端以为这个子元素坏了，
            NVDA 真机实测就**直接跳过整棵树**（trace 里只有 51 次 accChild、一条 accName 都没有）。
            跨进程交不出子对象的原因见 ``tree.flatten_for_provider`` 的说明。
            """
            try:
                ppdispChild[0] = None
                # 0 = 自己 / 有效子元素 / 超界 / 负数：**一律** S_FALSE + NULL。
                # MSAA 约定就是「这个 id 没有子对象」，客户端据此改用 childId 取属性；
                # 只有真出异常才 E_FAIL（E_FAIL 会让 NVDA 认为整棵树坏了）。
                return S_FALSE
            except Exception as exc:
                self._provider._error("accChild", exc)
                return E_FAIL

        def get_accName(self, varChild, pszName):
            try:
                index = _child_index(varChild)
                node = self._node() if index == 0 else self._child_node(varChild)
                name = node.name if node else ""
                pszName[0] = rt.bstr(name)
                self._provider._trace("accName", index, "len=%d" % len(name))
                return 0 if node else BADINDEX
            except Exception as exc:
                self._provider._error("accName", exc)
                return E_FAIL

        def get_accValue(self, varChild, pszValue):
            try:
                index = _child_index(varChild)
                node = self._node() if index == 0 else self._child_node(varChild)
                value = node.value if node else ""
                # trace 只记长度：金额/地址/节点 ID 不进磁盘
                self._provider._trace("accValue", index, "len=%d" % len(value))
                pszValue[0] = rt.bstr(value)
                return 0 if node else BADINDEX
            except Exception as exc:
                self._provider._error("accValue", exc)
                return E_FAIL

        def get_accDescription(self, varChild, pszDescription):
            try:
                index = _child_index(varChild)
                node = self._node() if index == 0 else self._child_node(varChild)
                pszDescription[0] = rt.bstr(node.description if node else "")
                return 0 if node else BADINDEX
            except Exception as exc:
                self._provider._error("accDescription", exc)
                return E_FAIL

        def get_accRole(self, varChild, pvarRole):
            try:
                index = _child_index(varChild)
                node = self._node() if index == 0 else self._child_node(varChild)
                rt.int_variant(pvarRole, node.role if node else 0)
                self._provider._trace("accRole", index, "ok")
                return 0 if node else BADINDEX
            except Exception as exc:
                self._provider._error("accRole", exc)
                return E_FAIL

        def get_accState(self, varChild, pvarState):
            try:
                index = _child_index(varChild)
                node = self._node() if index == 0 else self._child_node(varChild)
                bits = node.state if node else 0
                if node is not None and node.node_id == self._provider._selection_path:
                    bits |= STATE_SELECTED
                elif bits & STATE_SELECTABLE:
                    bits &= ~STATE_SELECTED            # 只报「这一个」被选中，不虚报
                rt.int_variant(pvarState, bits)
                self._provider._trace("accState", index, "state=%d" % bits)
                return 0 if node else BADINDEX
            except Exception as exc:
                self._provider._error("accState", exc)
                return E_FAIL

        def get_accHelp(self, varChild, pszHelp):
            try:
                pszHelp[0] = rt.bstr("")
                return 0
            except Exception:
                return E_FAIL

        def get_accHelpTopic(self, pszHelpFile, varChild, pidTopic):
            try:
                pszHelpFile[0] = rt.bstr("")
                pidTopic[0] = 0
                return 0
            except Exception:
                return E_FAIL

        def get_accKeyboardShortcut(self, varChild, psz):
            try:
                psz[0] = rt.bstr("")
                return E_NOTIMPL
            except Exception:
                return E_FAIL

        def get_accFocus(self, pvarChild):
            try:
                provider = self._provider
                focused = provider._focused_path
                if not focused:
                    rt.empty_variant(pvarChild)
                    return 0
                if focused == self._path:
                    rt.int_variant(pvarChild, 0)          # CHILDID_SELF
                    self._provider._trace("accFocus", 0, "self")
                    return 0
                # 焦点不在自己身上：只有当它是自己的直接子节点时才报给客户端
                kids = provider.children(self._path)
                if focused in kids:
                    rt.int_variant(pvarChild, kids.index(focused) + 1)
                    self._provider._trace("accFocus", kids.index(focused) + 1, "")
                else:
                    rt.empty_variant(pvarChild)
                return 0
            except Exception as exc:
                self._provider._error("accFocus", exc)
                return E_FAIL

        def get_accSelection(self, pvarChildren):
            try:
                rt.empty_variant(pvarChildren)
                return 0
            except Exception:
                return E_FAIL

        def get_accDefaultAction(self, varChild, psz):
            try:
                node = self._child_node(varChild)
                # S2 只有按钮有默认动作文案；真正触发在 S3（这里如实返回 E_NOTIMPL 的空实现）
                if node is not None and node.role == 0x2B:
                    psz[0] = rt.bstr("按下")
                    return 0
                psz[0] = rt.bstr("")
                return E_NOTIMPL
            except Exception:
                return E_FAIL

        def accSelect(self, flagsSelect, varChild):
            """``SELFLAG_TAKEFOCUS`` / ``SELFLAG_TAKESELECTION``：客户端（读屏）要求把焦点/选中
            挪到某个子元素。只改**记录 + 发事件**，不动 Tk 控件——本版 a11y 是只读镜像，
            真正搬 Tk 焦点由键盘路径负责（边界见票面 §6.4）。"""
            try:
                provider = self._provider
                index = _child_index(varChild)
                if index == 0:
                    return S_FALSE
                path = self._child_path(varChild)
                node = provider.node(path) if path else None
                if node is None:
                    return E_INVALIDARG
                if not flagsSelect & (_SELFLAG_TAKEFOCUS | _SELFLAG_TAKESELECTION):
                    return S_FALSE
                self._provider._trace("accSelect", index, "flags=%d" % int(flagsSelect))
                if flagsSelect & _SELFLAG_TAKEFOCUS:
                    provider.notify_focus(path)
                if flagsSelect & _SELFLAG_TAKESELECTION:
                    provider.select(path)
                return 0
            except Exception as exc:
                self._provider._error("accSelect", exc)
                return E_FAIL

        def accLocation(self, pxLeft, pyTop, pcxWidth, pcyHeight, varChild):
            """**要么给对，要么不给**：暂时只有页根能给出真实矩形，其余一律 E_NOTIMPL。"""
            try:
                if _child_index(varChild) != 0 or self._path:
                    return E_NOTIMPL
                root = self._provider.root
                pxLeft[0] = int(root.winfo_rootx())
                pyTop[0] = int(root.winfo_rooty())
                pcxWidth[0] = int(root.winfo_width())
                pcyHeight[0] = int(root.winfo_height())
                return 0
            except Exception as exc:
                self._provider._error("accLocation", exc)
                return E_FAIL

        def accNavigate(self, navDir, varStart, pvarEndUpAt):
            try:
                kids = self._provider.children(self._path)
                if not kids:
                    rt.empty_variant(pvarEndUpAt)
                    return 0
                if navDir in (7, 9):        # NAVDIR_FIRSTCHILD / DOWN
                    rt.int_variant(pvarEndUpAt, 1)
                elif navDir in (8, 10):     # NAVDIR_LASTCHILD / UP
                    rt.int_variant(pvarEndUpAt, len(kids))
                else:
                    rt.empty_variant(pvarEndUpAt)
                return 0
            except Exception as exc:
                self._provider._error("accNavigate", exc)
                return E_FAIL

        def accHitTest(self, xLeft, yTop, pvarChild):
            """坐标落在窗口外 → ``E_INVALIDARG``；窗内 → 报根（``CHILDID_SELF``）。

            以前无条件回 SELF：读屏点窗口外一屏空白也得到「这是本窗口」，坐标定位等于没用
            （复核 SHOULD-FIX 2）。窗内不编造子节点——本版是压平模型，命中具体行需要嵌套，
            见 `CHG-20261116-CLIENT-A11Y-NESTED-P2.md`。
            """
            try:
                if self._provider.rt is not None and not self._provider.contains_point(
                        int(xLeft), int(yTop)):
                    return E_INVALIDARG
                rt.int_variant(pvarChild, 0)
                return 0
            except Exception as exc:
                self._provider._error("accHitTest", exc)
                return E_FAIL

        def accDoDefaultAction(self, varChild):
            """按钮/开关的触发要 PostMessage 回 Tk 主线程，本版明确不支持（不假装能点）。"""
            try:
                return E_NOTIMPL
            except Exception as exc:                      # pragma: no cover - 契约：不许跨 COM 边界抛
                self._provider._error("accDoDefaultAction", exc)
                return E_FAIL

        def put_accName(self, varChild, szName):
            try:
                return E_NOTIMPL                          # 只读镜像：改名一律不接
            except Exception as exc:                      # pragma: no cover - 契约：不许跨 COM 边界抛
                self._provider._error("put_accName", exc)
                return E_FAIL

        def put_accValue(self, varChild, szValue):
            try:
                return E_NOTIMPL                          # 只读镜像：改值一律不接
            except Exception as exc:                      # pragma: no cover - 契约：不许跨 COM 边界抛
                self._provider._error("put_accValue", exc)
                return E_FAIL

        # ── IDispatch：交回 IAccessible 的型别信息（代理/桩要用） ──
        def GetTypeInfoCount(self, pctinfo):
            try:
                pctinfo[0] = 1
                return 0
            except Exception:
                return E_FAIL

        def GetTypeInfo(self, iTInfo, lcid, ppTInfo):
            try:
                if iTInfo != 0:
                    return BADINDEX
                ppTInfo[0] = rt.typeinfo
                return 0
            except Exception:
                return E_FAIL

        def GetIDsOfNames(self, riid, rgszNames, cNames, lcid, rgDispId):
            try:
                return rt.typeinfo.GetIDsOfNames(riid, rgszNames, cNames, lcid, rgDispId)
            except Exception:
                return E_FAIL

    return _NodeObject
