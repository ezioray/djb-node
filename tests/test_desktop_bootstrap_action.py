# SPDX-License-Identifier: Apache-2.0
"""UX-E2E-20261121 P1-3：首启「死路」→ 一次点击。

走查原话（``reports/UX-E2E-20261121/FINDINGS.md`` P1-3）：首启整屏「暂不可用」，唯一指引是
让客户敲 ``djb init``，横幅自己还写着「桌面端没有初始化入口，界面里点不到」——客户手上
没有终端 ⇒ 装完就是死的。

这里的用例只认**客户看得见的东西**：横幅不再自贬、首屏有可点的「初始化并连接」、点下去
走 controller 的 ``bootstrap`` 动作、失败给下一步。

**不联网、不注册节点**：自举工厂由测试注入假实现（真 ``ensure_ready`` 也不跑），
GUI 用例用临时 HOME。真机上这条链路唯一的副作用就是 ``~/.djb`` 的本地目录 + 起一个 daemon，
与 ``main()`` 首启完全同一条路（见 ``djb_node/bootstrap.py``）。
"""
from __future__ import annotations

import pathlib

import pytest

from djb_node.desktop import app as desktop_app
from djb_node.desktop import controller as desktop_controller
from djb_node.desktop import views as vw
from djb_node.desktop.datasource import FakeDataSource, Snapshot, Unavailable
from djb_node.desktop.views import VIEW_KEYS, build_view

REPO = pathlib.Path(__file__).resolve().parents[1]
#: 走查抓到的自贬句（客户读成「这软件没做完」）：一处都不许再出现，注释里也不行。
SELF_DEPRECATING = ("界面里点不到", "没有初始化入口", "没有对应入口")


def _uninit_snapshot() -> Snapshot:
    """真机首启的形状：status 域不可用，原因是「没初始化」。"""
    return Snapshot({"status": Unavailable("本机状态库不存在：先执行 djb init"),
                     "earnings": Unavailable("云端未接通（未登录节点：本机尚未注册节点）")})


# ───────────────────────────────────────────── 1 文案：不再自贬 + 给出界面内出路
def _user_facing_literals(path: pathlib.Path):
    """源码里**会显示给客户**的字符串字面量（跳过注释与 docstring）。

    为什么不用纯文本 grep：这些句子的历史在注释/docstring 里被引用是合理的（要写清「修前是什么」），
    但**上屏的**字符串一个字都不许留。只有 AST 能分清两者。
    """
    import ast

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


def test_no_self_deprecating_copy_in_client_sources():
    offenders = []
    for path in sorted((REPO / "djb_node").rglob("*.py")):
        for literal in _user_facing_literals(path):
            for phrase in SELF_DEPRECATING:
                if phrase in literal:
                    offenders.append(str(path.relative_to(REPO)) + "：" + literal[:60])
    assert not offenders, "上屏文案里还有自贬句：" + "；".join(offenders)


def test_terminalize_points_at_the_in_app_action_instead_of_a_dead_end():
    text = vw.terminalize("本机未初始化：先执行 djb init")
    assert "djb init" in text                                   # 终端命令还在（备选路径）
    assert vw.INIT_RECOVERY_LABEL in text                       # 但先给界面内的出路
    assert "点不到" not in text


def test_uninitialised_banner_says_what_to_click():
    """数据层把 CLI 原话翻成「点这个按钮」：横幅本身就是下一步（不再只是「去开终端」）。"""
    from djb_node.desktop.datasource import actionable_init_hint

    detail = actionable_init_hint("本机未初始化：先执行 djb init")
    assert detail.startswith("本机未初始化")                    # 原因照原样留着（客服要看）
    assert vw.INIT_RECOVERY_LABEL in detail
    assert "djb init" in detail                                # 终端仍是备选，不是唯一出路


def test_the_button_name_is_written_once_per_layer():
    """投影层与数据层各有一份字面量（不能互相 import）：必须逐字相等。"""
    from djb_node.desktop import datasource as ds

    assert ds.INIT_RECOVERY_LABEL == vw.INIT_RECOVERY_LABEL


# ───────────────────────────────────────────── 2 投影层：每页都拿到同一个出口
def test_every_page_carries_the_bootstrap_recovery_when_uninitialised():
    snapshot = _uninit_snapshot()
    for key in VIEW_KEYS:
        view = build_view(key, snapshot)
        assert view.recovery is not None, key
        assert view.recovery.key == "bootstrap" and view.recovery.label == vw.INIT_RECOVERY_LABEL
        assert "[" + vw.INIT_RECOVERY_LABEL + "]" in view.plain_text(), key


def test_healthy_snapshot_has_no_recovery_button():
    view = build_view("overview", Snapshot({"status": {"state": "online"}}))
    assert view.recovery is None
    assert "[" + vw.INIT_RECOVERY_LABEL + "]" not in view.plain_text()


def test_other_failures_do_not_get_the_bootstrap_button():
    """权限 / 超时 / 网络都不是「没初始化」：挂了按钮客户点了没变化，比没有按钮更糟。"""
    for reason in ("本机目录权限过宽：执行 djb init 会收紧到 0700",
                   "本机控制口超时（5 秒没有回应）",
                   "云端未接通（SSLError）"):
        view = build_view("overview", Snapshot({"status": Unavailable(reason)}))
        assert view.recovery is None, reason


