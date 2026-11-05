# DJB 联脑 · 节点客户端（M15 client-runtime，S1 + S2 P1 / Linux CLI + daemon）

本仓库的 openapi.yaml 是协议契约；本文件只描述客户端（repos/djb-node 的 djb_node/ 包）。
S1 范围：Linux CLI + daemon、本机回环镜像端点、M02 注册/心跳/策略/上游、M05 双签收据上报、
环境检测、L1–L3 限额、容量公告、更新骨架。

S2 P1 增量（2026-10-24，默认开关关闭）：平台抽象层（Windows/macOS/Linux 目录、服务描述、凭据后端探测）、
模型探针 L1→L2/L3 + 周期复探 + 公告门控、本地 token 计数与 usage 差异检测、第三方中转站接入、
模型身份证明客户端侧核心（L0–L5 线索采集）。**仍无托盘 / GUI、无真实出站长连接（C10）、
无服务端证明定级（C12 服务端侧）；不做内存/存储共享、不装驱动、不静默提权。**

## 1. 安装与首次运行

要求：Linux（Ubuntu 22.04+ / Debian 12+ / Fedora 最近两版；x86_64 / aarch64），Python 3.10+。
客户端**零第三方运行时依赖**（只用标准库），便于装进最小服务器镜像。

    python3 -m pip install --user -e .        # 或 python3 -m pip install .
    djb init                                  # 初始化 + 环境检测（结论 + 下一步）
    djb env --authorize                       # 可选：一次授权应用保守补全（可 rollback）
    djb auth --status                         # 未绑定也能离线自测
    djb upstream add --access A2 --base-url http://127.0.0.1:11434/v1 --models my-model
    djb start                                 # 启动 daemon，打印一次性 local_token
    djb status                                # 状态 / 原因 / 下一步 / 收据 / 限额

不装包也可以直接从仓库运行：PYTHONPATH=. python3 -m djb_node <command>。

## 2. 命令面（每个命令 --help 可用）

| 命令 | 作用 | 关键参数 |
| --- | --- | --- |
| djb init | 初始化目录 / 预设 / 限额 / 服务端地址；跑一次环境检测 | --gateway --port --preset --daily-tokens --concurrent --cpu/--gpu/--mem --channel --update-source --no-env |
| djb env | 环境检测（硬件/系统/运行时/加速栈/网络） | --authorize --rollback --budget --json --no-persist |
| djb auth | 节点密钥 / 绑定 / 轮换 | --status --set-key KEY\|- --pair --overlap-seconds --bind CODE --unbind --yes |
| djb start | 启动 daemon（只绑 127.0.0.1） | --foreground --port --timeout |
| djb stop | 停止 daemon（幂等） | --timeout --force |
| djb status | 状态与诊断摘要 | --json --show-token（读取本次进程令牌） |
| djb pause / resume | 用户暂停 / 恢复（重启后保留） | — |
| djb limits | 查看 / 设置并发、日周月配额、资源上限、时段、让路 | --preset --daily-tokens --weekly-tokens --monthly-tokens --concurrent --cpu/--gpu/--mem --yield/--no-yield --schedule --schedule-json --clear-schedule |
| djb upstream | A1/A2/B 上游、模型开关与最低价、探测、relay、A2 prompt | add / list / probe / discover / map / unmap / relay / enable / disable / price / remove / a2-prompt / declare |
| djb token count | 本地 token 计数（不联网；估算值标 estimated） | --model --text --file --messages --tools |
| djb plan | 多 plan 组：组与成员、组级共享日/月预算（金额 USD，内部按 minor 整数记账） | group add/list/remove/add-upstream/remove-upstream；budget set/show |
| djb flags | 查看 / 设置本机特性开关（登记表见 facts.FEATURE_FLAGS，默认关闭） | --enable NAME / --disable NAME |
| djb attest | 查看本地模型身份证明记录（客户端线索，不自我宣称 verified） | --id --model |
| djb announce | 手动公告容量（模型 / 价格 / QPS） | --dry-run |
| djb quotes | 供方报价：`show` 看将发送的行（不联网）/ `publish` 发一轮（TTL 恒 = 2 × 周期） | show / publish [--id] [--period-s N] [--dry-run] |
| djb update | 更新骨架：检查 / 安装 / 回滚 / 状态 | --check --apply --rollback --status --source --channel |
| djb logs | 查看脱敏日志（7 天保留） | --tail N --follow --level info\|debug\|warning\|error |
| djb diagnose | 生成脱敏诊断包（无凭据 / 无正文） | --output PATH |
| djb uninstall | 卸载：清服务 / 配置 / 凭据 / 缓存并自检残留 | --yes --keep-logs --dry-run |

