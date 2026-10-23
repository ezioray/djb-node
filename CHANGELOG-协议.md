# 协议变更记录（CHANGELOG）

> 规则：同一 MAJOR 内只加不改；破坏性变更走 MAJOR 并提前 ≥90 天通告。
> 每条必须写全：版本 / 日期 / 类型 / 影响面 / 兼容矩阵 / 复核人。

## 1.2.2 · 2026-10-23 · MINOR

**类型**：MINOR（新增 3 条路径 / 4 个操作 / 7 个 schema / 1 个 tag；无删除、无类型更改、无既有字段语义变更）

**变更**
1. 新增 `GET /healthz`（`operationId: getHealthz`）：存活 / 就绪探针，只返回 `status`（`ok` / `degraded`）、
   `version`、`checked_at` 与依赖健康摘要，不暴露拓扑 / 地址 / 凭据；`security: []` 公开端点。
2. 新增 `POST /internal/ops/audit/entries`（`appendAuditEntry`）：业务模块写审计的唯一入口。
   append-only、HMAC-SHA256 链（`prev_hash` + canonical JSON）、幂等键去重；高危动作缺
   `reason_code` / `ticket` / `before` / `after` / `approval_chain` 即拒（403 + `40301`）；
   断链 / 并发写锁返回 409（`40901`）或 423（`42301`）。安全域 `internalMTLS`（mTLS + 服务令牌，公网不可达）。
3. 新增 `GET /internal/ops/audit/entries`（`listAuditEntries`）：只读查询（`from_seq` / `action` /
   `target` / `limit`），**不提供** UPDATE / DELETE，供 M14 后台与排障读取。
4. 新增 `POST /internal/ops/audit/verify`（`verifyAuditChain`）：逐条重算校验 + 可选 `seal=true` 固化当日
   Merkle 根（同日幂等）。断链不修链：返回 `200` + `ok=false` + `broken_seq` + `problems`。
5. 新增 schema：`HealthStatus`、`AuditEntry`、`AuditAppendRequest`、`AuditAppendResponse`、
   `AuditEntryPage`、`AuditVerifyRequest`、`AuditVerifyResponse`；新增 tag `ops`。
7. `Error.code` 文档补登 M03 网关码段（仅文档，不改结构 / 响应 / 枚举）：
   `42901/42902`（429）、`44101`（441）、`45001`（500）、`45201/45202/45203`（408/202/403）、
   `50901/50902/50903`（503/504/502）、`50904`（鉴权不可用，S1 过渡仍用 503+45001）、
   `40401`（404 专用码）、`42601`（426）；M10 `47010`（角色不足，403）、`47011`（审计不可用，503）。
   对应 CR 台账 M03 CR-6 / CR-8 / CR-10、M10 CR-13 / CR-23；M04 的 `42230–42235` 子码含义待
   M04 实现定稿，排 1.2.3。

6. 既有 1.2.0 / 1.2.1 的路径、操作、schema、enum、错误码**逐字节未变**。

**影响面**
- M16 platform-infra：审计链接线所需契约冻结（对应 M16 §4.1 的内部 ops 面；部署 / 备份运行端点留待 T5/T6 的 1.2.3）。
- M14 admin-data：Ops Console 读审计走 `GET /internal/ops/audit/entries`。
- M06 hashchain / M10 AuditSink：改为经 `POST /internal/ops/audit/entries` 统一入链（实现侧接线，不改契约）。
- 客户端 / 门户：无影响（内部端点不进 SDK，公网不可达）。

**兼容矩阵**
- v1 未发布 → N / N-1 / N-2 不适用；无存量客户端。
- 破坏性变更：**无**（`python3 ci/spec_breaking.py b4a4e7d` → ✅ 无破坏性变更，仅 1 条 202 提示为 1.2.1 遗留）。
- 与 1.2.1 的关系：纯增量；67 → 70 路径、76 → 80 操作、120 → 127 schema。

**复核人**
- 提议 / 实现：lead（contract-integrator 职责）
- 复核：待 release-manager 在发布前复核（S1 内部契约，不影响对外）

