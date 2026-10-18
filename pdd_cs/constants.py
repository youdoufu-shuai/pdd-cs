# -*- coding: utf-8 -*-
"""全局常量：拼多多接口地址、消息类型、动作枚举、决策结果枚举。

所有拼多多接口地址集中在这里，便于平台改版时单点修改；也便于用 Mock 服务
（mock/pdd_mock.py）整体替换成 127.0.0.1 做端到端测试。
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# 拼多多商家后台接口（mms.pinduoduo.com）
# ---------------------------------------------------------------------------

#: 商家后台主域（可被配置覆盖，用于 Mock / 私有网关）
PDD_BASE_URL: str = "https://mms.pinduoduo.com"

#: 客服消息长连接基址（拼多多固定地址）
PDD_WS_URL: str = "wss://m-ws.pinduoduo.com/"

#: 长连接握手参数里的协议版本
PDD_WS_API_VERSION: str = "202506091557"

#: 获取长连接 access_token
PDD_TOKEN_PATH: str = "/chats/getToken"

#: 发送文本 / 图片消息
PDD_SEND_MESSAGE_PATH: str = "/plateau/chat/send_message"

#: 会话转人工
PDD_MOVE_CONVERSATION_PATH: str = "/plateau/chat/move_conversation"

#: 可分配的人工客服列表
PDD_ASSIGN_CS_LIST_PATH: str = "/latitude/assign/getAssignCsList"

#: 发送商品卡片（依赖 anti-content 签名）
PDD_GOODS_CARD_PATH: str = "/plateau/message/send/mallGoodsCard"

#: 客服在线 / 离线状态
PDD_CS_STATUS_PATH: str = "/plateau/chat/set_csstatus"

#: 当前登录客服信息
PDD_USER_INFO_PATH: str = "/janus/api/new/userinfo"
PDD_MALL_INFO_PATH: str = "/janus/api/userinfo"

#: 店铺信息
PDD_SHOP_INFO_PATH: str = "/earth/api/merchant/queryMerchantInfoByMallId"

#: 最近会话列表
PDD_CONVERSATIONS_PATH: str = "/plateau/chat/latest_conversations"

#: 单会话历史消息
PDD_CHAT_HISTORY_PATH: str = "/plateau/chat/list"

# ---------------------------------------------------------------------------
# 拼多多业务错误码 / 文案
# ---------------------------------------------------------------------------

#: 会话已过期（需要重新登录 / 刷新 Cookie）
PDD_SESSION_EXPIRED_CODE: int = 43001
PDD_SESSION_EXPIRED_KEYWORD: str = "会话已过期"

#: 发送文本被业务拒绝（例如内容违规、频率限制）
PDD_SEND_TEXT_ERROR_CODE: int = 10002

#: 客服在线状态取值
CS_STATUS_ONLINE: int = 1
CS_STATUS_OFFLINE: int = 3

# ---------------------------------------------------------------------------
# 消息类型（拼多多 WS 报文 message.type）
# ---------------------------------------------------------------------------
MSG_TYPE_TEXT: int = 0
MSG_TYPE_IMAGE: int = 1
MSG_TYPE_EMOTION: int = 5
MSG_TYPE_VIDEO: int = 14
MSG_TYPE_TRANSFER: int = 24
MSG_TYPE_GOODS_SPEC: int = 64
MSG_TYPE_WITHDRAW: int = 1002

# 文本子类型
SUB_TYPE_GOODS_INQUIRY: int = 0
SUB_TYPE_ORDER_INFO: int = 1

# ---------------------------------------------------------------------------
# 消息角色
# ---------------------------------------------------------------------------
ROLE_SELF: str = "SELF"        # 本店（客服 / 机器人）
ROLE_OTHER: str = "OTHER"      # 买家
ROLE_SYSTEM: str = "SYSTEM"    # 系统消息

# ---------------------------------------------------------------------------
# 回复类型
# ---------------------------------------------------------------------------
REPLY_TEXT: str = "TEXT"
REPLY_IMAGE: str = "IMAGE"
REPLY_TRANSFER: str = "TRANSFER"
REPLY_NO_REPLY: str = "NO_REPLY"

# ---------------------------------------------------------------------------
# 决策动作（与文档 docs/compliance-matrix.md 一一对应）
# ---------------------------------------------------------------------------
ACTION_BLACKLISTED: str = "blacklisted"      # 命中黑名单：不回复
ACTION_FILTERED: str = "filtered"            # 命中过滤规则：不自动回复
ACTION_OFF_HOURS: str = "off_hours"          # 非营业时间
ACTION_RISK_BLOCKED: str = "risk_blocked"    # 触发频率风控：暂停
ACTION_TRANSFER: str = "transfer"            # 转人工
ACTION_KEYWORD: str = "keyword"              # 关键词自动回复
ACTION_GOODS: str = "goods_specific"         # 商品专属回复
ACTION_AI: str = "ai"                        # AI 生成回复
ACTION_DEFAULT: str = "default"              # 默认回复
ACTION_NO_MATCH: str = "no_match"            # 无可用回复：不发送

# ---------------------------------------------------------------------------
# 消息处理结果（用于监控面板与风控日志）
# ---------------------------------------------------------------------------
RESULT_AUTO_REPLY: str = "auto_reply"
RESULT_AI_REPLY: str = "ai_reply"
RESULT_TRANSFER: str = "transfer"
RESULT_FILTERED: str = "filtered"
RESULT_BLACKLISTED: str = "blacklisted"
RESULT_NON_BUSINESS_HOURS: str = "non_business_hours"
RESULT_RISK_PAUSED: str = "risk_paused"
RESULT_NO_MATCH: str = "no_match"
RESULT_HUMAN_TAKEOVER: str = "human_takeover"
RESULT_FAILED: str = "failed"

# ---------------------------------------------------------------------------
# 风控类型
# ---------------------------------------------------------------------------
RISK_FREQUENCY_LIMIT: str = "frequency_limit"
RISK_DUPLICATE_REPLY: str = "duplicate_reply"
RISK_OFFSITE: str = "offsite_divert"
RISK_PROMISE: str = "risky_promise"
RISK_SENSITIVE: str = "sensitive_word"
RISK_EMOTION: str = "negative_emotion"
RISK_SLA: str = "sla_timeout"
