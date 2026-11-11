# SPDX-License-Identifier: Apache-2.0
"""C16 · 客户端消息中心：幂等 upsert、过滤、离线读态队列、ack、prune、周期、CLI、默认关闭。

不联网（本机回环 mock / 注入替身）、不真 sleep（注入 sleep 记录器）。
"""
from __future__ import annotations

import json
import logging
import os
import threading

import pytest

from djb_node import facts, logging_setup
from djb_node import notifications as notify_mod
from djb_node.api import NodeApi
from djb_node.config import ClientConfig, save_config
from djb_node.errors import NetworkError
from djb_node.httpclient import HttpResponse
from djb_node.notifications import (NotificationStore, notification_db_file,
                                     notifications_enabled, parse_unread_count)
from tests.mock_notify import MockNotify

SECRET = "ck_" + "test_notify_key_0123456789abcdef"
NOW = "2026-10-24T00:00:00Z"
ITEM = {"id": "n-1", "severity": "P1", "category": "quota",
        "template_key": "quota.near_limit",
        "payload": {"threshold_pct": 90, "window": "daily"},
        "created_at": "2026-10-23T00:00:00Z",
        "expires_at": "2026-11-23T00:00:00Z"}


# ─────────────────────────────────────────────────────────── 夹具 / 替身
class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.items: list[dict] = []

    def emit(self, record: logging.LogRecord) -> None:
        fields = getattr(record, "fields", None) or {}
        merged = {"event": getattr(record, "event", record.getMessage())}
        merged.update(fields)
        self.items.append(merged)


@pytest.fixture
def events():
    logger = logging_setup.get_logger()
    handler = Capture()
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    yield handler.items
    logger.removeHandler(handler)
    logger.setLevel(previous)


class FakeApi:
    def __init__(self, items=None, *, list_error=None, read_error=None, ack_error=None,
                 unread=0):
        self.items = list(items or [])
        self.list_error = list_error
        self.read_error = read_error
        self.ack_error = ack_error
        self.unread = unread
        self.list_calls: list[dict] = []
        self.reads: list[str] = []
        self.acks: list[str] = []
        self.stop_event: threading.Event | None = None
        self.stop_after: int | None = None

    def list_node_notifications(self, since=None, limit=50, category=None):
        self.list_calls.append({"since": since, "limit": limit, "category": category})
        if (self.stop_event is not None and self.stop_after is not None
                and len(self.list_calls) >= self.stop_after):
            self.stop_event.set()
        if self.list_error is not None:
            raise self.list_error
        return {"items": list(self.items), "unread_count": self.unread}

    def node_notification_unread_count(self):
        return {"unread_count": self.unread}

    def read_node_notification(self, notification_id):
        self.reads.append(notification_id)
        if self.read_error is not None:
            raise self.read_error
        return {"ok": True}

    def ack_node_notification(self, notification_id):
        self.acks.append(notification_id)
        if self.ack_error is not None:
            raise self.ack_error
        return {"ok": True}


def make_store(paths, *, api=None, config=None, sleep=None, clock=None,
               in_daemon=False) -> NotificationStore:
    config = config or ClientConfig(gateway_url="http://127.0.0.1:1", mirror_port=1717,
                                    extra={facts.NOTIFICATIONS_FLAG: True})
    return NotificationStore(paths, config=config, api=api, sleep=sleep, now=clock)


def _http_404() -> NetworkError:
    return NetworkError("mock upstream down", code=50903)


# ─────────────────────────────────────────────────────────── 开关
def test_flag_default_off_with_three_fields(monkeypatch):
    monkeypatch.delenv(facts.NOTIFICATIONS_ENV, raising=False)
    for config in (None, ClientConfig()):
        assert notifications_enabled(config) is False
    spec = facts.FEATURE_FLAGS[facts.NOTIFICATIONS_FLAG]
    assert facts.NOTIFICATIONS_FLAG == "client.notifications.enabled"
    assert spec["default"] is False
    assert spec["owner"] == "client-seat"
    assert spec["expire_at"] == "2027-06-30"
    assert "停拉取" in str(spec["rollback_rule"])
    assert spec.get("description") and "desc" not in spec
    on = ClientConfig(extra={"feature_flags": {facts.NOTIFICATIONS_FLAG: True}})
    assert notifications_enabled(on) is True


