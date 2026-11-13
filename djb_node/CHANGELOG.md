# DJB 联脑 · 客户端变更日志（djb-node）

> 产品版本记录在这里（`__version__` / `pyproject.toml [project].version` / Helm `appVersion` 三处同源，
> 测试 `tests/test_machine_room.py` 会断言它们一致）。
> **协议**契约变更另见仓库根 `CHANGELOG-协议.md`（口径是 `spec/openapi.yaml` 的 `info.version`），两者不混。
> `VERSION.txt` 是**构建期生成**的（built_at / commit / platform），由 `ops/release/build/*` 写入包内，
> 仓库里不手写 —— 手写一份静态文件会被误读成构建时间。

## 0.3.6 · 2026-11-13

**接入页精简 + 收款地址提交云端**（owner 2026-11-13 三条客户端指令）。`__version__` /
`pyproject.toml` / Helm `appVersion` 同源升到 `0.3.6`（0.3.5 已发 macOS，Windows 用 0.3.6，
不覆盖已发布制品）；**没有**协议契约变更（`openapi.yaml` 未动），**没有**新增运行时依赖。

> ⚠ **本分支只含 0.3.6 的新增改动**：0.3.5 的内容（`7779281` 版本 bump、`8ad418a` 灰度阶梯
> `(1,10,50,100)` + wave 0 内部档）在 `release/0.3.5` 分支上，**不在本分支基线 `5eea2fd` 里**。
> 构建 0.3.6 前必须先把 `release/0.3.5` 合入，否则 0.3.6 会**回退** 0.3.5 的灰度阶梯
> （见 `reports/UX-20261113/W5-CLIENT-036/REPORT.md` §6）。

### 接入配置页精简（owner：「接入的是贡献节点，不是要接入使用」）
- 页面口径改为**贡献节点**：绑定节点 → 填 USDT 收款地址 → 选价格模式；
  副标题不再提「上游地址」，说明文案改成「不再填 API Key / 上游地址」。
- **UI 移除三个字段**：`API Key`、`OpenAI 请求地址`、`Anthropic 请求地址`
  （`AccessConfig` 的 `api_key` / `openai_base_url` / `anthropic_base_url` 字段与
  `validate()` 规则**全部保留**，老配置文件照常读——**UI 移除、模型保留**）。
- 节点凭证（= 节点密钥，排障要用）保留但**折叠**进「高级（排障用）」区，默认不展开
  （`Field.advanced` / 渲染层 `_render_fields` 的折叠区）。
- 收款地址输入框不再预填掩码（`Field.prefill=False`）：旧行为下客户不动地址直接点「保存并回读」
  会把 `TQn9Xa…7f3d` 这种掩码当地址写回配置（必然校验失败）。
- 节点绑定区保留「复制节点 ID」并新增「粘贴绑定码绑定」（复用既有 `account.bind` 动作，不新造流程）。

### USDT 收款地址提交云端（owner 方案 B：客户端直接提交云端）
- 新增 `POST/GET /api/v1/node/payout-address`（头 `X-Client-Key`，与 cloud 侧契约逐字对齐）：
  `djb_node/desktop/nodecloud.py` 加 `payout_address()` / `submit_payout_address()`；
  `datasource.submit_payout_address()` 是**本层唯一写操作**，只在 worker 线程调用。
- 状态显示（字段 hint + 卡片）：未提交 / 待审核 / 已生效 / 已拒绝 / 已撤销 / 冷却中；
  **读不到就说「暂时读不到云端状态」**，绝不把未知渲染成「已生效」（`desktop/payout.py`）。
- 提交不阻塞 Tk 主线程：复用 W1 的 `RefreshWorker`（线程取数 + `after` 轮询回投），
  成功/失败都有 toast，**没有** `except: pass`。
- 401/403 给可执行文案（「节点还没绑定账户，先在网页端完成绑定」）；409 冷却 / 422 风控 /
  400 格式各自一句话，均带「地址未变更」。
- **只支持 Polygon（ERC20）地址**：提交前本地拦 `T` 开头（TRC20）并提示
  「TRC20 暂不支持，请填 Polygon（ERC20）地址」，不发请求；`validate_address()` 的 TRC20
  分支保留（老配置兼容）。理由见 cloud 侧三道闸（`CHAIN_ID=polygon` / 表约束
  `payout_addresses_chain_ck` / 入库 `address.lower()` 抹平 base58 大小写）。

