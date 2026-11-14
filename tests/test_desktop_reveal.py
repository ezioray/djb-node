# SPDX-License-Identifier: Apache-2.0
"""RC14 G7：密码框右侧「显示 / 隐藏」（客户反馈：看不见就改不了）。

客户场景：粘贴一长串密钥，少一位 → 保存后只看到「401 / 密钥被拒」，输入框是一串黑点，
除了整段重粘没法核对。这里给的补救只有一件事：**看得见**。

硬约束：
* 默认 ``secret=True`` 就可显示（``revealable=None`` 跟着 ``secret`` 走）；
* 显示**只**切 Entry 的 ``show`` 选项——不复制剪贴板、不写日志、不进快照；
* 只读掩码字段（值本来就不在本地）没有按钮，不给假承诺。
"""
from __future__ import annotations

import logging

import pytest

from djb_node.desktop import app as desktop_app
from djb_node.desktop import views as vw
from djb_node.desktop.controller import DesktopController
from djb_node.desktop.datasource import FakeDataSource
from tests.tk_support import needs_tk

SECRET = "sk-live-9f2c41aa77be4d0fa1"


def _view() -> vw.View:
    """造一页带秘密字段的视图（hidden 字段也要能被点开验证）。"""
    return vw.View(
        key="access", title="本机设置",
        fields=(vw.Field("请求密钥", SECRET, key="upstream_secret", editable=True,
                         secret=True, hint="粘贴新值才会覆盖"),
                vw.Field("只读掩码", "sk-…4d0f", key="readonly_secret", secret=True),
                vw.Field("价格模式", "auto", key="price_mode", editable=True)),
        actions=(vw.Action("access.save", "保存并回读", primary=True),))


def _app() -> tuple[object, vw.View]:
    controller = DesktopController(FakeDataSource(dict(desktop_app.demo_snapshot().sections)),
                                   cli=None)
    app = desktop_app.DesktopApp(controller)
    view = _view()
    app._render(view)                                  # noqa: SLF001 - 直接渲染造好的视图
    app._root.update()
    return app, view


def test_field_defaults_to_revealable_only_for_editable_secrets():
    assert vw.Field("k", "v", key="a", editable=True, secret=True).can_reveal() is True
    # 只读的掩码字段：值不在本地，给按钮就是假承诺
    assert vw.Field("k", "v", key="b", secret=True).can_reveal() is False
    # 非秘密字段不画
    assert vw.Field("k", "v", key="c", editable=True).can_reveal() is False
    # 显式关掉（某些运维口令即便可编辑也不许在屏幕上显示）
    assert vw.Field("k", "v", key="d", editable=True, secret=True,
                    revealable=False).can_reveal() is False


@needs_tk
def test_reveal_button_sits_next_to_the_input_and_toggles_the_mask():
    app, _ = _app()
    try:
        button = app._reveal_buttons["upstream_secret"]
        entry = app._entry_for("upstream_secret")
        assert entry.cget("show") == "•", "秘密字段默认必须是掩码"
        assert button.text == "显示"
        button._on_click()                             # noqa: SLF001 - 等价于点一下
        assert entry.cget("show") == "", "点了「显示」还是掩码"
        assert button.text == "隐藏"
        button._on_click()                             # noqa: SLF001
        assert entry.cget("show") == "•", "「隐藏」没有恢复掩码"
        assert button.text == "显示"
        # 秘密字段不预填（粘贴才会覆盖）：刚渲染时输入框是空的，值只在 hint 里显示掩码
        assert app._vars["upstream_secret"].get() == ""
        # 「显示」只影响掩码，**不改**输入框内容：粘进来的那串原样保留（保存路径读到同一串）
        app._vars["upstream_secret"].set(SECRET)
        button._on_click()                             # noqa: SLF001 - 显示
        assert app._vars["upstream_secret"].get() == SECRET
        assert entry.cget("show") == ""
    finally:
        app.stop()


@needs_tk
def test_readonly_and_plain_fields_have_no_reveal_button():
    app, _ = _app()
    try:
        assert "readonly_secret" not in app._reveal_buttons
        assert "price_mode" not in app._reveal_buttons
        assert set(app._reveal_buttons) == {"upstream_secret"}
    finally:
        app.stop()


@needs_tk
def test_revealing_never_logs_or_copies_the_secret(caplog, monkeypatch):
    """显示/隐藏是一次纯 UI 切换：日志里不许出现密钥，剪贴板也不许被写。"""
    copied: list[str] = []
    app, _ = _app()
    try:
        monkeypatch.setattr(app, "copy_text", lambda text: copied.append(str(text)) or True,
                            raising=False)
        with caplog.at_level(logging.DEBUG):
            app._reveal_buttons["upstream_secret"]._on_click()   # noqa: SLF001
            app._reveal_buttons["upstream_secret"]._on_click()   # noqa: SLF001
        assert copied == [], "显示密码不许碰剪贴板"
        assert SECRET not in caplog.text
        assert "sk-live" not in caplog.text
    finally:
        app.stop()


@needs_tk
def test_reveal_state_does_not_leak_into_the_next_render():
    """换页再回来 = 重新掩码（避免「上次谁点开过」留在屏幕上）。"""
    app, view = _app()
    try:
        app._reveal_buttons["upstream_secret"]._on_click()       # noqa: SLF001
        assert app._entry_for("upstream_secret").cget("show") == ""
        app._render(view)                              # noqa: SLF001
        app._root.update()
        assert app._entry_for("upstream_secret").cget("show") == "•"
        assert app._reveal_buttons["upstream_secret"].text == "显示"
    finally:
        app.stop()


def test_plain_text_never_contains_the_raw_secret():
    """文本快照（证据/无显示环境）里秘密字段也只能是掩码。"""
    view = vw.View(key="access", title="本机设置",
                   fields=(vw.Field("请求密钥", "sk-…4d0f", key="upstream_secret",
                                    secret=True),))
    assert "sk-…4d0f" in view.plain_text()
    assert SECRET not in view.plain_text()