def test_env_switch_activates_for_s2(monkeypatch):
    monkeypatch.setenv(facts.NOTIFICATIONS_ENV, "1")
    assert notifications_enabled(ClientConfig()) is True
    monkeypatch.setenv(facts.NOTIFICATIONS_ENV, "0")
    assert notifications_enabled(ClientConfig()) is False


# ─────────────────────────────────────────────────────────── upsert / 过滤
def test_upsert_is_idempotent(paths, clock):
    store = make_store(paths, clock=clock)
    assert store.upsert(ITEM) is True
    assert store.upsert(ITEM) is False                 # 同 id 幂等
    assert store.status()["total"] == 1
    assert len(store.list()) == 1


def test_upsert_updates_server_fields_but_keeps_local_read(paths, clock):
    store = make_store(paths, clock=clock)
    store.upsert(ITEM)
    store.mark_read("n-1")                             # 本地已读（此时无 api → 进队列）
    updated = dict(ITEM, severity="P0", template_key="quota.exhausted")
    assert store.upsert(updated) is False
    record = store.get("n-1")
    assert record["severity"] == "P0"
    assert record["template_key"] == "quota.exhausted"
    assert record["read_at"] is not None               # 本地读态不被覆盖


def test_list_filters_by_category_and_unread(paths, clock):
    store = make_store(paths, clock=clock)
    store.upsert(ITEM)
    store.upsert(dict(ITEM, id="n-2", category="update", template_key="update.ready"))
    assert {row["id"] for row in store.list(category="quota")} == {"n-1"}
    assert {row["id"] for row in store.list(category="update")} == {"n-2"}
    assert len(store.list(unread_only=True)) == 2
    store.mark_read("n-1")
    assert {row["id"] for row in store.list(unread_only=True)} == {"n-2"}
    assert len(store.list()) == 2                      # 已读仍在列表里


def test_unread_count_ignores_read_and_expired(paths, clock):
    store = make_store(paths, clock=clock)
    store.upsert(ITEM)
    store.upsert(dict(ITEM, id="n-2"))
    store.upsert(dict(ITEM, id="n-expired", expires_at="2026-10-23T12:00:00Z"))
    assert store.unread_count() == 2                   # 过期的不算未读、也不在列表
    assert {row["id"] for row in store.list()} == {"n-1", "n-2"}
    store.mark_read("n-1")
    assert store.unread_count() == 1


# ─────────────────────────────────────────────────────────── sync
def test_sync_stores_items_and_returns_counts(paths, clock, events):
    api = FakeApi([ITEM, dict(ITEM, id="n-2", created_at="2026-10-23T01:00:00Z")],
                  unread=2)
    store = make_store(paths, api=api, clock=clock)
    result = store.sync()
    assert result["added"] == 2 and result["updated"] == 0
    assert result["error_class"] is None and result["unread"] == 2
    assert store.status()["last_sync"] == "2026-10-24T00:00:00Z"
    assert [item["event"] for item in events].count("notifications.synced") == 1


def test_sync_is_idempotent_and_uses_cursor(paths, clock):
    api = FakeApi([ITEM])
    store = make_store(paths, api=api, clock=clock)
    store.sync()
    assert api.list_calls[0]["since"] is None
    again = store.sync()
    assert again["added"] == 0 and again["updated"] == 1
    assert api.list_calls[1]["since"] == "2026-10-23T00:00:00Z"   # 本地最大 created_at
    assert store.status()["total"] == 1


def test_sync_skips_expired_items(paths, clock):
    api = FakeApi([ITEM, dict(ITEM, id="n-old", expires_at="2026-10-23T12:00:00Z")])
    store = make_store(paths, api=api, clock=clock)
    result = store.sync()
    assert result["added"] == 1 and result["skipped_expired"] == 1
    assert store.get("n-old") is None


def test_sync_network_error_keeps_old_data(paths, clock, events):
    api = FakeApi([ITEM])
    store = make_store(paths, api=api, clock=clock)
    store.sync()
    store.api = FakeApi([], list_error=_http_404())
    result = store.sync()
    assert result["error_class"] == "fetch_failed"
    assert store.status()["total"] == 1                # 旧数据保留
    assert store.status()["last_error"] == "fetch_failed"
    assert [item for item in events if item["event"] == "notifications.sync_failed"]