### 包含 W1（`fix/ux-20261113`，基线 `5eea2fd`）的 4 条修复
- **S1 黑窗**：`platform/no_window.py` 唯一封装，`djb_node/` 16/16 子进程调用点全部无窗。
- **S2 主线程卡死**：`desktop/refresh_worker.py`，取数搬到 worker 线程 + `after` 轮询。
- **S3 节点 ID 空**：`DJB_HOME="None"`、绑定只查手填值、`NodeCloudClient` 缺 `binding()` 三个真 bug。
- **S4 复制无反馈**：`copy_text` + `clipboard.update()` + toast。

### 版本同源（三处 + Helm）
- `djb_node/__init__.py`（`__version__`，`facts.CLIENT_VERSION` 由它派生）＝
  `pyproject.toml [project].version` ＝ `deploy/machine-room/helm/djb-node/Chart.yaml appVersion`。
- `tests/rc8/t3_offline_verify.py` 的 `TARGET_VERSION` 抬到 `0.3.7`（必须比已装版本大，
  否则 update 判 `up_to_date`，三个正例会假红灯）。

### 未做 / 跨仓
- **未构建、未发布**：`win-build.py` / `ci/pkgbuild.py` 由 release-manager 在构建机跑。
- **TRC20 支持需独立立项**：要迁移出款链（Polygon → 多链）、加 `chain_id` 存储维度、
  接 TRON 出款轨；本版次只做「拦 + 说清楚」。
- 云端 `payout-address` 接口未部署（cloud 侧 `feat/ux-payout-20261113`），联调需等其发布。

## 0.3.4 · 2026-11-12

**到期墙 + 五通道版**（RC10 配套）。版本号 `__version__` / `pyproject.toml` / Helm `appVersion`
同源升到 `0.3.4`；`tests/rc8/t3_offline_verify.py` 的 `CURRENT_VERSION` 改为跟随
`facts.CLIENT_VERSION`（不再手工同步）。**没有**协议契约变更（`openapi.yaml` 未动），
**没有**新增运行时依赖，**没有**改动更新信任锚。

### 到期墙（10 个开关 → 2027-06-30）
- `facts.FEATURE_FLAGS` 里 10 条 `expire_at` 由 `2026-12-31` 改为 `2027-06-30`：
  `client.token.compare` / `client.channel` / `client.ipc.windows` / `client.model_sync` /
  `client.notifications` / `client.quotes.publish` / `client.relay` / `client.tray.control` /
  `client.update.mock_signature` / `supply.attestation`（均以 `.enabled` 结尾）。
  到期语义是「强制为关闭」：`config.feature_flag()` 先判 `flag_expired()` 再读配置，
  所以**靠配置/环境变量续命无效**，只能重出包。
- 其中 `client.channel.enabled`（default=True）到期会让**所有未升级客户端接不到单**
  （派单 150 s 超时 → 503，而 `djb status` 仍显示接单=是）；`client.ipc.windows.enabled`
  （default=True）到期让 Windows 控制不可用。
- `client.probe.periodic.enabled` 0.3.3 已续到 2027-06-30，本次不动。
- 同步硬编码断言：`test_feature_flags`（EXPIRE_DAY / AFTER_EXPIRE / 默认值）、
  `test_channel_client` / `test_model_sync` / `test_notifications` / `test_token_compare` /
  `test_windows_ipc`（fixture 的假元数据一起改，避免 12-31 后测试自坏）、
  `ipc_windows.py` 注释、`tests/windows_control.py` docstring、`docs/rfc/windows-control-ipc.md`。

### 特供档（official-supply）到期哨兵
- 新增 `tests/test_official_supply_profile.py`（3 例）：profile 显式开启的开关必须已登记在
  `facts.FEATURE_FLAGS`；到期余量 < 90 天即红；`client.relay` / `client.quotes.publish`
  是业务模式锁（默认 False，不能从 profile 删）。目的：把「profile 写 true 但开关到期后被压成关」
  变成 CI 红灯，而不是客户侧报价环静默不启动。
- `profiles/official-supply/README.md`：写明三项靠 `facts` 续期、不是 profile 的 `true` 保命，
  并给排查口径（`djb flags --json` 看 `expire_at`）。
- profile 本身**不改**：三个 `true` 保留（`relay` / `quotes.publish` 的默认值是 False，删了就掉档位；
  `probe.periodic` 默认已是 True，保留仅为显式）。

### 五通道更新源 + `--edition`（cherry-pick 28cf9d1、ffffeb2）
- 新增 `djb_node/update_source.py`：五通道契约单一真源；`resolve()` 给
  `origin ∈ {config, edition, mock}`、`kind ∈ {official-manifest, file, custom, mock}`。
