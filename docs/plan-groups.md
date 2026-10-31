# 多 plan 组：组内自动切换 + 组级共享预算（`plan.groups`）

> 状态：**已接线，默认关闭**。开关三字段登记在 `djb_node/facts.py`：owner `plan-groups`、
> expire_at `2027-06-30`、rollback_rule = 只关开关。本文记录口径、迁移与已知边界，供复核。

## 1. 数据模型（本地状态库 v3 → v4，纯加性）

| 表 | 关键列 | 说明 |
| --- | --- | --- |
| `plan_groups` | `id` / `name`(UNIQUE) / `priority` / `enabled` / `budget_usd_minor_day` / `budget_usd_minor_month` / `spent_usd_minor_day` / `spent_usd_minor_month` / `day_key` / `month_key` / `spent_tokens_day` / `spent_tokens_month` / `unknown_usage_count` / `allow_model_downgrade` / `model_fallbacks` | 组级共享预算；`budget_*` 为 NULL 表示不限 |
| `plan_group_members` | `group_id` / `upstream_id` / `priority` / `weight` / `enabled`，PK(`group_id`,`upstream_id`) | 一条上游 = 一个 plan |

- 金额一律 **USD minor 整数**（×100）。写入前 `plan_router.parse_usd_minor` 校验：拒绝负数、
  浮点、布尔、>2 位小数；不做 float 运算，不用 `Decimal` 以外的方式取整。
- 迁移只在 `PRAGMA user_version < 4` 时建新表 + 给 `plan_groups` 补 5 个带默认值的列；
  **不改老表、不回填**。已用 v3 形状库（删表 + `user_version=3`）验证：老数据逐字段不变，
  重复打开幂等（`tests/test_plan_groups.py::test_v3_db_migrates_to_v4_and_keeps_old_data`、
  `::test_migration_is_idempotent`）。版本只前进：v4 库被旧客户端打开会抛 `StoreDowngradeError`，
  所以**真实回滚动作 = 关开关**，不降版本、不删数据。

## 2. 选择与切换（`djb_node/plan_router.py`）

组按 `priority` 升序 → 组内成员按 `priority` 升序 → 同档按 `weight` 加权随机（生产路径注入
`random.randrange` 作为 picker；默认 picker 恒取第一个，只服务单测确定性）。

| 上游返回 / 情况 | 动作 | 可观测事件 |
| --- | --- | --- |
| 2xx | 用这个上游，记账 | `plan.chat` |
| 429 / 5xx（含 501/505/507） | 换下一个 plan | `plan.failover`（`reason=upstream_error`） |
| 401 / 403 | 换下一个 plan，该上游冷却 **300 s** | `plan.failover`（`reason=auth`） |
| 网络 / 超时 | 换下一个 plan | `plan.failover`（`reason=network`/`timeout`） |
| 400/404/413/422 等 | **不重试**，原样返回 | `plan.not_retryable` |
| 组预算耗尽 / 组关闭 / 模型不支持 | 跳过该组或该成员 | `plan.skip_upstream`、`plan.model_downgrade` |
| 无可用目标 | 503 `no_upstream` | `plan.no_target` |
| 开关开着但没有组 | 退回单上游 `route()` | `plan.no_groups`（warning） |

单次请求最多尝试 `plan_router.MAX_ATTEMPTS = 3` 个不同 plan；同一个 plan 不会被重复试
（`exclude` 累积）。少于 3 个 plan 时不会为了凑次数重打同一家。

## 3. 记账口径

- **有单价才算钱**：`upstreams.models[].min_price`（USD 字符串）+ `min_price_unit`
  （默认 `usd_per_1m_tokens`）→ `estimate_cost_minor()`。单位不认识、缺价、缺 usage 一律
  **不编造金额**：只记 `spent_tokens_day/month`；完全拿不到 usage 时 `unknown_usage_count += 1`。
- 取整：`Decimal` + `ROUND_HALF_UP` 到分。单次请求不足半分的按 0 记（保守，宁可少记不多记），
  真实消耗看 `spent_tokens_*`。
- 日 / 月窗口按 **UTC** 日期键（`day_key`=YYYY-MM-DD、`month_key`=YYYY-MM）。写入时在一条
  UPDATE 里用 CASE 完成跨天/跨月归零；读取侧 `effective_budget_left()` 遇到过期窗口返回
  “未花”，所以重启进程或隔天读库都不会把旧花费算成今天。
- 记账发生在**响应写给客户端之后**（同一请求内同步完成）。进程在这几毫秒内被杀会漏记一次，
  属于“宁可少记”的取舍；因此相关单测在断言账目前会等 handler 收工
  （`tests/test_plan_groups.py::_wait_idle`）。

## 4. CLI

```bash
djb plan group add --name main --day 1.00 --month 30.00 [--priority 100] [--allow-model-downgrade]
djb plan group add-upstream --group main --id u1 [--priority 5] [--weight 3]
djb plan group list [--json]
djb plan group remove-upstream --group main --id u1
djb plan group remove --group main
djb plan budget set --group main [--day 2.50] [--month 60.00] [--no-day|--no-month]
djb plan budget show [--group main] [--json]
djb flags --enable plan.groups     # 打开；重启 daemon 生效
```

开关关闭时 CLI 仍然读写配置，但输出会明确写“当前不生效”。

## 5. 已知边界（未做，别当成已支持）

1. **模型级预算 / `split_cost` 没有落库**：`plan_router` 支持按模型拆分成本与
   `allow_model_downgrade` 降级，但 v4 只存组级预算与 `model_fallbacks` 名单，按模型的
   `spent_by_model` 未建表；`split_cost()` 目前只有单测用到。
2. **冷却状态只在进程内**：`GroupState` 重启即清空，多进程/多实例之间不共享。
3. **没有跨进程写者仲裁**：daemon 与 CLI 同时改预算时靠 SQLite 事务串行，没有额外乐观锁。
4. **没有接 `djb status` / 诊断包**：`plan_router.stats_snapshot()` 只经 `LocalApiServer.plan_stats()`
   暴露，未进 `/v1/health`。
5. **日/月窗口用 UTC**，对 UTC+8 用户是本地早上 08:00 翻页；如需本地时区需另加配置项（未做）。
