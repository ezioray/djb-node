# SPDX-License-Identifier: Apache-2.0
"""C16 · 客户端消息中心（本地存储 / 未读 / 已读 / ack / 离线补拉）。

为三端托盘 UI 提供**统一数据层**（本模块不做 GUI）。服务端下发站内信元数据，客户端：

* 本地落一份（独立 SQLite 文件 ``notifications.db``，WAL + 事务；**不存正文/凭据**）；
* 按 ``id`` 幂等 upsert（重复拉取不产生重复行，也不覆盖本地已读态）；
* 未读 / 分类过滤 / 过期过滤 / 保留 180 天后 prune；
* 已读 / ack **先写本地**再调服务端；服务端失败进离线队列，``flush()`` 幂等重试
  （离线读态不丢，这是验收点）；
* 开关 ``client.notifications.enabled`` 默认关闭；S2 测试网 ``DJB_NOTIFY=1`` 显式激活。

数据形状（服务端 → 客户端，字段缺失一律兜底）::

    {"items": [{"id": "n-1", "severity": "P1", "category": "quota",
                "template_key": "quota.near_limit",
                "payload": {"threshold_pct": 90, "window": "daily"},
                "created_at": "2026-10-24T00:00:00Z",
                "expires_at": "2026-11-24T00:00:00Z"}],
     "next_cursor": "...", "unread_count": 3}

``payload`` 只放 id / 金额 / 时间 / 原因码（M12 INV-07 口径）；本模块原样存 JSON 字符串，
不进日志、不进事件。**本地已读/ack 时间戳永远不被服务端响应覆盖**（离线读态不丢）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable, Mapping

from . import facts
from .api import NodeApi
from .config import ClientConfig, Paths, feature_flag
from .errors import DjbError, UsageError
from .httpclient import HttpClient
from .logging_setup import event
from .util import iso, parse_iso, utcnow

__all__ = [
    "NotificationStore",
    "NotificationRecord",
    "notification_db_file",
    "notifications_enabled",
    "parse_unread_count",
]

_SCHEMA_SQL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS node_notifications (
        id TEXT PRIMARY KEY,
        severity TEXT NOT NULL DEFAULT 'P2',
        category TEXT NOT NULL DEFAULT '',
        template_key TEXT NOT NULL DEFAULT '',
        payload_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        expires_at TEXT,
        delivered_at TEXT NOT NULL,
        read_at TEXT,
        acked_at TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_node_notifications_unread
        ON node_notifications (read_at, created_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_node_notifications_created
        ON node_notifications (created_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS node_notification_outbox (
        id TEXT NOT NULL,
        action TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        queued_at TEXT NOT NULL,
        PRIMARY KEY (id, action)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS node_notification_state (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
)


def notifications_enabled(config: Any) -> bool:
    """开关判定：``facts`` 登记值 **或** S2 测试网 ``DJB_NOTIFY=1`` 显式激活。"""
    if config is not None and feature_flag(config, facts.NOTIFICATIONS_FLAG):
        return True
    return os.environ.get(facts.NOTIFICATIONS_ENV, "").strip() == "1"


def notification_db_file(paths: Paths) -> str:
    """本地消息库路径（独立文件，不动 state.db 的 schema 版本）。"""
    return os.path.join(paths.home, facts.NOTIFICATIONS_DB)


@dataclass
class NotificationRecord:
    id: str
    severity: str = "P2"
    category: str = ""
    template_key: str = ""
    payload: dict[str, Any] | None = None
    created_at: str = ""
    expires_at: str | None = None
    delivered_at: str = ""
    read_at: str | None = None
    acked_at: str | None = None

    @property
    def unread(self) -> bool:
        return not self.read_at

    def to_dict(self) -> dict[str, Any]:
        """给 CLI / UI 的形状（payload 原样，已读/ack 时间戳可用于展示）。"""
        return {"id": self.id, "severity": self.severity, "category": self.category,
                "template_key": self.template_key, "payload": dict(self.payload or {}),
                "created_at": self.created_at, "expires_at": self.expires_at,
                "delivered_at": self.delivered_at, "read_at": self.read_at,
                "acked_at": self.acked_at, "unread": self.unread}


class NotificationStore:
    """本地消息中心：SQLite 存储 + 未读/已读/ack + 离线队列 + 周期同步。"""

    def __init__(self, paths: Paths, *, config: ClientConfig | None = None,
                 api: NodeApi | None = None, http: HttpClient | None = None,
                 now: Callable[[], Any] = utcnow,
                 sleep: Callable[[float], Any] | None = None) -> None:
        self.paths = paths
        self.config = config
        self._now = now or utcnow
        self._sleep = sleep
        self.api = api
        if api is None and config is not None:
            self.api = NodeApi(config.gateway_url, http=http or HttpClient(
                timeout=float(facts.NOTIFICATIONS_SYNC_TIMEOUT_S)))
        self.db_path = notification_db_file(paths)
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self._last_sync: str | None = None
        self._last_error: str | None = None
        self._syncs = 0

    # ── 连接 / schema
    def connect(self) -> sqlite3.Connection:
        if self._connection is not None:
            return self._connection
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), mode=0o700,
                    exist_ok=True)
        windows_files = (self.db_path, self.db_path + "-wal", self.db_path + "-shm")
        if os.name == "nt":
            from .file_security import create_private_file_object
            # Pre-create all SQLite writable sidecars with explicit private DACL.
            # This is object confidentiality, NOT ancestor/path authentication.
            for path in windows_files:
                create_private_file_object(os.path.abspath(path))
        connection = sqlite3.connect(self.db_path, timeout=10.0,
                                     check_same_thread=False)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=5000")
            with connection:
                for statement in _SCHEMA_SQL:
                    connection.execute(statement)
                connection.execute("PRAGMA user_version="
                                   + str(facts.NOTIFICATIONS_SCHEMA_VERSION))
            if os.name == "nt":
                from .file_security import assert_private_file_object
                for path in windows_files:
                    assert_private_file_object(os.path.abspath(path))
            else:
                _chmod_private(self.db_path)
        except BaseException:
            connection.close()
            raise
        self._connection = connection
        return connection

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> "NotificationStore":
        self.connect()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # ── 内部工具
    def _state_get(self, key: str) -> str | None:
        row = self.connect().execute(
            "SELECT value FROM node_notification_state WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def _state_set(self, key: str, value: str) -> None:
        connection = self.connect()
        with connection:
            connection.execute(
                "INSERT INTO node_notification_state (key, value) VALUES (?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def _row_to_record(self, row: sqlite3.Row) -> NotificationRecord:
        payload: dict[str, Any] = {}
        try:
            raw = json.loads(row["payload_json"] or "{}")
            payload = raw if isinstance(raw, dict) else {}
        except ValueError:                                     # pragma: no cover - 脏数据
            payload = {}
        return NotificationRecord(
            id=str(row["id"]), severity=str(row["severity"] or "P2"),
            category=str(row["category"] or ""), template_key=str(row["template_key"] or ""),
            payload=payload, created_at=str(row["created_at"] or ""),
            expires_at=row["expires_at"], delivered_at=str(row["delivered_at"] or ""),
            read_at=row["read_at"], acked_at=row["acked_at"])

    def _expired(self, record: NotificationRecord) -> bool:
        expires = parse_iso(record.expires_at)
        return expires is not None and expires <= self._now()

    # ── 写入（幂等 upsert；不覆盖本地已读/ack）
    def upsert(self, payload: Mapping[str, Any]) -> bool:
        """单条 upsert：返回 True = 新增，False = 已存在（更新了服务端字段）。

        冲突时**只更新服务端字段**，``read_at`` / ``acked_at`` 用 ``COALESCE`` 保留本地值
        （服务端可能不知道本地离线读态）。
        """
        record = _record_of(payload, now=self._now)
        if not record.id:
            return False
        connection = self.connect()
        with connection:
            exists = connection.execute(
                "SELECT 1 FROM node_notifications WHERE id=?", (record.id,)).fetchone()
            connection.execute(
                """
                INSERT INTO node_notifications
                    (id, severity, category, template_key, payload_json, created_at,
                     expires_at, delivered_at, read_at, acked_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    severity=excluded.severity,
                    category=excluded.category,
                    template_key=excluded.template_key,
                    payload_json=excluded.payload_json,
                    created_at=excluded.created_at,
                    expires_at=excluded.expires_at,
                    delivered_at=excluded.delivered_at,
                    read_at=COALESCE(node_notifications.read_at, excluded.read_at),
                    acked_at=COALESCE(node_notifications.acked_at, excluded.acked_at)
                """,
                (record.id, record.severity, record.category, record.template_key,
                 json.dumps(record.payload or {}, ensure_ascii=False, sort_keys=True),
                 record.created_at, record.expires_at, record.delivered_at,
                 record.read_at, record.acked_at))
        return not bool(exists)

    # ── 读
    def list(self, category: str | None = None, unread_only: bool = False,
             limit: int = facts.NOTIFICATIONS_DEFAULT_LIMIT) -> list[dict[str, Any]]:
        """列表（created_at 倒序）；默认过滤已过期；``unread_only`` 只看未读。"""
        count = max(1, min(int(limit), facts.NOTIFICATIONS_MAX_LIMIT))
        clauses = ["(expires_at IS NULL OR expires_at > ?)"]
        params: list[Any] = [iso(self._now())]
        if category:
            clauses.append("category = ?")
            params.append(str(category))
        if unread_only:
            clauses.append("read_at IS NULL")
        sql = ("SELECT * FROM node_notifications WHERE " + " AND ".join(clauses)
               + " ORDER BY created_at DESC, id DESC LIMIT ?")
        params.append(count)
        rows = self.connect().execute(sql, params).fetchall()
        return [self._row_to_record(row).to_dict() for row in rows]

    def get(self, notification_id: str) -> dict[str, Any] | None:
        row = self.connect().execute(
            "SELECT * FROM node_notifications WHERE id=?", (str(notification_id),)).fetchone()
        return self._row_to_record(row).to_dict() if row else None

    def unread_count(self) -> int:
        """本地未读数（不含已过期）。"""
        row = self.connect().execute(
            "SELECT COUNT(*) AS n FROM node_notifications WHERE read_at IS NULL"
            " AND (expires_at IS NULL OR expires_at > ?)", (iso(self._now()),)).fetchone()
        return int(row["n"] if row else 0)

    # ── 已读 / ack（本地先落，服务端失败进队列）
    def _enqueue(self, notification_id: str, action: str, error: str | None) -> None:
        connection = self.connect()
        with connection:
            connection.execute(
                """
                INSERT INTO node_notification_outbox (id, action, attempts, last_error,
                                                    queued_at)
                VALUES (?,?,?,?,?)
                ON CONFLICT(id, action) DO UPDATE SET
                    attempts=node_notification_outbox.attempts + 1,
                    last_error=excluded.last_error
                """,
                (notification_id, action,
                 1 if error else 0, error, iso(self._now())))
            connection.execute(
                "DELETE FROM node_notification_outbox WHERE rowid IN ("
                " SELECT rowid FROM node_notification_outbox"
                " ORDER BY queued_at ASC LIMIT MAX(0, (SELECT COUNT(*)"
                " FROM node_notification_outbox) - ?))",
                (facts.NOTIFICATIONS_MAX_PENDING,))

    def mark_read(self, notification_id: str) -> dict[str, Any]:
        """本地置已读 + 调服务端；服务端失败保留 pending（离线读态不丢）。"""
        key = str(notification_id or "").strip()
        if not key:
            raise UsageError("通知 id 不能为空")
        stamp = iso(self._now())
        connection = self.connect()
        with connection:
            connection.execute(
                "UPDATE node_notifications SET read_at=COALESCE(read_at, ?) WHERE id=?",
                (stamp, key))
        synced = False
        error: str | None = None
        if self.api is not None:
            try:
                self.api.read_node_notification(key)
                synced = True
            except DjbError as exc:
                error = type(exc).__name__
                self._enqueue(key, "read", error)
                event("notifications.read_pending", level="warning", id=key,
                      error=error)
        else:
            error = "no_api"
            self._enqueue(key, "read", error)
        return {"id": key, "read_at": stamp, "synced": synced, "pending": not synced}

    def ack(self, notification_id: str) -> dict[str, Any]:
        """本地置 ack（同时视作已读）+ 调服务端；失败同样进 pending 队列。"""
        key = str(notification_id or "").strip()
        if not key:
            raise UsageError("通知 id 不能为空")
        stamp = iso(self._now())
        connection = self.connect()
        with connection:
            connection.execute(
                "UPDATE node_notifications SET acked_at=COALESCE(acked_at, ?),"
                " read_at=COALESCE(read_at, ?) WHERE id=?", (stamp, stamp, key))
        synced = False
        error: str | None = None
        if self.api is not None:
            try:
                self.api.ack_node_notification(key)
                synced = True
            except DjbError as exc:
                error = type(exc).__name__
                self._enqueue(key, "ack", error)
                event("notifications.ack_pending", level="warning", id=key,
                      error=error)
        else:
            error = "no_api"
            self._enqueue(key, "ack", error)
        return {"id": key, "acked_at": stamp, "synced": synced, "pending": not synced}

    def pending_acks(self, limit: int = 100) -> list[dict[str, Any]]:
        """离线队列（按入队时间）。"""
        rows = self.connect().execute(
            "SELECT id, action, attempts, last_error, queued_at"
            " FROM node_notification_outbox ORDER BY queued_at ASC LIMIT ?",
            (max(1, int(limit)),)).fetchall()
        return [{"id": str(row["id"]), "action": str(row["action"]),
                 "attempts": int(row["attempts"] or 0),
                 "last_error": row["last_error"], "queued_at": row["queued_at"]}
                for row in rows]

    def flush(self) -> dict[str, Any]:
        """重试离线队列（服务端动作幂等）：成功出队，失败累加 attempts 后保留。"""
        if self.api is None:
            return {"sent": 0, "failed": 0, "remaining": len(self.pending_acks()),
                    "error_class": "no_api"}
        sent = 0
        failed = 0
        for item in self.pending_acks(limit=facts.NOTIFICATIONS_MAX_PENDING):
            try:
                if item["action"] == "read":
                    self.api.read_node_notification(item["id"])
                else:
                    self.api.ack_node_notification(item["id"])
            except DjbError as exc:
                failed += 1
                self._enqueue(item["id"], item["action"], type(exc).__name__)
                continue
            connection = self.connect()
            with connection:
                connection.execute(
                    "DELETE FROM node_notification_outbox WHERE id=? AND action=?",
                    (item["id"], item["action"]))
            sent += 1
        remaining = len(self.pending_acks())
        if sent or failed:
            event("notifications.flushed", sent=sent, failed=failed, remaining=remaining)
        return {"sent": sent, "failed": failed, "remaining": remaining,
                "error_class": None}

    # ── 同步
    def sync(self, *, limit: int = facts.NOTIFICATIONS_DEFAULT_LIMIT,
             category: str | None = None) -> dict[str, Any]:
        """拉取并落库：``since`` 用本地最大 created_at 游标；过期条目丢弃；网络失败保留旧数据。"""
        with self._lock:
            self._syncs += 1
            if self.api is None:
                return {"added": 0, "updated": 0, "skipped_expired": 0,
                        "error_class": "no_api", "cursor": self._state_get("cursor")}
            since = self._state_get("cursor") or self._max_created_at()
            try:
                payload = self.api.list_node_notifications(since=since, limit=limit,
                                                           category=category)
            except DjbError as exc:
                self._last_error = "fetch_failed"
                event("notifications.sync_failed", level="warning",
                      error=type(exc).__name__, code=exc.code)
                return {"added": 0, "updated": 0, "skipped_expired": 0,
                        "error_class": "fetch_failed", "cursor": since}
            items = _items_of(payload)
            added = 0
            updated = 0
            skipped = 0
            for item in items:
                record = _record_of(item, now=self._now)
                if not record.id:
                    continue
                if self._expired(record):
                    skipped += 1
                    continue
                if self.upsert(item):
                    added += 1
                else:
                    updated += 1
            cursor = self._max_created_at() or since
            if cursor:
                self._state_set("cursor", cursor)
            self._last_sync = iso(self._now())
            self._last_error = None
            event("notifications.synced", added=added, updated=updated,
                  skipped_expired=skipped, unread=self.unread_count())
            return {"added": added, "updated": updated, "skipped_expired": skipped,
                    "error_class": None, "cursor": cursor,
                    "unread": self.unread_count()}

    def _max_created_at(self) -> str | None:
        row = self.connect().execute(
            "SELECT MAX(created_at) AS value FROM node_notifications").fetchone()
        return str(row["value"]) if row and row["value"] else None

    # ── 清理
    def prune(self, now: Any | None = None) -> dict[str, int]:
        """删除已过期条目与超过保留期（180 天）的条目；返回删除条数。"""
        moment = now or self._now()
        cutoff = iso(moment - timedelta(days=facts.NOTIFICATIONS_RETENTION_DAYS))
        connection = self.connect()
        with connection:
            expired = connection.execute(
                "DELETE FROM node_notifications WHERE expires_at IS NOT NULL"
                " AND expires_at <= ?", (iso(moment),)).rowcount
            old = connection.execute(
                "DELETE FROM node_notifications WHERE created_at < ?",
                (cutoff,)).rowcount
            orphan = connection.execute(
                "DELETE FROM node_notification_outbox WHERE id NOT IN"
                " (SELECT id FROM node_notifications)").rowcount
        return {"expired": int(expired or 0), "expired_by_age": int(old or 0),
                "orphan_pending": int(orphan or 0),
                "removed": int((expired or 0) + (old or 0))}

    # ── 状态
    def status(self) -> dict[str, Any]:
        """快照（只计数与时间，不含 payload / 凭据）。"""
        return {"enabled": notifications_enabled(self.config),
                "unread": self.unread_count(),
                "total": int(self.connect().execute(
                    "SELECT COUNT(*) AS n FROM node_notifications").fetchone()["n"]),
                "pending": len(self.pending_acks()),
                "last_sync": self._last_sync,
                "last_error": self._last_error,
                "cursor": self._state_get("cursor"),
                "syncs": self._syncs,
                "interval_s": facts.NOTIFICATIONS_INTERVAL_S,
                "db": facts.NOTIFICATIONS_DB}

    def server_unread_count(self) -> int | None:
        """服务端未读数（只读展示用；不可达返回 None，不影响本地）。

        字段口径见 `facts.NOTIFICATIONS_UNREAD_FIELDS`：spec 是 ``unread``，
        ``unread_count`` 作为回退（tolerant reader）。两者都缺 → None
        （不把「解析不到」伪装成 0，否则角标又是「没有新消息」）。
        """
        if self.api is None:
            return None
        try:
            payload = self.api.node_notification_unread_count()
        except DjbError:
            return None
        value = parse_unread_count(payload)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        return None

    # ── 周期（daemon 线程 / CLI --watch 复用）
    def run_periodic(self, stop_event: threading.Event) -> None:
        """启动即拉一次 + 每次 flush 离线队列；失败按 60/300/900/1800 s 退避。"""
        event("notifications.thread_started", interval_s=facts.NOTIFICATIONS_INTERVAL_S)
        backoff = 0
        try:
            while not stop_event.is_set():
                result = self.sync()
                self.flush()
                if result.get("error_class") is None:
                    backoff = 0
                    wait = float(facts.NOTIFICATIONS_INTERVAL_S)
                else:
                    wait = float(facts.NOTIFICATIONS_BACKOFF_S[
                        min(backoff, len(facts.NOTIFICATIONS_BACKOFF_S) - 1)])
                    backoff += 1
                event("notifications.tick", wait_s=wait,
                      unread=self.unread_count())
                self._wait(wait, stop_event)
        except Exception as exc:                               # pragma: no cover - 防御
            event("notifications.thread_crashed", level="warning",
                  error=type(exc).__name__)
        finally:
            event("notifications.thread_stopped", syncs=self._syncs)

    def _wait(self, seconds: float, stop_event: threading.Event) -> None:
        if self._sleep is not None:
            self._sleep(seconds)
            return
        stop_event.wait(seconds)


# ─────────────────────────────────────────────────────────── 归一工具
def parse_unread_count(payload: Any) -> int | None:
    """未读数响应 → int；解析不到返回 None（**不猜 0**）。

    字段优先级见 `facts.NOTIFICATIONS_UNREAD_FIELDS`（spec 的 ``unread`` 优先，
    ``unread_count`` 回退）。非整数 / 布尔 / 负数一律视为「解析不到」。
    """
    if not isinstance(payload, Mapping):
        return None
    for key in facts.NOTIFICATIONS_UNREAD_FIELDS:
        value = payload.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def _items_of(payload: Any) -> list[Mapping[str, Any]]:
    """服务端列表字段兜底：items / notifications / data。"""
    if not isinstance(payload, Mapping):
        return []
    for key in ("items", "notifications", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, Mapping)]
    return []


def _record_of(raw: Mapping[str, Any], *,
               now: Callable[[], Any] = utcnow) -> NotificationRecord:
    payload = raw.get("payload")
    created = raw.get("created_at")
    expires = raw.get("expires_at")
    severity = str(raw.get("severity") or "P2")
    stamp = iso(now())
    return NotificationRecord(
        id=str(raw.get("id") or "").strip(),
        severity=severity if severity in facts.NOTIFICATIONS_SEVERITIES else "P2",
        category=str(raw.get("category") or "").strip(),
        template_key=str(raw.get("template_key") or "").strip(),
        payload=dict(payload) if isinstance(payload, Mapping) else {},
        created_at=str(created) if created else stamp,
        expires_at=str(expires) if expires else None,
        delivered_at=stamp,
        read_at=str(raw["read_at"]) if raw.get("read_at") else None,
        acked_at=str(raw["acked_at"]) if raw.get("acked_at") else None,
    )


def _chmod_private(path: str) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:                                            # pragma: no cover - 防御
        pass
