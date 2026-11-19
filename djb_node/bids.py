# SPDX-License-Identifier: Apache-2.0
"""逐模型底价（RC14 I/J）：本地账本 + 预览 + 上报闸门。

背景（报告 §I/J 的客户投诉）：一个 Key 后面挂着 20+ 个模型，客户端却只能给**整个上游**
一个价，而且 0 会被云端 ``SKIP_ZERO_PRICE`` 静默丢掉——客户填了 0 以为「白送能换量」，
结果模型根本没进报价。所以：

* 底价按**模型**存，最小 ``0.01``（0 在本地就拦下并说清原因，不发出去等云端静默丢弃）；
* 本地账本落在本机 sqlite 的 kv 表（**不含凭据**），键 ``rc14/model_bids``；
* **云端未开通**：``POST /api/v1/user/provider/ask-settings/{model}`` 与
  ``/api/v1/schedule/quotes`` 的 ``models[]`` 到今天还没有主线实现 —— 所以上报默认**关闭**
  （flag ``client.upstream.bid_report.enabled``，三字段齐全），UI 只说「待云端开通」，
  绝不假成功；
* ``preview()`` 给出「将上报什么」的确定性形状（契约字段名以 CHG §2 为准），
  便于客户核对，也便于测试断言「预览 == 本地账本」。
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from . import facts

#: 本地账本的 kv 键（值形状：``{"version": 1, "bids": {model: {"min_price": "0.35"}}}``）
BIDS_KV_KEY = "rc14/model_bids"
BIDS_SCHEMA_VERSION = 1

#: 最小申报价（USD / 百万 token）。``0`` 会被云端 ``SKIP_ZERO_PRICE`` 静默丢掉。
MIN_PRICE = Decimal("0.01")
#: 整数位上限（云端 ``ask_min`` 口径：整数 ≤ 7 位）
MAX_INT_DIGITS = 7
#: 最多两位小数
MAX_DECIMALS = 2

PRICE_RE = re.compile(r"^[0-9]+(\.[0-9]+)?$")
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9._:/@+\-]{1,128}$")

ERROR_EMPTY = "填一个价格，或这行留空表示不提供"
ERROR_ZERO = "0 等于白送，至少填 0.01（云端会把 0 当「未申报」直接跳过）"
ERROR_FORMAT = "价格只能是数字，最多两位小数"
ERROR_TOO_BIG = "价格太大（整数最多 7 位）"
ERROR_MODEL = "模型 id 只能用字母数字和 ._:/@+-，例如 gpt-4o-mini"


def normalize_model(model: Any) -> str:
    """模型 id 规范化：去空白；非法就抛 ValueError（调用方转成可读文案）。"""
    text = str(model or "").strip()
    if not MODEL_ID_RE.match(text):
        raise ValueError(ERROR_MODEL)
    return text


def parse_price(text: Any) -> Decimal:
    """把用户输入的价格解析成 ``Decimal``（>0、≤2 位小数、整数 ≤7 位）。

    空串 = 「不提供这个模型」→ 抛 :class:`ValueError`，调用方据此**删除**该行，
    而不是写 0（写 0 只会在云端被静默丢掉，客户以为填了）。
    """
    raw = str(text or "").strip()
    if not raw:
        raise ValueError(ERROR_EMPTY)
    if not PRICE_RE.match(raw):
        raise ValueError(ERROR_FORMAT)
    whole, _, frac = raw.partition(".")
    if len(frac) > MAX_DECIMALS:
        raise ValueError(ERROR_FORMAT)
    if len(whole.lstrip("0") or "") > MAX_INT_DIGITS:
        raise ValueError(ERROR_TOO_BIG)
    try:
        value = Decimal(raw)
    except InvalidOperation:                                # pragma: no cover - 正则已挡
        raise ValueError(ERROR_FORMAT) from None
    if value <= 0:
        raise ValueError(ERROR_ZERO)
    if value < MIN_PRICE:
        raise ValueError(ERROR_ZERO)
    return value.quantize(MIN_PRICE)


def format_price(value: Any) -> str:
    """账本里的价格原样展示（``0.35`` 不带 ``0.350`` 这种噪声）；坏值给「—」，绝不补 0。"""
    if value in (None, "", 0):
        return "—" if value in (None, "") else "0"
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return "—"
    text = format(decimal.normalize(), "f")
    return text if text else "—"


class BidBook:
    """逐模型底价的本地账本（唯一写入口；不含凭据，只在本机 sqlite）。"""

    def __init__(self, store: Any = None) -> None:
        self.store = store

    # ── 读
    def load(self) -> dict[str, dict[str, Any]]:
        if self.store is None:
            return {}
        raw = self.store.get_kv(BIDS_KV_KEY, None)
        if not isinstance(raw, Mapping):
            return {}
        bids = raw.get("bids")
        if not isinstance(bids, Mapping):
            return {}
        out: dict[str, dict[str, Any]] = {}
        for model, item in bids.items():
            if not isinstance(item, Mapping):
                continue
            try:
                price = parse_price(item.get("min_price"))
            except ValueError:
                continue                    # 坏数据不猜：宁可不展示，也不写一个假价
            out[str(model)] = {"min_price": format_price(price)}
        return out

    def price_of(self, model: str) -> str:
        return str((self.load().get(str(model)) or {}).get("min_price") or "")

    # ── 写
    def set_price(self, model: str, text: Any) -> str:
        """设一行的底价；返回规范化后的价格文本。空串 = 移除这一行。"""
        model_id = normalize_model(model)
        bids = self.load()
        raw = str(text or "").strip()
        if not raw:
            bids.pop(model_id, None)
            self._save(bids)
            return ""
        price = parse_price(raw)
        bids[model_id] = {"min_price": format_price(price)}
        self._save(bids)
        return bids[model_id]["min_price"]

    def remove(self, model: str) -> bool:
        model_id = str(model or "").strip()
        bids = self.load()
        existed = bids.pop(model_id, None) is not None
        if existed:
            self._save(bids)
        return existed

    def clear(self) -> None:
        self._save({})

    def _save(self, bids: Mapping[str, Mapping[str, Any]]) -> None:
        if self.store is None:
            raise RuntimeError("本机数据库不可用（没有 store）：底价无法保存")
        self.store.set_kv(BIDS_KV_KEY, {
            "version": BIDS_SCHEMA_VERSION,
            "bids": {str(model): {"min_price": str(item["min_price"])}
                     for model, item in bids.items()},
        })

    # ── 预览与上报
    def report_payload(self, *, upstream_id: str = "") -> dict[str, Any]:
        """「将上报什么」的确定性形状（契约字段名以 CHG §2 / P9 为准）。

        ``models[]`` 每项 ``{model, min_price}``；``min_price`` 用字符串（钱不用 float 传）。
        没有底价时给空数组 + ``reason``，让 UI 说「还没有可上报的底价」，而不是发个空包。
        """
        bids = self.load()
        models = [{"model": model, "min_price": str(item["min_price"])}
                  for model, item in sorted(bids.items())]
        payload: dict[str, Any] = {
            "schema": BIDS_SCHEMA_VERSION,
            "upstream_id": str(upstream_id or ""),
            "unit": "usd_per_mtok",
            "models": models,
            "paths": list(facts.UPSTREAM_BID_REPORT_PATHS),
        }
        if not models:
            payload["reason"] = "no_bid"
        return payload


def report_enabled(config: Any) -> bool:
    """上报闸门（默认关闭）：flag 三字段齐全、到期强制关，求值走 config.feature_flag。"""
    from .config import feature_flag

    return bool(feature_flag(config, facts.UPSTREAM_BID_FLAG))


def report_blocker(config: Any) -> str:
    """上报不可用时的原因文案（空串 = 可以上报）。

    两种「不可用」必须分开说：开关关着（owner 未批准放开）vs 没有可上报的底价。
    """
    if not report_enabled(config):
        return ("上报开关关闭（默认关闭，等云端「按模型设价」接口开通后由 owner 放开）："
                "底价只保存在本机，未发往云端")
    return ""
