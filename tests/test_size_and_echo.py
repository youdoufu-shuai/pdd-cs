# -*- coding: utf-8 -*-
"""尺码图片、售后转人工、商品卡片回显测试。"""
from __future__ import annotations

from pdd_cs.engine.rules import load_rules


def _rules_for(config, keyword_file=None):
    return load_rules(
        keyword_file or config.rules.keyword_file,
        config.rules.transfer_file,
        config.rules.replace_file,
        config.rules.filter_file,
        config.rules.goods_file,
        config.rules.blacklist_file,
    )


def test_default_size_rules_do_not_use_store_data(service):
    rules = _rules_for(service.config)
    for query in ("女士尺码", "男士尺码"):
        rule = rules.match_keyword(query)
        assert rule and rule.reply_type == "TEXT"
        assert "商品详情页" in rule.reply
        assert "https://" not in rule.reply


def test_size_image_rules(service, tmp_path):
    keyword_file = tmp_path / "synthetic-size-keywords.yaml"
    keyword_file.write_text(
        "- id: sample_size\n"
        "  keywords: 示例[and]尺码\n"
        "  reply_type: image\n"
        "  reply: https://example.invalid/size-chart.png\n"
        "  followup: 示例尺码仅供测试。\n",
        encoding="utf-8",
    )
    rule = _rules_for(service.config, keyword_file).match_keyword("示例尺码")
    assert rule and rule.reply_type == "IMAGE"
    assert rule.reply == "https://example.invalid/size-chart.png"
    assert rule.followup == "示例尺码仅供测试。"


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
    goods_id = "0000000000123"
    worker.store.record_outbound(
        uid=uid,
        content=f"【商品卡片 goods_id={goods_id}】\n这款保暖背心很适合您",
        action="ai",
        result="ai_reply",
        shop_id=worker.shop_id,
    )
    link = f"https://mobile.yangkeduo.com/goods.html?goods_id={goods_id}"
    assert worker._is_own_echo(uid, link) is True
