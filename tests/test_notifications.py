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
from djb_node.notifications import NotificationStore, notification_db_file, notifications_enabled
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
    assert spec["expire_at"] == "2026-12-31"
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
    assert oct(os.stat(path).st_mode)[-3:] == "600"


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
    try:
        assert len(StubNotifications.instances) == 1
        assert daemon.status()["notifications"]["running"] is True
    finally:
        daemon.stop()
