# SPDX-License-Identifier: Apache-2.0
"""R8 F1/F4：桌面端绑定防呆（密钥失效自动恢复）与首启防火墙脚注。

四条验收线：

1. 恢复中：账户页显示「节点密钥已失效，正在自动恢复…」，绑定按钮置灰（reason 同一句）；
2. 恢复成功：清状态、回到绑定输入（``account.bind`` 重新可点，不再挂恢复按钮）；
3. 恢复失败：「自动恢复失败：<原因>；点此重试 / 到网页端解绑后重新绑定」，并给出两条可点动作；
4. 首次启动：绑定页/引导第 2 步有防火墙脚注（spec §2 原文，逐字）。
"""
from __future__ import annotations

from djb_node.desktop import controller as ctrl_mod
from djb_node.desktop import views as vw
from djb_node.desktop.controller import DesktopController
from djb_node.desktop.datasource import FakeDataSource, ProviderError


def _source(*, security: dict | None = None, status: dict | None = None):
    sections = {
        "account": {"profile": {}, "security": security or {}, "note": ""},
        "access": {"binding": {"state": "unbound"}},
    }
    if status is not None:
        sections["status"] = status
    return FakeDataSource(sections, source="test")


def _controller(*, security: dict | None = None, status: dict | None = None,
                cli=None, ask_code=None) -> DesktopController:
    return DesktopController(_source(security=security, status=status),
                             cli=cli, ask_code=ask_code)


def _action(view, key: str):
    return next(item for item in view.actions if item.key == key)


# ─────────────────────────────────────────────── 1. 恢复中：文案 + 置灰
def test_recovering_state_shows_copy_and_greys_bind_buttons():
    controller = _controller()
    controller.binding_recovery = "recovering"
    view = controller.show("account")
    text = view.plain_text()
    assert vw.BIND_RECOVERING_TEXT == "节点密钥已失效，正在自动恢复…"
    assert vw.BIND_RECOVERING_TEXT in text
    bind = _action(view, "account.bind")
    assert bind.enabled is False
    assert vw.BIND_RECOVERING_TEXT in bind.reason
    # 恢复中不许出现「点此重试」——正在跑，重复点只会多打一次云端
    assert "account.bind_recover" not in [item.key for item in view.actions]


def test_recovering_state_is_injected_into_account_snapshot():
    controller = _controller()
    controller.binding_recovery = "recovering"
    account = controller._snapshot_for_views().get("account")
    assert isinstance(account, dict)
    assert account["binding_recovery"] == {"state": "recovering", "reason": ""}


# ─────────────────────────────────────────────── 2. 成功：回到绑定输入
def test_recovery_success_returns_to_bind_input():
    calls: list[list[str]] = []
    controller = _controller(cli=lambda argv: calls.append(list(argv)) or {"ok": True})
    controller.binding_recovery = "failed"
    controller.binding_recovery_reason = "云端暂不可用"

    result = controller.action("account.bind_recover")
    assert result.ok is True
    assert result.message == vw.BIND_RECOVER_SUCCESS_TEXT
    assert "重新生成绑定码" in result.message
    assert calls == [["auth", "--recover"]]
    assert controller.binding_recovery == "" and controller.binding_recovery_reason == ""

    view = controller.show("account")
    assert _action(view, "account.bind").enabled is True
    assert vw.BIND_RECOVERING_TEXT not in view.plain_text()
    keys = [item.key for item in view.actions]
    assert "account.bind_recover" not in keys


def test_known_invalid_key_recovers_before_asking_for_a_code():
    calls: list[list[str]] = []
    controller = _controller(status={"reason_code": "credential_invalid"},
                             cli=lambda argv: calls.append(list(argv)) or {"ok": True},
                             ask_code=lambda: (_ for _ in ()).throw(
                                 AssertionError("密钥失效时不该先要绑定码")))
    result = controller.action("account.bind")
    assert result.ok is True and "节点密钥已恢复" in result.message
    assert calls == [["auth", "--recover"]]


def test_revoked_bind_failure_switches_to_recovery_instead_of_raw_error():
    calls: list[list[str]] = []

    def cli(argv):
        calls.append(list(argv))
        if argv[:2] == ["auth", "--bind"]:
            raise ProviderError("绑定失败 [reason=client_key_revoked]")
        return {"ok": True}

    controller = _controller(cli=cli, ask_code=lambda: "code-1")
    result = controller.action("account.bind")
    assert result.ok is True and "节点密钥已恢复" in result.message
    assert calls == [["auth", "--bind", "code-1"], ["auth", "--recover"]]


# ─────────────────────────────────────────────── 3. 失败：下一步 + 两条可点动作
def test_recovery_failure_gives_retry_and_web_actions_without_internal_words():
    def cli(argv):
        raise ProviderError("client_key_revoked：云端拒绝了这台电脑")

    controller = _controller(cli=cli)
    result = controller.action("account.bind_recover")
    assert result.ok is False
    assert "自动恢复失败" in result.message
    assert "点此重试 / 到网页端解绑后重新绑定" in result.message
    lowered = result.message.lower()
    for internal in ("client_key", "register", "poll"):
        assert internal not in lowered
    assert controller.binding_recovery == "failed"

    view = controller.show("account")
    text = view.plain_text()
    assert "自动恢复失败" in text and "点此重试 / 到网页端解绑后重新绑定" in text
    retry = _action(view, "account.bind_recover")
    assert retry.label == vw.BIND_RECOVER_RETRY_LABEL
    web = _action(view, "account.recover_open_web")
    assert web.label == vw.BIND_RECOVER_WEB_LABEL
    assert _action(view, "account.bind").enabled is False


def test_web_action_only_opens_the_console_and_never_touches_credentials():
    controller = _controller()
    result = controller.action("account.recover_open_web")
    assert result.effect == "open_url"
    assert result.payload.startswith("https://")
    assert "节点与绑定" in result.message


# ─────────────────────────────────────────────── 4. 防火墙脚注（spec §2）
def test_first_start_bind_page_has_firewall_footnote():
    view = vw.build_view("account", _source().snapshot())
    text = view.plain_text()
    assert vw.FIREWALL_NOTE == ("若 Windows 弹出防火墙授权，请允许“专用网络”，"
                                "否则本机镜像端口不可用")
    assert vw.FIREWALL_NOTE in text


def test_firewall_footnote_not_repeated_once_node_identity_exists():
    view = vw.build_view("account", _source(security={"node_id": "n_1"}).snapshot())
    assert vw.FIREWALL_NOTE not in view.plain_text()


def test_onboarding_bind_step_carries_firewall_footnote():
    assert vw.FIREWALL_NOTE in vw.onboarding_panel(2).body
    assert vw.FIREWALL_NOTE not in vw.onboarding_panel(1).body
    assert vw.FIREWALL_NOTE not in vw.onboarding_panel(3).body


def test_recovery_copy_lives_in_views_only_once():
    """UI 与 controller 共用 views 里的常量（避免两处文案漂移）。"""
    assert ctrl_mod.BIND_RECOVERING_TEXT is vw.BIND_RECOVERING_TEXT
    assert ctrl_mod.BIND_RECOVER_SUCCESS_TEXT is vw.BIND_RECOVER_SUCCESS_TEXT