**备注（复核要点）**
- 内部端点沿用 1.1.0 已登记的 `internalMTLS` 安全域，不新增安全方案。
- `audit_log` / `audit_anchors` 的字段与 M16 §5 一一对应；`alg` 冻结为 `hmac-sha256`，
  与 `ops/audit` 现行链一致（sha256 旧链已归档）。
- 423 只在「校验进行中 / 密钥缺失」时返回，避免把锁冲突误报成 409。

## 1.2.1 · 2026-10-23 · MINOR

**类型**：MINOR（新增 1 个响应码 202 与 1 个 schema；无删除、无类型更改、无必填字段新增）

**变更**
1. `POST /api/v1/auth/login` 新增 `202` 响应（扫码登录**轮询中间态**）：content schema **直接**
   `$ref: ScanStatus`（顶层 `status` / `expires_at` / `retry_after_s`），客户端按 `retry_after_s`
   轮询同一条端点；`confirmed` 后下一次轮询返回 `200` 令牌；过期 / 已用 / 未知票据返回
   `409` + `40905`。`security: []` 保持公开端点。过渡期 M01-T2 额外携带 `scan` 同值包裹，
   `ScanStatus` 的 `additionalProperties: true` 允许其存在（纯增量，客户端读顶层字段）。
2. 新增 schema `ScanStatus`：`status`（`pending` / `created` / `scanned` / `confirmed` / `expired`）、
   `expires_at`（RFC 3339 UTC）、`retry_after_s`（integer ≥1）。票据一次性、中间态 TTL ≤120 s，
   只存 `sha256(ticket)`，明文只出现在二维码一次。
3. 1.2.0 的 `200` / `400` / `401` / `403` / `429` 响应与既有 schema / enum **未改动**。

**影响面**
- M01 身份域（T2 已实现 202 中间态）：契约与实现对齐；客户端（M13 门户 / M15 客户端）按 202 轮询。
- 其它模块：仅登录端点新增一个响应码，不影响已实现调用方（`200` 路径不变）。
- 管理面 / 账本 / 计费：无影响。

**兼容矩阵**
- v1 未发布 → N / N-1 / N-2 不适用；无存量客户端。
- 破坏性变更：**无**。新增响应码与新增 schema 属兼容项（`python3 ci/spec_breaking.py b4a4e7d` 通过，仅 1 条「新增响应码 202」提示）。
- 与 1.2.0 的关系：纯增量；`LoginResponse` / `LoginRequest` 等既有定义逐字节未变。

**备注（复核要点）**
- `ScanStatus.status` 中 `pending` 与 `created` **同义**：`created` 是 M01-T2 更早的内部实现值，
  对外已映射为 `pending`（`SCAN_STATUS_MAP`）；按 lead 裁决 S1 保留同义值、客户端两者都接受，
  S2 前收敛为单值（记 CR-5 后续项），本版本不改。
- `confirmed` / `expired` 保留在枚举内以覆盖同义实现；正常收尾分别是 `200` 与 `409 + 40905`。
- `202` 的 content 直接引用 `ScanStatus`（不做内联信封），与 M01 `test_contract_spec.py` 的
  `schema["$ref"] == "#/components/schemas/ScanStatus"` 严格对账一致。
- 错误码 `40103`（refresh 无效 / 已轮换或撤销（重放）/ 过期）与 `40905`（一次性令牌失效：扫码票据 / 邀请令牌）
  已在 `Error.code` **登记（仅文档）**：M01 T2 已在用，不改 schema 结构、不新增响应、不改枚举。

**复核人**：lead（制单：contract-integrator / DEV-W0-SPEC-1.2.1）
**裁决依据**：task-13 · M01-T2 实现（worktrees/M01-identity-t2）· `_CR-LOG.md §5/§6`

## 1.2.0 · 2026-10-23 · MINOR

**类型**：MINOR（新增 39 个端点 / 3 个响应码 / 65 个 schema / 1 个安全方案；无删除、无类型更改、无必填字段新增）

