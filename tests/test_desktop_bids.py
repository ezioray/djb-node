# SPDX-License-Identifier: Apache-2.0
"""RC14 I/J：一 Key 多模型 + 逐模型底价（本地账本 / 预览 / 上报闸门 / 协议下拉）。

客户的原始投诉是三条，这里一条一条钉住：

1. **一个 Key 只能填一个上游**：协议二选一 + 单一请求链接（``base_url``），切换协议不改链接；
2. **同 Key 下 20 个模型只能给一个价**：按模型存底价，最小 ``0.01``（0 会被云端
   ``SKIP_ZERO_PRICE`` 静默丢掉）；
3. **不知道填的价有没有生效**：表里给「本机底价 / 生效来源」，云端没开通就说「待云端开通」。
"""
from __future__ import annotations

import pytest

from djb_node import bids as bids_mod
from djb_node import facts
from djb_node.desktop import controller as ctrl_mod
from djb_node.desktop import datasource as ds
from djb_node.desktop import views as vw


# ─────────────────────────────────────────────── 底价校验（钱不能用 float 猜）
@pytest.mark.parametrize("text,expected", [
    ("0.35", "0.35"), ("0.01", "0.01"), ("1", "1"), ("12.5", "12.5"),
    ("0.350", None),                                    # 三位小数：拒（不四舍五入）
])
def test_price_parsing_accepts_only_what_the_cloud_accepts(text, expected):
    if expected is None:
        with pytest.raises(ValueError):
            bids_mod.parse_price(text)
        return
    assert bids_mod.format_price(bids_mod.parse_price(text)) == expected


@pytest.mark.parametrize("text,fragment", [
    ("0", "0 等于白送"),
    ("0.00", "0 等于白送"),
    ("0.001", "最多两位小数"),
    ("abc", "只能是数字"),
    ("12345678", "整数最多 7 位"),
])
def test_bad_prices_are_refused_with_a_reason(text, fragment):
    with pytest.raises(ValueError) as info:
        bids_mod.parse_price(text)
    assert fragment in str(info.value)


def test_price_rejects_narrower_than_a_cent():
    """0.001 < 0.01：不能静默取整成 0（那就等于没设价）。"""
    with pytest.raises(ValueError):
        bids_mod.parse_price("0.001")


# ─────────────────────────────────────────────── 本地账本（kv，不含凭据）
class _Store:
    def __init__(self):
        self.kv = {}

    def get_kv(self, key, default=None):
        return self.kv.get(key, default)

    def set_kv(self, key, value):
        self.kv[key] = value


def test_bid_book_round_trips_and_keeps_strings():
    store = _Store()
    book = bids_mod.BidBook(store)
    assert book.set_price("gpt-4o-mini", "0.35") == "0.35"
    assert book.set_price("gpt-4o", "1") == "1"
    assert book.load() == {"gpt-4o-mini": {"min_price": "0.35"},
                           "gpt-4o": {"min_price": "1"}}
    # 存的是字符串：钱不经过 float
    assert isinstance(store.kv[bids_mod.BIDS_KV_KEY]["bids"]["gpt-4o"]["min_price"], str)


def test_empty_price_removes_the_row_instead_of_writing_zero():
    book = bids_mod.BidBook(_Store())
    book.set_price("m1", "0.2")
    assert book.set_price("m1", "") == ""
    assert book.load() == {}


def test_bid_book_ignores_corrupt_entries_instead_of_inventing_prices():
    store = _Store()
    store.set_kv(bids_mod.BIDS_KV_KEY,
                 {"version": 1, "bids": {"ok": {"min_price": "0.5"},
                                         "bad": {"min_price": "0"},
                                         "worse": "not-a-mapping"}})
    assert bids_mod.BidBook(store).load() == {"ok": {"min_price": "0.5"}}


def test_bid_book_without_store_refuses_to_pretend_it_saved():
    with pytest.raises(RuntimeError):
        bids_mod.BidBook(None).set_price("m1", "0.5")


