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
4. **刷新不发事件**：:meth:`Provider.refresh` 只换数据，**不发** ``EVENT_OBJECT_FOCUS/SELECTION``；
   只有用户操作（S3）才发。节点对象按 ``node_id`` 复用，刷新前后同一行还是同一个 IAccessible。

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
from typing import Any

from ..views import View
from .tree import Node, build_tree

__all__ = ["ENV_DISABLE", "ENV_TRACE", "Provider", "attach", "available"]

log = logging.getLogger("djb_node.desktop.a11y")

#: 故障逃生阀：置 ``0`` 就不装 provider（不是特性开关，是「读屏把界面拖死」时的应急阀）
ENV_DISABLE = "DJB_DESKTOP_A11Y"
#: 调试调用日志路径；不设就**一行都不写**
ENV_TRACE = "DJB_DESKTOP_A11Y_TRACE"

_E_FAIL = -2147467259            # 0x80004005
_E_NOTIMPL = -2147467263         # 0x80004001
_DISP_E_BADINDEX = -2147352572   # 0x80020004
_OBJID_CLIENT = -4
_WM_GETOBJECT = 0x003D
_EVENT_OBJECT_FOCUS = 0x8005

#: 一次刷新里最多报几条异常（防止读屏狂刷把日志撑爆）
_MAX_ERROR_LOGS = 20


def available() -> bool:
    """当前环境能不能装 provider（不 import comtypes，只看平台与逃生阀）。"""
    if os.environ.get(ENV_DISABLE, "1").strip() == "0":
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
        """换一页/一版数据。**不发任何焦点事件**（自动刷新不能把读屏的焦点顶走）。"""
        if self.rt is None:
            return
        try:
            tree = build_tree(view) if max_rows is None else build_tree(view, max_rows=max_rows)
        except Exception as exc:                      # pragma: no cover - 防御
            self._error("build_tree", exc)
            return
        self._tree = tree
        self._node_by_path = {}
        self._children_by_path = {}
        self._parent_by_path = {}
        self._index(tree, "")
        # 焦点所在节点可能被筛掉了：掉了就回根，并记一条（不发事件）
        if self._focused_path and self._focused_path not in self._node_by_path:
            self._focused_path = ""

    def _index(self, node: Node, parent_path: str) -> None:
        path = node.node_id
        self._node_by_path[path] = node
        self._parent_by_path[path] = parent_path
        self._children_by_path[path] = tuple(child.node_id for child in node.children)
        for child in node.children:
            self._index(child, path)

    def node(self, path: str) -> Node | None:
        return self._node_by_path.get(path)

    def children(self, path: str) -> tuple[str, ...]:
        return self._children_by_path.get(path, ())

    # ── 事件 / 日志 ───────────────────────────────────────────────────────────
    def notify_focus(self, path: str) -> None:
        """S3 用：把焦点挪到某个节点并**发** ``EVENT_OBJECT_FOCUS``。S2 不调用。"""
        if self.rt is None or path not in self._node_by_path:
            return
        self._focused_path = path
        try:
            self.rt.user32.NotifyWinEvent(_EVENT_OBJECT_FOCUS,
                                          self.rt.wt.HWND(self.outer or self.hwnd),
                                          _OBJID_CLIENT, self.child_id(path))
        except Exception as exc:                      # pragma: no cover
            self._error("NotifyWinEvent", exc)

    def child_id(self, path: str) -> int:
        """路径 → 它**在父节点里的** childId（1 起）；根给 0（CHILDID_SELF）。"""
        parent = self._parent_by_path.get(path, "")
        if not parent:
            return 0
        siblings = self._children_by_path.get(parent, ())
        try:
            return siblings.index(path) + 1
        except ValueError:
            return 0

    def _trace(self, method: str, child: Any, extra: str = "") -> None:
        if not self._trace_path:
            return
        try:
            with open(self._trace_path, "a", encoding="utf-8") as fh:
                fh.write("%s pid=%d method=%s child=%s %s\n"
                         % (__import__("time").strftime("%H:%M:%S"), os.getpid(), method,
                            child, extra))
        except Exception:
            pass

    def _error(self, method: str, exc: BaseException) -> None:
        self._error_count += 1
        if self._error_count <= _MAX_ERROR_LOGS:
            log.warning("desktop.a11y.error method=%s error=%r", method, exc)


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

        # ── 内部 ──
        def _node(self):
            return self._provider.node(self._path)

        def _child_path(self, var_child) -> str:
            index = _child_index(var_child)
            if index <= 0:
                return self._path
            kids = self._provider.children(self._path)
            if 1 <= index <= len(kids):
                return kids[index - 1]
            return ""

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
                pcount[0] = len(self._provider.children(self._path))
                return 0
            except Exception as exc:
                self._provider._error("accChildCount", exc)
                return E_FAIL

        def get_accChild(self, varChild, ppdispChild):
            try:
                node = self._child_node(varChild)
                ppdispChild[0] = None
                if node is None:
                    return 0 if _child_index(varChild) == 0 else E_FAIL
                if _child_index(varChild) == 0:
                    return 0
                child = self._child_object(self._child_path(varChild))
                ppdispChild[0] = child.QueryInterface(rt.IDispatch)
                return 0
            except Exception as exc:
                self._provider._error("accChild", exc)
                return E_FAIL

        def get_accName(self, varChild, pszName):
            try:
                index = _child_index(varChild)
                node = self._node() if index == 0 else self._child_node(varChild)
                pszName[0] = rt.bstr(node.name if node else "")
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
                return 0 if node else BADINDEX
            except Exception as exc:
                self._provider._error("accRole", exc)
                return E_FAIL

        def get_accState(self, varChild, pvarState):
            try:
                index = _child_index(varChild)
                node = self._node() if index == 0 else self._child_node(varChild)
                rt.int_variant(pvarState, node.state if node else 0)
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
                    return 0
                # 焦点不在自己身上：只有当它是自己的直接子节点时才报给客户端
                kids = provider.children(self._path)
                if focused in kids:
                    rt.int_variant(pvarChild, kids.index(focused) + 1)
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
            # S3 实现 SELFLAG_TAKEFOCUS | SELFLAG_TAKESELECTION；S2 明确不支持
            return E_NOTIMPL

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
            try:
                rt.int_variant(pvarChild, 0)     # 保守：报「就是我自己」，绝不编造子节点
                return 0
            except Exception:
                return E_FAIL

        def accDoDefaultAction(self, varChild):
            # S3：按钮/开关的触发要 PostMessage 回 Tk 主线程，S2 明确不支持
            return E_NOTIMPL

        def put_accName(self, varChild, szName):
            return E_NOTIMPL

        def put_accValue(self, varChild, szValue):
            return E_NOTIMPL

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
