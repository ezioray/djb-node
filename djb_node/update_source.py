# SPDX-License-Identifier: Apache-2.0
"""五通道更新源显式化（RC8.3-C 收口 / 0.3.1；P1-3 的正式收口）。

## 为什么有这个模块

0.3.0 的五通道里，只有 `profiles/official-supply/config.json` 显式写了 `update_source`；
machine-room 离线包（install.sh / manifest.json）与 Windows 便携包（zip 里没有 config、
也没有 init 脚本）**没有任何载体**；Linux 源码包只能靠 README 让人自己敲 `--update-source`。
而 `ClientConfig.update_source` 空串的语义是「用内置 mock 源」（S1 口径），
所以「没写」与「故意不联网」在客户端侧**无法区分**——审计口径见
`reports/RC8-AUDIT/ROUND2-RELEASE-PRE.md` §3.3 / P1-3。

## 0.3.1 口径（三句话）

1. **五个通道每个都有显式载体**，写的是**同一份签名清单** URL（:data:`MANIFEST_URL`）：
   消费端五通道共用一份清单，差异只在 edition / 平台，不在源地址——多份清单 = 多套签名，
   回滚面会翻倍。
2. 客户端侧把「源从哪来」显式化：:func:`resolve` 返回
   ``origin ∈ {config, edition, mock}``、``kind ∈ {official-manifest, custom, file, mock}``；
   `djb update --status` 与 `djb init --edition` 的提示据此如实显示（**人读输出不打印
   URL 本身**，见下；`update --status --json` 里的 `source` 仍是 0.3.0 的原有行为）。
3. **默认行为不变**（0.3.0 兼容）：`update_source` 为空且没给 edition → 仍然走 mock；
   只有显式给了 edition（`djb init --edition ...` 或包内 config）才会落默认 URL。

## 两条纪律

* 这里只管「从哪拉清单」；验签仍由 :mod:`djb_node.update` 的内置 Ed25519 钥环负责，
  两件事不要混在一处（否则「换个源」会被误读成「换信任锚」）。
* **不要把 MANIFEST_URL 打进 env / --version / --help 的输出**：构建流水线
  （`ops/release/build/*.sh`、`win-build.py`）在自检阶段对 stdout/stderr 全文扫
  `tkbank.ai`，命中即判护栏违规、不出包。所以人读输出只给分类（如
  ``official-manifest``），完整 URL 只在 `update --status --json` 里给。
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

#: 生产签名更新清单（唯一权威源；dl.aitokenmall.ai 是 502 旧域，禁用）。
MANIFEST_URL = "https://tkbank.ai/download/files/update-manifest.json"

#: 更新源种类（status / env 展示用；永不回显密钥材料）。
KIND_MOCK = "mock"
KIND_OFFICIAL = "official-manifest"
KIND_FILE = "file"
KIND_CUSTOM = "custom"
KINDS: tuple[str, ...] = (KIND_MOCK, KIND_OFFICIAL, KIND_FILE, KIND_CUSTOM)

#: 来源（谁决定的）。
ORIGIN_CONFIG = "config"
ORIGIN_EDITION = "edition"
ORIGIN_MOCK = "mock"
ORIGINS: tuple[str, ...] = (ORIGIN_CONFIG, ORIGIN_EDITION, ORIGIN_MOCK)


@dataclass(frozen=True)
class ChannelSpec:
    """一个发行通道的「更新源契约」：谁打包、源写在哪、是不是离线可用。"""

    channel: str            # 稳定标识（回滚/审计引用这个）
    edition: str            # MANIFEST.json / update-manifest.json 的 edition 取值
    platform: str
    artifact_kind: str      # 与 MANIFEST.json 的 kind 对齐
    carrier: str            # 显式载体（人读；CI 用它断言「真的有载体」）
    update_source: str = MANIFEST_URL
    offline_capable: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "channel": self.channel,
            "edition": self.edition,
            "platform": self.platform,
            "artifact_kind": self.artifact_kind,
            "carrier": self.carrier,
            "update_source": self.update_source,
            "offline_capable": self.offline_capable,
        }


#: **五通道**（顺序与 MANIFEST.json / update-manifest.json 的 artifacts 顺序一致）。
CHANNELS: tuple[ChannelSpec, ...] = (
    ChannelSpec(
        channel="consumer-linux-src", edition="consumer", platform="linux",
        artifact_kind="source-tar-gz",
        carrier="包内 README-INSTALL.txt 的 init 命令 + VERSION.txt#update_source",
    ),
    ChannelSpec(
        channel="consumer-macos", edition="consumer", platform="macos",
        artifact_kind="dmg",
        carrier="包内 README-INSTALL.txt 的 init 命令 + Info.plist 旁的 VERSION.txt#update_source",
    ),
    ChannelSpec(
        channel="consumer-windows", edition="consumer", platform="windows",
        artifact_kind="portable-onefile-zip",
        carrier="包内 config.json + start-windows.bat 的 init 命令 + VERSION.txt#update_source",
    ),
    ChannelSpec(
        channel="machine-room", edition="machine-room", platform="linux",
        artifact_kind="offline-tar-gz",
        carrier="offline/install.sh --update-source + config.template.json + manifest.json#update_source",
        offline_capable=True,
    ),
    ChannelSpec(
        channel="official-supply", edition="official-supply", platform="linux",
        artifact_kind="tar-gz",
        carrier="profiles/official-supply/config.json + VERSION.txt#update_source",
    ),
)

_EDITIONS: tuple[str, ...] = tuple(dict.fromkeys(spec.edition for spec in CHANNELS))


def channels() -> tuple[ChannelSpec, ...]:
    return CHANNELS


def channel_keys() -> tuple[str, ...]:
    return tuple(spec.channel for spec in CHANNELS)


def editions() -> tuple[str, ...]:
    return _EDITIONS


def spec(channel: str) -> ChannelSpec:
    """按通道标识取契约；未知通道 = 参数错（**不回落**到 consumer）。"""
    for item in CHANNELS:
        if item.channel == str(channel or "").strip():
            return item
    raise ValueError("未知更新通道：" + str(channel)
                     + "（已知：" + " / ".join(channel_keys()) + "）")


def spec_for(edition: str, platform: str = "") -> ChannelSpec:
    """按 edition(+platform) 取契约；同 edition 多平台时 platform 必须能唯一确定。"""
    matches = [item for item in CHANNELS if item.edition == str(edition or "").strip()]
    if platform:
        narrowed = [item for item in matches if item.platform == str(platform).strip()]
        if narrowed:
            matches = narrowed
    if not matches:
        raise ValueError("未知 edition：" + str(edition)
                         + "（已知：" + " / ".join(_EDITIONS) + "）")
    return matches[0]


def default_source(edition: str) -> str:
    """该 edition 的默认更新源（显式值；仅当使用者明确要求 edition 时才用）。"""
    return spec_for(edition).update_source


def _canonical(url: str) -> str:
    """归一化比较用：scheme/host 小写、去默认端口与结尾斜杠；其余保持原样。"""
    text = str(url or "").strip()
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https"):
        return text
    host = (parsed.hostname or "").lower()
    port = "" if parsed.port in (None, 80, 443) else ":" + str(parsed.port)
    return parsed.scheme.lower() + "://" + host + port + (parsed.path or "").rstrip("/")


def classify(source: str) -> str:
    """更新源分类：mock / official-manifest / file / custom（不联网、不解析内容）。"""
    text = str(source or "").strip()
    if not text:
        return KIND_MOCK
    if _canonical(text) == _canonical(MANIFEST_URL):
        return KIND_OFFICIAL
    parsed = urlparse(text)
    if parsed.scheme == "file" or (parsed.scheme == "" and text.startswith("/")):
        return KIND_FILE
    return KIND_CUSTOM


@dataclass(frozen=True)
class Resolution:
    """一次「本机到底从哪更新」的判定结果（只描述来源，不含任何密钥）。"""

    source: str
    origin: str            # config / edition / mock
    kind: str              # mock / official-manifest / file / custom
    edition: str = ""
    channel: str = ""
    offline_capable: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "origin": self.origin,
            "kind": self.kind,
            "edition": self.edition,
            "channel": self.channel,
            "offline_capable": self.offline_capable,
        }

    def label(self) -> str:
        """人读标签（**不含 URL**，可安全打进 env / 日志 / 构建自检）。"""
        return self.kind + " (origin=" + self.origin + ")"


def resolve(source: str, *, edition: str = "", platform: str = "") -> Resolution:
    """判定本机更新源：config 优先 → edition 默认 → mock（0.3.0 行为不变）。

    * ``source`` 非空 ⇒ origin=config（**显式配置永远压过 edition**，便于机房改内网镜像）；
    * ``source`` 为空且给了 ``edition`` ⇒ origin=edition，取该通道的显式默认值；
    * 两者都空 ⇒ origin=mock（mock 源 = 本机 versions/mock-manifest.json，未配置不联网）。
    """
    text = str(source or "").strip()
    use_edition = str(edition or "").strip()
    known = None
    if use_edition:
        known = next((item for item in CHANNELS if item.edition == use_edition), None)
        if known is None and platform:
            known = next((item for item in CHANNELS
                          if item.edition == use_edition and item.platform == platform), None)
    channel = known.channel if known is not None else ""
    offline = bool(known.offline_capable) if known is not None else False
    if text:
        origin = ORIGIN_CONFIG
    elif known is not None:
        text = known.update_source
        origin = ORIGIN_EDITION
    else:
        # 未知 edition（比如被新版写了本版不认识的档位）：**回落 mock，不联网、不抛**。
        # 这里的取舍是「宁可不动」而不是 fail-closed —— 更新源不是安全边界，
        # 崩 daemon 的代价比「这轮不更新」大得多；未知档位在 init 校验阶段仍会被拒。
        origin = ORIGIN_MOCK
    return Resolution(source=text, origin=origin, kind=classify(text),
                      edition=use_edition, channel=channel, offline_capable=offline)


def version_txt_lines() -> list[str]:
    """VERSION.txt 里的显式声明行（三个打包器共用，保证五通道口径一致）。"""
    return ["update_source    : " + MANIFEST_URL]
