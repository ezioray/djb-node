# SPDX-License-Identifier: Apache-2.0
"""RC15-039 P0 文案三条（#3 / #9 / #11）+ P1 两条（#33 / #37）的口径守卫。

每条都写成「改回去必须红」：断言落在**用户能看见的字**上，不是实现细节。
"""
from __future__ import annotations

import pathlib
import sys

import pytest

from djb_node.desktop import views as vw
from djb_node.desktop.app import demo_snapshot
from djb_node.tray import actions as tray_actions
from djb_node.tray.actions import ControlResult, TrayAction


# ── #3 接单失败：说清为什么 + 下一步，旧句全仓消失 ────────────────────────────
def _disabled_control() -> ControlResult:
    return tray_actions.apply_control(
        TrayAction.PAUSE, call=lambda *_a, **_k: {}, enabled=lambda: False,
        confirm=lambda _message: True)


def test_disabled_control_says_why_and_what_next_not_a_cli_jargon():
    result = _disabled_control()
    assert result.verified is False
    assert "没有执行" in result.message            # 说清「这次没成」
    assert "下一步" in result.message              # 说清「接下来做什么」
    assert "服务状态" in result.message            # 不让客户以为已经生效
    assert "命令指南" not in result.message        # 旧文案的黑话必须消失


def test_old_control_sentence_is_gone_from_the_whole_repo():
    """旧句不许以任何形式留着（代码、注释、文档都算）。"""
    old = "控制开关关闭" + "或已到期"          # 拆开写：这条测试自己不能成为命中项
    root = pathlib.Path(__file__).resolve().parent.parent
    hits = []
    for path in list(root.glob("djb_node/**/*.py")) + list(root.glob("*.md")) + \
            list(root.glob("docs/**/*.md")):
        if path.name.startswith("test_"):
            continue
        try:
            if old in path.read_text(encoding="utf-8"):
                hits.append(str(path.relative_to(root)))
        except (UnicodeDecodeError, OSError):
            continue
    assert not hits, "旧文案还在：" + ", ".join(hits)


# ── #9 绑定态只有一个判定源：不许同屏同时出现「3 / 3 接入完成」与「未绑定」 ─────────
def _snapshot(*, binding_state, config, profile=None, security=None):
    from djb_node.desktop import datasource as ds

    return ds.Snapshot({
        "config": {"node_secret": "", "price_mode": "", "usdt_address": ""} | config,
        "status": {"state": "online"},
        "access": {"config": config, "binding": {"state": binding_state}},
        "account": {"profile": profile or {}, "security": security or {}},
        "earnings": {}, "bench": {}, "yield": {}, "receipts": {"rows": []},
        "tasks": {}, "gpu": {}, "token_plan": {}, "logs": {},
    }, source="test")


def test_bound_state_has_one_source_of_truth_across_pages():
    """本机有凭据 + 价格 + 收款，但服务端说 unbound → 总览与账户页都必须算「未绑定」。"""
    snapshot = _snapshot(binding_state="unbound",
                         config={"node_secret": "ns-1", "price_mode": "auto",
                                 "usdt_address": "0x" + "a" * 40},
                         profile={"node_id": "n-42", "state": "active"})
    overview = vw.build_view("overview", snapshot).plain_text()
    account = vw.build_view("account", snapshot).plain_text()
    assert "未绑定" in account
    assert "3 / 3" not in overview, "服务端说未绑定，总览不许报 3 / 3"
    assert "接入完成" not in overview


def test_bound_state_says_done_only_when_server_says_bound():
    snapshot = _snapshot(binding_state="bound",
                         config={"price_mode": "auto", "usdt_address": "0x" + "a" * 40},
                         profile={"node_id": "n-42", "state": "active", "bound": True})
    overview = vw.build_view("overview", snapshot).plain_text()
    assert "3 / 3" in overview or "接入完成" in overview


# ── #33 节点存在 / 未绑定 / 在线 三个状态不许互相打脸 ──────────────────────────
def test_unbound_node_is_not_reported_as_missing():
    snapshot = _snapshot(binding_state="unbound",
                         config={},
                         profile={"node_id": "n-42", "state": "active",
                                  "last_seen": "2026-11-16T10:00:00Z"},
                         security={"node_id": "n-42", "storage": "本机密钥文件"})
    text = vw.build_view("account", snapshot).plain_text()
    assert "未绑定" in text
    assert "n-42" in text, "身份卡必须显示节点 ID"
    # 这句解释必须**真的在**（以前写成 `or "节点凭据" in text`，别的字段凑巧命中 → 用例不红）
    assert "节点本身是存在的" in text, "要解释「未绑定 ≠ 节点不存在」"


# ── #11 CLI 不许当解决方案 ───────────────────────────────────────────────────
def _snapshot_with_cli_next_steps():
    """把 CLI 的 next_step 塞进每个会渲染它的域（access / bench / daemon / token_plan）。

    复核抓到的漏网就是这两处：`views.py:1879` 原样上屏 `daemon.next_step`、
    `views.py:1096` 原样上屏 envcheck 的 `next_step`。
    """
    from djb_node.desktop import datasource as ds

    demo = demo_snapshot()
    sections = {key: dict(value) if isinstance(value, dict) else value
                for key, value in demo.sections.items()}
    sections["access"] = dict(sections.get("access") or {}, **{
        "daemon": {"next_step": "执行 djb auth --bind <一次性码> 绑定账户后才会接单"},
        "envcheck": {"checks": [
            {"id": "perm", "level": "warn", "summary": "目录权限过宽",
             "next_step": "执行 djb init 会收紧到 0700"},
        ]},
    })
    sections["bench"] = dict(sections.get("bench") or {}, **{
        "checks": [{"id": "perm", "level": "warn", "summary": "目录权限过宽",
                    "next_step": "执行 djb init 会收紧到 0700"}]})
    sections["token_plan"] = dict(sections.get("token_plan") or {}, **{
        "discover_note": "先执行 djb start 再读模型清单"})
    return ds.Snapshot(sections, source="test")


