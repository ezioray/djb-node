# SPDX-License-Identifier: Apache-2.0
"""逐模型底价（RC14 I/J）：本地账本 + 预览 + 上报闸门。

背景（报告 §I/J 的客户投诉）：一个 Key 后面挂着 20+ 个模型，客户端却只能给**整个上游**
一个价，而且 0 会被云端 ``SKIP_ZERO_PRICE`` 静默丢掉——客户填了 0 以为「白送能换量」，
结果模型根本没进报价。所以：

* 底价按**模型**存，最小 ``0.01``（0 在本地就拦下并说清原因，不发出去等云端静默丢弃）；
* 本地账本落在本机 sqlite 的 kv 表（**不含凭据**），键 ``rc14/model_bids``；

* R8-F6（2026-11-24）：**一次性上报**（桌面按钮）点击 + 确认后按本账本价逐模型
  ``POST /api/v1/schedule/quotes``（到期撤下、不自动续报），**不受** flag
  ``client.upstream.bid_report.enabled`` 限制——该 flag 只管 daemon 自动续报；
  flag **开**时 daemon/CLI 也读本账本价（与手动同源，避免两套价互相顶版本），
  flag **关**时维持 ``ModelEntry.min_price`` 的旧口径（rollback_rule 成立）。
* **自动续报**仍由 daemon 负责且默认关闭；两条路共用一本账本价，绝不假成功。
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

#: 成交模式（原始设计）：设一个**最低价**，撮合到的最优价 ≥ 最低价就成交。
#: 账本里显式记下来，方便以后加别的模式（如封顶价 ``fixed``）而不改历史数据。
MODE_AUTO_BEST = "auto_best"
#: 缺 mode 的老账本按这个解释（向后兼容，不迁移数据）。
DEFAULT_MODE = MODE_AUTO_BEST

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

    def prices(self) -> dict[str, str]:
        """扁平价格覆盖表（``{model: "0.35"}``），给 ``quotes.plan_quotes(prices=…)``。"""
        return {str(model): str(item["min_price"])
                for model, item in self.load().items()}

    def mode_of(self, model: str) -> str:
        """这一行的成交模式；老账本没有 mode 就是 ``auto_best``。

        刻意**不放进** :meth:`load` 的返回形状：``load()`` 是对外契约（UI/测试都按
        ``{model: {"min_price": str}}`` 读），mode 是后加的旁路信息，从原始 kv 读。
        """
        if self.store is None:
            return DEFAULT_MODE
        raw = self.store.get_kv(BIDS_KV_KEY, None)
        bids = raw.get("bids") if isinstance(raw, Mapping) else None
        item = bids.get(str(model)) if isinstance(bids, Mapping) else None
        if not isinstance(item, Mapping):
            return DEFAULT_MODE
        return str(item.get("mode") or DEFAULT_MODE)

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
        bids[model_id] = {"min_price": format_price(price), "mode": DEFAULT_MODE}
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
            "bids": {str(model): {"min_price": str(item["min_price"]),
                                  "mode": str(item.get("mode") or DEFAULT_MODE)}
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
    """**自动续报**闸门（默认关闭）：flag 三字段齐全、到期强制关，求值走 config.feature_flag。

    R8-F6 决策：一次性上报**不读**这个 flag（那是显式点击 + 确认的一次性动作）；
    它只决定 daemon/CLI 的自动环是否开启、以及自动环是否读本账本价。默认值保持
    `False`，不因本次功能调整。
    """
    from .config import feature_flag

    return bool(feature_flag(config, facts.UPSTREAM_BID_FLAG))


def report_blocker(config: Any) -> str:
    """配置级阻断文案（空串 = 可以发起一次性上报）。

    一次性上报已不再受自动续报 flag 约束（R8-F6），因此这里通常为空；
    「未绑定 / 没有底价 / 正在上报」这些**实时**阻断由视图层按当前快照给具体原因。
    保留本函数是为了调用方接口稳定（datasource 的底价区块仍读它）。
    """
    if config is None:
        return "本机配置读不到：无法确认网关地址（底价只保存在本机，未上报）"
    return ""


def auto_report_prices(store: Any, config: Any) -> dict[str, str] | None:
    """**自动续报**用的价格覆盖：flag 开 → 本机账本价；flag 关 → None（回落上游价）。

    只该被 daemon 报价环和 CLI ``quotes`` 调用。桌面的一次性上报**永远**用账本价
    （它本身就是「上报逐模型底价」），不经过这个函数，也不看 flag。
    账本读不到时返回 None（宁可回到旧口径，也不让报价环整轮失败）。
    """
    if not report_enabled(config):
        return None
    try:
        return BidBook(store).prices()
    except Exception:                                   # 库坏了：回落上游价，不拖垮报价
        return None
