# SPDX-License-Identifier: Apache-2.0
"""RC15-039 P0 文案三条（#3 / #9 / #11）+ P1 两条（#33 / #37）的口径守卫。

每条都写成「改回去必须红」：断言落在**用户能看见的字**上，不是实现细节。
"""
from __future__ import annotations

import pathlib

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
def test_no_page_tells_the_customer_to_run_a_half_cli_command():
    """界面上出现 CLI 时，必须写明「要在终端里跑」并给完整命令；不许只甩 `djb auth --bind`。

    用**未绑定**快照：这时账户页才会显示绑定步骤（demo 快照是已绑定态，看不到那段字）。
    """
    unbound = _snapshot(binding_state="unbound", config={})
    texts = {"account": vw.build_view("account", unbound).plain_text()}
    for key in ("overview", "token_plan", "agent"):
        texts[key] = vw.build_view(key, demo_snapshot()).plain_text()
    assert "手输绑定码" in texts["account"]      # 界面内动作确实在
    merged = "\n".join(texts.values())
    assert "djb auth --bind <码>" not in merged
    if "djb init" in merged:
        assert "终端" in merged, "提到 djb init 就必须说清要去终端跑"


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
