# SPDX-License-Identifier: Apache-2.0
"""排版 token（0.3.5 · M1-T2）：字阶 / 行高 / 字距 / 字重的**唯一**来源。

设计源：``reports/CLIENT-UX-20261112/01-FONT-TYPO.md`` §2.2（字阶表）、§2.4（排版规则）、
§2.5（可读性三档）。本模块是**纯数据 + 纯函数**：不 import ``tkinter``、不建窗口、不读写文件，
因此无图形环境也能断言（``pytest tests/test_desktop_type_tokens.py``）。

为什么另起一个模块而不是直接改 :mod:`djb_node.desktop.ui_kit`
==============================================================

0.3.4 的字号是「7 档 + 表格特例」的散量（``ui_kit.PX_*``，被 ``tests/test_desktop_typography.py``
逐值钉死）。0.3.5 要换成**带行高/字距/字重**的 token，属于渲染层改造（T4），本轮纵切片只落
数据结构与不变量，**不接线**：``ui_kit`` 一个字节不动，UI 外观零变化，现有 13 个视图的投影与
截图基线不受影响。**别把它当成「一行一处」的替换**：T4 要删掉 ``ui_kit.PX_*`` 49 行
（69 处引用），并补一张 0.3.4 的 7 档 → 本模块 9 档的映射表（``PX_TITLE/PX_CARD`` 这类
名字与值同时变），工作量在设计文档里记为 T4 的 2.5 人日。

三条硬规则（违反即排版退化，:func:`violations` 会报出来）
=========================================================

1. **字阶单调**：``micro < small < body < h2 < h1 < hero < display``（px 严格递增）。
   0.3.4 的问题正是 19/18/13 三档挤在一起，层级读不出来。
2. **CJK 下限 11px**：中文字号不许低于 :data:`CJK_MIN_PX`（10px 中文会糊）。
3. **中文不进 mono**：``mono`` 只承载 ASCII 数字/ID;含 CJK 的串走 sans
   （:func:`assert_text_ok` 会抛 :class:`TypographyError`，不做静默降级）。

行高口径：``line`` 是**行盒高度**（px@96dpi，实际乘 ``scale``）；``table`` 这一档的 ``line``
（28）设计上兼作 ``ttk.Treeview`` 的 ``rowheight``（01-FONT-TYPO §2.3），但**待接**：
当前 ``rowheight`` 仍硬编码 26（`app.py:275`），本轮字号接线未动它。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

__all__ = [
    "CJK_MIN_PX", "DEFAULT_SCALE", "MAX_SCALE", "MIN_SCALE", "READABILITY_PRESETS",
    "ROLE_TOKEN_MAP", "ROLE_WEIGHT_OVERRIDE", "TYPES", "TOKEN_ORDER", "TypeToken",
    "TypographyError", "ScaleDecision", "assert_text_ok", "cjk_latin_spacing", "has_cjk",
    "ellipsize", "line_height", "px", "resolve_scale", "scaled_line", "scaled_px",
    "round_px", "scaled_tracking", "token", "tracking", "violations", "weight",
]

#: CJK 最小字号（px@96dpi）：低于它就是糊，不是「小」。
CJK_MIN_PX = 11

#: 可读性档位（01-FONT-TYPO §2.5）；键是写进 ``desktop-prefs.json`` 的 ``ui.readability``。
READABILITY_PRESETS: dict[str, float] = {"compact": 0.9, "standard": 1.0, "large": 1.15}

DEFAULT_SCALE = 1.0
#: ``scale`` 夹取区间（§5 风险表：用户设置与系统 DPI 叠加过大时取边界并在设置里提示）。
MIN_SCALE = 0.8
MAX_SCALE = 1.6


class TypographyError(ValueError):
    """排版 token 用错（未知 token / 把中文塞进 mono / 字重不在 Tk 支持的集合里）。"""


@dataclass(frozen=True)
class TypeToken:
    """一档排版 token。

    ``px`` / ``line`` / ``tracking`` 都是 **px@96dpi 逻辑像素**，渲染时乘 ``scale``。
    ``px`` 允许小数（``small`` = 12.5，01-FONT-TYPO §2.2 明文规定），取整只在
    :func:`scaled_px` 里做（四舍五入，至少 1px）——**token 表里不许出现取整后的近似值**。
    """

    name: str
    px: float
    line: float
    weight: str                 # "normal" | "bold"（Tk 只有这两档）
    family: str = "sans"        # "sans" | "mono"
    #: 字距（px，正数疏/负数紧）。
    # 实现自定，待设计确认；Tk 不支持 letter-spacing，渲染层暂不消费。
    # （Tk 的 ``tkinter.font.Font`` 没有字距参数，``Text`` tag 也没有 letter-spacing；
    #   真要落地只能拆字画到 Canvas 上，代价与收益未评估 ⇒ 本轮只作为数据保留，
    #   因此这 5 个值目前是「渲染层无法承载的死数据」，不要拿它去调外观。）
    tracking: float = 0.0
    usage: str = ""
    #: ``line`` 的语义：``"line"`` = 行盒高度；``"row"`` = 表格行高（兼作行盒）
    line_kind: str = "line"


#: 字阶表 = 01-FONT-TYPO §2.2 逐行誊写（顺序即层级，勿随手插档）。
#:
#: 注意 ``tracking`` 一列**不在设计文档里**（§2.2 只有 px/行高/字重三列）：这 5 个值是
#: 实现自定，待设计确认；Tk 不支持 letter-spacing，渲染层暂不消费。
TOKEN_ORDER: tuple[str, ...] = (
    "display", "hero", "h1", "h2", "body", "small", "micro", "table", "mono",
)

TYPES: dict[str, TypeToken] = {
    "display": TypeToken("display", 30.0, 36.0, "bold", tracking=-0.4,
                         usage="仪表环中心数字 / 首屏大数"),
    "hero": TypeToken("hero", 20.0, 28.0, "bold", tracking=-0.2, usage="页面主标题"),
    "h1": TypeToken("h1", 18.0, 26.0, "bold", tracking=-0.1, usage="区块标题"),
    "h2": TypeToken("h2", 15.0, 22.0, "bold", usage="卡片标题"),
    "body": TypeToken("body", 14.0, 22.0, "normal", usage="正文 / 说明 / 表单"),
    "small": TypeToken("small", 12.5, 18.0, "normal", tracking=0.1, usage="辅助信息"),
    "micro": TypeToken("micro", 11.0, 16.0, "normal", tracking=0.3,
                       usage="徽标 / 角标（CJK 下限）"),
    "table": TypeToken("table", 12.0, 28.0, "normal", usage="表格正文（行高兼 rowheight）",
                       line_kind="row"),
    "mono": TypeToken("mono", 12.0, 16.0, "normal", family="mono",
                      usage="仅 ASCII 数字/ID（CJK 禁止进 mono）"),
}

#: 字阶单调链（不含 ``mono``/``table``：它们与正文并列，不是层级）
_RAMP: tuple[str, ...] = ("micro", "small", "body", "h2", "h1", "hero", "display")

#: 0.3.4 渲染层角色 → 0.3.5 token。**只用于 T4 接线对照**，本轮不生效。
#: ``heading``（表头 bold）用的是 ``small`` 的字号 + :data:`ROLE_WEIGHT_OVERRIDE` 的 bold，
#: 因为 01-FONT-TYPO §2.4 把表头定为「small 12.5 bold」，不是单独一档字阶。
ROLE_TOKEN_MAP: dict[str, str] = {
    "body": "body",
    "small": "small",
    "micro": "micro",
    "heading": "small",
    "table": "table",
    "mono": "mono",
    "nav": "small",
    "card": "h2",
    "title": "h1",
    "hero": "hero",
    "display": "display",
}

#: 角色的字重覆盖（None = 用 token 自带字重）
ROLE_WEIGHT_OVERRIDE: dict[str, str | None] = {"heading": "bold"}


# ── 取 token
def token(name: str) -> TypeToken:
    """按名字取 token；未知名字直接抛错（附已知档位），不返回默认值。"""
    key = str(name or "").strip()
    try:
        return TYPES[key]
    except KeyError:
        raise TypographyError(
            "未知排版 token：" + repr(name) + "（已知：" + " / ".join(TOKEN_ORDER) + "）"
        ) from None


def px(name: str) -> float:
    return token(name).px


def line_height(name: str) -> float:
    return token(name).line


def tracking(name: str) -> float:
    return token(name).tracking


def weight(name: str) -> str:
    return token(name).weight


# ── 缩放（DPI / 可读性档位）
def _clamp_scale(value: float) -> tuple[float, bool]:
    """夹到 [MIN_SCALE, MAX_SCALE]；返回 (值, 是否被夹过)。NaN/非数一律回 DEFAULT。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return DEFAULT_SCALE, True
    if math.isnan(number) or math.isinf(number) or number <= 0:
        return DEFAULT_SCALE, True
    if number < MIN_SCALE:
        return MIN_SCALE, True
    if number > MAX_SCALE:
        return MAX_SCALE, True
    return number, False


