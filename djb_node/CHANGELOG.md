# DJB 联脑 · 客户端变更日志（djb-node）

> 产品版本记录在这里（`__version__` / `pyproject.toml [project].version` / Helm `appVersion` 三处同源，
> 测试 `tests/test_machine_room.py` 会断言它们一致）。
> **协议**契约变更另见仓库根 `CHANGELOG-协议.md`（口径是 `spec/openapi.yaml` 的 `info.version`），两者不混。
> `VERSION.txt` 是**构建期生成**的（built_at / commit / platform），由 `ops/release/build/*` 写入包内，
> 仓库里不手写 —— 手写一份静态文件会被误读成构建时间。

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