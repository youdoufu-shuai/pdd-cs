# -*- coding: utf-8 -*-
"""会话质检（QC）：把“该被人工看一眼”的会话挑出来。

日常客服主管不会逐条翻聊天记录，而是按规则抽检：
    - 客服（含机器人）有没有乱承诺 / 导流 / 说敏感词；
    - 客户消息有没有被漏回、首响有没有超时；
    - 转人工有没有失败；
    - 是否连续重复同一句话；
    - 客户情绪是否升级、是否出现投诉/监管关键词。

规则可在 ``config/rules/qc.yaml`` 中增删，命中结果供面板与 CLI 使用。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from ..config import Config
from ..store import Store
from ..utils import now_beijing, similarity
from .compliance import ComplianceGuard

logger = logging.getLogger("pdd_cs.qc")

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


@dataclass
class QCRule:
    id: str
    name: str
    type: str
    severity: str = "medium"
    note: str = ""
    keywords: list[str] = field(default_factory=list)
    enabled: bool = True


@dataclass
class QCHit:
    rule_id: str
    rule_name: str
    severity: str
    shop_id: str
    uid: str
    nickname: str = ""
    snippet: str = ""
    detail: str = ""
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "severity": self.severity,
            "shop_id": self.shop_id,
            "uid": self.uid,
            "nickname": self.nickname,
            "snippet": self.snippet,
            "detail": self.detail,
            "created_at": self.created_at,
        }


def load_qc_rules(path: Path) -> list[QCRule]:
    if not path.exists():
        return []
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    except (OSError, yaml.YAMLError) as exc:
        logger.error("读取质检规则失败 %s: %s", path, exc)
        return []
    rules: list[QCRule] = []
    for index, item in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(item, dict):
            continue
        rule_id = str(item.get("id") or f"qc-{index + 1}")
        rules.append(
            QCRule(
                id=rule_id,
                name=str(item.get("name") or rule_id),
                type=str(item.get("type") or "outbound_check"),
                severity=str(item.get("severity") or "medium"),
                note=str(item.get("note") or ""),
                keywords=[str(k) for k in (item.get("keywords") or [])],
                enabled=bool(item.get("enabled", True)),
            )
        )
    return [r for r in rules if r.enabled]


class QCScanner:
    """按规则扫描最近会话，产出质检命中列表（带缓存）。"""

    def __init__(self, config: Config, guard: ComplianceGuard, store: Store, rules_path: Path):
        self.config = config
        self.guard = guard
        self.store = store
        self.rules = load_qc_rules(rules_path)
        self._cache: dict[tuple, tuple[float, dict[str, Any]]] = {}

    # ------------------------------------------------------------------
    def scan(
        self,
        *,
        shop_id: Optional[str] = None,
        days: Optional[int] = None,
        force: bool = False,
    ) -> dict[str, Any]:
        days = days or self.config.qc.days
        key = (shop_id or "all", days)
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached and not force and now - cached[0] < 60:
            return cached[1]

        dialogues = self.store.dialogue_rows(
            shop_id=shop_id, days=days, limit=self.config.qc.max_conversations
        )
        hits: list[QCHit] = []
        for dialogue in dialogues:
            hits.extend(self._scan_dialogue(dialogue))
        hits.sort(key=lambda h: (SEVERITY_ORDER.get(h.severity, 9), h.created_at), reverse=False)
        summary = {
            "high": sum(1 for h in hits if h.severity == "high"),
            "medium": sum(1 for h in hits if h.severity == "medium"),
            "low": sum(1 for h in hits if h.severity == "low"),
        }
        result = {
            "generated_at": now_beijing().strftime("%Y-%m-%d %H:%M:%S"),
            "days": days,
            "shop_id": shop_id or "all",
            "scanned_conversations": len(dialogues),
            "scanned_messages": sum(len(d.get("messages") or []) for d in dialogues),
            "hits": [h.to_dict() for h in hits],
            "summary": summary,
        }
        self._cache[key] = (now, result)
        return result

    # ------------------------------------------------------------------
    def _scan_dialogue(self, dialogue: dict[str, Any]) -> list[QCHit]:
        shop_id = str(dialogue.get("shop_id") or "default")
        uid = str(dialogue.get("uid") or "")
        nickname = str(dialogue.get("nickname") or "")
        messages = dialogue.get("messages") or []
        hits: list[QCHit] = []

        outbound: list[str] = []
        last_in = None
        first_in_at = None
        first_out_after_in_at = None

        for message in messages:
            content = str(message.get("content") or "")
            created = str(message.get("created_at") or "")
            direction = message.get("direction")
            if direction == "in":
                last_in = created
                if first_in_at is None:
                    first_in_at = created
                hits.extend(self._check_inbound(shop_id, uid, nickname, content, created))
            else:
                if content:
                    outbound.append(content)
                if first_out_after_in_at is None and first_in_at is not None and created >= first_in_at:
                    first_out_after_in_at = created
                hits.extend(self._check_outbound(shop_id, uid, nickname, content, created))

        # 未回复
        if messages and messages[-1].get("direction") == "in":
            rule = self._rule("unanswered")
            if rule:
                hits.append(
                    QCHit(
                        rule_id=rule.id,
                        rule_name=rule.name,
                        severity=rule.severity,
                        shop_id=shop_id,
                        uid=uid,
                        nickname=nickname,
                        snippet=str(messages[-1].get("content") or "")[:120],
                        detail="客户最后一条消息未得到回复",
                        created_at=str(messages[-1].get("created_at") or ""),
                    )
                )

        # 首响超时
        rule = self._rule("slow_first_response")
        if rule and first_in_at and first_out_after_in_at:
            try:
                from datetime import datetime

                tz = now_beijing().tzinfo
                start = datetime.strptime(first_in_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
                end = datetime.strptime(first_out_after_in_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
                seconds = (end - start).total_seconds()
                if seconds > self.config.compliance.sla.first_response_seconds:
                    hits.append(
                        QCHit(
                            rule_id=rule.id,
                            rule_name=rule.name,
                            severity=rule.severity,
                            shop_id=shop_id,
                            uid=uid,
                            nickname=nickname,
                            snippet=f"首响 {seconds:.0f}s",
                            detail=f"超过红线 {self.config.compliance.sla.first_response_seconds:.0f}s",
                            created_at=first_out_after_in_at,
                        )
                    )
            except (ValueError, TypeError):
                pass

        # 重复回复
        rule = self._rule("duplicate_reply")
        if rule:
            for previous, current in zip(outbound, outbound[1:]):
                if similarity(previous, current) >= 0.92:
                    hits.append(
                        QCHit(
                            rule_id=rule.id,
                            rule_name=rule.name,
                            severity=rule.severity,
                            shop_id=shop_id,
                            uid=uid,
                            nickname=nickname,
                            snippet=current[:120],
                            detail="连续两条回复高度相似",
                            created_at="",
                        )
                    )
                    break

        # 转人工失败
        if str(dialogue.get("last_result") or "") == "transfer_failed":
            rule = self._rule("transfer_failed")
            if rule:
                hits.append(
                    QCHit(
                        rule_id=rule.id,
                        rule_name=rule.name,
                        severity=rule.severity,
                        shop_id=shop_id,
                        uid=uid,
                        nickname=nickname,
                        detail="最近一次转人工失败",
                        created_at=str(dialogue.get("last_in_at") or ""),
                    )
                )
        return hits

    # ------------------------------------------------------------------
    def _check_outbound(
        self, shop_id: str, uid: str, nickname: str, content: str, created: str
    ) -> list[QCHit]:
        if not content:
            return []
        result = self.guard.check_outbound(content)
        hits: list[QCHit] = []
        seen_rules: set[str] = set()
        for violation in result.violations:
            rule = self._rule_for_outbound(violation.category)
            if not rule or rule.id in seen_rules:
                continue
            seen_rules.add(rule.id)
            hits.append(
                QCHit(
                    rule_id=rule.id,
                    rule_name=rule.name,
                    severity=rule.severity,
                    shop_id=shop_id,
                    uid=uid,
                    nickname=nickname,
                    snippet=content[:120],
                    detail=f"{violation.category}: {violation.matched}",
                    created_at=created,
                )
            )
        return hits

    def _check_inbound(
        self, shop_id: str, uid: str, nickname: str, content: str, created: str
    ) -> list[QCHit]:
        hits: list[QCHit] = []
        emotion_rule = self._rule("negative_emotion")
        if emotion_rule and any(word in content for word in self.config.compliance.negative_emotion_keywords):
            hits.append(
                QCHit(
                    rule_id=emotion_rule.id,
                    rule_name=emotion_rule.name,
                    severity=emotion_rule.severity,
                    shop_id=shop_id,
                    uid=uid,
                    nickname=nickname,
                    snippet=content[:120],
                    detail="客户出现负面情绪词",
                    created_at=created,
                )
            )
        keyword_rule = self._rule("complaint_keyword")
        if keyword_rule:
            for keyword in keyword_rule.keywords:
                if keyword and keyword in content:
                    hits.append(
                        QCHit(
                            rule_id=keyword_rule.id,
                            rule_name=keyword_rule.name,
                            severity=keyword_rule.severity,
                            shop_id=shop_id,
                            uid=uid,
                            nickname=nickname,
                            snippet=content[:120],
                            detail=f"命中关键词：{keyword}",
                            created_at=created,
                        )
                    )
                    break
        return hits

    def _rule(self, type_name: str) -> Optional[QCRule]:
        for rule in self.rules:
            if rule.type == type_name:
                return rule
        return None

    def _rule_for_outbound(self, category: str) -> Optional[QCRule]:
        mapping = {
            "promise": "risky_promise",
            "offsite": "offsite_divert",
            "sensitive": "sensitive_word",
            "blocked": "sensitive_word",
            "absolute": "absolute_claim",
            "privacy": "privacy_leak",
            "rude": "rude_reply",
        }
        rule_id = mapping.get(category)
        if rule_id:
            for rule in self.rules:
                if rule.id == rule_id:
                    return rule
        return None

    @property
    def stats(self) -> dict[str, Any]:
        return {"rules": len(self.rules), "names": [r.name for r in self.rules]}


__all__ = ["QCScanner", "QCRule", "QCHit", "load_qc_rules"]
