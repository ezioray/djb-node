# 协议变更记录（CHANGELOG）

> 规则：同一 MAJOR 内只加不改；破坏性变更走 MAJOR 并提前 ≥90 天通告。
> 每条必须写全：版本 / 日期 / 类型 / 影响面 / 兼容矩阵 / 复核人。

## 1.2.13 · 2026-10-24 · MINOR

**类型**：MINOR（新增 5 条节点/管理面路径、6 个操作、7 个 schema）
**计数**：139 → 144 路径、159 → 165 操作、252 → 259 schema

**变更**
1. `GET /api/v1/node/notifications`：节点离线补拉（本节点、未过期、分类、keyset 游标）。
2. `POST .../{id}/ack`、`.../{id}/read`（read 隐含 ack，幂等）。
3. `GET .../unread_count`：未读角标。
4. 管理面 `POST|GET /api/v1/admin/notifications/node`：入队/列表（运营/排障）。
5. payload 白名单 + PII/正文/凭据键拒绝；P0 必带过期；180 天保留。
6. 开关 `notify.node_push.enabled` 默认关；`DJB_NODE_PUSH=1` S2 激活。

**影响面**
- C15/C16：服务端队列 + 客户端消息中心；C10 `notify.push` 帧后续复用 pull/ack。
- 不碰账本；不写正文。

**复核人**：lead（C15 实现 / 规格登记；证据 `DEV-C15-PUSH-20261024T200821Z.txt`）

## 1.2.15 · 2026-10-24 · MINOR

**类型**：MINOR（新增 4 条管理面路径、5 个操作、9 个 schema）
**计数**：149 → 153 路径、171 → 176 操作、272 → 281 schema

**变更**
1. M17 canary/L4：`POST|GET /api/v1/admin/supply/integrity/canary/runs`、`POST /runs/{id}/result`、
   `GET /stats`、`POST /expire`。
2. 5 条内置合成题面（exact/contains/regex）；响应**只存 sha256/score**，正文不落库。
3. 超期 120s → missed；重放/跨节点 409/404；同一 (node,model) 只 1 条 issued。
4. 连续 3 次 pass → L4 行为线索（**不直接改定级**，由 C12/风控裁决）。
5. 开关 `supply.canary.enabled` 默认关；`DJB_CANARY=1` S2 激活；不写账本。

**复核人**：lead（C13 实现 / 规格登记；证据 `DEV-C13-CANARY-20261024T203204Z.txt`）

## 1.2.14 · 2026-10-24 · MINOR

**类型**：MINOR（新增 5 条管理面路径、6 个操作、13 个 schema）
**计数**：144 → 149 路径、165 → 171 操作、259 → 272 schema

**变更**
1. M17 可验证随机抽检：`POST/GET /api/v1/admin/supply/integrity/sampling/epochs`、`GET /{id}`、
   `POST /{id}/reveal`、`POST /draws/{id}/result`、`POST /verify`。
2. commit-reveal：open 只存 `sha256(seed)`，seed 仅在 open 响应出现一次；reveal 校验承诺后
   HMAC 排序 + 贡献权重无放回抽签；同 seed 可复算。
3. 结果复用 C12 证据：L3→passed、L2/L1→failed、过期→missed；escalate 只给 I0/I1/I2 建议。
4. 开关 `supply.sampling.enabled` 默认关；`DJB_SAMPLING=1` S2 激活。
5. 不写账本；seed 不落库；抽检不下发正文。

**复核人**：lead（C14b 实现 / 规格登记；证据 `DEV-C14-SAMPLING-20261024T195903Z.txt`）

## 1.2.12 · 2026-10-24 · MINOR

**类型**：MINOR（新增 6 条节点/管理面路径、7 个操作、11 个 schema；不改既有路径语义）
**计数**：133 → 139 路径、152 → 159 操作、241 → 252 schema

**变更**
1. `GET /api/v1/node/model-catalog?since=`：拉取模型/tokenizer 签名 bundle；`since` 已最新 → `not_modified=true`。
2. `POST /api/v1/node/model-sync/state`：上报已应用版本/漂移/错误类。
3. 管理面：bundle 列表/创建/发布/回滚 + 节点同步状态（5 条）。
4. `payload_hash` = canonical JSON `{version, expires_at, models}` 的 sha256；`signature` = `hmac-sha256:` + HMAC(payload_hash)。
5. 只分发元数据/指纹（vendor/canonical/aliases/context/tokenizer family/version/hash/effective_at），**不分发厂商 tokenizer 权重**。
6. 开关 `model.sync.enabled` 默认关；`DJB_MODEL_SYNC=1` S2 激活；`model.sync.auto_publish` 默认关。

**影响面**
- M18/C17：服务端注册表 + bundle；客户端 M18b 已实现同口径校验。
- M03/M13：后续 `/v1/models` 增加 tokenizer 版本、门户展示（1.2.13）。
- M17：版本不一致时降为 unknown/不冻结（后续接线）。

