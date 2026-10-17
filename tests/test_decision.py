# -*- coding: utf-8 -*-
"""决策链测试。"""
from __future__ import annotations

from datetime import timedelta

from pdd_cs.constants import (
    ACTION_DEFAULT,
    ACTION_FILTERED,
    ACTION_KEYWORD,
    ACTION_OFF_HOURS,
    ACTION_RISK_BLOCKED,
    ACTION_TRANSFER,
)
from pdd_cs.engine.decision import RuntimeContext
from pdd_cs.engine.rules import KeywordRule
from pdd_cs.pdd.message import ChatEvent
from pdd_cs.utils import now_beijing


def _event(text: str, **kwargs) -> ChatEvent:
    return ChatEvent(uid="u1", nickname="测试", role="OTHER", content=text, **kwargs)


def test_keyword_decision(service):
    decision = service.decision.decide(_event("什么时候发货"))
    assert decision.action == ACTION_KEYWORD
    assert decision.should_reply
    assert "发货" in decision.content
    assert decision.violations == []


def test_transfer_beats_keyword(service):
    decision = service.decision.decide(_event("我要投诉，然后问什么时候发货"))
    assert decision.action == ACTION_TRANSFER
    assert decision.handoff is not None


def test_filtered_decision(service):
    decision = service.decision.decide(_event("可以刷单吗"))
    assert decision.action == ACTION_FILTERED


def test_risk_blocked(service):
    now = now_beijing()
    ctx = RuntimeContext(session_reply_times=[now - timedelta(seconds=10)] * 6)
    decision = service.decision.decide(_event("你好"), ctx, now=now)
    assert decision.action == ACTION_RISK_BLOCKED
    assert decision.risk["scope"] == "session"


def test_off_hours(service):
    shop_config = service.decision.config
    shop_config.business_hours.enabled = True
    shop_config.business_hours.start = "08:00"
    shop_config.business_hours.end = "22:00"
    decision = service.decision.decide(
        _event("你好"), RuntimeContext(), now=now_beijing().replace(hour=3, minute=0)
    )
    assert decision.action == ACTION_OFF_HOURS
    assert decision.should_reply


def test_default_reply(service):
    decision = service.decision.decide(_event("今天天气不错"))
    assert decision.action == ACTION_DEFAULT
    assert decision.content


def test_no_match_transfer(service):
    service.decision.config.behavior.default_reply = ""
    decision = service.decision.decide(_event("今天天气不错"))
    assert decision.action == ACTION_TRANSFER


def test_promise_keyword_falls_back_to_human(service):
    service.decision.rules.keywords.insert(
        0,
        KeywordRule(
            id="bad",
            patterns=["催发货"],
            reply="放心，今天一定发，包退",
            priority=1,
        ),
    )
    decision = service.decision.decide(_event("催发货"))
    assert decision.action in {ACTION_TRANSFER, ACTION_DEFAULT}
    assert decision.handoff is not None or decision.content
    if decision.content:
        assert "今天一定发" not in decision.content


def test_human_takeover(service):
    service._activate_human_takeover  # noqa: B018 - 仅确认方法存在
    import asyncio

    asyncio.run(service._activate_human_takeover("u1", reason="测试"))
    assert service.sla.is_human_active("u1")
