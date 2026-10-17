# 拼多多客服协议说明（实现参考）

> 本文记录 `pdd-cs` 协议层（`pdd_cs/pdd/`）所依赖的拼多多商家后台客服接口形态，
> 用于排障与后续适配。字段来自对商家后台前端行为的公开可观测建模，
> **平台可能随时改版**；接口变化时只需修改 `pdd_cs/pdd/api.py` / `message.py`。

## 1. 登录与凭据

拼多多商家后台使用 Cookie 维持登录态。关键 Cookie：

| Cookie | 作用 |
| --- | --- |
| `PASS_ID` / `SUB_PASS_ID` | 登录态（必备） |
| `MSA` / `SUB_SYS_ID` | 会话/系统标识 |
| `anti_content` | 部分接口（商品卡片等）的风控签名 |
| `api_uid` / `mms_b84d1838` / `_nano_fp` 等 | 风控/会话辅助字段 |

`python -m pdd_cs login` 用 Chrome + CDP 读取登录后的 Cookie，保存到
`config/secrets/cookies.json`。也可以在 F12 → Network → 请求头 `Cookie:` 复制整行后
`python -m pdd_cs login --cookie "..."`。

## 2. 长连接（收消息）

```
wss://m-ws.pinduoduo.com/?access_token=<token>&role=mall_cs&client=web&version=202506091557
```

- `access_token` 来自 `POST https://mms.pinduoduo.com/chats/getToken`，请求体 `{"version":"3"}`；
- 连接建立后需要把客服置为在线（`set_csstatus`），否则平台不推送客户消息；
- 心跳：应用层 ping/pong，默认 30s；连续失败触发重连；
- 断线：指数退避重连（2s → 60s 封顶），重连前重新获取 token。

## 3. HTTP 接口

| 方法 | 路径 | 用途 | 备注 |
| --- | --- | --- | --- |
| POST | `/chats/getToken` | 获取长连接 token | `{"version":"3"}` |
| POST | `/plateau/chat/send_message` | 发文本/图片 | 见下 |
| POST | `/plateau/chat/move_conversation` | 会话转人工 | `data.conversation.{csid,uid,need_wx,remark}` |
| POST | `/latitude/assign/getAssignCsList` | 可分配人工客服 | `result.csList` 字典 |
| POST | `/plateau/chat/set_csstatus` | 客服在线状态 | `1` 在线 / `3` 离线 |
| GET | `/janus/api/new/userinfo` | 当前登录账号 | 校验登录态 |
| GET | `/earth/api/merchant/queryMerchantInfoByMallId` | 店铺信息 | 可选 |
| POST | `/plateau/chat/latest_conversations` | 最近会话 | 可选，用于同步昵称 |
| POST | `/plateau/chat/list` | 历史消息 | 可选，用于补上下文 |
| POST | `/plateau/message/send/mallGoodsCard` | 商品卡片 | 依赖 `anti-content` 签名 |

### 发送文本请求体（观测形态）

```json
{
  "data": {
    "cmd": "send_message",
    "request_id": "<uuid>",
    "message": {
      "to":   {"role": "user",    "uid": "<买家uid>"},
      "from": {"role": "mall_cs"},
      "content": "回复内容",
      "msg_id": null,
      "type": 0,
      "is_aut": 0,
      "manual_reply": 1
    }
  },
  "client": "WEB"
}
```

图片消息：`type: 1`、`content` 为图片 URL、带 `chat_type: "cs"`。

### 统一响应形态

```json
{"success": true, "result": {}}
{"success": false, "error_code": 43001, "error_msg": "会话已过期"}
```

- `error_code == 43001` 且 `error_msg` 含“会话已过期” → 登录态失效，需要重新登录；
- 发送文本在 `success: true` 时仍可能在 `result.error_code == 10002` 返回业务拒绝（内容违规/频率）。

## 4. 消息解析（收）

推送报文常见形态：

```json
{
  "response": "push",
  "message": {
    "type": 0,
    "sub_type": 0,
    "content": "什么时候发货",
    "msg_id": "...",
    "time": 1690000000000,
    "nickname": "买家昵称",
    "from": {"role": "user", "uid": 123},
    "to":   {"role": "mall_cs", "uid": 456}
  }
}
```

### message.type 对照

| type | 含义 | 项目内处理 |
| --- | --- | --- |
| 0 | 文本 | 按 sub_type 与 info 判断普通文本/商品咨询/订单 |
| 1 | 图片 | 转成“买家发送了一张图片”，AI 上下文提示 |
| 5 | 表情 | 取 `info.description` |
| 14 | 视频 | 提示文本 |
| 24 | 会话转接 | 忽略/记录 |
| 64 | 商品规格卡片 | 提取 goods_id / 商品名 / 规格 |
| 1002 | 撤回 | 取 `info.withdraw_hint`，不回复 |

### response 对照

| response | 含义 | 项目内处理 |
| --- | --- | --- |
| `push` | 普通消息 | 进入决策链 |
| `auth` | 鉴权回执 | 忽略 |
| `mall_system_msg` | 系统消息 | 忽略 |

> 注意：普通文本消息的 `sub_type` 也可能为 0，因此本项目以 `info` 中是否真有
> 商品/订单字段来判断是否结构化消息，避免误判（见 `message.py`）。

## 5. 已实现的风险控制

| 场景 | 处理 |
| --- | --- |
| 登录态过期 | 抛 `PDDSessionExpired`，服务记录“需要重新登录”，不再盲目重试 |
| 网络抖动 / 5xx / 429 | 指数退避 + 随机抖动重试（默认 3 次） |
| 发送过快 | 全局最小发送间隔（默认 1.2s） |
| 重复/违规内容 | 平台拒绝后记录风控日志，发兜底话术 |
| 长连接断开 | 指数退避重连，重连前刷 token |
| 心跳失败 | 连续 3 次失败主动断开重连 |
| 消息重复推送 | 按 `msg_id` 去重 |

## 6. 调试方法

1. 打开 DEBUG 日志：`config.yaml → app.log_level: DEBUG`；
2. 观察长连接原始帧：日志中 `收到消息(完整原文)`（默认打印，含会话明文，仅排障用）；
3. 用 Mock 复现：`python -m mock.pdd_mock --http-port 9080 --ws-port 9081`，改配置
   `pdd.base_url` / `pdd.ws_url` 指向 Mock；
4. 单测覆盖协议解析：`python -m pytest tests/test_message.py tests/test_api_client.py -q`。

## 7. 边界声明

- 本项目只使用**商家自己账号**在商家后台的正常客服能力，不爬取他人数据、不做批量注册；
- 接口字段与参数可能随平台改版失效，失效时以浏览器实际请求为准更新 `api.py`；
- 请遵守拼多多平台规则与协议，使用前自行评估第三方自动化工具的风险。