**兼容矩阵**
- 只加不改；无破坏性变更。

**复核人**：lead（C17 实现 / 规格登记；证据 `DEV-M18-SERVER-20261024T194914Z.txt`）

## 1.2.11 · 2026-10-24 · MINOR

**类型**：MINOR（新增 3 条节点/管理面路径、3 个操作、8 个 schema；不改既有路径语义）
**计数**：130 → 133 路径、149 → 152 操作、233 → 241 schema

**变更**
1. 新增 `POST /api/v1/node/upstream/attest/challenge`：签发 L1–L3 合成探针（5 min TTL、一次性）；
2. 新增 `POST /api/v1/node/upstream/attest`：回传证据；**服务端自己用 tokenizer 计数比对**
   （容差 `max(1, ceil(expected*1%))`），match→L3，超容差→L2 + `tokenizer_mismatch`（不判作弊），
   无 usage→L2/L1，failed→L0；只升不降；挑战重放/跨 node/model 100% 拒绝。
3. 新增 `GET /api/v1/admin/supply/integrity/attestations`：证明列表（node/model/level）。
4. `ModelAttestation`：id/node_id/upstream_id/model/level/state/source/tokenizer_version/evidence_ref/verified_at/expires_at；
   TTL L1/L2 24h、L3 7d（L4 30d / L5 90d 预留给 C13）。
5. 开关 `supply.attestation.enabled` 默认关；S2 测试网 `DJB_ATTEST=1` 显式激活。

**影响面**
- M17 证明段：服务端 L3+ 可定级；M04/M02 可按 `is_attested` 门控（接线由 Lead 排期）。
- 客户端：后续 `djb attest` 接 challenge/report（当前仍本地线索）。
- M05/M06：无影响；证明不写账本。
- 旧客户端：不涉及公开 `/v1`。

**兼容矩阵**
- v1 未发布 → 不适用；无破坏性变更。
- 与 1.2.10 的关系：纯新增。

**复核人**：lead（C12 实现 / 规格登记；证据 `DEV-C12-ATTEST-20261024T191751Z.txt`）

## 1.2.10 · 2026-10-24 · MINOR

**类型**：MINOR（新增 3 条管理面路径 / 4 个操作 / 8 个 schema；不改既有路径语义）
**计数**：127 → 130 路径、145 → 149 操作、225 → 233 schema

**变更**
1. 新增 `GET|POST /api/v1/admin/supply/integrity/holds`、`GET /holds/{id}`、
   `POST /holds/{id}/release`（M17 C14 待分润冻结）。
2. `IntegrityLevel` = I0–I4；冻结比例固定 **I0=0 / I1=50% / I2=80% / I3=I4=100%**（决策人 2026-10-24）。
3. 冻结只作用于「待分润」：**不写账本、不改余额**；`frozen_amount_micro = base_amount_micro * ratio_bp // 10000`。
4. 解除：I0/I1 一人；I2 需 2 名不同 reviewer；I3/I4 另需 `integrity:release:high`；
   幂等重放回同一结果。
5. `supply.freeze.enabled` 关闭时写接口 `503 + 50001 (feature_disabled)`；读接口只读可用。

**影响面**
- M14 后台：新增冻结列表/详情/创建/解除页面数据源。
- M17/M11：冻结与案件联动；**不改变账本/结算金额**。
- M04/M08：可读 `is_frozen` / `frozen_total`（接线由 Lead 排期）。
- 旧客户端：不涉及公开 `/v1`；tolerant reader 无影响。

**兼容矩阵**
- v1 未发布 → N/N-1/N-2 不适用；无破坏性变更。
- 与 1.2.9 的关系：纯新增（管理面）。

**复核人**：lead（C14 实现 / 规格登记；证据 `DEV-C14-FREEZE-20261024T120857Z.txt`）

## 1.2.9 · 2026-10-24 · MINOR

**类型**：MINOR（新增 2 条节点面路径 + 5 个 schema + **首次使用 `204`**；无删除、无类型更改、
无既有 path / 字段语义变更；路径 125 → 127、操作 143 → 145、schema 220 → 225）

**变更**
1. `POST /api/v1/node/channel/poll`（`pollNodeChannel`）：节点出站**长轮询**取一个任务。
   请求 `wait_s`（number，1–30，默认 25，可选）与 `capabilities`（string[]，可选，未知值忽略）；
   命中返回 `200` + `{job, server_time}`；`wait_s` 内无任务返回 **`204`（无响应体）**。
   单节点同时 1 个在途任务由通道层保证（不写进本文件阈值）。
