# SPDX-License-Identifier: Apache-2.0
"""S4 客户端代理偏好（M15-TR1）：只构造可观测口径，不实现路由。

卡片约束：客户端只暴露 ``composition=single`` + 四档 ``effort``；``auto`` 仅在
``single`` 下允许。这里做纯函数校验与请求字段构造，便于 GUI / CLI / 上层复用；
不读写凭据、不联网、不改 daemon 配置。

缺省（不传任何偏好）时 :func:`build_chat_request` 返回与输入等价的 dict，
不添加 ``djb_*`` 字段——旧请求逐字节不变。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .errors import UsageError

COMPOSITIONS = ("single", "mono_pair", "hetero_pair")
EFFORTS = ("minimal", "low", "medium", "high")
EFFORT_VALUES = EFFORTS + ("auto",)
PROXY_MODES = ("fidelity", "balanced", "max_save")

_AUTO_PAIR_MESSAGE = "djb_effort=auto 仅在 single 形态可用；mono_pair / hetero_pair 必须显式选四档之一"


@dataclass(frozen=True)
class ProxyPreferences:
    """客户端可选偏好；``None`` 表示不发送该字段（走服务端缺省）。"""

    effort: str | None = None
    composition: str = "single"
    proxy_mode: str | None = None

    def validate(self) -> None:
        if self.composition not in COMPOSITIONS:
            raise UsageError("未知 djb_composition：" + str(self.composition),
                             details={"allowed": list(COMPOSITIONS)})
        if self.effort is not None and self.effort not in EFFORT_VALUES:
            raise UsageError("未知 djb_effort：" + str(self.effort),
                             details={"allowed": list(EFFORT_VALUES)})
        if self.proxy_mode is not None and self.proxy_mode not in PROXY_MODES:
            raise UsageError("未知 djb_proxy_mode：" + str(self.proxy_mode),
                             details={"allowed": list(PROXY_MODES)})
        if self.effort == "auto" and self.composition != "single":
            raise UsageError(_AUTO_PAIR_MESSAGE,
                             details={"composition": self.composition, "effort": "auto"})

    def fields(self) -> dict[str, str]:
        """只返回**显式**要发送的字段；缺省不填，避免改变旧请求。"""
        self.validate()
        out: dict[str, str] = {}
        if self.composition != "single":
            out["djb_composition"] = self.composition
        if self.effort is not None:
            out["djb_effort"] = self.effort
        if self.proxy_mode is not None:
            out["djb_proxy_mode"] = self.proxy_mode
        return out

    def describe(self) -> dict[str, str]:
        """人类可读的生效口径；不编造省钱比例或账单金额。"""
        self.validate()
        return {
            "composition": self.composition,
            "effort": self.effort or "auto（服务端缺省）",
            "proxy_mode": self.proxy_mode or "fidelity（服务端缺省）",
        }


def build_chat_request(payload: Mapping[str, Any],
                       prefs: ProxyPreferences | None = None) -> dict[str, Any]:
    """复制请求并叠加显式偏好；不修改入参，不添加缺省 ``djb_*`` 字段。"""
    if not isinstance(payload, Mapping):
        raise UsageError("请求体必须是 JSON 对象")
    out = dict(payload)
    if prefs is None:
        return out
    out.update(prefs.fields())
    return out
