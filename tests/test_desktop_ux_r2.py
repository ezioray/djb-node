# SPDX-License-Identifier: Apache-2.0
"""UX-E2E 第二轮真机回归（CHG-20261121-UX-FIX-R2）· 客户端条目 R2-8 / R2-9 / R2-10 / R2-11。

四条都是**客户看得见**的问题（证据：``reports/UX-E2E-20261121/evidence/
round2-client-20261121T1355/``）：

* R2-9  界面出现字面 markdown：``**成交规则**`` / ``15%**加在客户那边**``；
* R2-10 横幅只有结论「暂不可用（云端未接通…）」，没有任何按钮/指引；
* R2-11 残留英文取值：``unchecked`` / ``file-0600`` / ``model-sync 状态`` / ``L2/L3`` / ``stable``；
* R2-8  测试机 config 里的 RC 地址不会回生产 —— 先做**只读展示 + 明确确认**的恢复按钮。

**不联网、不写生产**：R2-8 的用例全部在 ``tmp_path`` 里造 config.json，cli 用假实现。
"""
from __future__ import annotations

import ast
import pathlib
from typing import Any

import pytest

from djb_node.desktop import app as desktop_app
from djb_node.desktop import controller as desktop_controller
from djb_node.desktop import views as vw
from djb_node.desktop.datasource import FakeDataSource, Snapshot, Unavailable
from djb_node.desktop.views import VIEW_KEYS, build_view

REPO = pathlib.Path(__file__).resolve().parents[1]


