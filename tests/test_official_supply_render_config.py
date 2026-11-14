# SPDX-License-Identifier: Apache-2.0
"""install.sh 的 config 渲染（2026-11-15 审计 P2）：URL 必须原样落到 JSON。

现场（sed 版）：`--gateway 'https://gw.example.com/&'` 里的 `&` 在替换串中展开成
"整个匹配"，`#` 撞分隔符，`"` / `\\` 直接写坏 JSON —— 安装脚本不报错，装出来的
config 却让 daemon 起不来，或者把 URL 写进了别的字段。
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import shutil
import subprocess

PROFILE = pathlib.Path(__file__).resolve().parents[1] / "profiles" / "official-supply"
RENDER = PROFILE / "render_config.py"
INSTALL_SH = PROFILE / "install.sh"


def _load_render():
    spec = importlib.util.spec_from_file_location("render_config", RENDER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_render_keeps_url_characters_verbatim(tmp_path):
    render = _load_render()
    target = tmp_path / "config.json"
    tricky = 'https://gw.example.com/a/&b#frag?x=1&y=%22"\\'
    config = render.render(str(PROFILE / "config.json"), str(target), tricky,
                           "https://gw.example.com/manifest.json?x=1&y=2")
    assert config["gateway_url"] == tricky
    written = json.loads(target.read_text(encoding="utf-8"))      # 必须是合法 JSON
    assert written["gateway_url"] == tricky
    assert written["update_source"] == "https://gw.example.com/manifest.json?x=1&y=2"


def test_render_preserves_profile_fields(tmp_path):
    render = _load_render()
    target = tmp_path / "config.json"
    source = json.loads((PROFILE / "config.json").read_text(encoding="utf-8"))
    config = render.render(str(PROFILE / "config.json"), str(target), "https://gw.example.com",
                           "https://gw.example.com/manifest.json")
    assert config["edition"] == source["edition"] == "official-supply"
    assert config["extra"]["feature_flags"] == source["extra"]["feature_flags"]
    assert config["mirror_port"] == source["mirror_port"]


def test_install_sh_no_longer_uses_sed_for_urls():
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "render_config.py" in text
    assert 's#"gateway_url"' not in text and "s#\"gateway_url\"" not in text


def test_install_sh_syntax_ok():
    bash = shutil.which("bash")
    if bash is None:                                              # pragma: no cover
        import pytest
        pytest.skip("没有 bash")
    result = subprocess.run([bash, "-n", str(INSTALL_SH)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
