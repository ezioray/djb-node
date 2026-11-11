# SPDX-License-Identifier: Apache-2.0
"""特供档 profile 的开关到期哨兵（0.3.4 / task-30）。

## 为什么需要这个文件

`profiles/official-supply/config.json` 用 `extra.feature_flags` 显式开了三个开关：
`client.relay.enabled`、`client.probe.periodic.enabled`、`client.quotes.publish.enabled`。
但 `config.feature_flag()` 的求值顺序是 **先判 `flag_expired()`，再读配置**
（`djb_node/config.py:335-350`，口径见 `tests/test_feature_flags.py`）：

    到期 → 强制关闭，**配置里的 `true` 一点用都没有**

于是「profile 写 `true`」≠「这个档位一直能用」。2026-12-31 那批开关到期时，
特供档的表现是：报价环不启动 / relay 入口消失，而 `config.json` 看起来完全正常
（`reports/NEXT-20261112/impl/88-FLAG-EXPIRY.md` §6.3/§6.4 已核实）。

本文件的三个断言把这件事变成 CI 红灯，而不是客户侧的功能静默消失：
1. profile 里出现的每个开关都必须真的登记在 `facts.FEATURE_FLAGS`（拼错 = 静默无效）；
2. 这些开关的 `expire_at` 距今天必须还有余量（< 90 天就红，留一个发布窗口的提前量）；
3. 业务模式锁：relay / quotes.publish 必须仍被显式打开（特供档的主用上游类型是
   `third_party_relay`，靠默认值是 False，删掉这两行等于把档位打回普通客户端）。
"""
from __future__ import annotations

import json
import pathlib
from datetime import date

import pytest

from djb_node import facts
from djb_node.config import _parse_expire_at, flag_expired

PROFILE = pathlib.Path(__file__).resolve().parents[1] / "profiles" / "official-supply" / "config.json"

#: 到期余量下限：小于这个天数就红，给「改 facts + 重出包 + 客户升级」留一个窗口。
EXPIRY_MARGIN_DAYS = 90

#: 特供档的业务模式锁（删掉就不是特供档了，见本文件 §3）。
PROFILE_REQUIRED_ON = ("client.relay.enabled", "client.quotes.publish.enabled")


def _profile_flags() -> dict[str, bool]:
    raw = json.loads(PROFILE.read_text(encoding="utf-8"))
    return dict(raw["extra"]["feature_flags"])


def test_profile_flags_are_all_registered():
    """profile 写错开关名 = 静默无效（tolerant reader 不报错），必须在这里拦住。"""
    flags = _profile_flags()
    assert flags, "profile 应当显式开启至少一个开关"
    unknown = sorted(set(flags) - set(facts.FEATURE_FLAGS))
    assert not unknown, "profile 里的开关未登记在 facts.FEATURE_FLAGS：" + "、".join(unknown)
    for name, value in flags.items():
        assert value is True, name + " 在 profile 里只应该写 true（false 直接删行更清楚）"


def test_profile_flags_have_expiry_margin():
    """到期哨兵：profile 依赖的开关必须还有余量，否则续期窗口一到就静默掉功能。"""
    flags = _profile_flags()
    today = date.today()
    for name in sorted(flags):
        expire_at = facts.FEATURE_FLAGS[name].get("expire_at")
        expires = _parse_expire_at(expire_at)
        assert expires is not None, name + " 的 expire_at 不合法：" + str(expire_at)
        remaining = (expires - today).days
        assert remaining >= EXPIRY_MARGIN_DAYS, (
            name + " 只剩 " + str(remaining) + " 天到期（expire_at=" + str(expire_at) + "）；"
            "到期后 profile 的 true 会被 flag_expired 压成关闭（特供档掉报价/relay）。"
            "处理：续 facts.FEATURE_FLAGS 的 expire_at + 重出包，并同步本测试的余量口径。")
        assert not flag_expired(name, today=today), name + " 已经到期"