def test_storage_only_mode_without_api(paths, clock):
    """`NotificationStore(paths)`（无 config / 无 api）：纯本地存储，不联网。"""
    store = NotificationStore(paths, now=clock)
    assert store.api is None
    assert store.upsert(ITEM) is True and store.unread_count() == 1
    result = store.sync()
    assert result["error_class"] == "no_api" and result["added"] == 0
    marked = store.mark_read("n-1")
    assert marked["pending"] is True and store.get("n-1")["read_at"] is not None
    assert store.flush()["error_class"] == "no_api"
    assert store.server_unread_count() is None


# ─────────────────────────────────────────────────────────── 已读 / ack / 离线队列
def test_mark_read_local_and_server_success(paths, clock):
    api = FakeApi([ITEM])
    store = make_store(paths, api=api, clock=clock)
    store.sync()
    result = store.mark_read("n-1")
    assert result["synced"] is True and result["pending"] is False
    assert api.reads == ["n-1"]
    assert store.get("n-1")["read_at"] is not None
    assert store.pending_acks() == []


def test_mark_read_offline_keeps_local_state_and_queues(paths, clock, events):
    api = FakeApi([ITEM])
    store = make_store(paths, api=api, clock=clock)
    store.sync()
    store.api = FakeApi([], read_error=_http_404())
    result = store.mark_read("n-1")
    assert result["synced"] is False and result["pending"] is True
    assert store.get("n-1")["read_at"] is not None          # 离线读态不丢
    pending = store.pending_acks()
    assert [(item["id"], item["action"]) for item in pending] == [("n-1", "read")]
    assert pending[0]["last_error"] == "NetworkError"
    assert [item for item in events if item["event"] == "notifications.read_pending"]


def test_ack_sets_read_and_queues_on_failure(paths, clock):
    store = make_store(paths, api=FakeApi([ITEM]), clock=clock)
    store.sync()
    store.api = FakeApi([], ack_error=_http_404())
    result = store.ack("n-1")
    assert result["pending"] is True
    record = store.get("n-1")
    assert record["acked_at"] is not None and record["read_at"] is not None
    assert [item["action"] for item in store.pending_acks()] == ["ack"]


def test_flush_retries_pending_and_clears_on_success(paths, clock, events):
    store = make_store(paths, api=FakeApi([ITEM]), clock=clock)
    store.sync()
    store.api = FakeApi([], read_error=_http_404())
    store.mark_read("n-1")
    assert len(store.pending_acks()) == 1
    store.api = FakeApi([])                                # 服务端恢复
    result = store.flush()
    assert result["sent"] == 1 and result["remaining"] == 0
    assert store.pending_acks() == []
    assert [item for item in events if item["event"] == "notifications.flushed"]


def test_flush_keeps_pending_and_counts_attempts_on_failure(paths, clock):
    store = make_store(paths, api=FakeApi([ITEM]), clock=clock)
    store.sync()
    store.api = FakeApi([], read_error=_http_404())
    store.mark_read("n-1")
    first = store.flush()
    assert first["failed"] == 1 and first["remaining"] == 1
    assert store.pending_acks()[0]["attempts"] >= 2        # 入队 1 次 + 失败 1 次
    second = store.flush()
    assert second["failed"] == 1 and second["remaining"] == 1


def test_flush_is_idempotent_for_duplicates(paths, clock):
    store = make_store(paths, api=FakeApi([ITEM]), clock=clock)
    store.sync()
    store.api = FakeApi([], read_error=_http_404())
    store.mark_read("n-1")
    store.mark_read("n-1")                                  # 重复入队只留一行
    assert len(store.pending_acks()) == 1
    store.api = FakeApi([])
    assert store.flush()["sent"] == 1
    assert store.flush()["sent"] == 0                       # 再 flush 无事可做


# ─────────────────────────────────────────────────────────── prune / 落盘
def test_prune_removes_expired_and_ancient(paths, clock):
    store = make_store(paths, clock=clock)
    store.upsert(ITEM)                                                     # 未过期
    store.upsert(dict(ITEM, id="n-exp", expires_at="2026-10-23T12:00:00Z"))  # 已过期
    store.upsert(dict(ITEM, id="n-ancient", created_at="2026-01-01T00:00:00Z",
                      expires_at=None))                                    # 超 180 天
    result = store.prune()
    assert result["expired"] == 1 and result["expired_by_age"] == 1
    assert {row["id"] for row in store.list()} == {"n-1"}
    assert store.get("n-exp") is None and store.get("n-ancient") is None