# ─────────────────────────────────────────────── 预览与上报闸门
def test_preview_payload_matches_the_contract_shape():
    book = bids_mod.BidBook(_Store())
    book.set_price("gpt-4o-mini", "0.35")
    payload = book.report_payload(upstream_id="up_1")
    assert payload["models"] == [{"model": "gpt-4o-mini", "min_price": "0.35"}]
    assert payload["unit"] == "usd_per_mtok"
    assert payload["paths"] == list(facts.UPSTREAM_BID_REPORT_PATHS)
    assert "reason" not in payload


def test_empty_book_preview_says_why_it_is_empty():
    payload = bids_mod.BidBook(_Store()).report_payload()
    assert payload["models"] == [] and payload["reason"] == "no_bid"


def test_report_flag_is_registered_with_three_fields_and_off_by_default():
    spec = facts.FEATURE_FLAGS[facts.UPSTREAM_BID_FLAG]
    assert spec["default"] is False
    for field in facts.FLAG_REQUIRED_FIELDS:
        assert spec.get(field) not in (None, ""), field
    assert bids_mod.report_blocker({}) != ""              # 默认关：给原因
    assert "待云端" not in bids_mod.report_blocker({})


# ─────────────────────────────────────────────── 视图（三步同页）
def _plan_snapshot(**over) -> ds.Snapshot:
    sections = dict(ds.demo_snapshot().sections) if hasattr(ds, "demo_snapshot") else {}
    plan = dict(sections.get("token_plan") or {})
    plan.update({
        "upstreams": [{"id": "up_1", "access_type": "B", "state": "verified",
                       "enabled": True, "model_count": 2, "verified_models": 1,
                       "max_qps": 1.0, "last_probe_at": "2026-11-14T10:00:00Z"}],
        "models": [
            {"model": "gpt-4o-mini", "upstream_id": "up_1", "canonical": "gpt-4o-mini",
             "enabled": True, "announceable": True},
            {"model": "internal-x", "upstream_id": "up_1", "canonical": None,
             "enabled": True, "announceable": False,
             "probe_error_class": "model_not_listed"},
        ],
        "bids": {"rows": {"gpt-4o-mini": {"min_price": "0.35"}},
                 "cloud": {"state": "pending", "text": "待云端开通"},
                 "report_blocker": "上报开关关闭（默认关闭…）",
                 "preview": {"models": [{"model": "gpt-4o-mini", "min_price": "0.35"}]}},
        "protocol": facts.DEFAULT_UPSTREAM_PROTOCOL,
        "base_url": "", "secret_seen": "未保存", "discover_note": "",
        "limits": {}, "usage": [], "note": "",
    })
    plan.update(over)
    sections["token_plan"] = plan
    return ds.Snapshot(sections)


def test_token_plan_page_has_the_three_steps():
    view = vw.build_view("token_plan", _plan_snapshot())
    keys = {field.key for field in view.fields}
    assert {"protocol", "base_url", "upstream_secret", "min_price", "bid_cloud"} <= keys
    protocol = next(field for field in view.fields if field.key == "protocol")
    assert protocol.choices == facts.UPSTREAM_PROTOCOLS
    secret = next(field for field in view.fields if field.key == "upstream_secret")
    assert secret.secret and secret.can_reveal()          # G7 的显示按钮一起用上
    assert not secret.prefill                             # 密钥框永不预填
    actions = {action.key for action in view.actions}
    assert {"token_plan.discover", "token_plan.save_price",
            "token_plan.preview_report", "token_plan.report"} <= actions


def test_model_table_shows_local_price_and_source():
    text = vw.build_view("token_plan", _plan_snapshot()).plain_text()
    assert "gpt-4o-mini" in text and "0.35 元/百万" in text
    assert "本机底价" in text and "未映射" in text            # 未映射模型如实标出
    assert "待云端开通" in text                               # 云端没开通就说清楚


