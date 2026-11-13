# SPDX-License-Identifier: Apache-2.0
"""F16 · 节点控制台只读接口客户端（`GET /api/v1/node/{tasks,receipts,earnings,profile}`）。

设计原则（与桌面端其余部分一致）：

1. **以只读为主**：除 `submit_payout_address`（USDT 收款地址提交，owner 2026-11-13 方案 B）
   之外全是 GET；写操作**只有这一处**，且只由 worker 线程调用（见 `desktop/app.py`）。
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
from ..util import new_request_id
from .datasource import ProviderError

__all__ = ["NodeCloudClient", "micro_text", "LOGIN_HINT"]

#: 401 的下一步（给视图「暂不可用（原因）」后面那句提示用）
LOGIN_HINT = "重新登录节点后重试（djb auth --pair）"

PATHS = {
    "tasks": "/api/v1/node/tasks",
    "receipts": "/api/v1/node/receipts",
    "earnings": "/api/v1/node/earnings",
    "profile": "/api/v1/node/profile",
    "receipt": "/api/v1/node/receipts/",       # 单条：+ receipt_id
    "node_balance": "/api/v1/node/balance",    # 节点绑定账户的可用余额（节点钥可读）
    "binding": "/api/v1/node/binding",         # 节点绑定状态（只读 GET，?node_id=）
    # USDT 收款地址（owner 2026-11-13 方案 B：客户端直接提交云端）。
    # 路径/字段按 cloud 侧契约逐字对齐；**云端只收 Polygon（EVM）**，见 payout.CHAIN_HINT。
    "payout_address": "/api/v1/node/payout-address",
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


#: 云端状态原词 → 中文（只翻译，不改写；未知词一律原样显示并标「未知」）
STATE_TEXT = {
    "recorded": "已记录", "settled": "已结算", "corrected": "已更正",
    "accepted": "已受理", "rejected": "已拒绝", "expired": "已过期",
    "failed": "失败", "error": "错误", "running": "进行中", "done": "已完成",
}


def state_text(state: str) -> str:
    """状态原词的中文说法；没登记的词返回空串（调用方负责如实显示原词）。"""
    return STATE_TEXT.get(str(state or ""), "")


def _task_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """任务行：补 tokens 合计与耗时（视图直接用），其余字段原样保留。"""
    tokens_in, tokens_out = row.get("tokens_in"), row.get("tokens_out")
    total = None
    if tokens_in is not None or tokens_out is not None:
        total = int(tokens_in or 0) + int(tokens_out or 0)
    state = str(row.get("state") or "")
    return {**row,
            "id": row.get("task_id") or row.get("request_id") or row.get("id"),
            # 云端状态原样保留（recorded/settled/…），在途与否由 inflight 单独表达——
            # 用 inflight 顶替 state 会把「已记录」这个原词吞掉。
            "state": state,
            "state_text": state_text(state),
            "inflight": bool(row.get("inflight")),
            "tokens": total,
            "elapsed_s": _seconds(row.get("started_at"), row.get("finished_at")),
            "amount": micro_text(row.get("amount_micro"))}


def _receipt_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """收据行：``state=accepted/rejected/expired`` 是**受理结果**，不是本机校验结论。

    以前把 accepted 翻成 verified：本机一次校验都没做，却在界面上写「已验证」——
    所以现在原词保留（``state``），中文另给（``state_text``），校验结论只由本机比对得出。
    """
    state = str(row.get("state") or "")
    # 列表行的时间字段是 ``issued_at``（服务端由 DB 的 received_at 映射而来）；
    # 单条详情端点才叫 ``received_at``。按错字段读，列表时间会一直是空。
    issued = row.get("issued_at") or row.get("received_at")
    # 金额：``revenue_micro`` 是正式字段，``revenue_minor`` 同名同值但已标 deprecated
    revenue = row.get("revenue_micro")
    if revenue is None:
        revenue = row.get("revenue_minor")
    return {**row, "state": state, "state_text": state_text(state),
            "rejected_code": row.get("rejected_code"),
            "reject_reason": row.get("reject_reason"),
            "issued_at": issued, "created_at": issued,
            "amount": micro_text(revenue)}


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


def _payout_error(response: Any, status: int) -> ProviderError:
    """收款地址接口的错误信封 → **可执行**中文（401/403/409/422 各自给下一步）。

    判定口径（cloud-payout 2026-11-13 契约）：先看 ``error.code``（40913 链不支持 /
    40919 冷却 / 42210 准入 / 42211 风险 / 40001 格式），``error.reason`` 兜底；
    两条都没给就退回状态码级别的话术——**不猜具体原因**。
    """
    code: int | None = None
    reason = ""
    field_name = ""
    try:
        raw = response.error_code()
        code = raw if isinstance(raw, int) else None
    except Exception:                      # pragma: no cover - 伪 response
        code = None
    try:
        payload = response.json()
    except Exception:                      # pragma: no cover - 伪 response
        payload = None
    if isinstance(payload, Mapping):
        error = payload.get("error")
        if isinstance(error, Mapping):
            raw_reason = error.get("reason")
            if isinstance(raw_reason, str):
                reason = raw_reason.strip()
            if isinstance(error.get("field"), str):
                field_name = error["field"].strip()
            if code is None and isinstance(error.get("code"), int):
                code = int(error["code"])
    by_code = {
        40101: ("节点凭据无效或缺失（401）",
                "先在网页端完成节点绑定（配对会刷新节点密钥），再回来提交"),
        40305: ("节点还没绑定账户（403）", "先在网页端完成节点绑定，再回来提交收款地址"),
        40401: ("服务未开放（404）",
                "云端收款地址功能还没开启，等平台开放后再试；地址未变更（不用反复重试）"),
        40913: ("云端只支持 Polygon（ERC20）链上 USDT 收款地址，TRC20 暂不支持（409）",
                "把地址改成 0x 开头 42 位后重试"),
        40919: ("收款地址 24 小时内只能改一次，冷却未过（409）",
                "等冷却结束再改（「云端状态」里会显示可再改时间）"),
        42210: ("该地址未通过准入校验（422）", "确认这是你本人在 Polygon 上的收款地址后重试"),
        42211: ("云端风控判定该地址风险过高（422）", "换一个地址；有疑问联系平台客服"),
        40001: ("云端判定收款地址格式不对（400）", "按 0x 开头 42 位填写；TRC20 暂不支持"),
    }
    # ``reason`` 优先：40919 同时用于「24h 冷却」与「幂等冲突」，只看 code 会把后者
    # 说成「等冷却」——那会让客户白等 24 小时（cloud-payout 2026-11-13 明确提醒）。
    by_reason = {
        "chain_unsupported": by_code[40913],
        "address_cooldown": by_code[40919],
        "node_not_bound": by_code[40305],
        "feature_disabled": by_code[40401],
        "idempotency_conflict": (
            "提交被云端判为重复请求（409/40919 幂等冲突）",
            "再点一次「提交到云端」（客户端每次都会换新的幂等键）；地址未变更"),
    }
    if reason in by_reason:
        reason_text, hint = by_reason[reason]
        return ProviderError(reason_text, hint=hint)
    if code in by_code:
        reason_text, hint = by_code[code]
        if code == 40001 and field_name == "body":
            return ProviderError("云端拒收了请求内容（400）",
                                 hint="客户端只发 address / label；请把这个情况报给平台（是 bug）")
        return ProviderError(reason_text, hint=hint)
    if status == 401:
        return ProviderError("节点凭据无效或缺失（401）", hint=by_code[40101][1])
    if status == 403:
        return ProviderError("节点还没绑定账户（403）", hint=by_code[40305][1])
    if status == 404:
        return ProviderError("服务未开放（404）",
                             hint="云端还没开放收款地址接口，等平台开启后再试；地址未变更")
    if status == 409:
        return ProviderError("收款地址暂时不能改（409）",
                             hint="先看「云端状态」里的冷却时间，再重试")
    if status == 422:
        return ProviderError("云端拒绝了这个地址（422）", hint="确认地址归属后重试")
    if status == 429:
        return ProviderError("提交太频繁（429）", hint="稍后重试")
    if status >= 500:
        return ProviderError("云端暂时不可用（" + str(status) + "）",
                             hint="稍后重试；地址未变更")
    return ProviderError("提交被拒绝（" + str(status) + "）",
                         hint="看云端返回的 message；地址未变更")


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

        # home=None 时用平台解析（Windows = %APPDATA%\DJB）：写死 ~/.djb 会读到空配置
        # → 整块云端数据「未登录节点」，而本机明明已经注册过（S3 同一类 root cause）。
        from ..config import resolve_home
        paths = Paths(os.fspath(home) if home else resolve_home())
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
        return self._request_json(kind, path)

    def _request_json(self, kind: str, path: str, *,
                      headers: Mapping[str, str] | None = None) -> Mapping[str, Any]:
        """一次 GET → JSON；失败一律映射成可读 ``ProviderError``（不含 URL / 令牌）。"""
        if self._transport is not None:
            return self._transport(kind, {})
        return self._request("GET", path, kind=kind, headers=headers)

    def _request(self, method: str, path: str, *, kind: str,
                 json_body: Any | None = None,
                 headers: Mapping[str, str] | None = None,
                 idempotency_key: str | None = None) -> Mapping[str, Any]:
        """一次请求 → JSON。GET / POST 共用同一套状态码口径（只加不改）。

        ``kind`` 只用于**错误文案分域**：``payout_address`` 的 401/403/409/422 有专门的
        可执行文案（节点没绑定账户 / 冷却中 / 链不支持 / 风控拒），其余域维持老口径。
        """
        request_headers = dict(headers) if headers else build_headers(
            client_key=self._client_key, idempotency_key=idempotency_key)
        url = join_url(self.base_url, path)
        try:
            if json_body is None:
                response = self._http.request(method, url, headers=request_headers,
                                              timeout=self._timeout)
            else:
                response = self._http.request(method, url, headers=request_headers,
                                              json_body=json_body, timeout=self._timeout)
        except Exception as exc:              # 网络层：只说类型，不把 URL 抛给界面
            raise ProviderError("云端连不上（" + type(exc).__name__ + "）") from None
        status = int(getattr(response, "status", 0) or 0)
        if kind == "payout_address" and status >= 400:
            raise _payout_error(response, status)
        if status == 401:
            raise ProviderError("登录已过期（401）", hint=LOGIN_HINT)
        if status == 403:
            raise ProviderError("节点无权访问该数据（403）", hint=LOGIN_HINT)
        if status == 404:
            # 单条收据 / profile 对「不存在或不属于本节点」也回 404：不谎称接口没上线
            if kind == "receipt":
                raise ProviderError("云端没有这条收据（404）",
                                    hint="确认收据号；跨节点收据不会返回")
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

    # ─────────────────────────────────────────────────────── USDT 收款地址（方案 B）
    def payout_address(self) -> dict[str, Any]:
        """读云端已登记的收款地址（``GET``，只读）。

        契约：没有登记过 → ``data: null``。这里统一归一成**空 dict**，让上层
        「空 = 尚未提交」与「读不到 = 抛 ProviderError」两件事分开（不混成同一句文案）。
        """
        if self._transport is not None:
            payload = self._transport("payout_address", {})
        else:
            payload = self._request("GET", PATHS["payout_address"], kind="payout_address")
        data = payload.get("data") if isinstance(payload, Mapping) else None
        return dict(data) if isinstance(data, Mapping) else {}

    def submit_payout_address(self, address: str, label: str = "") -> dict[str, Any]:
        """提交收款地址（``POST``，**唯一的写操作**；只在 worker 线程调用）。

        * 带 ``X-DJB-Idempotency-Key``（cloud 侧两个幂等头都收，本仓既有头名优先）——
          网络超时后客户再点一次不会变成两条变更记录；
        * **不自动重试**：写操作重试会撞 24h 冷却（409/40919），失败由用户点按钮重试；
        * 只发 ``0x`` 地址（TRC20 由 ``payout.submit_blocker`` 在本地拦掉，不到云端）。
        """
        body: dict[str, Any] = {"address": str(address).strip()}
        text_label = str(label or "").strip()
        if text_label:
            body["label"] = text_label
        if self._transport is not None:
            payload = self._transport("payout_submit", dict(body))
        else:
            payload = self._request("POST", PATHS["payout_address"], kind="payout_address",
                                    json_body=body, idempotency_key=new_request_id())
        data = payload.get("data") if isinstance(payload, Mapping) else None
        return dict(data) if isinstance(data, Mapping) else {}

    def fetch(self, kind: str) -> dict[str, Any]:
        """按 ``page.next_cursor`` 翻页聚合；返回 ``{items, next_cursor, pages, truncated}``。"""
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        pages = 0
        page_error = ""
        while pages < self.max_pages:
            try:
                payload = self._get(kind, cursor=cursor, limit=self.page_limit)
            except ProviderError as exc:
                if pages == 0:
                    raise                     # 第一页就失败：整个域不可用，如实上报
                # 后面的页被拒（云端游标坏 / 400 / 422）：已拿到的照常显示，
                # 但要标成 truncated 并给出原因——不能假装「就这么多」。
                page_error = exc.reason
                break
            pages += 1
            mapper = _ROW_MAPPERS.get(kind)
            rows = _items(payload)
            items.extend([mapper(row) for row in rows] if mapper else rows)
            cursor = _next_cursor(payload)
            if not cursor:
                break
        return {"items": items, "next_cursor": cursor, "pages": pages,
                "truncated": bool(cursor) or bool(page_error),
                "page_error": page_error}

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
        # features 把「名为 _minor、实为 micro-USD」的字段改成 *_micro（旧的标 deprecated）：
        # 先认新名，旧名兜底，避免过渡期读空。数值一律按 micro-USD 解释，绝不当「分」。
        def amount(*names: str) -> Any:
            for name in names:
                if name in payload:
                    return payload.get(name)
            return None

        settled_minor = amount("settled_micro", "settled_minor")
        pending_minor = amount("pending_micro", "pending_minor")
        corrected_minor = amount("corrected_micro", "corrected_minor")
        today_minor = amount("today_micro", "today_minor", "settled_today_micro")
        balance_minor = amount("balance_micro", "balance_minor")
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
            "state": str(row.get("state") or ""),
            # 中文 + 原词一起给：原词是契约里的状态词，翻译只是方便读
            "status": ((state_text(str(row.get("state") or "")) + "（"
                        + str(row.get("state")) + "）")
                       if state_text(str(row.get("state") or ""))
                       else str(row.get("state") or "—")),
            "created_at": row.get("recorded_at") or row.get("settled_at") or "—",
            # features 已加性补上：导出 CSV 的「收据 / Token」两列不再只能写「—」
            "receipt_id": row.get("receipt_id"),
            "tokens": row.get("tokens"),
        } for row in rows]
        return {
            "currency": str(payload.get("currency") or "USDT"),
            "settled_minor": settled_minor, "pending_minor": pending_minor,
            "corrected_minor": corrected_minor,
            "today_minor": today_minor, "balance_minor": balance_minor,
            "settled": micro_text(settled_minor),
            "pending": micro_text(pending_minor),
            "corrected": micro_text(corrected_minor),
            "today": micro_text(today_minor),
            "balance": micro_text(balance_minor),
            "next_settlement_at": payload.get("next_settlement_at"),
            "note": reason,
            "payouts": payouts,
        }

    def receipt(self, receipt_id: str) -> dict[str, Any]:
        """单条收据的云端受理结果（校验用）；不存在/非本节点 → 404 → 可读原因。"""
        ident = str(receipt_id or "").strip()
        if not ident:
            raise ProviderError("收据号为空")
        payload = self._request_json("receipt", PATHS["receipt"] + quote(ident, safe=""))
        return dict(payload)

    def node_balance(self) -> dict[str, Any]:
        """节点绑定账户的可用余额（micro-USD）。三态严格区分，不互相冒充。

        * 有余额/账单为 0 → ``balance`` 文本（0 是事实，账本明说没充过值）；
        * 未绑定账户 → ``ProviderError("节点未绑定账户…")``（**不是 0**）；
        * 取不到 → ``ProviderError(原因)``。
        """
        payload = self._request_json("node_balance", PATHS["node_balance"])
        minor = payload.get("available_minor")
        if minor is None:
            reason = str(payload.get("unavailable_reason") or "")
            if reason == "node_not_bound":
                raise ProviderError("节点未绑定账户",
                                    hint="在服务端把节点绑定到账户后可见")
            raise ProviderError(reason_text(reason) if reason else "云端没有给出可用余额",
                                hint="稍后刷新")
        return {"balance_minor": int(minor), "balance": micro_text(minor),
                "as_of": payload.get("as_of"),
                "note": reason_text(payload.get("note")) if payload.get("note") else ""}

    def binding(self, node_id: str) -> dict[str, Any]:
        """节点绑定状态（只读）：``GET /api/v1/node/binding?node_id=…``。

        与 :meth:`djb_node.api.NodeApi.binding` 同一个端点、同一套返回字段。桌面端历史上
        **没有**这个方法，而 ``_access_section`` 一直在调 ``_cloud_call("binding", …)`` →
        ``getattr(cloud, "binding", None)`` 永远 None → 绑定卡恒为
        「暂不可用（云端接口未接线（binding））」。属于接口缺口的补齐，不改任何既有语义。
        """
        if not str(node_id or "").strip():
            raise ProviderError("还没有节点 ID：先完成注册", hint="节点进程起来后自动注册")
        path = PATHS["binding"] + "?node_id=" + quote(str(node_id), safe="")
        payload = self._request_json("binding", path)
        return dict(payload) if isinstance(payload, Mapping) else {}

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