2. `POST /api/v1/node/channel/result`（`submitNodeChannelResult`）：回传结果。
   请求 `job_id`(uuid) / `status`(completed|failed) / `content` / `finish_reason` /
   `tokens_in` / `tokens_out` / `usage_source`(actual|estimated|dual，可空) / `latency_ms` /
   `first_byte_ms` / `error_code`（字符串，可空）/ `error_class` / `upstream_request_id`；
   返回 `200` + `{accepted, duplicate}`。**不接受** `succeeded` / `node_id` / `duration_ms` /
   `completed_at` / `usage` 嵌套（未知字段一律忽略）。
   **按 `job_id` 幂等**：同载荷重放 `duplicate=true`（只受理一次）；同 `job_id` 不同载荷 → `409` + `40919`；
   `job_id` 不存在 / 不属于该节点 → `404` + `40401`。`status=failed` 必须给 `error_code` 或 `error_class`。
3. 新增 5 个 schema：`NodeChannelPollRequest`、`NodeChannelJob`、`NodeChannelPollResponse`、
   `NodeChannelResultRequest`、`NodeChannelResultAck`。`NodeChannelJob` 字段：
   `job_id` / `kind:"chat.completions"` / `request_id` / **`request`**（对象：`model`、`messages`、
   `max_tokens`、`temperature`、`stop`）/ `timeout_s`（可选）/ `lease_expires_at`——
   **`model` / `messages` 不在 job 顶层**（与客户端 `parse_job` 对齐）。
   job **不含任何凭据 / 上游端点**（H1 口径不变；`request` 为正文，只在内存与响应体过一手）。
4. 错误码**只复用不新造**：`50903`（通道不可用 / 未接线 → 503）、`40401`（任务不存在或越权）、
   `40919`（同 `job_id` 不同载荷）、`42901`（通道限流）、`42601`（协议不兼容，与其余节点端点同口径）。
5. 既有 1.2.0–1.2.8 的路径、操作、schema、enum、错误码**语义未变**；旧客户端忽略新路径即可。

**影响面**
- M02 node-supply（C10a）：两条端点 + `app/supply/channel.py` 接线位；队列未接线 → `503` / `50903`（fail-closed），
  不 import M03、不建表、正文不落库。
- M03 gateway-protocol（C10b）：提供队列后端（内存队列 / 租约 / 重派），经 `channel.set_channel_backend` 注入。
- M15 / 客户端（C10c）：长轮询循环；客户端零第三方运行时依赖（stdlib `http.client`）。
- M04 / M05 / M16：无影响（通道不新增事件，也不改收据 / 审计字段）。

**兼容矩阵**
- v1 未发布 → N / N-1 / N-2 不适用；无存量客户端。
- 破坏性变更：**无**（`python3 ci/spec_breaking.py HEAD` → ✅ 无破坏性变更，无兼容提示）。
- 与 1.2.8 的关系：纯新增；125 → 127 路径、143 → 145 操作、220 → 225 schema。
- `204` 是**新操作**上的新响应码，不改变任何既有操作的响应集合。

**复核人**：lead（本席实现 C10a；证据 `ops/audit/reports/DEV-C10-CONTRACT-<UTC>.txt`）

## 1.2.8 · 2026-10-24 · MINOR

**类型**：MINOR（`Model` 新增 2 个可选响应字段；无删除、无类型更改、无既有 path 语义变更；
路径 / 操作 / schema 数不变：125 / 143 / 220）

**变更**
1. `Model.available`（`boolean`，可选）：当前是否可服务。服务端按「有正容量公告 + 上游
   `state=verified` 且 `verified_at` 在新鲜窗口（默认 900 s）内」判定；**缺省表示未计算/
   降级镜像，客户端不得推断为可用**。
2. `Model.probe`（`object`，可选）：可用性依据，`additionalProperties=false`；字段：
   `level`（string|null）、`verified_at`（date-time|null）、`fresh`（boolean）、
   `reason`（`no_capacity` / `unverified` / `stale` / null）。
3. 既有 1.2.0–1.2.7 的路径、操作、schema、enum、错误码**语义未变**；旧客户端忽略新字段即可。

**影响面**
- M03 gateway：`/v1/models` 透传可选字段（`providers._MODEL_FIELDS` / `router._MODEL_FIELDS`）。
- M13 portal-web：C9c 已按三态消费；1.2.8 后从「状态未知」升级为真实状态。
- M02/M04：只读 `v_supply_capacity_latest` 与 `v_supply_upstream_verified`，不新增写入。
- 客户端 / SDK：tolerant reader；不传该字段的服务端仍兼容。

**兼容矩阵**
- v1 未发布 → N/N-1/N-2 不适用。
- 破坏性变更：**无**（纯字段级新增；`ci/spec_breaking.py` 复核）。
- 与 1.2.7 的关系：纯补丁；125/143/220 不变。

**复核人**：lead（本席实现 C9 收尾；证据 `ops/audit/reports/DEV-C9-CLOSE-<UTC>.txt`）

## 1.2.7 · 2026-10-24 · MINOR

**类型**：MINOR（1 个只读可选响应字段 + 1 个请求字段由必填改为可选 + `Error.reason` 新增 1 个枚举值；
无删除、无类型更改、无既有 path 语义变更；路径 / 操作 / schema 数不变）

