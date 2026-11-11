# 官方特供版 · B 端多模型供方接入（djb-node profile）

给**聚合型上游**（自己接了一堆模型、对外提供 OpenAI 兼容端点）开的节点配置：
装好即具备「容量公告 + 周期报价」能力，节点能被云端调度到，而不是只挂个"已接入"。

| 项 | 值 |
| --- | --- |
| 适用 | Linux（systemd）、Python 3.10+、能出网 443；一台机器**只跑一个节点** |
| 状态目录 | `/var/lib/djb-official-supply`（`DJB_HOME`，0700；SQLite / 日志 / 凭据） |
| 程序目录 | `/opt/djb-official-supply/venv` |
| 运行身份 | 系统用户 `djb-supply`（非 root，`nologin`） |
| 入站端口 | **无**（只绑 `127.0.0.1:1717`，调度不走入站） |
| 默认开启 | `client.relay.enabled`、`client.probe.periodic.enabled`、`client.quotes.publish.enabled` |
| 客户端版本 | **0.3.4**（与 Windows / macOS / Linux 三端同一份源码、同一个 `djb_node.__version__`） |
| 报价参数 | 周期 60 s · TTL 120 s（**TTL 恒等于 2 × 周期**） |

> ⚠ **这三项靠 `facts.FEATURE_FLAGS[*].expire_at` 续期，不是靠本 profile 的 `true` 保命。**
> 开关到期后 `config.feature_flag()` 先判 `flag_expired()` 再读配置，profile 里写 `true` 会被
> 直接压成关闭（`flag_expired` 优先），届时本档位表现为「报价环不启动 / relay 入口消失」，
> 而配置看起来完全正常。0.3.4 已把三项的 `expire_at` 续到 **2027-06-30**（与全表 10 个 12-31
> 开关同批）；续期窗口临近时由 `tests/test_official_supply_profile.py` 的到期哨兵先红。
> 排查口径：`djb flags --json | grep -A2 '<flag 名>'` 看 `expire_at`，不要只看 config.json。

> 本文只讲「怎么把这台机器变成能接单的供方节点」。客户端通用命令见仓库根
> `README-CLIENT.md`；机制与取舍见 `design/17-官方供给与价格配置-设计方案-v1.md` §15。

---

## 0. 前置检查（30 秒）

```sh
python3 -V                                   # 需要 3.10+
ss -ltnp 2>/dev/null | grep 1717 || echo "1717 空闲（好）"
getent passwd djb-supply || echo "还没有 djb-supply 用户（install.sh 会建）"
```

三条硬前提：

1. **一台机器一个节点**。节点身份由 `(platform, hardware_fingerprint)` 唯一确定，
   同一台机器跑两个 daemon 只会互相顶掉/被服务端判重复；要多节点请加机器。
   真撞上了也不会是一句抽象 409：客户端把它翻成可执行指引（`djb status` 的 `下一步`）——

   | 服务端返回 | 含义 | 客户端给的下一步 |
   | --- | --- | --- |
   | `40903` | 同指纹已注册且已绑定账户，本次又没带有效密钥（INV-01 防凭据接管） | ① 原机还有密钥 → `djb auth --set-key -` 复用；② 重装/换机丢了密钥 → 原账户解绑或 `djb auth --bind <码>`；③ 要多节点 → 加机器 |

   这条是**永久错误**：客户端不会自动重试注册（重试既不会好，也拿不到别人的身份），
   必须由人先解绑或复用密钥。
2. **一个节点可以挂多个上游**（多个聚合站/自建站都行），云端按 `(access, model, unit)`
   取报价，同一个模型只认**最新一条**；客户端已经帮你收敛成「同模型取最低价一条」。
3. **计量单位只有一个**：`unit = "token"`。它不是价格单位，别改成
   `usd_per_1m_tokens`（云端的容量公告与报价按 `(node_id, access, unit)` 对齐，
   写成别的值 ⇒ 节点直接"看不见"，调度侧只会得到 `no_quote`）。

---

## 1. 装（一条命令）

