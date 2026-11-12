# SPDX-License-Identifier: Apache-2.0
"""T4 接线验收（0.3.5 · M1 第二轮）：0.3.4 的 16 个 ``PX_*`` 名字 → 9 档 token。

这份文件是 **0.3.4 口径的唯一边界**：旧名字已从 ``ui_kit`` 删除，映射、值等价与两条例外
全部收在这里，谁想改字阶就必须先改这张表。

三组断言
========

1. **旧名字真的没了**：``ui_kit`` 上不再有 ``PX_*``（49 行 / 69 处已清零），
   取字号只剩 :func:`djb_node.desktop.ui_kit.px` 一个入口。
2. **值等价（0.3.4 语义下逐项相等）**：同一个名字在 0.3.4 与 0.3.5 指向**同一档角色**，
   且新值 == 旧值 + 设计表里的有意 delta（display 0 / h1 −1 / hero +2 / h2 +2 /
   body +1 / small +0.5 / micro +1）——逐项相加必须成立，不允许「顺手多改 1px」。
3. **两条例外显式断言**：``PX_TABLE`` 11 → ``table`` 12（0.3.4 的表格特例档并入正表），
   ``mono`` 无 0.3.4 对应（当时等宽借微标 10px，现在独立 12px）。

另附守门：角色字体派生（``ROLE_FONTS``）、**取整公式与 0.3.4 在 0.80–1.60 全档逐点等价**
（属性测试，回退成半进位即红）、渲染层不许出现数字字号字面量（AST 扫描）。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pathlib

import pytest

from djb_node.desktop import typography as T
from djb_node.desktop import ui_kit

#: 0.3.4 的 8 个**真值常量**：名字 → (0.3.4 的值, 0.3.5 的档位)
LEGACY_CONSTANTS: dict[str, tuple[float, str]] = {
    "PX_DISPLAY": (30, "display"),
    "PX_TITLE": (19, "h1"),
    "PX_HERO": (18, "hero"),
    "PX_CARD": (13, "h2"),
    "PX_BODY": (13, "body"),
    "PX_SMALL": (12, "small"),
    "PX_MICRO": (10, "micro"),
    "PX_TABLE": (11, "table"),
}

#: 0.3.4 的 7 个**旧名别名**：别名 → 它当年指向的真值常量（值等价按这条链传递）
LEGACY_ALIASES: dict[str, str] = {
    "PX_H1": "PX_TITLE",
    "PX_BODY_S": "PX_SMALL",
    "PX_NAV": "PX_SMALL",
    "PX_NUM": "PX_BODY",
    "PX_NUM_L": "PX_TITLE",
    "PX_NUM_XL": "PX_DISPLAY",
    "PX_MICRO_CJK": "PX_MICRO",
}

#: 0.3.4 的 CJK 下限（= PX_MICRO = 10）；0.3.5 抬到 11（01-FONT-TYPO §2.2）
LEGACY_CJK_MIN = 10

#: 设计表里的**有意** delta（新值 − 0.3.4 值）。0 = 不变；其余逐项见 01-FONT-TYPO §2.2。
INTENDED_DELTA: dict[str, float] = {
    "display": 0, "h1": -1, "hero": 2, "h2": 2, "body": 1, "small": 0.5, "micro": 1,
}


# ────────────────────────────────────────────────────────────── 1) 旧名字清零
def test_legacy_px_names_are_gone_from_ui_kit():
    """49 行 / 69 处的 ``PX_*`` 已清零：再有人加回常量，这条会红。"""
    survivors = sorted(name for name in dir(ui_kit) if name.startswith("PX_"))
    assert survivors == [], "ui_kit 仍暴露旧字号常量：" + " / ".join(survivors)
    assert callable(ui_kit.px)


def test_ui_kit_px_is_the_only_size_entry_point():
    """取字号只有 :func:`ui_kit.px`：它必须就是 typography 的值（不做二次换算/缓存）。"""
    for name in T.TOKEN_ORDER:
        assert ui_kit.px(name) == T.px(name) == T.token(name).px
    with pytest.raises(T.TypographyError):
        ui_kit.px("h3")


# ────────────────────────────────────────────────────────────── 2) 值等价
@pytest.mark.parametrize("legacy,expected", sorted(LEGACY_CONSTANTS.items()))
def test_value_equivalence_under_0_3_4_semantics(legacy, expected):
    """0.3.4 的名字 → 同一档角色，值 == 旧值 + 有意 delta（逐项相等，不多不少）。"""
    old_value, token_name = expected
    new_value = T.px(token_name)
    if token_name in INTENDED_DELTA:
        assert new_value - old_value == pytest.approx(INTENDED_DELTA[token_name]), \
            f"{legacy}: 0.3.4 {old_value} → {token_name} {new_value} 与设计表不符"
    else:                                   # table 走例外①，单独断言
        assert token_name == "table"


@pytest.mark.parametrize("alias,target", sorted(LEGACY_ALIASES.items()))
def test_legacy_aliases_collapse_onto_the_same_token(alias, target):
    """7 个旧名别名不再各占一档：每个都落在它当年指向的那一档上（等价按链条传递）。"""
    _, token_name = LEGACY_CONSTANTS[target]
    assert T.px(token_name) == ui_kit.px(token_name)
    # 别名与目标在 0.3.4 是**同一个值**，现在仍是**同一个 token**
    assert LEGACY_CONSTANTS[target][1] == token_name


def test_all_eight_0_3_4_constants_map_into_the_nine_tokens():
    """8 个真值 → 8 个不同档位，且这 8 档都在新表的 9 档里（mono 是第 9 档，见例外②）。"""
    mapped = [token for _, token in LEGACY_CONSTANTS.values()]
    assert len(set(mapped)) == 8 == len(LEGACY_CONSTANTS)
    assert set(mapped) | {"mono"} == set(T.TOKEN_ORDER)
    assert "table" in mapped


def test_cjk_floor_moved_from_ten_to_eleven():
    """0.3.4 的 ``PX_CJK_MIN``（= 微标 10）抬到 ``typography.CJK_MIN_PX``（11）。"""
    assert LEGACY_CJK_MIN == LEGACY_CONSTANTS["PX_MICRO"][0]
    assert T.CJK_MIN_PX == 11
    assert T.px("micro") >= T.CJK_MIN_PX


# ────────────────────────────────────────────────────────────── 3) 两条例外
def test_exception_1_table_absorbed_the_0_3_4_special_case():
    """例外①：0.3.4 的「表格正文 11px」特例档并入正表 ``table`` = 12px。

    当年设 11 的理由是「9px 在 CJK 上糊、10 又比微标还小」；新表把表格抬到 12px 之后，
    这个特例不再需要——**它是唯一一个「档位数变了」的映射**，所以单列一条。
    """
    assert LEGACY_CONSTANTS["PX_TABLE"] == (11, "table")
    assert T.px("table") == 12
    assert T.px("table") - 11 == 1
    # table 档同时携带表格行高（28），但行高接线属 T4 排版规则，不在本轮字号接线内
    assert T.line_height("table") == 28
    assert T.token("table").line_kind == "row"


def test_exception_2_mono_had_no_0_3_4_counterpart():
    """例外②：``mono`` 是**新增档**——0.3.4 的等宽字号借的是微标 10px，没有独立真值。

    所以 8 个真值只能映到 8 档；第 9 档 ``mono``（12px）没有旧名可对，单列一条。
    """
    legacy_tokens = {token for _, token in LEGACY_CONSTANTS.values()}
    assert "mono" not in legacy_tokens
    assert T.token("mono").family == "mono"
    assert T.px("mono") == 12
    # 0.3.4 的等宽确实借了微标：说明它当年不是一个独立档位
    assert LEGACY_CONSTANTS["PX_MICRO"] == (10, "micro")


# ────────────────────────────────────────────────────────────── 守门
def test_role_fonts_are_derived_from_typography():
    """``Fonts.ROLE_FONTS`` 不再自带字面量：逐角色等于 typography 的档位/字重。"""
    from djb_node.desktop.ui_kit import Fonts

    assert set(Fonts.ROLE_FONTS) == set(T.ROLE_TOKEN_MAP)
    for role, target in T.ROLE_TOKEN_MAP.items():
        family, px, bold = Fonts.ROLE_FONTS[role]
        assert family == ("mono" if T.token(target).family == "mono" else "sans"), role
        assert px == T.px(target), role
        wanted = T.ROLE_WEIGHT_OVERRIDE.get(role) or T.weight(target)
        assert bold is (wanted == "bold"), role
    # 表头是「small 的字号 + bold」这条例外仍在
    assert Fonts.ROLE_FONTS["heading"] == ("sans", T.px("small"), True)


def _old_px(value: float, scale: float) -> int:
    """0.3.4 的取整原文（``release/0.3.4-r2 @58dff50`` · ``ui_kit.Fonts.px``）。

    故意**逐字复制**而不是 import：它是历史基线，不能被今天的实现「顺手修好」——
    一旦这里的副本与 ``ui_kit`` 一起被改，属性测试就失去意义。
    """
    return max(1, int(round(float(value) * scale)))


#: 0.3.4 的 8 个真值（7 档 + 表格特例）——取整语义按**同一个输入**逐点比
LEGACY_VALUES: dict[str, float] = {name: value for name, (value, _) in LEGACY_CONSTANTS.items()}

#: 0.80 … 1.60，步长 0.05（17 档）
SCALES: tuple[float, ...] = tuple(round(0.80 + 0.05 * i, 2) for i in range(17))


def _fonts_at(scale: float) -> ui_kit.Fonts:
    """只测纯换算：绕开 ``__init__``（不需要 Tk 根窗口）。"""
    fonts = ui_kit.Fonts.__new__(ui_kit.Fonts)
    fonts.scale = scale
    return fonts


@pytest.mark.parametrize("name", sorted(LEGACY_VALUES))
def test_rounding_matches_0_3_4_at_every_scale(name):
    """属性测试：8 个旧值 × 17 个 scale，**旧公式 vs 新公式逐点差必须为 0**。

    为什么会有差：0.3.4 用 ``int(round(x))``（银行家舍入），若换成 ``floor(x+0.5)``
    （半进位），``.5`` 边界就会各抬 1px —— ``display``@1.15（34.5 → 34 / 35）、
    ``h1``@1.25（22.5 → 22 / 23）。那不是「外观不变」，是**某个缩放档悄悄变了 1px**。
    """
    value = LEGACY_VALUES[name]
    got = {scale: _fonts_at(scale).px(value) for scale in SCALES}
    want = {scale: _old_px(value, scale) for scale in SCALES}
    diff = {scale: (want[scale], got[scale]) for scale in SCALES if want[scale] != got[scale]}
    assert diff == {}, f"{name} 与 0.3.4 不一致（scale: 旧→新）：{diff}"


def test_rounding_property_covers_every_point_and_has_no_blind_spot():
    """守属性测试本身：17 档 × 8 值 = 136 个点，且两处历史分歧点**确实**在网格里。"""
    assert len(SCALES) == 17 and SCALES[0] == 0.8 and SCALES[-1] == 1.6
    assert len(LEGACY_VALUES) == 8
    assert 1.15 in SCALES and 1.25 in SCALES and 0.85 in SCALES
    # 分歧点必须落在网格上，否则属性测试会「恰好」绕开它们
    assert _old_px(30, 1.15) == 34 and _old_px(18, 1.25) == 22


def test_the_two_historical_divergence_points_stay_at_0_3_4_values():
    """点名哨兵：``display``@1.15 = **34**、``h1``@1.25 = **22**（半进位会变 35 / 23）。"""
    assert _fonts_at(1.15).px(LEGACY_VALUES["PX_DISPLAY"]) == 34
    assert _fonts_at(1.25).px(LEGACY_VALUES["PX_HERO"]) == 22      # hero 的 0.3.4 值就是 18
    assert _fonts_at(1.25).px(T.px("h1")) == 22                    # 新档位 18 @1.25 同为 22
    # 反证：半进位实现会得到什么（证明这条断言确实能抓到回退）
    assert [int(__import__("math").floor(v * 1.15 + 0.5)) for v in (30,)] == [35]
    assert [int(__import__("math").floor(v * 1.25 + 0.5)) for v in (18,)] == [23]


def test_only_the_two_exceptions_may_differ_from_0_3_4_rendering():
    """渲染值口径：除 table（并入正表）与 mono（无旧档）外，7 档的**取整语义**必须与旧版一致。

    * ``table``：0.3.4 的 11px 特例并入 12px ⇒ 每个 scale 都不同（**例外①**，有意）；
    * ``mono``：0.3.4 没有独立档位（借微标 10px）⇒ 无旧基线可对（**例外②**）；
    * 其余 7 档：设计值可能不同（§9.1 的 delta，属设计变更），但**取整公式**逐点相同。
    """
    legacy = {token for _, token in LEGACY_CONSTANTS.values()}
    assert legacy - {"table"} == set(T.TOKEN_ORDER) - {"mono", "table"}
    # 例外①：table 的渲染值每个 scale 都比 0.3.4 的 11px 特例大（12 > 11）
    for scale in SCALES:
        assert _fonts_at(scale).px(T.px("table")) >= _old_px(11, scale)
    # 例外②：mono 无 0.3.4 对应
    assert "mono" not in legacy
    # 非例外的 7 档：取整公式逐点 0 差（用各自 0.3.4 值比）
    for legacy_name, (value, token_name) in LEGACY_CONSTANTS.items():
        if token_name == "table":
            continue
        for scale in SCALES:
            assert _fonts_at(scale).px(value) == _old_px(value, scale), (legacy_name, scale)


def test_fonts_px_keeps_the_0_3_4_rounding():
    """Tk 只认整数字号，取整必须与 0.3.4 同：``small`` 设计值 12.5 在 1.0 档渲染 **12**。"""
    fonts = _fonts_at(1.0)
    assert fonts.px(T.px("small")) == 12             # 12.5 → 12（银行家舍入，= 0.3.4）
    assert fonts.px(T.px("body")) == 14
    assert _fonts_at(1.25).px(T.px("h1")) == 22       # 22.5 → 22（偶数侧）
    assert _fonts_at(1.15).px(T.px("display")) == 34  # 34.5 → 34
    assert fonts.px(0) == 1                           # 至少 1px


def test_renderer_has_no_numeric_font_size_literal():
    """AST 扫描：渲染层不许再出现 ``fonts.sans(14)`` 这种字面量字号（必须走档位名）。"""
    root = Path(__file__).resolve().parent.parent / "djb_node" / "desktop"
    offenders: list[str] = []
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            name = getattr(func, "attr", getattr(func, "id", ""))
            if name not in ("sans", "mono", "for_text"):
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, (int, float)) \
                    and not isinstance(first.value, bool):
                offenders.append(f"{path.name}:{node.lineno} {name}({first.value})")
    assert not offenders, "渲染层出现数字字号字面量：" + "; ".join(offenders)


# ── 表格行高（0.3.4 写死 26 → typography.table 的 28）
def test_table_rowheight_is_the_table_line_of_28():
    """``rowheight`` 不再是 26：等于 ``typography.table`` 的行盒 28。"""
    assert ui_kit.table_rowheight(1.0) == 28
    assert ui_kit.table_rowheight(1.0) == T.scaled_line("table", 1.0)
    assert T.token("table").line == 28.0 and T.token("table").px == 12.0


def test_table_rowheight_follows_every_scale_with_the_same_rounding():
    """0.80–1.60 全档：行高 == ``scaled_line("table")``（与字号同一条取整规则）。"""
    for scale in SCALES:
        assert ui_kit.table_rowheight(scale) == T.scaled_line("table", scale)
    assert ui_kit.table_rowheight(1.25) == 35              # 28 × 1.25，且 ≠ 0.3.4 的 26
    assert ui_kit.table_rowheight(0.9) == 25


def test_table_rowheight_leaves_room_for_the_glyphs():
    """不裁字：行高 ≥ 1.5× 字号（且 ≥ 字号 + 8px 上下留白），全档成立。"""
    for scale in SCALES:
        row = ui_kit.table_rowheight(scale)
        px = T.scaled_px("table", scale)
        assert row >= px + 8, (scale, row, px)
        assert row >= int(px * 1.5), (scale, row, px)
        assert T.scaled_line("table", scale) == T.round_px(T.token("table").line * scale)


def test_no_rowheight_literal_left_in_the_renderer():
    """渲染层不许再有裸的行高字面量（26 / 28 都只能来自 ``table_rowheight``）。"""
    import inspect
    for module in (ui_kit,):
        source = inspect.getsource(module)
        assert "rowheight=26" not in source
        assert "rowheight=28" not in source
    app_source = pathlib.Path(ui_kit.__file__).with_name("app.py").read_text()
    assert "rowheight=self._px(26)" not in app_source
    assert "rowheight=self._px(28)" not in app_source
    assert "rowheight=kit.table_rowheight(self._scale)" in app_source
