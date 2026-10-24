## 改了什么

<!-- 一句话；关联 issue / RFC 编号 -->

## 类型

- [ ] feat  - [ ] fix  - [ ] docs  - [ ] test  - [ ] chore  - [ ] rfc

## 边界自查

- [ ] 只改本模块写域；未跨模块写表/改契约
- [ ] 接口只加不改；新增端点/字段已先改 `openapi.yaml`
- [ ] 新增开关默认关闭，owner / expire_at / rollback_rule 三字段齐全
- [ ] 无凭据、密钥、真实配置、用户正文进入代码/日志/测试
- [ ] 账本相关改动附复算证据（迁移前后差异 0）；否则不涉及
- [ ] 测试：`PYTHONPATH=. python -m pytest tests -q` 全绿（附原始尾巴）

## 证据

<!-- ops/audit/reports/... 或 CI 链接；无证据视为未完成 -->

## 回滚

<!-- 关闭哪个开关 / revert 哪个 commit / 是否需要数据迁移 -->
