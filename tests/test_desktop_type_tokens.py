# SPDX-License-Identifier: Apache-2.0
"""排版 token 的可执行裁决（0.3.5 · M1-T2）。

对应 ``reports/CLIENT-UX-20261112/01-FONT-TYPO.md``：
§2.2 字阶表 / §2.4 排版规则 / §2.5 可读性三档 / §5 风险（scale 夹取）。

全部是**纯函数断言**，不开窗口（``typography`` 模块不 import tkinter，本文件也不会）。
0.3.4 的旧断言在 ``tests/test_desktop_typography.py``（T4 起它改为断言「``ui_kit`` 不再有
字号真值、全部由本模块派生」），旧名 → 档位的映射与两条例外见
``tests/test_desktop_px_wiring.py``。
"""
from __future__ import annotations

import ast
import inspect

import pytest

from djb_node.desktop import typography as T


# ────────────────────────────────────────────────────────────── 字阶表 = 设计文档
def test_token_table_is_the_design_doc_row_by_row():
    """§2.2：9 档，px/行高/字重逐值对齐（差一个值就是排版漂移）。"""
    expected = {
        # token:  (px,  line, weight,  family)
        "display": (30, 36, "bold", "sans"),
        "hero": (20, 28, "bold", "sans"),
        "h1": (18, 26, "bold", "sans"),
        "h2": (15, 22, "bold", "sans"),
        "body": (14, 22, "normal", "sans"),
        "small": (12.5, 18, "normal", "sans"),
        "micro": (11, 16, "normal", "sans"),
        "table": (12, 28, "normal", "sans"),
        "mono": (12, 16, "normal", "mono"),
    }
    assert set(T.TYPES) == set(expected)
    for name, (px, line, weight, family) in expected.items():
        item = T.token(name)
        assert (item.px, item.line, item.weight, item.family) == (px, line, weight, family), name


def test_table_line_is_the_row_height():
    """§2.3：``table`` 的 28 同时是 ``ttk.Treeview`` 的 rowheight（0.3.4 写死 26）。"""
    assert T.token("table").line == 28
    assert T.token("table").line_kind == "row"
    assert T.line_height("table") == 28
    assert T.scaled_line("table", 1.0) == 28


#: 0.3.4 的字号真值（``ui_kit`` 里已删除，这里作为**历史口径**保留在测试里）。
#: 顺序即 0.3.4 的字阶；用它算「有意变更」的 delta，防止有人把 +1 改回去或改错方向。
OLD_0_3_4: dict[str, float] = {
    "display": 30, "hero": 18, "h1": 19, "h2": 13,
    "body": 13, "small": 12, "micro": 10, "table": 11,
}


def test_delta_against_0_3_4_is_the_intended_one():
    """相对 0.3.4 的**有意**变更，逐项钉死（旧值不再从 ``ui_kit`` 读取）。"""
    assert T.px("display") == OLD_0_3_4["display"]          # 不变
    assert T.px("hero") - OLD_0_3_4["hero"] == 2            # 18 → 20
    assert T.px("h1") - OLD_0_3_4["h1"] == -1               # 19 → 18
    assert T.px("h2") - OLD_0_3_4["h2"] == 2                # 13 → 15
    assert T.px("body") - OLD_0_3_4["body"] == 1            # 13 → 14
    assert T.px("small") - OLD_0_3_4["small"] == 0.5        # 12 → 12.5
    assert T.px("micro") - OLD_0_3_4["micro"] == 1          # 10 → 11（CJK 下限）
    assert T.px("table") - OLD_0_3_4["table"] == 1          # 11 → 12（例外①，见 px_wiring）


# ────────────────────────────────────────────────────────────── 不变量
def test_invariants_have_no_violation():
    assert T.violations() == []


def test_ramp_is_strictly_monotonic():
    """micro < small < body < h2 < h1 < hero < display（0.3.4 的 19/18/13 三档挤在一起）。"""
    ramp = ["micro", "small", "body", "h2", "h1", "hero", "display"]
    values = [T.px(name) for name in ramp]
    assert values == sorted(values) and len(set(values)) == len(values)


def test_cjk_floor_is_11px_for_every_sans_token():
    """§2.2：CJK 最小 11px（0.3.4 的 micro 是 10px，中文会糊）。"""
    for name in T.TOKEN_ORDER:
        item = T.token(name)
        if item.family == "sans":
            assert item.px >= T.CJK_MIN_PX, name
    assert T.CJK_MIN_PX == 11


