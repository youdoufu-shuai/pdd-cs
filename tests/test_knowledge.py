# -*- coding: utf-8 -*-
"""知识库检索测试。"""
from __future__ import annotations

from pathlib import Path

from pdd_cs.engine.knowledge import KnowledgeBase


def _make_kb(tmp_path: Path) -> KnowledgeBase:
    (tmp_path / "policy.md").write_text(
        "# 店铺政策\n\n## 发货时效\n\n本店默认 48 小时内发货，以商品详情页公示为准。\n\n## 退换货\n\n支持 7 天无理由退换货，售后按平台页面流程处理。\n",
        encoding="utf-8",
    )
    (tmp_path / "faq.md").write_text(
        "# 商品FAQ\n\n## 尺码\n\n尺码建议对照详情页尺码表选择。\n",
        encoding="utf-8",
    )
    return KnowledgeBase(tmp_path)


def test_search_shipping(tmp_path: Path):
    kb = _make_kb(tmp_path)
    results = kb.search("什么时候发货", top_k=3, min_score=0.0)
    assert results
    assert any("发货" in r.content for r in results)


def test_search_returns_relevant_section(tmp_path: Path):
    kb = _make_kb(tmp_path)
    results = kb.search("尺码怎么选", top_k=3, min_score=0.0)
    assert results
    assert "尺码" in results[0].content


def test_search_empty(tmp_path: Path):
    kb = _make_kb(tmp_path)
    assert kb.search("完全无关的内容xyz", top_k=3, min_score=50.0) == []


def test_format_for_prompt(tmp_path: Path):
    kb = _make_kb(tmp_path)
    results = kb.search("发货", top_k=2, min_score=0.0)
    text = kb.format_for_prompt(results)
    assert "【" in text and "】" in text


def test_reload_on_change(tmp_path: Path):
    kb = _make_kb(tmp_path)
    before = kb.stats()["snippets"]
    (tmp_path / "new.md").write_text("# 新文档\n\n## 新章节\n\n新内容。\n", encoding="utf-8")
    assert kb.reload_if_changed()
    assert kb.stats()["snippets"] > before


def test_project_knowledge_loads():
    from pdd_cs.config import PROJECT_ROOT, Config

    config = Config.load(PROJECT_ROOT / "config" / "config.example.yaml")
    kb = KnowledgeBase(config.knowledge.dir)
    assert kb.stats()["files"] >= 3
    results = kb.search("退款怎么处理", top_k=3, min_score=0.0)
    assert results