**变更**
1. `PaymentOrderDetail` 新增 `original_route_masked`（`string|null`，`readOnly: true`）：
   形态 `psp:<provider>:****<末4>`，无成功 `payment_attempt` 时为 `null`；
   **只用于展示，永不作为入参**；真实原路不入响应（退款只回 `original_route_hash`）。
2. `RefundRequestCreateRequest.original_route` 由**必填改为可选**：省略时服务端按 `order_no` 对应的
   **唯一**成功 `payment_attempt` 推导真实原路；提供时必须**逐字等于**真实原路，掩码串
   （`psp:<provider>:****<末4>`）不接受。
3. 无法唯一确定原路（无成功 attempt 或多次成功）→ `409` + `40919`（`reason=original_route_unavailable`）；
   **不新造错误码**（40919 沿用 1.2.5 登记的「状态冲突通用码」）。
4. `Error.reason` 枚举新增 `original_route_unavailable`（新增 enum 值，客户端须容忍未知值）。
5. 既有 1.2.0–1.2.6 的路径、操作、schema、enum、错误码**语义未变**。

**影响面**
- M08 payments-treasury：退款创建允许服务端推导原路；订单详情新增只读掩码原路，供 M13 退款页展示。
- M13 portal-web：退款页展示 `psp:<provider>:****<末4>`；提交时可省略 `original_route`，不再要求用户填明文。
- 客户端 / SDK：响应新增可选只读字段 + `Error.reason` 新增枚举值（tolerant reader 必须忽略未知值）；
  请求侧放宽（字段由必填改可选）对老客户端无影响。
- M09 / M16：无影响（不涉及对账 / 审计字段）。

**兼容矩阵**
- v1 未发布 → N / N-1 / N-2 不适用；无存量客户端。
- 破坏性变更：**无**（`python3 ci/spec_breaking.py 91991f5` → ✅ 无破坏性变更；仅 1 条「不再强制 original_route」兼容提示）。
- 与 1.2.6 的关系：纯补丁；125 → 125 路径、143 → 143 操作、220 → 220 schema（仅字段级增改）。

**复核人**：contract-integrator（提议 / 实现，task-10 / DEV-SPEC-1.2.7）· lead（复核；证据 `ops/audit/reports/DEV-W0-SPEC-1.2.7-<UTC>.txt`）

**备注（复核要点）**
- `original_route_masked` 的 `<末4>` 取自真实原路标识末尾 4 位；前缀统一为 `psp:<provider>:`，
  与 `PaymentAttempt.provider` 对齐；**不得**把掩码回传为退款入参（服务端按字面校验拒绝）。
- 原路推导只认「唯一成功 attempt」：0 个或 ≥2 个成功都返回 409 + 40919 + `original_route_unavailable`，
  不做启发式猜测；同一笔退款仍受唯一 `refund_request` 约束。
- 40919 语义为「状态冲突通用码」，本补丁复用而不新增码；若要独立子码，走 1.2.8 MINOR（不本版）。
- 掩码原路属于展示字段，`readOnly: true` 明确不可写；SDK 生成时不应把它放进请求模型。

## 1.2.6 · 2026-10-24 · MINOR

**类型**：MINOR（新增 3 条路径 / 3 个操作 / 3 个 schema；tag 归一 + 1 个可选字段 + 取消语义与错误码补登；
无删除、无类型更改、无既有 path / schema 语义变更）

**变更**
1. **tag 归一（元数据）**：`/api/v1/admin/**` → `tag=admin`；`/api/v1/pay/**` → `tag=pay`（用方支付面）；
   `/api/v1/provider/**` → **新增 `tag=provider`**（共享方控制台面，不并入 `pay`）。
   1.2.5 引入的 `Treasury` / `AdminData` / `AdminOps` 三个 tag 全部下线（tag 级出现 0 次），
   全局 tags 保留 `admin` / `pay` / `provider` 等小写 tag。既有 `/api/v1/provider/quote-settings`
   （GET / PUT）由 `node` 改为 `provider`，属**仅元数据**变更（鉴权与语义不变，M04 报价设置仍属该端点）。
2. **M14 枚举与取消语义**：批量操作 `step` 冻结为 `preview|execute|undo`（增改走 MINOR + 双写）；
   导出取消统一表达为 `state → failed` + `failure_reason=cancelled_by_requester`，
   **不新增** `cancelled` 状态值（`ExportRequest.state` 六值不变），重复取消幂等。
3. **M02 用户态端点（M13 CR-1 阻塞）**：
   `GET /api/v1/user/nodes`（`listUserNodes`）：返回 node_id / state（绑定视图 unbound·pending_receipt·bound·revoked）/
   lifecycle_state（M02 权威 pending·active·frozen·retired）/ last_seen / applied_version / pending_reason；
   `POST /api/v1/user/nodes/{node_id}/unbind`（`unbindUserNode`）：幂等解绑（重复调用返回同一结果，
   撤销客户端密钥、立即停单、保留历史收据与锁定收益），需 step-up（424 + `42401`）。
   新增 schema `UserNode` / `UserNodePage` / `UserNodeUnbindRequest`；鉴权 `userBearer`。
