# SPDX-License-Identifier: Apache-2.0
"""12 视图投影 + 窗口逻辑（无 Tk）：文案红线、缺失数据口径、动作路由、保存回读。

视图层是纯函数（Snapshot → View），所以这里能用文本快照断言 UI 说了什么——
「无留痕等于没做」在界面上就是：**说出来的每句话都可断言**。
"""
from __future__ import annotations

import os
import time

import pytest

from djb_node.desktop import access as ac
from djb_node.desktop import datasource as ds
from djb_node.desktop.datasource import Snapshot
from djb_node.desktop import views as vw
from djb_node.desktop.app import demo_snapshot
from djb_node.desktop.controller import ControlOutcome, DesktopController

SECRET = "djb_sk_live_9f2c41aa77be"
#: 侧栏顺序 = IA 顺序（4 组：运行 → 收益 → 接入 → 系统；见 views.NAV_SECTIONS）
SPEC_ORDER = ["overview", "bench", "gpu", "models", "tasks", "receipts", "earnings",
              "token_plan", "agent", "access", "yield", "account", "logs"]
SPEC_TITLES = ["总览", "性能基准", "GPU 监控", "模型与探针", "任务队列", "收据", "收益",
               "提供 Token Plan", "Agent 接入", "本机设置", "暂停接单", "账户与安全",
               "日志"]
#: 分组口径（00-PLAN §1.1 已裁决）：组 key / 中文名 / 拉丁微标 / 成员顺序
SPEC_SECTIONS = [
    # 2026-11-23 R2.1 ⑤：微标中文化（英文眉标残留 0 处），组顺序/成员一个不差
    ("run", "运行", "运行", ["overview", "bench", "gpu", "models", "tasks"]),
    ("earn", "收益", "收益", ["receipts", "earnings", "token_plan"]),
    ("connect", "接入", "接入", ["agent", "access"]),
    ("system", "系统", "系统", ["yield", "account", "logs"]),
]
#: 合规红线：不许出现「许愿式」金融承诺。**唯一例外**是 owner 2026-11-16 D-2 那句结算口径
#: （`views.SETTLE_NOTE`，原话里含「提现」）；除它之外任何地方出现「提现」都算违规——
#: 旧注释写的「『不可提现』这类否定式免责声明是允许的」已作废，别再照它写文案。
FORBIDDEN_PROMISES = ("充值", "APY", "保本", "固定收益", "储值账户", "稳赚", "保证收益")


def snapshot_all_unavailable(reason="测试：数据源未接线"):
    return ds.Snapshot({name: ds.Unavailable(reason) for name in ds.SECTIONS})


# ─────────────────────────────────────────────────────────────── 结构
def test_thirteen_views_in_spec_order():
    assert list(vw.VIEW_KEYS) == SPEC_ORDER
    for (key, title), expected_key, expected_title in zip(vw.VIEWS, SPEC_ORDER, SPEC_TITLES):
        assert key == expected_key
        assert title.replace(" ", "") == expected_title.replace(" ", "")


def test_nav_sections_cover_every_view_exactly_once():
    """分组是「加结构」不是「删项/并项」：13 项展平后必须与 VIEW_KEYS 完全一致。"""
    assert len(vw.NAV_SECTIONS) == 4
    flattened = [key for section in vw.NAV_SECTIONS for key in section.keys]
    assert flattened == SPEC_ORDER
    assert sorted(flattened) == sorted(vw.VIEW_KEYS)
    assert len(flattened) == len(set(flattened)) == 13
    # 每个视图都能查到所属分组，且不认识的 key 直接报错（不猜）
    for key in vw.VIEW_KEYS:
        assert vw.SECTION_FOR_VIEW[key].key in {section.key for section in vw.NAV_SECTIONS}
    with pytest.raises(KeyError):
        vw.page_kicker("nope")


def test_nav_sections_follow_the_agreed_groups():
    """钉住 00-PLAN §1.1 裁决：4 组、组名/微标/成员顺序一个不差。"""
    actual = [(section.key, section.title, section.micro, list(section.keys))
              for section in vw.NAV_SECTIONS]
    assert actual == SPEC_SECTIONS
    # 组顺序 = 用户旅程：先看运行，再看收益，要扩大供给才看接入，系统放最后
    assert [section.key for section in vw.NAV_SECTIONS] == ["run", "earn", "connect", "system"]


def test_every_page_kicker_is_two_level_chinese():
    """B3 / R2.1 ⑤：页头眉标 =「组名 / 页名」两级；**不含英文**（英文残留 0 处）。"""
    from djb_node.desktop import ia
    for key in vw.VIEW_KEYS:
        kicker = vw.page_kicker(key)
        assert not ia.has_english(kicker), kicker
        parts = kicker.split(" / ")
        assert len(parts) == 2, kicker
        assert parts[0] == vw.SECTION_FOR_VIEW[key].micro
        assert parts[1] == vw.PAGE_MICROS[key]
    assert vw.page_kicker("overview") == "运行 / 总览"
    assert vw.page_kicker("token_plan") == "收益 / 提供套餐"
    assert vw.page_kicker("logs") == "系统 / 运行日志"


def test_every_view_renders_from_demo_data():
    snapshot = demo_snapshot()
    for key in vw.VIEW_KEYS:
        view = vw.build_view(key, snapshot)
        assert isinstance(view, vw.View)
        assert view.title and view.subtitle
        for name in ("cards", "actions", "notes", "unavailable"):
            assert isinstance(getattr(view, name), tuple), (key, name)
        assert view.plain_text().strip()


def test_every_view_survives_total_outage():
    snapshot = snapshot_all_unavailable()
    for key in vw.VIEW_KEYS:
        view = vw.build_view(key, snapshot)
        text = view.plain_text()
        assert "暂不可用" in text, key
        assert "Traceback" not in text
        # 缺数据不许显示成 0
        assert "0 张" not in text and "0.00 USDT" not in text


def test_unknown_view_key_is_rejected():
    with pytest.raises((KeyError, ValueError)):
        vw.build_view("nope", demo_snapshot())


def test_copy_has_no_forbidden_promises():
    snapshot = demo_snapshot()
    for key in vw.VIEW_KEYS:
        text = vw.build_view(key, snapshot).plain_text()
        for word in FORBIDDEN_PROMISES:
            assert word not in text, (key, word)
        # 「提现」只允许出现在 SETTLE_NOTE 这一句里（RC15-039 #34：唯一出处）；其余地方
        # 一律不许提，更不许承诺可提现——这句是 owner D-2 的原话，本身就是否定式
        # 「收益不等于可提现余额」，所以旧规则「必须以『不可』开头」改成「必须在常量内」。
        masked = text.replace(vw.SETTLE_NOTE, "")
        assert "提现" not in masked, (key, masked[max(0, masked.find("提现") - 12):][:24])


def test_never_leaks_secrets():
    config = ac.AccessConfig(node_id="n1", api_key=SECRET, node_secret=SECRET,
                             scoped_token=SECRET)
    snapshot = demo_snapshot()
    sections = dict(snapshot.sections)
    sections["access"] = {"config": config.display(), "valid": True, "binding": None,
                          "note": "", "path": "~/.djb/desktop/access-config.json"}
    for key in ("access", "agent"):
        text = vw.build_view(key, ds.Snapshot(sections)).plain_text()
        assert SECRET not in text


# ─────────────────────────────────────────────────────────────── 具体口径
def test_overview_three_redundant_signals():
    view = vw.build_view("overview", demo_snapshot())
    text = view.plain_text()
    assert vw.Mark.DONE in text                       # 图标 + 文案，不只靠颜色
    assert "运行中" in text


def test_missing_value_is_dash_not_zero():
    sections = dict(demo_snapshot().sections)
    sections["earnings"] = {"balance": None, "today": None, "currency": "USDT"}
    view = vw.build_view("overview", ds.Snapshot(sections))
    assert "—" in view.plain_text()


def test_gpu_absent_says_so():
    """无 GPU 时必须说实话。

    演示数据自设计改版起**带一块卡**（否则总览的仪表环与四指标全是「—」，实拍看不出设计），
    所以这里不再拿 demo 快照当「无 GPU」样本，而是自造一个 no-GPU 快照——
    断言的意图不变：没有设备就明说，不许留空表假装正常。
    """
    sections = dict(demo_snapshot().sections)
    sections["gpu"] = {"present": False, "devices": [],
                       "note": "本机无可用 GPU（没有 nvidia-smi；没有 rocm-smi）"}
    view = vw.build_view("gpu", ds.Snapshot(sections))
    text = view.plain_text()
    assert "无可用 GPU" in text or "暂不可用" in text
    assert view.table is not None and not view.table.rows


def test_gpu_absent_keeps_the_overview_gauge_honest():
    """没有 GPU 时仪表环给「—」，**不许**画成 0%（0 和「没有数据」是两件事）。"""
    sections = dict(demo_snapshot().sections)
    sections["gpu"] = {"present": False, "devices": [], "note": "无 GPU"}
    hero = vw.build_view("overview", ds.Snapshot(sections)).hero
    assert hero is not None and hero.gauge is not None
    assert hero.gauge.ratio is None and hero.gauge.value == "—"
    assert {metric.label: metric.value for metric in hero.metrics}["显存"] == "—"


def test_models_incompatible_rows_greyed_with_reason():
    """云端目录（附加表）里不兼容的模型仍要标灰 + 给原因（0.3.6 的验收不回退）。"""
    view = vw.build_view("models", demo_snapshot())
    catalog = view.tables[0]                      # 主表已换成探针事实（RC14 D）
    greyed = [row for row in catalog.rows if row.disabled]
    assert greyed, "演示数据里应有不兼容模型"
    assert all(row.cells[-1] for row in greyed), "标灰必须给原因"


def test_models_page_projects_probe_details_with_three_state_columns():
    """D（RC14）：探针表纯投影 probe_details()，三态不混（None ≠ False）。"""
    view = vw.build_view("models", demo_snapshot())
    assert view.title == "模型与探针"
    assert view.table.columns[:3] == ("模型", "上游", "最近探测")
    assert set(view.table.columns[3:6]) == {"L2", "L3", "用量"}
    rows = {row.cells[0]: row for row in view.table.rows}
    assert len(rows) == 4, "演示数据的四行探针结论都该出现"
    # 通过：三列全 ✓、状态可公告
    ok = rows["qwen2.5-7b-instruct"]
    assert ok.cells[3:6] == ("✓", "✓", "✓") and ok.cells[6] == "可公告"
    # L3 失败：L2 ✓ / L3 ✗，状态「降级：流式不可用」，原因翻译成人话
    degraded = rows["llama-3.1-8b-instruct"]
    assert degraded.cells[3:6] == ("✓", "✕", "✓")
    assert degraded.cells[6].startswith("降级")
    assert "流式不可用" in degraded.cells[7]
    # 失败：L2 ✗，原因「密钥被拒」
    failed = rows["claude-3-7-sonnet"]
    assert failed.cells[3] == "✕" and failed.cells[6] == "失败"
    assert "密钥被拒" in failed.cells[7]
    # 从未探测：三列全是「—」（不是 ✗），最近探测「从未」
    never = rows["gpt-4o-mini"]
    assert never.cells[3:6] == ("—", "—", "—")
    assert never.cells[2] == "从未" and never.cells[6] == "未探测"


def test_models_page_counts_and_probe_action():
    view = vw.build_view("models", demo_snapshot())
    cards = {card.title: card.value for card in view.cards}
    assert cards["可公告"] == "1 个" and cards["从未探测"] == "1 个"
    actions = {action.key: action for action in view.actions}
    assert actions["probe.run"].enabled is True
    # 两个新鲜窗口的数字只能来自 facts（900 / 600）
    assert any("15 分钟内" in note for note in view.notes)
    assert any("10 分钟内" in note for note in view.notes)


