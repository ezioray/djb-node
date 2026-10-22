# 协议变更记录（CHANGELOG）

> 规则：同一 MAJOR 内只加不改；破坏性变更走 MAJOR 并提前 ≥90 天通告。
> 每条必须写全：版本 / 日期 / 类型 / 影响面 / 兼容矩阵 / 复核人。

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