def test_prune_drops_orphan_pending(paths, clock):
    store = make_store(paths, clock=clock)
    store.upsert(ITEM)
    store.mark_read("n-1")                                  # 无 api → 入队
    assert len(store.pending_acks()) == 1
    store.prune()                                           # 消息还在 → 队列保留
    assert len(store.pending_acks()) == 1
    store.upsert(dict(ITEM, id="n-1", expires_at="2026-10-23T12:00:00Z"))
    store.prune()                                           # 消息被删 → 队列清孤儿
    assert store.pending_acks() == []


def test_db_file_is_private(paths, clock):
    store = make_store(paths, clock=clock)
    store.upsert(ITEM)
    path = notification_db_file(paths)
    assert os.path.basename(path) == facts.NOTIFICATIONS_DB
    try:
        if os.name == "nt":
            from djb_node.file_security import assert_private_file_object
            for filename in (path, path + "-wal", path + "-shm"):
                assert os.path.isfile(filename)
                assert_private_file_object(os.path.abspath(filename))
        else:
            assert oct(os.stat(path).st_mode)[-3:] == "600"
    finally:
        store.close()


def test_events_carry_no_token_or_payload(paths, clock, events):
    api = FakeApi([ITEM])
    store = make_store(paths, api=api, clock=clock)
    store.sync()
    store.mark_read("n-1")
    dumped = json.dumps(events, ensure_ascii=False)
    assert SECRET not in dumped
    assert "threshold_pct" not in dumped           # payload 不进事件
    keys = {key for item in events for key in item}
    assert not keys & {"payload", "content", "token", "client_key"}


# ─────────────────────────────────────────────────────────── 周期
def test_periodic_syncs_then_waits_interval(paths, clock):
    sleeps: list[float] = []
    api = FakeApi([ITEM])
    store = make_store(paths, api=api, clock=clock, sleep=sleeps.append)
    stop = threading.Event()
    api.stop_event = stop
    api.stop_after = 2
    store.run_periodic(stop)
    assert len(api.list_calls) == 2
    assert sleeps == [float(facts.NOTIFICATIONS_INTERVAL_S)] * 2


def test_periodic_backoff_on_failure(paths, clock):
    sleeps: list[float] = []
    api = FakeApi([], list_error=_http_404())
    store = make_store(paths, api=api, clock=clock, sleep=sleeps.append)
    stop = threading.Event()
    api.stop_event = stop
    api.stop_after = 3
    store.run_periodic(stop)
    assert sleeps == [60.0, 300.0, 900.0]


def test_periodic_stops_when_event_already_set(paths, clock):
    sleeps: list[float] = []
    api = FakeApi([ITEM])
    store = make_store(paths, api=api, clock=clock, sleep=sleeps.append)
    stop = threading.Event()
    stop.set()
    store.run_periodic(stop)
    assert api.list_calls == [] and sleeps == []


# ─────────────────────────────────────────────────────────── api.py 层
class FakeHttp:
    def __init__(self, response: HttpResponse) -> None:
        self.response = response
        self.calls: list[dict] = []

    def request(self, method, url, *, headers=None, json_body=None, body=None,
                timeout=None):
        self.calls.append({"method": method, "url": url, "headers": dict(headers or {}),
                           "json_body": json_body, "timeout": timeout})
        return self.response


def _json_response(payload) -> HttpResponse:
    return HttpResponse(status=200, headers={},
                        body=json.dumps(payload).encode(), url="", elapsed_ms=1)


def test_api_list_notifications_builds_query_and_sends_key():
    http = FakeHttp(_json_response({"items": []}))
    api = NodeApi("https://gateway.test", client_key=SECRET, http=http)
    api.list_node_notifications(since="2026-10-23T00:00:00Z", limit=10, category="quota")
    call = http.calls[0]
    assert facts.NOTIFICATIONS_PATH in call["url"]
    assert "since=2026-10-23T00" in call["url"] and "limit=10" in call["url"]
    assert "category=quota" in call["url"]
    from djb_node import protocol
    assert call["headers"][protocol.NODE_KEY_HEADER] == SECRET
    assert call["timeout"] == facts.NOTIFICATIONS_SYNC_TIMEOUT_S