def test_every_one_of_the_13_views_marks_cli_steps_as_needing_a_terminal():
    """复核口径：**13 个视图全 build**，任何一行出现 `djb <命令>` 都必须同句说清「要去终端跑」。"""
    snapshot = _snapshot_with_cli_next_steps()
    views = vw.build_views(snapshot)
    assert len(views) == 13, "视图数变了：%d" % len(views)
    offenders = []
    for key, view in views.items():
        for line in view.plain_text().splitlines():
            if "djb " in line and "终端" not in line:
                offenders.append("%s: %s" % (key, line.strip()[:90]))
    assert not offenders, "这些行把 CLI 当唯一指引：" + " | ".join(offenders)


def test_cli_never_appears_as_the_only_route_on_the_account_page():
    """未绑定快照：绑定走界面内动作，`djb auth --bind <码>` 不许当解决方案出现。"""
    unbound = _snapshot(binding_state="unbound", config={})
    text = vw.build_view("account", unbound).plain_text()
    assert "手输绑定码" in text                      # 界面内动作确实在
    assert "djb auth --bind <码>" not in text


def test_datasource_marks_cli_init_as_needing_a_terminal():
    """CLI 的「先执行 djb init」被搬上屏时，必须补一句「这条要在终端里跑」。"""
    import subprocess

    from djb_node.desktop import datasource as ds_mod
    from djb_node.desktop.datasource import ProviderError

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 3, stdout="",
                                           stderr="djb: 本机未初始化：先执行 djb init\n")

    original = ds_mod.subprocess.run
    ds_mod.subprocess.run = fake_run
    try:
        with pytest.raises(ProviderError) as excinfo:
            ds_mod.LocalDataSource(home=pathlib.Path("/tmp"))._cli_runner(["status"])
    finally:
        ds_mod.subprocess.run = original
    message = str(excinfo.value)
    assert "本机未初始化" in message           # CLI 原话还在（别把原因吃掉）
    assert "终端" in message and "djb init" in message


# ── #37 「请求地址」一处口径 ────────────────────────────────────────────────
def test_base_url_wording_is_identical_on_every_page():
    hint = vw.BASE_URL_HINT
    labels = set()
    for key in ("agent", "token_plan", "access"):
        view = vw.build_view(key, demo_snapshot())
        for field in view.fields:
            if "请求地址" in field.label or field.key in ("openai_base_url",
                                                          "anthropic_base_url", "base_url"):
                labels.add(field.label)
                assert field.hint == hint, "%s 的填法提示与统一口径不一致：%r" % (
                    field.label, field.hint)
    assert labels <= {"OpenAI 请求地址", "Anthropic 请求地址", "请求地址"}, labels
    assert "请求链接" not in vw.build_view("token_plan", demo_snapshot()).plain_text()


# ── #3 真实入口：托盘 sink 在开关关着时的兜底文案 ────────────────────────────
class _FakeBackend:
    def __init__(self):
        self.feedback: list[tuple[str, str]] = []
        self.pages: list[str] = []

    def show_feedback(self, title, body):
        self.feedback.append((str(title), str(body)))

    def show_page(self, page):
        self.pages.append(str(page))

    def confirm_control(self, _message):
        return False


def test_tray_sink_uses_human_words_when_control_is_disabled(tmp_path, monkeypatch):
    """复核口径：**从真实入口**（`desktop_intent_sink` 闭包）+ HOME 无配置 出发。

    以前 `enabled()` 为假时根本不进 `apply_control`，直接落「命令指南 / 未执行」，
    把 `djb pause` 当唯一指引；所以只改 `apply_control` 的文案是假绿。
    """
    monkeypatch.setenv("DJB_HOME", str(tmp_path))          # 空 HOME：没有控制开关配置
    monkeypatch.setattr(sys, "platform", "linux")          # 走控制开关判定，不是 Windows 短路

    from djb_node.tray import app as tray_app
    from djb_node.tray.model import TrayAction, TrayIntent

    backend = _FakeBackend()
    sink = tray_app.desktop_intent_sink(backend, refresh=lambda: None)
    sink(TrayIntent(TrayAction.PAUSE, command=("djb", "pause"), description="暂停接单（受控）"))

    assert backend.feedback, "托盘没有给出任何反馈"
    title, body = backend.feedback[-1]
    assert "命令指南" not in title and "命令指南" not in body
    assert "没有执行" in body                 # 说清这次没成
    assert "下一步" in body                   # 说清接下来做什么
    assert "服务状态一个字都没改" in body      # 不让客户以为已经生效
    assert "djb pause" not in body, "CLI 不许再当唯一指引"


def test_onboarding_bound_flag_uses_the_same_single_source():
    """复核第 3 条：`controller._bound_flag()`（喂 onboarding）不许有第二个判定源。"""
    from djb_node.desktop.controller import DesktopController

    unbound_with_credentials = _snapshot(
        binding_state="unbound", config={},
        profile={"node_id": "n-42", "state": "active"},
        security={"node_id": "n-42", "storage": "本机密钥文件"})
    controller = DesktopController.__new__(DesktopController)   # 不跑 __init__，只测判定
    controller._snapshot = unbound_with_credentials
    assert controller._bound_flag() is False, "服务端说未绑定，引导流程不许当成已绑定"
    assert controller._bound_flag() == vw.binding_state(unbound_with_credentials)[0]