退出码语义：0 成功；1 未分类；2 用法；3 未初始化；4 daemon 未运行；5 凭据缺失/无效；
6 网络或上游不可达；7 签名/哈希/策略校验失败；8 状态冲突（端口占用、非法迁移等）。

### 2.1 S2 P1 能力与开关（默认关闭）

| 能力 | 命令 / 行为 | 开关 | 回滚 |
| --- | --- | --- | --- |
| 周期复探 | `djb upstream probe --id [--level L2\|L3]`；启动先做一轮 L1–L3，之后按开关周期复探（10 min 起，按模型数退避、失败指数退避） | `client.probe.periodic.enabled` | 关闭 → 只保留手动 probe；公告按已存 `last_probe_at` 判新鲜 |
| 公告门控 | 只有 `verified` + 15 min 内新鲜 + 启用的模型拿正 QPS；未验证/过期模型 `max_qps=0` | 无（随探针） | — |
| token 计数 | `djb token count --model M --text ...`；返回 tokens / tokenizer / estimated；无 tiktoken 时启发式（中文 ≈1 token/字） | `client.token.compare.enabled`（差异检测） | 关闭 → 只记录上游 usage，不比较 |
| 第三方中转站 | `djb upstream add --kind third_party_relay --auth bearer --secret-stdin` → `discover` → `map`；未映射不公告 | `client.relay.enabled` | 关闭 → 隐藏写路径；已有配置只读、不删除 |
| 报价发布（供方） | `djb quotes show / publish`；daemon 按 `quotes.period_s`（默认 60 s）周期报价，`ttl_s` 恒 = 2 × 周期；0 价 / 未定价 / 探针不新鲜（> 600 s）/ 未验证一律不发；同一 `(access, model, unit)` 只发最低价一条 | `client.quotes.publish.enabled` | 关闭 → 停发新报价（daemon 报价环不启动、CLI publish 拒发）；已发报价 ≤ TTL 自然过期，不改账本 |
| 模型身份证明 | 客户端产出 L0–L5 证据线索，`djb attest` 查看；本地不宣称 verified | `supply.attestation.enabled` | 关闭 → 回退 L2 可达性；已存证明只读 |
| 多 plan 组 | `djb plan group add --name main --day 1.00 --month 30.00` → `add-upstream --group main --id <upstream>`；请求按组 priority → 成员 priority → 同档 weight 选一个上游，429/401/403/5xx/超时会换下一个（最多 3 次），组预算（日/月，USD，minor 整数）耗尽整组跳过 | `plan.groups` | 关闭（默认即关）→ 退回单上游 `route()` 路径，库表与 spent_* 只读保留；库版本只前进（v4） |
| 平台抽象层 | `platform.paths` / `service` / `credentials`；Linux 路径与 S1 逐字一致，macOS/Windows 默认目录按系统约定 | 无 | — |

开关三字段（owner / expire_at / rollback_rule）登记在 `djb_node/facts.py`；`djb flags` 只能改登记过的开关，
到期强制关。开关改动写 `config.json` 的 `extra.feature_flags`；`djb daemon reload` 可热启停
`channel` / `model_sync` / `notifications` / `quotes` 四个子系统线程，其余开关**重启 daemon 生效**。

**尚未接线**：`LocalApiServer(settings=...)` 已由 daemon 注入用于 token 差异检测；relay 发现结果进入容量公告、
服务端 L3+ 定级、真实出站长连接（C10）、托盘消息中心（C15/C16）在 P2。

## 3. 本机回环端点（本机端点、非开放契约）

按 Lead 裁决（2026-10-24，CR-1 接受）：**127.0.0.1:1717/v1 是本机镜像端点，不进 OpenAPI，
不是开放契约；禁止任何外部依赖把它当公网 API。**

    GET  /v1/models             模型 + 价格 + 可用性（price_version）
    POST /v1/chat/completions   OpenAI 兼容；stream=true 时 SSE 透传
    GET  /v1/health             本机自检（daemon / 上游 / 策略新鲜度；本机可免令牌）

硬约束：

* INV-03：只绑 127.0.0.1（或 ::1）；0.0.0.0 / :: 一律拒绝；非回环来源直接 403。
* local_token：daemon 进程启动时一次性生成，只在内存中；**进程退出即失效、不落盘、不外传**。
  取法：djb start 输出，或 djb status --show-token（同 UID 的 0600 控制套接字）。
* 错误信封与 spec Error 一致（type / code / message / request_id）。
* 不落 prompt / response 正文；日志与诊断只有元数据（模型、tokens、耗时、状态）。

## 4. 本地数据与凭据