**变更**
1. **M01 身份与租户**（11 路径 / 13 操作）：`/api/v1/auth/login`、`/auth/refresh`（轮换）、`/auth/logout`、
   `/user/profile`（GET / PATCH）、`/user/api-keys`（GET / POST）、`/user/api-keys/{id}`（DELETE）、
   `/user/api-keys/{id}/rotate`、`/admin/accounts/{id}/freeze`、`/unfreeze`、`/roles`、
   `/admin/tenants/{id}/quotas`；错误码 `40110`（登录凭据错误，不区分账号不存在与口令错误）。
2. **M02 节点供给**（4 路径 / 6 操作）：`/api/v1/node/pair`（明文只回一次）、`/node/binding`（GET / POST / DELETE）、
   `/node/upstream/probe`（回执式：客户端本地探测后上报，服务端不探测）、
   `/admin/supply/nodes/{id}/review`；心跳请求新增**可选** `runtime_state`；错误码 `40305`（节点状态不允许该动作）。
3. **M03 网关**：新增 `402` 响应（`PaymentRequired`，码 `40201`）；HTTP `413` 专用码 `40013`、
   HTTP `409` 通用码 `40919`；批次响应对齐 `cost_estimate` / `results_ref`，
   `price_saved_pct` 作为**可选**字段（`BatchAccepted` 与 `Batch`），产品 §6.4.4 回改。
4. **M04 调度与定价**（5 路径）：`/api/v1/schedule/route`（内部端点，服务间 mTLS）、
   `/schedule/replay`（202 + `job_id`，并发 1）、`/schedule/decisions/{decision_id}`、
   `/schedule/quotes`、`/schedule/bids`。
5. **M05 计量与收据**（4 路径）：`/api/v1/user/usage/records`、`/node/receipts/{receipt_id}`、
   `/admin/usage/upstream-bills`、`/admin/usage/corrections`。
6. **M06 账本**（6 路径）：`/admin/ledger/trial-balance`、`/accounts`（42 科目与余额）、
   `/merkle/{period}`、`/recompute`（只读，不修数据）——以上只读；`/journals/{id}`（只读）、
   `/reversals`（账本唯一写路径，双人复核、整单等额）。错误码 `40920–40924`、`42220–42223`。
7. **M10 配置中心**（5 路径）：`/admin/config/namespaces`、`/config/{key}/diff`、`/impact`、`/audit`、
   `/config/publish-requests/{id}/promote`（灰度 1 → 5 → 25 → 100）。错误码 `47001–47009`；
   新增 `424` 响应（`StepUpRequired`，码 `42401`），过渡期实现可用 `403 + 47009`。
8. **组件**：新增响应 `PaymentRequired`(402) / `StepUpRequired`(424) / `UnprocessableEntity`(422)；
   新增安全方案 `internalMTLS`（仅内部端点 `/api/v1/schedule/route` 使用）。
9. **一致性**：`operationId` 全量唯一；所有 `$ref` 可解析；既有路径、必填字段、枚举值与状态码**未改动**；
   错误码仅登记在 `Error.code` 说明与 04 码段表（`00-conventions §5` 段位），不改变 `Error` 结构。

**影响面**
- 客户端：仅新增端点与可选字段；未知 enum 仍需按 tolerant reader 忽略。
  `/api/v1/auth/login`、`/auth/refresh` 为公开端点（`security: []`）。
- 云端 / 节点：M01 / M02 / M04 / M05 / M06 / M10 实现另开 PR（先契约后实现），实现 PR 引用本版本。
- 管理面：新增管理端点属内网/VPN + 强 MFA + step-up（≤5 分钟），公网不可达。
- 账本与计费：冲正、复算、校正均为双人复核 + 留痕；不改变既有分录语义与 `source_type` 白名单。

**兼容矩阵**
- v1 未发布 → N / N-1 / N-2 不适用；无存量客户端与存量 SDK。
- 破坏性变更：**无**。`ci/spec_breaking.py HEAD` 通过，输出仅「新增响应码」提示。
- 与 1.1.0 的关系：纯增量；1.1.0 的枚举（含 `LedgerEntry.status` 五值、钱包 / 签名事件）与字段语义不变。