def test_models_page_without_upstreams_says_where_to_start():
    """没有上游时：探针表空态指路「提供 Token Plan」，且「立即探测」置灰带原因。"""
    sections = dict(demo_snapshot().sections)
    sections["models"] = {"catalog": [], "local": [], "online": True, "note": ""}
    view = vw.build_view("models", Snapshot(sections=sections))
    assert "提供 Token Plan" in view.table.empty
    actions = {action.key: action for action in view.actions}
    assert actions["probe.run"].enabled is False
    assert "还没有接入上游" in actions["probe.run"].reason


def test_yield_actions_follow_state():
    online = vw.build_view("yield", demo_snapshot())
    pause = {a.key: a for a in online.actions}
    assert pause["yield.pause"].enabled is True
    assert pause["yield.resume"].enabled is False

    sections = dict(demo_snapshot().sections)
    sections["yield"] = {"state": "paused", "reason": "user", "reason_label": "本机暂停",
                         "accepting": False, "mode": "paused"}
    paused = vw.build_view("yield", ds.Snapshot(sections))
    actions = {a.key: a for a in paused.actions}
    assert actions["yield.pause"].enabled is False and actions["yield.pause"].reason
    assert actions["yield.resume"].enabled is True
    assert "已暂停" in paused.plain_text()


def test_access_fields_are_contribution_node_only():
    """本机设置页只放**本机**配置；绑定与收款已搬去「账户与安全」（RC14 E）。

    字段收敛到 node_id / 两个排障只读 / 价格模式 / 节点凭证；``usdt_address`` 与
    API Key / 两个上游地址都不在本页（前者的唯一位在账户页，见下一个用例）。
    """
    view = vw.build_view("access", demo_snapshot())
    keys = {field.key for field in view.fields}
    for needed in ("node_id", "node_secret", "price_mode", "daemon_state", "register_state"):
        assert needed in keys, needed
    for removed in ("api_key", "openai_base_url", "anthropic_base_url", "usdt_address"):
        assert removed not in keys, removed
    assert view.title == "本机设置"
    assert "API Key" not in view.plain_text()
    assert "OpenAI 请求地址" not in view.plain_text()
    assert "Anthropic 请求地址" not in view.plain_text()
    secrets = [f for f in view.fields if f.secret]
    assert {f.key for f in secrets} == {"node_secret"}
    assert all("…" in f.value or f.value in ("—", "未设置") for f in secrets)


def test_access_removed_fields_stay_in_model_and_validate():
    """「UI 移除、模型保留」：字段与校验规则一个都没删，老配置文件照常读。

    这条是给 release-manager 的护栏：页面精简**不许**顺手删 ``AccessConfig`` 字段或放宽
    ``validate()``（否则老配置 / 老 profile 会被判不合法）。
    """
    fields = set(ac.AccessConfig.__dataclass_fields__)
    assert {"api_key", "openai_base_url", "anthropic_base_url"} <= fields
    config = ac.AccessConfig(api_key="k", openai_base_url="http://127.0.0.1:1717/v1",
                             anthropic_base_url="http://127.0.0.1:1717")
    config.validate()                        # 三个字段仍然参与校验，且合法值不被拒
    broken = ac.AccessConfig(openai_base_url="ftp://上游改坏了")
    with pytest.raises(ValueError):
        broken.validate()
    # 收款地址校验只加不改：TRC20 仍被接受（老配置兼容），仅「提交云端」这条路只放行 EVM
    trc20 = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"     # 合法 base58，验「老配置仍能读」
    assert ac.validate_address(trc20) == trc20


def test_unwired_views_say_unavailable_not_fake_success():
    for key, action in (("receipts", "receipts.verify"),
                        ("earnings", "earnings.export"), ("account", "account.logout")):
        view = vw.build_view(key, demo_snapshot())
        assert action in {a.key for a in view.actions}, key


def test_models_page_no_longer_promises_a_save_that_does_nothing():
    """G1-1（RC14 P0）：模型页摘掉「保存并上报意向」，且不再写「逐模型开关」。

    三处一起改才算修好：按钮（点了永远无声）、表头note（暗示可编辑）、副标题
    （「逐模型开关」是**不存在**的能力）。只删按钮而留文案 = 同一个谎话换个地方说。
    """
    view = vw.build_view("models", demo_snapshot())
    assert all("models." not in action.key for action in view.actions)
    assert "逐模型开关" not in (view.subtitle or "")
    assert "逐模型开关" not in " ".join(view.notes)
    # 但必须留下**去哪里设置**的指路（否则客户不知道该点哪）
    assert any("Token Plan" in note for note in view.notes)


# ─────────────────────────────── 共享 Token Plan（第 13 个导航页，任务 A）
def _token_plan_snapshot(*, upstreams, bound=True, usage=None):
    """三种状态共用的一份快照：上游清单 / 绑定态 / 今日用量都可调。"""
    sections = dict(demo_snapshot().sections)
    sections["token_plan"] = {
        "upstreams": list(upstreams),
        "limits": {"daily_tokens": 200000, "weekly_tokens": 0, "monthly_tokens": 0,
                   "concurrent": 4},
        "usage": list(usage or []), "preset": "balanced",
        "node": {"node_id": "node-7c31" if bound else None, "bound": bound},
        "note": ""}
    sections["access"] = {"config": {"node_id": "node-7c31"},
                          "valid": True, "saved": True,
                          "binding": {"state": "bound" if bound else "unbound"},
                          "note": "", "path": "~/.djb/desktop/access-config.json"}
    return ds.Snapshot(sections)


def test_token_plan_nav_item_sits_before_access_in_earn_section():
    """客户找不到入口 = 导航表里根本没有；这条钉住「标题、在接入配置之前、属收益组末位」。

    R2 分组后它从平铺第 9 项变为第 8 项（组内顺序变了，产品意图没变）：
    先看到「共享了什么、还能共享多少」，再进「接入」组动手。
    """
    assert ("token_plan", "提供 Token Plan") in vw.VIEWS
    order = list(vw.VIEW_KEYS)
    assert order.index("token_plan") == 7                     # 第 8 项
    assert order.index("token_plan") < order.index("access")
    assert vw.SECTION_FOR_VIEW["token_plan"].key == "earn"
    assert vw.SECTION_FOR_VIEW["access"].key == "connect"
    # 收益组的最后一项就是它：组内不会被别的项挤到后面
    assert vw.SECTION_FOR_VIEW["token_plan"].keys[-1] == "token_plan"
    assert len(vw.VIEWS) == 13


def test_token_plan_key_is_showable_and_renders_plain_text(tmp_path):
    """tools/live_views.py 与无显示环境验收都走 controller.show(key).plain_text()。"""
    controller = make_controller(tmp_path)
    view = controller.show("token_plan")
    assert view.key == "token_plan" and view.title == "提供 Token Plan"
    text = view.plain_text()
    assert text.startswith("# 提供 Token Plan")
    assert controller.view == "token_plan"


def test_token_plan_lists_upstreams_with_state_models_and_qps():
    view = vw.build_view("token_plan", demo_snapshot())
    text = view.plain_text()
    assert view.table is not None
    # RC14 I/J：主表变成「逐模型底价」，上游清单是第二张表（tables[0]）
    upstream_table = view.tables[0]
    assert [row.cells[0] for row in upstream_table.rows] == ["up_moonshot_01", "up_local_agent"]
    assert "✓ 已验证" in text and "◐ 已申报（待验证）" in text
    assert "3 个（已验证 2）" in text                          # 申报数 ≠ 已验证数
    assert "最大 QPS" in text


def test_token_plan_empty_state_tells_customer_what_to_do():
    snapshot = _token_plan_snapshot(upstreams=[])
    view = vw.build_view("token_plan", snapshot)
    text = view.plain_text()
    assert "你还没有接入任何 Token Plan" in text
    assert "读取模型清单" in text                               # 空态必须给下一步，不是只说暂无
    assert view.table is not None and not view.table.rows
    assert not view.tables[0].rows


def test_token_plan_unbound_state_points_at_binding():
    snapshot = _token_plan_snapshot(upstreams=[], bound=False)
    text = vw.build_view("token_plan", snapshot).plain_text()
    assert "✕ 未绑定" in text
    assert "还没绑定" in text and "账户与安全" in text


def test_token_plan_says_unavailable_instead_of_zero_when_data_missing():
    snapshot = ds.Snapshot({name: ds.Unavailable("测试：数据源未接线") for name in ds.SECTIONS})
    text = vw.build_view("token_plan", snapshot).plain_text()
    assert "暂不可用" in text
    assert "0 条上游" not in text                              # 缺数据 ≠ 零上游
    assert "未启用" not in text                                # 限额读不到时不许编「周/月未启用」


def test_token_plan_today_card_uses_limiter_usage_and_receipts():
    usage = [{"name": "daily", "limit": 200000, "used": 41892, "remaining": 158108}]
    snapshot = _token_plan_snapshot(
        upstreams=[{"id": "u1", "access_type": "A1", "state": "verified", "enabled": True,
                    "model_count": 1, "verified_models": 1, "max_qps": 1.0}],
        usage=usage)
    text = vw.build_view("token_plan", snapshot).plain_text()
    assert "41892 tokens" in text
    # 收据数来自 receipts 域，别写死条数（演示数据会随版式需要增删）
    rows = snapshot.sections["receipts"]["rows"]
    assert str(len(rows)) + " 张收据" in text


def test_token_plan_open_access_is_a_goto_action_without_network(tmp_path):
    """主操作只换视图：不发请求、不写配置（写入仍走 access.save 那条老路）。"""
    controller = make_controller(tmp_path)
    calls = controller.source.calls
    result = controller.action("token_plan.open_access")
    assert result.ok and result.effect == "goto" and result.payload == "access"
    assert controller.source.calls == calls                    # 没有触发任何取数/网络


def test_token_plan_disabled_upstream_is_labelled_not_hidden():
    snapshot = _token_plan_snapshot(
        upstreams=[{"id": "u1", "access_type": "A1", "state": "verified", "enabled": False,
                    "model_count": 2, "verified_models": 2, "max_qps": 0.0}])
    view = vw.build_view("token_plan", snapshot)
    assert len(view.tables[0].rows) == 1
    assert "已停用" in view.plain_text()


# ─────────────────────────────────────────────────────────────── 控制器
def make_controller(tmp_path, *, sections=None, store=None, control=None, bus=None):
    sections = sections if sections is not None else dict(demo_snapshot().sections)
    source = ds.FakeDataSource(sections, source="test")
    access = ac.AccessService(store or ac.MemoryAccessStore(),
                              reloader=lambda: ac.RELOAD_UNSUPPORTED)
    return DesktopController(source, access=access, control=control, bus=bus)


def test_controller_starts_on_overview_and_switches(tmp_path):
    controller = make_controller(tmp_path)
    assert controller.view == "overview"
    assert controller.show("logs").key == "logs"
    assert controller.next_view().key == "overview"          # 13 项循环
    assert controller.next_view(-1).key == "logs"
    with pytest.raises(ValueError):
        controller.show("nope")


def test_controller_refresh_updates_message_and_timestamp(tmp_path):
    controller = make_controller(tmp_path)
    controller.refresh()
    assert "已刷新" in controller.message


def test_controller_refresh_survives_source_crash(tmp_path):
    class Broken:
        def snapshot(self, sections=None):
            raise RuntimeError("boom")

    controller = DesktopController(Broken())
    view = controller.refresh()
    assert "刷新失败" in controller.message
    assert "暂不可用" in view.plain_text()