def test_api_unread_count_and_actions_use_expected_paths():
    http = FakeHttp(_json_response({"unread_count": 3}))
    api = NodeApi("https://gateway.test", client_key=SECRET, http=http)
    assert api.node_notification_unread_count()["unread_count"] == 3
    api.read_node_notification("n-1")
    api.ack_node_notification("n-1")
    assert http.calls[0]["url"].endswith(facts.NOTIFICATIONS_UNREAD_PATH)
    assert http.calls[1]["url"].endswith("/api/v1/node/notifications/n-1/read")
    assert http.calls[2]["url"].endswith("/api/v1/node/notifications/n-1/ack")
    assert http.calls[1]["method"] == "POST"


# ─────────────────────────────────────────────────────────── 真实回环集成
def test_integration_sync_read_and_flush(paths, credentials):
    from djb_node.httpclient import HttpClient

    server = MockNotify().start()
    try:
        server.items = [ITEM]
        server.unread_count = 1
        credentials.set_node("node-1", SECRET)
        config = ClientConfig(gateway_url=server.base_url, mirror_port=1717,
                              extra={facts.NOTIFICATIONS_FLAG: True})
        api = NodeApi(config.gateway_url, client_key=credentials.client_key(),
                      http=HttpClient(timeout=5.0))
        store = NotificationStore(paths, config=config, api=api)
        result = store.sync()
        assert result["added"] == 1 and store.unread_count() == 1
        assert server.list_headers[0]["x-client-key"] == SECRET
        # 服务端暂时失败 → 本地已读 + 排队
        server.fail_read_times = 1
        first = store.mark_read("n-1")
        assert first["pending"] is True and store.unread_count() == 0
        assert len(store.pending_acks()) == 1
        # 恢复后 flush 成功
        flushed = store.flush()
        assert flushed["sent"] == 1 and store.pending_acks() == []
        assert server.actions == [{"id": "n-1", "action": "read"},
                                  {"id": "n-1", "action": "read"}]
        assert store.server_unread_count() == 1
    finally:
        server.stop()


# ─────────────────────────────────────────────────────────── CLI
def test_cli_notify_list_read_ack_unread_sync(paths, store, credentials, capsys):
    from djb_node.cli import main

    server = MockNotify().start()
    try:
        server.items = [ITEM]
        server.unread_count = 1
        config = ClientConfig(gateway_url=server.base_url, mirror_port=1717)
        save_config(paths, config)
        credentials.set_node("node-1", SECRET)
        assert main(["notify", "sync", "--json"]) == 0
        synced = json.loads(capsys.readouterr().out)
        assert synced["added"] == 1 and synced["unread"] == 1
        assert main(["notify", "list", "--json"]) == 0
        listed = json.loads(capsys.readouterr().out)
        assert listed["count"] == 1 and listed["items"][0]["id"] == "n-1"
        assert main(["notify", "unread", "--json"]) == 0
        unread = json.loads(capsys.readouterr().out)
        assert unread["unread"] == 1 and unread["server_unread"] == 1
        assert main(["notify", "read", "--id", "n-1", "--json"]) == 0
        read = json.loads(capsys.readouterr().out)
        assert read["synced"] is True
        assert main(["notify", "ack", "--id", "n-1", "--json"]) == 0
        acked = json.loads(capsys.readouterr().out)
        assert acked["synced"] is True
        assert SECRET not in json.dumps([synced, listed, unread, read, acked])
        assert [item["action"] for item in server.actions] == ["read", "ack"]
    finally:
        server.stop()


# ─────────────────────────────────────────────────────────── daemon 线程起停
class StubNotifications:
    instances: list["StubNotifications"] = []

    def __init__(self, paths, *, config=None, api=None, http=None, now=None):
        self.stop_event: threading.Event | None = None
        self.closed = False
        StubNotifications.instances.append(self)

    def status(self):
        return {"unread": 0, "total": 0, "pending": 0, "last_sync": None,
                "last_error": None, "cursor": None, "syncs": 0}

    def run_periodic(self, stop_event):
        self.stop_event = stop_event
        stop_event.wait(30)

    def close(self):
        self.closed = True