4. **`/v1/models` 增补**：`Model.price_version` 为**可选**字段（价格快照版本，M04 model_catalog / M10 同族）；
   鉴权**不变**（保持 `userBearer`）：公开侧（未登录文档站 / 降级镜像）可缺省该字段，客户端必须容忍缺失。
5. **M05 账单导出**：`GET /api/v1/user/billing/export`（`exportUserBilling`，归属 M05，`userBearer`）：
   流式 `text/csv` / `application/json`，`Content-Disposition: attachment`，有效期由
   `X-DJB-Export-Expires-At` 表达（TTL ≤15 分钟，过期重新申请）；不含正文 / 凭据 / PII 正文。
6. `Error.code` 文档补登（仅文档）：`40101` 通用未认证码（HTTP 401；未认证 / 凭据无效或过期 /
   鉴权未接线 fail-closed），M01 会话、M02 节点密钥、M08 / M14 未认证路径统一复用。
7. 既有 1.2.0–1.2.5 的路径、操作、schema、enum、错误码**语义未变**；仅 tag 元数据归一（见 1）。

**影响面**
- M02 node-supply：新增用户态节点列表 / 解绑两个端点（所有权仍属 M02；M13 只读消费）。
- M05 metering-receipt：新增账单导出端点（M05 §4.1 与 01-ownership 需回填 `/api/v1/user/billing/*`）。
- M13 portal-web：控制台节点绑定视图与账单导出接入；tag 分组变化（admin / pay / provider / user）需同步 SDK 生成。
- M08 payments-treasury：端点语义不变，仅 tag 由 `Treasury` 归一为 `admin` / `pay` / `provider`。
- M15 client-runtime：本地镜像 `/v1/models` 已回传 `price_version`；公开侧缺失时按降级展示。
- 客户端 / SDK：`/v1/models` 新增可选字段；未知字段 / 未知 enum 仍按 tolerant reader 忽略。

**兼容矩阵**
- v1 未发布 → N / N-1 / N-2 不适用；无存量客户端。
- 破坏性变更：**无**（`python3 ci/spec_breaking.py 6b135ae` → ✅ 无破坏性变更；tag 变更不计破坏性）。
- 与 1.2.5 的关系：纯增量；122 → 125 路径、140 → 143 操作、217 → 220 schema。

**复核人**：contract-integrator（提议 / 实现，task-6 / DEV-SPEC-1.2.6）· lead（复核；未定项见证据 `ops/audit/reports/DEV-W0-SPEC-1.2.6-<UTC>.txt`）

**备注（复核要点）**
- tag 归一：既有 `/api/v1/admin/**` 原本已是 `admin`，未改动；本次只改 1.2.5 新增面与
  `/api/v1/provider/quote-settings` 一处既有 tag（node → provider）。若下游按 tag 生成 SDK 分组，需重建客户端。
- **裁决 A（用户节点 state，写入契约不改结构）**：`state` 固定为用户面四值
  `unbound|pending_receipt|bound|revoked`；响应同时带 `lifecycle_state`（M02 权威
  `pending|active|frozen|retired`）。映射：无绑定→`unbound`、`pending`→`pending_receipt`、
  `active`→`bound`、`frozen`→`bound`（另带 restricted 提示，读 `lifecycle_state=frozen`）、
  `retired`→`revoked`。M02 内部 `NodeBinding.state` 不改名，只做视图映射。
- **裁决 B（版本命名）**：`applied_version` 保持契约名（M10 running 口径）；
  M13 规格里的 `last_applied_version` 由 BFF 做字段映射，**不改契约**。
- **裁决 C（账单导出口径）**：接受 TTL 头（`X-DJB-Export-Expires-At`）表达有效期；
  若 M05 以后改为离线打包 + 一次性 token，**只加 optional**，不破坏 1.2.6。
- **裁决 D（回填责任）**：模块规格（M02 §4.1 / M05 §4.1）与 01-ownership 的回填由 Lead 处理，本席不改 modules。
- 新增端点尚未有实现路由：M02 / M05 需按本契约补 routes；M13 接入前只显示桩（M13 CR-2 口径）。

## 1.2.5 · 2026-10-24 · MINOR

**类型**：MINOR（新增 25 条路径 / 31 个操作 / 46 个 schema / 3 个 tag；无删除、无类型更改、无既有字段语义变更）

