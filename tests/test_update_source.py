# SPDX-License-Identifier: Apache-2.0
"""五通道更新源显式化（RC8.3-C / 0.3.1，P1-3 收口）。

覆盖三件事：

1. **五个通道都有显式载体**：channel 表完整、都指向同一份签名清单、仓库里真的存在
   对应的载体文件（config / install.sh / VERSION 生成处），不是一个内存里的字符串；
2. **客户端解析口径**：config 优先 → edition 默认 → mock；未知 edition 不炸；
   `label()` 不含 URL（构建自检对 `tkbank.ai` 是硬护栏）；
3. **0.3.0 兼容**：空 update_source 仍走 mock、写回配置形状不变、旧清单（无 key_id）照旧。
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from djb_node import facts, update_source
from djb_node.config import ClientConfig, Paths, load_config, save_config
from djb_node.update import UpdateManager
from tests.update_fixtures import build_manifest, use_test_anchor

REPO = Path(__file__).resolve().parent.parent


def _head_sha() -> str:
    """pkgbuild 的 --sha 自 0.3.5 起必填且要 40 位全 sha（P2）。"""
    return subprocess.check_output(["git", "-C", str(REPO), "rev-parse", "HEAD"],
                                   text=True).strip()


def _pkg_env() -> dict:
    """pkgbuild 自 0.3.5 起拒绝无 SOURCE_DATE_EPOCH 的构建（P1 复核修）：测试按提交时间给。"""
    epoch = subprocess.check_output(
        ["git", "-C", str(REPO), "show", "-s", "--format=%ct", "HEAD"], text=True).strip()
    return {**os.environ, "SOURCE_DATE_EPOCH": epoch}


# ─────────────────────────────────────────────────────────── 通道表本身
def test_five_channels_are_explicit_and_point_to_one_manifest():
    channels = update_source.channels()
    assert len(channels) == 5
    assert [item.channel for item in channels] == [
        "consumer-linux-src", "consumer-macos", "consumer-windows",
        "machine-room", "official-supply",
    ]
    # 五通道**同一个源**：多份清单 = 多套签名 = 回滚面翻倍
    assert {item.update_source for item in channels} == {update_source.MANIFEST_URL}
    assert update_source.MANIFEST_URL.endswith("/download/files/update-manifest.json")
    # 每个通道都必须写清「载体」——没有载体就等于没显式化
    for item in channels:
        assert item.carrier.strip(), item.channel
        assert item.edition in update_source.editions()
    # edition 与 MANIFEST/update-manifest 的口径一致（consumer/machine-room/official-supply）
    assert set(update_source.editions()) == {"consumer", "machine-room", "official-supply"}
    assert update_source.spec("machine-room").offline_capable is True


def test_spec_lookup_is_strict():
    with pytest.raises(ValueError):
        update_source.spec("not-a-channel")
    with pytest.raises(ValueError):
        update_source.spec_for("not-an-edition")
    # 同 edition 多平台：platform 能收窄
    assert update_source.spec_for("consumer", "windows").channel == "consumer-windows"
    assert update_source.spec_for("consumer", "macos").channel == "consumer-macos"
    assert update_source.default_source("official-supply") == update_source.MANIFEST_URL


@pytest.mark.parametrize("carrier", [
    "profiles/official-supply/config.json",
    "deploy/machine-room/offline/install.sh",
    "deploy/machine-room/offline/build.sh",
    "ci/pkgbuild.py",
])
def test_carrier_files_exist_in_repo(carrier: str) -> None:
    path = REPO / carrier
    assert path.is_file(), carrier
    text = path.read_text(encoding="utf-8")
    assert "update_source" in text, carrier


def test_official_supply_config_carries_explicit_source_and_edition():
    config = json.loads((REPO / "profiles/official-supply/config.json").read_text(encoding="utf-8"))
    assert config["update_source"] == update_source.MANIFEST_URL
    assert config["edition"] == "official-supply"


# ─────────────────────────────────────────────────────────── 分类 / 解析
@pytest.mark.parametrize("source,kind", [
    ("", update_source.KIND_MOCK),
    (update_source.MANIFEST_URL, update_source.KIND_OFFICIAL),
    # 同名清单的 http 变体**不算** official：明文降级按自定义源对待（要显式写出来才生效）
    (update_source.MANIFEST_URL.replace("https://", "http://"), update_source.KIND_CUSTOM),
    (update_source.MANIFEST_URL + "/", update_source.KIND_OFFICIAL),
    ("/srv/mirror/manifest.json", update_source.KIND_FILE),
    ("file:///srv/mirror/manifest.json", update_source.KIND_FILE),
    ("https://mirror.internal.example/update-manifest.json", update_source.KIND_CUSTOM),
])
def test_classify(source: str, kind: str) -> None:
    assert update_source.classify(source) == kind


def test_resolve_precedence():
    # 1) config 显式值永远压过 edition（内网镜像场景）
    resolved = update_source.resolve("https://mirror.internal.example/u.json",
                                     edition="machine-room")
    assert (resolved.origin, resolved.kind) == (update_source.ORIGIN_CONFIG,
                                                update_source.KIND_CUSTOM)
    assert resolved.channel == "machine-room"
    assert resolved.offline_capable is True
    # 2) 只给 edition → 落该通道显式默认值
    resolved = update_source.resolve("", edition="official-supply")
    assert resolved.source == update_source.MANIFEST_URL
    assert (resolved.origin, resolved.kind) == (update_source.ORIGIN_EDITION,
                                                update_source.KIND_OFFICIAL)
    # 3) 都不给 → mock（0.3.0 行为不变）
    resolved = update_source.resolve("")
    assert (resolved.source, resolved.origin, resolved.kind) == (
        "", update_source.ORIGIN_MOCK, update_source.KIND_MOCK)
    # 4) 未知 edition（新版写了本版不认识的档位）→ 回落 mock，不抛
    resolved = update_source.resolve("", edition="future-edition")
    assert (resolved.origin, resolved.kind) == (update_source.ORIGIN_MOCK,
                                                update_source.KIND_MOCK)


def test_label_never_leaks_manifest_url():
    """构建自检对 tkbank.ai 是硬护栏：标签里绝不能出现 URL。"""
    for edition in ("", *update_source.editions()):
        label = update_source.resolve("", edition=edition).label()
        assert "tkbank.ai" not in label
        assert "http" not in label
    assert update_source.resolve("", edition="machine-room").label() == \
        "official-manifest (origin=edition)"


# ─────────────────────────────────────────────────────────── 0.3.0 兼容
def test_empty_source_config_keeps_030_behaviour(home) -> None:
    """0.3.0 装机的 config.json（没有 edition、update_source 为空）必须原样可用。"""
    paths = Paths(str(home))
    paths.prepare()
    config = ClientConfig.from_dict({"gateway_url": "https://gw.example.invalid"})
    assert config.edition == ""
    assert config.update_source == ""
    # 写回形状与 0.3.0 一致：没有 edition 键
    assert "edition" not in config.to_dict()
    save_config(paths, config)
    reloaded = load_config(paths)
    assert reloaded.update_source == "" and reloaded.edition == ""
    assert update_source.resolve(reloaded.update_source,
                                 edition=reloaded.edition).kind == update_source.KIND_MOCK


def test_edition_roundtrip_and_validation(tmp_path) -> None:
    config = ClientConfig.from_dict({"edition": "machine-room"})
    assert config.edition == "machine-room"
    assert config.to_dict()["edition"] == "machine-room"
    # 老客户端读 0.3.1 的配置：edition 落 extra（不认识也不炸）
    legacy_like = ClientConfig.from_dict({"edition": "machine-room"})
    assert legacy_like.extra in ({}, None) or "edition" not in legacy_like.extra
    with pytest.raises(ValueError):
        ClientConfig(edition="not-an-edition").validate()


def test_old_manifest_without_key_id_still_verifies(monkeypatch) -> None:
    """0.3.0 的清单口径不变：无 key_id 时任一可用钥验过即可（轮换兼容窗口）。"""
    use_test_anchor(monkeypatch)
    from djb_node import update

    manifest = build_manifest(key_id=None)          # 0.3.0 轮换前的清单形状
    assert "key_id" not in manifest["payload"]
    payload = update.verify_manifest(manifest, update.keyring_anchors())
    assert payload["version"] == "0.2.0"


# ─────────────────────────────────────────────────────────── CLI / status
def test_cli_init_edition_writes_explicit_source(home, capsys) -> None:
    from djb_node.cli import main

    assert main(["init", "--no-env", "--edition", "machine-room"]) == 0
    out = capsys.readouterr().out
    assert "官方" not in out  # 人读输出不出现 URL 片段
    assert "tkbank.ai" not in out
    paths = Paths(str(home))
    config = load_config(paths)
    assert config.edition == "machine-room"
    assert config.update_source == update_source.MANIFEST_URL


def test_cli_init_update_source_beats_edition(home, capsys) -> None:
    from djb_node.cli import main

    mirror = "https://mirror.internal.example/update-manifest.json"
    assert main(["init", "--no-env", "--edition", "machine-room",
                 "--update-source", mirror]) == 0
    capsys.readouterr()
    config = load_config(Paths(str(home)))
    assert config.update_source == mirror          # 显式源优先
    assert config.edition == "machine-room"


def test_cli_init_edition_keeps_custom_source(home, capsys) -> None:
    from djb_node.cli import main

    mirror = "/srv/mirror/manifest.json"
    assert main(["init", "--no-env", "--update-source", mirror]) == 0
    capsys.readouterr()
    assert main(["init", "--no-env", "--edition", "official-supply"]) == 0
    out = capsys.readouterr().out
    config = load_config(Paths(str(home)))
    assert config.update_source == mirror          # 自定义源不被 --edition 覆盖
    assert "保留已有自定义" in out


def test_update_status_reports_source_kind_and_origin(paths, store) -> None:
    manager = UpdateManager(paths, store, source="", edition="machine-room",
                            client_version="0.1.0")
    status = manager.status()
    assert status["source_origin"] == update_source.ORIGIN_EDITION
    assert status["source_kind"] == update_source.KIND_OFFICIAL
    assert status["edition"] == "machine-room"
    assert status["source_channel"] == "machine-room"
    # 既有键形状不变（0.3.0 的调用方不受影响）
    for key in ("channel", "current_version", "source", "signature"):
        assert key in status


# ─────────────────────────────────────────────────────────── 打包侧载体
def _pkgbuild_plan(tmp_path: Path, kind: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(REPO / "ci" / "pkgbuild.py"), "--kind", kind,
         "--plan", "--out", str(tmp_path / "pkg"), "--sha", _head_sha()],
        cwd=str(REPO), capture_output=True, text=True, timeout=120, env=_pkg_env())


@pytest.mark.parametrize("kind,channel", [
    ("linux-src", "consumer-linux-src"),
    ("official-supply", "official-supply"),
    ("machine-room-offline", "machine-room"),
    ("windows-assets", "consumer-windows"),
])
def test_pkgbuild_plan_covers_all_linux_and_windows_carriers(tmp_path, kind, channel):
    result = _pkgbuild_plan(tmp_path, kind)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "channel=" + channel in result.stdout
    assert "tkbank.ai" not in result.stdout.split("BUILD_RESULT")[0].lower()
    assert not (tmp_path / "pkg").exists()          # --plan 不写盘


def test_pkgbuild_refuses_release_surface(tmp_path) -> None:
    target = REPO.parent / "ops" / "rc" / "artifacts" / "downloads"
    result = subprocess.run(
        [sys.executable, str(REPO / "ci" / "pkgbuild.py"), "--kind", "linux-src",
         "--plan", "--out", str(target), "--sha", _head_sha()],
        cwd=str(REPO), capture_output=True, text=True, timeout=120, env=_pkg_env())
    assert result.returncode == 2
    assert "发布面" in result.stderr


def test_windows_assets_carry_explicit_source(tmp_path) -> None:
    result = subprocess.run(
        [sys.executable, str(REPO / "ci" / "pkgbuild.py"), "--kind", "windows-assets",
         "--out", str(tmp_path / "out"), "--sha", _head_sha()],
        cwd=str(REPO), capture_output=True, text=True, timeout=300, env=_pkg_env())
    assert result.returncode == 0, result.stdout + result.stderr
    stem = re.search(r"BUILD_RESULT (\{.*\})", result.stdout)
    assert stem, result.stdout
    payload = json.loads(stem.group(1))
    assert payload["channel"] == "consumer-windows"
    assert payload["update_source"] == update_source.MANIFEST_URL
    assert payload["sha256"] and payload["bytes"] > 0
    unpacked = tmp_path / "out" / ("djb-node-%s-windows-x64-artifacts" % facts.CLIENT_VERSION)
    config = json.loads((unpacked / "config.json").read_text(encoding="utf-8"))
    assert config["update_source"] == update_source.MANIFEST_URL
    assert config["edition"] == "consumer"
    bat = (unpacked / "start-windows.bat").read_text(encoding="utf-8")
    assert "--update-source" in bat and "--edition" in bat
    version_txt = (unpacked / "VERSION.txt").read_text(encoding="utf-8")
    assert "update_source    : " + update_source.MANIFEST_URL in version_txt
    # 人读输出不出现生产 URL（护栏）；机器可读的 BUILD_RESULT 里必须有
    assert "tkbank.ai" not in result.stdout.split("BUILD_RESULT")[0].lower()


def test_machine_room_offline_package_declares_source(tmp_path) -> None:
    """机房离线包：manifest.json / config.template.json 都必须显式写出 update_source。"""
    result = subprocess.run(
        [sys.executable, str(REPO / "ci" / "pkgbuild.py"),
         "--kind", "machine-room-offline", "--out", str(tmp_path / "out"), "--sha", _head_sha()],
        cwd=str(REPO), capture_output=True, text=True, timeout=900, env=_pkg_env())
    assert result.returncode == 0, (result.stdout + result.stderr)[-2500:]
    payload = json.loads(re.search(r"BUILD_RESULT (\{.*\})", result.stdout).group(1))
    assert payload["manifest_update_source"] == update_source.MANIFEST_URL
    assert payload["manifest_edition"] == "machine-room"
    assert os.path.getsize(payload["path"]) == payload["bytes"]