@pytest.fixture
def daemon_env(paths, store, credentials):
    from tests.mock_m02 import MockServer
    from tests.test_daemon import _daemon

    server = MockServer().start()
    yield paths, store, credentials, server, _daemon
    server.stop()


def test_daemon_has_no_notifications_by_default(daemon_env, monkeypatch):
    from djb_node import daemon as daemon_module

    monkeypatch.delenv(facts.NOTIFICATIONS_ENV, raising=False)
    paths, store, credentials, mock, make_daemon = daemon_env
    StubNotifications.instances = []
    monkeypatch.setattr(daemon_module, "NotificationStore", StubNotifications)
    daemon = make_daemon(paths, store, credentials, mock)
    daemon.start()
    # 2026-11-05 起 Windows 本地控制通道**默认开启**（决策人批准，见 tests/feature_flag_policy.py），
    # 不再有"控制不可用"的默认形态；kill switch 关掉时的降级由
    # tests/test_control_channel_default.py 覆盖。
    assert daemon._started is True
    assert daemon.status()["control"]["available"] is True
    try:
        assert StubNotifications.instances == []
        assert daemon._notifications is None and daemon._notifications_thread is None
        status = daemon.status()["notifications"]
        assert status["enabled"] is False and status["running"] is False
        assert daemon.status()["flags"][facts.NOTIFICATIONS_FLAG]["enabled"] is False
    finally:
        daemon.stop()


def test_daemon_starts_and_stops_notifications_thread(daemon_env, monkeypatch):
    from djb_node import daemon as daemon_module

    monkeypatch.delenv(facts.NOTIFICATIONS_ENV, raising=False)
    paths, store, credentials, mock, make_daemon = daemon_env
    StubNotifications.instances = []
    monkeypatch.setattr(daemon_module, "NotificationStore", StubNotifications)
    config = ClientConfig(gateway_url=mock.base_url, mirror_port=1717,
                          extra={facts.NOTIFICATIONS_FLAG: True})
    daemon = make_daemon(paths, store, credentials, mock, config=config)
    daemon.start()
    # 2026-11-05 起 Windows 本地控制通道**默认开启**（决策人批准，见 tests/feature_flag_policy.py），
    # 不再有"控制不可用"的默认形态；kill switch 关掉时的降级由
    # tests/test_control_channel_default.py 覆盖。
    assert daemon._started is True
    assert daemon.status()["control"]["available"] is True
    thread = daemon._notifications_thread
    try:
        assert len(StubNotifications.instances) == 1
        assert thread is not None and thread.is_alive()
        status = daemon.status()["notifications"]
        assert status["enabled"] is True and status["running"] is True
        assert status["interval_s"] == facts.NOTIFICATIONS_INTERVAL_S
    finally:
        daemon.stop()
    assert thread is not None and not thread.is_alive()
    assert daemon._notifications_thread is None
    assert StubNotifications.instances[0].closed is True       # 线程退出时关库
    assert daemon.status()["notifications"]["running"] is False


def test_daemon_env_switch_starts_thread_for_s2(daemon_env, monkeypatch):
    from djb_node import daemon as daemon_module

    monkeypatch.setenv(facts.NOTIFICATIONS_ENV, "1")
    paths, store, credentials, mock, make_daemon = daemon_env
    StubNotifications.instances = []
    monkeypatch.setattr(daemon_module, "NotificationStore", StubNotifications)
    daemon = make_daemon(paths, store, credentials, mock)
    daemon.start()
    # 2026-11-05 起 Windows 本地控制通道**默认开启**（决策人批准，见 tests/feature_flag_policy.py），
    # 不再有"控制不可用"的默认形态；kill switch 关掉时的降级由
    # tests/test_control_channel_default.py 覆盖。
    assert daemon._started is True
    assert daemon.status()["control"]["available"] is True
    try:
        assert len(StubNotifications.instances) == 1
        assert daemon.status()["notifications"]["running"] is True
    finally:
        daemon.stop()