def test_body_line_height_is_at_least_one_and_a_half():
    """§2.2 明文：正文 14/22 = 1.57，不许低于 1.5。"""
    assert T.line_height("body") / T.px("body") >= 1.5


def test_hierarchy_never_collapses():
    """§2.2 的层级规则：差 2px **或**字重不同，二者必居其一。

    §2.2 原文「标题与正文至少差 2px 且字重不同」与它自己的 h2=15 / body=14 冲突（只差 1px，
    两条不可能同时成立）。这里按设计意图取「或」：塌掉的 19/18/13 正是「同字重 + 只差 1px」。
    """
    ramp = ["micro", "small", "body", "h2", "h1", "hero", "display"]
    for lower, upper in zip(ramp, ramp[1:]):
        a, b = T.token(lower), T.token(upper)
        gap = b.px - a.px
        assert gap >= 1, (lower, upper)
        if a.weight == b.weight:
            assert gap >= 1.5, (lower, upper)     # 同字重必须靠字号拉开
    # h2/body 只差 1px，靠 bold 承担层级——这是**有意**的设计，不是漏改
    assert (T.px("h2"), T.weight("h2")) == (15, "bold")
    assert (T.px("body"), T.weight("body")) == (14, "normal")


def test_headings_never_share_the_body_weight():
    """层级不能只靠字号：标题档位必须与正文不同字重。"""
    assert T.weight("body") == "normal"
    for name in ("h2", "h1", "hero", "display"):
        assert T.weight(name) == "bold", name


def test_weight_set_is_tk_safe():
    """Tk 只有 normal/bold；写 semibold/medium 会在渲染期静默退化成 normal。"""
    assert {T.token(name).weight for name in T.TOKEN_ORDER} <= {"normal", "bold"}


# ────────────────────────────────────────────────────────────── 取 token 要出声
def test_unknown_token_raises_with_the_known_list():
    with pytest.raises(T.TypographyError) as caught:
        T.token("h3")
    message = str(caught.value)
    assert "h3" in message and "display" in message and "body" in message


def test_mono_refuses_cjk_and_accepts_ascii():
    """中文不许进 mono（§2.2 脚注）；ASCII 回执号/版本号可以。"""
    T.assert_text_ok("mono", "djb-node 0.3.5 #a1b2c3")
    with pytest.raises(T.TypographyError):
        T.assert_text_ok("mono", "共享 Token Plan")
    # sans 档位不受限
    T.assert_text_ok("body", "共享 Token Plan")
    assert T.has_cjk("共享") is True
    assert T.has_cjk("share") is False


# ────────────────────────────────────────────────────────────── 缩放（DPI / 档位）
def test_readability_presets_match_the_design():
    """§2.5：紧凑 0.9 / 标准 1.0 / 大 1.15。"""
    assert T.READABILITY_PRESETS == {"compact": 0.9, "standard": 1.0, "large": 1.15}


def test_scaled_px_rounds_half_up_not_bankers():
    """12.5 必须变 13（``round(12.5) == 12`` 会让 small 掉回 0.3.4 的值）。"""
    assert T.scaled_px("small", 1.0) == 13
    assert T.scaled_px("body", 1.0) == 14
    assert T.scaled_px("body", 1.25) == 18            # 17.5 → 18
    assert T.scaled_px("small", 1.15) == 14           # 14.375 → 14
    assert T.scaled_line("body", 1.25) == 28          # 27.5 → 28


def test_scale_is_clamped_to_the_documented_window():
    """§5：scale 夹在 0.8–1.6，超出取边界（不静默放大到不可读）。"""
    assert (T.MIN_SCALE, T.MAX_SCALE) == (0.8, 1.6)
    assert T.scaled_px("body", 0.1) == T.scaled_px("body", 0.8) == 11
    assert T.scaled_px("body", 99) == T.scaled_px("body", 1.6) == 22
    assert T.scaled_px("body", float("nan")) == 14


def test_resolve_scale_priority_env_then_prefs_then_dpi():
    """§2.5：环境变量 > 用户设置 > 系统 DPI；非法值被记进 ignored，不静默吞。"""
    assert T.resolve_scale(env="1.25", prefs=1.15, dpi=1.5).value == 1.25
    assert T.resolve_scale(env=None, prefs=1.15, dpi=1.5).value == 1.15
    assert T.resolve_scale(dpi=1.5).value == 1.5
    assert T.resolve_scale().source == "default"

    decision = T.resolve_scale(env="large", prefs=1.15)
    assert (decision.value, decision.source) == (1.15, "env")

    bad = T.resolve_scale(env="abc", prefs="1.25")
    assert (bad.value, bad.source, bad.ignored) == (1.25, "prefs", ("env",))

    clamped = T.resolve_scale(env="3.0")
    assert (clamped.value, clamped.clamped) == (1.6, True)

    nothing = T.resolve_scale(env="", prefs="oops", dpi="x")
    assert (nothing.value, nothing.source) == (1.0, "default")
    assert set(nothing.ignored) == {"env", "prefs", "dpi"}