# ───────────────────────────────────────────── 3 controller：动作本身（注入假自举）
def _controller(factory=None, **kwargs):
    source = FakeDataSource({"status": Unavailable("本机状态库不存在：先执行 djb init")})
    return desktop_controller.DesktopController(source, bootstrap_factory=factory, **kwargs)


def test_bootstrap_action_reports_success_without_touching_the_network():
    calls: list[int] = []

    def factory():
        calls.append(1)                        # 假自举：一次都不联网、不起进程
        return {"boot": {"enabled": True, "created": True, "errors": []},
                "daemon": {"enabled": True, "started": True, "reason": "", "errors": []}}

    result = _controller(factory).action("bootstrap")
    assert result.ok and calls == [1]
    assert "节点已启动" in result.message
    assert result.payload["daemon"]["started"] is True      # 回投给界面回填「节点进程」字段


def test_bootstrap_action_turns_missing_cli_into_a_next_step():
    """打包漏了 djb-node.exe：不许只说「失败」，要告诉客户怎么装对。"""
    result = _controller(lambda: {
        "boot": {"enabled": True, "created": True, "errors": []},
        "daemon": {"enabled": True, "started": False, "reason": "no-cli", "errors": []},
    }).action("bootstrap")
    assert result.ok is False
    assert "djb-node.exe" in result.message and "解压" in result.message


def test_bootstrap_action_survives_a_raising_factory():
    def factory():
        raise RuntimeError("disk full")

    result = _controller(factory).action("bootstrap")
    assert result.ok is False
    assert "RuntimeError" in result.message and "下一步" in result.message
    assert "本机没有改动" in result.message


def test_bootstrap_action_without_a_channel_refuses_instead_of_pretending():
    result = _controller(None).action("bootstrap")
    assert result.ok is False and "暂不可用" in result.message
    assert "没有任何改动" in result.message


def test_bootstrap_action_respects_the_disabled_switch():
    """``DJB_BOOTSTRAP_DISABLE``（或 enabled=False）：不许谎报「已初始化」。"""
    result = _controller(lambda: {"boot": {"enabled": False}, "daemon": {"enabled": False}}
                         ).action("bootstrap")
    assert result.ok is False and "DJB_BOOTSTRAP_DISABLE" in result.message


def test_async_channel_is_used_when_injected():
    """GUI 注入 worker 时主线程只派发：动作立刻返回「正在初始化」，不阻塞。"""
    seen: list[str] = []
    controller = _controller(lambda: {"boot": {}, "daemon": {"enabled": True, "started": True}})
    controller.bootstrap_via = lambda: seen.append("started") or True
    result = controller.action("bootstrap")
    assert result.effect == "bootstrap" and result.payload["state"] == "running"
    assert seen == ["started"]
    assert "正在初始化" in result.message


# ───────────────────────────────────────────── 4 GUI：按钮真的画出来、点了走动作
def _window(tmp_path, controller):
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except Exception:                              # pragma: no cover - 无显示
        pytest.skip("没有可用的 Tk 显示")
    app = desktop_app.DesktopApp(controller, root=root)
    root.geometry("1200x780")
    app.show_view("overview")
    root.update_idletasks()
    root.update()
    return app, root


def test_first_screen_renders_a_clickable_bootstrap_button(tmp_path):
    """首屏必须有一颗**能点**的按钮；点它走 controller 的动作键（不是直接调函数）。"""
    calls: list[int] = []

    def factory():
        calls.append(1)
        return {"boot": {"enabled": True, "created": True, "errors": []},
                "daemon": {"enabled": True, "started": True, "reason": "", "errors": []}}

    controller = _controller(factory)
    app, root = _window(tmp_path, controller)
    try:
        controller.bootstrap_via = None             # 同步回落：点一下就把结论拿回来
        button = app._recovery_button               # noqa: SLF001 - 断言用
        assert button.text == vw.INIT_RECOVERY_LABEL
        button._on_click()                          # noqa: SLF001 - 等价于点一下
        root.update_idletasks()
        assert calls == [1], "按钮没接到 bootstrap 动作"
    finally:
        root.destroy()


def test_bootstrap_button_goes_through_the_worker_off_the_main_thread(tmp_path):
    """真窗口下 worker 必须被接上：点一下**立刻**返回，结论稍后由 worker 回投。"""
    import time

    calls: list[int] = []

    def factory():
        time.sleep(0.05)
        calls.append(1)
        return {"boot": {"enabled": True, "created": True, "errors": []},
                "daemon": {"enabled": True, "started": True, "reason": "", "errors": []}}

    app, root = _window(tmp_path, _controller(factory))
    try:
        assert app._controller.bootstrap_via is not None      # noqa: SLF001 - 一开窗就接上
        began = time.monotonic()
        app._recovery_button._on_click()                       # noqa: SLF001
        assert time.monotonic() - began < 0.2, "点「初始化并连接」被自举阻塞了"
        deadline = time.monotonic() + 5.0
        while not calls and time.monotonic() < deadline:       # 泵事件循环等 worker 回投
            root.update()
            time.sleep(0.02)
        assert calls == [1]
    finally:
        root.destroy()