默认目录 ~/.djb（DJB_HOME 或 --home 可改；目录 0700）：

    state.db          安装 / Profile / 策略快照 / 公告 / 收据缓冲 / 用量窗口（SQLite，PRAGMA user_version 只前进）
    config.json       非敏感配置（镜像端口、服务端地址、更新频道、日志级别、extra.feature_flags）
    credentials/      节点 client_key 与上游密钥（**0600 文件；S1 显式降级，S2 接 OS 安全存储**）
    keys/             收据缓冲密钥 / 供方签名密钥 / 更新 mock 密钥（0600）
    logs/             djb.log（JSON 行，轮转 5 MiB，保留 7 天，自动脱敏）
    runtime/          daemon.sock（0600 控制套接字）、daemon.out
    versions/         更新包版本目录（保留最近 2 版）
    diagnostics/      诊断包（0600 zip）

INV-02：凭据只存本机；**不上行、不进日志、不进诊断包、不进事件与收据**；A2 只配端点，
不读写 agent 的凭据。S2 接 OS 安全存储 / KMS 后迁移，格式已预留 storage 字段。

## 5. 状态机、让路与限额

daemon 运行态：stopped / starting / online / paused / error / updating；
暂停必须带原因：user_paused / yield_paused / resource_limited / quota_exhausted / schedule_paused /
unbound / conservative / receipt_backlog / upstream_invalid / credential_invalid / account_frozen /
retired / update_failed。**任何「不在赚」的状态都有 reason + next_step**（INV-01），status / health / 日志可见。

优先级 **L1 > L2 > L3**（INV-04）：

* L1 让路：负载、温度、电池、图形会话空闲、渲染/游戏进程；任一检测器异常 -> 视为用户在用并暂停
  （更保守）；服务器无图形会话属于「不适用」，不误暂停。
* L2 资源：CPU 40% / GPU 70% / 内存 70% 默认上限，可调不可越界；超限只停新单。
* L3 配额：服务端 policy 的 quotas[] + 本地 daily/weekly/monthly（0 = 该窗口不限制）+ 允许时段；
  一律**软停**（服务完在途再停）、不结转、不中断用户任务；并发满只拒单不暂停。

## 6. 出站调用与重试

    POST /api/v1/node/register              201  首次 pending；同指纹幂等
    POST /api/v1/node/pair                  201  生成 / 轮换 X-Client-Key（明文只回一次）
    GET  /api/v1/node/binding               200  查询绑定
    POST /api/v1/node/binding               200  一次性码绑定
    POST /api/v1/node/heartbeat             200  心跳 + 容量公告（runtime_state / capacity / load）
    POST /api/v1/node/upstream              200  申报 A1/A2/B（只上行 credential_handle）
    POST /api/v1/node/upstream/probe        200  上报本地体检结论（服务端不探测）
    GET  /api/v1/node/policy                200 / 304（If-None-Match = policy_version；304 按本地 TTL 续租）
    GET  /api/v1/protocol/capabilities      200  能力交集；协议不兼容 -> error + 升级指引
    GET  /api/v1/protocol/deprecations      200  弃用清单
    POST /api/v1/node/receipts (M05)        200  双签收据批量上报（X-DJB-Idempotency-Key）

头：X-Client-Key / DJB-Protocol-Version / DJB-Capabilities / X-Request-Id / X-DJB-Idempotency-Key。
重试：指数退避 1s / 5s / 30s / 5min / 30min，≤8 次；连续 3 次失败 -> 保守模式（不接新单，继续心跳）。
未知状态只查询：不重建密钥、不换绑、不删本地收据缓冲。

## 7. 双签收据

规范串（M05 §8.2 / 04 CR-2 已裁决）：hmac-sha256 前缀 + 64 位小写十六进制；字段顺序
receipt_id / task_id / access / usage_amount / usage_unit / price_amount；按上报**原样字符串**拼接，
分隔符换行。两侧密钥必须不同：供方（本机 keys/receipt_provider.key）签 sig_provider，
需方（平台）签 sig_consumer；**缺一即不入缓冲**。

缓冲（Lead 2026-10-24 裁决 CR-2）：上限 min(24h, M05 约束)；M05 未规定更短 TTL -> 保留 24 h；
超 24 h **不丢弃**，标 stale（数据库 stale=1，诊断包可见计数），停止接单（paused receipt_backlog），
恢复网络后退避重推；收据按 receipt_id 幂等，服务端可重收。

## 8. 环境检测与更新

环境检测：硬件 / 系统 / 运行时 / 加速栈 / 网络，总预算 ≤10 s；每条结论都有「下一步」；
缺加速栈只给指引（不装驱动、不静默提权）；补全动作先出计划，djb env --authorize 一次性应用，
djb env --rollback 原样回滚（记录改动前后值）。

