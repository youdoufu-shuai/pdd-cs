# -*- coding: utf-8 -*-
"""运营洞察 / 待处理队列 / 会话搜索 / 订单上下文测试。"""
from __future__ import annotations

import asyncio

from pdd_cs.pdd.message import ChatEvent, CTX_ORDER_INFO


def _seed(service) -> None:
    store = service.store
    # 转人工（带原因）
    store.record_inbound(uid="i-1", nickname="小圆", role="OTHER", context_type="text", content="我要投诉")
    store.record_outbound(
        uid="i-1", content="[转人工] 投诉/差评风险", action="transfer",
        result="transfer", reason="complaint: 投诉/差评风险（投诉）",
    )
    # 关键词命中
    store.record_inbound(uid="i-2", nickname="阿泽", role="OTHER", context_type="text", content="什么时候发货")
    store.record_outbound(
        uid="i-2", content="发货时效以商品详情页为准", action="keyword",
        result="auto_reply", rule_id="ship_time", reason="命中关键词规则 ship_time",
    )
    # 知识缺口（no_match）
    store.record_inbound(uid="i-3", nickname="大熊", role="OTHER", context_type="text", content="你们支持以旧换新吗")
    store.record_outbound(
        uid="i-3", content="", action="no_match", result="no_match",
        status="sent", question="你们支持以旧换新吗",
    )
    # 待处理：客户还在等
    store.record_inbound(uid="i-4", nickname="晴天", role="OTHER", context_type="text", content="在吗？")


def test_insights(service):
    _seed(service)
    data = service.insights()
    assert data["hourly"], "按小时进线统计不应为空"
    assert any(item["reason"].startswith("complaint") for item in data["transfer_reasons"])
    assert any(item["rule_id"] == "ship_time" for item in data["top_rules"])
    assert any("以旧换新" in item["question"] for item in data["knowledge_gaps"])
    assert data["shop_id"] == "all"

    per_shop = service.insights(shop_id="default")
    assert per_shop["shop_id"] == "default"
    assert per_shop["transfer_reasons"]


def test_pending_queue(service):
    _seed(service)
    pending = service.pending()
    uids = {item["uid"] for item in pending}
    assert "i-4" in uids, f"等待回复的会话应在待处理队列: {uids}"
    item = next(i for i in pending if i["uid"] == "i-4")
    assert item["waiting_seconds"] is not None
    assert item["shop_name"] == "默认店铺"

    assert service.status()["pending_count"] >= 1


def test_search(service):
    _seed(service)
    assert service.search("小圆")
    assert service.search("以旧换新")
    assert service.search("不存在的关键词xyz") == []


def test_order_context_persisted(service):
    worker = service.default_worker
    order_event = ChatEvent(
        uid="order-buyer",
        nickname="下单客户",
        role="OTHER",
        context_type=CTX_ORDER_INFO,
        content="[订单咨询] 订单号 240101-123 测试商品",
        order_info={"order_id": "240101-123", "goods_name": "测试商品", "amount": 99.5},
        msg_id="order-1",
    )
    asyncio.run(worker.handle_events([order_event]))
    conv = service.store.get_conversation("order-buyer", shop_id="default")
    assert conv is not None
    assert conv["last_order"]["order_id"] == "240101-123"

    # 后续消息的决策上下文里能带上历史订单
    follow_up = ChatEvent(uid="order-buyer", nickname="下单客户", role="OTHER",
                          content="我的订单到哪了", context_type="text")
    ctx = worker._build_context(follow_up)  # noqa: SLF001
    assert ctx.last_order["order_id"] == "240101-123"
    assert ctx.last_order["goods_name"] == "测试商品"
