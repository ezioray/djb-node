# SPDX-License-Identifier: Apache-2.0
"""RC14 C：设备码自动绑定（客户端不再手抄绑定码）。

lead 给的口径逐条钉住：

* ``POST /node/binding/device`` 拿 ``user_code`` / ``verify_url`` / ``expires_in``；
* ``POST /node/binding/device/poll`` 回 ``pending|approved|denied|expired``；
* 退避 **5s → 15s → 30s**（之后 30s），**满 10 分钟停止**；
* 云端 404 / 超时 / 连不上 → 「绑定服务未开通，请用高级→手输绑定码」，**不假成功**；
* 开关 ``client.binding.device_flow.enabled`` 默认关：关 → 只留手输码老入口。
"""
from __future__ import annotations

import json

import pytest

from djb_node import device_flow as df
from djb_node import facts
from djb_node.desktop import controller as ctrl_mod
from djb_node.desktop import datasource as ds


class _CloudError(Exception):
    def __init__(self, status=None, reason=""):
        super().__init__(reason or str(status))
        self.status = status
        self.reason = reason


class _Clock:
    """假时钟 + 假 sleep：一秒都不真等。"""

    def __init__(self):
        self.now = 0.0
        self.sleeps: list[int] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(int(seconds))
        self.now += float(seconds)


# ─────────────────────────────────────────────── 退避与截止
def test_backoff_is_five_fifteen_thirty_then_steady():
    assert [df.next_delay(i) for i in range(6)] == [5, 15, 30, 30, 30, 30]


def test_deadline_is_ten_minutes():
    assert df.DEADLINE_S == 600
    flow = df.DeviceFlow(state="pending")
    assert df.should_stop(flow, 599) is False
    assert df.should_stop(flow, 600) is True
    assert df.should_stop(df.DeviceFlow(state="approved"), 1) is True


def test_not_open_text_is_the_agreed_copy():
    assert df.NOT_OPEN_TEXT.startswith("绑定服务未开通，请用高级→手输绑定码")


# ─────────────────────────────────────────────── 起手（/device）
def test_start_parses_contract_fields():
    flow = df.parse_start({"user_code": "8F3K-2P9Q", "device_code": "dc_1",
                           "verify_url": "https://djb.example/activate",
                           "expires_in": 600, "interval": 5})
    assert flow.state == "pending" and flow.user_code == "8F3K-2P9Q"
    assert flow.verify_url.endswith("/activate") and flow.expires_in == 600


def test_start_without_codes_is_unavailable_not_pending():
    flow = df.parse_start({"verify_url": "https://djb.example/activate"})
    assert flow.state == "unavailable"
    assert flow.hint == df.NOT_OPEN_TEXT
    assert flow.user_code == ""                      # 不编造用户码


# ─────────────────────────────────────────────── 轮询（/poll）
@pytest.mark.parametrize("state,approved", [("approved", True), ("denied", False),
                                            ("expired", False), ("pending", False)])
def test_poll_states_map_exactly(state, approved):
    flow = df.apply_poll(df.DeviceFlow(state="pending", device_code="dc"), {"state": state})
    assert flow.state == state
    assert flow.approved is approved                 # 只有 approved 才算绑定成功


def test_unknown_state_never_counts_as_success():
    flow = df.apply_poll(df.DeviceFlow(state="pending"), {"state": "weird"})
    assert flow.state == "unavailable" and flow.approved is False


def test_poll_body_uses_the_agreed_field_name():
    assert df.poll_body(df.DeviceFlow(device_code="dc_9")) == {"device_code": "dc_9"}


# ─────────────────────────────────────────────── 端到端（假 post + 假时钟）
def _runner(script, clock, *, stop=None):
    calls: list[tuple[str, dict]] = []

    def post(path, body):
        calls.append((path, dict(body)))
        if path.endswith("/device"):
            return {"user_code": "8F3K-2P9Q", "device_code": "dc_1",
                    "verify_url": "https://djb.example/activate", "expires_in": 600}
        item = script.pop(0) if script else {"state": "pending"}
        if isinstance(item, Exception):
            raise item
        return item

    runner = df.DeviceFlowRunner(post, sleep=clock.sleep, clock=clock.clock, stop=stop)
    return runner, calls