**变更**
1. M08 支付与资金补齐 21 个操作 / 17 条路径（M08 CR-P3 / CR-P4，M14 CR-2）：
   - 收款面（用方身份）：`POST /api/v1/pay/orders`（`createPaymentOrder`，201）、
     `GET /api/v1/pay/orders/{id}`（`getPaymentOrder`）、
     `POST /api/v1/pay/webhooks/{provider}`（`receivePaymentWebhook`，`security: []` 独立验签端点，不进用户鉴权）、
     `POST /api/v1/pay/refund-requests`（`createRefundRequest`，201）。
   - 共享方面：`GET /api/v1/provider/payouts`（`listProviderPayouts`）、
     `POST /api/v1/provider/payout-address`（`updateProviderPayoutAddress`）。
   - 钱包池面（管理面，step-up + 幂等键）：`GET|POST /api/v1/admin/treasury/pools`（`listTreasuryPools` / `createTreasuryPool`）、
     `POST /api/v1/admin/treasury/provision`（`provisionTreasuryWallets`，201）、
     `POST /api/v1/admin/treasury/activate/{batch}`（`activateTreasuryBatch`）、
     `POST /api/v1/admin/treasury/switch`（`switchTreasuryWallet`）、
     `POST /api/v1/admin/treasury/retire`（`retireTreasuryWallet`）。
   - 出款面：`POST /api/v1/admin/treasury/payouts`（`createTreasuryPayoutBatch`，201）、
     `POST /api/v1/admin/treasury/payouts/{id}/approve`（`approveTreasuryPayoutBatch`）、
     `POST /api/v1/admin/treasury/payouts/{id}/sign-request`（`requestTreasuryPayoutSignature`，201）。
   - 地址与 gas：`GET|POST /api/v1/admin/treasury/addresses`、`GET|POST /api/v1/admin/treasury/deposit-address`、
     `GET|POST /api/v1/admin/treasury/gas-policy`。
   新增 schema 31 个：PaymentOrder / PaymentOrderCreateRequest / PaymentAttempt / PaymentOrderDetail /
   PaymentWebhookAck / RefundRequest / RefundRequestCreateRequest / PayoutItem / PayoutItemInput /
   PayoutBatch / PayoutBatchCreateRequest / PayoutBatchPage / PayoutApproveRequest / SigningRequest /
   TreasuryWallet / TreasuryPool / TreasuryPoolPage / TreasuryPoolCreateRequest / TreasuryProvisionRequest /
   TreasuryProvisionBatch / TreasuryActivateRequest / TreasurySwitchRequest / TreasurySwitchAck /
   TreasuryRetireRequest / TreasuryAddress / TreasuryAddressRequest / TreasuryAddressPage / DepositAddress /
   DepositAddressRequest / GasPolicy / GasPolicyRequest。
   字段名与 M08 席位 T1 模型（`worktrees/M08-treasury/app/treasury/models.py` 的 payload / parser）对齐。
2. M14 数据治理与 Ops 补齐 10 个操作 / 8 条路径（M14 CR-2）：
   `GET|POST /api/v1/admin/data/export-requests`（`listAdminDataExportRequests` / `createAdminDataExportRequest`，201）、
   `GET /api/v1/admin/data/export-requests/{export_id}`（`getAdminDataExportRequest`）、
   `POST /api/v1/admin/data/export-requests/{export_id}/cancel`（`cancelAdminDataExportRequest`）、
   `POST|GET /api/v1/admin/data/retention-jobs`（`createRetentionJob`，201 / `listRetentionJobs`）、
   `GET /api/v1/admin/ops/health`（`getOpsHealth`）、`GET /api/v1/admin/ops/changes`（`listOpsChanges`）、
   `POST /api/v1/admin/ops/batch-ops`（`executeOpsBatch`）、`GET /api/v1/admin/ops/audit-replay`（`replayOpsAudit`）。
   新增 schema 15 个：ExportRequest / ExportRequestCreateRequest / ExportRequestPage / ExportDownload /
   RetentionJob / RetentionJobCreateRequest / RetentionJobPage / OpsHealth / OpsHealthBlock / OpsChange /
   OpsChangePage / OpsBatchOpRequest / OpsBatchOp / AuditReplay / AuditReplayEvent。
   字段名与 M14 席位 T1 模型 / router（`worktrees/M14-admin/app/data_gov`）对齐；`operationId` 取实现已注册值。
3. 新增 tag `Treasury` / `AdminData` / `AdminOps`（按 Lead 指令逐字写入；既有 tag 保持小写不变）。
4. `Error.code` 文档补登（仅文档，不改结构 / 响应 / 枚举）：`40310–40312`、`40001`、`40901`、`40919`、`49001`；
   并逐码钉死 `40910–40918` 的资金语义（重复回调 / 金额不符 / 确认数不足 / 链·资产不支持 / 扩容批次过期 /
   地址冲突 / 缺 attestation / 退款处理中 / 退款失败），`42210` / `42211` 出款准入与风控拒绝。
5. 敏感红线：schema 不含卡号、私钥 / 助记词、keystore、完整签名；退款只回 `original_route_hash`（不回原文），
   签名只以 `signing_request_id` / `auth_hash_prefix` / `signature_verified` 结论出现；
   webhook 请求体为通道原始事件，不落正文。
6. 既有 1.2.0–1.2.4 的路径、操作、schema、enum、错误码**逐字节未变**。