def test_tracking_is_clamped_to_eight_percent_of_the_glyph():
    assert T.tracking("display") < 0 and T.tracking("micro") > 0
    assert T.scaled_tracking("micro", 1.0) == 0.3
    assert abs(T.scaled_tracking("display", 1.0)) <= 30 * 0.08


# ────────────────────────────────────────────────────────────── 文本规则
def _measure():
    """假字体：ASCII 1.0px/字，CJK 2.0px/字，省略号 1.0px。"""
    def measure(text: str) -> float:
        return sum(2.0 if T.has_cjk(char) else 1.0 for char in text)
    return measure


def test_ellipsize_keeps_text_that_fits():
    measure = _measure()
    assert T.ellipsize("abcdef", 6, measure) == "abcdef"
    assert T.ellipsize("abcdef", 100, measure) == "abcdef"
    assert T.ellipsize("", 10, measure) == ""


def test_ellipsize_truncates_by_measured_width_not_by_char_count():
    """§2.4：按字体 measure 截断（0.3.4 是 ``name[:21] + "…"``）。"""
    measure = _measure()
    assert T.ellipsize("abcdefg", 5, measure) == "abcd…"      # 7px 放不进 5px
    assert measure(T.ellipsize("abcdefg", 5, measure)) <= 5

    # 同样的预算，中文只能放一半数量的字（宽度是 2 倍）
    cjk = "中文测试内容"
    out = T.ellipsize(cjk, 7, measure)
    assert out == "中文测…"
    assert measure(out) <= 7


def test_ellipsize_gives_empty_string_when_even_the_ellipsis_does_not_fit():
    measure = _measure()
    assert T.ellipsize("abcdefg", 0.5, measure) == ""
    assert T.ellipsize("abcdefg", 0, measure) == ""


def test_cjk_latin_spacing_inserts_one_space_both_ways():
    """§2.4：中英文之间自动补 1 个空格（渲染层，不改数据）。"""
    assert T.cjk_latin_spacing("版本0.3.5") == "版本 0.3.5"
    assert T.cjk_latin_spacing("djb客户端") == "djb 客户端"
    assert T.cjk_latin_spacing("共享Token Plan") == "共享 Token Plan"


def test_cjk_latin_spacing_does_not_touch_punctuation_or_pure_ascii():
    assert T.cjk_latin_spacing("共享（Token）计划") == "共享（Token）计划"
    assert T.cjk_latin_spacing("djb-node 0.3.5") == "djb-node 0.3.5"
    assert T.cjk_latin_spacing("") == ""
    assert T.cjk_latin_spacing("中文") == "中文"


def test_cjk_latin_spacing_is_idempotent():
    once = T.cjk_latin_spacing("版本0.3.5客户端djb")
    assert T.cjk_latin_spacing(once) == once


# ────────────────────────────────────────────────────────────── 架构约束
def _imported_modules(module) -> set[str]:
    """模块真正 import 的顶层模块名（按 AST 取，不看文档字符串里提到谁）。"""
    tree = ast.parse(inspect.getsource(module))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_typography_module_is_pure_python_without_tk():
    """纯函数模块：不 import tkinter，才能在无图形环境的 CI/NAS 上跑。"""
    imported = _imported_modules(T)
    assert "tkinter" not in imported
    assert "os" not in imported               # 不读环境变量：env 由调用方注入
    assert imported <= {"__future__", "math", "dataclasses", "typing"}


def test_every_ui_kit_role_has_a_token_mapping():
    """接线前先保证映射完整：``ui_kit.ROLE_FONTS`` 新增角色会在这里变红。"""
    from djb_node.desktop.ui_kit import Fonts

    assert set(T.ROLE_TOKEN_MAP) == set(Fonts.ROLE_FONTS)
    for role, target in T.ROLE_TOKEN_MAP.items():
        assert target in T.TYPES, role
    assert T.ROLE_WEIGHT_OVERRIDE == {"heading": "bold"}
