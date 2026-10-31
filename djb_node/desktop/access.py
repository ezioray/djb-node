# SPDX-License-Identifier: Apache-2.0
"""接入配置：模型 / 原子落盘 / 掩码 / 「保存 → reload → 回读一致」。

设计边界
========

* **纯 stdlib**：不 import tkinter、不 import 第三方；本模块可在无图形环境跑全量测试。
* **秘密只落盘、不上屏**：:class:`AccessConfig` 里的 ``node_secret`` / ``api_key`` /
  ``scoped_token`` 只用于写盘与调用，任何投影（:meth:`AccessConfig.display`）只给
  :func:`mask_secret` 的掩码；日志/异常消息一律不拼接原值。
* **不谎报「已生效」**：``保存 → daemon reload → 回读一致`` 三步各自有明确结果，
  reload 用 :data:`RELOAD_*` 四态表达；daemon 没有热重载命令时如实返回
  ``RELOAD_UNSUPPORTED``，文案要求重启 daemon，**不写「已生效」**。
* **回读不一致即回滚**：落盘前备份旧文件，回读比对不等就还原旧文件并抛
  ``ValueError``（调用方显示「保存未生效」），不留半写状态。
* **文件权限**：目录 0700、文件 0600（与 ``gui/window.py`` 的草稿一致）；
  地址（USDT 收款地址）不是秘密，但列表里也只给掩码，避免误截图外泄。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Mapping

__all__ = [
    "ACCESS_FILE", "AccessConfig", "AccessService", "FileAccessStore", "MemoryAccessStore",
    "PRICE_MODES", "RELOAD_DAEMON_DOWN", "RELOAD_FAILED", "RELOAD_OK", "RELOAD_UNSUPPORTED",
    "SaveResult", "default_access_path", "mask_address", "mask_secret", "validate_address",
    "validate_url",
]

#: 相对 ``$DJB_HOME`` 的配置文件位置
ACCESS_FILE = os.path.join("desktop", "access-config.json")
#: 价格模式（auto = 跟随平台建议价；manual = 本机最低价）
PRICE_MODES: dict[str, str] = {"auto": "跟随平台建议价", "manual": "本机自定义最低价"}

#: reload 结果四态（唯一取值来源；文案由 :data:`RELOAD_TEXT` 映射）
RELOAD_OK = "reloaded"
RELOAD_DAEMON_DOWN = "daemon_down"
RELOAD_UNSUPPORTED = "unsupported"
RELOAD_FAILED = "failed"

RELOAD_TEXT: dict[str, str] = {
    RELOAD_OK: "daemon 已重载，配置生效。",
    RELOAD_DAEMON_DOWN: "daemon 未运行；配置已落盘，下次启动时读取。",
    RELOAD_UNSUPPORTED: "当前 daemon 无热重载命令；重启 daemon 后生效（未生效前不谎报）。",
    RELOAD_FAILED: "重载请求失败；重启 daemon 后生效。",
}

_MAX_URL = 512
_TRC20 = re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$")
_EVM = re.compile(r"^0x[0-9a-fA-F]{40}$")


# ─────────────────────────────────────────────────────────────── 掩码
def mask_secret(value: Any, *, keep: int = 4) -> str:
    """``djb_sk_…abcd`` 风格掩码；空值给「—」，短值不泄露中间字符。

    前缀取第一个下划线段（如 ``djb_sk``）；没有下划线就取前 4 位。
    """
    text = str(value or "")
    if not text:
        return "—"
    head = text.split("_", 1)[0]
    prefix = head + "_" if "_" in text and len(head) >= 2 else text[:min(4, len(text))]
    if len(text) <= keep + 1:
        return "…" + text[-2:] if len(text) > 2 else "…"
    return prefix + "…" + text[-keep:]


def mask_address(value: Any) -> str:
    """收款地址掩码：前 6 / 后 4（够核对、不够复制）。"""
    text = str(value or "").strip()
    if not text:
        return "—"
    if len(text) <= 12:
        return text[:3] + "…"
    return text[:6] + "…" + text[-4:]


# ─────────────────────────────────────────────────────────────── 校验
def validate_url(value: str, *, label: str) -> str:
    """请求地址校验：必须 http(s) 且有主机名；不做 DNS 解析（离线也能编辑）。"""
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) > _MAX_URL:
        raise ValueError(label + "过长（上限 " + str(_MAX_URL) + " 字符）")
    if not re.match(r"^https?://[^\s/]+", text):
        raise ValueError(label + "必须是 http(s):// 开头的地址")
    if any(ch in text for ch in "\r\n\t \"'<>"):
        raise ValueError(label + "含非法字符")
    return text.rstrip("/")


def validate_address(value: str) -> str:
    """USDT 收款地址：支持 TRC20（T + 33 位 base58）与 EVM（0x + 40 hex）。"""
    text = str(value or "").strip()
    if not text:
        return ""
    if _TRC20.match(text) or _EVM.match(text):
        return text
    raise ValueError("USDT 收款地址格式不对：TRC20 应为 T 开头 34 位，EVM 应为 0x 开头 42 位")


# ─────────────────────────────────────────────────────────────── 模型
@dataclass(frozen=True)
class AccessConfig:
    """一份接入配置（``node_secret`` / ``api_key`` / ``scoped_token`` 是秘密）。"""

    node_id: str = ""
    node_secret: str = ""
    api_key: str = ""
    price_mode: str = "auto"
    price_note: str = ""
    usdt_address: str = ""
    openai_base_url: str = ""
    anthropic_base_url: str = ""
    agent_downlink_url: str = ""
    scoped_token: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if self.price_mode not in PRICE_MODES:
            raise ValueError("价格模式必须是 " + " / ".join(PRICE_MODES))
        if not isinstance(self.node_id, str) or len(self.node_id) > 128:
            raise ValueError("节点标识必须是不超过 128 字符的文本")
        for name, label in (("openai_base_url", "OpenAI 请求地址"),
                            ("anthropic_base_url", "Anthropic 请求地址"),
                            ("agent_downlink_url", "Agent 下行地址")):
            validate_url(getattr(self, name), label=label)
        validate_address(self.usdt_address)
        for name in ("node_secret", "api_key", "scoped_token"):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise ValueError(name + " 必须是文本")
            if "\n" in value or "\r" in value:
                raise ValueError(name + " 不能含换行")
        if not isinstance(self.extra, dict):
            raise ValueError("extra 必须是对象")

    def display(self) -> dict[str, str]:
        """可上屏投影：秘密只给掩码，地址给半掩码，其余原样（已校验）。"""
        return {
            "node_id": self.node_id or "—",
            "node_secret": mask_secret(self.node_secret),
            "api_key": mask_secret(self.api_key),
            # 原值给编辑框（写回时不能把中文标签当枚举值存回去），中文标签单独给 hint
            "price_mode": self.price_mode,
            "price_mode_label": PRICE_MODES.get(self.price_mode, self.price_mode),
            "price_note": self.price_note or "—",
            "usdt_address": mask_address(self.usdt_address),
            "openai_base_url": self.openai_base_url or "未设置",
            "anthropic_base_url": self.anthropic_base_url or "未设置",
            "agent_downlink_url": self.agent_downlink_url or "未设置",
            "scoped_token": mask_secret(self.scoped_token),
        }

    def to_wire(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["price_mode"] = self.price_mode
        return payload

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any] | None) -> "AccessConfig":
        """tolerant reader：未知字段进 ``extra``，缺失字段取默认，不因脏文件崩溃。"""
        raw = dict(raw or {})
        known = {name for name in asdict(cls()) if name != "extra"}
        values: dict[str, Any] = {}
        for name in known:
            value = raw.get(name)
            if isinstance(value, str):
                values[name] = value.strip()
        extra = dict(raw.get("extra") or {}) if isinstance(raw.get("extra"), dict) else {}
        for key, value in raw.items():
            if key not in known and key != "extra":
                extra[key] = value
        config = cls(**values)
        config.extra.clear()
        config.extra.update(extra)
        try:
            config.validate()
        except ValueError:
            # 脏值不阻塞打开设置页：交出原值，由 UI 标红提示用户改正。
            pass
        return config


# ─────────────────────────────────────────────────────────────── 落盘
def default_access_path(home: str | os.PathLike | None = None) -> Path:
    base = Path(home or os.environ.get("DJB_HOME") or (Path.home() / ".djb")).expanduser()
    return base / ACCESS_FILE


class FileAccessStore:
    """原子写 + 回读校验 + 失败回滚（不写 store.db，避免与 daemon schema 耦合）。"""

    def __init__(self, path: str | os.PathLike | None = None) -> None:
        self._path = Path(path) if path is not None else default_access_path()

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> AccessConfig:
        if not self._path.exists():
            return AccessConfig()
        if self._path.stat().st_size > 65536:
            raise ValueError("接入配置过大，拒绝读取")
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("接入配置格式错误")
        return AccessConfig.from_wire(raw)

    def save(self, config: AccessConfig) -> AccessConfig:
        """写盘并回读；不一致则还原旧文件（有备份时）并抛 ``ValueError``。"""
        config.validate()
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup: bytes | None = None
        if self._path.exists():
            backup = self._path.read_bytes()
        payload = json.dumps(config.to_wire(), ensure_ascii=False, indent=2) + "\n"
        handle_path = None
        try:
            fd, handle_path = tempfile.mkstemp(prefix=".access-", dir=str(self._path.parent))
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(handle_path, 0o600)
            os.replace(handle_path, self._path)
            handle_path = None
            readback = self.load()
            if readback != config:
                raise ValueError("接入配置回读不一致")
            return readback
        except Exception:
            if backup is not None:
                self._path.write_bytes(backup)
            elif self._path.exists():
                self._path.unlink()
            raise
        finally:
            if handle_path and os.path.exists(handle_path):
                os.unlink(handle_path)


class MemoryAccessStore:
    """测试替身：只存内存，可注入读取/写入故障。"""

    def __init__(self, config: AccessConfig | None = None, *,
                 fail_save: bool = False, corrupt_readback: bool = False) -> None:
        self._config = config or AccessConfig()
        self._fail_save = fail_save
        self._corrupt = corrupt_readback
        self.saves = 0

    def load(self) -> AccessConfig:
        return replace(self._config)

    def save(self, config: AccessConfig) -> AccessConfig:
        config.validate()
        if self._fail_save:
            raise OSError("磁盘不可写")
        self.saves += 1
        self._config = (replace(config, node_id=config.node_id + "-x")
                        if self._corrupt else replace(config))
        if self._corrupt:
            raise ValueError("接入配置回读不一致")
        return replace(self._config)


@dataclass(frozen=True)
class SaveResult:
    """保存结果：``saved`` 与 ``readback_ok`` 分开，调用方不得只看一个。"""

    saved: bool
    readback_ok: bool
    reload: str
    message: str
    display: Mapping[str, str] = field(default_factory=dict)

    @property
    def effective(self) -> bool:
        """只有「回读一致 + 重载成功」才允许对外说已生效。"""
        return self.saved and self.readback_ok and self.reload == RELOAD_OK


class AccessService:
    """保存流程：validate → save → readback → reload → 结论文案。"""

    def __init__(self, store: Any, *, reloader: Callable[[], str] | None = None) -> None:
        self._store = store
        self._reloader = reloader

    def load(self) -> AccessConfig:
        return self._store.load()

    def save(self, config: AccessConfig) -> SaveResult:
        try:
            config.validate()
        except ValueError as exc:
            return SaveResult(False, False, RELOAD_FAILED, "未保存：" + str(exc),
                              config.display())
        try:
            self._store.save(config)
        except (OSError, ValueError):
            return SaveResult(False, False, RELOAD_FAILED,
                              "保存失败或回读不一致，已保留原配置；未生效。",
                              config.display())
        try:
            readback = self._store.load()
        except (OSError, ValueError):
            return SaveResult(True, False, RELOAD_FAILED, "已写入但回读失败；未生效。",
                              config.display())
        display = readback.display() if isinstance(readback, AccessConfig) else config.display()
        if readback != config:
            return SaveResult(True, False, RELOAD_FAILED, "回读与草稿不一致；未生效。", display)
        state = self._reload()
        message = "已保存并回读一致。" + RELOAD_TEXT[state]
        return SaveResult(True, True, state, message, display)

    def reload(self) -> str:
        """显式请求一次重载（不改配置）；返回 :data:`RELOAD_*` 取值。"""
        return self._reload()

    def _reload(self) -> str:
        if self._reloader is None:
            return RELOAD_UNSUPPORTED
        try:
            state = str(self._reloader() or "")
        except Exception:
            return RELOAD_FAILED
        return state if state in RELOAD_TEXT else RELOAD_FAILED