def test_controller_bench_run_then_copy(tmp_path):
    controller = make_controller(tmp_path)
    first = controller.action("bench.run")
    assert first.ok is True
    assert controller.benchmark and "hash_mbps" in controller.benchmark
    copied = controller.action("bench.copy")
    assert copied.effect == "copy" and "sha256=" in copied.payload


def test_controller_log_filter_keeps_only_level(tmp_path):
    controller = make_controller(tmp_path)
    controller.show("logs")
    view = controller.set_log_level("error")
    assert view.table.rows
    assert all(any("error" in cell for cell in row.cells) for row in view.table.rows)
    assert controller.set_log_level("all").table.rows


def test_controller_save_access_roundtrip(tmp_path):
    store = ac.MemoryAccessStore()
    access = ac.AccessService(store, reloader=lambda: ac.RELOAD_OK)
    controller = DesktopController(ds.FakeDataSource(dict(demo_snapshot().sections)),
                                   access=access)
    controller.set_draft("api_key", SECRET)
    controller.set_draft("usdt_address", "0x" + "a1" * 20)
    result = controller.action("access.save")
    assert result.ok is True
    assert store.load().api_key == SECRET
    assert controller.drafts == {}                       # 成功后清草稿
    assert "生效" in result.message


def test_controller_blank_secret_keeps_previous(tmp_path):
    store = ac.MemoryAccessStore(ac.AccessConfig(api_key=SECRET, node_id="n1"))
    access = ac.AccessService(store, reloader=lambda: ac.RELOAD_OK)
    controller = DesktopController(ds.FakeDataSource(dict(demo_snapshot().sections)),
                                   access=access)
    controller.set_draft("node_id", "n2")
    controller.set_draft("api_key", "   ")
    controller.action("access.save")
    saved = store.load()
    assert saved.api_key == SECRET and saved.node_id == "n2"


def test_controller_invalid_draft_does_not_touch_store(tmp_path):
    store = ac.MemoryAccessStore(ac.AccessConfig(api_key=SECRET))
    controller = DesktopController(ds.FakeDataSource(dict(demo_snapshot().sections)),
                                   access=ac.AccessService(store))
    controller.set_draft("usdt_address", "bogus")
    result = controller.action("access.save")
    assert result.ok is False and "USDT" in result.message
    assert store.load().api_key == SECRET


def test_controller_save_without_reload_says_restart(tmp_path):
    store = ac.MemoryAccessStore()
    controller = DesktopController(
        ds.FakeDataSource(dict(demo_snapshot().sections)),
        access=ac.AccessService(store, reloader=lambda: ac.RELOAD_UNSUPPORTED))
    controller.set_draft("api_key", SECRET)
    result = controller.action("access.save")
    assert result.ok is False
    assert "重启 daemon" in result.message and "配置生效" not in result.message


def test_controller_unwired_action_is_honest(tmp_path):
    controller = make_controller(tmp_path)
    for key in ("account.enable_2fa", "nope.unknown"):
        result = controller.action(key)
        assert result.ok is False, key
        assert "暂不可用" in result.message, key


def test_controller_2fa_opens_the_console_security_page(tmp_path, monkeypatch):
    """G1-2：2FA 的**真入口**——把客户送到控制台安全页，而不是回一句「暂不可用」。"""
    from djb_node import facts

    monkeypatch.setattr(facts, "WEB_CONSOLE_URL", "https://console.example.test")
    result = make_controller(tmp_path).action("account.open_2fa_page")
    assert result.effect == "open_url"
    assert result.payload == "https://console.example.test" + facts.WEB_CONSOLE_SECURITY_PATH
    assert result.payload.endswith("/console/security")


def test_receipts_verify_and_earnings_export_are_no_longer_stubs(tmp_path):
    """三个旧桩里的两个已接线：没选中/没数据时给的是「怎么做」，不是「未接线」。"""
    controller = make_controller(tmp_path)
    verify = controller.action("receipts.verify")
    export = controller.action("earnings.export")
    for result in (verify, export):
        assert result.ok is False
        assert "未接线" not in result.message
    assert "选中一条收据" in verify.message
    # 演示快照里有明细行，但测试里没有保存对话框 → 如实说「没有对话框」，也不假装导出成功
    assert "保存对话框" in export.message or "没有可导出的记录" in export.message


def test_logout_is_no_longer_a_stub(tmp_path):
    """登出已接线：没登录时说「无需登出」，登录了要二次确认——不是「未接线」。"""
    result = make_controller(tmp_path).action("account.logout", confirm=lambda _m: True)
    assert result.ok is False
    assert "未接线" not in result.message
    assert "尚未登录" in result.message or "本机命令通道未接通" in result.message


def test_controller_pause_requires_confirmation(tmp_path):
    class Channel:
        def __init__(self):
            self.calls = []

        def control(self, action, confirm):
            self.calls.append(action)
            if not confirm("确认暂停？"):
                return ControlOutcome(False, "已取消，未执行任何操作", "online")
            return ControlOutcome(True, "已暂停并回读确认", "paused")

    channel = Channel()
    controller = make_controller(tmp_path, control=channel)
    denied = controller.action("yield.pause")            # 默认 confirm 为 False
    assert denied.ok is False and "取消" in denied.message
    granted = controller.action("yield.pause", confirm=lambda _m: True)
    assert granted.ok is True and granted.payload == "paused"
    assert channel.calls == ["pause", "pause"]


def test_controller_publishes_state_to_bus(tmp_path):
    from djb_node.gui.state import ControlBus

    bus = ControlBus()
    seen = []
    bus.subscribe(seen.append)
    controller = make_controller(tmp_path, bus=bus)
    controller.refresh()
    assert seen and seen[-1].state == "online"
    assert bus.published >= 1


def test_controller_status_line_never_blank(tmp_path):
    controller = make_controller(tmp_path, sections={})
    assert "暂不可用" in controller.status_line()


def test_controller_action_exception_does_not_escape(tmp_path):
    def boom(**kwargs):
        raise RuntimeError("跑分炸了（内部路径 /root/x）")

    controller = make_controller(tmp_path)
    controller.benchmark_runner = boom
    result = controller.action("bench.run")
    assert result.ok is False
    assert "跑分失败" in result.message and "RuntimeError" in result.message
    assert "/root" not in result.message                 # 不把内部路径带上屏


def test_access_view_says_unsaved_when_no_config_file(tmp_path):
    """没保存过就说「尚未保存」：不许把「默认值通过校验」说成配置有效。"""
    from djb_node.desktop.datasource import Snapshot, FakeDataSource

    config = {"node_id": "—", "node_secret": "未设置", "api_key": "未设置",
              "price_mode": "auto", "price_mode_label": "跟随平台建议价",
              "usdt_address": "—", "openai_base_url": "未设置",
              "anthropic_base_url": "未设置"}
    source = FakeDataSource({"access": {"config": config, "valid": None, "saved": False,
                                        "binding": None, "note": "", "path": "/tmp/x"}})
    text = vw.build_view("access", source.snapshot()).plain_text()
    assert "尚未保存" in text
    assert "通过校验" not in text


def test_access_view_says_valid_after_save(tmp_path):
    from djb_node.desktop.datasource import FakeDataSource

    config = {"node_id": "node-7c31", "node_secret": "djb_…77be", "api_key": "未设置",
              "price_mode": "auto", "price_mode_label": "跟随平台建议价",
              "usdt_address": "—", "openai_base_url": "未设置",
              "anthropic_base_url": "未设置"}
    source = FakeDataSource({"access": {"config": config, "valid": True, "saved": True,
                                        "binding": {"state": "bound", "node_id": "node-7c31"},
                                        "note": "", "path": "/tmp/x"}})
    text = vw.build_view("access", source.snapshot()).plain_text()
    assert "通过校验" in text and "已绑定" in text


def test_earnings_all_money_cards_say_unavailable_together():
    """整域取不到时四张金额卡口径一致（不许一张「暂不可用」三张「—」）。"""
    from djb_node.desktop.datasource import FakeDataSource

    source = FakeDataSource({})          # 只给空 sections：earnings 不可用
    text = vw.build_view("earnings", source.snapshot()).plain_text()
    assert text.count("暂不可用") >= 4
    assert "0.00" not in text and "USDT 0" not in text


def test_account_view_shows_node_identity_from_console_api():
    """F16：节点侧 profile 给的是节点身份，不是邮箱；视图要如实展示节点字段。"""
    from djb_node.desktop.datasource import FakeDataSource

    section = {"account": {"profile": {"node_id": "node-7c31", "state": "active",
                                       "region": "cn-east", "platform": "linux",
                                       "bound": True,
                                       "last_seen_at": "2026-11-01T10:00:00Z"},
                           "security": {"storage": "file", "node_id": "node-7c31"},
                           "note": ""}}
    text = vw.build_view("account", FakeDataSource(section).snapshot()).plain_text()
    assert "node-7c31" in text and "已绑定" in text
    assert "cn-east" in text and "linux" in text
    # F46：时间要按本地时区印（原来是 UTC 原串 + 6 位微秒），但日期本身不许丢
    assert "2026-11-01" in text and "T10:00:00Z" not in text
    assert "邮箱" not in text                      # 节点接口没有邮箱，不编一个出来


def test_earnings_table_is_labelled_detail_not_payout():
    from djb_node.desktop.datasource import FakeDataSource

    section = {"earnings": {"payouts": [{"id": "req-1", "amount": "1.50", "currency": "USDT",
                                         "status": "settled", "created_at": "2026-11-01"}],
                            "settled": "1.50", "pending": None, "currency": "USDT"}}
    text = vw.build_view("earnings", FakeDataSource(section).snapshot()).plain_text()
    assert "明细 | 金额 | 币种 | 状态 | 时间" in text.replace("\n", " ")
    assert "出款" not in text


def test_account_view_labels_email_as_email_when_no_node_id():
    """没有 node_id 时第一栏必须叫「邮箱」，不能把邮箱标成「节点标识」。"""
    from djb_node.desktop.datasource import FakeDataSource

    section = {"account": {"profile": {"email": "node@example.com", "role": "共享方",
                                       "twofa_enabled": False},
                           "security": {"storage": "file", "node_id": "node-7c31"},
                           "note": ""}}
    text = vw.build_view("account", FakeDataSource(section).snapshot()).plain_text()
    assert "节点标识" not in text
    assert "邮箱" in text and "node@example.com" in text


# ─────────────────────────────────────────────────────────── 登出（解绑）真的接到 CLI
def make_logout_controller(calls: list, *, payload=None, error=None):
    from djb_node.desktop.controller import DesktopController
    from djb_node.desktop.datasource import FakeDataSource

    section = {"account": {"profile": {"node_id": "node-7c31", "state": "active"},
                           "security": {"storage": "file", "node_id": "node-7c31"},
                           "note": ""}}

    def cli(argv):
        calls.append(list(argv))
        if error is not None:
            raise error
        return payload if payload is not None else {"unbound": True}

    return DesktopController(FakeDataSource(section), cli=cli)


def test_logout_requires_confirmation_and_does_nothing_when_declined():
    calls: list = []
    controller = make_logout_controller(calls)
    result = controller.action("account.logout", confirm=lambda _m: False)
    assert result.ok is False and result.effect == "cancelled"
    assert calls == []                                   # 没确认就绝不动凭据
    assert "未改动" in result.message


def test_logout_unbinds_through_the_cli():
    calls: list = []
    controller = make_logout_controller(calls)
    result = controller.action("account.logout", confirm=lambda _m: True)
    assert calls == [["auth", "--unbind", "--yes"]]
    assert result.ok is True and "已登出" in result.message


