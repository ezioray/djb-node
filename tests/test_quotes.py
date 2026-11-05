# SPDX-License-Identifier: Apache-2.0
"""CR-20261106-OFFICIAL-SUPPLY-01 · 客户端报价发布（design/17 §15）。

按 CR §3 的六个坑位逐条落测：

* P1 0 价拒发（未定价 / 0 / 折算 0 分都不上行）；
* P2 `ttl_s == 2 × period_s` 硬校验（参数与配置都拒，配置非法时 daemon fail-closed）；
* P3 `upstream add --access B` 必须显式 `--kind`；
* P4 探针未验证 / 不新鲜（> 600 s）不报价；
* P6 容量公告与报价的 `unit` 逐字一致 = `token`；
* 批量发布：单行失败不拖垮整批；内容不变时服务端复用（`created=false` 不续期）。

全部离线：上行端点由 `tests/mock_m02.MockServer` 承接，不联网、不连数据库。
"""
from __future__ import annotations

import json
import time
from datetime import timedelta

import pytest

from djb_node import facts, protocol, quotes
from djb_node.api import NodeApi
from djb_node.cli import main
from djb_node.config import Paths, load_config, save_config
from djb_node.errors import UsageError
from djb_node.httpclient import HttpClient
from djb_node.secure import CredentialStore
from djb_node.store import Store
from djb_node.upstream import ModelEntry, UpstreamManager, capacity_unit
from djb_node.util import iso, utcnow
from tests.mock_m02 import CLIENT_KEY


# ─────────────────────────────────────────────────────────── 夹具 / 工具
@pytest.fixture
def manager(store, credentials) -> UpstreamManager:
    return UpstreamManager(store, credentials)


def _seed(store: Store, *, upstream_id: str = "u1", access: str = "B",
          models=(), state: str = "verified") -> None:
    """落库一条「已探针」的上游：每个 model 给 id / min_price / age_s 即可。"""
    moment = utcnow()
    entries = []
    for item in models:
        entries.append({
            "id": item["id"],
            "enabled": bool(item.get("enabled", True)),
            "min_price": item.get("min_price"),
            "min_price_unit": facts.DEFAULT_MIN_PRICE_UNIT,
            "inference_ok": item.get("inference_ok", True),
            "last_probe_at": iso(moment - timedelta(seconds=float(item.get("age_s", 0)))),
        })
    store.add_upstream(upstream_id=upstream_id, access_type=access,
                       base_url="https://agg.example.com/v1", models=entries,
                       credential_ref=None, state=state)


def _plan(manager: UpstreamManager, *, period_s=60):
    return quotes.plan_quotes(manager, now=utcnow(), period_s=period_s)


def _point_at_mock(paths: Paths, mock) -> None:
    config = load_config(paths)
    config.gateway_url = mock.base_url
    save_config(paths, config)


# ─────────────────────────────────────────────────────────── P6：unit 单一口径
def test_capacity_unit_is_token():
    assert facts.CAPACITY_UNIT == "token"
    assert capacity_unit() == "token"
    # 价格单位仍在（只服务本地成本估算），但**不再**是公告的 unit
    assert facts.DEFAULT_MIN_PRICE_UNIT != capacity_unit()


def test_capacity_announcement_uses_token_unit(store, manager):
    """P6 回归：容量公告的 `unit` 曾写成 `min_price_unit`（usd_per_1m_tokens）。"""
    _seed(store, models=[{"id": "gpt-4o", "min_price": "0.5"}])
    entries = manager.announce_capacity(store.profile())
    assert [entry["unit"] for entry in entries] == ["token"]
    assert entries[0]["ask_price"] == "0.5"            # 价格字符串原样，不改口径


def test_quote_rows_use_token_unit(store, manager):
    _seed(store, models=[{"id": "gpt-4o", "min_price": "0.75"}])
    plan = _plan(manager)
    assert [row.unit for row in plan.rows] == ["token"]
    assert plan.to_dict()["unit"] == "token"


# ─────────────────────────────────────────────────────────── P1：0 价拒发
def test_unpriced_and_zero_price_are_not_published(store, manager):
    _seed(store, models=[{"id": "no-price", "min_price": None},
                         {"id": "zero", "min_price": "0"},
                         {"id": "too-small", "min_price": "0.0049"},
                         {"id": "ok", "min_price": "0.75"}])
    plan = _plan(manager)
    assert [row.model for row in plan.rows] == ["ok"]
    assert plan.skipped[quotes.SKIP_ZERO_PRICE] == 3   # 未定价 / 0 / 折算 0 分