def test_report_action_is_disabled_with_a_reason_while_the_cloud_is_closed():
    view = vw.build_view("token_plan", _plan_snapshot())
    report = next(action for action in view.actions if action.key == "token_plan.report")
    assert report.enabled is False and "上报开关关闭" in report.reason


def test_save_and_preview_stay_clickable_so_a_click_is_never_silent():
    """CR-20261119-CLIENT-TOKENPLAN-NOOP F4：置灰按钮点了不播报 = 「点了没反应」。

    前提不满足时由**动作**回一句人话（下面两条单测验话术），界面不再靠置灰静音。
    """
    empty = _plan_snapshot(bids={"rows": {}, "cloud": {"state": "pending"},
                                 "report_blocker": "关", "preview": {"models": []}})
    view = vw.build_view("token_plan", empty)
    for key in ("token_plan.save_price", "token_plan.preview_report"):
        action = next(item for item in view.actions if item.key == key)
        assert action.enabled is True
    assert "保存接入" in next(item for item in view.actions
                              if item.key == "token_plan.discover").label


# ─────────────────────────────────────────────── 动作（本地保存 / 预览 / 上报）
class _Source:
    """最小数据源：只实现 token_plan 域与底价需要的两个口子。"""

    def __init__(self, store, *, discover=None):
        self._store = store
        self._discover = discover
        self.calls = 0

    def snapshot(self, sections=None):
        return _plan_snapshot()

    def bids_store(self):
        return self._store

    def discover_upstream_models(self, base_url, protocol, secret):
        self.calls += 1
        if self._discover is not None:
            return self._discover(base_url, protocol, secret)
        return {"base_url": base_url, "protocol": protocol, "models": [], "rejected": [],
                "error_class": None, "http_status": 200, "latency_ms": 12,
                "mapped": 0, "total": 0, "note": "上游返回 0 个模型"}


def _controller(source) -> ctrl_mod.DesktopController:
    return ctrl_mod.DesktopController(source)


def test_save_price_writes_local_book_and_never_reports():
    store = _Store()
    controller = _controller(_Source(store))
    controller.show("token_plan")
    controller.selected["token_plan"] = "gpt-4o-mini"
    controller.set_draft("min_price", "0.42")
    result = controller.action("token_plan.save_price")
    assert result.ok and "0.42" in result.message and "未上报" in result.message
    assert bids_mod.BidBook(store).load()["gpt-4o-mini"]["min_price"] == "0.42"


def test_save_price_requires_a_selected_row():
    controller = _controller(_Source(_Store()))
    result = controller.action("token_plan.save_price")
    assert not result.ok and "选一行" in result.message


def test_zero_price_is_refused_before_it_reaches_the_cloud():
    store = _Store()
    controller = _controller(_Source(store))
    controller.selected["token_plan"] = "gpt-4o-mini"
    controller.set_draft("min_price", "0")
    result = controller.action("token_plan.save_price")
    assert not result.ok and "0.01" in result.message
    assert bids_mod.BidBook(store).load() == {}


def test_report_with_flag_off_says_nothing_was_sent():
    controller = _controller(_Source(_Store()))
    controller.selected["token_plan"] = "gpt-4o-mini"
    controller.set_draft("min_price", "0.5")
    controller.action("token_plan.save_price")
    result = controller.action("token_plan.report")
    assert not result.ok and "未发送" in result.message


def test_preview_lists_what_would_be_reported():
    controller = _controller(_Source(_Store()))
    controller.selected["token_plan"] = "gpt-4o-mini"
    controller.set_draft("min_price", "0.5")
    controller.action("token_plan.save_price")
    result = controller.action("token_plan.preview_report")
    assert result.ok and "gpt-4o-mini=0.5" in result.message
    assert result.payload["payload"]["models"] == [{"model": "gpt-4o-mini",
                                                    "min_price": "0.5"}]


