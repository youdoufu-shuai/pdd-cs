# 运营手册

面向店铺运营 / 客服主管。技术细节见 [architecture.md](architecture.md)，规则依据见 [platform-rules.md](platform-rules.md)。

> 多店铺：每个店铺独立登录（`python -m pdd_cs login --shop <id>`），`run` 一次带起全部店铺；
> 面板右上角切换「全部店铺 / 单个店铺」，店铺状态、待处理、质检、洞察都会带店铺标识。

## 零、多店铺接入

```bash
# 1) 配置 shops（config/config.yaml）
#    shops:
#      - id: shop-a
#        name: A店（女装）
#        cookie_file: config/secrets/shop-a.json
#      - id: shop-b
#        name: B店（百货）
#        cookie_file: config/secrets/shop-b.json

python -m pdd_cs login --shop shop-a
python -m pdd_cs login --shop shop-b
python -m pdd_cs doctor          # 逐店铺检查登录态
python -m pdd_cs run             # 一个进程带起所有店铺
```

单个店铺失效不会影响其它店铺：面板「店铺状态」会显示异常店铺，日志/告警里也会提示重新登录。

## 一、上线前检查清单

- [ ] `python -m pdd_cs doctor` 全部通过（Cookie、规则、知识库、端口）
- [ ] `config/persona.md` 已改成自己店铺的语气
- [ ] `config/knowledge/store-policy.md` 的发货时效、快递、运费险、退换货、发票、营业时间已按真实政策填写
- [ ] `config/knowledge/product-faq.md` 已按商品类目补充（尺码、材质、保质期等）
- [ ] `config/rules/keywords.yaml` 高频问题话术已覆盖，且**没有**“今天一定发/包退/保证”这类承诺
- [ ] `config/rules/transfer.yaml` 已加自己店铺需要的转人工场景
- [ ] `config.yaml::compliance.human_handoff.assign_cs_name` 指定了接手的人工客服（或确认自动选第一个）
- [ ] 用测试客户号走一遍：普通咨询、售后、投诉、人工请求、加微信、承诺话术
- [ ] 面板能看到会话、自动回复、转人工、风控日志
- [ ] 小流量灰度 1~2 天，确认无异常再全量

## 二、日常运行

### 启动 / 停止

```bash
python -m pdd_cs run        # 启动（面板 http://127.0.0.1:38617）
Ctrl+C                      # 停止
```

建议用开机自启（Windows 任务计划 / macOS launchd / Linux systemd），崩溃后自动拉起。

### 每天看什么（面板）

| 指标 | 正常范围 | 异常处理 |
| --- | --- | --- |
| 连接状态 | 已连接（在线） | 断线会显示重连中/异常，检查网络与登录态 |
| 今日进线 | 按店铺量 | 突然归零：检查长连接/客服是否在线 |
| 自动回复 | 应接近进线数 | 明显偏低：看“最近会话”里大量 transfer/no_match 的原因 |
| 转人工 | 比例稳定 | 突然升高：话术/知识库可能缺内容，或规则误判 |
| 首响达标率 | ≥ 90%（按自己标准） | 下降：检查是否被频率风控、AI 超时、消息积压 |
| 风控拦截 | 少量 | 出现 content_blocked：看具体是哪句话，回去改话术 |
| 最近错误 | 无 | 出现“登录态过期”立即重新 login |

### 日志

`data/logs/pdd_cs.log`（滚动，单文件 5MB×5）。排障时看最后 200 行即可。

## 三、人工接管与转人工

### 人工怎么接手

1. 在拼多多「多多客服」工作台里直接回复客户 → 系统检测到人工消息后，**自动暂停该会话 15 分钟**
   （`behavior.human_takeover_minutes`），期间不再自动回复。
2. 需要看上下文：打开面板 → 最近会话，或 `GET /api/messages?uid=...`。
3. 想提前恢复自动回复：等接管时间结束，或重启服务（不推荐）。

### 转人工是怎么发生的

- 命中投诉/售后/退款/人身安全/监管/情绪/大额/重复追问/客户要人工 → 自动转接；
- 先给客户发提示（`notify_message`），再调用平台接口把会话转给人工客服；
- 转接成功后该会话暂停自动回复；
- 转接失败（没有可用客服等）：按 `fallback_to_manual` 暂停自动回复并写风控日志，
  面板「风控与合规日志」会显示 `transfer_failed`。

### 转给谁

`compliance.human_handoff.assign_cs_name` 填客服名称（比如“小美”），留空则自动选第一个可分配客服。
建议在拼多多后台先把人工客服子账号建好，并确认当前账号有分配权限。

## 四、知识库维护

按信息变化速度分三档维护（这是最省力的做法）：

| 类型 | 例子 | 维护方式 |
| --- | --- | --- |
| 几乎不变 | 材质、尺码表、使用方式 | 建好基本不用动 |
| 随批次变化 | 生产日期、库存 | **不要写死数值**，只写“怎么查/以实物为准” |
| 随时变化 | 价格、活动、赠品 | 不进知识库，转人工或实时数据 |

更新后不需要重启：面板刷新时 AI 检索会用最新文件（`KnowledgeBase.reload_if_changed` 在搜索前检查）。
保险起见，改完运行 `python -m pdd_cs simulate "相关问题"` 验证一遍。

## 五、话术红线（发给客服团队）

1. 不承诺具体发货时间、到达时间。
2. 不承诺退款/退货/赔偿结果，不说“包退”“我说了算”。
3. 不给微信/QQ/手机号/链接/二维码。
4. 不索要身份证、银行卡、验证码。
5. 不说“最”“第一”“100%”“保证”这类绝对化用语。
6. 客户要人工、投诉、售后、涉及安全 → 先安抚，再转人工。
7. 不确定的事说“我帮您转人工确认一下”，不要编。