def _as_scale(raw: object) -> float | None:
    """把 env / prefs 里的原始值转成 scale；转不动返回 None（由调用方记进 ``ignored``）。"""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


@dataclass(frozen=True)
class ScaleDecision:
    """``scale`` 的裁决结果——**带来源**，排障时不用猜是哪一层生效的。

    ``source`` ∈ ``{"env", "prefs", "dpi", "default"}``；``ignored`` 列出被丢弃的层级
    （非法值不静默吞掉，交给设置页提示「已忽略 DJB_UI_SCALE=abc」）。
    """

    value: float
    source: str
    clamped: bool
    ignored: tuple[str, ...] = ()


def resolve_scale(*, env: object = None, prefs: object = None,
                  dpi: object = None) -> ScaleDecision:
    """按 01-FONT-TYPO §2.5 的优先级定标：环境变量 > 用户设置 > 系统 DPI > 默认 1.0。

    纯函数：不读 ``os.environ``、不读 prefs 文件，由调用方把三层值传进来（可测、可回放）。
    ``env`` 接受 :data:`READABILITY_PRESETS` 里的档位名或数字串（``"1.25"``）。
    """
    ignored: list[str] = []
    for source, raw in (("env", env), ("prefs", prefs), ("dpi", dpi)):
        if raw is None:
            continue
        candidate: float | None
        if source == "env" and isinstance(raw, str) and raw.strip().lower() in READABILITY_PRESETS:
            candidate = READABILITY_PRESETS[raw.strip().lower()]
        else:
            candidate = _as_scale(raw)
        if candidate is None:
            ignored.append(source)
            continue
        value, clamped = _clamp_scale(candidate)
        return ScaleDecision(value=value, source=source, clamped=clamped,
                             ignored=tuple(ignored))
    return ScaleDecision(value=DEFAULT_SCALE, source="default", clamped=False,
                         ignored=tuple(ignored))


