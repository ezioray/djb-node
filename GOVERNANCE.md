# 治理（DJB 公开仓）

## 1 角色

| 角色 | 职责 | 谁 |
| --- | --- | --- |
| 维护者（maintainer） | 合并 PR、裁决 RFC、发布、安全响应 | 项目发起人指定，当前 1–2 人 |
| 复核人（reviewer） | 代码/契约/安全复核；账本与凭据边界改动必须二人复核 | 由维护者邀请 |
| 贡献者（contributor） | 提 issue / PR / RFC，签 DCO | 任何人 |

## 2 决策

- 日常改动：**懒共识**——PR 72 小时无反对且 CI 全绿，维护者可合并。
- 契约、账本、安全边界、发布流程：至少 1 名复核人 + 维护者批准；账本改动必须附复算证据。
- 争议：维护者裁决并记录在 PR；重大分歧走 RFC。
- 反作弊阈值、名单、评分权重不进公开仓，见 `CONTRIBUTING.md §1`。

## 3 RFC

- 模板：`docs/rfc/0000-template.md`；编号递增，状态 `draft → accepted → implemented / rejected`。
- 未 accepted 的 RFC 不得进入实现；实现 PR 必须引用 RFC 编号。

## 4 发布与安全

- 发布由维护者执行，附变更摘要、SBOM、校验和；客户端安装包必须签名（Windows EV / Apple Developer ID）。
- 漏洞按 `SECURITY.md` 私下报告；修复前不公开细节；90 天协同披露。
- 任何凭据、密钥、真实配置不得进入 issue、PR、日志或对话。

## 5 行为

参与即表示同意 `CODE_OF_CONDUCT.md`。
