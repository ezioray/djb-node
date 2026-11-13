# SPDX-License-Identifier: Apache-2.0
"""S4 回归：复制节点 ID「点了没反应 / 粘出来是旧的」——剪贴板要真的写进去 + 有反馈。

真机两个独立缺陷叠在一起：

1. ``clipboard_append`` 之后**没有** ``update()``：Tk 只是把内容登记在本进程，
   剪贴板所有权要等事件循环转一圈才转移；客户粘出来是旧内容（或空）；
2. ``_copy`` 用 ``except Exception: pass`` 吞掉失败，且动作结果的 ``effect="copy"``
   **不在** toast 名单里 → 成功失败都没有任何提示（客户原话：「点了没反应」）。

这里用假 root 直接测真方法（不需要显示器）：
``copy_text`` 必须调用 ``update()``；失败必须返回 False；``_on_action`` 必须 toast。
"""
from __future__ import annotations

import pytest

from djb_node.desktop import app as desktop_app
from djb_node.desktop.controller import ActionResult


class FakeRoot:
    def __init__(self, *, fail_on: str = "") -> None:
        self.text = ""
        self.updates = 0
        self.cleared = 0
        self._fail_on = fail_on

    def clipboard_clear(self) -> None:
        if self._fail_on == "clear":
            raise RuntimeError("clipboard busy")
        self.cleared += 1
        self.text = ""

    def clipboard_append(self, text: str) -> None:
        if self._fail_on == "append":
            raise RuntimeError("clipboard busy")
        self.text += text

    def update(self) -> None:
        self.updates += 1


def _bare_app(root=None) -> desktop_app.DesktopApp:
    """绕过 Tk 构造：只装配这一条路径需要的属性。"""
    app = desktop_app.DesktopApp.__new__(desktop_app.DesktopApp)
    app._root = root if root is not None else FakeRoot()
    return app


def test_copy_text_writes_and_forces_event_loop_turn():
    app = _bare_app()
    assert app.copy_text("nd_abc123") is True
    assert app._root.text == "nd_abc123"      # noqa: SLF001 - 测试读假 root 状态
    assert app._root.updates == 1, "没有 update()：真机上剪贴板所有权不会转移，粘出来是旧的"


def test_copy_text_reports_failure_instead_of_swallowing(monkeypatch):
    events = []
    monkeypatch.setattr("djb_node.logging_setup.event",
                        lambda name, **fields: events.append((name, fields)), raising=False)
    app = _bare_app(FakeRoot(fail_on="append"))

    assert app.copy_text("nd_abc123") is False
    assert events and events[0][0] == "desktop.copy_failed"
    assert events[0][1].get("error") == "RuntimeError"


class _Status:
    def __init__(self) -> None:
        self.text = ""

    def configure(self, **kwargs) -> None:
        self.text = str(kwargs.get("text") or "")


def _app_for_action(result: ActionResult, root=None) -> desktop_app.DesktopApp:
    app = _bare_app(root)

    class _Controller:
        def action(self, key, confirm=None):
            return result

    app._controller = _Controller()                     # noqa: SLF001
    app._vars = {}                                      # noqa: SLF001
    app._status = _Status()                             # noqa: SLF001
    app._update_status = lambda: None                   # noqa: SLF001 - 状态条重画与本用例无关
    return app


def test_copy_action_toasts_success():
    """成功必须说一句（旧实现 effect="copy" 不在 toast 名单里 → 零反馈）。"""
    app = _app_for_action(ActionResult("已复制节点 ID：nd_abc", effect="copy",
                                      payload="nd_abc"))
    app._on_action("access.copy_node_id")               # noqa: SLF001
    assert app._root.text == "nd_abc"                   # noqa: SLF001
    assert "已复制节点 ID" in app._status.text           # noqa: SLF001


def test_copy_action_toasts_failure_when_clipboard_rejects():
    app = _app_for_action(ActionResult("已复制节点 ID：nd_abc", effect="copy",
                                       payload="nd_abc"),
                          root=FakeRoot(fail_on="append"))
    app._on_action("access.copy_node_id")               # noqa: SLF001
    assert "复制失败" in app._status.text                # noqa: SLF001
    assert not app._status.text.startswith("✓")          # 失败不能打勾
