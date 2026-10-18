# -*- coding: utf-8 -*-
"""尺码图片、售后转人工、商品卡片回显测试。"""
from __future__ import annotations

from pdd_cs.engine.rules import load_rules


def test_size_image_rules(service):
    config = service.config
    rules = load_rules(
        config.rules.keyword_file,
        config.rules.transfer_file,
        config.rules.replace_file,
        config.rules.filter_file,
        config.rules.goods_file,
        config.rules.blacklist_file,
    )
    women = rules.match_keyword("女士尺码")
    men = rules.match_keyword("男士尺码")
    assert women and women.reply_type == "IMAGE"
    assert "size-women" in women.reply
    assert "偏小" in women.followup
    assert men and men.reply_type == "IMAGE"
    assert "size-men" in men.reply


def test_after_sales_transfer_rule(service):
    config = service.config
    rules = load_rules(
        config.rules.keyword_file,
        config.rules.transfer_file,
        config.rules.replace_file,
        config.rules.filter_file,
        config.rules.goods_file,
        config.rules.blacklist_file,
    )
    for text in ("我要退货", "售后怎么处理", "申请维修", "退运费"):
        assert rules.match_transfer(text), text


def test_bot_goods_card_link_echo_is_not_human(service):
    service._build_workers()
    worker = service.worker("default") or service.worker("1")
    assert worker is not None
    uid = "echo-card-user"
    goods_id = "1016239261096"
    worker.store.record_outbound(
        uid=uid,
        content=f"【商品卡片 goods_id={goods_id}】\n这款保暖背心很适合您",
        action="ai",
        result="ai_reply",
        shop_id=worker.shop_id,
    )
    link = f"https://mobile.yangkeduo.com/goods.html?goods_id={goods_id}"
    assert worker._is_own_echo(uid, link) is True