def test_discover_runs_through_the_source_and_reports_counts():
    source = _Source(_Store(), discover=lambda base, proto, secret: {
        "base_url": base, "protocol": proto, "error_class": None, "http_status": 200,
        "latency_ms": 20, "mapped": 1,
        "models": [{"model": "gpt-4o-mini", "canonical": "gpt-4o-mini", "state": "mapped"}],
        "rejected": [], "total": 1, "note": "读到模型清单了"})
    controller = _controller(source)
    controller.set_draft("base_url", "https://api.example.com/v1")
    controller.set_draft("protocol", "openai-chat")
    result = controller.action("token_plan.discover")
    assert result.ok and "1 个模型" in result.message and "目录 1 个" in result.message
    assert source.calls == 1
    assert controller.last_discover["total"] == 1


class _Cli:
    """假本机命令通道：记录 argv / stdin（**密钥绝不出现在 argv 里**由下面的断言验）。"""

    def __init__(self, *, upstreams=(), add_payload=None, add_error=None):
        self._upstreams = [dict(item) for item in upstreams]
        self._add_payload = add_payload
        self._add_error = add_error
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(self, argv, stdin_text=None):
        words = list(argv)
        self.calls.append((words, stdin_text))
        if words[:2] == ["upstream", "list"]:
            return {"upstreams": [dict(item) for item in self._upstreams], "count": len(self._upstreams)}
        if words[:2] == ["upstream", "add"]:
            if self._add_error is not None:
                raise ctrl_mod.ProviderError(self._add_error)
            payload = dict(self._add_payload or {})
            record = {"id": "up_new01", "access_type": "A1",
                      "base_url": words[words.index("--base-url") + 1]}
            payload.setdefault("upstream", record)
            payload.setdefault("declare", {"declared": False, "reason": "daemon 未运行"})
            self._upstreams.append(record)
            return payload
        raise AssertionError("意外的本机命令：" + " ".join(words))


def _discovered_model_list():
    return lambda base, proto, secret: {
        "base_url": base, "protocol": proto, "error_class": None, "http_status": 200,
        "latency_ms": 20, "mapped": 2,
        "models": [{"model": "gpt-4o-mini", "canonical": "gpt-4o-mini", "state": "mapped"},
                   {"model": "deepseek-chat", "canonical": None, "state": "unmapped"}],
        "rejected": [], "total": 2, "note": ""}


def test_discover_saves_the_upstream_and_keeps_the_secret_out_of_argv():
    """F1：读完清单要真的落成一条 A1 上游；密钥只走 stdin。"""
    cli = _Cli()
    controller = ctrl_mod.DesktopController(_Source(_Store(), discover=_discovered_model_list()),
                                            cli=cli)
    controller.set_draft("base_url", "https://api.example.com/v1")
    controller.set_draft("protocol", "openai-chat")
    controller.set_draft("upstream_secret", "sk-unit-test-123456")
    result = controller.action("token_plan.discover")

    assert result.ok and "2 个模型" in result.message
    assert "已添加上游 up_new01" in result.message
    add = [call for call in cli.calls if call[0][:2] == ["upstream", "add"]]
    assert len(add) == 1
    words, stdin_text = add[0]
    assert "sk-unit-test-123456" not in " ".join(words)        # 密钥不进 argv
    assert "--secret-stdin" in words
    assert stdin_text.strip() == "sk-unit-test-123456"         # 只经 stdin
    assert words[words.index("--models") + 1] == "gpt-4o-mini,deepseek-chat"
    assert words[words.index("--protocol") + 1] == "openai-chat"
    assert result.payload["forget"] == ["upstream_secret"]     # 存好之后密钥框回空
    assert controller.discover_message in result.message       # F3：结论常驻页面


