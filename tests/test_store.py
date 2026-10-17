# -*- coding: utf-8 -*-
"""SQLite 存储、统计与导出测试。"""
from __future__ import annotations

import json
from datetime import timedelta

from pdd_cs.store import Store
from pdd_cs.utils import now_beijing


def _store(tmp_path) -> Store:
    return Store(tmp_path / "test.db")


def test_record_inbound_and_outbound(tmp_path):
    store = _store(tmp_path)
    try:
        store.record_inbound(
            uid="u1", nickname="小圆", role="OTHER", context_type="text",
            content="什么时候发货", msg_id="m1",
        )
        store.record_inbound(
            uid="u1", nickname="小圆", role="OTHER", context_type="text",
            content="发什么快递", msg_id="m2",
        )
        assert store.is_duplicate_message("u1", "m1")
        assert not store.is_duplicate_message("u1", "m3")

        store.record_outbound(uid="u1", content="回复1", action="keyword", result="auto_reply")
        conv = store.get_conversation("u1")
        assert conv["inbound_count"] == 2
        assert conv["outbound_count"] == 1
        assert conv["nickname"] == "小圆"

        messages = store.get_messages("u1")
        assert len(messages) == 3
        assert messages[-1]["direction"] == "out"

        history = store.history_for_ai("u1")
        assert history[0]["role"] == "user"
        assert history[-1]["role"] == "assistant"
        assert store.recent_replies("u1") == ["回复1"]
    finally:
        store.close()


def test_human_takeover(tmp_path):
    store = _store(tmp_path)
    try:
        store.record_inbound(
            uid="u1", nickname="", role="OTHER", context_type="text", content="你好"
        )
        until = now_beijing() + timedelta(minutes=15)
        store.set_human_takeover("u1", until)
        assert store.get_human_takeover("u1") is not None
        store.expire_human_takeovers()  # 未过期不应清除
        assert store.get_human_takeover("u1") is not None

        store.set_human_takeover("u1", now_beijing() - timedelta(minutes=1))
        cleared = store.expire_human_takeovers()
        assert cleared >= 1
        assert store.get_human_takeover("u1") is None
    finally:
        store.close()


def test_count_repeat_asks(tmp_path):
    store = _store(tmp_path)
    try:
        for i in range(3):
            store.record_inbound(
                uid="u1", nickname="", role="OTHER", context_type="text",
                content="什么时候发货", msg_id=f"m{i}",
            )
        assert store.count_repeat_asks("u1", "什么时候发货") >= 3
        assert store.count_repeat_asks("u1", "完全不同的问题") == 0
    finally:
        store.close()


def test_risk_logs_and_stats(tmp_path):
    store = _store(tmp_path)
    try:
        store.record_inbound(
            uid="u1", nickname="", role="OTHER", context_type="text", content="你好"
        )
        store.record_outbound(
            uid="u1", content="回复", action="keyword", result="auto_reply"
        )
        store.log_risk("u1", "content_rewritten", "offsite:微信")
        stats = store.today_stats()
        assert stats["inbound_messages"] == 1
        assert stats["auto_replies"] == 1
        assert stats["risk_events"] == 1
        logs = store.list_risk_logs()
        assert logs[0]["risk_type"] == "content_rewritten"
        assert store.reply_result_breakdown().get("auto_reply") == 1
    finally:
        store.close()


def test_export_json(tmp_path):
    store = _store(tmp_path)
    try:
        store.record_inbound(
            uid="u1", nickname="买家", role="OTHER", context_type="text",
            content="你好", msg_id="m1",
        )
        store.record_outbound(uid="u1", content="您好～", action="keyword", result="auto_reply")
        store.log_risk("u1", "content_blocked", "promise:今天一定发")
        target = tmp_path / "export.json"
        count = store.export(target)
        assert count == 2  # 1 条进线 + 1 条回复
        data = json.loads(target.read_text(encoding="utf-8"))
        assert data["conversations"]
        assert data["messages"]
        assert data["replies"]
        assert data["risk_logs"]
        assert data["exported_at"]
    finally:
        store.close()


def test_kv(tmp_path):
    store = _store(tmp_path)
    try:
        store.set_kv("shop_name", "测试店铺")
        store.set_kv("channel_state", {"state": "connected"})
        assert store.get_kv("shop_name") == "测试店铺"
        assert store.get_kv("channel_state")["state"] == "connected"
        assert store.get_kv("missing", "default") == "default"
    finally:
        store.close()


def test_conversation_listing_order(tmp_path):
    store = _store(tmp_path)
    try:
        store.record_inbound(uid="u1", nickname="A", role="OTHER", context_type="text", content="a")
        store.record_inbound(uid="u2", nickname="B", role="OTHER", context_type="text", content="b")
        conversations = store.list_conversations()
        assert {c["uid"] for c in conversations} == {"u1", "u2"}
    finally:
        store.close()
