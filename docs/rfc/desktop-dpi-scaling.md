# RFC · 桌面端 DPI 缩放（Windows 125% / 150%，Linux Xft）

- 状态：implemented（2026-11-07）
- 作者 / 日期：lead / 2026-11-07
- 关联模块 / 契约版本：`djb_node/desktop`（无契约变更，仅桌面壳）
- 需要谁批：维护者

## 1 问题

Windows 交互桌面是 **2560×1600 @125%**（逻辑 2048×1280）。Tk 默认 DPI-unaware，
Windows 的 DWM 会把整个窗口位图拉伸 1.25×：

1. 文字发糊（像素被插值），截图取证也糊；
2. 1180×780 的窗口在 125% 下只占 944×624 逻辑 px，观感比设计稿小 20%；
3. 窗口坐标与物理像素不一致，截图/自动化容易裁边。

Linux 在 Xft.dpi=120/144 的高分屏上同理（Tk 会自己按 `tk scaling` 放大字体，
但 app 的像素几何不放大，字与容器比例失衡）。

## 2 目标与非目标

- 目标：在 125%/150% 下，**文字清晰（不拉伸）+ 物理尺寸与 96dpi 设计稿一致**
  （1180×780 逻辑 → 1475×975 物理 @125%）。
- 非目标：不做逐显示器 DPI 切换（Per-Monitor V2 只开感知，不监听 `WM_DPICHANGED`）；
  不改 `gui/theme.py` 的字体 pin；不做 macOS Retina 特判（见 §7 未验证）。

## 3 方案

顺序（`DesktopApp.__init__`）：

1. `platform_ui.enable_high_dpi()` —— 必须在 `Tk()` 之前；PMv2 → 10.0.15063+
   的 `SetProcessDpiAwareness(2)` → `SetProcessDPIAware()` 三级回退，幂等。
2. 建 Tk 之后读倍率：`DJB_UI_SCALE` 环境变量优先，否则
   `platform_ui.ui_scale(root)`（`winfo fpixels 1i / 96`，已 clamp 到 1.0–2.0；
   aqua 返回 1.0）。
3. `self._px(n) = round(n * scale)`，只作用于**布局常量**：
   初始几何、minsize、侧栏宽、内容 padding、页头/页脚高、状态条高、`_wrap_hint` 的侧栏扣除。
4. 字体：`kit.fonts_for(root, scale=scale)` 在 `apply_kit_styles` **之前**调用
   （缓存按 root 键，先到先得；晚调会让 ttk 拿到 1.0 的字体）。
5. 表格行高：`style.configure("Kit.Treeview", rowheight=self._px(26))`，
   避免 14px 字挤在 26px 行里。
6. 几何收缩：`platform_ui.initial_geometry/fit_minsize` 按屏幕减 32px 边距收缩，
   1366×728 / 1024×600 上不出界（XPLAT-04）。

**只加不改**：`platform_ui.py` 是新文件；`app.py` 只改常量取值，不动控件树/接口。

## 4 安全与隐私

无凭据、无网络、无账本。环境变量只影响像素取值，非法值回落 1.0。

## 5 账本与资金

不涉及。

## 6 特性开关

| 开关 | 默认 | owner | expire_at | rollback_rule |
| --- | --- | --- | --- | --- |
| `DJB_UI_SCALE`（覆盖倍率） | 未设 = 自动探测 | lead | 2027-02-01 | 设 `DJB_UI_SCALE=1.0` 立即回到 96dpi 布局；无需改代码/重启数据 |

说明：这是**回滚/测试用覆盖**，不是灰度开关；自动探测失败时等价于 `1.0`。

## 7 测试与验收

- 单测：`tests/test_desktop_platform_ui.py`（54 passed，含假 windll 三级回退）。
- Linux Xvfb：`tools/smoke-views.sh <wt> 1180x780 6` 13/13；`DJB_UI_SCALE=1.25` +
  1475×975 13/13；`ui_audit.py` 1475×975@1.25 仅 1 条滚动型 `overflow-bottom`。
- Windows 真机：`enable_high_dpi` 后窗口物理 1475×975，截图无 DWM 拉伸；
  13 视图实拍见 `evidence/win-r2/`。
- macOS：**未在真机验证**。`ui_scale` 在 aqua 返回 1.0，字体仍按点值
  （`font_size_arg`）；Retina 观感需真机复核（`winfo fpixels 1i` / `tk scaling`）。

## 8 回滚

代码回滚：revert 本合并提交即可（`platform_ui.py` 独立文件，app.py 改动集中在
`__init__` 与 6 个常量）；运行时回滚：`DJB_UI_SCALE=1.0`。