def test_price_to_minor_rounds_half_up():
    assert quotes.price_to_minor("0.005") == 1        # 半分进一（ROUND_HALF_UP）
    assert quotes.price_to_minor("0.0049") == 0
    assert quotes.price_to_minor("0.75") == 75
    assert quotes.price_to_minor("8") == 800
    assert quotes.price_to_minor("1.005") == 101     # 不经过 float，不丢精度
    for bad in (None, "", "abc", "-1", True, 0.75):
        assert quotes.price_to_minor(bad) is None


def test_quote_row_refuses_zero_price():
    with pytest.raises(UsageError):
        quotes.QuoteRow(upstream_id="u1", access="B", model="m", price_min=0,
                        ttl_s=120)


# ─────────────────────────────────────────────────────────── P2：TTL = 2 × 周期
def test_ttl_must_be_double_period(store, manager):
    with pytest.raises(UsageError) as info:
        quotes.validate_ttl(ttl_s=300, period_s=60)
    assert "2 × 发布周期" in info.value.message
    assert quotes.validate_ttl(ttl_s=120, period_s=60) == 120

    _seed(store, models=[{"id": "gpt-4o", "min_price": "0.75"}])
    plan = _plan(manager, period_s=90)
    assert plan.ttl_s == 180
    assert all(row.ttl_s == 180 for row in plan.rows)


def test_period_and_ttl_config_validation():
    assert quotes.resolve_period(None) == facts.QUOTES_PERIOD_DEFAULT_S
    assert quotes.resolve_period({"quotes.period_s": 90}) == 90
    assert quotes.resolve_period({"quotes": {"period_s": 30, "ttl_s": 60}}) == 30
    with pytest.raises(UsageError):
        quotes.resolve_period({"quotes.period_s": 60, "quotes.ttl_s": 300})
    with pytest.raises(UsageError):
        quotes.resolve_period(None, period_s=facts.QUOTES_PERIOD_MAX_S + 1)
    with pytest.raises(UsageError):
        quotes.resolve_period(None, period_s=0)


# ─────────────────────────────────────────────────────────── P4：探针门控
def test_stale_or_unverified_probe_is_not_published(store, manager):
    _seed(store, models=[
        {"id": "fresh", "min_price": "0.5", "age_s": facts.PROBE_FRESH_AFTER_S - 1},
        {"id": "stale", "min_price": "0.5", "age_s": facts.PROBE_FRESH_AFTER_S + 1},
        {"id": "never", "min_price": "0.5", "inference_ok": None},
        {"id": "failed", "min_price": "0.5", "inference_ok": False},
    ])
    plan = _plan(manager)
    assert [row.model for row in plan.rows] == ["fresh"]
    assert plan.skipped[quotes.SKIP_STALE] == 1
    assert plan.skipped[quotes.SKIP_NOT_PROBED] == 2


def test_disabled_model_and_gated_upstream_are_skipped(store, manager):
    _seed(store, upstream_id="u1",
          models=[{"id": "off", "min_price": "0.5", "enabled": False}])
    _seed(store, upstream_id="u2", models=[{"id": "rejected", "min_price": "0.5"}],
          state="rejected")
    plan = _plan(manager)
    assert plan.rows == []
    assert plan.skipped[quotes.SKIP_DISABLED] == 1
    assert plan.skipped[quotes.SKIP_UPSTREAM] == 1


def test_duplicate_model_keeps_cheapest(store, manager):
    """B 端多上游常有重叠模型：同一 (access, model, unit) 只发最便宜的一条。"""
    _seed(store, upstream_id="agg-a", models=[{"id": "gpt-4o", "min_price": "0.9"}])
    _seed(store, upstream_id="agg-b", models=[{"id": "gpt-4o", "min_price": "0.6"}])
    plan = _plan(manager)
    assert len(plan.rows) == 1
    assert plan.rows[0].upstream_id == "agg-b"
    assert plan.rows[0].price_min == 60
    assert plan.skipped[quotes.SKIP_DUPLICATE] == 1


# ─────────────────────────────────────────────────────────── 上行载荷 / 批量发布
def test_payload_matches_cloud_quote_request(store, manager):
    _seed(store, models=[{"id": "gpt-4o", "min_price": "0.75"}])
    row = _plan(manager).rows[0]
    body = row.payload()
    assert set(body) == {"access", "model", "unit", "price_min", "ttl_s", "currency"}
    assert body["price_min"] == 75 and isinstance(body["price_min"], int)
    assert body["ttl_s"] == 120 and body["unit"] == "token"
    assert body["currency"] == "USD"
    assert "node_id" not in body                    # 缺省不带（服务端按密钥取）
    assert row.payload(node_id="node-1")["node_id"] == "node-1"