def test_approved_on_third_poll_and_wait_times_are_5_15_30():
    clock = _Clock()
    runner, calls = _runner([{"state": "pending"}, {"state": "pending"},
                             {"state": "approved"}], clock)
    flow = runner.run()
    assert flow.state == "approved" and flow.approved is True
    assert clock.sleeps == [5, 15, 30]                # 逐次退避
    assert [path.rsplit("/", 1)[-1] for path, _ in calls] == ["device", "poll", "poll", "poll"]
    assert calls[1][1] == {"device_code": "dc_1"}


def test_denied_stops_immediately():
    clock = _Clock()
    runner, calls = _runner([{"state": "denied"}], clock)
    flow = runner.run()
    assert flow.state == "denied" and flow.approved is False
    assert len(calls) == 2                             # 一次 start + 一次 poll


def test_pending_forever_stops_at_ten_minutes():
    clock = _Clock()
    runner, calls = _runner([], clock)                 # 永远 pending
    flow = runner.run()
    assert flow.state == "expired" and "10 分钟" in flow.reason
    assert clock.now >= df.DEADLINE_S
    assert clock.now < df.DEADLINE_S + 60              # 没有无限等
    assert len(calls) <= 1 + df.DEADLINE_S // 30 + 2


def test_404_on_start_degrades_with_the_agreed_copy():
    clock = _Clock()

    def post(path, body):
        raise _CloudError(404, "not found")

    flow = df.DeviceFlowRunner(post, sleep=clock.sleep, clock=clock.clock).run()
    assert flow.state == "unavailable"
    assert flow.hint == df.NOT_OPEN_TEXT
    assert flow.approved is False


def test_timeout_during_poll_retries_then_degrades_when_the_endpoint_is_missing():
    clock = _Clock()
    script = [_CloudError(None, "云端连不上（TimeoutError）"), _CloudError(404, "gone")]
    runner, calls = _runner(script, clock)
    flow = runner.run()
    # 第一次超时 → 继续按退避重试；第二次 404 → 服务未开通
    assert flow.state == "unavailable" and flow.hint == df.NOT_OPEN_TEXT
    assert flow.events.count("poll_retry") == 1


def test_stop_flag_ends_the_loop_without_fake_success():
    clock = _Clock()
    runner, _calls = _runner([], clock, stop=lambda: True)
    flow = runner.run()
    assert flow.state == "pending" and flow.approved is False
    assert "cancelled" in flow.events


# ─────────────────────────────────────────────── 控制器：开关 / 降级 / 状态注入
class _Cloud:
    def __init__(self, start=None, polls=None):
        self._start = start or {"user_code": "U1", "device_code": "dc",
                                "verify_url": "https://x/activate", "expires_in": 600}
        self._polls = list(polls or [])
        self.paths: list[str] = []

    def device_start(self):
        self.paths.append("/device")
        if isinstance(self._start, Exception):
            raise self._start
        return self._start

    def device_poll(self, device_code):
        self.paths.append("/poll")
        item = self._polls.pop(0) if self._polls else {"state": "expired"}
        if isinstance(item, Exception):
            raise item
        return item


class _Source:
    def __init__(self, home, cloud=None):
        self._home = str(home)
        self.cloud = cloud

    def snapshot(self, sections=None):
        sections_map = dict(ds.Snapshot(sections={}).sections)
        account = {"profile": {"bound": False}, "security": {"node_id": "node-1"},
                   "update": {}, "twofa": None}
        sections_map.update({"account": account,
                             "access": {"config": {}, "binding": {"state": "unbound"}}})
        return ds.Snapshot(sections_map)


@pytest.fixture()
def home(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"home": str(tmp_path)}), encoding="utf-8")
    return tmp_path


def _controller(home, *, enabled, cloud=None, polls=None, monkeypatch=None):
    source = _Source(home, _Cloud(polls=polls) if cloud is None else cloud)
    controller = ctrl_mod.DesktopController(source)
    controller.onboarding = None
    monkeypatch.setattr(controller, "device_flow_enabled", lambda: enabled)
    return controller