def round_px(value: float) -> int:
    """设计 px → 实际整数 px（至少 1）：**与 0.3.4 逐档等价**，用 ``round()``。

    为什么不用 ``floor(x + 0.5)``（半进位）：Tk 只认整数像素，换一套取整就会在 ``.5``
    边界上悄悄改外观 —— ``display`` @1.15（34.5：0.3.4 → **34**，半进位 → 35）、
    ``h1`` @1.25（22.5：0.3.4 → **22**，半进位 → 23）。0.3.4 的 ``ui_kit.Fonts.px``
    就是 ``int(round(float(v) * scale))``（Python 的银行家舍入），这里**原样继承**，
    并由 ``tests/test_desktop_px_wiring.py::test_rounding_matches_0_3_4_at_every_scale``
    在 0.80–1.60 逐点钉住。

    副作用（已在 M1-PROGRESS §9.7 记录）：``small`` 的设计值是 12.5，但 1.0 档下
    ``round(12.5) == 12`` ⇒ 渲染与 0.3.4 完全一致。**设计值保留 12.5，渲染值由本函数派生**，
    不为「让 12.5 变成 13」而改取整语义。
    """
    return max(1, int(round(float(value))))


def scaled_px(name: str, scale: float = DEFAULT_SCALE) -> int:
    """设计 px → 实际整数 px（乘 ``scale`` 并夹取，至少 1）。"""
    factor, _ = _clamp_scale(scale)
    return round_px(token(name).px * factor)


