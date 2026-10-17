# 架构说明

## 设计目标

1. **只做拼多多，支持多店铺**：不引入多平台抽象，把拼多多的协议、规则、话术做到位；
   一个进程管理 N 个店铺，每个店铺独立 Cookie / 长连接 / 会话，共用规则与知识库。
2. **规则可配置**：平台规则写进 `config/`，不写死在代码里，规则变化时改配置 + 重跑测试。
3. **发送前一定过合规**：所有出站文本走同一条 `enforce()`，没有旁路。
4. **可离线验证**：自带 Mock，没有店铺账号也能跑通全链路并做自动化测试。
5. **单机部署**：一个进程、SQLite、`127.0.0.1` 面板，商家自己电脑就能跑。

## 分层

```
┌────────────────────────────────────────────────────────────────────┐
│ 拼多多商家后台（多多客服）                                          │
│   收：wss://m-ws.pinduoduo.com/  （长连接，需要 access_token）      │
│   发：POST /plateau/chat/send_message                              │
│   转人工：POST /plateau/chat/move_conversation                     │
└───────────────┬────────────────────────────────────────────────────┘
                │  Cookie（扫码登录获取）
┌───────────────▼────────────────────────────────────────────────────┐
│ pdd_cs/pdd —— 协议层                                                │
│   auth.py       Cookie 解析/保存/校验                               │
│   cdp_login.py  拉起 Chrome 扫码登录，CDP 读取 Cookie               │
│   api.py        HTTP 接口（重试 / 会话过期 / 发送节流 / 签名）       │
│   channel.py    WebSocket 长连接（心跳 / 指数退避重连 / 状态）       │
│   message.py    推送报文解析 → ChatEvent                            │
└───────────────┬────────────────────────────────────────────────────┘
                │  事件
┌───────────────▼────────────────────────────────────────────────────┐
│ pdd_cs/engine —— 决策层                                             │
│   service.py    编排：去重 → 落库 → SLA → 决策 → 执行 → 统计        │
│   decision.py   优先级决策链（黑名单→过滤→风控→转人工→营业时间→    │
│                 关键词→商品→AI→默认回复）                           │
│   rules.py      关键词/转人工/替换/过滤/商品规则                    │
│   compliance.py 平台规则守卫（出站检查/入站信号/重复治理/披露）      │
│   ai.py         OpenAI 兼容模型 + 硬性提示词                         │
│   knowledge.py  Markdown 知识库检索                                  │
│   sla.py        首响/响应时效统计                                    │
└───────────────┬────────────────────────────────────────────────────┘
                │
     ┌──────────▼──────────┐      ┌──────────────────────────┐
     │ store.py（SQLite）   │      │ server.py（本地面板）     │
     │ 会话/消息/回复/风控   │◄────►│ /api/status · 测试台      │
     └─────────────────────┘      └──────────────────────────┘
```

## 一次消息的完整链路

```
客户发消息
  → WS 收到帧
  → parse_ws_frame() 解析为 ChatEvent（文本/图片/订单/商品卡片）
  → service._handle_event()
       ├─ 去重（msg_id）
       ├─ 记录入站 + 更新会话
       ├─ SLA 记录“等待回复”
       ├─ 人工接管中？ → 跳过
       └─ 串行锁 + 0.6s 合并窗口（连续消息只回最后一次）
  → decision.decide()
       1. 黑名单
       2. 入站过滤（刷单/广告/测试）
       3. 频率风控（单会话 / 单店铺）
       4. 转人工信号（安全/监管/人工/投诉/售后/价格/大额/情绪/重复/关键词）
       5. 营业时间
       6. 关键词规则
       7. 商品专属回复
       8. AI（知识库检索 + 硬性规则）
       9. 默认回复
      10. 无匹配策略（转人工/静默）
  → 出站合规 enforce()
       ├─ 承诺/导流/敏感词/绝对化/隐私/态度 检查
       ├─ 改写或兜底（必要时升级为转人工）
       ├─ 重复话术检测与多样化
       └─ 长度限制
  → 拟人延迟（2~4s，给人工留接管窗口）
  → api.send_text() / send_image() / move_conversation()
  → 落库（回复结果、耗时、违规记录）
  → SLA 记录“已回复”，面板实时刷新
```

## 多店铺架构

```
                    ┌──────────────────────────────────────────┐
                    │ PDDAutoReplyService（多店铺编排）          │
                    │  · 共享：SQLite / 规则 / 知识库 / 告警 / 质检│
                    │  · 聚合：状态 / 洞察 / 待处理 / 搜索        │
                    └───────────┬──────────────┬────────────────┘
                                │              │
                 ┌──────────────▼───┐   ┌──────▼───────────────┐
                 │ ShopWorker       │   │ ShopWorker           │
                 │ 店铺 A           │   │ 店铺 B               │
                 │ Cookie / API     │   │ Cookie / API         │
                 │ WebSocket        │   │ WebSocket            │
                 │ 决策引擎 + SLA   │   │ 决策引擎 + SLA       │
                 │ 转人工 / 拍单    │   │ 转人工 / 拍单        │
                 └──────────────────┘   └──────────────────────┘
```

- **配置**：`config.yaml → shops[]`，每个店铺可写 `overrides`（覆盖 behavior/compliance/
  business_hours 等），`Config.for_shop()` 生成店铺级配置视图；
