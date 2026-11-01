# SPDX-License-Identifier: Apache-2.0
"""测试夹具：全部本机临时目录 + mock HTTP 服务端，无数据库、无外网。"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from djb_node.config import Paths
from djb_node.secure import CredentialStore
from djb_node.store import PolicyRecord, ProfileRecord, Store
from djb_node.util import iso


class Clock:
    """可推进的固定时钟（不 sleep）。"""

    def __init__(self, start: datetime | None = None) -> None:
        self.current = start or datetime(2026, 10, 24, 0, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> datetime:
        self.current = self.current + timedelta(seconds=seconds)
        return self.current


@pytest.fixture
def home(tmp_path, monkeypatch):
    target = tmp_path / "home"
    monkeypatch.setenv("DJB_HOME", str(target))
    return target


@pytest.fixture
def paths(home) -> Paths:
    item = Paths(str(home))
    item.prepare()
    return item


@pytest.fixture
def store(paths) -> Store:
    # 安装记录里的版本显式钉住：不要跟着 facts.CLIENT_VERSION 漂，
    # 否则「更新可用」类用例会因为包版本一变就变成 up_to_date（测的东西就变了）。
    item = Store.create(paths.db_file, version="0.1.0")
    yield item
    item.close()


@pytest.fixture
def credentials(paths) -> CredentialStore:
    return CredentialStore(paths)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def mock_server():
    from tests.mock_m02 import MockServer

    server = MockServer().start()
    yield server
    server.stop()


@pytest.fixture
def mock(mock_server):
    return mock_server


def make_policy(*, policy_version: str = "p-1", quotas=None, directives=None,
                issued: datetime | None = None, ttl_s: int = 3600,
                digest: str = "a" * 64, protocol_version: str = "1.0.0") -> PolicyRecord:
    issued = issued or datetime(2026, 10, 24, 0, 0, 0, tzinfo=timezone.utc)
    return PolicyRecord(
        policy_version=policy_version, protocol_version=protocol_version,
        digest=digest, issued_at=iso(issued),
        expires_at=iso(issued + timedelta(seconds=ttl_s)),
        quotas=list(quotas or []), directives=list(directives or []),
        settings={}, verify_state="digest_only", fetched_at=iso(issued))


def make_profile(**fields) -> ProfileRecord:
    profile = ProfileRecord()
    for key, value in fields.items():
        setattr(profile, key, value)
    return profile