**备注（复核要点）**
- M04 `quotes` / `bids` 与 M06 `journals/{id}` / `reversals` 属模块 §4.1 已声明、但不在 CR 逐条清单内的
  **additive 补登**（证据 AH-1），目的是让 `route-vs-spec` 差集为 0；复核时可单独回退而不影响其它条目。
- `40013`（413 专用码）与 `40919`（409 通用码）为本版本新增编号（CR-3 只规定段位、未给码值），
  口径见证据 AH-2；如需换号，改 `Error.code` 说明与响应描述即可，无结构影响。

**复核人**：lead（制单：contract-integrator / DEV-W0-SPEC-1.2.0）
**裁决依据**：`_CR-REGISTER.md` W1 段 · `_CR-LOG.md §5 / §6` · `W1-EXECUTION-PLAN.md` DEV-W0-SPEC-1.2.0

## 1.1.0 · 2026-10-19 · MINOR

**类型**：MINOR（新增端点；状态枚举按「未发布」口径直接修正）

**变更**
1. `LedgerEntry.status`：`pending/locked/settled/reversed` → `pending/confirmed/locked/settled/void`
   （补 `confirmed`；`reversed` 退役，冲正改用 `void` + `reverse_of`）。
   管理面 `GET /api/v1/admin/ledger/entries` 的 `status` 查询参数同步。
2. 新增配置中心 8 个操作：`listConfigItems` / `listConfigDrifts` / `getConfigItem` /
   `createConfigDraft` / `publishConfigItem` / `approveConfigPublish` /
   `rollbackConfigItem` / `killSwitchConfigItem`。
3. 新增 10 个 schema：`ConfigItem` / `ConfigItemPage` / `ConfigDraftRequest` /
   `ConfigPublishRequest` / `ConfigApprovalRequest` / `ConfigRollbackRequest` /
   `ConfigKillSwitchRequest` / `ConfigPublishAck` / `ConfigDrift` / `ConfigDriftList`；
   新增参数 `AdminStepUp`（`X-Admin-Step-Up`）。
4. 发布单状态机：`pending_approval → approved → scheduled → rolling_out → active`；
   终止 `rejected / expired / cancelled / rolled_back / failed`；`expired` 不自动放行。
5. `source_type` 16 值白名单（`chain_tx`/`payment_attempt`/`conversion_trade`/`usage_settle`/
   `lock_expiry`/`payout_accrual`/`payout_item`/`payout_batch`/`wallet_transfer`/`refund`/
   `chargeback`/`rebate`/`fx_reval`/`loss_pool`/`void`/`unidentified`），新增 `SourceType` schema
   与管理面 `source_type` 查询参数；版本化：新增值按 MINOR 先改 spec。
6. 通知偏好 / Webhook 订阅 / 报价设置 9 个操作（附录 C.3），含一次性 `secret` 与 P0 不可退订（`40902`）。
7. 报表中心 5 个操作（附录 C.5）：`recon-daily` / `exceptions` / `por` / `wallet-pool` / `monthly`，
   `period=YYYY-MM`，返回 `ReportEnvelope`。
8. 钱包与签名：`TreasuryIntent.kind` 增 `WALLET_PROVISION`（第八值）；`Wallet.status` 七值
   （`active/paused/draining/retired/provisioning/zero/frozen`）；`SigningEvent.event` 六事件
   （`requested/presented/signed/verified/co_signed/broadcast`）。
9. 错误码登记：`40910–40918`、`42210`、`42211`、`40201`（详见 Error schema 与附录 C.4）。

**影响面**
- 客户端：需容忍新增 enum 值（已有 tolerant reader 规则）；`reversed` 不再出现。
- 云端：实现侧另开 PR（同一变更先 spec 后实现）。
- 管理面：新增端点属内网/强 MFA/step-up，公网不可达。

**兼容矩阵**
- v1 未发布 → N/N-1/N-2 不适用；无存量客户端。
- 破坏性变更：仅「移除 `reversed`」一项，因未发布按 MINOR 处理并在此登记。

**复核人**：lead（制单）· 决策人（口径：RPT-0002 / v4 §20.4 §21.12）