def test_publish_counts_created_then_reused(mock, store, manager):
    _seed(store, models=[{"id": "gpt-4o", "min_price": "0.75"}])
    api = NodeApi(mock.base_url, client_key=CLIENT_KEY, http=HttpClient(timeout=10.0))
    plan = _plan(manager)
    first = quotes.publish(api, plan.rows)
    assert (first.created, first.reused, first.failed) == (1, 0, 0)
    second = quotes.publish(api, plan.rows)         # 内容不变 → 服务端复用（不续期）
    assert (second.created, second.reused, second.failed) == (0, 1, 0)
    assert len(mock.quotes) == 2
    assert mock.quotes[0]["unit"] == "token"
    assert mock.quotes[0]["price_min"] == 75
    assert mock.quotes[0]["ttl_s"] == 120


def test_single_row_failure_does_not_abort_batch(mock, store, manager):
    _seed(store, models=[{"id": "bad", "min_price": "0.5"},
                         {"id": "good-1", "min_price": "0.5"},
                         {"id": "good-2", "min_price": "0.5"}])
    mock.quote_fail_models = {"bad"}
    api = NodeApi(mock.base_url, client_key=CLIENT_KEY, http=HttpClient(timeout=10.0))
    result = quotes.publish(api, _plan(manager).rows)
    assert (result.created, result.failed) == (2, 1)
    assert result.errors[0]["model"] == "bad"
    assert result.errors[0]["error"] == "invalid"
    assert result.errors[0]["code"] == 40001
    assert result.to_dict()["error_classes"] == {"invalid": 1}


def test_publish_counts_auth_failure(mock, store, manager):
    _seed(store, models=[{"id": "gpt-4o", "min_price": "0.75"}])
    api = NodeApi(mock.base_url, client_key="ck_wrong", http=HttpClient(timeout=10.0))
    result = quotes.publish(api, _plan(manager).rows)
    assert (result.created, result.failed) == (0, 1)
    assert result.errors[0]["error"] == "auth"


def test_protocol_announces_quotes():
    assert protocol.PATHS["quotes"] == "/api/v1/schedule/quotes"
    assert protocol.CLIENT_CAPABILITIES[-1] == "quotes"


# ─────────────────────────────────────────────────────────── daemon 报价环
def test_daemon_does_not_start_quotes_when_flag_off(paths, store, credentials, mock,
                                                    clock, windows_control_usable):
    from tests.test_daemon import _daemon

    _seed(store, models=[{"id": "gpt-4o", "min_price": "0.75"}])
    daemon = _daemon(paths, store, credentials, mock, clock=clock)
    daemon.start()
    try:
        assert daemon.status()["quotes"]["enabled"] is False
        assert daemon.status()["quotes"]["running"] is False
        assert not [item for item in mock.requests
                    if item["path"] == "/api/v1/schedule/quotes"]
    finally:
        daemon.stop()


def test_daemon_publishes_when_flag_on(paths, store, credentials, mock, clock,
                                       windows_control_usable):
    from djb_node.config import ClientConfig
    from tests.test_daemon import _daemon

    config = ClientConfig(gateway_url=mock.base_url, mirror_port=1717,
                          extra={"feature_flags": {facts.QUOTES_FLAG: True},
                                 "quotes.period_s": 60})
    daemon = _daemon(paths, store, credentials, mock, clock=clock, config=config)
    daemon.start()
    try:
        assert daemon.status()["quotes"]["enabled"] is True
        assert daemon.status()["quotes"]["running"] is True
        # 线程启动后立刻发一轮（此时还没有可报价模型 → 只记计数，不上行）
        deadline = time.time() + 3.0
        while time.time() < deadline and not daemon._last_quotes:
            time.sleep(0.02)
        assert daemon._last_quotes["rows"] == 0
        assert daemon._last_quotes["reason"] == "periodic"
        # 启动探针之后再加可报价模型（否则会被启动探针判 rejected）
        _seed(store, models=[{"id": "gpt-4o", "min_price": "0.75"},
                             {"id": "gpt-4o-mini", "min_price": "0.15"}])
        summary = daemon._publish_quotes(60, reason="manual")
        assert summary["rows"] == 2 and summary["created"] == 2
        assert summary["ttl_s"] == 120
        # 内容未变再发一轮 → 全部复用（不升版本、不续期）
        again = daemon._publish_quotes(60, reason="manual")
        assert again["rows"] == 2 and again["reused"] == 2 and again["created"] == 0
        assert len(mock.quotes) == 4
    finally:
        daemon.stop()
    assert daemon.status()["quotes"]["running"] is False