## 五（补充）、日常运营工具箱

| 想做的事 | 怎么做 |
| --- | --- |
| 早上看经营数据 | 面板「运营概览 / 店铺状态 / 运营洞察」，或 `python -m pdd_cs stats` |
| 处理超时和转接失败 | 面板「待处理队列」；开启告警后会自动推送到团队群 |
| 复盘客服/机器人话术 | `python -m pdd_cs qc --days 7`，或面板「会话质检」 |
| 上线前检查话术风险 | `python -m pdd_cs lint --strict`（可接 CI） |
| Excel 维护话术 | `python -m pdd_cs rules export --type keywords --output kw.csv`，改完再 `rules import` |
| 拉黑骚扰客户 | 编辑 `config/rules/blacklist.yaml`（uid 或关键词） |
| 导出聊天记录 | `python -m pdd_cs export --days 30 --shop shop-a` |
| 测试告警通道 | `python -m pdd_cs alert-test` |

### 告警通知配置（config.yaml）

```yaml
alerts:
  enabled: true
  webhook_url: https://oapi.dingtalk.com/robot/send?access_token=xxx
  format: dingtalk        # generic | dingtalk | feishu | wecom
  events: [shop_offline, shop_online, connection_error, session_expired, transfer,
           transfer_failed, sla_overdue, send_failed]
  min_interval_seconds: 60
  offline_after_seconds: 120     # 掉线超过 2 分钟告警
  offline_repeat_seconds: 1800   # 未恢复每 30 分钟提醒一次

# 进程心跳：进程挂了/服务器宕机时由外部监控报警（详见 deployment.md）
monitor:
  heartbeat_url: https://hc-ping.com/xxxxxxxx
  heartbeat_interval_seconds: 300
```

建议至少打开 `shop_offline`、`session_expired`、`transfer_failed`、`sla_overdue` 四类——
它们代表“客户在等但没人接”。进程级故障由外部监控轮询 `GET /healthz?strict=1` 或接收心跳发现。

### 会话质检怎么看

- 面板「会话质检」按严重程度列出命中：高（乱承诺/导流/隐私/转人工失败/投诉关键词）、
  中（漏回/首响超时/敏感词/情绪）、低（重复话术/绝对化用语）；
- 每条命中带店铺、客户、原文片段，点开「会话监控」可看到完整上下文；
- 质检是**事后抽检**：实时拦截在发送前已经做掉了，质检用来发现“规则没覆盖到”的问题。

## 六、故障排查

| 现象 | 可能原因 | 处理 |
| --- | --- | --- |
| 启动提示“尚未登录” | Cookie 文件不存在 | `python -m pdd_cs login` |
| 启动提示“登录态已失效” | Cookie 过期/被踢下线 | 重新 login；面板“最近错误”有提示 |
| 连接状态一直“重连中” | 网络/代理阻断 wss，或 token 获取失败 | `doctor` 看接口是否通；检查代理 |
| 收不到消息 | 客服未上线/不在接待分组/被其他端接管 | 后台把客服置为在线；确认账号在接待组 |
| 发送被拒（rejected） | 内容违规/频率过快/敏感词 | 看风控日志的 `send_rejected`，改话术；系统会自动兜底 |
| AI 不回复 | 未配 Key/接口失败 | `doctor` 看 AI 状态；面板“AI 模型”是否未启用 |
| 转人工失败 | 没有可分配客服/无权限 | 后台建人工客服子账号；日志看 `transfer_failed` |
| 面板打不开 | 端口被占用 | 改 `server.port`；`doctor` 会提示端口可用性 |
| 只有一个店铺异常 | 该店 Cookie 过期/被踢 | 面板「店铺状态」→ 单独 `login --shop <id>`，其它店铺不受影响 |
| 质检命中很多 | 话术库有风险表述 | 跑 `lint` 定位，改 `keywords.yaml` / 知识库后重新上线 |
| 进程挂了/服务器宕机 | 没有进程发 Webhook | 配外部监控（Healthchecks.io / Uptime Kuma）收心跳或轮询 strict 健康检查，见 [deployment.md](deployment.md) |
| 首响达标率下降 | 消息集中、AI 慢、被风控暂停 | 面板“超时等待”列表；检查风控日志 |

## 七、数据与备份

- 数据库：`data/pdd_cs.db`（SQLite），每日备份这个文件即可；
- 日志：`data/logs/`；
- Cookie：`config/secrets/cookies.json`（敏感，勿外传，勿提交 git）；
- 导出：`python -m pdd_cs export --days 30` → JSON（会话/消息/回复/风控），用于客诉复盘或申诉取证；
- 清理：SQLite 可长期保留；对话量大时按季度归档导出后删除旧库（停止服务再操作）。

## 八、变更与灰度

1. 改配置/话术 → `python -m pdd_cs simulate "场景"` 回归；
2. 重启服务前，先看面板是否有大量“等待回复”的会话，避免打断接待；
3. 大促前：提前扩容人工排班、把高频问题补进知识库、确认 AI Key 余额；
4. 平台规则变化：按 [platform-rules.md](platform-rules.md) 第五节流程更新。

## 九、排班建议

- AI 负责 7×24 的稳定问题（发货时效、快递、尺码表、售后流程、发票流程）；
- 人工重点覆盖：售后/退款、投诉、大额订单、情绪激动、复杂咨询；
- 夜间：开启 AI 接待，复杂问题留待次日人工（对应 `business_hours` 与转人工策略）；
- 大促：把 `compliance.risk` 的频率上限适当调低，避免连续回复过快被平台风控，
  同时把 `human_takeover_minutes` 调大，方便人工持续接管。