def test_logout_failure_keeps_credentials_and_says_why():
    from djb_node.desktop.datasource import ProviderError

    calls: list = []
    controller = make_logout_controller(
        calls, error=ProviderError("无法连接服务端（gw.test）：网络或服务不可用"))
    result = controller.action("account.logout", confirm=lambda _m: True)
    assert result.ok is False
    assert "登出未成功" in result.message and "网络或服务不可用" in result.message
    assert "本机凭据未改动" in result.message


def test_logout_without_node_says_nothing_to_do():
    from djb_node.desktop.controller import DesktopController
    from djb_node.desktop.datasource import FakeDataSource

    calls: list = []
    controller = DesktopController(FakeDataSource({"account": {"profile": {}, "security": {},
                                                               "note": ""}}),
                                   cli=lambda argv: calls.append(argv))
    result = controller.action("account.logout", confirm=lambda _m: True)
    assert result.ok is False and "尚未登录" in result.message
    assert calls == []


# ─────────────────────────────────────────────────────────── 校验收据（云端单条 vs 本机缓存）
LOCAL_RECEIPT = {"receipt_id": "rc-1", "task_id": "t-1", "access": "A1",
                 "sig_provider": "hmac-sha256:" + "ab" * 32,
                 "price": {"amount": "0.0012", "unit": "USDT"}}


def make_verify_controller(*, local=None, cloud=None, rows=None):
    from djb_node.desktop.controller import DesktopController
    from djb_node.desktop.datasource import FakeDataSource, ProviderError

    receipt_rows = rows if rows is not None else [
        {"receipt_id": "rc-1", "task_id": "t-1", "status": "verified",
         "hash": "abababab…", "revenue_minor": 1_200}]

    class Source(FakeDataSource):
        def local_receipt(self, receipt_id):
            if isinstance(local, Exception):
                raise local
            return local if local is not None else LOCAL_RECEIPT

        def cloud_receipt(self, receipt_id):
            if isinstance(cloud, Exception):
                raise cloud
            return cloud if cloud is not None else {"receipt_id": "rc-1", "state": "accepted"}

    return DesktopController(Source({"receipts": {"rows": receipt_rows, "note": ""}}))


def test_verify_needs_a_selected_row():
    controller = make_verify_controller()
    result = controller.action("receipts.verify")
    assert result.ok is False and "选中一条收据" in result.message


def test_verify_matching_receipt_says_verified():
    controller = make_verify_controller()
    controller.select_row("receipts", "rc-1")
    result = controller.action("receipts.verify")
    assert result.ok is True and "已验证" in result.message and "accepted" in result.message


def test_verify_lists_differing_fields_with_both_values():
    rows = [{"receipt_id": "rc-1", "task_id": "t-9", "status": "verified",
             "hash": "ffffffff…", "revenue_minor": 2_000}]
    controller = make_verify_controller(rows=rows)
    controller.select_row("receipts", "rc-1")
    result = controller.action("receipts.verify")
    assert result.ok is False and "不一致" in result.message
    for field in ("task_id", "hash", "金额"):
        assert field in result.message


def test_verify_unreachable_cloud_is_unavailable_not_a_verdict():
    from djb_node.desktop.datasource import ProviderError

    controller = make_verify_controller(cloud=ProviderError("云端连不上（OSError）"))
    controller.select_row("receipts", "rc-1")
    result = controller.action("receipts.verify")
    assert result.ok is False
    assert "暂不可用" in result.message and "verified" not in result.message


def test_verify_without_local_cache_is_honest():
    controller = make_verify_controller(local=None)
    controller.source.local_receipt = lambda _rid: None          # 本机没有这条
    controller.select_row("receipts", "rc-1")
    result = controller.action("receipts.verify")
    assert result.ok is False and "本机没有" in result.message


# ─────────────────────────────────────────────────────────── 导出收益明细（本地 CSV）
def test_earnings_export_writes_csv_with_bom_and_verifies_row_count(tmp_path):
    from djb_node.desktop.controller import DesktopController
    from djb_node.desktop.datasource import FakeDataSource

    section = {"earnings": {"settled": "1.50", "pending": None, "currency": "USDT",
                            "payouts": [
                                {"id": "req-1", "task_id": "t-1", "amount": "1.50",
                                 "currency": "USDT", "status": "settled",
                                 "created_at": "2026-11-01T10:00:00Z"},
                                {"id": "req-2", "task_id": "t-2", "amount": "0.25",
                                 "currency": "USDT", "status": "settled",
                                 "created_at": "2026-11-01T10:05:00Z"}]}}
    target = tmp_path / "out" / "earnings.csv"
    controller = DesktopController(FakeDataSource(section), save_path=lambda _name: str(target))
    result = controller.action("earnings.export")
    assert result.ok is True and "已导出 2 行" in result.message and "回读 2 行一致" in result.message
    raw = target.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")                       # BOM：Excel 不乱码
    text = raw.decode("utf-8-sig")
    lines = text.splitlines()
    assert lines[0] == "时间,任务,请求,收据,Token,金额"
    assert "2026-11-01T10:00:00Z,t-1,req-1,—,—,1.50" in lines[1]
    assert "0.25" in lines[2]


def test_earnings_export_cancel_writes_nothing(tmp_path):
    from djb_node.desktop.controller import DesktopController
    from djb_node.desktop.datasource import FakeDataSource

    section = {"earnings": {"payouts": [{"id": "req-1", "amount": "1.00"}]}}
    controller = DesktopController(FakeDataSource(section), save_path=lambda _name: None)
    result = controller.action("earnings.export")
    assert result.ok is False and result.effect == "cancelled"
    assert list(tmp_path.iterdir()) == []


def test_earnings_export_escapes_commas_in_cells(tmp_path):
    from djb_node.desktop.controller import DesktopController
    from djb_node.desktop.datasource import FakeDataSource

    section = {"earnings": {"payouts": [{"id": "req,1", "task_id": 'ta"sk',
                                         "amount": "1.00", "created_at": "2026-11-01"}]}}
    target = tmp_path / "e.csv"
    controller = DesktopController(FakeDataSource(section), save_path=lambda _name: str(target))
    assert controller.action("earnings.export").ok is True
    line = target.read_text(encoding="utf-8-sig").splitlines()[1]
    assert '"req,1"' in line and '"ta""sk"' in line


def test_unavailable_earnings_never_says_empty(tmp_path):
    """真联调发现：详情域不可用时表格空文案还写「暂无计费明细」，读起来像「真的是 0」。"""
    from djb_node.desktop.datasource import ProviderError
    from djb_node.desktop.views import build_view

    class Cloud:
        def fetch(self, kind):
            return {"items": [], "next_cursor": None, "truncated": False}

        def earnings(self):
            raise ProviderError("该节点还没有计费记录")

    from djb_node.desktop.datasource import LocalDataSource

    # 必须给临时 home：默认 home 是真的 ~/.djb，测试绝不能去开它
    snapshot = LocalDataSource(home=tmp_path, cloud=Cloud()).snapshot()
    text = build_view("earnings", snapshot).plain_text()
    assert "（暂不可用）" in text
    assert "暂无计费明细" not in text
    assert "置灰：暂不可用" in text


def test_logout_disabled_reason_tells_which_side_is_missing():
    """云端有节点身份但本机无凭据 → 理由要说「本机无凭据」，不能说「尚未登录」。"""
    from djb_node.desktop.views import build_view

    cloud_only = {"account": {"profile": {"node_id": "n_1", "state": "active"},
                              "security": {}, "note": ""}}
    text = build_view("account", ds.FakeDataSource(cloud_only).snapshot()).plain_text()
    assert "本机无凭据" in text and "尚未登录" not in text

    nothing = {"account": {"profile": {}, "security": {}, "note": ""}}
    text = build_view("account", ds.FakeDataSource(nothing).snapshot()).plain_text()
    assert "尚未登录" in text


# ───────────────────────────────────────────── 收据状态词 / 拒绝码 / 本机校验
def _receipts_text(tmp_path, rows):
    class Cloud:
        def fetch(self, kind):
            return {"items": rows, "next_cursor": None, "truncated": False}

        def earnings(self):
            return {"settled": None, "pending": None, "payouts": [], "currency": "USDT",
                    "note": ""}

    return vw.build_view("receipts", ds.LocalDataSource(home=tmp_path, cloud=Cloud()).snapshot()).plain_text()


def test_receipt_states_are_shown_verbatim_plus_chinese(tmp_path):
    text = _receipts_text(tmp_path, [
        {"receipt_id": "r1", "state": "accepted", "state_text": "已受理", "tokens": 10,
         "created_at": "2026-11-01T00:00:00Z", "local_check": "no_local"},
        {"receipt_id": "r2", "state": "expired", "state_text": "已过期", "rejected_code": 40001,
         "reject_reason": "receipt_window_exceeded", "tokens": None,
         "created_at": "2026-11-01T00:01:00Z", "local_check": "no_local"},
    ])
    assert "✓ 已受理" in text                     # accepted 不许再被翻成「已验证」
    assert "已过期（40001 请求不合法；receipt_window_exceeded）" in text
    assert "本机无副本" in text


def test_unknown_reject_code_is_named_as_unknown(tmp_path):
    text = _receipts_text(tmp_path, [
        {"receipt_id": "r3", "state": "rejected", "state_text": "已拒绝",
         "rejected_code": 99999, "reject_reason": None, "tokens": None,
         "created_at": "2026-11-01T00:02:00Z", "local_check": "no_local"},
    ])
    assert "99999 未知拒绝码" in text              # 没登记的码如实说未知，不编解释


def test_receipt_view_counts_only_locally_matched_as_verified(tmp_path):
    text = _receipts_text(tmp_path, [
        {"receipt_id": "r1", "state": "accepted", "state_text": "已受理", "tokens": 10,
         "created_at": "2026-11-01T00:00:00Z", "local_check": "match"},
        {"receipt_id": "r2", "state": "accepted", "state_text": "已受理", "tokens": 20,
         "created_at": "2026-11-01T00:01:00Z", "local_check": "mismatch"},
        {"receipt_id": "r3", "state": "accepted", "state_text": "已受理", "tokens": 30,
         "created_at": "2026-11-01T00:02:00Z", "local_check": "no_local"},
    ])
    assert "[本机已验证] 1 张" in text
    assert "不一致 1 张" in text and "本机无副本 1 张" in text


def test_receipt_verify_compares_masked_cloud_hash_as_prefix():
    """云端 hash 是「前 8 位 + …」的掩码，只能比前缀；方向写反会把一致判成不一致。"""
    from djb_node.desktop.controller import _receipt_differences

    local = {"receipt_id": "r1", "task_id": "t1", "sig_provider": "hmac-sha256:15e80bafdeadbeef",
             "price": {"amount": "1.25"}}
    cloud = {"receipt_id": "r1", "task_id": "t1", "hash": "15e80baf…", "revenue_minor": 1_250_000}
    assert _receipt_differences(local, cloud, {"state": "accepted"}) == []

    cloud_bad = {"receipt_id": "r1", "task_id": "t1", "hash": "deadbeef…",
                 "revenue_minor": 1_250_000}
    diffs = _receipt_differences(local, cloud_bad, {"state": "accepted"})
    assert [field for field, _mine, _theirs in diffs] == ["hash"]


def test_yield_state_never_claims_running_when_daemon_stopped(tmp_path):
    """daemon 没跑时状态卡不许写「运行中」——那是把停止说成在跑。"""
    from djb_node.desktop.datasource import FakeDataSource

    snapshot = FakeDataSource({"yield": {"state": "stopped", "reason_label": "daemon 未运行",
                                         "accepting": False}}, source="test").snapshot()
    lines = [line for line in vw.build_view("yield", snapshot).plain_text().split("\n") if line]
    card = next(line for line in lines if line.startswith("[当前状态]"))
    assert "运行中" not in card
    assert "已停止" in card and "daemon 未运行" in card