- `djb init --edition {consumer|machine-room|official-supply}`：落该通道的签名更新源
  （`--update-source` 优先级更高，已有自定义源不被覆盖）；`config.edition` 空则不写回，
  0.3.0 配置形状不变。`djb update --status` 增 `source_kind` / `source_origin` / `edition` /
  `source_channel`（只加不改）。
- 载体补齐：机房版 `offline/config.template.json` + `manifest.json#update_source` +
  `install.sh --update-source/--djb-home/--config-only`；Windows `config.json` +
  `start-windows.bat`；Linux 源码包 / official-supply 的 `VERSION.txt#update_source` +
  `README-INSTALL.txt` 的 `--edition` init 命令。
- 新增仓内打包器 `ci/pkgbuild.py`（linux-src / official-supply / machine-room-offline /
  windows-assets，从 commit 出包 + 自检 + 拒绝写发布面）与 `ci/check_pkg_manifest.py`。
- 人读输出不打印清单 URL（构建自检对 `tkbank.ai` 是硬护栏），完整 URL 只在
  `update --status --json` 里。
- 机房版 fleet 模板补 `"edition": "machine-room"`（`deploy/machine-room/fleet/config.json.template`）：
  批量铺机时也带档位，`update --status` 能显示 `origin=config` 之外的 edition 归属。

### 合并说明
- 两个 cherry-pick 的冲突全在版本号与 CHANGELOG：按 0.3.3 基线解（版本保留 0.3.3，由本版次统一抬到 0.3.4），
  CHANGELOG 按 `0.3.3 → 0.3.2 → 0.3.1 → 0.3.0` 时间倒序合并（0.3.1 段此前不在主线）。
- `tests/rc8/t3_offline_verify.py` 取 incoming 方案（`CURRENT_VERSION = facts.CLIENT_VERSION`），
  `TARGET_VERSION` 由 `0.3.2` 抬到 `0.3.5`（必须比已装版本大，否则 update 判 up_to_date）。

### 未做 / 跨仓
- **未构建任何制品、未发布、未上传**。构建在 QA 通过后进行（`ci/pkgbuild.py` 已就位）。
- 云端同名开关 `supply.attestation.enabled`（`app/integrity/features.py`）仍 `2026-12-31`，
  待 88 号 §8「批次 2」在 cloud 仓同步，否则出现「客户端采、云端不认」。
- `client.update.mock_signature.enabled` 续期仅为消除到期噪声，**必须保持默认关闭**
  （它是验签旁路，开 = 安全事故）。
- macOS dmg 需 Apple 凭据与公证，不在本版次（`--edition` 代码路径已就绪）。

## 0.3.3 · 2026-11-12

**到期修复版**（RC10 配套）。**没有**协议契约变更（`openapi.yaml` 未动），**没有**新增运行时依赖；
版本号 `djb_node.__version__` / `pyproject.toml` / Helm `appVersion` 三处同源升到 `0.3.3`。

### 修复
- **探针开关到期续期**：`client.probe.periodic.enabled` 的 `expire_at` 由 `2026-12-15` 改为
  `2027-06-30`（`facts.py`）。原值会在 **2026-12-16 起静默 fail-closed**：`FeatureSwitch` 先判期
  再看 env，到期后 `DJB_FLAG_CLIENT_PROBE_PERIODIC_ENABLED=true` 也救不回来 ⇒ 周期复探关闭
  ⇒ 探针 15 min 过期后供方容量归零，而 `djb status` 仍显示「接单=是」。
  依据 `reports/RLS-20261109-CLIENT-025/DECISION-PROBE-FLAG-RENEWAL.md`（cherry-pick `e88e815`）。
- 测试口径随之改为**从 `expire_at` 推导到期日**（+1 天），下次续期不再需要改测试逻辑；
  并补齐 `tests/test_upstream_gating.py` 中同一条写死 `2026-12-15` 的断言（上游 `e88e815`
  漏改该文件，pick 后该用例会转红）。

### 影响面
- 仅影响 **0.3.3 及之后**的构建。已分发的 0.3.2 包内仍是 `2026-12-15`，**必须在 12-15 前替换**，
  否则到期后现网供方会静默掉容量（不报错，只表现为没人接单）。
- 回滚：`git revert` 本版本，或回退到 0.3.2 制品；开关本身可用
  `DJB_FLAG_CLIENT_PROBE_PERIODIC_ENABLED=false` / `djb flags --disable` 立即停用。

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
## 0.3.1 · 2026-11-11