def test_windows_start_control_unavailable_does_not_hide_other_errors(daemon_env, monkeypatch):
    from djb_node import daemon as module
    paths, store, credentials, mock, make_daemon = daemon_env
    daemon = make_daemon(paths, store, credentials, mock)
    def fail(*a, **k): raise ValueError("test-factory-failure")
    monkeypatch.setattr(module, "control_server_factory", fail)
    with pytest.raises(ValueError, match="test-factory-failure"):
        daemon._start_windows()
    assert not daemon._started and daemon._control is None


def test_windows_start_fake_available_control_preserves_wiring(daemon_env, monkeypatch):
    from djb_node import daemon as module
    paths, store, credentials, mock, make_daemon = daemon_env
    daemon = make_daemon(paths, store, credentials, mock)
    calls = []
    class Control:
        alive = False
        def open(self): calls.append("open")
        def start(self): self.alive = True; calls.append("start")
        def is_alive(self): return self.alive
        def join(self, **kw): self.alive = False
        def close_bound(self): calls.append("close")
    control = Control()
    monkeypatch.setattr(module, "control_server_factory", lambda *a, **k: control)
    daemon._start_windows()
    try:
        assert calls == ["open", "start"]
        assert daemon.status()["control"] == {"available": True, "reason": None}
        assert daemon.control({"cmd": "ping"})["ok"] is True
    finally:
        daemon.stop()


def test_windows_start_fake_unavailable_open_keeps_daemon(daemon_env, monkeypatch):
    from djb_node import daemon as module
    from djb_node.ipc_windows import IpcUnavailable
    paths, store, credentials, mock, make_daemon = daemon_env
    daemon = make_daemon(paths, store, credentials, mock)
    closed = []
    class Control:
        def open(self): raise IpcUnavailable("test-unavailable")
        def close_bound(self): closed.append(True)
    monkeypatch.setattr(module, "control_server_factory", lambda *a, **k: Control())
    daemon._start_windows()
    try:
        assert daemon._started and daemon._control is None and closed == [True]
        assert daemon.status()["control"]["available"] is False
        with pytest.raises(IpcUnavailable): daemon.control({"cmd": "pause"})
    finally:
        daemon.stop()


def test_file_object_api_does_not_call_ancestor_pins(monkeypatch):
    from djb_node import file_security as fs
    sid = "S-1-5-21-123"
    calls = []
    class Backend:
        def identity(self): return sid
        def volume(self, root): calls.append("volume")
        def open(self, path): return 7
        def close(self, handle): calls.append("close")
        def snapshot(self, handle):
            return fs._Security(sid, True, False, True, (fs._Ace(0,0,fs._FILE,sid),), False, False, 1)
    monkeypatch.setattr(fs, "_Win32", Backend)
    monkeypatch.setattr(fs.os, "name", "nt")
    monkeypatch.setattr(fs, "_pinned_ancestors", lambda *a: pytest.fail("object-only API pinned ancestors"))
    fs.assert_private_file_object(r"E:\private\notifications.db")
    assert calls == ["volume", "close"]


def test_file_object_api_rejects_public_dacl(monkeypatch):
    from djb_node import file_security as fs
    sid = "S-1-5-21-123"
    closed = []
    class Backend:
        def identity(self): return sid
        def volume(self, root): pass
        def open(self, path): return 7
        def close(self, handle): closed.append(handle)
        def snapshot(self, handle):
            return fs._Security(sid, True, False, True,
                (fs._Ace(0,0,fs._FILE,sid), fs._Ace(0,0,0x120089,"S-1-1-0")), False, False, 1)
    monkeypatch.setattr(fs, "_Win32", Backend)
    monkeypatch.setattr(fs.os, "name", "nt")
    with pytest.raises(fs.FileSecurityError, match="unsafe_dacl"):
        fs.assert_private_file_object(r"E:\private\notifications.db")
    assert closed == [7]



