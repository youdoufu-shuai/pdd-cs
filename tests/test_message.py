# -*- coding: utf-8 -*-
"""消息解析测试。"""
from __future__ import annotations

from pdd_cs.constants import ROLE_OTHER, ROLE_SELF
from pdd_cs.pdd.message import (
    CTX_GOODS_INQUIRY,
    CTX_IMAGE,
    CTX_ORDER_INFO,
    CTX_TEXT,
    ChatEvent,
    parse_message,
    parse_ws_frame,
)


def _push(**message_overrides) -> dict:
    message = {
        "type": 0,
        "sub_type": 0,
        "content": "什么时候发货",
        "msg_id": "m-1",
        "time": 1700000000000,
        "nickname": "买家小A",
        "from": {"role": "user", "uid": 12345},
        "to": {"role": "mall_cs", "uid": 999},
    }
    message.update(message_overrides)
    return {"response": "push", "message": message}


def test_parse_text_message():
    event = parse_message(_push())
    assert event is not None
    assert event.uid == "12345"
    assert event.nickname == "买家小A"
    assert event.role == ROLE_OTHER
    assert event.context_type == CTX_TEXT
    assert event.content == "什么时候发货"
    assert event.msg_id == "m-1"
    assert event.dedupe_key == "id:m-1"


def test_parse_self_message():
    payload = _push()
    payload["message"]["from"] = {"role": "mall_cs", "uid": 999}
    payload["message"]["to"] = {"role": "user", "uid": 12345}
    event = parse_message(payload)
    assert event is not None
    assert event.role == ROLE_SELF
    assert event.uid == "12345"


def test_parse_image_message():
    event = parse_message(_push(type=1, content="https://img.example/1.jpg"))
    assert event is not None
    assert event.context_type == CTX_IMAGE
    assert "图片" in event.content


def test_parse_order_info():
    payload = _push(
        sub_type=1,
        content="",
        info={"order_sequence_no": "240101-123", "goods_name": "测试商品", "order_amount": 59.9},
    )
    event = parse_message(payload)
    assert event is not None
    assert event.context_type == CTX_ORDER_INFO
    assert event.order_info["order_id"] == "240101-123"
    assert "订单咨询" in event.content


def test_parse_goods_inquiry():
    payload = _push(
        sub_type=0,
        content="",
        info={"goods_id": "1001", "goods_name": "夏季短袖", "goods_price": "39.9"},
    )
    event = parse_message(payload)
    assert event is not None
    assert event.context_type == CTX_GOODS_INQUIRY
    assert event.goods_info["goods_id"] == "1001"


def test_parse_batch_frame():
    frame = {"messages": [_push()["message"], _push(msg_id="m-2")["message"]]}
    events = parse_ws_frame(frame)
    assert len(events) == 2
    assert events[0].msg_id == "m-1"
    assert events[1].msg_id == "m-2"


def test_parse_auth_frame_returns_empty():
    assert parse_ws_frame({"response": "auth", "uid": 1}) == []


def test_parse_invalid_json_returns_empty():
    assert parse_ws_frame("not-json") == []


def test_display_text():
    event = ChatEvent(uid="1", role="OTHER", content="你好啊")
    assert "买家" in event.display_text()