def scaled_line(name: str, scale: float = DEFAULT_SCALE) -> int:
    """行盒高度（px）→ 实际整数 px；``table`` 档同时是 ``rowheight``。"""
    factor, _ = _clamp_scale(scale)
    return round_px(token(name).line * factor)


def scaled_tracking(name: str, scale: float = DEFAULT_SCALE) -> float:
    """字距（px，保留 2 位小数）。夹到该档字号的 ±8%，避免极端档位把字顶到重叠。

    **预留接口**：Tk 不支持 letter-spacing（见 :class:`TypeToken.tracking` 的注释），
    渲染层暂不调用本函数；它存在只是为了把设计值原样带过 T4，且夹取规则有测试兜底。
    """
    item = token(name)
    factor, _ = _clamp_scale(scale)
    limit = item.px * 0.08
    return round(max(-limit, min(limit, item.tracking * factor)), 2)


# ── 文本规则
def _is_cjk(char: str) -> bool:
    """CJK 表意文字 + 假名 + 谚文。**不含**全角标点（（）：「」）——标点两侧不补空格。"""
    code = ord(char)
    return (
        0x3400 <= code <= 0x4DBF          # CJK 扩展 A
        or 0x4E00 <= code <= 0x9FFF       # CJK 基本区
        or 0xF900 <= code <= 0xFAFF       # 兼容表意
        or 0x3040 <= code <= 0x30FF       # 平假名 / 片假名
        or 0xAC00 <= code <= 0xD7AF       # 谚文
    )


def _is_latin(char: str) -> bool:
    return char.isascii() and (char.isalnum())


def has_cjk(text: str) -> bool:
    return any(_is_cjk(char) for char in str(text))


def assert_text_ok(name: str, text: str) -> None:
    """中文不许进 mono：``mono`` 遇到含 CJK 的串直接抛错（不静默换族/换字号）。"""
    item = token(name)
    if item.family == "mono" and has_cjk(text):
        raise TypographyError(
            "token 'mono' 只能承载 ASCII（含中文的串请用 'body'/'small'），收到：" + repr(text)
        )


def cjk_latin_spacing(text: str) -> str:
    """中英混排：中英文之间补 1 个空格（01-FONT-TYPO §2.4）。

    **渲染层函数**：只影响显示，不改数据（落库/回执/日志一律用原串）。
    幂等：空格本身既不是 CJK 也不是 Latin，重复调用不会越补越多。
    """
    source = str(text)
    if not source:
        return source
    out: list[str] = [source[0]]
    for char in source[1:]:
        prev = out[-1]
        if (_is_cjk(prev) and _is_latin(char)) or (_is_latin(prev) and _is_cjk(char)):
            out.append(" ")
        out.append(char)
    return "".join(out)


def ellipsize(text: str, budget: float, measure: Callable[[str], float],
              ellipsis: str = "…") -> str:
    """按**字体实测宽度**截断（01-FONT-TYPO §2.4：不再按字符数切 ``text[:21]``）。

    ``measure(s) -> float`` 由渲染层注入（Tk 侧传 ``tkfont.Font.measure``）；
    本函数不碰 Tk，因此可在无图形环境断言。放不下时尽量保留前缀 + ``ellipsis``；
    连省略号都放不下就返回空串（调用方按空态处理，不画半个字）。
    """
    source = str(text)
    if not source:
        return ""
    if measure(source) <= budget:
        return source
    if budget <= 0:
        return ""
    if measure(ellipsis) > budget:
        return ""
    # 二分找最长的、加省略号后仍放得下的前缀
    low, high = 0, len(source) - 1
    best = 0
    while low <= high:
        mid = (low + high) // 2
        if measure(source[:mid] + ellipsis) <= budget:
            best = mid
            low = mid + 1
        else:
            high = mid - 1
    return source[:best] + ellipsis if best > 0 else ellipsis