@pytest.mark.parametrize("failure", ["restore", "mirror", "join"])
def test_windows_failed_start_cleanup_sentinel_blocks_retry(monkeypatch, failure):
    from types import SimpleNamespace
    import threading
    from djb_node import daemon as module
    from djb_node.ipc_windows import IpcUnavailable
    from djb_node.errors import UsageError
    d = module.Daemon.__new__(module.Daemon)
    d._started = False; d._control = None
    d._stop_event = threading.Event()
    d.paths = SimpleNamespace(prepare=lambda: None, log_file="unused")
    d.store = SimpleNamespace(get_kv=lambda _: {}, is_initialized=lambda: True, set_kv=lambda *a: None)
    d.config = SimpleNamespace(log_level="INFO")
    d.credentials = SimpleNamespace(ensure_permissions=lambda: None)
    d._persist = lambda: None
    from djb_node.util import utcnow
    d._now = utcnow
    restores = []
    def restore(value):
        restores.append(value)
        if failure == "restore" and len(restores) > 1: raise RuntimeError("rollback")
    d.machine = SimpleNamespace(snapshot="before", restore=restore, start=lambda: None)
    def start_fail(): raise ValueError("start-failed")
    def close():
        if failure == "mirror": raise RuntimeError("close-failed")
    d.server = SimpleNamespace(bind=lambda: None, port=1, token_fingerprint=lambda: "test",
                               start=start_fail, close_bound=close)
    class Control:
        alive = False
        def open(self): pass
        def start(self): self.alive = True
        def is_alive(self): return self.alive
        def join(self, **kw): pass
    control = Control()
    def factory(*a, **k):
        if failure != "join": raise IpcUnavailable("test")
        return control
    monkeypatch.setattr(module, "control_server_factory", factory)
    monkeypatch.setattr(module, "setup_logging", lambda *a, **k: None)
    with pytest.raises(ValueError, match="start-failed"):
        d._start_windows()
    assert d._start_cleanup_incomplete is True
    assert d._control is (control if failure == "join" else None)
    with pytest.raises(UsageError, match="清理未完成"):
        d.start()



def test_windows_open_unavailable_close_failure_preserves_error_and_blocks_retry(daemon_env, monkeypatch):
    from djb_node import daemon as module
    from djb_node.ipc_windows import IpcUnavailable
    from djb_node.errors import UsageError
    paths, store, credentials, mock, make_daemon = daemon_env
    daemon = make_daemon(paths, store, credentials, mock)
    failure = OSError("test-close-failed")
    class Control:
        def open(self): raise IpcUnavailable("test-open-unavailable")
        def close_bound(self): raise failure
    control = Control()
    monkeypatch.setattr(module, "control_server_factory", lambda *a, **k: control)
    with pytest.raises(OSError, match="^test-close-failed$") as raised:
        daemon._start_windows()
    assert raised.value is failure
    assert daemon._start_cleanup_incomplete is True
    assert daemon._control is control
    with pytest.raises(UsageError, match="清理未完成"):
        daemon.start()


# ─────────────────────────────────────────────────────────── G4：未读数口径
def test_unread_count_parses_spec_field_and_fallback():
    """spec 字段是 ``unread``；``unread_count`` 只是 tolerant 回退；都没有 -> None（不猜 0）。"""
    assert parse_unread_count({"unread": 3}) == 3
    assert parse_unread_count({"unread_count": 4}) == 4
    assert parse_unread_count({"unread": 0}) == 0            # 真的是 0 与「拿不到」要区分
    assert parse_unread_count({"unread": 2, "unread_count": 9}) == 2   # spec 优先
    assert parse_unread_count({}) is None
    assert parse_unread_count(None) is None
    assert parse_unread_count({"unread": "3"}) is None
    assert parse_unread_count({"unread": True}) is None
    assert parse_unread_count({"unread": -1}) is None


def test_server_unread_count_uses_spec_shape(paths, credentials):
    """回环集成：服务端按 spec 回 `{"unread": n}`，客户端必须读出 n（G4 的另一半）。"""
    from djb_node.httpclient import HttpClient
    server = MockNotify().start()
    try:
        server.unread_count = 7
        credentials.set_node("node-1", SECRET)
        config = ClientConfig(gateway_url=server.base_url, mirror_port=1717,
                              extra={facts.NOTIFICATIONS_FLAG: True})
        api = NodeApi(config.gateway_url, client_key=credentials.client_key(),
                      http=HttpClient(timeout=5.0))
        store = NotificationStore(paths, config=config, api=api)
        assert store.server_unread_count() == 7
        # 旧错路径（unread-count）现在服务端 404 -> 客户端不得把 404 当成 0
        server.unread_count = 0
        assert store.server_unread_count() == 0
    finally:
        server.stop()
