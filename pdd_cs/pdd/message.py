# -*- coding: utf-8 -*-
"""拼多多客服消息解析。

拼多多长连接推送的是 JSON 文本，主要形态：

    {"response": "push", "message": {
        "type": 0, "sub_type": 0,
        "content": "什么时候发货",
        "msg_id": "...", "time": 1690000000000,
        "nickname": "买家昵称",
        "from": {"role": "user", "uid": 123},
        "to":   {"role": "mall_cs", "uid": 456}
    }}

    {"response": "push", "message": {"type": 1, ...}}         图片
    {"response": "push", "message": {"type": 24, ...}}        转接
    {"response": "auth", ...}                                 鉴权回执
    {"response": "mall_system_msg", ...}                      系统消息

解析目标：把一条原始报文变成统一的 `ChatEvent`，供决策引擎使用。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from ..constants import (
    MSG_TYPE_EMOTION,
    MSG_TYPE_GOODS_SPEC,
    MSG_TYPE_IMAGE,
    MSG_TYPE_TEXT,
    MSG_TYPE_TRANSFER,
    MSG_TYPE_VIDEO,
    MSG_TYPE_WITHDRAW,
    ROLE_OTHER,
    ROLE_SELF,
    ROLE_SYSTEM,
    SUB_TYPE_GOODS_INQUIRY,
    SUB_TYPE_ORDER_INFO,
)
from ..utils import normalize_text, ts_to_beijing

logger = logging.getLogger("pdd_cs.message")

# 上下文类型（与决策引擎、日志展示对应）
CTX_TEXT = "text"
CTX_IMAGE = "image"
CTX_VIDEO = "video"
CTX_EMOTION = "emotion"
CTX_GOODS_INQUIRY = "goods_inquiry"
CTX_GOODS_SPEC = "goods_spec"
CTX_ORDER_INFO = "order_info"
CTX_TRANSFER = "transfer"
CTX_WITHDRAW = "withdraw"
CTX_SYSTEM = "system"
CTX_AUTH = "auth"
CTX_UNKNOWN = "unknown"


@dataclass
class ChatEvent:
    """一条解析后的客服消息。"""

    uid: str = ""                      # 买家 uid（会话标识）
    nickname: str = ""                 # 买家昵称
    role: str = ROLE_OTHER             # OTHER=买家 / SELF=本店客服 / SYSTEM=系统
    msg_type: int = MSG_TYPE_TEXT      # 拼多多 message.type
    context_type: str = CTX_TEXT       # 语义类型
    content: str = ""                  # 文本内容（结构化消息会转成可读文本）
    raw_content: Any = None            # 原始 content
    msg_id: str = ""                   # 平台消息 ID
    timestamp: float = 0.0             # 平台时间戳（秒或毫秒）
    order_info: dict[str, Any] = field(default_factory=dict)
    goods_info: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    is_auto: bool = False              # 平台标记的自动消息（如自动回复）

    @property
    def time_text(self) -> str:
        return ts_to_beijing(self.timestamp).strftime("%Y-%m-%d %H:%M:%S")

    @property
    def dedupe_key(self) -> str:
        """去重键：优先平台 msg_id，其次会话 + 内容 + 时间。"""
        if self.msg_id:
            return f"id:{self.msg_id}"
        return f"k:{self.uid}:{self.role}:{self.content[:80]}:{int(self.timestamp)}"

    def display_text(self) -> str:
        """用于日志 / 面板展示的一行摘要。"""
        prefix = {"OTHER": "买家", "SELF": "客服", "SYSTEM": "系统"}.get(self.role, self.role)
        body = self.content.replace("\n", " ")[:120]
        if self.context_type in {CTX_IMAGE, CTX_VIDEO, CTX_EMOTION} and not body:
            body = f"[{self.context_type}]"
        return f"{prefix}({self.uid}): {body}"


def _safe_get(data: Any, *keys: str, default: Any = None) -> Any:
    node = data
    for key in keys:
        if not isinstance(node, dict):
            return default
        node = node.get(key)
        if node is None:
            return default
    return node


def _pick(source: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if isinstance(source, dict) and source.get(key) is not None:
            return source[key]
    return None


def _merge_info(payload: dict[str, Any]) -> dict[str, Any]:
    """合并 message.info 与 message.info.data（不同消息类型层级不一致）。"""
    info = _safe_get(payload, "message", "info", default={})
    info = info if isinstance(info, dict) else {}
    data = info.get("data") if isinstance(info.get("data"), dict) else {}
    return {**info, **data}


def _extract_goods(payload: dict[str, Any]) -> dict[str, Any]:
    info = _merge_info(payload)
    return {
        "goods_id": _pick(info, "goods_id", "goodsID"),
        "goods_name": _pick(info, "goods_name", "goodsName"),
        "price": _pick(info, "goods_price", "goodsPrice"),
        "spec": _pick(info, "goods_spec", "spec"),
        "thumb": _pick(info, "thumb_url", "goodsThumbUrl"),
    }


def _extract_order(payload: dict[str, Any]) -> dict[str, Any]:
    info = _merge_info(payload)
    return {
        "order_id": _pick(info, "order_sequence_no", "orderSequenceNo", "order_id"),
        "goods_id": _pick(info, "goods_id", "goodsID"),
        "goods_name": _pick(info, "goods_name", "goodsName"),
        "spec": _pick(info, "goods_spec", "spec"),
        "after_sales_status": _pick(info, "after_sales_status", "afterSalesStatus"),
        "after_sales_type": _pick(info, "after_sales_type", "afterSalesType"),
        "amount": _pick(info, "order_amount", "orderAmount", "amount"),
    }


def _goods_text(goods: dict[str, Any]) -> str:
    name = goods.get("goods_name") or ""
    spec = goods.get("spec") or ""
    parts = [p for p in (name, spec) if p]
    return f"[商品咨询] {' '.join(parts)}".strip()


def _order_text(order: dict[str, Any]) -> str:
    name = order.get("goods_name") or ""
    return f"[订单咨询] 订单号{order.get('order_id') or ''} {name}".strip()


def parse_message(payload: dict[str, Any]) -> Optional[ChatEvent]:
    """把一条原始报文解析成 ChatEvent；无法识别时返回 None。"""
    if not isinstance(payload, dict):
        return None
    response = payload.get("response")
    if response == "auth":
        return None
    if response == "mall_system_msg":
        return ChatEvent(
            context_type=CTX_SYSTEM,
            role=ROLE_SYSTEM,
            content="[系统消息]",
            raw=payload,
        )
    if response != "push":
        return None

    message = payload.get("message") or {}
    if not isinstance(message, dict):
        return None

    from_role = _safe_get(message, "from", "role") or ""
    from_uid = _safe_get(message, "from", "uid")
    to_uid = _safe_get(message, "to", "uid")
    role = ROLE_OTHER
    if from_role in {"mall_cs", "cs", "shop"}:
        role = ROLE_SELF
    elif from_role in {"system", "mall_system"}:
        role = ROLE_SYSTEM

    # 买家 uid：买家消息取 from，客服消息取 to
    uid = from_uid if role == ROLE_OTHER else to_uid
    uid_text = str(uid) if uid is not None else ""

    msg_type = message.get("type")
    msg_type = int(msg_type) if isinstance(msg_type, (int, float, str)) and str(msg_type).lstrip("-").isdigit() else MSG_TYPE_TEXT
    sub_type = _safe_get(message, "sub_type")

    event = ChatEvent(
        uid=uid_text,
        nickname=str(message.get("nickname") or ""),
        role=role,
        msg_type=msg_type,
        msg_id=str(message.get("msg_id") or ""),
        timestamp=float(message.get("time") or 0) or 0.0,
        raw=payload,
        is_auto=bool(message.get("is_aut") or message.get("is_auto")),
    )

    if msg_type == MSG_TYPE_TEXT:
        info = _merge_info(payload)
        # 拼多多文本消息的 sub_type 并不严格：普通文本也可能带 sub_type=0。
        # 这里以「info 里确实有商品/订单字段」为准，避免把普通文本误判成商品卡片。
        has_goods = bool(_pick(info, "goods_id", "goodsID", "goods_name", "goodsName"))
        has_order = bool(_pick(info, "order_sequence_no", "orderSequenceNo", "order_id"))
        if sub_type == SUB_TYPE_ORDER_INFO and has_order:
            order = _extract_order(payload)
            event.context_type = CTX_ORDER_INFO
            event.order_info = order
            event.content = _order_text(order)
            event.raw_content = order
        elif sub_type == SUB_TYPE_GOODS_INQUIRY and has_goods:
            goods = _extract_goods(payload)
            event.context_type = CTX_GOODS_INQUIRY
            event.goods_info = goods
            event.content = _goods_text(goods)
            event.raw_content = goods
        else:
            event.context_type = CTX_TEXT
            event.raw_content = message.get("content")
            event.content = normalize_text(str(message.get("content") or ""))
    elif msg_type == MSG_TYPE_IMAGE:
        event.context_type = CTX_IMAGE
        event.raw_content = message.get("content")
        event.content = "[买家发送了一张图片]"
    elif msg_type == MSG_TYPE_VIDEO:
        event.context_type = CTX_VIDEO
        event.raw_content = message.get("content")
        event.content = "[买家发送了一段视频]"
    elif msg_type == MSG_TYPE_EMOTION:
        event.context_type = CTX_EMOTION
        event.content = str(_safe_get(payload, "info", "description") or "[表情]")
    elif msg_type == MSG_TYPE_WITHDRAW:
        event.context_type = CTX_WITHDRAW
        event.content = str(_safe_get(payload, "info", "withdraw_hint") or "[撤回了一条消息]")
    elif msg_type == MSG_TYPE_GOODS_SPEC:
        goods = _extract_goods(payload)
        event.context_type = CTX_GOODS_SPEC
        event.goods_info = goods
        event.content = _goods_text(goods)
        event.raw_content = goods
    elif msg_type == MSG_TYPE_TRANSFER:
        event.context_type = CTX_TRANSFER
        event.content = "[会话转接]"
    else:
        event.context_type = CTX_UNKNOWN
        event.content = f"[暂不支持的消息类型 {msg_type}]"

    if not event.uid:
        return None
    return event


def parse_ws_frame(raw: Any) -> list[ChatEvent]:
    """解析一帧长连接数据，可能包含一条或多条消息。

    Args:
        raw: 原始数据（bytes / str / dict / list）。
    """
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return []
    payload: Any = raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            logger.info("收到非 JSON 长连接数据（已忽略）: %s", text[:200])
            return []
    if isinstance(payload, list):
        events: list[ChatEvent] = []
        for item in payload:
            event = parse_message(item) if isinstance(item, dict) else None
            if event:
                events.append(event)
        return events
    if isinstance(payload, dict):
        # 有些推送是 {"messages": [...]} 的批量结构
        batch = payload.get("messages")
        if isinstance(batch, list):
            events = []
            for item in batch:
                if isinstance(item, dict) and "response" not in item:
                    item = {"response": "push", "message": item}
                event = parse_message(item) if isinstance(item, dict) else None
                if event:
                    events.append(event)
            if events:
                return events
        event = parse_message(payload)
        return [event] if event else []
    return []


__all__ = [
    "ChatEvent",
    "parse_message",
    "parse_ws_frame",
    "CTX_TEXT",
    "CTX_IMAGE",
    "CTX_GOODS_INQUIRY",
    "CTX_GOODS_SPEC",
    "CTX_ORDER_INFO",
    "CTX_SYSTEM",
    "CTX_WITHDRAW",
]
