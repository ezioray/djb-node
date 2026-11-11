# DJB 联脑 · 客户端变更日志（djb-node）

> 产品版本记录在这里（`__version__` / `pyproject.toml [project].version` / Helm `appVersion` 三处同源，
> 测试 `tests/test_machine_room.py` 会断言它们一致）。
> **协议**契约变更另见仓库根 `CHANGELOG-协议.md`（口径是 `spec/openapi.yaml` 的 `info.version`），两者不混。
> `VERSION.txt` 是**构建期生成**的（built_at / commit / platform），由 `ops/release/build/*` 写入包内，
> 仓库里不手写 —— 手写一份静态文件会被误读成构建时间。

## 0.3.2 · 2026-11-11

RC9 **生产分发**（Windows x64 便携包 + Linux x64 源码包）。**没有**协议契约变更（`openapi.yaml` 未动），
**没有**新增运行时依赖；版本号 `djb_node.__version__` / `pyproject.toml` / Helm `appVersion` 三处同源升到 `0.3.2`。

### 修复
- 官方特供预置档 `profiles/official-supply/config.json` 的 `gateway_url` 由占位域
  `https://gateway.example.invalid` 改为正式环境 `https://tkbank.ai`（与 `update_source` 同源；
  0.3.0 分发物仍带占位域，是本轮的发布缺陷）。
- 桌面端首启竞态：daemon 尚未注册完成时账户页永久显示「未登录节点」、绑定入口被藏（`9cdb002`）。

### 分发
- Windows 便携包内置 `config.json`：显式 `gateway_url=https://tkbank.ai` +
  `update_source=https://tkbank.ai/download/files/update-manifest.json`，不再依赖空值（内置 mock 源）。
- 未签名：owner 已接受公测阶段 Windows 未签名出包，清单如实标 `unsigned-test`（不再等 EV 证书）。
- `tests/rc8/t3_offline_verify.py` 的 `CURRENT_VERSION` 常量随版本同步到 `0.3.2`。

## 0.3.0 · 2026-11-10

RC8 **全客户端同步版本**：Windows onefile / macOS arm64 dmg / Linux 源码包 / 机房端离线包 / 官方特供版
五通道统一 `0.3.0`、同一 commit、同一 `djb_node.__version__`、同一 `update_source` 与同一份 MANIFEST。
**没有**协议契约变更（`openapi.yaml` 未动），**没有**新增运行时依赖。

### 安全（更新链）
- 更新信任锚改为**钥环**（`facts.UPDATE_KEYRING`：primary + transition）：清单声明 `key_id` 时**精确取钥**，
  未知/已吊销/过渡到期的 key_id 一律拒；制品签名与清单钥绑定。
- primary 当前是**占位公钥**（仓库内无对应私钥）：`ops/release/update_anchor.py` 的 release gate 拒绝签名/发版，
  直到 owner 在离线签名机生成最终公钥并注入（`ops/release/set-update-anchor.py`）。
- 旧 E2E 锚 `djb-e2e-2026-11-02` **默认吊销**（`UPDATE_REVOKED_KEY_IDS`）；应急覆盖
  `UPDATE_TRANSITION_OVERRIDE` 默认关，且不得越过 `2026-11-24` 硬上限。
- 下载分类修正：制品 HTTP 非 200 => `state=download_failed`（退出码 6），不再冒充 `failed_verify`（7）。

### 分发
- 默认 `update_source` 指向 `https://tkbank.ai/download/files/update-manifest.json`
  （official-supply 预置档 / 机房版 fleet 默认）；`ops/fnos/install-linux-client.sh`
  新增显式 `--update-source` 参数（**默认不写 config**，保持原安装行为）。
- 机房版离线包签名统一 Ed25519（`openssl pkeyutl -rawin`；OpenSSL 3 不支持 dgst+Ed25519），
  `verify.sh` 默认严格验签（有签名无公钥即失败）。

### 测试
- 新增 T1 四发行版 × 三制品矩阵、T2 核心链路（假网关 + `--port 17171`）、T3-a 离线验签 16 用例；
  CI 增加 pytest(3.10/3.11/3.12) / 验签矩阵 / 私钥泄漏自检 / L0 矩阵 job。

## 0.2.5 · 2026-11-09

ROUND3（C-01b）+ 基线红收敛。**没有**协议/契约变更（`openapi.yaml` 未动），也**没有**新增运行时依赖。

