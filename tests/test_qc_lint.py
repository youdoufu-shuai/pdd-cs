# -*- coding: utf-8 -*-
"""会话质检（QC）与话术体检（lint）测试。"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from pdd_cs.config import Config, PROJECT_ROOT
from pdd_cs.engine.compliance import ComplianceGuard
from pdd_cs.engine.lint import lint_all, lint_rules
from pdd_cs.engine.rules import load_rules
from pdd_cs.utils import now_beijing


def _seed_qc(service) -> None:
    store = service.store
    base = now_beijing() - timedelta(hours=1)
    # 1) 客服乱承诺
    store.record_inbound(
        uid="qc-promise", nickname="P", role="OTHER", context_type="text",
        content="什么时候发货", created_at=base,
    )
    store.record_outbound(
        uid="qc-promise", content="放心，今天一定发，包退", action="keyword",
        result="auto_reply", created_at=base + timedelta(seconds=5),
    )
    # 2) 客户未回复
    store.record_inbound(
        uid="qc-unanswered", nickname="U", role="OTHER", context_type="text",
        content="在吗？怎么不理人", created_at=base,
    )
    # 3) 转人工失败
    store.record_inbound(
        uid="qc-transfer", nickname="T", role="OTHER", context_type="text",
        content="我要投诉", created_at=base,
    )
    store.record_outbound(
        uid="qc-transfer", content="[转人工失败]", action="transfer",
        result="transfer", status="failed", created_at=base + timedelta(seconds=3),
    )
    store.mark_result("qc-transfer", "transfer_failed")
    # 4) 重复回复
    store.record_inbound(
        uid="qc-duplicate", nickname="D", role="OTHER", context_type="text",
        content="发货时间", created_at=base,
    )
    store.record_outbound(
        uid="qc-duplicate", content="发货时效以商品详情页公示为准哦", action="keyword",
        result="auto_reply", created_at=base + timedelta(seconds=2),
    )
    store.record_outbound(
        uid="qc-duplicate", content="发货时效以商品详情页公示为准哦", action="keyword",
        result="auto_reply", created_at=base + timedelta(seconds=8),
    )
    # 5) 首响超时
    store.record_inbound(
        uid="qc-slow", nickname="S", role="OTHER", context_type="text",
        content="你好", created_at=base,
    )
    store.record_outbound(
        uid="qc-slow", content="您好～", action="keyword", result="auto_reply",
        created_at=base + timedelta(seconds=360),
    )


def test_qc_scan_hits(service):
    _seed_qc(service)
    result = service.qc_scan(days=7, force=True)
    assert result["scanned_conversations"] >= 5
    rule_ids = {hit["rule_id"] for hit in result["hits"]}
    for expected in (
        "risky_promise",
        "unanswered",
        "transfer_failed",
        "duplicate_reply",
        "slow_first_response",
    ):
        assert expected in rule_ids, f"未命中 {expected}: {rule_ids}"
    assert result["summary"]["high"] >= 2
    # 按严重程度排序：high 在前
    severities = [hit["severity"] for hit in result["hits"]]
    assert severities[0] == "high"

    # 缓存生效（force=False 返回同一对象内容）
    cached = service.qc_scan(days=7)
    assert cached["generated_at"] == result["generated_at"]


def test_qc_shop_filter(service):
    _seed_qc(service)
    service.store.record_inbound(
        uid="other-shop", nickname="X", role="OTHER", context_type="text",
        content="你好", shop_id="shop-x",
    )
    result = service.qc_scan(shop_id="shop-x", days=7, force=True)
    assert all(hit["shop_id"] == "shop-x" for hit in result["hits"])
    assert result["scanned_conversations"] == 1


def test_lint_project_passes():
    config = Config.load(PROJECT_ROOT / "config" / "config.example.yaml")
    guard = ComplianceGuard.from_file(config.compliance, config.rules.compliance_file)
    report = lint_all(config, guard)
    assert report.checked_texts > 50
    assert report.ok, [i.__dict__ for i in report.errors[:5]]
    assert report.summary()["errors"] == 0


def test_lint_detects_risky_keyword(tmp_path: Path):
    bad_rules = tmp_path / "bad_keywords.yaml"
    bad_rules.write_text(
        """
- id: bad-1
  keywords: 催发货
  reply: 放心，今天一定发，包退，加微信也行
- id: bad-2
  keywords: 优惠
  reply: 给您赔你 100 元，保证到货
""",
        encoding="utf-8",
    )
    config = Config.load(PROJECT_ROOT / "config" / "config.example.yaml")
    config.rules.keyword_file = bad_rules
    guard = ComplianceGuard.from_file(config.compliance, config.rules.compliance_file)
    report = lint_rules(config, guard)
    assert not report.ok
    assert report.summary()["errors"] >= 3
    categories = {issue.category for issue in report.errors}
    assert "promise" in categories
    assert "offsite" in categories or "blocked" in categories


def test_blacklist(tmp_path: Path):
    blacklist = tmp_path / "blacklist.yaml"
    blacklist.write_text(
        """
- uid: "bad-user"
  note: 恶意骚扰
- keyword: 律师函
  note: 法律函件
- keyword: 已关闭
  enabled: false
""",
        encoding="utf-8",
    )
    rules = load_rules(
        PROJECT_ROOT / "config" / "rules" / "keywords.yaml",
        PROJECT_ROOT / "config" / "rules" / "transfer.yaml",
        PROJECT_ROOT / "config" / "rules" / "replace.yaml",
        PROJECT_ROOT / "config" / "rules" / "filters.yaml",
        PROJECT_ROOT / "config" / "rules" / "goods.yaml",
        blacklist,
    )
    assert rules.is_blacklisted("bad-user", "你好") is not None
    assert rules.is_blacklisted("good-user", "我要发律师函") is not None
    assert rules.is_blacklisted("good-user", "已关闭") is None
    assert rules.is_blacklisted("good-user", "你好") is None
    assert rules.stats()["blacklist"] == 3