def test_today_card_says_utc_boundary_so_local_midnight_does_not_look_like_a_bug():
    """「今日」按 UTC 日界：本地刚过零点但 UTC 还是昨天时，0.00 会被当成 bug——卡上要写明口径。"""
    from djb_node.desktop.datasource import FakeDataSource

    snapshot = FakeDataSource({"earnings": {"currency": "USDT", "today": "0.00", "pending": "4.15",
                                           "settled": "0.00", "payouts": []}},
                              source="test").snapshot()
    text = vw.build_view("earnings", snapshot).plain_text()
    card = next(line for line in text.split("\n") if line.startswith("[今日]"))
    assert "0.00" in card and "UTC" in card


def _account_snapshot(*, binding_state: str, profile: dict | None = None,
                      security: dict | None = None):
    from djb_node.desktop.datasource import FakeDataSource

    return FakeDataSource({
        "account": {"profile": profile or {}, "security": security or {}, "note": ""},
        "access": {"binding": {"state": binding_state}},
    }, source="test").snapshot()


def test_unbound_account_shows_bind_entry_with_code_hint_and_web_url():
    """用户反馈「没看到绑定入口」：未绑定时账户页必须有醒目按钮 + 可照抄的步骤 + 网页地址。"""
    from djb_node import facts

    view = vw.build_view("account", _account_snapshot(binding_state="unbound"))
    keys = [action.key for action in view.actions]
    assert "account.bind" in keys
    bind = next(action for action in view.actions if action.key == "account.bind")
    assert bind.primary is True and bind.label == "绑定网页端"

    text = view.plain_text()
    assert "绑定：在网页端 → 共享算力 → 节点与绑定 → 生成绑定码" in text
    # RC15-039 #11：绑定是界面内动作，提示里**不许**再甩半条 CLI 命令（客户手边没终端）
    assert "手输绑定码" in text
    assert "djb auth --bind" not in text, "CLI 不许再当解决方案出现在界面上"
    assert facts.WEB_CONSOLE_URL in text
    assert "account.open_web" in keys                 # 地址在界面上可点开


def test_bound_account_hides_bind_entry():
    """已绑定（binding.state=bound）不再显示绑定按钮，避免误以为没绑。"""
    view = vw.build_view("account", _account_snapshot(
        binding_state="bound",
        profile={"node_id": "n-1", "bound": True, "state": "active"},
        security={"node_id": "n-1", "storage": "file-0600"}))
    keys = [action.key for action in view.actions]
    assert "account.bind" not in keys
    assert "account.open_web" not in keys
    assert "绑定网页端" not in view.plain_text()


def test_bind_action_passes_code_to_cli_and_refreshes():
    """controller 的绑定动作：一次性码只进 CLI 参数，不进消息；成功后刷新快照。"""
    calls: list[list[str]] = []

    def cli(argv):
        calls.append(list(argv))
        return {"ok": True}

    controller = DesktopController(
        ds.FakeDataSource({"account": {"profile": {}, "security": {}, "note": ""}},
                          source="test"),
        cli=cli, ask_code=lambda: "  bind-code-1  ")
    result = controller.action("account.bind")
    assert calls == [["auth", "--bind", "bind-code-1"]]
    assert result.ok is True and result.effect == "refresh"
    assert "bind-code-1" not in result.message


def test_bind_action_cancel_and_failure_do_not_touch_credentials():
    calls: list[list[str]] = []
    controller = DesktopController(
        ds.FakeDataSource({"account": {"profile": {}, "security": {}, "note": ""}},
                          source="test"),
        cli=lambda argv: calls.append(list(argv)) or {"ok": True},
        ask_code=lambda: "   ")
    cancelled = controller.action("account.bind")
    assert calls == [] and cancelled.ok is False
    assert "已取消" in cancelled.message

    controller.ask_code = lambda: "bad-code"
    controller.cli = lambda argv: {"ok": False, "error": "invalid_or_expired"}
    failed = controller.action("account.bind")
    assert failed.ok is False and "invalid_or_expired" in failed.message


# ───────────── 日志页：两行同一时间戳不许把窗口打死（0.2.3 实拍崩溃）
def test_logs_rows_have_unique_keys_even_with_identical_timestamps():
    """同一微秒的两行日志（init 与 daemon 各写一遍同一事件）→ Treeview iid 撞车
    会抛 TclError「Item ... already exists」，整个窗口直接死。行 key 必须唯一。"""
    ts = "2026-11-06T17:01:16.072172Z"
    snapshot = ds.Snapshot({"logs": {"rows": [
        {"ts": ts, "level": "info", "event": "daemon.start"},
        {"ts": ts, "level": "info", "event": "daemon.start"},
        {"ts": ts, "level": "warning", "event": "daemon.heartbeat_forbidden"},
    ], "path": "x.log", "levels": ["all", "info"], "note": ""}})
    view = vw.build_view("logs", snapshot)
    keys = [row.key for row in view.table.rows]
    assert len(keys) == 3
    assert len(set(keys)) == 3
    assert all(keys)                       # 空 key 会让 Tk 自动编号，等于放弃可控性


def test_unique_iid_never_repeats_and_keeps_plain_key():
    from djb_node.desktop.app import unique_iid

    used: set[str] = set()
    assert unique_iid("a", used) == "a"
    used.add("a")
    assert unique_iid("a", used) == "a#2"
    used.add("a#2")
    assert unique_iid("a", used) == "a#3"
    assert unique_iid("", used) == ""      # 空 key 交给 Tk 自动编号


def test_account_bind_entry_survives_unavailable_account_domain():
    """正式环境实测（0.3.0）：未登录时账户域本身不可用，账户页只剩一行「暂不可用」，
    绑定按钮被一起藏掉 → 用户没有任何可点的下一步。绑定入口必须与「域不可用」解耦。"""
    snapshot = ds.FakeDataSource({
        "account": ds.Unavailable("云端未接通（未登录节点：本机尚未注册节点）"),
        "access": {"binding": {"state": "unbound"}},
    }, source="test").snapshot()
    view = vw.build_view("account", snapshot)
    keys = [action.key for action in view.actions]
    assert "account.bind" in keys and "account.open_web" in keys
    bind = next(action for action in view.actions if action.key == "account.bind")
    assert bind.primary is True
    text = view.plain_text()
    assert "绑定网页端" in text
    assert "不是配置损坏" in text                     # 红字旁边给出可操作解释
    assert "共享算力 → 节点与绑定 → 生成绑定码" in text            # 可照抄的步骤仍在


# ─────────────────────────── 接入配置：节点 ID 上屏 + 复制（P0-node，2026-11-12）
def _access_snapshot(*, node_id="", source="", secrets=False):
    """接入配置页快照：只覆盖 access 域，其余沿用 demo 数据。

    ``node_id`` / ``source`` 由 datasource._resolve_node_id 产出（config/credentials/store/""）。
    """
    config = ac.AccessConfig(node_secret="djb_ck_live_secret_value" if secrets else "",
                             api_key=SECRET if secrets else "").display()
    sections = dict(demo_snapshot().sections)
    sections["access"] = {"config": config, "valid": True, "saved": True, "binding": None,
                          "node_id": node_id, "node_id_source": source,
                          "note": "", "path": "~/.djb/desktop/access-config.json"}
    return ds.Snapshot(sections)


def test_access_shows_node_id_from_credentials_readonly_and_copyable():
    """凭证里有 node_id 而 access.json 没有：页面要显示真值，且只读 + 可一键复制。

    只读是必须的：动作执行前会把所有可编辑字段收成草稿（app._on_action），
    节点 ID 若可编辑，「保存并回读」就会把展示值写回 access.json。
    """
    view = vw.build_view("access", _access_snapshot(node_id="nd_from_credentials",
                                                   source="credentials"))
    field = next(item for item in view.fields if item.key == "node_id")
    assert field.value == "nd_from_credentials"
    assert field.editable is False and "只读" in field.hint
    action = next(item for item in view.actions if item.key == "access.copy_node_id")
    assert action.enabled is True
    assert "nd_from_credentials" in view.plain_text()


def test_access_without_node_id_explains_the_real_reason():
    """三级回退全空 → **不说**「先启动一次」（客户已经启动了，照着做也没用）。

    改成：值说「尚未生成 + 卡在哪一环」，提示给真因/下一步，复制动作置灰并指向原因。
    """
    view = vw.build_view("access", _access_snapshot())
    field = next(item for item in view.fields if item.key == "node_id")
    assert field.value != "先启动一次"
    assert "尚未生成" in field.value
    assert field.editable is False
    assert field.hint                       # 有下一步，不是空白
    action = next(item for item in view.actions if item.key == "access.copy_node_id")
    assert action.enabled is False
    assert "先启动" not in action.reason     # 旧误导文案必须消失
    assert "注册状态" in action.reason or "节点进程" in action.reason
    assert "先启动一次" not in view.plain_text()


def test_access_secrets_stay_masked_after_node_id_change():
    """加了节点 ID 之后，秘密字段仍然只显示掩码，明文不上屏。"""
    view = vw.build_view("access", _access_snapshot(node_id="nd_x", source="credentials",
                                                   secrets=True))
    text = view.plain_text()
    assert SECRET not in text
    assert "djb_ck_live_secret_value" not in text
    secrets = {field.key: field for field in view.fields if field.secret}
    # 节点凭证 = 节点密钥（唯一还在页面上的秘密）；api_key 已从 UI 移除，自然也不上屏
    assert "…" in secrets["node_secret"].value
    assert "api_key" not in secrets


def test_access_node_id_keeps_legacy_config_value_for_demo_snapshots():
    """没有新字段的调用方（demo / FakeDataSource）行为不变：仍显示 config 里的手填值。"""
    view = vw.build_view("access", demo_snapshot())
    field = next(item for item in view.fields if item.key == "node_id")
    assert field.value == "node-7c31"
    assert field.editable is True          # 手填值保持可编辑（与 0.3.2 一致）


def test_access_copy_node_id_action_returns_clipboard_payload(tmp_path):
    """动作只把节点 ID 交给剪贴板（effect=copy），不写配置、不发请求。"""
    snapshot = _access_snapshot(node_id="nd_copy_me", source="credentials")
    controller = make_controller(tmp_path, sections=dict(snapshot.sections))
    result = controller.action("access.copy_node_id")
    assert result.ok and result.effect == "copy" and result.payload == "nd_copy_me"

    empty = make_controller(tmp_path, sections=dict(_access_snapshot().sections))
    missing = empty.action("access.copy_node_id")
    assert missing.ok is False
    assert "接入配置" in missing.message            # 指向能看原因的地方
    assert "先启动一次" not in missing.message


# ──────────────────────────── RC15-039 #1：表单校验内联化（字段下方红字 + 聚焦首错）
def test_field_errors_collect_every_bad_field_at_once():
    """一次给全部错误（老 ``validate`` 一次只报一条 → 客户改一步、点一次、再被打回）。"""
    config = ac.AccessConfig(price_mode="跟随平台建议价", node_id="n" * 200,
                             node_secret="abc\ndef")
    errors = config.field_errors()
    assert set(errors) == {"price_mode", "node_id", "node_secret"}
    assert "价格模式" in errors["price_mode"]
    # 红字直接显示在输入框下方，不许把内部字段名（node_secret）印到界面上
    assert "node_secret" not in errors["node_secret"] and "节点凭证" in errors["node_secret"]


def test_validate_still_raises_the_first_error_in_the_old_order():
    """``validate`` 只是取第一条抛出：调用方（老的保存路径）看到的行为不变。"""
    config = ac.AccessConfig(price_mode="跟随平台建议价", usdt_address="bogus")
    with pytest.raises(ValueError) as info:
        config.validate()
    assert "价格模式" in str(info.value)          # 价格模式仍先于收款地址报
    assert ac.AccessConfig(node_id="n").field_errors() == {}