def test_daemon_fail_closed_on_bad_ttl_config(paths, store, credentials, mock, clock,
                                             windows_control_usable):
    from djb_node.config import ClientConfig
    from tests.test_daemon import _daemon

    _seed(store, models=[{"id": "gpt-4o", "min_price": "0.75"}])
    config = ClientConfig(gateway_url=mock.base_url, mirror_port=1717,
                          extra={"feature_flags": {facts.QUOTES_FLAG: True},
                                 "quotes.period_s": 60, "quotes.ttl_s": 300})
    daemon = _daemon(paths, store, credentials, mock, clock=clock, config=config)
    # 配置非法：线程照起（开关状态不撒谎），但每轮 fail-closed、一个字节都不上行
    daemon._start_quotes()
    try:
        assert daemon._quotes_thread is not None
        assert daemon._quotes_period() is None
        with pytest.raises(UsageError):
            daemon._publish_quotes(60)
        assert mock.quotes == []
        assert daemon.status()["quotes"]["ttl_s"] == 120    # 上报实际生效的默认值
    finally:
        daemon._stop_quotes()


# ─────────────────────────────────────────────────────────── CLI
@pytest.fixture
def ready(home):
    assert main(["init", "--no-env"]) == 0
    return Paths(str(home))


def _json_out(capsys) -> dict:
    return json.loads(capsys.readouterr().out.strip())


def test_cli_quotes_show_is_offline(ready, store, capsys):
    _seed(store, models=[{"id": "gpt-4o", "min_price": "0.75"},
                         {"id": "free", "min_price": "0"}])
    assert main(["quotes", "show", "--json"]) == 0
    payload = _json_out(capsys)
    assert payload["period_s"] == 60 and payload["ttl_s"] == 120
    assert payload["unit"] == "token"
    assert [row["model"] for row in payload["rows"]] == ["gpt-4o"]
    assert payload["skipped"] == {quotes.SKIP_ZERO_PRICE: 1}


def test_cli_quotes_publish_dry_run_sends_nothing(ready, store, mock, capsys):
    _seed(store, models=[{"id": "gpt-4o", "min_price": "0.75"}])
    _point_at_mock(ready, mock)
    assert main(["quotes", "publish", "--dry-run", "--json"]) == 0
    payload = _json_out(capsys)
    assert payload["dry_run"] is True
    assert payload["requests"][0]["unit"] == "token"
    assert payload["requests"][0]["ttl_s"] == 120
    assert mock.quotes == []                        # 只打印，没有上行


def test_cli_quotes_publish_requires_flag(ready, store, capsys):
    _seed(store, models=[{"id": "gpt-4o", "min_price": "0.75"}])
    assert main(["quotes", "publish"]) == facts.EXIT_USAGE
    assert facts.QUOTES_FLAG in capsys.readouterr().err


def test_cli_quotes_publish_when_enabled(ready, store, credentials, mock, capsys):
    _seed(store, models=[{"id": "gpt-4o", "min_price": "0.75"}])
    assert main(["flags", "--enable", facts.QUOTES_FLAG]) == 0
    _point_at_mock(ready, mock)
    credentials.set_node("node-1", CLIENT_KEY)
    capsys.readouterr()                             # 丢掉 flags 的输出
    assert main(["quotes", "publish", "--json"]) == 0
    payload = _json_out(capsys)
    assert payload["result"]["created"] == 1
    assert mock.quotes[0]["model"] == "gpt-4o"
    assert mock.quotes[0]["node_id"] == "node-1"
    assert mock.quotes[0]["unit"] == "token"


def test_cli_upstream_add_b_requires_explicit_kind(ready, capsys):
    code = main(["upstream", "add", "--access", "B", "--base-url",
                 "https://agg.example.com/v1", "--models", "gpt-4o"])
    assert code == facts.EXIT_USAGE
    assert "--kind" in capsys.readouterr().err


def test_cli_upstream_add_b_with_kind_is_accepted(ready, store, mock, capsys):
    _point_at_mock(ready, mock)                     # 申报走本机 mock，不碰外网
    code = main(["upstream", "add", "--access", "B", "--kind", "enterprise_gateway",
                 "--base-url", "https://agg.example.com/v1", "--models", "gpt-4o",
                 "--json"])
    assert code == 0
    payload = _json_out(capsys)
    assert payload["upstream"]["access_type"] == "B"
    assert store.upstreams()[0].upstream_kind == "enterprise_gateway"


def test_model_entry_defaults_stay_backward_compatible():
    """只加不改：旧 model JSON（无探针字段）仍能解析，且不影响报价门控。"""
    entry = ModelEntry.from_dict({"id": "legacy", "min_price": "0.5"})
    assert entry.inference_ok is None and entry.last_probe_at is None
    assert entry.to_dict() == {"id": "legacy", "enabled": True, "min_price": "0.5",
                               "min_price_unit": facts.DEFAULT_MIN_PRICE_UNIT}
