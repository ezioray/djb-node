# SPDX-License-Identifier: Apache-2.0
"""12 视图投影 + 窗口逻辑（无 Tk）：文案红线、缺失数据口径、动作路由、保存回读。

视图层是纯函数（Snapshot → View），所以这里能用文本快照断言 UI 说了什么——
「无留痕等于没做」在界面上就是：**说出来的每句话都可断言**。
"""
from __future__ import annotations

import pytest

from djb_node.desktop import access as ac
from djb_node.desktop import datasource as ds
from djb_node.desktop import views as vw
from djb_node.desktop.app import demo_snapshot
from djb_node.desktop.controller import ControlOutcome, DesktopController

SECRET = "djb_sk_live_9f2c41aa77be"
#: 侧栏顺序 = IA 顺序（4 组：运行 → 收益 → 接入 → 系统；见 views.NAV_SECTIONS）
SPEC_ORDER = ["overview", "bench", "gpu", "models", "tasks", "receipts", "earnings",
              "token_plan", "agent", "access", "yield", "account", "logs"]
SPEC_TITLES = ["总览", "性能基准", "GPU 监控", "可提供模型", "任务队列", "收据", "收益",
               "共享 Token Plan", "Agent 接入", "接入配置", "让路/节能", "账户与安全",
               "日志"]
#: 分组口径（00-PLAN §1.1 已裁决）：组 key / 中文名 / 拉丁微标 / 成员顺序
SPEC_SECTIONS = [
    ("run", "运行", "RUN", ["overview", "bench", "gpu", "models", "tasks"]),
    ("earn", "收益", "EARN", ["receipts", "earnings", "token_plan"]),
    ("connect", "接入", "CONNECT", ["agent", "access"]),
    ("system", "系统", "SYSTEM", ["yield", "account", "logs"]),
]
#: 合规红线：不许出现「许愿式」金融承诺；「不可提现」这类否定式免责声明是允许的
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


def test_every_page_kicker_is_two_level_ascii():
    """页头眉标 =「组名 / 页名」两级；整串 ASCII，交给 mono 不会跳基线。"""
    for key in vw.VIEW_KEYS:
        kicker = vw.page_kicker(key)
        assert kicker.isascii(), kicker
        parts = kicker.split(" / ")
        assert len(parts) == 2, kicker
        assert parts[0] == vw.SECTION_FOR_VIEW[key].micro
        assert parts[1] == vw.PAGE_MICROS[key]
    assert vw.page_kicker("overview") == "RUN / OVERVIEW"
    assert vw.page_kicker("token_plan") == "EARN / TOKEN PLAN"
    assert vw.page_kicker("logs") == "SYSTEM / LOGS"


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
        # 「提现」只允许以否定形式出现（额度不可提现）
        for index in range(len(text)):
            if text.startswith("提现", index):
                assert text[max(0, index - 2):index] == "不可", (key, text[index - 12:index + 6])


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
    view = vw.build_view("models", demo_snapshot())
    greyed = [row for row in view.table.rows if row.disabled]
    assert greyed, "演示数据里应有不兼容模型"
    assert all(row.cells[-1] for row in greyed), "标灰必须给原因"


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


def test_access_fields_include_every_required_config():
    view = vw.build_view("access", demo_snapshot())
    keys = {field.key for field in view.fields}
    for needed in ("node_id", "node_secret", "api_key", "price_mode", "usdt_address",
                   "openai_base_url", "anthropic_base_url"):
        assert needed in keys, needed
    secrets = [f for f in view.fields if f.secret]
    assert {f.key for f in secrets} >= {"node_secret", "api_key"}
    assert all("…" in f.value or f.value in ("—", "未设置") for f in secrets)


def test_unwired_views_say_unavailable_not_fake_success():
    for key, action in (("models", "models.save"), ("receipts", "receipts.verify"),
                        ("earnings", "earnings.export"), ("account", "account.logout")):
        view = vw.build_view(key, demo_snapshot())
        assert action in {a.key for a in view.actions}, key


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
    assert ("token_plan", "共享 Token Plan") in vw.VIEWS
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
    assert view.key == "token_plan" and view.title == "共享 Token Plan"
    text = view.plain_text()
    assert text.startswith("# 共享 Token Plan")
    assert controller.view == "token_plan"


def test_token_plan_lists_upstreams_with_state_models_and_qps():
    view = vw.build_view("token_plan", demo_snapshot())
    text = view.plain_text()
    assert view.table is not None
    assert [row.cells[0] for row in view.table.rows] == ["up_moonshot_01", "up_local_agent"]
    assert "✓ 已验证" in text and "◐ 已申报（待验证）" in text
    assert "3 个（已验证 2）" in text                          # 申报数 ≠ 已验证数
    assert "最大 QPS" in text


def test_token_plan_empty_state_tells_customer_what_to_do():
    snapshot = _token_plan_snapshot(upstreams=[])
    view = vw.build_view("token_plan", snapshot)
    text = view.plain_text()
    assert "你还没有接入任何 Token Plan" in text
    assert "去接入配置" in text                                # 空态必须给下一步，不是只说暂无
    assert view.table is not None and not view.table.rows


def test_token_plan_unbound_state_points_at_binding():
    snapshot = _token_plan_snapshot(upstreams=[], bound=False)
    text = vw.build_view("token_plan", snapshot).plain_text()
    assert "✕ 未绑定" in text
    assert "还没绑定" in text and "接入配置" in text


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
    assert view.table is not None and len(view.table.rows) == 1
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
    for key in ("models.save", "account.enable_2fa", "nope.unknown"):
        result = controller.action(key)
        assert result.ok is False, key
        assert "暂不可用" in result.message, key


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
    from djb_node.desktop.datasource import FakeDataSource

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
    assert "cn-east" in text and "linux" in text and "2026-11-01T10:00:00Z" in text
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
    assert "绑定：在网页端 → 账户/节点 → 生成绑定码" in text
    assert "djb auth --bind <码>" in text
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
    assert "账户/节点 → 生成绑定码" in text            # 可照抄的步骤仍在


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
    assert "…" in secrets["api_key"].value and "…" in secrets["node_secret"].value


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
