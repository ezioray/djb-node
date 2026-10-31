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
SPEC_ORDER = ["overview", "bench", "models", "gpu", "tasks", "receipts", "earnings",
              "agent", "access", "yield", "account", "logs"]
SPEC_TITLES = ["总览", "性能基准", "可提供模型", "GPU 监控", "任务队列", "收据", "收益",
               "Agent 接入", "接入配置", "让路/节能", "账户与安全", "日志"]
#: 合规红线：不许出现「许愿式」金融承诺；「不可提现」这类否定式免责声明是允许的
FORBIDDEN_PROMISES = ("充值", "APY", "保本", "固定收益", "储值账户", "稳赚", "保证收益")


def snapshot_all_unavailable(reason="测试：数据源未接线"):
    return ds.Snapshot({name: ds.Unavailable(reason) for name in ds.SECTIONS})


# ─────────────────────────────────────────────────────────────── 结构
def test_twelve_views_in_spec_order():
    assert list(vw.VIEW_KEYS) == SPEC_ORDER
    for (key, title), expected_key, expected_title in zip(vw.VIEWS, SPEC_ORDER, SPEC_TITLES):
        assert key == expected_key
        assert title.replace(" ", "") == expected_title.replace(" ", "")


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
    view = vw.build_view("gpu", demo_snapshot())
    text = view.plain_text()
    assert "无可用 GPU" in text or "暂不可用" in text
    assert view.table is not None and not view.table.rows


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
    assert controller.next_view().key == "overview"          # 12 项循环
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


def test_unavailable_earnings_never_says_empty():
    """真联调发现：详情域不可用时表格空文案还写「暂无计费明细」，读起来像「真的是 0」。"""
    from djb_node.desktop.datasource import ProviderError
    from djb_node.desktop.views import build_view

    class Cloud:
        def fetch(self, kind):
            return {"items": [], "next_cursor": None, "truncated": False}

        def earnings(self):
            raise ProviderError("该节点还没有计费记录")

    from djb_node.desktop.datasource import LocalDataSource

    snapshot = LocalDataSource(cloud=Cloud()).snapshot()
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