```sh
# 在线装（从 PyPI / 内网源取 djb-node）
sudo ./install.sh --gateway https://gateway.example.com

# 离线装（把 wheel 一起带过去）
sudo ./install.sh --gateway https://gateway.example.com --wheel ./djb_node-0.3.4-py3-none-any.whl

# 先看会做什么（不需要 root，不改任何东西）
./install.sh --gateway https://gateway.example.com --dry-run
```

`install.sh` **幂等**：重跑不会重建用户、不会覆盖已有 `config.json`、
unit 内容没变就不重写、不 `daemon-reload`。它**不会** start/restart 任何服务
（包括本单元）——要不要跑由你决定：

```sh
sudo systemctl enable --now djb-official-supply
```

装完先写密钥（交互输入，不进 shell 历史）：

```sh
sudo -u djb-supply DJB_HOME=/var/lib/djb-official-supply \
     /opt/djb-official-supply/venv/bin/djb auth --set-key -
```

> 密钥只落在 `$DJB_HOME/credentials/`（0600），永远不会出现在配置、日志、命令行参数里。
> 没有密钥时 daemon 照样起得来，但报价会**主动不发**（日志 `quotes.publish_skipped`
> `reason=no_client_key`），不会去刷一堆 401。

---

## 2. 接上游（聚合站 = B 类 + 显式 `--kind`）

B 类接入**必须显式**给 `--kind`：不给的话服务端会把 kind 默认推成
`enterprise_gateway`，中转站会被策略当成"企业网关"，`ignored_kinds` / 策略分流会全部判错。
客户端现在直接拒绝这种"静默套默认值"的添加（P3）。

**本预置档把这一条固定了**：`config.json` 的 `extra["upstream.kind"] = "third_party_relay"`，
所以在这台机器上 `--kind` 可以省略（客户端按预置档的值接入，不是服务端默认值）；
显式给一个**别的** `--kind` 会被拒绝（预置档是承诺，不能在命令行上悄悄漂走）。
普通客户端（没有预置档）仍然必须自己写 `--kind`。

```sh
# 中转站（第三方聚合）——密钥从 stdin 读，避免进 shell 历史
printf '%s' "$AGG_KEY" | sudo -u djb-supply DJB_HOME=/var/lib/djb-official-supply \
  /opt/djb-official-supply/venv/bin/djb upstream add \
    --access B --kind third_party_relay \
    --base-url https://agg.example.com/v1 \
    --models gpt-4o,gpt-4o-mini,deepseek-chat \
    --secret-stdin

# 自建/企业网关（不是中转站）就用 --kind enterprise_gateway
```

多个聚合站就重复上面的命令（各自一个 `--id`，`upstream list` 可看）。
**同一个模型被两个上游同时提供是正常的**：客户端只发最便宜的那一条并计入
`skipped.duplicate`，不会每轮在两个价格之间反复横跳。

---

## 3. 定价（USD / 1M tokens，落库是"分"）

```sh
djb upstream price --id <上游id> --model gpt-4o --min-price 0.75   # 0.75 美元/1M
djb upstream price --id <上游id> --model deepseek-chat --min-price 0.14
```

规则（客户端在生成报价行时逐条执行，不看人）：

| 规则 | 说明 |
| --- | --- |
| 0 价 / 未定价 → **不发** | 0 价等于白送（云端 `charge_minor_for_tokens(price=0)=0`），一律计入 `skipped.zero_price` |
| 换算成**整数分** | `0.75` → `75`，`0.005` → `1`（四舍五入到分）；折算成 0 分的（如 `0.0049`）同样不发 |
| 小数最多到分 | 别写 `0.0001` 这种；写 3 位以上小数会被四舍五入 |

> 价格只上行到 `POST /api/v1/schedule/quotes`（鉴权后），日志里**只有计数**，
> 不写价格、也不写模型清单。

---

## 4. 体检 + 干跑（不上行、不花钱）

```sh
# 体检：只打上游的 /models，把结论写本地并上报（服务端不探测你的上游）
djb upstream probe --id <上游id>

# 干跑：把"将要发送的报价行"原样打出来（不含网络请求）
djb quotes publish --dry-run
```

干跑输出读法：