RC8.3-C **收口版**：把 0.3.0 留下的 P1（五通道 `update_source` 未显式化）与 B7（CI `linux-matrix`
占位）关掉。**没有**协议契约变更（`openapi.yaml` 未动），**没有**新增运行时依赖，
**没有**改动更新信任锚（验签仍由 `facts.UPDATE_KEYRING` 的 Ed25519 钥环负责）。

### 五通道更新源显式化（P1-3 收口）
- 新增 `djb_node/update_source.py`：五通道契约单一真源（channel / edition / carrier /
  update_source），五通道**指向同一份签名清单**——多份清单 = 多套签名 = 回滚面翻倍。
- 客户端侧显式化：`resolve()` 返回 `origin ∈ {config, edition, mock}`、
  `kind ∈ {official-manifest, file, custom, mock}`；`djb update --status` 新增
  `source_kind` / `source_origin` / `edition` / `source_channel` 四个**只加不改**的字段。
- `djb init --edition {consumer|machine-room|official-supply}`：显式落该通道的签名更新源；
  `--update-source` 优先级更高；**已有自定义源（内网镜像/本地目录）不会被 `--edition` 覆盖**。
- 载体补齐（0.3.0 时只有 official-supply 有）：
  - 机房版：`offline/config.template.json` + `manifest.json#update_source` +
    `install.sh --update-source/--djb-home/--config-only`（装完直接落 0600 的 config.json）；
  - Windows：`ci/pkgbuild.py --kind windows-assets` 产出 `config.json` + 带
    `--update-source` 的 `start-windows.bat`（并入 zip 的步骤见发布单 §Windows）；
  - Linux 源码包 / official-supply：`VERSION.txt#update_source` + `README-INSTALL.txt`
    里那条带 `--edition/--update-source` 的 init 命令。
- **0.3.0 兼容**：`update_source` 为空且没给 edition → 仍走 mock（不联网）；写回配置形状不变
  （没有 edition 时不写 `edition` 键）；旧客户端读到 `edition` 当未知字段进 `extra`；0.3.0 的
  `update-manifest.json`（不含 `key_id`）照旧可验签。
- 人读输出（`env` / `--version` / `--help` / 打包器 stdout）**不打印清单 URL**：
  构建自检对 `tkbank.ai` 是硬护栏（命中即不出包），完整 URL 只在 `update --status --json`
  与 `BUILD_RESULT` 的机器可读字段里。

### CI / 构建（B7 去占位）
- 新增仓内打包器 `ci/pkgbuild.py`（`linux-src` / `official-supply` / `machine-room-offline` /
  `windows-assets`）：从 commit 出包（`git archive` 白名单）、内置启动器、`VERSION.txt`、
  `SHA256SUMS.txt`、自检（`./djb --version` + `tkbank.ai` 护栏）、拒绝写发布面。
  与 `ops/release/build/*.sh` 的区别：前者假设 NAS 工作区布局，CI 里跑不了。
- 新增 `ci/check_pkg_manifest.py`：包内自洽（`VERSION.txt` / `SHA256SUMS.txt` / wheel）+
  **五个包逐个断言显式 `update_source`** + 可选与 MANIFEST 草案逐项对齐。
- `.github/workflows/ci.yml`：新增 `package` job（4 kind 并行构建 + 上传 artifact）；
  `linux-matrix-l0` 从「无制品就 exit 0」改成**下载真实制品 → 缺制品即失败**，
  再跑 T1 矩阵（4 发行版 × 3 制品容器校验）。

### 测试
- 新增 `tests/test_update_source.py`（38 例）：通道表/分类/解析优先级、0.3.0 兼容、
  CLI `init --edition`、`update --status` 新字段、四个打包 kind 的载体断言、发布面保护。
- `tests/test_machine_room.py`：离线包清单从 3 文件变 4（新增 `config.template.json`），
  新增「`--djb-home` 落显式 update_source / `--dry-run` 不写盘 / `--config-only` 缺参数即拒」。
- 基线红修正（随 96194dc 回合）：`test_update.py` 的占位公钥断言改为「owner 已注入真实锚」。

### 已知未闭（不阻塞发版，见发布单）
- Windows Authenticode：无 EV 证书 ⇒ 仍 `unsigned-test`；SmartScreen 首启会拦一次。
- macOS：0.3.1 只给构建脚本与验收清单；真 dmg + 公证需 Apple 凭据（owner/djb-release）。
- 自动更新放量仍是 `rollout.wave=1`（5%）；wave 2 需 owner 在 24h 观察后决定。

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