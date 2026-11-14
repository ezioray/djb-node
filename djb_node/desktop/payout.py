# SPDX-License-Identifier: Apache-2.0
"""USDT 收款地址 · 云端状态口径（纯投影：无 Tk / 无网络 / 无 IO）。

owner 2026-11-13 决策（**方案 B**）：收款地址由客户端**直接提交云端**，不是只存本地。
契约（与 cloud 侧并行实现，字段名按此对齐，客户端不自创字段）：

    POST /api/v1/node/payout-address    Header: X-Client-Key: <节点密钥 node_secret>
      Body {"address": "T… 或 0x…", "label": "可选"}
      200 {"data": {"address", "status", "cooldown_until"}, "request_id": "…"}
      401 缺/错节点凭据 · 403 节点未绑定账户 · 422 地址格式不合法 · 409 冷却期内不可再改

    GET  /api/v1/node/payout-address    Header: X-Client-Key
      200 {"data": {"address", "status", "cooldown_until", "updated_at"} | null | {}}

三条硬规矩（每条都有测试钉住；**别为了「好看」放宽**）：

1. **拉不到就说拉不到**：未知一律 :data:`UNREADABLE`（「暂时读不到云端状态」），
   绝不把「不知道」渲染成「已生效」——收款地址写错 = 钱打到别人账上。
2. **空数据不是错误**：``data`` 为 ``null`` / ``{}`` = 还没提交过，文案是
   :data:`UNSUBMITTED`（「尚未提交」），而不是「取数失败」。
3. **原词优先**：没登记的状态词原样带出并标「未知状态（原词）」，不翻译成猜的意思；
   冷却中（``cooldown_until`` 在将来）优先显示，因为它解释了「为什么这次改不了」。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from .access import mask_address

__all__ = ["UNREADABLE", "UNSUBMITTED", "STATUS_TEXT", "CHAIN_HINT", "cooldown_active",
           "short_state", "status_text", "submit_blocker", "submit_message", "summarize"]

#: 云端**只收 Polygon（EVM）** 地址。理由（cloud-payout 2026-11-13 实测，三道硬闸）：
#: ① 服务端 ``CHAIN_ID=polygon``；② 表约束 ``payout_addresses_chain_ck CHECK (chain_id
#: IN ('polygon'))``；③ 入库前 ``address.lower()`` 会把 base58 的大小写抹平 → TRC20 地址
#: 必然写坏。所以客户端在「提交」这条路上只放行 ``0x`` + 40 hex，**TRC20 直接本地拦**，
#: 不把请求打到云端等 409（客户会以为「重试就行」）。
#: 本地 ``validate_address()`` 仍保留 TRC20 分支（老配置兼容、只加不改），两条路互不影响。
CHAIN_HINT = "Polygon / ERC20，0x 开头 42 位；TRC20 暂不支持"

#: 云端状态原词 → 中文（只翻译，不改写；未知词由 :func:`summarize` 原样带出）
STATUS_TEXT: dict[str, str] = {
    "pending": "待审核",
    "whitelisted": "已生效",
    "rejected": "已拒绝",
    # cloud-payout 契约里还有 revoked：白名单被撤销（通常是风控/换绑），要重新提交
    "revoked": "已撤销",
}

#: 还没提交过（``data`` 为 null / {}）——不是错误
UNSUBMITTED = "尚未提交"
#: 读不到云端状态（网络 / 401 / 接口未上线）——**不许**替换成「已生效」
UNREADABLE = "暂时读不到云端状态"


def status_text(status: Any) -> str:
    """状态原词 → 中文；空值 / 未登记原词返回空串（调用方负责如实显示原词）。"""
    return STATUS_TEXT.get(str(status or "").strip().lower(), "")


def _moment(value: Any) -> datetime | None:
    """ISO8601 → aware datetime；解析不了返回 None（不猜时区）。"""
    text = str(value or "").strip()
    if not text:
        return None
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:                       # 服务端漏时区：按 UTC 读（契约口径）
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def cooldown_active(data: Mapping[str, Any] | None, *, now: datetime | None = None) -> bool:
    """``cooldown_until`` 是否在未来（= 现在不能再改）。解析不了按「没有冷却」处理。"""
    if not isinstance(data, Mapping):
        return False
    until = _moment(data.get("cooldown_until"))
    if until is None:
        return False
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return until > moment


def submit_blocker(address: Any) -> str:
    """「提交到云端」之前的**链闸**：返回空串 = 可以提交；否则返回可执行文案。

    只放行 ``0x`` + 40 hex（Polygon / ERC20）。TRC20（``T`` 开头）在这里被拦下并在本地
    toast 说明，**不发请求**——云端对 TRC20 一定回 409/422，客户会误以为「再点几次就好」。
    格式本身（长度、hex）由 :func:`djb_node.desktop.access.validate_address` 先判，
    这里只判链，两句文案不重复。
    """
    text = str(address or "").strip()
    if not text:
        return "先填 USDT 收款地址（" + CHAIN_HINT + "）"
    if not text.startswith("0x"):
        return "TRC20 暂不支持，请填 Polygon（ERC20）地址（0x 开头 42 位）"
    return ""


def short_state(data: Mapping[str, Any] | None, *, now: datetime | None = None) -> str:
    """**短**状态词（卡片上屏用）：未提交 / 待审核 / 已生效 / 已拒绝 / 冷却中 / 未知状态。

    ``data is None`` = 这一轮没读到（网络 / 401 / 接口未上线）→ :data:`UNREADABLE`。
    冷却中优先：它是「这次为什么改不了」的直接答案，压在审核状态之前说。
    """
    if data is None:
        return UNREADABLE
    address = str(data.get("address") or "").strip()
    status = str(data.get("status") or "").strip().lower()
    if cooldown_active(data, now=now):
        return "冷却中"
    if not address and not status:
        return UNSUBMITTED
    label = STATUS_TEXT.get(status, "")
    if not status:
        return UNREADABLE                     # 有地址没状态：不替服务端编状态
    if not label:
        return "未知状态（" + status + "）"
    return label


def summarize(data: Mapping[str, Any] | None, *, now: datetime | None = None) -> str:
    """一行「云端状态」文案（接入页字段 hint 用，细节比 :func:`short_state` 多）。

    ``data`` 为 ``None`` 表示**这一轮没读到**（网络失败 / 未登录节点 / 接口未上线）；
    ``data`` 为空 dict / 全空字段表示**云端确实没有地址**（还没提交过）。两者不能混。
    """
    if data is None:
        return "云端：" + UNREADABLE
    address = str(data.get("address") or "").strip()
    status = str(data.get("status") or "").strip().lower()
    if not address and not status:
        return "云端：" + UNSUBMITTED + "（填好后点「提交到云端」）"
    parts = [mask_address(address)] if address else []
    if cooldown_active(data, now=now):
        parts.append("冷却中（这次改动要等冷却结束，可再改时间 "
                     + str(data.get("cooldown_until")) + "）")
    elif status == "rejected":
        parts.append("已拒绝（改地址后重新提交）")
    elif status == "revoked":
        parts.append("已撤销（白名单被撤销，需重新提交）")
    elif status and not STATUS_TEXT.get(status):
        parts.append("未知状态（" + status + "）")
    elif status:
        parts.append(STATUS_TEXT[status])
    else:
        parts.append(UNREADABLE)
    return "云端：" + "．".join(parts)


def submit_message(data: Mapping[str, Any] | None) -> str:
    """提交成功后的 toast 文案：只复述服务端回的状态，不写「已生效」。"""
    row = data if isinstance(data, Mapping) else {}
    status = str(row.get("status") or "").strip().lower()
    if status == "whitelisted":
        return "已提交到云端：收款地址已生效。"
    if status == "pending":
        return "已提交到云端：待审核（审核通过后生效，页面会显示审核结果）。"
    if status == "rejected":
        return "已提交到云端，但云端判定「已拒绝」：按提示改地址后重新提交。"
    if status == "revoked":
        return "已提交到云端，但云端状态是「已撤销」：确认地址归属后重新提交。"
    return "已提交到云端：云端未返回状态，点「刷新」核对（不替你判断是否生效）。"
