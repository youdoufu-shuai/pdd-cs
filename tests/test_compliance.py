# -*- coding: utf-8 -*-
"""平台规则合规层测试。"""
from __future__ import annotations

import pytest

from pdd_cs.engine.compliance import ComplianceGuard
from pdd_cs.utils import now_beijing


@pytest.fixture()
def guard(config):
    return ComplianceGuard.from_file(config.compliance, config.rules.compliance_file)


# ---------------------------------------------------------------------------
# 出站检查
# ---------------------------------------------------------------------------
def test_offsite_wechat_rewritten(guard):
    result = guard.check_outbound("可以加微信吗")
    assert any(v.category == "offsite" for v in result.violations)
    enforced = guard.enforce("可以加微信吗")
    assert "微信" not in enforced.text
    assert not enforced.blocked


def test_offsite_phone_removed(guard):
    enforced = guard.enforce("您打我电话 13800138000 就行")
    assert "13800138000" not in enforced.text


def test_offsite_cannot_bypass(guard):
    """即使改写后仍无法消除导流，也允许发送（平台内沟通），但不能带联系方式。"""
    enforced = guard.enforce("加我微信 abcdef123")
    assert "微信" not in enforced.text or "平台" in enforced.text


def test_pdd_product_link_allowed(guard):
    link = "https://mobile.yangkeduo.com/goods.html?goods_id=1016239261096"
    result = guard.check_outbound(f"这款保暖背心可以看这里：{link}")
    assert not any(v.category == "offsite" for v in result.violations)
    enforced = guard.enforce(f"这款保暖背心可以看这里：{link}")
    assert link in enforced.text


def test_external_link_still_removed(guard):
    enforced = guard.enforce("看这个 https://evil.example.com/x")
    assert "evil.example.com" not in enforced.text


def test_promise_delivery_blocked_and_fallback(guard):
    enforced = guard.enforce("放心，今天一定发！")
    assert enforced.violations, "应命中发货承诺"
    assert not enforced.blocked  # 已改用兜底话术
    assert enforced.needs_human
    assert "详情页" in enforced.text or "平台" in enforced.text


def test_promise_refund_blocked(guard):
    enforced = guard.enforce("包退包换，肯定能退")
    assert any(v.category == "promise" for v in enforced.violations)
    assert enforced.needs_human


def test_platform_claim_blocked(guard):
    result = guard.check_outbound("仅退款我说了算")
    assert result.blocked


def test_pdd_blocked_word_rewritten(guard):
    enforced = guard.enforce("详情可以点链接查看")
    assert "链接" not in enforced.text
    assert "页面" in enforced.text


def test_sensitive_word_rewritten(guard):
    enforced = guard.enforce("我让专业人员联系您")
    assert "专业人员" not in enforced.text
    assert "售后同事" in enforced.text


def test_privacy_blocked(guard):
    enforced = guard.enforce("请提供您的身份证号和银行卡")
    assert enforced.blocked


def test_absolute_words(guard):
    result = guard.check_outbound("这是全网最好的产品")
    assert any(v.category == "absolute" for v in result.violations)


def test_rude_reply(guard):
    result = guard.check_outbound("爱买不买")
    assert any(v.category == "rude" for v in result.violations)


def test_clean_reply_passes(guard):
    result = guard.check_outbound("发货时效以商品详情页公示为准哦，下单后可在订单页查看物流进度。")
    assert result.clean
    assert not result.blocked


# ---------------------------------------------------------------------------
# 转人工信号
# ---------------------------------------------------------------------------
def test_handoff_safety_first(guard):
    signal = guard.handoff_signal("孩子吃了之后过敏了")
    assert signal is not None
    assert signal.category == "safety"
    assert signal.priority == 1


def test_handoff_legal(guard):
    signal = guard.handoff_signal("我要去 12315 投诉")
    assert signal is not None
    assert signal.category == "legal"


def test_handoff_human_request(guard):
    signal = guard.handoff_signal("转人工")
    assert signal is not None
    assert signal.category == "human_request"


def test_handoff_complaint(guard):
    signal = guard.handoff_signal("我要投诉你们")
    assert signal is not None
    assert signal.category in {"complaint", "keyword"}


def test_handoff_after_sales(guard):
    signal = guard.handoff_signal("我要退货")
    assert signal is not None
    assert signal.category in {"after_sales", "keyword"}


def test_handoff_price(guard):
    signal = guard.handoff_signal("能便宜点吗")
    assert signal is not None
    assert signal.category == "price"


def test_handoff_amount(guard):
    signal = guard.handoff_signal("这个怎么处理", order_amount=200)
    assert signal is not None
    assert signal.category == "amount"


def test_handoff_emotion(guard):
    signal = guard.handoff_signal("气死了，太差了")
    assert signal is not None
    assert signal.category == "emotion"


def test_handoff_repeat(guard):
    signal = guard.handoff_signal("到底什么时候发货", repeat_count=3)
    assert signal is not None
    assert signal.category == "repeat"


def test_no_handoff_for_normal_question(guard):
    assert guard.handoff_signal("请问这个尺码怎么选") is None


# ---------------------------------------------------------------------------
# 重复话术
# ---------------------------------------------------------------------------
def test_duplicate_detected(guard):
    text = "发货时效以商品详情页公示为准哦"
    assert guard.is_duplicate(text, [text])


def test_duplicate_diversified(guard):
    text = "发货时效以商品详情页公示为准哦"
    diverse = guard.diversify(text, [text])
    assert diverse
    assert diverse != text


def test_no_duplicate_for_different_text(guard):
    assert not guard.is_duplicate("您好，请问有什么可以帮您？", ["发货时效以详情页为准"])


# ---------------------------------------------------------------------------
# 身份披露
# ---------------------------------------------------------------------------
def test_disclosure_first_reply(guard):
    text = guard.apply_disclosure("您好～", first_reply=True)
    assert "智能客服" in text
    assert "人工" in text


def test_disclosure_later_reply(guard):
    text = guard.apply_disclosure("您好～", first_reply=False)
    assert text == "您好～"

