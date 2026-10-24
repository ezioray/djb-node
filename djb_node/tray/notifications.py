# SPDX-License-Identifier: Apache-2.0
"""C3 消息中心投影：``NotificationStore.list()`` → 托盘角标数据（**不读正文**）。

输出形状（稳定，壳直接消费）::

    {"unread": 3, "p0_unread": 1,
     "latest": [{"id": "n-1", "severity": "P0", "title": "配额已用尽",
                 "created_at": "2026-10-24T00:00:00Z"}, ...]}

设计边界
========

* **只取计数与标题**：``payload`` / 正文 / 凭据一律不进入投影；``latest`` 每条只有四个字段。
* 标题来源优先级：服务端下发的 ``title``（若有）→ 本地 ``template_key`` 文案表 →
  ``template_key`` 原文 → ``category`` → 「通知」。本地表只做**标题展示**，不编造正文。
* 无 store / 空库 / 消息库损坏（``list()`` 抛异常）一律返回 0，**托盘不因消息库异常崩溃**；
  壳若需排查可自行读 ``notifications.db``（本层不打日志、不做 IO）。
* 未读口径与 ``NotificationStore`` 一致：``read_at`` 为空即未读；过期过滤由 store 负责。
* 计数上限：``unread`` 取 store 的精确计数；``p0_unread`` 由前 ``FETCH_LIMIT`` 条（200）统计，
  超出该窗口时只少不多（托盘只用来决定是否加 ``!`` 前缀，不用于对账）。
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

__all__ = [
    "summarize",
    "from_store",
    "title_of",
    "TEMPLATE_TITLES",
    "DEFAULT_TITLE",
    "DEFAULT_LATEST",
]

#: ``latest`` 默认条数 / 上限
DEFAULT_LATEST = 5
MAX_LATEST = 20
#: 从 store 拉取的上限（与 facts.NOTIFICATIONS_MAX_LIMIT 同量级；只用于计数，不展示正文）
FETCH_LIMIT = 200
DEFAULT_TITLE = "通知"

#: 已知 template_key → 标题（只做展示；未知 key 原样回退，不猜内容）
TEMPLATE_TITLES: dict[str, str] = {
    "quota.near_limit": "配额接近上限",
    "quota.exhausted": "配额已用尽",
    "update.ready": "有新版本可更新",
    "receipt.backlog": "收据积压",
    "upstream.invalid": "上游失效",
    "credential.invalid": "凭据失效",
    "account.frozen": "账户已冻结",
    "policy.updated": "策略已更新",
    "notice.generic": "通知",
}


def title_of(item: Mapping[str, Any] | None) -> str:
    """单条通知的展示标题（不读正文）。"""
    if not isinstance(item, Mapping):
        return DEFAULT_TITLE
    raw_title = item.get("title")
    if isinstance(raw_title, str) and raw_title.strip():
        return raw_title.strip()
    template = str(item.get("template_key") or "").strip()
    if template in TEMPLATE_TITLES:
        return TEMPLATE_TITLES[template]
    if template:
        return template
    category = str(item.get("category") or "").strip()
    return category or DEFAULT_TITLE


def summarize(items: Iterable[Mapping[str, Any]] | None,
              *, limit: int = DEFAULT_LATEST) -> dict[str, Any]:
    """把通知行投影成 ``{unread, p0_unread, latest}``（纯函数）。"""
    rows = [row for row in (items or ()) if isinstance(row, Mapping)]
    count = _clamp_limit(limit)
    unread_rows = [row for row in rows if _is_unread(row)]
    p0_rows = [row for row in unread_rows if _severity(row) == "P0"]
    latest = [
        {
            "id": str(row.get("id") or ""),
            "severity": _severity(row),
            "title": title_of(row),
            "created_at": str(row.get("created_at") or ""),
        }
        for row in rows[:count]
    ]
    return {"unread": len(unread_rows), "p0_unread": len(p0_rows), "latest": latest}


def from_store(store: Any | None, *, limit: int = DEFAULT_LATEST) -> dict[str, Any]:
    """从 ``NotificationStore``（任何带 ``list()`` 的对象）取投影；异常一律降级为 0。"""
    empty = {"unread": 0, "p0_unread": 0, "latest": []}
    if store is None or not hasattr(store, "list"):
        return empty
    try:
        rows = list(store.list(limit=FETCH_LIMIT) or ())
    except Exception:                                      # 托盘是旁路：坏库不拖垮壳
        return empty
    summary = summarize(rows, limit=limit)
    try:
        summary["unread"] = max(0, int(store.unread_count()))
    except Exception:                                      # pragma: no cover - 替身无此方法
        pass
    return summary


def _is_unread(row: Mapping[str, Any]) -> bool:
    return not row.get("read_at")


def _severity(row: Mapping[str, Any]) -> str:
    return str(row.get("severity") or "P2").strip().upper() or "P2"


def _clamp_limit(limit: int) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):                        # pragma: no cover - 防御
        return DEFAULT_LATEST
    return max(0, min(value, MAX_LATEST))
