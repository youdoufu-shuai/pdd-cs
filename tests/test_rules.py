# -*- coding: utf-8 -*-
"""规则匹配测试。"""
from __future__ import annotations

from pathlib import Path

from pdd_cs.engine.rules import load_rules


def _write(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def test_keyword_or_and_and_semantics(tmp_path: Path):
    kw = _write(
        tmp_path,
        "kw.yaml",
        """
- id: a
  keywords: 发货|几天发
  match: fuzzy
  reply: 回复A
  priority: 10
- id: b
  keywords: 发货[and]几天
  match: fuzzy
  reply: 回复B
  priority: 1
""",
    )
    rules = load_rules(kw, tmp_path / "none.yaml", tmp_path / "none2.yaml", tmp_path / "none3.yaml", tmp_path / "none4.yaml")
    assert rules.match_keyword("什么时候发货").id == "a"
    assert rules.match_keyword("发货大概几天").id == "b"  # 优先级更高
    assert rules.match_keyword("无关内容") is None


def test_exact_and_regex(tmp_path: Path):
    kw = _write(
        tmp_path,
        "kw.yaml",
        """
- id: exact
  keywords: 在吗
  match: exact
  reply: 在的
- id: regex
  keywords: "\\\\d{6}"
  match: regex
  reply: 收到验证码
  priority: 5
""",
    )
    rules = load_rules(kw, tmp_path / "a.yaml", tmp_path / "b.yaml", tmp_path / "c.yaml", tmp_path / "d.yaml")
    assert rules.match_keyword("在吗").id == "exact"
    assert rules.match_keyword("在吗？") is None
    assert rules.match_keyword("验证码123456").id == "regex"


def test_transfer_and_filter(tmp_path: Path):
    transfer = _write(
        tmp_path,
        "transfer.yaml",
        """
- keywords: 投诉|举报
  reason: 投诉
- keywords: 人工
  reason: 人工
""",
    )
    filters = _write(
        tmp_path,
        "filters.yaml",
        """
- condition_type: contains
  condition_value: 刷单
- condition_type: regex
  condition_value: ^(测试|test)$
""",
    )
    rules = load_rules(
        tmp_path / "none.yaml", transfer, tmp_path / "r.yaml", filters, tmp_path / "g.yaml"
    )
    assert rules.match_transfer("我要投诉").reason == "投诉"
    assert rules.match_transfer("转人工") is not None
    assert rules.match_transfer("你好") is None
    assert rules.is_filtered("专业刷单") is not None
    assert rules.is_filtered("测试") is not None
    assert rules.is_filtered("你好") is None


def test_replace_and_goods(tmp_path: Path):
    replaces = _write(
        tmp_path,
        "r.yaml",
        """
- pattern: 链接
  replace: 页面
- pattern: 微信
  replace: 平台消息
""",
    )
    goods = _write(
        tmp_path,
        "g.yaml",
        """
- goods_id: "1001"
  reply: 商品专属回复
""",
    )
    rules = load_rules(
        tmp_path / "none.yaml", tmp_path / "none2.yaml", replaces, tmp_path / "none3.yaml", goods
    )
    text, applied = rules.apply_replace("点这个链接，加微信聊")
    assert text == "点这个页面，加平台消息聊"
    assert len(applied) == 2
    assert rules.match_goods("1001").reply == "商品专属回复"
    assert rules.match_goods("9999") is None


def test_real_project_rules_load():
    """项目内置规则文件能被正常加载。"""
    from pdd_cs.config import PROJECT_ROOT, Config

    config = Config.load(PROJECT_ROOT / "config" / "config.example.yaml")
    rules = load_rules(
        config.rules.keyword_file,
        config.rules.transfer_file,
        config.rules.replace_file,
        config.rules.filter_file,
        config.rules.goods_file,
    )
    stats = rules.stats()
    assert stats["keywords"] >= 10
    assert stats["transfers"] >= 5
    assert rules.match_keyword("什么时候发货") is not None
    assert rules.match_transfer("我要投诉") is not None