更新骨架（S1 允许 mock 源）：清单签名 + 安装包签名 + sha256 双校验 -> 分桶灰度（5% / 25% / 100%，
每桶观测 ≥24 h）-> 安装 -> 首次启动自检 -> 失败回滚 -> 保留最近 2 版；离线不报错、保持当前版本；
不做降级。S1 用 HMAC-SHA256 mock 密钥（DJB_UPDATE_SIGNING_KEY 或 keys/update_mock.key），
S2 换 M16 发布签名（Ed25519 + 固定公钥）——见 CR-M15-06。

## 9. 安全不变量对照（M15 §3）

| 不变量 | 实现 |
| --- | --- |
| INV-01 不静默失败 | 所有暂停带 reason + next_step；status / health / 日志三处可见 |
| INV-02 凭据只存本机 | 0600 credentials + keys；日志/诊断/事件/收据零凭据；诊断包脱敏复扫 |
| INV-03 只绑回环 + 一次性令牌 | 绑定前校验 127.0.0.1；令牌仅内存，进程退出失效；非回环来源 403 |
| INV-04 优先级与保守 | L1 > L2 > L3；检测失败按用户在用；本地执行上限可调不可越界 |
| INV-05 超限软停 | 只拒新单，不中断在途；不结转；差异 >1% 告警函数 |
| INV-06 策略新鲜度 | digest 形状 + 协议版本 + 过期检查；非法/过期 -> conservative（不接新单） |
| INV-07 收据缓冲 | 密封存储；≤24h；超期 stale + 停接单 + 退避重推（Lead 裁决，不静默丢弃） |
| INV-08 更新校验 | 清单/包签名 + sha256；失败拒绝安装并回滚；保留 2 版 |
| INV-09 结论带下一步 | 环境检测每条结论必须有 next_step |
| INV-10 不隐藏/不共享/不提权 | 无内核模块、无驱动安装、无存储共享、进程可见、卸载残留自检 |

## 10. 测试

    cd repos/djb-node
    PYTHONPATH=. /path/to/djb-cloud/.venv/bin/python -m pytest tests -q

当前：171 passed；覆盖率 87%（限值模块 95%、状态机 96%）。测试全部用 mock HTTP 服务端，
无数据库、无外网；覆盖 CLI、状态机、令牌一次性、回环只绑 127.0.0.1、退避重试、限额、
诊断脱敏、收据 stale、更新签名/回滚、子进程 start/stop 生命周期。

## 11. 变更请求（CR）与裁决

| CR | 内容 | 状态 / 裁决 |
| --- | --- | --- |
| CR-1 | 本机 127.0.0.1:1717/v1 不在 spec | **Lead 2026-10-24 接受**：本机端点、非开放契约、禁止外部依赖；04 补备注 |
| CR-2 | 收据缓冲 24h 与 M05 TTL 关系 | **Lead 2026-10-24 裁决**：min(24h, M05)，M05 无更短 TTL -> 24h 保留 + 退避重推；超期标 stale 计入诊断，不静默丢弃；receipt_id 幂等 |
| CR-M15-02 | 缓冲加密用标准库 HMAC-CTR（非审计 AEAD） | S1 接受，前缀 s1: 预留 S2 迁移 AES-GCM / ChaCha20-Poly1305 |
| CR-M15-04 | spec 只给 daily 默认值，weekly/monthly 无默认 | daily=500000；weekly/monthly 默认 0（不额外限制），由 djb limits 显式启用 |
| CR-M15-05 | NodePolicy 无签名与 expires_at | **Lead 2026-10-24 接受**：S1 用 digest + 新鲜度（issued_at + TTL；304 续租）；签名/expires_at 进 S2 硬化清单 |
| CR-M15-06 | 更新签名算法：S1 mock HMAC | S1 mock；S2 换 Ed25519 + 固定公钥（M16 发布签名） |
| CR-M15-07 | 本机 40101 未在 spec 码段表 | 沿用 M15 §4.1 的 40101；待 spec 补登（不新造其它码） |
| CR-M15-08 | 收据签名算法 | 已由 M05 CR-2 裁决（hmac-sha256 + 6 字段 + 原样字符串），本客户端按此实现 |

## 12. 不做 / 边界

* 不做服务端业务：注册准入、策略生成、收据验签、计费、调度都在 M02 / M04 / M05。
* 不写任何服务端表；不缓存他人数据；不发内部事件（只通过 API 上报）。
* 不改 spec/openapi.yaml、CHANGELOG、ci/、README/SECURITY/LICENSE；本文件是新增的客户端说明。
* 不监听 0.0.0.0；不上传凭据；不落 prompt/response 正文；不做真钱；不 push。
* S2/S3 扩展点：托盘/GUI 只读本机状态（在线态、今日用量）+ 暂停/恢复；复用同一本机 API；
  提权逐次申请；普通用户运行；不反向要求服务端加接口。
