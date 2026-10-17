![ci](https://github.com/ezioray/djb-node/actions/workflows/ci.yml/badge.svg)

# DJB Protocol · spec

> **`openapi.yaml` 是本协议的唯一契约。** 客户端、云端、SDK、文档站都从它生成或校验，
> 任何一方都不得绕过它自行定义接口。

| 消费者 | 用法 | 违反的典型症状 |
| --- | --- | --- |
| 客户端 SDK | 由 spec 生成 TypeScript / Rust 类型与方法 | 手写类型、字段名与 spec 不一致 |
| 云端服务 | 由 spec 生成或校验路由与入参出参 | 路由存在但 spec 没有；响应字段 spec 没有 |
| 契约测试 | 用 spec 驱动冒烟与 schema 校验 | 断言的是实现细节而非契约 |
| 文档站 | 由 spec 渲染 API 参考 | 文档里出现 spec 没有的端点或字段 |

## 兼容规则

1. **只加不改**：同一 MAJOR 内不删字段、不改类型/语义/状态码；未知字段必须被忽略。
2. **反作弊不进 spec**：只暴露「可能返回 429/403 + 粗粒度原因码 + `Retry-After`」，
   不写阈值、名单、评分权重或判定条件。
3. **时间与金额**：时间戳一律 UTC（RFC 3339）；金额与系数用十进制字符串，不用浮点。
4. **破坏性变更**走 MAJOR 升级，并提供迁移说明；CI 会对比上一提交自动拦截。

## CI

| Job | 作用 |
| --- | --- |
| `spec-lint` | OpenAPI 3.x 结构校验；每个操作必须有 `responses`；spec 内不得出现反作弊内部字段 |
| `spec-contract` | 与上一提交对比，拦截破坏性变更（删端点/删响应码/新增必填字段/类型变更） |
| `secret-scan` | 密钥扫描（含历史） |
| `redline-scan` | 红线表述扫描 |
| `dco` | 提交须带 `Signed-off-by` |

## 本地校验

```bash
pip install pyyaml
python -c "import yaml; yaml.safe_load(open('openapi.yaml'))"
python ci/spec_breaking.py HEAD~1     # 破坏性变更检查
```

## 许可

Apache-2.0（见 `LICENSE`）。