def _user_facing_literals(path: pathlib.Path):
    """源码里**会显示给客户**的字符串字面量（跳过注释与 docstring）。

    与 ``test_desktop_bootstrap_action`` 同一手法：注释里引用「修前长什么样」是合理的，
    但上屏的字符串一个字都不许留 markdown 标记。只有 AST 能分清两者。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None) or []
        first = body[0] if body else None
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            docstrings.add(id(first.value))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docstrings):
            yield node.value


# ───────────────────────────────────────────── R2-9：字面 markdown 星号
def test_no_markdown_stars_in_any_user_facing_literal():
    """R2-9：Tk 标签不渲染 markdown，``**`` 只会原样显示成两个星号。

    这条守的是**上屏层**（``djb_node/desktop/``）的全部字面量，不只是当次抓到的两处：
    新写的文案里再出现 ``**`` 会立刻红。CLI / 日志层的文本不在此列（那些不走 Tk 标签，
    而且 ``facts.py`` 里有 ``2**5 = 32 倍`` 这类**数学表达式**，字符串里出现 ``**`` 是正当的）。
    """
    offenders = []
    for path in sorted((REPO / "djb_node" / "desktop").rglob("*.py")):
        for literal in _user_facing_literals(path):
            if "**" in literal:
                offenders.append(str(path.relative_to(REPO)) + "：" + literal[:60])
    assert not offenders, "上屏文案里还有 markdown 星号：" + "；".join(offenders)


def test_token_plan_text_has_no_stars_and_keeps_the_fee_example():
    """费率那句改完之后：没有星号，但「能验算的三段」还在（不能为了去星号把话说没了）。"""
    text = build_view("token_plan", desktop_app.demo_snapshot()).plain_text()
    assert "**" not in text
    assert "成交规则" in text
    assert "加在客户那边" in text
    assert vw.FEE_EXAMPLE_ASK in text and "115.00" in text


@pytest.mark.parametrize("key", VIEW_KEYS)
def test_no_stars_on_any_page_even_when_everything_is_down(key):
    """降级快照（每页都缺数据）也不许把 markdown 漏到屏幕上。"""
    down = Snapshot({name: Unavailable("云端未接通（未登录节点：本机尚未注册节点）")
                     for name in VIEW_KEYS})
    assert "**" not in build_view(key, down).plain_text(), key


# ───────────────────────────────────────────── R2-10：云端未接通 → 「去绑定」
def _cloud_down_snapshot(reason: str = "云端未接通（未登录节点：本机尚未注册节点）") -> Snapshot:
    return Snapshot({"status": {"state": "online"},
                     "account": Unavailable(reason),
                     "earnings": Unavailable(reason)})


def test_cloud_banner_carries_a_bind_action_pointing_at_the_account_page():
    view = build_view("overview", _cloud_down_snapshot())
    recovery = view.recovery
    assert recovery is not None, "云端未接通时横幅必须有一颗按钮"
    assert recovery.label == vw.CLOUD_RECOVERY_LABEL == "去绑定"
    assert recovery.nav == vw.CLOUD_RECOVERY_NAV == "account"
    assert recovery.key == ""                       # 跳页动作，不走 controller 动作键
    assert "[" + vw.CLOUD_RECOVERY_LABEL + "]" in view.plain_text()


@pytest.mark.parametrize("key", VIEW_KEYS)
def test_every_page_gets_the_bind_button_when_the_cloud_is_unreachable(key):
    view = build_view(key, _cloud_down_snapshot())
    assert view.recovery is not None, key
    assert view.recovery.nav == "account", key


def test_hint_distinguishes_unregistered_from_unbound():
    """「本机还没注册」和「注册了没绑定」下一步不同——不能一句话糊过去。"""
    unregistered = build_view("overview", _cloud_down_snapshot()).recovery
    unbound = build_view("overview", _cloud_down_snapshot(
        "云端未接通（未登录节点：节点已注册但未绑定账号）")).recovery
    assert "重新注册" in unregistered.hint
    assert "在浏览器确认绑定" in unbound.hint


def test_uninitialised_wins_over_cloud_down():
    """两颗按钮不同时出现：本机没初始化时先解决「能跑」，不去绑定。"""
    snapshot = Snapshot({"status": Unavailable("本机状态库不存在：先执行 djb init"),
                         "account": Unavailable("云端未接通（未登录节点：本机尚未注册节点）")})
    recovery = build_view("overview", snapshot).recovery
    assert recovery is not None and recovery.key == "bootstrap" and not recovery.nav


def test_other_cloud_failures_do_not_get_the_bind_button():
    """网络/证书错误不是「没绑定」：给绑定按钮等于让客户白跑一趟。"""
    for reason in ("云端未接通（SSLError）", "云端连不上（超时 5 s）", "云端接口未接线（profile）"):
        view = build_view("overview", Snapshot({"status": {"state": "online"},
                                                "account": Unavailable(reason)}))
        assert view.recovery is None, reason


def test_healthy_client_keeps_no_recovery_button():
    assert build_view("overview", Snapshot({"status": {"state": "online"}})).recovery is None


def test_nav_failure_is_reported_not_swallowed(tmp_path):
    """R2-10 的「失败态不静默」：跳不过去要播报（而不是点了没反应）。"""
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except Exception:                               # pragma: no cover - 无显示
        pytest.skip("没有可用的 Tk 显示")
    controller = desktop_controller.DesktopController(FakeDataSource(_cloud_down_snapshot().sections))
    app = desktop_app.DesktopApp(controller, root=root)
    try:
        app.show_view("overview")
        root.update()
        app._recovery_nav("no-such-page")            # noqa: SLF001 - 等价于点了个坏页面键
        root.update()
        assert "没能切到" in app._status.cget("text")                # noqa: SLF001 - 状态条
    finally:
        root.destroy()


def test_bind_button_navigates_to_the_account_page(tmp_path):
    """点「去绑定」= 切到「账户与安全」（绑定入口就在那一页），不改任何数据。"""
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except Exception:                               # pragma: no cover - 无显示
        pytest.skip("没有可用的 Tk 显示")
    controller = desktop_controller.DesktopController(FakeDataSource(_cloud_down_snapshot().sections))
    app = desktop_app.DesktopApp(controller, root=root)
    try:
        app.show_view("overview")
        root.update_idletasks()
        button = app._recovery_button                # noqa: SLF001 - 断言用
        assert button.text == vw.CLOUD_RECOVERY_LABEL
        button._on_click()                           # noqa: SLF001
        root.update()
        assert controller.view == "account"
    finally:
        root.destroy()


# ───────────────────────────────────────────── R2-11：残留英文取值
def _account_snapshot(**update: Any) -> Snapshot:
    """真机 0.5.7 的形状：storage=file-0600、channel=stable、verify_state=unchecked。"""
    base = dict(desktop_app.demo_snapshot().sections)
    account = dict(base.get("account") or {})
    account["security"] = {"storage": "file-0600", "node_id": "node-7c31"}
    account["update"] = {"current_version": "0.5.7", "channel": "stable",
                         "verify_state": "unchecked", "checked_at": "", "note": "", **update}
    base["account"] = account
    return Snapshot(base)


def test_account_page_speaks_chinese_for_storage_channel_and_state():
    text = build_view("account", _account_snapshot()).plain_text()
    assert "file-0600" not in text                    # 存储后端 ID 不上屏
    assert "本机文件" in text and "0600" in text
    assert "stable 渠道" not in text and "稳定版渠道" in text
    assert "unchecked" not in text and "还没有检查过" in text


def test_unknown_state_code_is_wrapped_in_chinese_instead_of_leaking():
    """词表里没有的状态：保留原码给工程师，但外面必须包中文（客户不该读到枚举名）。"""
    text = build_view("account", _account_snapshot(verify_state="brand_new_state")).plain_text()
    assert "未知状态（brand_new_state）" in text


def test_unknown_storage_backend_keeps_the_raw_value_but_says_what_it_is():
    from djb_node.desktop import views as views_mod

    assert views_mod.credential_storage_text("weird-store") == "其他方式（weird-store）"
    assert views_mod.credential_storage_text("") == "—"


def test_models_page_explains_l2_l3_and_model_sync_in_chinese():
    text = build_view("models", desktop_app.demo_snapshot()).plain_text()
    assert "model-sync 状态" not in text
    assert "模型同步（model-sync）状态" in text
    assert "L2/L3" not in text
    # 缩写可以留，但必须有一句中文解释「这两个字母是什么意思」
    assert "L2 推理" in text and "L3 流式" in text
    assert "L2 = 一次真实推理能不能通" in text