### 功能
- **C-01b 周期复探默认开启**：`client.probe.periodic.enabled` 的 `default` 改为 `true`。
  修前默认关闭 ⇒ 探针一过期（15 min）就掉容量，而 `djb status` 仍显示「接单=是」，
  供方只能靠人工 `djb upstream probe` 维持（Windows 0.2.4 真机原文见 ROUND3 报告 win-probe.json）。
  三字段：owner=`client-seat`；expire_at=`2026-12-15T00:00:00+08:00`；rollback_rule=
  `djb flags --disable client.probe.periodic.enabled` 或 **环境变量**
  `DJB_FLAG_CLIENT_PROBE_PERIODIC_ENABLED=false`（压过配置）或 revert 本提交。
  决策留痕：`reports/ROUND3-20261109/DECISIONS.md §D-R3-01`。
- **周期常量与不变式对齐**：`PROBE_PERIODIC_DEFAULT_S` 600 → **450**（= `PROBE_FRESH_S // 2`）。
  修前 600 恒被 `PROBE_PERIODIC_CAP_S`(450) 钳制，常量与实际行为自相矛盾；现在「基础周期 ≤ 半新鲜度」
  是恒成立的不变式，P6 的新鲜度语义（900 s）不变。
- **`djb status` 周期行精确化**：`每 8 分钟`（= `round(450/60)`，已越过自己的上限）→
  `每 7.5 分钟（450 秒，上限 7.5 分钟（450 秒）…）`；`probe.cap_s` 进 status payload，
  渲染层不必再猜上限。其余「还剩多久掉容量」类文案改为**向下取整**（宁可少说 0.5 分钟，不夸大剩余新鲜度）。
- **特性开关支持环境变量闸门**：`config.feature_flag` 取值优先级改为
  「环境变量（`DJB_FLAG_<NAME>`）> 配置 > 登记表默认」；未设该变量时行为与改前逐字一致。
  用途：真机取证/事故处置不必先改配置文件。

### 修复
- **同进程多窗口导航行高漂移**（真缺陷，Xvfb 实测）：`app._build_shell` 曾写
  `kit.NavItem.HEIGHT = self._px(kit.NavItem.HEIGHT)` —— 对**类属性**原地乘 DPI 比例，
  第 N 个根窗口的导航项高 34→35→36→37→41 px，13 项于是在 900 高下「放不下」、侧栏凭空出滚动条。
  现改为从基常量 `kit.NAV_ITEM_HEIGHT` 派生（幂等）；100%/125% 下连续开 5 个窗口高度不漂移（有用例钉住）。
- **`djb status` 安装行不再并列两个版本**：`安装：` 打的是**本机 home 安装记录**
  （只在 `djb init` / 自升级 apply·rollback 时写），覆盖安装不会更新它。记录与运行版本不一致时
  追加事实括注「（本机 home 记录，运行版本 X）」，一致时输出与改前逐字相同。
- `_spawn_daemon` 的 `"daemon.out"` 改用 `_DAEMON_OUT_NAME` 常量（单一口径）；
  删除零引用的 `cli._control_reachable()`（无调用方，见 ROUND3 报告零引用证明）。

### 测试
- **基线红收敛（3/3 有结论）**：
  - `tests/test_daemon_leak_guard.py` 2 failed 根因不是「环境」而是 `tests/process_tree.py::wait_gone`
    把**僵尸进程**（SIGKILL 后未 reap，`/proc/<pid>` 仍在）当成活着 ⇒ 改为按 `/proc/<pid>/stat`
    的状态位判定（Z/X 视为已退出并顺手 `waitpid`）。
  - `test_short_window_scrolls_the_nav_and_keeps_every_item_reachable` 的顺序敏感由上面那条
    **导航行高漂移**导致，已按产品缺陷修复（不动断言）。
  - `test_main_returns_headless_code_without_display` 不再依赖「运行环境没有 DISPLAY」：
    注入「连不上显示」的 Tk 桩 + 清空 `DISPLAY`/`WAYLAND_DISPLAY`，有/无显示器都能跑，
    不再需要 `--deselect`。
- 新增：C-01b 三路径（默认开 / 显式关（配置+环境变量）/ 非法值回退）、周期显示值 ≤ 上限、
  DPI 多窗口不漂移、安装行括注、环境变量开关优先级。