def test_profile_keeps_third_party_relay_business_locks():
    """relay / quotes.publish 必须显式开：两者 facts 默认是 False，删行 = 档位失效。"""
    flags = _profile_flags()
    for name in PROFILE_REQUIRED_ON:
        assert flags.get(name) is True, name + " 是特供档的业务模式锁，profile 不能删"
        assert facts.FEATURE_FLAGS[name]["default"] is False, name + " 若改成默认开，本断言要复核"


# ─────────────────────────────────── install.sh：升级必须带档位（W6 演练 R-2 / R-4）
INSTALL_SH = PROFILE.parent / "install.sh"


def test_install_sh_syntax_ok():
    """`bash -n`：脚本语法先过关（改 `--edition` 时不至于把 heredoc 弄坏）。"""
    import shutil
    import subprocess
    bash = shutil.which("bash")
    if bash is None:                                  # pragma: no cover - 极简环境
        pytest.skip("没有 bash")
    result = subprocess.run([bash, "-n", str(INSTALL_SH)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_install_sh_passes_edition_to_init():
    """W6 演练 R-2：`djb init` 必须带 `--edition official-supply`（dry-run 打印也要带）。

    漏了它的后果：0.3.3 的 config 没有 `edition` 字段，升级后 `update --status` 的
    `edition` / `source_channel` 为空，自动升级只剩 config 里的 URL 一条线索。
    """
    text = INSTALL_SH.read_text(encoding="utf-8")
    init_lines = [line for line in text.splitlines() if "init --no-env" in line]
    assert len(init_lines) >= 2, "应有 dry-run 与真跑两条 init 调用"
    for line in init_lines:
        assert "--edition official-supply" in line, "缺档位：" + line.strip()


def test_install_sh_hints_edition_on_existing_config():
    """W6 演练 R-4：已有配置不覆盖时，要提示「升级请确认 edition」。"""
    text = INSTALL_SH.read_text(encoding="utf-8")
    idx = text.index("已有配置，不覆盖")
    tail = text[idx:idx + 700]
    assert "djb init --edition official-supply" in tail


def _run_cli(args: list[str], home: pathlib.Path) -> None:
    import os
    import subprocess
    import sys
    env = dict(os.environ)
    env["DJB_HOME"] = str(home)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run([sys.executable, "-B", "-m", "djb_node.cli"] + args,
                            cwd=str(PROFILE.parents[2]), env=env,
                            capture_output=True, text=True)
    assert result.returncode == 0, (result.stdout or "") + (result.stderr or "")


def test_upgrade_from_0_3_3_config_lands_edition(tmp_path):
    """R-2 的回归测：0.3.3 的 config（无 edition）+ 新的 init 命令 → edition 落地且不丢字段。

    这里用**真 CLI**（`init --no-env --edition official-supply`）跑一遍，而不是只断言文本：
    演练里漏的正是「文本改了但命令没生效」这一类。
    """
    home = tmp_path / "home"
    home.mkdir()
    legacy = {
        "gateway_url": "https://tkbank.ai",
        "mirror_port": 1717,
        "update_channel": "stable",
        "update_source": "https://tkbank.ai/download/files/update-manifest.json",
        "log_level": "info",
        "yield_enabled": False,
        "extra": {"feature_flags": {"client.relay.enabled": True,
                                    "client.quotes.publish.enabled": True,
                                    "client.probe.periodic.enabled": True},
                  "quotes.period_s": 60, "quotes.ttl_s": 120,
                  "upstream.kind": "third_party_relay"},
    }
    (home / "config.json").write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")

    _run_cli(["init", "--no-env", "--edition", "official-supply"], home)

    data = json.loads((home / "config.json").read_text(encoding="utf-8"))
    assert data["edition"] == "official-supply"                 # ← 演练里漏的就是这一条
    assert data["gateway_url"] == legacy["gateway_url"]         # 不覆盖服务端地址
    assert data["update_source"] == legacy["update_source"]     # 已有的官方源原样保留
    assert data["extra"]["upstream.kind"] == "third_party_relay"  # 业务模式不被 init 弄丢
    assert data["extra"]["feature_flags"]["client.relay.enabled"] is True
