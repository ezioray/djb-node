# SPDX-License-Identifier: Apache-2.0
"""D2 + D4（2026-11-18 客户报障）：版本如实 + 失败提示不许被刷新吃掉。

真机现象（报障原文）：「点 OK 没反应，客户端版本 0.3.11，绑定页面显示 0.3.0」。

* **D4**：``_toast(ok=False)`` 只写状态条，``_update_status()``（每轮刷新都调）随即用
  ``_status_line()`` 覆写 ⇒ 错误行活不过 10 秒。本文件钉住「失败粘住、成功即清、
  换页/新动作才退场」。
* **D2**：版本卡读 ``update.status()["current_version"]``（= 安装记录），手工解压换包
  不写记录 ⇒ 侧栏 v0.3.11 与卡面 0.3.0 自相矛盾。本文件钉住「卡面写程序版本」+
  「启动自检把记录对齐到程序版本」。
"""
from __future__ import annotations

from typing import Any

import pytest

from djb_node import facts
from djb_node.desktop import app as desktop_app
from djb_node.desktop import controller as desktop_controller
from djb_node.desktop.controller import DesktopController
from djb_node.desktop.views import build_views
from djb_node.desktop import datasource as ds
from djb_node.desktop.datasource import FakeDataSource, ProviderError
from djb_node.update import UpdateManager


class _Label:
    """假状态栏 / 右侧标签（``configure(**kwargs)`` 就是它们唯一会收到的调用）。"""

    def __init__(self) -> None:
        self.text = ""
        self.foreground = ""
        self.calls: list[dict[str, Any]] = []

    def configure(self, **kwargs: Any) -> None:
        self.calls.append(dict(kwargs))
        if "text" in kwargs:
            self.text = str(kwargs.get("text") or "")
        if "foreground" in kwargs:
            self.foreground = str(kwargs.get("foreground") or "")


def _bare_app(status_line: str = "已连接 · 3 上游") -> desktop_app.DesktopApp:
    """只装 ``_toast`` / ``_update_status`` / ``_clear_sticky`` 需要的最小属性。"""
    app = desktop_app.DesktopApp.__new__(desktop_app.DesktopApp)
    app._status = _Label()                       # noqa: SLF001 - 最小装配
    app._status_right = _Label()                 # noqa: SLF001
    app._sticky_error = ""                       # noqa: SLF001
    app._sticky_job = None                       # noqa: SLF001
    app._theme_name = "day"                      # noqa: SLF001 - _status_right_text 要它
    app._controller = type("C", (), {"snapshot": {}})()   # noqa: SLF001
    app._status_line = lambda: status_line       # noqa: SLF001 - 实例属性遮蔽方法
    app._pill_text = lambda: ("RUNNING", "ok")   # noqa: SLF001
    app._update_identity = lambda: None          # noqa: SLF001
    app._update_clock = lambda: None             # noqa: SLF001
    return app


# ─────────────────────────────────────────────────────────── D4：失败提示粘住
def test_failed_toast_survives_status_refresh():
    """失败文案必须活过 :meth:`_update_status`（旧行为：10 秒内被「已连接 · …」盖掉）。"""
    app = _bare_app()
    app._toast("绑定未成功：绑定码已过期，请重新生成", ok=False)
    assert app._status.text.startswith("! ")                      # noqa: SLF001
    assert "绑定码已过期" in app._status.text                      # noqa: SLF001

    app._update_status()                                          # 模拟下一轮刷新
    assert "绑定码已过期" in app._status.text, "失败提示被常规状态行覆盖了"  # noqa: SLF001
    assert app._status.foreground == desktop_app.dual.color("red")  # noqa: SLF001


def test_success_toast_replaces_sticky_error():
    """成功播报即清粘住态（否则旧的失败红字会一直挂着，反而骗人）。"""
    app = _bare_app()
    app._toast("绑定未成功：绑定码已过期", ok=False)
    app._toast("已保存接入配置", ok=True)
    assert app._sticky_error == ""                                # noqa: SLF001
    assert app._status.text.startswith("✓ ")                      # noqa: SLF001
    app._update_status()
    assert app._status.text == "已连接 · 3 上游"                   # noqa: SLF001


def test_clear_sticky_restores_regular_status_line():
    app = _bare_app("已连接 · 2 上游")
    app._toast("绑定未成功：绑定码已过期", ok=False)
    app._clear_sticky()                                           # noqa: SLF001
    assert app._sticky_error == ""                                # noqa: SLF001
    assert app._status.text == "已连接 · 2 上游"                   # noqa: SLF001


def test_new_action_clears_previous_error():
    """按了新动作 = 上一条失败已经看过；新失败会重新粘上。"""
    app = _bare_app()
    app._vars = {}                                                # noqa: SLF001
    app._render = lambda view: None                               # noqa: SLF001
    app._toast("绑定未成功：绑定码已过期", ok=False)

    class _C:
        snapshot: dict[str, Any] = {}

        def set_draft(self, key, value):                          # noqa: ANN001 - 桩
            return None

        def action(self, key, confirm=None):                      # noqa: ANN001 - 桩
            from djb_node.desktop.controller import ActionResult
            return ActionResult("失败原因：" + key, ok=False)

        def current(self):                                        # noqa: ANN001 - 桩
            return build_views(desktop_app.demo_snapshot())["overview"]

    app._controller = _C()                                        # noqa: SLF001
    app._on_action("account.bind")                                # noqa: SLF001
    # 新动作用新的失败文案替换旧的（清 → 立刻重新粘上），绝不留上一条的原文
    assert app._sticky_error.startswith("! 失败原因：account.bind")  # noqa: SLF001