```
上游  access  模型          单价(USD分/1M)  TTL(s)  unit
----  ------  ------------  --------------  ------  ----
u1    B       gpt-4o        75              120     token
周期 60s · TTL 120s · unit token · 共 1 行 · 跳过：zero_price=2
```

* `TTL(s)` 恒等于 `2 × 周期`。云端的"内容不变 = 原样复用**不续期**"是硬行为，
  TTL 只比周期大一点点就会在每个周期末尾掉出候选集，所以别手改；
* `跳过：...` 是**每条模型的落选原因计数**，先看它再看要不要调价/补探针。

---

## 5. 开跑与自检

```sh
djb quotes publish            # 手动发一轮（首次验证用；重复发同一内容服务端复用，不升版本）
djb status --json | jq .quotes
```

`status.quotes` 字段：

| 字段 | 含义 |
| --- | --- |
| `enabled` / `running` | 开关是否打开 / 报价线程是否在跑（关闭时恒 `false`，行为与无此能力版本一致） |
| `period_s` / `ttl_s` | 当前周期与 TTL（应为 60 / 120） |
| `at` / `rows` | 上一轮时间 / 生成的行数 |
| `created` / `reused` / `failed` | 上一轮：新建 / 服务端复用 / 失败 |
| `skipped` / `skipped_total` | 上一轮落选原因计数（`zero_price`、`stale_probe`、`not_probed`、`duplicate`、`upstream_gated`、`disabled`、`invalid_price`） |
| `error_classes` | 上一轮失败分类（`auth`、`forbidden`、`rate_limit`、`network`、`invalid`…） |

daemon 日志每轮一行 `event="quotes.publish"`，只有数字（行数/新建/复用/失败/跳过计数），
不含价格与模型名。

### 5.1 探针失败即停发（P4 的运行期门控）

relay 的「更严证明」不是一句注释，而是**每一轮报价前都算**的两道闸：

| 闸 | 判据 | 不满足时 |
| --- | --- | --- |
| 探针结论 | 模型 `inference_ok is True`（`djb upstream probe` 真的打通过一次） | 该模型不发，计 `skipped.not_probed` |
| 新鲜度 | `last_probe_at` 距今 ≤ **600 s**（比云侧 900 s 更保守 300 s） | 该模型不发，计 `skipped.stale_probe` |

所以「上游挂了」不需要人工停报价：探针一旦失败，下一轮就自动不发该模型；
周期复探（本档默认开启）负责在恢复后自动把它放回来。要立刻全停就用 §7 的回滚第一步。

巡检动作（建议每天一次，或价格/上游有变动时）：

```sh
# ① 干跑：本轮要发什么、为什么跳过（离线，不上行）
djb quotes publish --dry-run

# ② 看「探针类」跳过是否在涨（涨 = 上游不稳/探针没跑起来）
djb status --json | jq '.quotes.skipped'
```

判据：`skipped.not_probed` / `skipped.stale_probe` 只应在「刚接上游、还没 probe」
或「上游真的故障」时非 0；两者长期非 0 说明周期复探没开或上游不可用。

### 5.2 毛利巡检：`negative_margin=0` 是硬门

对客价必须 ≥ 成本价（倒挂绝不发）。巡检有两条入口，判据逐字一致：

```sh
# 客户端侧（本档）——价格与门控的离线自证：TTL/零价/倒挂/候选集
#   ① ttl_s == 2 × period_s；② zero_cost_skipped > 0 且 negative_margin == 0；
#   ③ 只有「社区无候选」的模型被发价；④（--live）自营节点进候选集
./verify.sh                       # 全离线；--live 才真发价（运维在预发执行）

# 运维侧（真实用量）——按窗口算负毛利并输出禁报名单
python3 margin_check.py --node-id <节点id> --window-min 60 --json
```

判据（任一不满足 = 停 cron / 停发，先查价格表再谈重新开）：

