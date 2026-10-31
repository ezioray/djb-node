# SPDX-License-Identifier: Apache-2.0
"""本机状态库（SQLite；M15 §5：state.db / upstreams / receipts 本地，无服务端表）。

三条硬规则：

1. **PRAGMA user_version 只前进**：库版本高于本进程 -> 拒绝运行（降级安装拒绝），
   低于本进程 -> 前向迁移；不做逆向迁移。
2. **收据只存密封串**：receipts.sealed 是 crypto.seal 的结果；明文只在内存里，
   不落库、不落日志（INV-02 / §5 保留 ≤24 h）。
3. **凭据不进库**：client_key / 签名密钥只在 0600 文件（secure.py），库里只有
   credential_ref（本地引用名）。
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from . import facts
from .errors import StoreDowngradeError, StoreNotInitializedError
from .util import iso, new_install_id, parse_iso, sha256_hex, utcnow

SCHEMA_VERSION = 3

_SCHEMA_SQL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS install (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        install_id_hash TEXT NOT NULL,
        version TEXT NOT NULL,
        channel TEXT NOT NULL,
        bucket INTEGER NOT NULL,
        os TEXT NOT NULL,
        arch TEXT NOT NULL,
        installed_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS profile (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        preset TEXT NOT NULL,
        daily_tokens INTEGER NOT NULL,
        weekly_tokens INTEGER NOT NULL,
        monthly_tokens INTEGER NOT NULL,
        concurrent INTEGER NOT NULL,
        schedule TEXT NOT NULL,
        default_tier TEXT NOT NULL,
        cpu_pct INTEGER NOT NULL,
        gpu_pct INTEGER NOT NULL,
        mem_pct INTEGER NOT NULL,
        yield_enabled INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS upstreams (
        id TEXT PRIMARY KEY,
        access_type TEXT NOT NULL,
        base_url TEXT NOT NULL,
        models TEXT NOT NULL,
        credential_ref TEXT,
        state TEXT NOT NULL,
        last_probe_at TEXT,
        protocol TEXT,
        upstream_kind TEXT,
        created_at TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS policy (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        policy_version TEXT NOT NULL,
        protocol_version TEXT NOT NULL,
        digest TEXT NOT NULL,
        issued_at TEXT,
        expires_at TEXT,
        quotas TEXT NOT NULL,
        directives TEXT NOT NULL,
        settings TEXT NOT NULL,
        verify_state TEXT NOT NULL,
        fetched_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS announcements (
        seq INTEGER PRIMARY KEY,
        kind TEXT,
        access TEXT,
        model TEXT,
        max_qps REAL,
        ask_price TEXT,
        unit TEXT,
        resource_level TEXT,
        expires_at TEXT,
        raw TEXT NOT NULL,
        recorded_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS receipts (
        receipt_id TEXT PRIMARY KEY,
        sealed TEXT NOT NULL,
        buffered_at TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TEXT,
        last_error TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS usage_windows (
        window_key TEXT NOT NULL,
        window_start TEXT NOT NULL,
        tokens INTEGER NOT NULL DEFAULT 0,
        requests INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (window_key, window_start)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS update_state (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        current_version TEXT,
        next_version TEXT,
        bucket INTEGER,
        rollback_to TEXT,
        verify_state TEXT,
        channel TEXT,
        checked_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS env_state (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        detected_at TEXT,
        authorized_at TEXT,
        plan TEXT,
        report TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS kv (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
)


@dataclass
class InstallRecord:
    install_id_hash: str
    version: str
    channel: str
    bucket: int
    os: str
    arch: str
    installed_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "install_id_hash": self.install_id_hash,
            "version": self.version,
            "channel": self.channel,
            "bucket": self.bucket,
            "os": self.os,
            "arch": self.arch,
            "installed_at": self.installed_at,
        }


@dataclass
class ProfileRecord:
    preset: str = facts.DEFAULT_PRESET
    daily_tokens: int = facts.DEFAULT_DAILY_TOKENS
    weekly_tokens: int = 0          # 0 = 未启用（spec 只给 daily 默认值，见 README CR-M15-04）
    monthly_tokens: int = 0
    concurrent: int = facts.DEFAULT_CONCURRENT
    schedule: list[dict[str, Any]] = field(default_factory=list)
    default_tier: str = "realtime"
    cpu_pct: int = facts.DEFAULT_CPU_PCT
    gpu_pct: int = facts.DEFAULT_GPU_PCT
    mem_pct: int = facts.DEFAULT_MEM_PCT
    yield_enabled: bool = facts.DEFAULT_YIELD_ENABLED

    def to_dict(self) -> dict[str, Any]:
        return {
            "preset": self.preset,
            "limits": {
                "daily_tokens": self.daily_tokens,
                "weekly_tokens": self.weekly_tokens,
                "monthly_tokens": self.monthly_tokens,
                "concurrent": self.concurrent,
            },
            "schedule": list(self.schedule),
            "default_tier": self.default_tier,
            "resources": {"cpu_pct": self.cpu_pct, "gpu_pct": self.gpu_pct,
                          "mem_pct": self.mem_pct},
            "yield_enabled": bool(self.yield_enabled),
        }


@dataclass
class UpstreamRecord:
    id: str
    access_type: str
    base_url: str
    models: list[dict[str, Any]]
    credential_ref: str | None
    state: str
    last_probe_at: str | None
    protocol: str
    created_at: str
    enabled: bool
    upstream_kind: str | None = None        # M17 §4.1；NULL=未标注

    def model_ids(self) -> list[str]:
        return [str(item.get("id")) for item in self.models if item.get("id")]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "access_type": self.access_type,
            "base_url": self.base_url,
            "models": list(self.models),
            "credential_ref": self.credential_ref,
            "state": self.state,
            "last_probe_at": self.last_probe_at,
            "protocol": self.protocol,
            "created_at": self.created_at,
            "enabled": bool(self.enabled),
        }


@dataclass
class PolicyRecord:
    policy_version: str
    protocol_version: str
    digest: str
    issued_at: str | None
    expires_at: str | None
    quotas: list[dict[str, Any]]
    directives: list[str]
    settings: dict[str, Any]
    verify_state: str
    fetched_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_version": self.policy_version,
            "protocol_version": self.protocol_version,
            "digest": self.digest,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "quotas": list(self.quotas),
            "directives": list(self.directives),
            "settings": dict(self.settings),
            "verify_state": self.verify_state,
            "fetched_at": self.fetched_at,
        }

    def is_fresh(self, now: datetime | None = None) -> bool:
        moment = now or utcnow()
        expiry = parse_iso(self.expires_at)
        if expiry is None:
            return False
        return moment < expiry

    def is_expired(self, now: datetime | None = None) -> bool:
        return not self.is_fresh(now)


@dataclass
class ReceiptRow:
    receipt_id: str
    sealed: str
    buffered_at: str
    attempts: int
    next_attempt_at: str | None
    last_error: str | None
    stale: bool = False


@dataclass
class UpdateRecord:
    current_version: str | None = None
    next_version: str | None = None
    bucket: int = 0
    rollback_to: str | None = None
    verify_state: str = "unchecked"
    channel: str = facts.DEFAULT_UPDATE_CHANNEL
    checked_at: str | None = None


class Store:
    def __init__(self, path: str, *, now=utcnow) -> None:
        self.path = os.path.abspath(path)
        self._now = now
        self._connection: sqlite3.Connection | None = None

    # ─────────────────────────────────────────────────────── 连接 / schema
    def connect(self) -> sqlite3.Connection:
        if self._connection is not None:
            return self._connection
        if not os.path.exists(self.path):
            raise StoreNotInitializedError("本机状态库不存在：先执行 djb init")
        # daemon 的主循环与 Unix 控制套接字线程共用一个 Store（同一文件、WAL），
        # 因此关掉 sqlite3 的线程归属检查；SQLite 本身按串行化模式编译。
        connection = sqlite3.connect(self.path, timeout=10.0, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            connection.close()
            raise StoreDowngradeError(
                "本地状态库版本 " + str(version) + " 高于本客户端支持的 "
                + str(SCHEMA_VERSION) + "：拒绝降级运行（请升级客户端）")
        if version < SCHEMA_VERSION:
            with connection:
                if version < 1:
                    for statement in _SCHEMA_SQL:
                        connection.execute(statement)
                if version < 3 and not _has_column(connection, "upstreams", "upstream_kind"):
                    # v3：上游加类型标签（M17 §4.1 upstream_kind；NULL=未标注，旧行不回填）
                    connection.execute("ALTER TABLE upstreams ADD COLUMN upstream_kind TEXT")
                if version < 2 and not _has_column(connection, "receipts", "stale"):
                    # v2：收据增加 stale 标记（超 24 h 不丢弃，标 stale + 停接单，Lead 裁决 CR-2）
                    connection.execute("ALTER TABLE receipts ADD COLUMN stale INTEGER"
                                       " NOT NULL DEFAULT 0")
                connection.execute("PRAGMA user_version=" + str(SCHEMA_VERSION))
        self._connection = connection
        return connection

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> "Store":
        self.connect()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            with connection:
                yield connection
        finally:
            pass

    # ─────────────────────────────────────────────────────── 初始化
    @classmethod
    def create(cls, path: str, *, version: str = facts.CLIENT_VERSION,
               channel: str = facts.DEFAULT_UPDATE_CHANNEL,
               os_name: str = facts.PLATFORM, arch: str | None = None,
               install_id: str | None = None, now=utcnow) -> "Store":
        """建库（幂等）：已存在直接打开并建缺失表。"""
        directory = os.path.dirname(os.path.abspath(path)) or "."
        os.makedirs(directory, mode=0o700, exist_ok=True)
        connection = sqlite3.connect(path, timeout=10.0, check_same_thread=False)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            version_before = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version_before > SCHEMA_VERSION:
                raise StoreDowngradeError(
                    "本地状态库版本高于本客户端：" + str(version_before) + " > "
                    + str(SCHEMA_VERSION))
            with connection:
                for statement in _SCHEMA_SQL:
                    connection.execute(statement)
                if version_before < 2 and not _has_column(connection, "receipts",
                                                           "stale"):
                    connection.execute("ALTER TABLE receipts ADD COLUMN stale INTEGER"
                                       " NOT NULL DEFAULT 0")
                connection.execute("PRAGMA user_version=" + str(SCHEMA_VERSION))
                exists = connection.execute(
                    "SELECT COUNT(*) FROM install").fetchone()[0]
                if not exists:
                    install_id = install_id or new_install_id()
                    bucket = bucket_for(install_id)
                    connection.execute(
                        "INSERT INTO install (singleton, install_id_hash, version, channel,"
                        " bucket, os, arch, installed_at) VALUES (1,?,?,?,?,?,?,?)",
                        (sha256_hex(install_id), version, channel, bucket, os_name,
                         arch or _arch(), iso(now())))
                profile_exists = connection.execute(
                    "SELECT COUNT(*) FROM profile").fetchone()[0]
                if not profile_exists:
                    connection.execute(
                        "INSERT INTO profile (singleton, preset, daily_tokens, weekly_tokens,"
                        " monthly_tokens, concurrent, schedule, default_tier, cpu_pct, gpu_pct,"
                        " mem_pct, yield_enabled) VALUES (1,?,?,?,?,?,?,?,?,?,?,?)",
                        (facts.DEFAULT_PRESET, facts.DEFAULT_DAILY_TOKENS, 0, 0,
                         facts.DEFAULT_CONCURRENT, "[]", "realtime", facts.DEFAULT_CPU_PCT,
                         facts.DEFAULT_GPU_PCT, facts.DEFAULT_MEM_PCT, 1))
        finally:
            connection.close()
        try:
            os.chmod(path, 0o600)
        except OSError:                                        # pragma: no cover
            pass
        return cls(path, now=now)

    def is_initialized(self) -> bool:
        try:
            row = self.connect().execute("SELECT COUNT(*) FROM install").fetchone()
        except StoreNotInitializedError:
            return False
        return bool(row and row[0])

    # ─────────────────────────────────────────────────────── install
    def install(self) -> InstallRecord:
        row = self.connect().execute("SELECT * FROM install WHERE singleton = 1").fetchone()
        if row is None:
            raise StoreNotInitializedError("安装记录缺失：先执行 djb init")
        return InstallRecord(
            install_id_hash=row["install_id_hash"], version=row["version"],
            channel=row["channel"], bucket=int(row["bucket"]), os=row["os"],
            arch=row["arch"], installed_at=row["installed_at"])

    def update_install(self, **fields: Any) -> InstallRecord:
        allowed = {"version", "channel", "bucket", "os", "arch"}
        pairs = {key: value for key, value in fields.items() if key in allowed}
        if pairs:
            assignments = ", ".join(key + " = ?" for key in pairs)
            with self._conn() as connection:
                connection.execute("UPDATE install SET " + assignments + " WHERE singleton = 1",
                                   tuple(pairs.values()))
        return self.install()

    # ─────────────────────────────────────────────────────── profile
    def profile(self) -> ProfileRecord:
        row = self.connect().execute("SELECT * FROM profile WHERE singleton = 1").fetchone()
        if row is None:
            return ProfileRecord()
        try:
            schedule = json.loads(row["schedule"] or "[]")
        except ValueError:
            schedule = []
        if not isinstance(schedule, list):
            schedule = []
        return ProfileRecord(
            preset=row["preset"], daily_tokens=int(row["daily_tokens"]),
            weekly_tokens=int(row["weekly_tokens"]), monthly_tokens=int(row["monthly_tokens"]),
            concurrent=int(row["concurrent"]), schedule=schedule,
            default_tier=row["default_tier"], cpu_pct=int(row["cpu_pct"]),
            gpu_pct=int(row["gpu_pct"]), mem_pct=int(row["mem_pct"]),
            yield_enabled=bool(row["yield_enabled"]))

    def save_profile(self, profile: ProfileRecord) -> ProfileRecord:
        with self._conn() as connection:
            connection.execute(
                "INSERT INTO profile (singleton, preset, daily_tokens, weekly_tokens,"
                " monthly_tokens, concurrent, schedule, default_tier, cpu_pct, gpu_pct,"
                " mem_pct, yield_enabled) VALUES (1,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(singleton) DO UPDATE SET preset=excluded.preset,"
                " daily_tokens=excluded.daily_tokens, weekly_tokens=excluded.weekly_tokens,"
                " monthly_tokens=excluded.monthly_tokens, concurrent=excluded.concurrent,"
                " schedule=excluded.schedule, default_tier=excluded.default_tier,"
                " cpu_pct=excluded.cpu_pct, gpu_pct=excluded.gpu_pct,"
                " mem_pct=excluded.mem_pct, yield_enabled=excluded.yield_enabled",
                (profile.preset, profile.daily_tokens, profile.weekly_tokens,
                 profile.monthly_tokens, profile.concurrent,
                 json.dumps(profile.schedule, ensure_ascii=False), profile.default_tier,
                 profile.cpu_pct, profile.gpu_pct, profile.mem_pct,
                 1 if profile.yield_enabled else 0))
        return self.profile()

    # ─────────────────────────────────────────────────────── upstreams
    def add_upstream(self, *, upstream_id: str, access_type: str, base_url: str,
                     models: list[dict[str, Any]], credential_ref: str | None,
                     protocol: str = facts.DEFAULT_UPSTREAM_PROTOCOL,
                     upstream_kind: str | None = None,
                     state: str = "declared", created_at: str | None = None) -> UpstreamRecord:
        moment = created_at or iso(self._now())
        with self._conn() as connection:
            connection.execute(
                "INSERT INTO upstreams (id, access_type, base_url, models, credential_ref,"
                " state, last_probe_at, protocol, upstream_kind, created_at, enabled)"
                " VALUES (?,?,?,?,?,?,NULL,?,?,?,1)",
                (upstream_id, access_type, base_url,
                 json.dumps(models, ensure_ascii=False), credential_ref, state, protocol,
                 upstream_kind, moment))
        return self.upstream(upstream_id)

    def upstream(self, upstream_id: str) -> UpstreamRecord:
        row = self.connect().execute(
            "SELECT * FROM upstreams WHERE id = ?", (upstream_id,)).fetchone()
        if row is None:
            from .errors import UsageError
            raise UsageError("上游不存在：" + upstream_id)
        return _row_to_upstream(row)

    def upstreams(self) -> list[UpstreamRecord]:
        rows = self.connect().execute(
            "SELECT * FROM upstreams ORDER BY created_at, id").fetchall()
        return [_row_to_upstream(row) for row in rows]

    def update_upstream(self, upstream_id: str, **fields: Any) -> UpstreamRecord:
        allowed = {"access_type", "base_url", "credential_ref", "state",
                   "last_probe_at", "protocol", "enabled"}
        pairs: dict[str, Any] = {}
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key == "models":
                continue
            if key == "enabled":
                pairs[key] = 1 if value else 0
            else:
                pairs[key] = value
        if "models" in fields:
            pairs["models"] = json.dumps(fields["models"], ensure_ascii=False)
        if pairs:
            assignments = ", ".join(key + " = ?" for key in pairs)
            with self._conn() as connection:
                connection.execute(
                    "UPDATE upstreams SET " + assignments + " WHERE id = ?",
                    tuple(pairs.values()) + (upstream_id,))
        return self.upstream(upstream_id)

    def delete_upstream(self, upstream_id: str) -> bool:
        with self._conn() as connection:
            cursor = connection.execute("DELETE FROM upstreams WHERE id = ?", (upstream_id,))
        return bool(cursor.rowcount)

    def set_model_enabled(self, upstream_id: str, model_id: str, enabled: bool) -> UpstreamRecord:
        record = self.upstream(upstream_id)
        models = []
        found = False
        for item in record.models:
            item = dict(item)
            if str(item.get("id")) == model_id:
                item["enabled"] = bool(enabled)
                found = True
            models.append(item)
        if not found:
            from .errors import UsageError
            raise UsageError("模型不在该上游的清单里：" + model_id)
        return self.update_upstream(upstream_id, models=models)

    def set_model_price(self, upstream_id: str, model_id: str, min_price: str) -> UpstreamRecord:
        record = self.upstream(upstream_id)
        models = []
        found = False
        for item in record.models:
            item = dict(item)
            if str(item.get("id")) == model_id:
                item["min_price"] = str(min_price)
                item["min_price_unit"] = facts.DEFAULT_MIN_PRICE_UNIT
                found = True
            models.append(item)
        if not found:
            from .errors import UsageError
            raise UsageError("模型不在该上游的清单里：" + model_id)
        return self.update_upstream(upstream_id, models=models)

    # ─────────────────────────────────────────────────────── policy
    def save_policy(self, record: PolicyRecord) -> PolicyRecord:
        with self._conn() as connection:
            connection.execute(
                "INSERT INTO policy (singleton, policy_version, protocol_version, digest,"
                " issued_at, expires_at, quotas, directives, settings, verify_state, fetched_at)"
                " VALUES (1,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(singleton) DO UPDATE SET policy_version=excluded.policy_version,"
                " protocol_version=excluded.protocol_version, digest=excluded.digest,"
                " issued_at=excluded.issued_at, expires_at=excluded.expires_at,"
                " quotas=excluded.quotas, directives=excluded.directives,"
                " settings=excluded.settings, verify_state=excluded.verify_state,"
                " fetched_at=excluded.fetched_at",
                (record.policy_version, record.protocol_version, record.digest,
                 record.issued_at, record.expires_at,
                 json.dumps(record.quotas, ensure_ascii=False),
                 json.dumps(record.directives, ensure_ascii=False),
                 json.dumps(record.settings, ensure_ascii=False),
                 record.verify_state, record.fetched_at))
        return self.policy_or_raise()

    def policy(self) -> PolicyRecord | None:
        row = self.connect().execute(
            "SELECT * FROM policy WHERE singleton = 1").fetchone()
        return _row_to_policy(row) if row is not None else None

    def policy_or_raise(self) -> PolicyRecord:
        record = self.policy()
        if record is None:
            from .errors import PolicyError
            raise PolicyError("策略快照不存在：等待服务端下发（当前为保守模式）")
        return record

    def clear_policy(self) -> None:
        with self._conn() as connection:
            connection.execute("DELETE FROM policy WHERE singleton = 1")

    # ─────────────────────────────────────────────────────── announcements
    def record_announcement(self, seq: int, entry: dict[str, Any]) -> bool:
        """按 seq 幂等（重复心跳 / 重放不产生重复公告）。"""
        with self._conn() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO announcements (seq, kind, access, model, max_qps,"
                " ask_price, unit, resource_level, expires_at, raw, recorded_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (int(seq), entry.get("kind"), entry.get("access"), entry.get("model"),
                 entry.get("max_qps"), entry.get("ask_price"), entry.get("unit"),
                 entry.get("resource_level"), entry.get("expires_at"),
                 json.dumps(entry, ensure_ascii=False), iso(self._now())))
        return bool(cursor.rowcount)

    def announcements(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.connect().execute(
            "SELECT * FROM announcements ORDER BY seq DESC LIMIT ?", (int(limit),)).fetchall()
        return [dict(row) for row in rows]

    # ─────────────────────────────────────────────────────── receipts
    def add_receipt(self, receipt_id: str, sealed: str, *, buffered_at: str | None = None,
                    attempts: int = 0, next_attempt_at: str | None = None) -> bool:
        with self._conn() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO receipts (receipt_id, sealed, buffered_at, attempts,"
                " next_attempt_at, last_error) VALUES (?,?,?,?,?,NULL)",
                (receipt_id, sealed, buffered_at or iso(self._now()), int(attempts),
                 next_attempt_at))
        return bool(cursor.rowcount)

    def receipts(self, limit: int = 500) -> list[ReceiptRow]:
        rows = self.connect().execute(
            "SELECT * FROM receipts ORDER BY buffered_at, receipt_id LIMIT ?",
            (int(limit),)).fetchall()
        return [_row_to_receipt(row) for row in rows]

    def pending_receipts(self, now: datetime | None = None, limit: int = 500) -> list[ReceiptRow]:
        moment = iso(now or self._now())
        rows = self.connect().execute(
            "SELECT * FROM receipts WHERE next_attempt_at IS NULL OR next_attempt_at <= ?"
            " ORDER BY buffered_at, receipt_id LIMIT ?", (moment, int(limit))).fetchall()
        return [_row_to_receipt(row) for row in rows]

    def delete_receipt(self, receipt_id: str) -> bool:
        with self._conn() as connection:
            cursor = connection.execute("DELETE FROM receipts WHERE receipt_id = ?",
                                        (receipt_id,))
        return bool(cursor.rowcount)

    def mark_receipt_retry(self, receipt_id: str, *, attempts: int,
                           next_attempt_at: str | None, last_error: str | None) -> None:
        with self._conn() as connection:
            connection.execute(
                "UPDATE receipts SET attempts = ?, next_attempt_at = ?, last_error = ?"
                " WHERE receipt_id = ?",
                (int(attempts), next_attempt_at, (last_error or "")[:200], receipt_id))

    def receipt_count(self) -> int:
        return int(self.connect().execute("SELECT COUNT(*) FROM receipts").fetchone()[0])

    def oldest_receipt_at(self) -> str | None:
        row = self.connect().execute(
            "SELECT MIN(buffered_at) FROM receipts").fetchone()
        return row[0] if row and row[0] else None

    def stale_receipt_count(self) -> int:
        return int(self.connect().execute(
            "SELECT COUNT(*) FROM receipts WHERE stale = 1").fetchone()[0])

    def mark_receipts_stale(self, cutoff: datetime) -> list[str]:
        """超 24 h 的收据**不丢弃**，标 stale 并计入诊断（Lead 2026-10-24 裁决 CR-2）。"""
        limit = iso(cutoff)
        rows = self.connect().execute(
            "SELECT receipt_id FROM receipts WHERE buffered_at < ? AND stale = 0",
            (limit,)).fetchall()
        ids = [str(row[0]) for row in rows]
        if ids:
            with self._conn() as connection:
                connection.executemany(
                    "UPDATE receipts SET stale = 1 WHERE receipt_id = ?",
                    [(item,) for item in ids])
        return ids

    def purge_receipts_older_than(self, cutoff: datetime) -> list[str]:
        """丢弃超期收据（INV-07：>24 h 丢弃并停接单）；返回被丢弃的 receipt_id。"""
        limit = iso(cutoff)
        rows = self.connect().execute(
            "SELECT receipt_id FROM receipts WHERE buffered_at < ?", (limit,)).fetchall()
        ids = [str(row[0]) for row in rows]
        if ids:
            with self._conn() as connection:
                connection.executemany("DELETE FROM receipts WHERE receipt_id = ?",
                                       [(item,) for item in ids])
        return ids

    # ─────────────────────────────────────────────────────── 用量窗口
    def add_usage(self, window_key: str, window_start: str, tokens: int,
                  requests: int = 1) -> None:
        with self._conn() as connection:
            connection.execute(
                "INSERT INTO usage_windows (window_key, window_start, tokens, requests)"
                " VALUES (?,?,?,?) ON CONFLICT(window_key, window_start) DO UPDATE SET"
                " tokens = tokens + excluded.tokens, requests = requests + excluded.requests",
                (window_key, window_start, int(tokens), int(requests)))

    def usage_total(self, window_key: str, *, since: str | None = None) -> int:
        if since:
            row = self.connect().execute(
                "SELECT COALESCE(SUM(tokens), 0) FROM usage_windows"
                " WHERE window_key = ? AND window_start >= ?", (window_key, since)).fetchone()
        else:
            row = self.connect().execute(
                "SELECT COALESCE(SUM(tokens), 0) FROM usage_windows WHERE window_key = ?",
                (window_key,)).fetchone()
        return int(row[0] or 0)

    def usage_window(self, window_key: str, window_start: str) -> tuple[int, int]:
        row = self.connect().execute(
            "SELECT tokens, requests FROM usage_windows WHERE window_key = ? AND window_start = ?",
            (window_key, window_start)).fetchone()
        if row is None:
            return 0, 0
        return int(row["tokens"]), int(row["requests"])

    def prune_usage(self, older_than: str) -> int:
        with self._conn() as connection:
            cursor = connection.execute(
                "DELETE FROM usage_windows WHERE window_start < ?", (older_than,))
        return int(cursor.rowcount or 0)

    # ─────────────────────────────────────────────────────── 更新状态
    def update_state(self) -> UpdateRecord:
        row = self.connect().execute(
            "SELECT * FROM update_state WHERE singleton = 1").fetchone()
        if row is None:
            return UpdateRecord()
        return UpdateRecord(
            current_version=row["current_version"], next_version=row["next_version"],
            bucket=int(row["bucket"] or 0), rollback_to=row["rollback_to"],
            verify_state=row["verify_state"] or "unchecked",
            channel=row["channel"] or facts.DEFAULT_UPDATE_CHANNEL,
            checked_at=row["checked_at"])

    def save_update_state(self, record: UpdateRecord) -> UpdateRecord:
        with self._conn() as connection:
            connection.execute(
                "INSERT INTO update_state (singleton, current_version, next_version, bucket,"
                " rollback_to, verify_state, channel, checked_at) VALUES (1,?,?,?,?,?,?,?)"
                " ON CONFLICT(singleton) DO UPDATE SET current_version=excluded.current_version,"
                " next_version=excluded.next_version, bucket=excluded.bucket,"
                " rollback_to=excluded.rollback_to, verify_state=excluded.verify_state,"
                " channel=excluded.channel, checked_at=excluded.checked_at",
                (record.current_version, record.next_version, int(record.bucket),
                 record.rollback_to, record.verify_state, record.channel, record.checked_at))
        return self.update_state()

    # ─────────────────────────────────────────────────────── 环境检测授权
    def env_state(self) -> dict[str, Any]:
        row = self.connect().execute(
            "SELECT * FROM env_state WHERE singleton = 1").fetchone()
        if row is None:
            return {}
        return {
            "detected_at": row["detected_at"],
            "authorized_at": row["authorized_at"],
            "plan": json.loads(row["plan"] or "[]"),
            "report": json.loads(row["report"] or "{}"),
        }

    def save_env_state(self, *, detected_at: str | None, authorized_at: str | None,
                       plan: list[dict[str, Any]], report: dict[str, Any]) -> None:
        with self._conn() as connection:
            connection.execute(
                "INSERT INTO env_state (singleton, detected_at, authorized_at, plan, report)"
                " VALUES (1,?,?,?,?) ON CONFLICT(singleton) DO UPDATE SET"
                " detected_at=excluded.detected_at, authorized_at=excluded.authorized_at,"
                " plan=excluded.plan, report=excluded.report",
                (detected_at, authorized_at,
                 json.dumps(plan, ensure_ascii=False), json.dumps(report, ensure_ascii=False)))

    # ─────────────────────────────────────────────────────── kv
    def get_kv(self, key: str, default: Any = None) -> Any:
        row = self.connect().execute(
            "SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except ValueError:
            return default

    def set_kv(self, key: str, value: Any) -> None:
        with self._conn() as connection:
            connection.execute(
                "INSERT INTO kv (key, value) VALUES (?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, ensure_ascii=False)))

    def delete_kv(self, key: str) -> None:
        with self._conn() as connection:
            connection.execute("DELETE FROM kv WHERE key = ?", (key,))

    # ─────────────────────────────────────────────────────── 诊断
    def counts(self) -> dict[str, int]:
        connection = self.connect()
        return {
            "upstreams": int(connection.execute(
                "SELECT COUNT(*) FROM upstreams").fetchone()[0]),
            "receipts": int(connection.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]),
            "announcements": int(connection.execute(
                "SELECT COUNT(*) FROM announcements").fetchone()[0]),
            "policy_snapshots": int(connection.execute(
                "SELECT COUNT(*) FROM policy").fetchone()[0]),
        }


def _has_column(connection: sqlite3.Connection, table: str, column: str) -> bool:
    try:
        rows = connection.execute("PRAGMA table_info(" + table + ")").fetchall()
    except sqlite3.Error:                                  # pragma: no cover - 防御
        return False
    return any(str(row[1]) == column for row in rows)


def bucket_for(install_id: str) -> int:
    """确定性分桶：sha256(install_id) 前 8 位十六进制 mod 100（0–99）。"""
    return int(sha256_hex(install_id)[:8], 16) % 100


def _row_to_upstream(row: sqlite3.Row) -> UpstreamRecord:
    try:
        models = json.loads(row["models"] or "[]")
    except ValueError:
        models = []
    if not isinstance(models, list):
        models = []
    return UpstreamRecord(
        id=row["id"], access_type=row["access_type"], base_url=row["base_url"],
        models=models, credential_ref=row["credential_ref"], state=row["state"],
        last_probe_at=row["last_probe_at"], protocol=row["protocol"] or "",
        created_at=row["created_at"], enabled=bool(row["enabled"]),
        upstream_kind=(row["upstream_kind"] if "upstream_kind" in row.keys() else None))


def _row_to_policy(row: sqlite3.Row) -> PolicyRecord:
    def _load(text: Any, fallback: Any) -> Any:
        try:
            value = json.loads(text or "")
        except ValueError:
            return fallback
        return value if isinstance(value, type(fallback)) else fallback

    return PolicyRecord(
        policy_version=row["policy_version"], protocol_version=row["protocol_version"],
        digest=row["digest"], issued_at=row["issued_at"], expires_at=row["expires_at"],
        quotas=_load(row["quotas"], []), directives=_load(row["directives"], []),
        settings=_load(row["settings"], {}), verify_state=row["verify_state"],
        fetched_at=row["fetched_at"])


def _row_to_receipt(row: sqlite3.Row) -> ReceiptRow:
    return ReceiptRow(receipt_id=row["receipt_id"], sealed=row["sealed"],
                      buffered_at=row["buffered_at"], attempts=int(row["attempts"]),
                      next_attempt_at=row["next_attempt_at"],
                      last_error=row["last_error"],
                      stale=bool(row["stale"]) if "stale" in row.keys() else False)


def _arch() -> str:
    import platform
    return platform.machine() or "unknown"


def utc_window_start(window_seconds: int, now: datetime | None = None) -> datetime:
    """按 window_seconds 对齐到 UTC 窗口起点（配额窗口唯一口径）。"""
    moment = (now or utcnow()).astimezone(timezone.utc)
    epoch = int(moment.timestamp())
    start = (epoch // int(window_seconds)) * int(window_seconds)
    return datetime.fromtimestamp(start, tz=timezone.utc)


def window_key(name: str, seconds: int) -> str:
    return str(name) + ":" + str(int(seconds))


def next_window_start(window_seconds: int, now: datetime | None = None) -> datetime:
    return utc_window_start(window_seconds, now) + timedelta(seconds=int(window_seconds))