def test_with_field_errors_is_a_pure_projection():
    """投影层不产生错误：只有贴错误这一个纯函数会改视图，且空错误 = 原对象。"""
    base = vw.build_view("access", _access_snapshot(node_id="nd_1", source="config"))
    assert base.focus_field == "" and all(not item.error for item in base.fields)
    assert vw.with_field_errors(base, None) is base
    assert vw.with_field_errors(base, {}) is base
    # 键不在本页 → 什么都不贴（不许凭空造红字/造焦点）
    assert vw.with_field_errors(base, {"api_key": "x"}) is base

    marked = vw.with_field_errors(base, {"node_id": "节点标识必须是不超过 128 字符的文本"})
    assert marked.focus_field == "node_id"
    field = next(item for item in marked.fields if item.key == "node_id")
    assert field.error.startswith("节点标识")
    assert "！节点标识必须是不超过 128 字符的文本" in marked.plain_text()
    # 其余字段一个字都没变
    assert [item.value for item in marked.fields] == [item.value for item in base.fields]


def test_save_access_returns_field_errors_and_keeps_the_store(tmp_path):
    """保存失败：逐字段原因随结果回投，配置文件一个字节都不动。"""
    store = ac.MemoryAccessStore(ac.AccessConfig(node_id="nd_old", price_mode="auto"))
    controller = make_controller(tmp_path, sections=dict(
        _access_snapshot(node_id="nd_old", source="config").sections))
    controller.access = ac.AccessService(store, reloader=lambda: ac.RELOAD_OK)
    controller.set_draft("price_mode", "跟随平台建议价")
    controller.set_draft("node_id", "n" * 200)
    result = controller.action("access.save")
    assert result.ok is False
    assert set(result.fields) == {"price_mode", "node_id"}
    assert "价格模式" in result.message and "2" in result.message
    assert store.load().price_mode == "auto" and store.load().node_id == "nd_old"


def test_save_access_success_clears_field_errors(tmp_path):
    """改好再存：``fields`` 为空（渲染层据此把红字清掉，不留残留）。"""
    store = ac.MemoryAccessStore(ac.AccessConfig(node_id="nd_old", price_mode="auto"))
    controller = make_controller(tmp_path, sections=dict(
        _access_snapshot(node_id="nd_old", source="config").sections))
    controller.access = ac.AccessService(store, reloader=lambda: ac.RELOAD_OK)
    controller.set_draft("price_mode", "manual")
    result = controller.action("access.save")
    assert result.ok is True and result.fields == {}
    assert store.load().price_mode == "manual"


# ──────────────────── RC15-039 #5：日志的「原因 / 下一步」真上屏 + 级别筛选真能筛
def _logs_snapshot(rows):
    sections = dict(demo_snapshot().sections)
    sections["logs"] = {"rows": rows, "levels": list(ds.LOG_LEVELS), "total": len(rows),
                        "note": "", "path": "/tmp/djb-test/logs/desktop.jsonl"}
    return ds.Snapshot(sections)


_LOG_ROWS = [
    {"ts": "2026-11-16T10:00:01Z", "level": "info", "event": "daemon.started", "fields": {}},
    {"ts": "2026-11-16T10:00:05Z", "level": "error", "event": "upstream.probe_failed",
     "fields": {"reason": "上游返回 401", "next_step": "在接入配置里更新密钥"}},
    {"ts": "2026-11-16T10:00:09Z", "level": "warning", "event": "yield.detected",
     "fields": {"reason": "全屏游戏在前台"}},
]


def test_logs_table_shows_reason_and_next_step_verbatim():
    """错误行自带的 reason / next_step **原样**上屏；没有就写「—」，绝不编一句原因。"""
    view = vw.build_view("logs", _logs_snapshot(_LOG_ROWS))
    assert view.table.columns == ("级别", "时间", "事件", "原因", "下一步")
    error_row = view.table.rows[1]
    assert error_row.cells[3] == "上游返回 401"
    assert error_row.cells[4] == "在接入配置里更新密钥"
    # 行里没有这两个字段 → 两个「—」（不是空字符串，也不是「成功」）
    assert view.table.rows[0].cells[3:] == ("—", "—")
    assert "在接入配置里更新密钥" in view.plain_text()


def test_logs_has_a_real_level_filter_and_no_phantom_promise():
    """页脚旧文案承诺「all / debug / info / …」却没有控件（走查原话）。

    现在：筛选项是真的（每个级别一个 option）、页脚只说它真做了什么。
    """
    view = vw.build_view("logs", _logs_snapshot(_LOG_ROWS))
    item = view.filters[0]
    assert item.key == "log_level" and item.action == "logs.filter"
    values = [value for value, _text in item.options]
    assert values == ["all"] + list(ds.LOG_LEVELS)
    assert item.current_text() == "全部级别"
    assert "只改本页显示" in view.plain_text()
    assert "筛选只改本页显示；日志文件本身一个字都不动。" in view.notes


def test_controller_log_filter_really_filters_and_leaves_the_file_alone(tmp_path):
    """级别筛选是**真筛**（filter 之前只有一句页脚）：行数、卡片、复制内容一起变。"""
    controller = make_controller(tmp_path, sections=dict(_logs_snapshot(_LOG_ROWS).sections))
    controller.show("logs")
    controller.set_draft("log_level", "error")
    result = controller.action("logs.filter")
    assert result.ok and result.effect == "filter"
    assert "1 行" in result.message and "未改动" in result.message
    view = controller.current()
    assert len(view.table.rows) == 1 and "error" in view.table.rows[0].cells[0]
    assert view.filters[0].current_text() == "error（错误）"
    assert "共 3 行 · 已筛选" in view.plain_text()          # 卡片说清「1 是筛出来的」
    assert controller.drafts == {}                          # 筛选不写进配置草稿
    # 复制跟随筛选：只剩筛出来的那一行
    copied = controller.action("logs.copy")
    assert "upstream.probe_failed" in copied.payload and "daemon.started" not in copied.payload
    controller.set_draft("log_level", "all")
    controller.action("logs.filter")
    assert len(controller.current().table.rows) == 3


def test_controller_log_filter_refuses_an_unknown_level(tmp_path):
    """不认识的级别：明确拒绝，**不许**静默退回「全部」（那会让客户以为筛过了）。"""
    controller = make_controller(tmp_path, sections=dict(_logs_snapshot(_LOG_ROWS).sections))
    controller.show("logs")
    controller.set_draft("log_level", "alert")
    result = controller.action("logs.filter")
    assert result.ok is False and "alert" in result.message
    assert len(controller.current().table.rows) == 3


# ───────────────── RC15-039 #4：自动刷新（可见 10s / 最小化或空闲 60s；可关；不叠请求）
def test_auto_refresh_defaults_on_and_the_toggle_is_on_the_settings_page():
    """默认开 + 开关就在「本机设置」（不是藏在配置文件里）。"""
    controller = DesktopController(ds.FakeDataSource(dict(demo_snapshot().sections)))
    assert controller.auto_refresh is True
    view = controller.show("access")
    toggle = view.switches[0]
    assert toggle.key == "auto_refresh" and toggle.value is True
    assert toggle.action == "access.toggle_auto_refresh"
    assert "[自动刷新：开]" in view.plain_text()

    controller.set_draft("auto_refresh", "off")
    result = controller.action("access.toggle_auto_refresh")
    assert result.ok and "已关闭" in result.message
    assert controller.auto_refresh is False
    assert "[自动刷新：关]" in controller.current().plain_text()
    assert controller.drafts == {}                 # 开关值不进配置草稿


def test_auto_refresh_switch_refuses_an_unknown_state():
    controller = DesktopController(ds.FakeDataSource(dict(demo_snapshot().sections)))
    controller.set_draft("auto_refresh", "maybe")
    result = controller.action("access.toggle_auto_refresh")
    assert result.ok is False and controller.auto_refresh is True


def test_quiet_refresh_never_clobbers_the_message_but_reports_failures(tmp_path):
    """自动刷新是后台动作：成功不许顶掉客户正在读的那句；失败必须留一句。"""
    controller = make_controller(tmp_path)
    controller.message = "未保存：价格模式必须是 auto / manual"
    controller.apply_refresh(controller.snapshot, sections=("status",), quiet=True)
    assert controller.message == "未保存：价格模式必须是 auto / manual"

    controller.apply_refresh(None, sections=("status",), error=RuntimeError("boom"), quiet=True)
    assert "刷新失败" in controller.message         # 失败不静默


def test_theme_save_keeps_the_auto_refresh_flag(tmp_path):
    """切主题是读改写：不许把「自动刷新已关闭」顺手重置成默认。"""
    from djb_node.desktop import theme as dual

    dual.save_flag("auto_refresh", False, tmp_path)
    dual.save("night", tmp_path)
    payload = dual.load_prefs(tmp_path)
    assert payload == {"auto_refresh": False, "theme": "night"}
    assert dual.load_flag("auto_refresh", tmp_path, default=True) is False
    dual.save("day", tmp_path)
    assert dual.load_flag("auto_refresh", tmp_path, default=True) is False
    assert dual.load(tmp_path) == "day"


# ───────────────── RC15-039 #17：档位三档（全速/均衡/省心）、二次确认、回读 daemon
def _yield_sections(preset: str, state: str = "online") -> dict:
    sections = dict(demo_snapshot().sections)
    sections["yield"] = {"state": state, "reason": None, "reason_label": None,
                         "accepting": state != "paused", "mode": "balanced",
                         "preset": preset, "yield_enabled": True}
    return sections


def test_yield_table_has_exactly_three_tiers_and_paused_is_not_one_of_them(tmp_path):
    """owner D-1：档位只有三个；``paused`` 是状态，不许占一行。"""
    text = make_controller(tmp_path, sections=_yield_sections("均衡")).show("yield").plain_text()
    assert "三档模式" in text and "四档" not in text
    for label in ("全速", "均衡", "省心"):
        assert label in text
    assert "暂停 |" not in text                      # 表里没有「暂停」这一行
    assert "| paused" not in text
    # 暂停时：表还是三行，当前档位照旧显示（暂停不吞掉档位）
    paused = make_controller(tmp_path, sections=_yield_sections("省心", "paused")).show("yield")
    assert [row.key for row in paused.table.rows] == ["full", "balanced", "saver"]
    assert "✓ 当前" in paused.plain_text()
    assert any("已暂停" in card.value for card in paused.cards)


def test_yield_current_tier_comes_from_the_preset_and_degrades_honestly(tmp_path):
    view = make_controller(tmp_path, sections=_yield_sections("全力以赴")).show("yield")
    # RC15-039 复核 M7：说明列已删（列 = 档位 / 键 / 当前）
    assert view.table.columns == ("档位", "键", "当前")
    current = [row.cells[0] for row in view.table.rows if "当前" in row.cells[2]]
    assert current == ["全速"]                        # 「全速」在本机命令里叫「全力以赴」
    assert view.table.activate == "yield.apply_mode"  # 回车/双击能切
    unknown = make_controller(tmp_path, sections=_yield_sections("")).show("yield")
    assert not any("当前" in row.cells[2] for row in unknown.table.rows)
    assert "未知（没有回读到）" in unknown.plain_text()


