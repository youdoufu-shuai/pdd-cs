# -*- coding: utf-8 -*-
"""端到端测试：Mock 拼多多服务 + 完整服务编排。

覆盖：
    1. 长连接收到买家消息 → 关键词回复 → 通过 HTTP 发出；
    2. 投诉消息 → 转人工（调用 move_conversation）；
    3. 人工客服介入 → 自动回复暂停（人工接管）；
    4. 所有发出的内容都通过平台规则合规检查。
"""
from __future__ import annotations

import asyncio
import time
from typing import Callable

from mock.pdd_mock import MockPDDServer, make_push
from pdd_cs.engine.service import PDDAutoReplyService
from pdd_cs.utils import now_beijing


def _wait_until(predicate: Callable[[], bool], timeout: float = 12.0, interval: float = 0.1) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_end_to_end(config):
    asyncio.run(_scenario(config))


async def _scenario(config) -> None:
    mock = await MockPDDServer().start()
    config.pdd.base_url = mock.base_url
    config.pdd.ws_url = mock.ws_url
    config.llm.enabled = False

    service = PDDAutoReplyService(config)
    try:
        await service.start()
        assert await asyncio.to_thread(
            _wait_until, lambda: service.channel is not None and service.channel.connected, 12
        ), "长连接未建立"

        # ---- 1) 普通咨询：关键词自动回复 ----
        await mock.push_text("什么时候发货？", uid="buyer-1", msg_id="m-1")
        assert await asyncio.to_thread(
            _wait_until, lambda: len(mock.state.sent) >= 1, 12
        ), "没有发出回复"
        first = mock.state.sent[0]
        assert first["uid"] == "buyer-1"
        assert "发货" in first["content"] or "详情页" in first["content"]
        # 合规：不得出现承诺 / 导流
        assert "一定发" not in first["content"]
        assert "微信" not in first["content"]

        # ---- 2) 投诉：转人工 ----
        await mock.push_text("我要投诉，收到的货有问题", uid="buyer-2", msg_id="m-2")
        assert await asyncio.to_thread(
            _wait_until, lambda: len(mock.state.transfers) >= 1, 12
        ), "没有执行转人工"
        transfer = mock.state.transfers[0]
        assert transfer["uid"] == "buyer-2"
        assert transfer["cs_uid"] in {"1001", "1002"}
        # 转人工前应有一条提示消息
        assert any(item["uid"] == "buyer-2" for item in mock.state.sent)
        assert any(
            conv.get("last_result") == "transfer"
            for conv in service.store.list_conversations(10)
        )

        # ---- 3) 人工接管：人工回复后停止自动回复 ----
        await mock.push_raw(
            make_push(
                "您好，我是人工客服，我来为您处理",
                uid="buyer-3",
                from_role="mall_cs",
                msg_id="m-3",
            )
        )
        await asyncio.sleep(0.8)
        assert service.store.get_human_takeover("buyer-3") is not None, "未记录人工接管"

        before = len(mock.state.sent)
        await mock.push_text("还在吗？", uid="buyer-3", msg_id="m-4")
        await asyncio.sleep(2.5)
        assert len(mock.state.sent) == before, "人工接管期间不应自动回复"

        # ---- 4) 合规：承诺类关键词回复被替换为兜底并转人工 ----
        from pdd_cs.engine.rules import KeywordRule

        service.decision.rules.keywords.insert(
            0,
            KeywordRule(
                id="bad-promise",
                patterns=["催一下"],
                reply="放心，今天一定发，包退",
                priority=1,
            ),
        )
        await mock.push_text("催一下发货", uid="buyer-4", msg_id="m-5")
        assert await asyncio.to_thread(
            _wait_until,
            lambda: any(
                item["uid"] == "buyer-4" and "一定发" not in item["content"]
                for item in mock.state.sent
            ),
            12,
        ), "承诺类话术没有被拦截"
        assert not any("一定发" in item["content"] for item in mock.state.sent)

        # ---- 5) 统计 ----
        stats = service.store.today_stats()
        assert stats["inbound_messages"] >= 4
        assert stats["conversations"] >= 3
        status = service.status()
        assert status["channel"]["connected"] is True
        assert status["sla"]["conversations"] >= 3
    finally:
        await service.stop()
        await mock.stop()
