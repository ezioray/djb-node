# SPDX-License-Identifier: Apache-2.0
"""F16 · 节点控制台只读接口客户端（`GET /api/v1/node/{tasks,receipts,earnings,profile}`）。

设计原则（与桌面端其余部分一致）：

1. **只读**：只发 GET，不带副作用；写操作不在本模块。
2. **凭据不进 URL / 日志 / 屏幕**：与 daemon 出站同一套头（``X-Client-Key``，见 ``protocol``）。
3. **取不到就说取不到**：任何失败都抛 :class:`ProviderError`（带**短中文原因**），
   由视图渲染成「暂不可用（原因）」；**绝不返回 0、绝不造行**。
4. **游标分页**：``page.next_cursor`` 逐页取，页数有上限（默认 5 页），
   取满上限就如实说「还有更多」，不假装拿全了。
5. **服务端降级**：响应里带 ``unavailable_reason`` 时视为该域不可用（不是空数据）。

金额口径（与 web 一致）：服务端给 ``*_minor`` 整数 = micro-USD（1e-6），
显示至少 2 位、最多 6 位小数、去尾零（``1_500_000 → 1.50``，``1_234_567 → 1.234567``）。
"""
from __future__ import annotations

import json
import os
from typing import Any, Mapping
from urllib.parse import quote

from ..httpclient import HttpClient, join_url
from ..protocol import build_headers
from .datasource import ProviderError

__all__ = ["NodeCloudClient", "micro_text", "LOGIN_HINT"]

#: 401 的下一步（给视图「暂不可用（原因）」后面那句提示用）
LOGIN_HINT = "重新登录节点后重试（djb auth --pair）"

PATHS = {
    "tasks": "/api/v1/node/tasks",
    "receipts": "/api/v1/node/receipts",
    "earnings": "/api/v1/node/earnings",
    "profile": "/api/v1/node/profile",
}

#: 一页最多取多少条（服务端可下调，客户端不硬要）
PAGE_LIMIT = 50
#: 自动翻页上限：超过就如实说「还有更多」
MAX_PAGES = 5

_ITEM_KEYS = ("items", "data", "rows", "tasks", "receipts", "list")

#: 服务端机器可读原因 → 可读中文（只有真的知道含义的才翻译，其余原样带出）
REASON_TEXT = {
    "no_usage_for_node": "该节点还没有计费记录",
    "settlement_schedule_not_configured": "结算排期尚未配置（不影响已结算金额）",
}