# ─────────────────────────────────────────────────────────── D4：失败文案带下一步
def test_bind_failure_with_expired_code_tells_how_to_fix():
    """服务端 409（``binding_code_expired``）→ 文案必须说「去网页端重新生成」。"""
    controller = DesktopController(
        FakeDataSource({"account": {"profile": {}, "security": {}, "note": ""}}, source="test"),
        cli=_raise(ProviderError("绑定码已过期，请重新生成 [reason=binding_code_expired]")),
        ask_code=lambda: "kyet-expired-code")
    result = controller.action("account.bind")
    assert result.ok is False
    assert "绑定码已过期" in result.message
    assert "重新生成" in result.message, "失败文案没给出下一步（客户只能再点一次 OK）"


def test_bind_failure_with_network_error_has_no_code_hint():
    """网络类失败不许挂「码过期」的引导（会把客户指向错误的原因）。"""
    controller = DesktopController(
        FakeDataSource({"account": {"profile": {}, "security": {}, "note": ""}}, source="test"),
        cli=_raise(ProviderError("本地命令超时（20s）")),
        ask_code=lambda: "any-code")
    result = controller.action("account.bind")
    assert result.ok is False
    assert desktop_controller.BIND_CODE_HINT not in result.message


def _snapshot_with_update(update: dict[str, Any]) -> Any:
    """演示快照 + 指定的 ``account.update``（版本卡的数据源）。"""
    base = desktop_app.demo_snapshot()
    account = dict(base.get("account") or {})
    account["update"] = update
    sections = dict(base.sections)
    sections["account"] = account
    return ds.Snapshot(sections, fetched_at=base.fetched_at, source="test")


def _raise(exc: BaseException):
    def cli(argv):                                            # noqa: ANN001, ARG001 - 桩
        raise exc
    return cli


# ─────────────────────────────────────────────────────────── D2：版本如实
def test_update_status_exposes_package_and_recorded_version(paths, store, clock):
    manager = UpdateManager(paths, store, source="", client_version="0.3.0", now=clock)
    status = manager.status()
    assert status["package_version"] == facts.CLIENT_VERSION
    assert status["install_record_version"] == store.install().version
    assert status["current_version"] == status["install_record_version"]   # 老键语义不变


def test_reconcile_install_record_aligns_stale_record(paths, store, clock):
    """0.3.11 的包 + 0.3.0 的安装记录（手工换包）→ 自检后记录变成 0.3.11。"""
    store.update_install(version="0.3.0")
    manager = UpdateManager(paths, store, source="", client_version=facts.CLIENT_VERSION,
                            now=clock)
    first = manager.reconcile_install_record()
    assert first["ok"] is True and first["changed"] is True
    assert first["from"] == "0.3.0" and first["to"] == facts.CLIENT_VERSION
    assert store.install().version == facts.CLIENT_VERSION

    again = manager.reconcile_install_record()                # 幂等：第二次零写
    assert again == {"ok": True, "changed": False, "version": facts.CLIENT_VERSION}


def test_reconcile_without_install_record_does_nothing(paths, clock, monkeypatch):
    """未 ``djb init`` 的机器：自检不许凭空建库，只回一个理由。"""
    manager = UpdateManager(paths, _NoInstallStore(), source="", now=clock)  # type: ignore[arg-type]
    result = manager.reconcile_install_record()
    assert result["ok"] is False and result["reason"]


class _NoInstallStore:
    def install(self):                                        # noqa: ANN201 - 桩
        from djb_node.store import StoreNotInitializedError
        raise StoreNotInitializedError("安装记录缺失：先执行 djb init")


def test_account_view_shows_package_version_and_flags_stale_record():
    """版本卡：写程序版本；安装记录不一致时提示里点明「已按实际程序显示」。"""
    snapshot = _snapshot_with_update({"channel": "stable", "edition": "windows",
                                      "package_version": "0.3.11",
                                      "install_record_version": "0.3.0",
                                      "verify_state": "up_to_date"})
    view = build_views(snapshot)["account"]
    field = next(item for item in view.fields if item.key == "update_version")
    assert field.value == "0.3.11"
    assert "0.3.0" in field.hint and "已按实际程序显示" in field.hint
    assert field.tone == "amber"


def test_account_view_keeps_plain_hint_when_record_matches():
    snapshot = _snapshot_with_update({"channel": "stable", "edition": "windows",
                                      "package_version": "0.3.11",
                                      "install_record_version": "0.3.11",
                                      "verify_state": "up_to_date"})
    view = build_views(snapshot)["account"]
    field = next(item for item in view.fields if item.key == "update_version")
    assert field.value == "0.3.11"
    assert field.tone == "muted" and "已按实际程序显示" not in field.hint


if __name__ == "__main__":                                    # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