**影响面**
- M08 payments-treasury：S1（无真钱）接口面冻结；真钱动作仍受 S1–S2 置灰约束（K1；S3 才开真钱）。
- M14 admin-data：导出 / 保留 / Ops 只读面冻结；写动作一律调 Owner API，M14 不写业务表。
- M13 portal-web：支付页接 `POST /pay/orders` + 轮询 `GET /pay/orders/{id}`；共享方 payout 页接 `/provider/payouts`，
  导出详情页用 `download`（一次性链接，≤15 分钟）。
- M09 / M16：出款闸门读取 `attestation_ref`（M09）；健康总览 / 变更日历只读复用 M16 审计链与 `deploy_runs` 口径。
- 客户端 / SDK：仅新增端点与可选字段；未知 enum 仍按 tolerant reader 忽略。

**兼容矩阵**
- v1 未发布 → N / N-1 / N-2 不适用；无存量客户端。
- 破坏性变更：**无**（`python3 ci/spec_breaking.py f7e6b54` → ✅ 无破坏性变更）。
- 与 1.2.4 的关系：纯增量；97 → 122 路径、109 → 140 操作、171 → 217 schema。

**复核人**：contract-integrator（提议 / 实现，task-5 / DEV-SPEC-1.2.5）· lead（复核；未定字段清单见证据 `ops/audit/reports/DEV-W0-SPEC-1.2.5-<UTC>.txt`）

**备注（复核要点）**
- tag 大小写：既有 `admin` / `user` / `node` 等保持小写；新增 `Treasury` / `AdminData` / `AdminOps` 按 Lead
  指令逐字写入，消费方按精确字符串过滤（大小写敏感）。
- 未定字段（裁决后按「只加不改」补）：`FundClass` 取值（CR-P5）；地址白名单 / 平台收款地址的 `status` 枚举
  （实现为 pending / whitelisted / rejected / revoked，本 spec 已在地址面冻结、平台收款地址面留 string）；
  `TreasuryRetireRequest.wallet_id` 的位置（本 spec 放 body；M08 现 `parse_retire` 只收 `step` / `attestation_ref`）；
  `createTreasuryPool` 的 body 字段（无实现，取自 facts §2）；收银台跳转 URL 字段名（M13 接入时补 optional）。
- M08 §7 与实现的口径差异（CR-P7）：回调验签失败本 spec 采用 `400 + 40001`（实现口径），
  重复回调 `409 + 40910`、金额不符 `409 + 40911`；§7 原文把验签失败也归 40910，待 Lead 回改正文。
- gas 模式两套写法：03-facts §9 `billing.gas_fee_mode`（INTERNAL / EXTERNAL + bearer）与 M08 §8.1
  三值（INTERNAL / EXTERNAL_PLATFORM / EXTERNAL_PAYER）；本 spec 采用三值 + 可选 `gas_bearer`，
  归一逻辑在实现（`normalize_gas_mode`），待 Lead 统一正文与配置键命名。
- 路径口径：M08 §4.1 用 `/api/v1/admin/treasury/*`；产品 §19.10.2 早期建议 `/api/v1/treasury/wallets/*` 与
  `/treasury/wallets/activate/{batch_id}/activate`。本版按 M08 §4.1 + 任务卡路径冻结，旧建议路径**不实现**。
- 状态机口径：导出 `pending → approved → running → done`，终止 `failed / expired`；删除类操作只出报告不删账本；
  批量五步的状态集（`previewed / executed / undone / failed`）按实现冻结；`activate` 只收 `signing_request_id`，
  不接受签名字节；`sign-request` 只 propose，签名仍由决策人链下完成。
- 事件契约本版未动（M08 的 7 个生产事件与 M14 的 2 个生产事件均已在 02-events 登记）。

## 1.2.4 · 2026-10-23 · MINOR

**类型**：MINOR（新增 17 条路径 / 19 个操作 / 30 个 schema；无删除、无类型更改、无既有字段语义变更）

**变更**
1. M07 信誉与 COP 补齐 10 条（CR-2 / CR-4）：
   用户面 `GET /user/reputation`、`GET /user/reputation/events`、`GET /user/cop`、`GET /user/cop/ledger`、
   `POST /user/cop/redemptions`、`GET /user/cop/redemptions/{id}`、`POST /user/cop/transfers`、
   `GET /user/cop/invites`；管理面 `GET /admin/reputation/scores`、`POST /admin/reputation/grants`。
2. M11 风控补齐 9 个操作（CR-1 / CR-4）：
   `GET|POST /admin/risk/rules`、`GET|POST /admin/risk/limits`、`GET /admin/risk/cases`、
   `GET /admin/risk/cases/{id}`、`POST /admin/risk/cases/{id}/decisions`、
   `POST /admin/risk/appeals`、`POST /admin/risk/appeals/{id}/decide`。
   阈值 / 权重 / 名单**不入库、不入响应、不入事件**，只出现 `config_ref` 与 `rule_version`（M11 INV-01）。