def micro_text(value: Any) -> str | None:
    """micro-USD 整数 → 显示文本：至少 2 位、最多 6 位、去尾零；非法值返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        minor = int(value)
    except (TypeError, ValueError):
        return None
    sign = "-" if minor < 0 else ""
    whole, frac = divmod(abs(minor), 1_000_000)
    text = f"{frac:06d}".rstrip("0")
    return f"{sign}{whole}.{text.ljust(2, '0')}"


def reason_text(code: Any) -> str:
    """服务端的机器可读原因 → 可读中文（未知码原样带出，不编故事）。"""
    text = str(code or "").strip()
    return REASON_TEXT.get(text, "服务端未给出数据（" + text + "）" if text else "")


def _seconds(start: Any, end: Any) -> int | None:
    """两个 ISO 时刻的秒差；解析不了就给 None（不猜）。"""
    if not isinstance(start, str) or not isinstance(end, str):
        return None
    from datetime import datetime
    try:
        first = datetime.fromisoformat(start.replace("Z", "+00:00"))
        second = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0, int((second - first).total_seconds()))


def _task_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """任务行：补 tokens 合计与耗时（视图直接用），其余字段原样保留。"""
    tokens_in, tokens_out = row.get("tokens_in"), row.get("tokens_out")
    total = None
    if tokens_in is not None or tokens_out is not None:
        total = int(tokens_in or 0) + int(tokens_out or 0)
    state = str(row.get("state") or "")
    return {**row,
            "id": row.get("task_id") or row.get("request_id") or row.get("id"),
            # recorded=已记录未结算，按「在途」展示；settled/corrected=已结算
            "state": ("inflight" if row.get("inflight") else
                      "done" if state in ("settled", "corrected") else state),
            "state_raw": state,
            "tokens": total,
            "elapsed_s": _seconds(row.get("started_at"), row.get("finished_at")),
            "amount": micro_text(row.get("amount_micro"))}


def _receipt_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """收据行：服务端 state=accepted/rejected/expired，映射成视图的 verified/failed。"""
    state = str(row.get("state") or "")
    status = ("verified" if state == "accepted"
              else "failed" if state in ("rejected", "expired") else state)
    return {**row, "status": status, "state_raw": state,
            "created_at": row.get("issued_at"),
            "amount": micro_text(row.get("revenue_minor"))}


_ROW_MAPPERS = {"tasks": _task_row, "receipts": _receipt_row}


def _items(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    for key in _ITEM_KEYS:
        rows = payload.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, Mapping)]
    return []


def _next_cursor(payload: Mapping[str, Any]) -> str | None:
    page = payload.get("page")
    for source in (page, payload):
        if isinstance(source, Mapping):
            value = source.get("next_cursor")
            if isinstance(value, str) and value:
                return value
    return None


class NodeCloudClient:
    """节点控制台只读客户端。``transport`` 可注入（单测不需要真网络）。"""

    def __init__(self, base_url: str, client_key: str | None, *,
                 http: Any | None = None, timeout: float = 6.0,
                 page_limit: int = PAGE_LIMIT, max_pages: int = MAX_PAGES,
                 transport: Any | None = None) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self._client_key = client_key
        self._http = http or HttpClient()
        self._timeout = float(timeout)
        self.page_limit = int(page_limit)
        self.max_pages = int(max_pages)
        self._transport = transport

    # ─────────────────────────────────────────────────────── 装配
    @classmethod
    def from_home(cls, home: str | os.PathLike | None, *,
                  transport: Any | None = None) -> "NodeCloudClient | None":
        """从 ``~/.djb`` 读网关地址与节点密钥；没登录过节点就返回 ``None``（不是报错）。"""
        from ..config import Paths, load_config
        from ..secure import CredentialStore

        paths = Paths(str(home) if home else os.path.expanduser("~/.djb"))
        try:
            config = load_config(paths)
            key = CredentialStore(paths).client_key()
        except Exception:                     # 配置坏了不该让窗口打不开
            return None
        if not key or not config.gateway_url:
            return None
        return cls(config.gateway_url, key, transport=transport)

    # ─────────────────────────────────────────────────────── 取数
    def _get(self, kind: str, *, cursor: str | None = None,
             limit: int | None = None) -> Mapping[str, Any]:
        params = []
        if limit:
            params.append("limit=" + str(int(limit)))
        if cursor:
            params.append("cursor=" + quote(str(cursor), safe=""))
        path = PATHS[kind] + ("?" + "&".join(params) if params else "")
        if self._transport is not None:
            return self._transport(kind, {"cursor": cursor, "limit": limit})
        headers = build_headers(client_key=self._client_key)
        url = join_url(self.base_url, path)
        try:
            response = self._http.request("GET", url, headers=headers, timeout=self._timeout)
        except Exception as exc:              # 网络层：只说类型，不把 URL 抛给界面
            raise ProviderError("云端连不上（" + type(exc).__name__ + "）") from None
        status = int(getattr(response, "status", 0) or 0)
        if status == 401:
            raise ProviderError("登录已过期（401）", hint=LOGIN_HINT)
        if status == 403:
            raise ProviderError("节点无权访问该数据（403）", hint=LOGIN_HINT)
        if status == 404:
            # profile 对不存在的节点也回 404：不能一律说成「接口没上线」
            raise ProviderError("云端返回 404（接口未上线或节点不存在）",
                                hint="确认节点已配对；接口未上线时等平台发布")
        if status >= 500:
            raise ProviderError("云端暂时不可用（" + str(status) + "）", hint="稍后重试")
        if status >= 400:
            raise ProviderError("请求被拒绝（" + str(status) + "）")
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError):
            raise ProviderError("云端返回不是 JSON") from None
        if not isinstance(payload, Mapping):
            raise ProviderError("云端返回结构异常")
        return payload

    def fetch(self, kind: str) -> dict[str, Any]:
        """按 ``page.next_cursor`` 翻页聚合；返回 ``{items, next_cursor, pages, truncated}``。"""
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        pages = 0
        while pages < self.max_pages:
            payload = self._get(kind, cursor=cursor, limit=self.page_limit)
            pages += 1
            mapper = _ROW_MAPPERS.get(kind)
            rows = _items(payload)
            items.extend([mapper(row) for row in rows] if mapper else rows)
            cursor = _next_cursor(payload)
            if not cursor:
                break
        return {"items": items, "next_cursor": cursor, "pages": pages,
                "truncated": bool(cursor)}

    def list_tasks(self, *, cursor: str | None = None,
                   limit: int | None = None) -> dict[str, Any]:
        payload = self._get("tasks", cursor=cursor, limit=limit)
        return {"items": [_task_row(row) for row in _items(payload)],
                "next_cursor": _next_cursor(payload)}

    def list_receipts(self, *, cursor: str | None = None,
                      limit: int | None = None) -> dict[str, Any]:
        payload = self._get("receipts", cursor=cursor, limit=limit)
        return {"items": [_receipt_row(row) for row in _items(payload)],
                "next_cursor": _next_cursor(payload)}

    def earnings(self) -> dict[str, Any]:
        """归一化收益：``*_minor`` 是 micro-USD，另给显示文本（口径与 web 一致）。

        服务端在「查不到」时给 ``null`` + ``unavailable_reason``；有数据但排期未配置时
        也会带原因——所以这里的规则是：**没有任何金额与明细才判整域不可用**，
        否则照常展示并把原因降级成 note。
        """
        payload = self._get("earnings")
        settled_minor = payload.get("settled_minor")
        pending_minor = payload.get("pending_minor")
        today_minor = payload.get("today_minor")
        balance_minor = payload.get("balance_minor")
        rows = [row for row in (payload.get("rows") or []) if isinstance(row, Mapping)]
        reason = reason_text(payload.get("unavailable_reason"))
        if settled_minor is None and pending_minor is None and not rows:
            raise ProviderError(reason or "服务端没有返回收益数据",
                                hint="等首笔计费结算后再刷新")
        payouts = [{
            "id": row.get("request_id") or row.get("task_id") or "—",
            "task_id": row.get("task_id"),
            "amount": micro_text(row.get("amount_micro")),
            "currency": str(payload.get("currency") or "USDT"),
            "status": str(row.get("state") or "—"),
            "created_at": row.get("recorded_at") or row.get("settled_at") or "—",
        } for row in rows]
        return {
            "currency": str(payload.get("currency") or "USDT"),
            "settled_minor": settled_minor, "pending_minor": pending_minor,
            "corrected_minor": payload.get("corrected_minor"),
            "today_minor": today_minor, "balance_minor": balance_minor,
            "settled": micro_text(settled_minor),
            "pending": micro_text(pending_minor),
            "corrected": micro_text(payload.get("corrected_minor")),
            "today": micro_text(today_minor),
            "balance": micro_text(balance_minor),
            "next_settlement_at": payload.get("next_settlement_at"),
            "note": reason,
            "payouts": payouts,
        }

    def profile(self) -> dict[str, Any]:
        """节点身份（服务端 404 = 节点不存在/无权查看，不编占位值）。"""
        payload = self._get("profile")
        capabilities = payload.get("capabilities")
        return {
            "node_id": payload.get("node_id"),
            "state": payload.get("state"),
            "region": payload.get("region"),
            "platform": payload.get("platform"),
            "upstream_kind": payload.get("upstream_kind"),
            "capabilities": [str(item) for item in (capabilities or [])
                             if isinstance(item, str)],
            "bound": payload.get("bound"),
            "last_seen_at": payload.get("last_seen_at"),
            # 兼容旧/演示形状（有就给，没有就是 None）
            "email": payload.get("email"),
            "role": payload.get("role"),
            "twofa_enabled": payload.get("twofa_enabled", payload.get("mfa_enabled")),
        }