| 输出 | 合格值 | 不合格怎么办 |
| --- | --- | --- |
| `counts.negative_margin` | **0** | >0 = 倒挂（成本涨了没调价 / 加价 ≤ 0）→ 立刻停发，修价格表后复算 |
| `counts.zero_cost_skipped` | **> 0** | =0 说明 P1 的零价闸没被触发过（夹具/上游变了），要核对成本源 |
| `margin_check` 退出码 | `0`（无负毛利） | 非 0 → 看 `newly_denied`，这些模型下一轮起不再报价 |

两条都是**离线可复算**的：价格表 + 用量夹具在，结论就可重放（不需要生产凭据）。

---

## 6. 常见错误对照

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `quotes.publish_skipped reason=no_client_key` | 还没写节点密钥 | `djb auth --set-key -`（见 §1） |
| 干跑 `跳过：not_probed=N` | 模型没体检过 | `djb upstream probe --id <id>` |
| 干跑 `跳过：stale_probe=N` | 探针结论超过 **600 s**（比云侧 900 s 更保守，故意提前停发） | 打开周期探针（本 profile 默认已开 `client.probe.periodic.enabled`）或手工 `djb upstream probe` |
| 干跑 `跳过：zero_price=N` | 没定价 / 0 价 / 折算 0 分 | `djb upstream price ...` |
| 干跑 `跳过：upstream_gated=N` | 上游被服务端判 rejected 或本地停用 | `djb upstream list` 看 state；修正上游后重新 probe |
| 干跑 `跳过：duplicate=N` | 同一模型有更便宜的上游 | 正常，不用管 |
| 报错 `ttl_s 必须 = 2 × 发布周期` | 手工改了 `config.json` 的 `quotes.ttl_s` | 改周期请**同时**把 `ttl_s` 设为 2×，例如周期 90 → `ttl_s: 180`；否则 daemon **fail-closed**（不建线程、不上行） |
| 报错 `B 类接入必须显式 --kind` | `upstream add --access B` 少了 `--kind` | 中转站用 `--kind third_party_relay` |
| `quotes publish` 报"开关关闭" | `client.quotes.publish.enabled` 被关了 | `djb flags --enable client.quotes.publish.enabled` |
| `djb status` 下一步说「硬件指纹已在服务端注册过」 | 同机重复注册（服务端 `40903`，一机一节点） | 原机还有密钥 → `djb auth --set-key -` 复用；丢了 → 原账户解绑或 `djb auth --bind <码>`；要多节点 → 加机器 |
| 干跑 `跳过：stale_probe` 长期不降 | 周期复探被关了 / 上游不可达 | `djb flags --enable client.probe.periodic.enabled`；仍不降看上游 `/models` 是否可达 |
| 服务端 401 | 密钥错/被轮换 | `djb auth --pair` 重新生成，或 `--set-key` 重设 |
| 服务端 404 节点不存在 | 密钥对应的节点在服务端不存在 | 先 `djb auth --bind <码>` 或重新注册 |

---

## 7. 回滚（三步，无残留）

```sh
# 1) 立刻停发新报价（已发报价 ≤ TTL（120 s）自然过期，不会继续被调度）
djb flags --disable client.quotes.publish.enabled

# 2) 停服务
sudo systemctl stop djb-official-supply

# 3) 需要彻底下线节点：解绑（高危，需 --yes）
djb auth --unbind --yes
```

改回旧版本的客户端即可：报价能力是**追加**的（新路径 + 新能力位），
旧版本不认识这些也不会报错，行为与接入前一致。

---

## 8. 安全边界（这版刻意做到最小）

* **无凭据进制品**：`config.json` / unit / 本 README 里没有任何真实域名或密钥，
  `https://gateway.example.invalid` 是不可解析占位域（RFC 6761），必须替换；
* **凭据只在 0600 文件**：上游密钥走 `--secret-stdin`，节点密钥走 `auth --set-key -`，
  都不进 shell 历史、不进日志、不进提交；
* **无入站**：daemon 只绑 `127.0.0.1:1717`，云侧调度通过出站心跳/报价完成；
* **日志最小化**：报价日志只有计数与失败分类，不含价格、不含模型清单；
* **非 root**：`djb-supply` + `NoNewPrivileges` + `ProtectSystem=strict`，
  可写点只有状态目录。
