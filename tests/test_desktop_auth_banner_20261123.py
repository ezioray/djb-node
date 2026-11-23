# SPDX-License-Identifier: Apache-2.0
"""R2.1 ①：401 不再是死路 —— 顶部横幅 +「重新登录」+ 登录后回原页。

来源：`CHG-20261123-R2-1-NODE-QUICKWINS` ①；报告 P0-1（真机：登录过期后全站 401，
界面只有一句「暂不可用（登录已过期）」，没有任何可点的下一步）。

四条断言线：

1. **数据层**：401/403 的 ``ProviderError`` → ``Unavailable(auth_required=True)``，
   ``Snapshot.login_required()`` 能回答「哪个域在喊登录过期」；
2. **动作层**：``auth.relogin`` 只派发（``effect="relogin"``），能自动绑定优先设备码；
3. **窗口层**：横幅默认不占位、有 401 才出现；点「重新登录」记下原页；
4. **回原页**：登录成功后回到原来那一页（不是把客户丢到账户页）。
"""
from __future__ import annotations

import pytest

from djb_node.desktop import app as desktop_app
from djb_node.desktop.controller import ActionResult, DesktopController
from djb_node.desktop.datasource import (FakeDataSource, ProviderError, Snapshot, Unavailable)
from tests import tk_support

AUTH_REASON = "登录已过期（401）"
AUTH_HINT = "点顶部「重新登录」，或在「账户与安全」重新绑定这台电脑"


def _sections(**overrides):
    """一份「其它都正常、只有指定域 401」的数据形状（够渲染 13 页的最小集）。"""
    data = {name: {} for name in ("status", "bench", "gpu", "models", "tasks", "receipts",
                                  "earnings", "agent", "token_plan", "access", "yield",
                                  "account", "logs")}
    data.update(overrides)
    return data


# ── 1. 数据层：401 是整窗状态，不是一个域的事
def test_unavailable_carries_auth_flag_and_snapshot_answers_login_required():
    snap = Snapshot({"tasks": Unavailable(AUTH_REASON, AUTH_HINT, auth_required=True),
                     "receipts": Unavailable("云端连不上（timeout）")})
    assert snap.needs_login is True
    assert snap.login_required() == ("tasks", AUTH_REASON)
    # 普通不可用（网络/未上线）不许冒充「登录过期」：否则横幅会天天挂着
    plain = Snapshot({"tasks": Unavailable("云端连不上（timeout）")})
    assert plain.needs_login is False and plain.login_required() == ("", "")


def test_provider_error_defaults_to_not_auth():
    assert ProviderError("云端连不上").auth_required is False
    assert ProviderError("登录已过期（401）", hint=AUTH_HINT,
                         auth_required=True).auth_required is True


def test_unavailable_dict_keeps_auth_flag_for_evidence():
    payload = Unavailable(AUTH_REASON, AUTH_HINT, auth_required=True).to_dict()
    assert payload["auth_required"] is True and payload["status"] == "unavailable"


# ── 2. 动作层：只派发，不在主线程里跑网络
def test_relogin_action_prefers_device_flow(monkeypatch):
    controller = DesktopController(FakeDataSource(_sections()))
    monkeypatch.setattr(controller, "device_flow_enabled", lambda: True)
    result = controller.action("auth.relogin")
    assert result.ok and result.effect == "relogin" and result.payload == "device"
    assert "原来的页面" in result.message


def test_relogin_action_falls_back_to_bind_code(monkeypatch):
    controller = DesktopController(FakeDataSource(_sections()))
    monkeypatch.setattr(controller, "device_flow_enabled", lambda: False)
    result = controller.action("auth.relogin")
    assert result.ok and result.effect == "relogin" and result.payload == "code"
    # 不再把客户甩到终端敲 djb auth --pair（报告 P0-1 的原话）
    assert "auth --pair" not in result.message


# ── 3/4. 窗口层：横幅显隐 + 回原页（真窗口；无显示则 skip）
def _app(sections):
    root = tk_support.new_root()
    controller = DesktopController(FakeDataSource(sections, source="test"))
    app = desktop_app.DesktopApp(controller, root=root)
    root.update_idletasks()
    root.update()
    return app, root


@tk_support.needs_tk
def test_banner_is_hidden_until_a_domain_reports_401():
    app, root = _app(_sections())
    try:
        app.show_view("overview")
        root.update()
        assert bool(app._auth_banner.winfo_ismapped()) is False   # 默认不占高度
    finally:
        try:
            app.stop()                      # 它会连同 root 一起销毁
        except Exception:                   # pragma: no cover - 窗口已销毁
            pass
        try:
            root.destroy()
        except Exception:                   # pragma: no cover - app.stop() 已经销毁
            pass


@tk_support.needs_tk
def test_banner_appears_with_reason_and_relogin_button():
    app, root = _app(_sections(tasks=Unavailable(AUTH_REASON, AUTH_HINT,
                                                auth_required=True)))
    try:
        app.show_view("overview")
        root.update()
        assert bool(app._auth_banner.winfo_ismapped()) is True
        text = app._auth_text.cget("text")
        assert "登录已过期" in text and AUTH_REASON in text
        assert "本机数据不受影响" in text
        assert app._auth_button.text == "重新登录"
    finally:
        try:
            app.stop()                      # 它会连同 root 一起销毁
        except Exception:                   # pragma: no cover - 窗口已销毁
            pass
        try:
            root.destroy()
        except Exception:                   # pragma: no cover - app.stop() 已经销毁
            pass


@tk_support.needs_tk
def test_relogin_remembers_the_page_and_returns_after_success(monkeypatch):
    app, root = _app(_sections(tasks=Unavailable(AUTH_REASON, AUTH_HINT,
                                                auth_required=True)))
    try:
        app.show_view("receipts")
        root.update()
        monkeypatch.setattr(app._controller, "device_flow_enabled", lambda: False)
        asked: list[str] = []
        app._controller.ask_code = lambda: asked.append("asked") or None   # 取消输入
        app._on_relogin()
        root.update()
        assert app._return_view == "receipts"          # 记住原页
        assert app._controller.view == "receipts"      # 没有把客户甩到账户页
        # 登录成功（设备码/绑定码两条路最终都走这里）→ 回原页
        app.show_view("account")
        app._resume_after_login(True)
        root.update()
        assert app._controller.view == "receipts"
        assert app._return_view == ""
    finally:
        try:
            app.stop()                      # 它会连同 root 一起销毁
        except Exception:                   # pragma: no cover - 窗口已销毁
            pass
        try:
            root.destroy()
        except Exception:                   # pragma: no cover - app.stop() 已经销毁
            pass


@tk_support.needs_tk
def test_failed_login_keeps_the_page_and_the_banner(monkeypatch):
    app, root = _app(_sections(tasks=Unavailable(AUTH_REASON, AUTH_HINT,
                                                auth_required=True)))
    try:
        app.show_view("earnings")
        root.update()
        app._return_view = "earnings"
        app._apply_device(ActionResult("绑定没成功：短码过期", ok=False))
        root.update()
        assert app._controller.view == "earnings"      # 留在原地
        assert app._return_view == "earnings"          # 下次成功还能回
        assert bool(app._auth_banner.winfo_ismapped()) is True   # 横幅不许消失（不是已登录）
    finally:
        try:
            app.stop()                      # 它会连同 root 一起销毁
        except Exception:                   # pragma: no cover - 窗口已销毁
            pass
        try:
            root.destroy()
        except Exception:                   # pragma: no cover - app.stop() 已经销毁
            pass


if __name__ == "__main__":                             # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
