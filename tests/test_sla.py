# -*- coding: utf-8 -*-
"""响应时效（SLA）测试。"""
from __future__ import annotations

from datetime import timedelta

from pdd_cs.engine.sla import SLATracker
from pdd_cs.utils import now_beijing


def test_first_response_and_rate():
    tracker = SLATracker(first_response_seconds=180, warn_seconds=120)
    start = now_beijing()
    tracker.on_inbound("u1", "买家", when=start)
    assert tracker.get("u1").waiting_seconds is not None
    tracker.on_outbound("u1", auto=True, when=start + timedelta(seconds=30))
    stat = tracker.get("u1")
    assert stat.first_response_seconds == 30
    snapshot = tracker.snapshot()
    assert snapshot["conversations"] == 1
    assert snapshot["auto_replies"] == 1
    assert snapshot["within_sla_rate"] == 1.0


def test_slow_response_not_within_sla():
    tracker = SLATracker(first_response_seconds=180)
    start = now_beijing()
    tracker.on_inbound("u1", when=start)
    tracker.on_outbound("u1", when=start + timedelta(seconds=240))
    assert tracker.snapshot()["within_sla_rate"] == 0.0


def test_overdue_detection():
    tracker = SLATracker(first_response_seconds=180, warn_seconds=0)
    tracker.on_inbound("u1")
    overdue = tracker.overdue(threshold_seconds=0)
    assert len(overdue) == 1
    assert overdue[0].uid == "u1"


def test_human_takeover():
    tracker = SLATracker()
    tracker.on_inbound("u1")
    tracker.pause_for_human("u1", 10)
    assert tracker.is_human_active("u1")
    # 人工接管期间不应出现在等待告警里
    assert tracker.overdue(threshold_seconds=0) == []
    tracker.resume_auto("u1")
    assert not tracker.is_human_active("u1")


def test_repeat_outbound_counts():
    tracker = SLATracker()
    start = now_beijing()
    for i in range(3):
        tracker.on_inbound("u1", when=start + timedelta(seconds=i))
        tracker.on_outbound("u1", auto=True, when=start + timedelta(seconds=i, milliseconds=500))
    assert tracker.get("u1").outbound_count == 3
    assert tracker.get("u1").auto_reply_count == 3