- **隔离**：会话主键 (shop_id, uid)、消息/回复/风控全部带 shop_id，人工接管按店铺生效；
- **聚合**：`/api/status` 返回聚合 KPI + 每个店铺状态；面板右上角切换店铺；
- **容错**：单个店铺 Cookie 失效只让该店铺进入错误状态，其它店铺继续接待（`startup_errors`）。

## 运营辅助模块

| 模块 | 作用 | 关键代码 |
| --- | --- | --- |
| 告警通知 | 店铺掉线/登录过期/断线/转人工失败/超时未回 → 钉钉/飞书/企业微信/自定义 Webhook | `alerts.py`、`ShopWorker._notify` |
| 掉线监测 | 店铺连接状态巡检、掉线/恢复通知、重复提醒 | `service.check_offline_alerts` |
| 进程心跳 | 定期向外部监控上报存活状态，进程崩溃也能被发现 | `alerts.HeartbeatReporter` |
| 会话质检 | 按规则事后抽检乱承诺/导流/漏回/首响超时/重复回复/投诉 | `engine/qc.py` |
| 话术体检 | 上线前扫描关键词话术/知识库/人设的平台规则风险 | `engine/lint.py` |
| 运营洞察 | 24 小时进线、转人工原因、命中话术 Top、知识缺口 | `Store` 聚合查询 + `/api/insights` |
| 待处理队列 | 超时未回、转接失败、人工接管中的会话 | `Store.pending_conversations` |
| 话术库 CSV | 关键词/转人工/替换词与 Excel 互转 | `rules_io.py` |
| 客户黑名单 | 按 uid/关键词静默 | `config/rules/blacklist.yaml` |
| 订单上下文 | 订单卡片沉淀到会话，后续问答带上 | `Store.set_last_order`、`RuntimeContext.last_order` |

## 关键设计决策

### 为什么不用 Selenium/Playwright 点击页面？

拼多多客服工作台本身就是前后端分离的：消息走 WebSocket、发送走 HTTP 接口。
直接走同一套接口：

- 更快（无页面渲染、无选择器等待）；
- 更稳（页面改版不影响，接口字段变化时才需要适配）；
- 更省资源（一个进程即可，不需要常驻浏览器）。

代价是接口字段/风控会变化，因此协议层集中在 `pdd_cs/pdd/api.py`，便于单点适配。
登录仍用浏览器（扫码），因为拼多多登录需要短信/扫码验证，自动化登录既复杂又容易触发风控。

### 为什么单店铺、单进程？

- 拼多多的客服账号、客服子账号、消息会话都是按店铺隔离的；
- 商家通常只有 1~3 个店铺，单进程 + 一份配置最省心；
- 多店铺 = 复制目录，改 `server.port` 与 `pdd.cookie_file`，各跑一个进程（互不影响）。

### 为什么 SQLite？

每天几千条消息、单机写入，SQLite 足够；零配置、单文件、可直接备份/导出。
`store.py` 把 SQL 收敛在一个文件，将来换 MySQL 只改这一层。

### 并发模型

- `asyncio` 单事件循环：长连接、心跳、决策、发送协调；
- 同步 HTTP 调用（`urllib`）通过 `asyncio.to_thread` 丢线程池，避免阻塞事件循环；
- 发送节流（最小间隔）与 SQLite 写入都有锁；
- 每个会话一把 `asyncio.Lock` 串行处理，连续消息有 0.6s 合并窗口，避免“客户连发 3 条，机器人回 3 条”。

## 数据模型（SQLite）

| 表 | 用途 |
| --- | --- |
| `conversations` | 会话维度的昵称、进线/回复次数、人工接管截止时间、最后结果 |
| `messages` | 双向消息流水（in/out），带 msg_id 去重索引 |
| `replies` | 每条自动回复的动作、结果、状态（sent/rejected/failed）、耗时 |
| `risk_logs` | 风控/合规事件（内容拦截、改写、频率、转接失败、登录过期） |
| `kv` | 运行状态（店铺名、登录态摘要、连接状态，按 `shop:<id>:...` 存） |
| `replies.rule_id/reason/question` | 命中话术、转人工原因、触发问题（供洞察/质检/知识缺口） |

## 扩展点

| 想做什么 | 改哪里 |
| --- | --- |
| 加关键词/话术 | `config/rules/keywords.yaml` |
| 加转人工场景 | `config/rules/transfer.yaml` + `config.yaml::transfer_keywords` |
| 加平台敏感词 | `config/rules/compliance.yaml::sensitive` + `rewrite_map` |
| 换大模型 | `config.yaml::llm`（任何 OpenAI 兼容接口） |
| 提高/降低风控强度 | `config.yaml::compliance` |
| 换知识库 | `config/knowledge/*.md` |
| 改面板样式 / 板块 | `pdd_cs/web/dashboard.html` |
| 接自有系统（订单/ERP） | `engine/service.py::_build_context` / `engine/ai.py` 注入上下文 |
| 改告警渠道 | `alerts.py::_build_payload`（或只用内置钉钉/飞书/企业微信格式） |
| 加质检规则 | `config/rules/qc.yaml` |
| 换存储 | `store.py` |
| 适配平台接口变更 | `pdd_cs/pdd/api.py`、`pdd_cs/pdd/message.py` |
