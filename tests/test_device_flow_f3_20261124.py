# SPDX-License-Identifier: Apache-2.0
"""R8 F3（2026-11-24）：设备码三处客户端断点。

云端现网形状（repos/djb-cloud/app/rc8_admin/device_auth.py）：

* ``POST /node/binding/device`` 回 ``verify_url="/activate"``（**相对路径**）；
* ``POST /node/binding/device/poll`` 回 ``status``（不是 ``state``）。

客户端必须：相对 verify_url 用网关基址拼绝对地址再交给浏览器；poll 两个字段都认；
create 请求带上本机 node_id。
"""
from __future__ import annotations

from djb_node import device_flow as df


class _Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[int] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(int(seconds))
        self.now += float(seconds)


# ─────────────────────────────────────────────── poll：status / state 双兼容
def test_poll_accepts_cloud_status_field():
    flow = df.apply_poll(df.DeviceFlow(state="pending", device_code="dc"),
                         {"status": "approved", "node_id": "n_1"})
    assert flow.state == "approved" and flow.approved is True
    assert flow.bound_node_id == "n_1"


def test_poll_accepts_legacy_state_field_unchanged():
    flow = df.apply_poll(df.DeviceFlow(state="pending"), {"state": "denied"})
    assert flow.state == "denied" and flow.approved is False


def test_state_wins_when_both_fields_are_present():
    flow = df.apply_poll(df.DeviceFlow(state="pending"),
                         {"state": "approved", "status": "denied", "node_id": "n_1"})
    assert flow.state == "approved"


def test_status_expired_is_a_terminal_state():
    flow = df.apply_poll(df.DeviceFlow(state="pending"), {"status": "expired"})
    assert flow.state == "expired" and flow.approved is False
    assert df.should_stop(flow, 0) is True


def test_unknown_status_still_never_counts_as_success():
    flow = df.apply_poll(df.DeviceFlow(state="pending"), {"status": "weird"})
    assert flow.state == "unavailable" and flow.approved is False


# ─────────────────────────────────────────────── verify_url：相对路径 → 绝对地址
def test_relative_verify_url_is_joined_with_gateway_base():
    flow = df.parse_start({"user_code": "U1", "device_code": "dc",
                           "verify_url": "/activate", "expires_in": 600},
                          base_url="https://tkbank.ai")
    assert flow.verify_url == "https://tkbank.ai/activate"


def test_absolute_verify_url_is_kept_as_is():
    flow = df.parse_start({"user_code": "U1", "device_code": "dc",
                           "verify_url": "https://web.example/activate"},
                          base_url="https://tkbank.ai")
    assert flow.verify_url == "https://web.example/activate"


def test_relative_verify_url_without_base_is_not_opened_as_relative():
    flow = df.parse_start({"user_code": "U1", "device_code": "dc",
                           "verify_url": "/activate"})
    assert flow.verify_url == "", "没有基址时宁可不给链接，也不让 webbrowser 打开相对路径"


def test_non_http_verify_url_is_rejected():
    flow = df.parse_start({"user_code": "U1", "device_code": "dc",
                           "verify_url": "javascript:alert(1)"},
                          base_url="https://tkbank.ai")
    assert flow.verify_url == ""


def test_runner_uses_base_url_for_relative_verify_url_and_sends_node_id():
    clock = _Clock()
    calls: list[tuple[str, dict]] = []

    def post(path, body):
        calls.append((path, dict(body)))
        if path.endswith("/device"):
            return {"user_code": "U1", "device_code": "dc", "verify_url": "/activate",
                    "expires_in": 600, "status": "pending"}
        return {"status": "approved", "node_id": "n_1"}

    flow = df.DeviceFlowRunner(post, sleep=clock.sleep, clock=clock.clock,
                               node_id="node-1", base_url="https://tkbank.ai").run()
    assert calls[0][1] == {"node_id": "node-1"}
    assert flow.verify_url == "https://tkbank.ai/activate"
    assert flow.state == "approved" and flow.bound_node_id == "n_1"