class _PresetCli:
    """假 CLI：记下每次调用；``limits --preset`` 写进 ``preset``，``status`` 回报它。"""

    def __init__(self, preset: str, *, readback: str | None = None, boom: bool = False,
                 disk: str | None = None) -> None:
        self.preset = preset
        self.readback = readback                # None = 回读就是写入值
        self.boom = boom
        # `disk` = 本机配置里的**真**档位（可与快照不同：造"快照落后"用）
        self.disk = preset if disk is None else disk
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv):
        argv = tuple(argv)
        self.calls.append(argv)
        if self.boom and argv[:1] == ("limits",):
            raise RuntimeError("boom")
        if argv[:1] == ("limits",) and "--json" in argv and "--preset" not in argv:
            # 复核 M6：`djb limits --json` 只读本机 profile（离线可用），不写任何东西
            return {"profile": {"preset": self.disk}}
        if argv[:1] == ("limits",) and "--preset" in argv:
            self.preset = argv[argv.index("--preset") + 1]
            self.disk = self.preset                 # 写也写在本机配置上
            return {"profile": {"preset": self.preset}}
        if argv[:1] == ("status",):
            seen = self.preset if self.readback is None else self.readback
            return {"state": "online", "profile": {"preset": seen}}
        return {}


def test_switch_tier_needs_a_selection_a_tier_and_a_second_confirmation(tmp_path):
    controller = make_controller(tmp_path, sections=_yield_sections("均衡"))
    cli = _PresetCli("均衡")
    controller.cli = cli
    assert controller.action("yield.apply_mode").ok is False      # 没选行
    assert cli.calls == []

    controller.select_row("yield", "暂停")                          # 选中不是档位的行
    assert "不是档位" in controller.action("yield.apply_mode").message

    controller.select_row("yield", "省心")                          # 选了，但确认框被取消
    asked: list[str] = []
    result = controller.action("yield.apply_mode", confirm=lambda text: asked.append(text) or False)
    assert result.effect == "cancelled" and result.ok is False
    assert "已取消切换" in result.message
    # 取消就一个字节都不写（`limits --json` 是**只读**读原档，不算写；复核 M6）
    assert [call for call in cli.calls if "--preset" in call] == []
    assert ("limits", "--json") in cli.calls
    assert "在跑的任务不会被打断" in asked[0] and "回读" in asked[0]


def test_switch_tier_writes_the_preset_and_verifies_the_daemon_readback(tmp_path):
    controller = make_controller(tmp_path, sections=_yield_sections("均衡"))
    cli = _PresetCli("均衡")
    controller.cli = cli
    controller.select_row("yield", "省心")
    result = controller.action("yield.apply_mode", confirm=lambda _text: True)
    assert result.ok is True and result.effect == "refresh"
    assert ("limits", "--preset", "省心") in cli.calls               # 写的是命令的取值
    assert ("status",) in cli.calls                                 # 独立回读
    assert "已切到「省心」" in result.message and "回读" in result.message


def test_switch_tier_mismatch_writes_the_old_tier_back_and_says_so(tmp_path):
    """回读不一致 = 失败（owner D-1）：写回原档 + 出声，界面不许装作切成功了。"""
    controller = make_controller(tmp_path, sections=_yield_sections("均衡"))
    cli = _PresetCli("均衡", readback="全力以赴")                    # daemon 回报的却是全速
    controller.cli = cli
    controller.select_row("yield", "省心")
    result = controller.action("yield.apply_mode", confirm=lambda _text: True)
    assert result.ok is False
    assert ("limits", "--preset", "均衡") in cli.calls               # 写回原档
    assert "回读" in result.message and "「均衡」档" in result.message


def test_switch_tier_without_a_readback_is_not_success(tmp_path):
    controller = make_controller(tmp_path, sections=_yield_sections("均衡"))
    controller.cli = _PresetCli("均衡", readback="")                 # daemon 没跑：没有 profile
    controller.select_row("yield", "省心")
    result = controller.action("yield.apply_mode", confirm=lambda _text: True)
    assert result.ok is False
    assert "回读不到" in result.message and "不是" not in result.message


def test_switch_tier_reports_an_already_current_tier_and_a_write_failure(tmp_path):
    controller = make_controller(tmp_path, sections=_yield_sections("省心"))
    controller.cli = _PresetCli("省心")
    controller.select_row("yield", "省心")
    assert "已经" in controller.action("yield.apply_mode", confirm=lambda _t: True).message

    controller = make_controller(tmp_path, sections=_yield_sections("均衡"))
    controller.cli = _PresetCli("均衡", boom=True)
    controller.select_row("yield", "全速")
    result = controller.action("yield.apply_mode", confirm=lambda _t: True)
    assert result.ok is False and "档位未切换" in result.message


# ───────────────── RC15-039 #34：结算口径只剩一个常量（owner D-2 原文，逐字）
def test_settlement_wording_is_one_constant_used_verbatim(tmp_path):
    from djb_node.desktop.views import SETTLE_NOTE

    assert SETTLE_NOTE == "结算币种：USDT（按 1 USDT = 1 USD 展示）；收益不等于可提现余额。"
    text = make_controller(tmp_path).show("earnings").plain_text()
    assert text.count(SETTLE_NOTE) == 1
    # 四张卡的去向都要能对上这一句：余额卡不许暗示可提现
    for card in ("[今日]", "[待结算]", "[已结算]", "[余额（可用）]"):
        assert card in text
    assert "余额（可用）" in text and "可提现" not in text.split("[余额（可用）]")[1].splitlines()[0]


def test_no_view_promises_settlement_with_the_old_conflicting_lines(tmp_path):
    """全客户端（13 页）不许再出现互相打脸的那两句手写文案。"""
    controller = make_controller(tmp_path)
    for key in vw.VIEW_KEYS:
        text = controller.show(key).plain_text()
        assert "不可提现" not in text, key
        assert "直接结算" not in text, key


def test_rollback_uses_the_real_local_tier_not_the_stale_snapshot(tmp_path):
    """复核 M6：快照落后 + 回读失败 —— 写回必须是**本机配置里的真原档**。

    旧实现 `before = current_preset()` 读快照：真原档「省心」、快照「均衡」时，
    失败消息会说"已写回原来的「均衡」档"，落盘变成谁也没选过的值。
    """
    controller = make_controller(tmp_path, sections=_yield_sections("均衡"))   # 快照说「均衡」
    cli = _PresetCli("均衡", readback="", disk="省心")                          # 真原档是「省心」
    controller.cli = cli
    controller.select_row("yield", "均衡")                       # 客户要切到「均衡」
    result = controller.action("yield.apply_mode", confirm=lambda _t: True)

    assert result.ok is False
    # 写「均衡」（用户选的）→ 回读不到 → 写回**真原档「省心」**（不是快照里的「均衡」）
    written = [call[2] for call in cli.calls
               if call[:1] == ("limits",) and "--preset" in call]
    assert written == ["均衡", "省心"]
    assert cli.disk == "省心"                                    # 落盘还是真原档
    assert "写回原来的「省心」" in result.message


def test_rollback_refuses_to_guess_when_the_local_tier_is_unreadable(tmp_path):
    """复核 M6：读不到真原档 → **不回写**，并明说「原档未知，请手动选一次」。"""
    controller = make_controller(tmp_path, sections=_yield_sections("均衡"))
    cli = _PresetCli("均衡", readback="", disk="")
    controller.cli = cli
    controller.select_row("yield", "省心")
    asked: list[str] = []
    result = controller.action("yield.apply_mode", confirm=lambda text: asked.append(text) or True)

    assert result.ok is False
    written = [call for call in cli.calls if call[:1] == ("limits",) and "--preset" in call]
    assert written == [("limits", "--preset", "省心")]           # 只写了用户选的那一次
    assert "原档位未知" in result.message and "手动选一次" in result.message
    assert cli.disk == "省心"                                    # 只写了用户选的那一次
    # 确认框里就要先讲清楚：读不到原档 → 失败时不回写
    assert "不会" in asked[0] and "回写" in asked[0]


def test_log_empty_text_does_not_claim_the_file_is_missing_when_it_is_only_filtered(tmp_path):
    """复核 M4：筛到 0 行 ≠「日志文件尚未生成」（同屏卡片写着「共 4 行」）。

    三种「表体为空」必须分开说：筛空 / 有行但解析不出 / 真没有日志。
    """
    controller = make_controller(tmp_path)
    controller.show("logs")
    view = controller.set_log_level("debug")            # demo 数据里没有 debug 行
    text = view.plain_text()
    assert view.table.rows == ()
    assert "当前筛选（debug）下没有日志行" in text
    assert "日志文件尚未生成" not in text
    assert "共 4 行" in text                            # 卡片仍说真话：文件里有 4 行

    # 真没有日志（域可用、total=0）时才说「尚未生成」
    sections = dict(demo_snapshot().sections)
    sections["logs"] = {"rows": [], "total": 0, "levels": ["info"], "level": "all",
                        "path": "~/.djb/logs/daemon.log"}
    empty = make_controller(tmp_path, sections=sections).show("logs").plain_text()
    assert "日志文件尚未生成" in empty


def test_yield_copy_only_claims_what_this_version_actually_does(tmp_path):
    """复核 M7（owner a′）：说明列三句删掉；「已回读」只在切档成功后才出现。

    验收原话：这一页 `plain_text()` 里不许同时出现「满载/实时算力/不使用电脑时」
    与「不生效」这类互斥说法。
    """
    sections = _yield_sections("均衡")
    controller = make_controller(tmp_path, sections=sections)
    text = controller.show("yield").plain_text()
    for phrase in ("满载", "实时算力", "不使用电脑时"):
        assert phrase not in text, phrase
    assert "本版只记录档位偏好" in text and "后续版本生效" in text
    assert "不生效" not in text
    assert "已回读" not in text                    # 首屏没有任何回读动作

    # 切档成功（回读一致）之后：「已回读」才出现
    sections["yield"]["preset"] = "省心"           # 落盘后数据源读到的就是新档
    controller.cli = _PresetCli("均衡")
    controller.select_row("yield", "省心")
    result = controller.action("yield.apply_mode", confirm=lambda _t: True)
    assert result.ok is True, result.message
    assert "已回读" in controller.show("yield").plain_text()


def test_access_page_offers_reset_only_when_the_node_is_retired():
    """清退恢复入口：只在 retired 时可点；其它原因置灰并说清为什么。"""
    from djb_node.desktop import datasource as ds
    from djb_node.desktop import views as vw

    retired = vw.build_view("access", ds.Snapshot({"access": {
        "node_id": "n_x", "daemon": {"running": True, "state": "paused",
                                     "reason": "已清退", "reason_code": "retired"}}}))
    action = next(item for item in retired.actions if item.key == "access.reset_node")
    assert action.enabled is True

    healthy = vw.build_view("access", ds.Snapshot({"access": {
        "node_id": "n_x", "daemon": {"running": True, "state": "running",
                                     "reason": "未绑定", "reason_code": "unbound"}}}))
    disabled = next(item for item in healthy.actions if item.key == "access.reset_node")
    assert disabled.enabled is False and "清退" in disabled.reason

    text = retired.plain_text()
    assert "节点已清退（不可逆）" in text          # 不再让客户以为「重装就好」
    assert "重新注册" in text


#: R2.1 ⑤ 的**产品名**白名单：这些是品牌/协议名，不做翻译（owner 若要求中文化另开票）。
EYEBROW_PRODUCT_NAMES = ("Token Plan", "Agent", "GPU", "CPU")

