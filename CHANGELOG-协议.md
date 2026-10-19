# 协议变更记录（CHANGELOG）

> 规则：同一 MAJOR 内只加不改；破坏性变更走 MAJOR 并提前 ≥90 天通告。
> 每条必须写全：版本 / 日期 / 类型 / 影响面 / 兼容矩阵 / 复核人。

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