# ── 不变量
def violations() -> list[str]:
    """返回排版表的不变量违规（空列表 = 合格）。每条都是**可执行的设计裁决**。"""
    problems: list[str] = []

    if tuple(TYPES) != TOKEN_ORDER:
        problems.append("TOKEN_ORDER 与 TYPES 的键集/顺序不一致")

    for name in TOKEN_ORDER:
        item = token(name)
        if item.name != name:
            problems.append(f"{name}: 自名字段与键不一致")
        if item.px <= 0 or item.line <= 0:
            problems.append(f"{name}: px/line 必须为正")
        if item.line < item.px:
            problems.append(f"{name}: 行盒 {item.line} 小于字号 {item.px}")
        if item.weight not in {"normal", "bold"}:
            problems.append(f"{name}: 字重只能是 normal/bold（Tk 限制）")
        if item.family not in {"sans", "mono"}:
            problems.append(f"{name}: 字族只能是 sans/mono")
        if item.family == "sans" and item.px < CJK_MIN_PX:
            problems.append(f"{name}: CJK 字号 {item.px} < 下限 {CJK_MIN_PX}")
        if item.line_kind not in {"line", "row"}:
            problems.append(f"{name}: line_kind 只能是 line/row")
        if abs(item.tracking) > item.px * 0.08:
            problems.append(f"{name}: 字距 {item.tracking}px 超过字号 8%")

    # 1) 字阶单调（px 严格递增）
    for smaller, bigger in zip(_RAMP, _RAMP[1:]):
        if px(smaller) >= px(bigger):
            problems.append(f"字阶不单调：{smaller}({px(smaller)}) ≥ {bigger}({px(bigger)})")

    # 2) 正文行高 ≥ 1.5×（§2.2 明文：14/22 = 1.57）
    body = token("body")
    if body.line < body.px * 1.5:
        problems.append(f"body 行高 {body.line} < 1.5×字号 {body.px}")

    # 3) 所有档位行高 ≥ 1.2×（大数字可紧，但不得叠字）
    for name in TOKEN_ORDER:
        item = token(name)
        if item.line < item.px * 1.2 - 1e-9:
            problems.append(f"{name}: 行高 {item.line} < 1.2×字号 {item.px}")

    # 4) 层级差：标题带（bold）同字重相邻档位 ≥2px；正文带（normal）≥1.5px；
    #    字重不同（标题 vs 正文）时 1px 即可——因为层级同时由字重承担。
    #    注：§2.2 的原话是「标题与正文至少差 2px 且字重不同」，但它自己给的 h2=15 / body=14
    #    只差 1px，两条同时满足不可能。这里取**设计意图**：差 2px 或字重不同，二者必居其一；
    #    当初 19/18/13 之所以塌掉，正是因为「同字重 + 只差 1px」。这条在测试里显式钉住。
    for lower, upper in zip(_RAMP, _RAMP[1:]):
        a, b = token(lower), token(upper)
        gap = b.px - a.px
        if a.weight != b.weight:
            least = 1.0
        elif a.weight == "bold":
            least = 2.0
        else:
            least = 1.5
        if gap < least - 1e-9:
            problems.append(
                f"层级压缩：{lower}({a.px}) → {upper}({b.px}) 只差 {gap}px"
                f"（同字重要求 ≥{least}px）"
            )

    # 5) 标题必须与正文不同字重（否则只靠字号差，CJK 上读不出层级）
    for name in _RAMP:
        if name in {"body", "small", "micro"}:
            continue
        if token(name).weight == token("body").weight:
            problems.append(f"{name}: 标题档位与正文同字重（层级只能靠字号）")

    # 6) mono 只能是 ASCII 档：不许承载 CJK（这里用字号/行高关系做静态兜底）
    if token("mono").family != "mono":
        problems.append("mono 档位的 family 必须是 mono")

    # 7) 角色映射必须指向真实档位
    for role, target in ROLE_TOKEN_MAP.items():
        if target not in TYPES:
            problems.append(f"角色 {role} 映射到未知 token {target}")
    for role, override in ROLE_WEIGHT_OVERRIDE.items():
        if role not in ROLE_TOKEN_MAP:
            problems.append(f"字重覆盖里有未知角色 {role}")
        if override is not None and override not in {"normal", "bold"}:
            problems.append(f"角色 {role} 的字重覆盖 {override} 非法")

    return problems