def test_no_english_in_any_page_eyebrow():
    """13 页的眉标（页名 / 主卡眉标 / 徽标 / 右侧栏卡片眉标 / 图表眉标）不许有英文字母。

    R2.1 ⑤ 的验收口径：「侧栏 / 页头眉标中文，英文残留 0 处」。这里把**眉标层**钉住，
    正文里的技术名（OpenAI / USDT / nvidia-smi / 节点 ID）不在这一层，属于有意保留。
    """
    from djb_node.desktop import ia

    snapshot = demo_snapshot()
    offenders: dict[str, list[str]] = {}
    for key in vw.VIEW_KEYS:
        view = vw.build_view(key, snapshot)
        eyebrows = [view.title]
        if view.hero is not None:
            eyebrows += [view.hero.kicker, view.hero.chip, view.hero.title]
        for rail in view.rail:
            eyebrows += [rail.kicker, rail.title]
        if view.chart is not None:
            eyebrows += [view.chart.kicker, view.chart.title]
        hits = [text for text in eyebrows if text and ia.has_english(text)
                and not any(name in text for name in EYEBROW_PRODUCT_NAMES)]
        if hits:
            offenders[key] = hits
    assert offenders == {}, "眉标里还有英文：" + repr(offenders)


# ─────────────────── 2026-11-24 真机复查：文案与布局（F25/F26/F27/F28/F29）

def test_access_action_bar_splits_into_groups_so_no_button_is_clipped():
    """F27：五个动作挤一条栏时最右一颗被卡片裁掉（真机只看到「绑定与…」）。

    分组渲染会给每组单起一行，行内按钮数从 5 降到 ≤3，宽度不再溢出。
    """
    view = vw.build_view("access", _access_snapshot(node_id="nd_1"))
    groups = vw.action_groups(view.actions)
    assert len(groups) >= 2, "接入配置页动作必须分组，否则按钮行会溢出"
    for title, members in groups:
        assert title, "分组必须带小标题，客户才知道这一行是干什么的"
        assert len(members) <= 3, ("每组 ≤3 颗按钮（真机可用宽度），实测 " + title
                                   + " 有 " + str(len(members)) + " 颗")


def test_task_queue_notes_do_not_leak_internal_field_names():
    """F26：任务队列页脚注曾把 inflight/recorded/settled 这些内部字段名写给客户。"""
    view = vw.build_view("tasks", ds.Snapshot(dict(demo_snapshot().sections)))
    text = " ".join(view.notes) + " " + view.plain_text()
    for internal in ("inflight", "recorded", "settled", "origin", "schema"):
        assert internal not in text, "内部字段名外泄：" + internal
    assert "进行中" in " ".join(view.notes)


def test_token_plan_subtitle_tells_the_next_action():
    """F28：页面副标题改成「下一步做什么」，首屏就有操作焦点（不再是功能清单）。"""
    view = vw.build_view("token_plan", _token_plan_snapshot(upstreams=()))
    assert view.subtitle.startswith("下一步："), view.subtitle
    assert "测试联通性" in view.subtitle


def test_token_plan_price_hint_is_short_and_free_of_engineer_words():
    """F28：最低价说明曾堆了 5 个概念（云端驳回 / 牌价 / 最优价 / 客户实付 / 服务端快照）。"""
    view = vw.build_view("token_plan", _token_plan_snapshot(upstreams=()))
    field = [f for f in view.fields if f.key == "min_price"][0]
    assert len(field.hint) <= 100, field.hint         # 两行以内，真机不折成 5 行
    for word in ("驳回", "快照", "牌价"):
        assert word not in field.hint
    assert "客户实付" in field.hint and "不成交" in field.hint


def test_missing_cloud_endpoint_message_has_no_method_name():
    """F29：缺接口时曾把 model_catalog/binding 这样的方法名打到客户屏幕上。"""
    from djb_node.desktop import datasource as dsrc

    class NoMethods:
        pass

    src = object.__new__(dsrc.LocalDataSource)
    src._offline_reason = ""
    src.notes = []
    src._cloud = object()                     # 有一个「云对象」，但它没有 model_catalog 方法
    try:
        dsrc.LocalDataSource._cloud_call(src, "model_catalog")
    except dsrc.ProviderError as exc:
        assert "model_catalog" not in exc.reason
        assert "还没开通" in exc.reason
    else:                                              # pragma: no cover - 防御
        raise AssertionError("缺接口必须抛 ProviderError")


def test_cli_credential_line_uses_customer_wording():
    """F25：CLI `djb status` 把后端代号 file-0600 直接打给客户。"""
    from djb_node import cli

    assert cli._storage_label("file-0600") == "仅本机可读（0600）"
    assert cli._storage_label("") == "—"
    assert cli._storage_label("future-backend") == "future-backend"


def test_token_plan_action_bar_is_grouped_by_step():
    """F30：七颗按钮一行，1360px 宽时右侧被裁；分组后每组 ≤4 颗。"""
    view = vw.build_view("token_plan", _token_plan_snapshot(upstreams=()))
    groups = vw.action_groups(view.actions)
    assert len(groups) >= 2
    assert [t for t, _ in groups] == ["第 1 步 · 接入", "第 2 步 · 测试与保存",
                                     "第 3 步 · 定价与上报"]
    for title, members in groups:
        assert len(members) <= 4, title + " 一行有 " + str(len(members)) + " 颗"


def test_unavailable_reason_has_no_nested_parentheses_or_cli_terms():
    """F31：真机显示「暂不可用（这项数据平台还没开通（不是你的配置问题））」双层括号 +
    「本地已应用 暂不可用 · model-sync 状态」里的 CLI 子命令名。"""
    from djb_node.desktop import datasource as dsrc

    src = object.__new__(dsrc.LocalDataSource)
    src._offline_reason = ""
    src.notes = []
    src._cloud = object()
    try:
        dsrc.LocalDataSource._cloud_call(src, "model_catalog")
    except dsrc.ProviderError as exc:
        assert "（" not in exc.reason and "(" not in exc.reason, exc.reason
    view = vw.build_view("models", ds.Snapshot(dict(demo_snapshot().sections)))
    text = view.plain_text()
    assert "model-sync" not in text


def test_overview_status_card_never_contradicts_its_own_title():
    """F37：标题「正在接单」+ 副标题「接单状态未知」= 同一张卡自相矛盾（真机 0.5.17）。"""
    snapshot = ds.Snapshot(dict(demo_snapshot().sections))
    view = vw.build_view("overview", snapshot)
    text = view.plain_text()
    if "正在接单" in text:
        assert "接单状态未知" not in text, "标题说在接单，副标题就必须给出接单口径"


def test_start_daemon_is_single_flight(tmp_path):
    """F36：一次启动拉起两个 daemon。第二个启动请求必须被闸门挡下。"""
    from djb_node import bootstrap

    paths = bootstrap.Paths(str(tmp_path))
    os.makedirs(paths.runtime_dir, exist_ok=True)
    first = bootstrap._spawn_lock(paths)
    assert first, "第一个启动者必须拿到锁"
    assert bootstrap._spawn_lock(paths) is None, "第二个启动者必须被挡下（不许再拉一个 daemon）"
    bootstrap._spawn_unlock(first)
    again = bootstrap._spawn_lock(paths)
    assert again, "上一个启动者退出后，锁必须能再次拿到"
    bootstrap._spawn_unlock(again)


def test_spawn_lock_steals_stale_lock(tmp_path):
    """进程崩了没删锁：不许把节点永久卡住，超时后要抢过来。"""
    from djb_node import bootstrap

    paths = bootstrap.Paths(str(tmp_path))
    os.makedirs(paths.runtime_dir, exist_ok=True)
    lock = os.path.join(paths.runtime_dir, "spawn.lock")
    with open(lock, "w", encoding="utf-8") as handle:
        handle.write("0\n")
    old = time.time() - bootstrap.SPAWN_LOCK_STALE_S - 5
    os.utime(lock, (old, old))
    got = bootstrap._spawn_lock(paths)
    assert got, "残锁必须被抢过来（fail-open）"
    bootstrap._spawn_unlock(got)


def test_last_seen_is_shown_in_local_time():
    """F46：真机把 UTC 原串（含 6 位微秒）直接印在「最后在线」上，客户读成本地时间。"""
    assert vw._local_time("2026-11-23T20:38:09.651933Z").endswith(":38")
    assert "." not in vw._local_time("2026-11-23T20:38:09.651933Z")
    assert vw._local_time("") == "—"
    assert vw._local_time("不是时间") == "不是时间"


def test_receipts_subtitle_has_no_internal_jargon():
    """F40：副标题原来写「Merkle 路径」，客户看不懂。"""
    snapshot = ds.Snapshot(dict(demo_snapshot().sections))
    view = vw.build_view("receipts", snapshot)
    assert "Merkle" not in view.subtitle


def test_empty_receipts_page_says_it_once():
    """F39：空收据页把「还没有收据」印了两遍（空态 + note 各一次）。"""
    snapshot = ds.Snapshot({"receipts": {"rows": [], "origin": "cloud", "note": "还没有收据"}})
    text = vw.build_view("receipts", snapshot).plain_text()
    assert text.count("还没有收据") == 1


def test_overview_recent_receipts_says_empty_once():
    """F39 同款（概览「最近收据」）：空表 + note 都在说「还没有收据」。"""
    snapshot = ds.Snapshot({"receipts": {"rows": [], "origin": "cloud", "note": "还没有收据"}})
    text = vw.build_view("overview", snapshot).plain_text()
    assert text.count("还没有收据") == 1


def test_overview_surfaces_cloud_local_disagreement():
    """F50：云端 accepting=False 而本机 online 时，卡片不许只挑一边说。"""
    section = {"status": {"state": "online"},
               "yield": {"mode": "auto", "accepting": False}}
    text = vw.build_view("overview", ds.Snapshot(section)).plain_text()
    if "正在接单" in text and "云端标记" in text:
        assert "对不上" in text


def test_overview_does_not_claim_paused_when_accepting_unknown():
    """F50 更正：accepting 读不到（None）时不许说「不接新任务」，state=online 更不许自相矛盾。"""
    section = {"status": {"state": "online"}, "yield": {"mode": "auto", "accepting": None}}
    text = vw.build_view("overview", ds.Snapshot(section)).plain_text()
    assert "正在接单" in text
    assert "不接新任务" not in text
    assert "未确认" in text or "待确认" in text


def test_overview_shows_heartbeat_time_in_local_clock():
    """F50b：心跳时间戳是 UTC，卡片上必须印本地时间（否则 11:59 显示成 03:59）。"""
    from datetime import datetime

    stamp = "2026-11-24T03:59:12.123456Z"
    section = {"status": {"state": "online"},
               "yield": {"mode": "auto", "accepting": True, "accepting_source": "heartbeat",
                         "heartbeat_at": stamp}}
    text = vw.build_view("overview", ds.Snapshot(section)).plain_text()
    local = datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone()
    assert local.strftime("%H:%M") in text
    assert "03:59" not in text or local.strftime("%H:%M") == "03:59"


def test_account_page_has_one_2fa_entry_and_no_duplicate_last_check():
    """F47/F48（2026-11-24 真机）：2FA 只留一颗能点的；「上次检查/上次结果」不许重复两行。"""
    account = {"profile": {"email": "a@b.c"},
               "security": {"node_id": "n_1"},
               "update": {"checked_at": "", "verify_state": "", "package_version": "0.5.28"}}
    section = {"account": account, "status": {"state": "online"}, "access": {}, "yield": {}}
    view = vw.build_view("account", ds.Snapshot(section))
    labels = [a.label for a in view.actions]
    assert not any("启用双因素验证" == label for label in labels), labels
    assert sum("2FA" in label or "双因素" in label for label in labels) == 1, labels
    text = view.plain_text()
    assert text.count("上次检查") == 1
    assert "上次结果" not in text


def test_binding_card_tells_where_to_bind():
    """F49：未绑定时，卡片里要写清「去哪儿点」，不能只报状态。"""
    account = {"profile": {"email": "a@b.c"}, "security": {"node_id": "n_1"}}
    section = {"account": account, "status": {"state": "online"},
               "access": {"binding": {"bound": False}}, "yield": {}}
    text = vw.build_view("account", ds.Snapshot(section)).plain_text()
    assert "② 绑定节点" in text
