# -*- coding: utf-8 -*-
"""AI 商品目录与商品链接测试。"""
from __future__ import annotations

from pdd_cs.engine.ai import AIReplyGenerator


def test_ai_keeps_pdd_product_link(config, tmp_path):
    ai = AIReplyGenerator(config)
    link = "https://mobile.yangkeduo.com/goods.html?goods_id=1016239261096"
    cleaned = ai._sanitize(f"这款保暖背心很合适，可以看这里：{link}")
    assert link in cleaned


def test_ai_removes_external_link(config):
    ai = AIReplyGenerator(config)
    cleaned = ai._sanitize("看这个 https://evil.example.com/x")
    assert "evil.example.com" not in cleaned


def test_ai_includes_product_catalog_for_product_question(config, tmp_path):
    config.knowledge.dir = tmp_path / "knowledge"
    config.knowledge.dir.mkdir(parents=True, exist_ok=True)
    (config.knowledge.dir / "product-catalog.md").write_text(
        "# 商品目录\n\n## 商品：保暖背心\n- 商品链接：https://mobile.yangkeduo.com/goods.html?goods_id=1016239261096\n",
        encoding="utf-8",
    )
    ai = AIReplyGenerator(config)
    prompt = ai._build_system_prompt([], handoff_hint=False, include_catalog=True)
    assert "店铺商品目录" in prompt
    assert "1016239261096" in prompt