3. `Error.code` 文档补登：`42240`（M07 兑换/转让校验失败）、风控段 `48001–48006`。
4. 既有 1.2.0–1.2.3 的路径、操作、schema、enum、错误码**逐字节未变**。

**影响面**
- M07 reputation-cop：S1 基础面（评分 / COP 台账 / 邀请）冻结；转让默认关（K4）。
- M11 risk-abuse：管理面 9 条冻结；限额执行仍由 M03/M04/M08 读 `v_risk_limits` 本地判定，本模块不进转发路径。
- M14：Ops Console 只读复用两模块列表接口。
- M08：出款前风控可经信封扩展字段对接（不新增端点）。

**兼容矩阵**
- v1 未发布 → N / N-1 / N-2 不适用；无存量客户端。
- 破坏性变更：**无**（`python3 ci/spec_breaking.py b4a4e7d`）。
- 与 1.2.3 的关系：纯增量；80 → 97 路径、90 → 109 操作、141 → 171 schema。

**复核人**：lead（contract-integrator 职责，提议 / 实现）· release-manager（发布前复核，内部/管理面）

**备注（复核要点）**
- `40201`（余额不足）与 `40902`（开关/状态冲突）沿用既有登记；M07 的校验失败统一 `42240`（**不占用** M06 的 `42220–42223`）。
- `subject_hash` 只接受加盐哈希 / 簇 ID；不接受明文账号 / IP / 设备号（M11 INV-05）。
- 风控处置只产生提案（`proposal_id`），账户状态仍由 M01 执行（M11 INV-04）。

## 1.2.3 · 2026-10-23 · MINOR

**类型**：MINOR（新增 10 条路径 / 10 个操作 / 14 个 schema；无删除、无类型更改、无既有字段语义变更）

**变更**
1. M12 站内信补齐 2 条（CR-2）：
   `GET /api/v1/user/notifications`（`listNotifications`，游标分页、level / unread_only 过滤、返回 unread 计数）；
   `POST /api/v1/user/notifications/{id}/read`（`markNotificationRead`，`id=all` 为保留值表示全部已读，重复无副作用）。
   新增 schema `Notification` / `NotificationList` / `NotificationReadAck`。
2. M09 对账补齐 8 条（CR-1 / CR-2）：
   `GET /api/v1/admin/treasury/recon/runs`、`GET .../runs/{run_id}`（含差异汇总）、
   `POST .../runs/{run_id}/sign-off`（双人签核，未结 P0/P1 拒绝）、
   `GET .../exceptions`、`POST .../exceptions/{id}/resolve`、
   `POST .../attestations`、`GET .../attestations/{id}`、`GET .../attestations/{id}/validate`（内部面，出款前校验）。
   新增 schema `ReconRun` / `ReconRunPage` / `ReconDiffSummary` / `ReconRunDetail` / `ReconException` /
   `ReconExceptionPage` / `ReconSignOffRequest` / `ReconExceptionResolveRequest` / `ReconAttestation` /
   `ReconAttestationRequest` / `ReconAttestationValidation`。
3. `Error.code` 文档补登：`40902`（M12 P0 不可退订）、对账段 `49201–49206` / `49210–49212`（M09 CR-3）。
4. `ReportPeriod` 支持日粒度 `YYYY-MM-DD`（M09 CR-1，只加不改；月粒度行为不变）。
5. 既有 1.2.0 / 1.2.1 / 1.2.2 的路径、操作、schema、enum、错误码**逐字节未变**。

**影响面**
- M12 notification：S1 站内信 + 偏好 + Webhook 三条读/写端点齐套；S2 邮件/重试/死信按实现接。
- M09 recon-reports：批次 / 差异 / 凭证三个聚合的读写面冻结；报表 5 条沿用 1.1.0。
- M08：出款前校验走 `validate` 内部面（无需 admin 会话）。
- M14：Ops Console 只读调用 M09 的 runs / exceptions。

**兼容矩阵**
- v1 未发布 → N / N-1 / N-2 不适用；无存量客户端。
- 破坏性变更：**无**（`python3 ci/spec_breaking.py b4a4e7d` → ✅ 无破坏性变更）。
- 与 1.2.2 的关系：纯增量；70 → 80 路径、80 → 90 操作、127 → 141 schema。

**复核人**：lead（contract-integrator 职责，提议 / 实现）· release-manager（发布前复核，内部/管理面契约）

**备注（复核要点）**
- `id=all` 是路径保留值，不对应真实通知 id；客户端传 `all` 时 `notification_id` 返回 null。
- 对账金额一律有符号整数最小单位；`diff_minor` 不取绝对值，符号即方向。
- `validate` 只返回 `valid/status/reasons`，不返回凭证正文、签名或金额明细（避免出款侧拿到不必要信息）。

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

**复核人**：lead（contract-integrator 职责，提议 / 实现）· release-manager（待发布前复核；S1 内部契约，不影响对外）

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