### 已知限制
- macOS 仍无真机验证（仅代码级 + 单测），沿用 0.2.4 结论。
- `安装：` 行的记录版本只在自升级时更新；覆盖安装后它会落后于二进制，属既定设计（status 已加括注说明）。

## 0.2.4 · 2026-11-08

本轮 = 客户端 UI R2 合入 + E2E 补丁包 P4/P6 + 缺陷单 E2E-06 / E2E-09。

### 合并
- 合入 `feat/desktop-ui-parity-20261107@56e9584`：字体 7 档（**中文不进 mono**）、侧栏 4 分区
  （运行 / 收益 / 接入 / 系统，13 视图不删不并）、13 视图宽窄窗不裁切、Windows/Linux DPI 感知
  （125% 不再被 DWM 拉伸）、Linux 滚轮 / 换主题 / 部分刷新 / 备注行四个真 bug 修复，
  以及 0.2.3 的 Windows UTF-8 解码、环境误报、日志页崩溃修复。
- 冲突处保留 1.8.1 契约线语义：共享 Token Plan 视图（仍在「接入配置」之前）、
  `DJB_BOOTSTRAP_DISABLE` 自举开关、限额读不到时不谎报「周/月未启用」。

### 修复
- **P4 / E2E-04 出站通道**：`client.channel.enabled` 默认**开启**（默认关闭时云端派单必然 150 s 超时）；
  心跳响应新增 `channel_required` 时客户端也视为开（落盘 `store.kv=channel`，跨重启生效）；
  **本地显式关闭始终优先** —— `djb flags --disable` 仍是可靠的回滚路径。
- **P4 可见性**：`djb status` 新增「通道：已连接 / 未开启 / 已断开」，只按 wanted/running/state 说证据，
  状态不一致时宁可报已断开，不谎报已连接。
- **P6 / E2E-11 探针超时**：`PROBE_TIMEOUT_S` 改为环境变量可配（`DJB_NODE_PROBE_TIMEOUT_S`，默认 20 s，
  非法/越界回退默认值）。本地 Ollama / thinking 模型（实测 16–21 s）不再必然被判 timeout。
- **P6 / E2E-12 退避越界**：周期复探（含失败退避、模型数缩放）钳到 `PROBE_FRESH_S / 2`，
  杜绝「一次失败退避到 1200 s > 900 s 窗口 ⇒ 每周期掉 5 分钟容量，两次失败永久 0 容量」。
- **P6 可见性**：`djb upstream list` 每行带「最近探针：ok/timeout @ 时间（X 分钟后掉容量）」；
  `djb status` 新增「容量：N/M 个模型可广播」与逐模型「掉容量原因」（未探测 / 探针未通过(分类) / 过期 / 已停用）；
  模型级新增本地字段 `probe_error_class`（只在失败时写，成功即清；不上行）。
- **E2E-06 让路暂停**：`djb resume` 现在会清掉 `paused(yield_paused)` 并**关闭自动让路**
  （重新打开：`djb limits --yield`；`djb status` 的 resume_note 里直接给这条命令）；
  daemon 未运行时按持久化快照走等价路径（改本机 profile），下次 start 不会又回到让路暂停。
  `djb status` 新增「暂停细节」，把让路信号 / 资源超限说清楚，并与用户暂停区分。
- **E2E-09 端口竞态**：`djb stop` 等**端口真正释放**才说「已停止」；等不到就说
  「控制口已关闭，但端口 X 仍被 pid=Y 占用」，不再打一句「已停止」了事；
  `djb start` 失败时回显占用者 pid 与 `daemon.out` 尾行（`[Errno 98]` 这类真实原因），退出码语义不变。

### 测试
- 新增 `tests/test_cli_ports.py`（E2E-09 端口/停止竞态）、`tests/test_cli_status_visibility.py`
  （通道三态 / 最近探针 / 掉容量原因 / 暂停细节），以及通道协商（P4-B）、resume 清让路（E2E-06）用例。
- 收窄套件（desktop + probe + facts + cli + daemon）全绿；13 视图 Xvfb 冒烟 1440×900 / 1180×780 / 900×620
  全部 13/13。逐项证据见 `reports/ROUND2-20261108/client/`。

### 已知限制
- macOS 仍无真机验证（沿用 UI R2 结论：仅代码级 + 单测）。
- `test_daemon_leak_guard` 在本机（NAS）与改前基线（6f4ef66）**同样失败**：进程树回收超时，属环境问题，
  与本版改动无关（对照证据见报告）。