def test_flag_off_points_at_the_manual_code_path(home, monkeypatch):
    controller = _controller(home, enabled=False, monkeypatch=monkeypatch)
    result = controller.action("account.bind_device")
    assert not result.ok and "手输绑定码" in result.message
    assert controller.device_flow is None              # 一个请求都没发


def test_approved_result_says_bound_and_refreshes(home, monkeypatch):
    controller = _controller(home, enabled=True, polls=[{"state": "approved"}],
                             monkeypatch=monkeypatch)
    result = controller.action("account.bind_device")
    assert result.ok and "绑定成功" in result.message
    assert result.effect == "refresh"
    assert controller.device_flow.approved is True


def test_denied_result_is_not_ok(home, monkeypatch):
    controller = _controller(home, enabled=True, polls=[{"state": "denied"}],
                             monkeypatch=monkeypatch)
    result = controller.action("account.bind_device")
    assert not result.ok and "拒绝" in result.message


def test_missing_endpoint_uses_the_agreed_copy_and_no_fake_success(home, monkeypatch):
    cloud = _Cloud(start=_CloudError(404, "no route"))
    controller = _controller(home, enabled=True, cloud=cloud, monkeypatch=monkeypatch)
    result = controller.action("account.bind_device")
    assert not result.ok
    assert "绑定服务未开通，请用高级→手输绑定码" in result.message
    assert controller.device_flow.approved is False


def test_no_cloud_client_degrades(home, monkeypatch):
    controller = _controller(home, enabled=True, monkeypatch=monkeypatch)
    controller._device_client = lambda: None
    result = controller.action("account.bind_device")
    assert not result.ok and "绑定服务未开通" in result.message


def test_pending_state_reaches_the_account_view(home, monkeypatch):
    controller = _controller(home, enabled=True, monkeypatch=monkeypatch)
    controller.device_flow = df.DeviceFlow(state="pending", user_code="8F3K-2P9Q",
                                           verify_url="https://djb.example/activate",
                                           attempts=2)
    view = controller.show("account")
    keys = [action.key for action in view.actions]
    assert "account.bind_device_cancel" in keys and "account.copy_verify_url" in keys
    assert "account.bind_device" in keys
    text = view.plain_text()
    assert "8F3K-2P9Q" in text and "5/15/30" in text


def test_manual_code_entry_stays_reachable_while_device_flow_is_on(home, monkeypatch):
    controller = _controller(home, enabled=True, monkeypatch=monkeypatch)
    keys = [action.key for action in controller.show("account").actions]
    assert "account.bind" in keys                      # 老入口不能断


def test_cancel_stops_waiting_without_requests(home, monkeypatch):
    controller = _controller(home, enabled=True, monkeypatch=monkeypatch)
    controller.device_flow = df.DeviceFlow(state="pending", user_code="U1")
    result = controller.action("account.bind_device_cancel")
    assert result.ok and controller.device_flow.state == "expired"
    assert result.effect == "refresh"


def test_copy_verify_url_copies_the_link(home, monkeypatch):
    controller = _controller(home, enabled=True, monkeypatch=monkeypatch)
    controller.device_flow = df.DeviceFlow(state="pending", user_code="U1",
                                          verify_url="https://djb.example/activate")
    result = controller.action("account.copy_verify_url")
    assert result.effect == "copy" and result.payload.endswith("/activate")


def test_flag_registered_with_three_fields_and_on_by_default():
    # 2026-11-18：三端已上线，0.3.12 起默认开启；三字段（owner/expire_at/rollback_rule）
    # 仍必须齐全——这是「可回滚」的前提，不是可选装饰。
    spec = facts.FEATURE_FLAGS[facts.DEVICE_FLOW_FLAG]
    assert spec["default"] is True
    for name in ("owner", "expire_at", "rollback_rule", "description"):
        assert str(spec.get(name) or "").strip(), name
    assert facts.DEVICE_FLOW_FLAG == "client.binding.device_flow.enabled"