def test_discover_does_not_add_the_same_endpoint_twice():
    cli = _Cli(upstreams=[{"id": "up_old", "base_url": "https://api.example.com/v1/"}])
    controller = ctrl_mod.DesktopController(_Source(_Store(), discover=_discovered_model_list()),
                                            cli=cli)
    controller.set_draft("base_url", "https://api.example.com/v1")
    result = controller.action("token_plan.discover")
    assert "已经接入过" in result.message and "up_old" in result.message
    assert not [call for call in cli.calls if call[0][:2] == ["upstream", "add"]]


def test_discover_without_a_cli_channel_says_the_list_was_not_saved():
    controller = ctrl_mod.DesktopController(_Source(_Store(), discover=_discovered_model_list()))
    controller.set_draft("base_url", "https://api.example.com/v1")
    result = controller.action("token_plan.discover")
    assert result.ok and "没有被保存" in result.message


def test_discover_reports_a_failed_save_without_claiming_success():
    cli = _Cli(add_error="B 类接入必须显式 --kind")
    controller = ctrl_mod.DesktopController(_Source(_Store(), discover=_discovered_model_list()),
                                            cli=cli)
    controller.set_draft("base_url", "https://api.example.com/v1")
    result = controller.action("token_plan.discover")
    assert "保存接入失败" in result.message and "没有创建上游" in result.message


def test_discover_without_a_link_asks_for_it_first():
    source = _Source(_Store())
    controller = _controller(source)
    result = controller.action("token_plan.discover")
    # RC15-039 #37：界面上统一叫「请求地址」（以前一处叫「请求链接」，客户以为是两个字段）
    assert not result.ok and "请求地址" in result.message
    assert source.calls == 0                              # 不发半个请求


def test_discover_failure_surfaces_the_reason_not_a_fake_success():
    source = _Source(_Store(), discover=lambda base, proto, secret: {
        "base_url": base, "protocol": proto, "error_class": "unauthorized",
        "http_status": 401, "latency_ms": 9, "models": [], "rejected": [],
        "mapped": 0, "total": 0, "note": "密钥无效，或这个 Key 没有读模型列表的权限"})
    controller = _controller(source)
    controller.set_draft("base_url", "https://api.example.com/v1")
    result = controller.action("token_plan.discover")
    assert not result.ok and "密钥无效" in result.message


def test_selected_model_and_price_are_injected_into_the_view():
    store = _Store()
    bids_mod.BidBook(store).set_price("gpt-4o-mini", "0.35")
    controller = _controller(_Source(store))
    controller.selected["token_plan"] = "gpt-4o-mini"
    view = controller.show("token_plan")
    hint = next(field for field in view.fields if field.key == "min_price").hint
    assert "gpt-4o-mini" in hint and "0.35" in hint


# ─────────────────────────────────────────────── 真窗口：协议是只读下拉
@pytest.mark.parametrize("has_display", [True])
def test_protocol_renders_as_a_readonly_combobox(has_display, monkeypatch):
    tk = pytest.importorskip("tkinter")
    from djb_node.desktop.app import DesktopApp

    try:
        root = tk.Tk()
    except Exception:                                  # pragma: no cover - 无显示
        pytest.skip("没有可用的 Tk 显示")
    store = _Store()
    app = DesktopApp(_controller(_Source(store)), root=root)
    try:
        app.show_view("token_plan")
        root.update()
        widget = app._entry_for("protocol")
        assert widget is not None
        assert widget.winfo_class() == "TCombobox"
        assert str(widget.cget("state")) == "readonly"
        assert list(widget.cget("values")) == list(facts.UPSTREAM_PROTOCOLS)
        # 密钥框：掩码 + 有「显示」按钮（G7）
        secret = app._entry_for("upstream_secret")
        assert str(secret.cget("show")) == "•"
        assert "upstream_secret" in app._reveal_buttons
    finally:
        app.stop()                                   # stop() 已经销毁窗口
        try:
            root.destroy()
        except Exception:                             # pragma: no cover - 已销毁
            pass
