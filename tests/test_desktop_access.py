# SPDX-License-Identifier: Apache-2.0
"""接入配置回归：掩码、地址/URL 校验、原子落盘 0600、回读一致、reload 不谎报。

安全断言集中在两处：**磁盘 0600** 和 **任何展示/上报文本都不含明文秘密**。
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from djb_node.desktop import access as ac

SECRET = "djb_sk_live_9f2c41aa77be"
NODE_SECRET = "djb_ns_live_4e11bc23"
ADDRESS = "TQn9Y2khhvnJv8rKdT9jXb1sVJg8J3f7dQ"


# ─────────────────────────────────────────────────────────────── 掩码 / 校验
@pytest.mark.parametrize("value,expected", [
    (SECRET, "djb_…77be"),       # 前缀段 + 省略号 + 末 4 位
    (NODE_SECRET, "djb_…bc23"),
    ("abc", "…bc"),              # 短值只留末尾两位
    ("k", "…"),
    ("", "—"),
])
def test_mask_secret_never_returns_full_value(value, expected):
    masked = ac.mask_secret(value)
    assert masked == expected
    if len(value) > 8:
        assert value not in masked


def test_mask_address_keeps_head_and_tail():
    masked = ac.mask_address(ADDRESS)
    assert masked.startswith(ADDRESS[:6]) and masked.endswith(ADDRESS[-4:])
    assert ADDRESS not in masked


@pytest.mark.parametrize("address", [ADDRESS, "0x" + "a1" * 20])
def test_validate_address_accepts(address):
    assert ac.validate_address(address) == address


def test_validate_address_blank_means_unset():
    assert ac.validate_address("  ") == ""


@pytest.mark.parametrize("address", [
    "TQn9Y2khhvnJv8rKdT9jXb1sVJg8J3f7d",       # 少一位
    "0x1234",
    "bc1qxyz",
    "TQn9Y2khhvnJv8rKdT9jXb1sVJg8J3f7dQ!",
])
def test_validate_address_rejects(address):
    with pytest.raises(ValueError):
        ac.validate_address(address)


def test_validate_url_normalises_trailing_slash():
    assert ac.validate_url("http://127.0.0.1:1717/v1/", label="地址") == \
        "http://127.0.0.1:1717/v1"
    assert ac.validate_url("", label="地址") == ""


@pytest.mark.parametrize("url", ["ftp://x", "127.0.0.1:1717", "http://", "http://a b"])
def test_validate_url_rejects(url):
    with pytest.raises(ValueError):
        ac.validate_url(url, label="地址")


# ─────────────────────────────────────────────────────────────── 模型
def test_config_validate_names_the_bad_field():
    with pytest.raises(ValueError) as info:
        ac.AccessConfig(usdt_address="not-an-address").validate()
    assert "USDT" in str(info.value)
    with pytest.raises(ValueError) as info:
        ac.AccessConfig(openai_base_url="nope").validate()
    assert "OpenAI" in str(info.value)
    with pytest.raises(ValueError) as info:
        ac.AccessConfig(price_mode="跟随平台建议价").validate()
    assert "价格模式" in str(info.value)
    ac.AccessConfig(node_id="n", api_key=SECRET).validate()      # 合规配置不抛


def test_config_display_masks_every_secret():
    config = ac.AccessConfig(node_id="node-1", node_secret=NODE_SECRET, api_key=SECRET,
                             scoped_token=SECRET, usdt_address=ADDRESS,
                             openai_base_url="http://127.0.0.1:1717/v1")
    display = config.display()
    blob = json.dumps(display, ensure_ascii=False)
    assert SECRET not in blob and NODE_SECRET not in blob and ADDRESS not in blob
    assert display["node_id"] == "node-1"
    assert display["openai_base_url"] == "http://127.0.0.1:1717/v1"


def test_config_roundtrip_wire_keeps_values():
    config = ac.AccessConfig(node_id="n", api_key=SECRET, price_mode="manual",
                             price_note="按 token 计", usdt_address=ADDRESS)
    again = ac.AccessConfig.from_wire(config.to_wire())
    assert again.api_key == SECRET and again.price_mode == "manual"
    assert again.price_note == "按 token 计"


def test_config_from_wire_tolerates_junk():
    """脏文件不阻塞打开设置页：原值照读、未知键进 extra、合规性交给保存时校验。"""
    again = ac.AccessConfig.from_wire({"unknown": 1, "api_key": None, "price_mode": "??"})
    assert again.api_key == ""
    assert again.price_mode == "??"                     # 不猜用户意思
    assert again.extra["unknown"] == 1
    with pytest.raises(ValueError):
        again.validate()                                # 保存时才拦，并给明确原因


def test_price_mode_display_separates_raw_and_label():
    """编辑框给原值（auto/manual），中文标签单独给 hint；写回不会存中文。"""
    config = ac.AccessConfig(price_mode="manual")
    display = config.display()
    assert display["price_mode"] == "manual"
    assert display["price_mode_label"] == ac.PRICE_MODES["manual"]


# ─────────────────────────────────────────────────────────────── 落盘
def test_file_store_writes_private_file_atomically(tmp_path):
    path = tmp_path / "desktop" / "access-config.json"
    store = ac.FileAccessStore(path)
    saved = store.save(ac.AccessConfig(api_key=SECRET))
    assert isinstance(saved, ac.AccessConfig) and saved.api_key == SECRET
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600, oct(mode)
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
    # 目录里不留临时文件
    assert [p.name for p in path.parent.iterdir()] == ["access-config.json"]
    assert json.loads(path.read_text(encoding="utf-8"))["api_key"] == SECRET


def test_file_store_load_missing_returns_default(tmp_path):
    config = ac.FileAccessStore(tmp_path / "desktop" / "access-config.json").load()
    assert config == ac.AccessConfig()


def test_file_store_load_corrupt_is_tolerated(tmp_path):
    path = tmp_path / "desktop" / "access-config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        ac.FileAccessStore(path).load()          # 坏文件明确报错，不静默当成空配置
    # 用户在设置页重新保存一次即可自愈：写入新文件并回读一致
    result = ac.AccessService(ac.FileAccessStore(path)).save(ac.AccessConfig(api_key=SECRET))
    assert result.saved is True and result.readback_ok is True
    assert json.loads(path.read_text(encoding="utf-8"))["api_key"] == SECRET


def test_file_store_readback_mismatch_rolls_back(tmp_path, monkeypatch):
    path = tmp_path / "desktop" / "access-config.json"
    store = ac.FileAccessStore(path)
    store.save(ac.AccessConfig(node_id="before"))
    original = store.load
    monkeypatch.setattr(store, "load", lambda: ac.AccessConfig(node_id="tampered"))
    with pytest.raises(ValueError):
        store.save(ac.AccessConfig(node_id="after"))
    monkeypatch.undo()
    assert original().node_id == "before"              # 旧值必须还在


def test_memory_store_corrupt_readback_is_reported(tmp_path):
    """回读不一致（存储层自己抛）→ 不能算成功，必须说「未生效」。"""
    store = ac.MemoryAccessStore(corrupt_readback=True)
    result = ac.AccessService(store).save(ac.AccessConfig(api_key=SECRET))
    assert result.effective is False
    assert result.saved is False or result.readback_ok is False
    assert "未生效" in result.message


def test_memory_store_write_failure_is_reported():
    store = ac.MemoryAccessStore(fail_save=True)
    result = ac.AccessService(store).save(ac.AccessConfig(api_key=SECRET))
    assert result.saved is False and result.effective is False
    assert "原配置" in result.message


# ─────────────────────────────────────────────────────────────── 保存服务
class Recorder:
    def __init__(self, state=ac.RELOAD_OK):
        self.state = state
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.state


def test_service_save_ok_reports_effective(tmp_path):
    reloader = Recorder(ac.RELOAD_OK)
    service = ac.AccessService(ac.FileAccessStore(tmp_path / "c.json"), reloader=reloader)
    result = service.save(ac.AccessConfig(api_key=SECRET))
    assert result.saved and result.readback_ok and result.reload == ac.RELOAD_OK
    assert result.effective is True
    assert reloader.calls == 1


def test_service_save_invalid_does_not_write(tmp_path):
    path = tmp_path / "c.json"
    service = ac.AccessService(ac.FileAccessStore(path))
    result = service.save(ac.AccessConfig(usdt_address="bogus"))
    assert result.saved is False and result.effective is False
    assert not path.exists()
    assert "USDT" in result.message


@pytest.mark.parametrize("state,word", [
    (ac.RELOAD_DAEMON_DOWN, "daemon 未运行"),
    (ac.RELOAD_UNSUPPORTED, "无热重载命令"),
    (ac.RELOAD_FAILED, "重载未成功"),
])
def test_service_never_claims_effective_without_reload(tmp_path, state, word):
    service = ac.AccessService(ac.FileAccessStore(tmp_path / "c.json"),
                               reloader=Recorder(state))
    result = service.save(ac.AccessConfig(api_key=SECRET))
    assert result.saved is True and result.readback_ok is True
    assert result.reload == state
    assert result.effective is False
    assert ac.RELOAD_TEXT[state] in result.message
    assert word in result.message
    assert "配置生效" not in result.message


def test_service_without_reloader_says_unsupported(tmp_path):
    service = ac.AccessService(ac.FileAccessStore(tmp_path / "c.json"))
    result = service.save(ac.AccessConfig(api_key=SECRET))
    assert result.reload == ac.RELOAD_UNSUPPORTED
    assert result.effective is False


def test_service_reload_is_explicit_and_idempotent(tmp_path):
    reloader = Recorder(ac.RELOAD_DAEMON_DOWN)
    service = ac.AccessService(ac.FileAccessStore(tmp_path / "c.json"), reloader=reloader)
    assert service.reload() == ac.RELOAD_DAEMON_DOWN
    assert service.reload() == ac.RELOAD_DAEMON_DOWN
    assert reloader.calls == 2


def test_service_reports_display_without_secrets(tmp_path):
    service = ac.AccessService(ac.FileAccessStore(tmp_path / "c.json"))
    result = service.save(ac.AccessConfig(api_key=SECRET, node_secret=NODE_SECRET))
    blob = json.dumps(result.display, ensure_ascii=False)
    assert SECRET not in blob and NODE_SECRET not in blob
