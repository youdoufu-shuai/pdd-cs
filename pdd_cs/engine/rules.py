# -*- coding: utf-8 -*-
"""规则加载与匹配：关键词回复、转人工、替换词、入站过滤、商品专属回复。

匹配语义沿用 ChatGPT-On-CS（开源项目）的约定，商家已有的话术表可以低成本迁移：

    - ``|``     多个候选写法，命中任一即可，例如：``发货|几天发|什么时候发``
    - ``[and]`` 多个片段需按顺序全部出现，例如：``发货[and]几天``
    - ``[or]``  同一关键词的多条候选回复，随机选一条
    - ``[~]``   随机字符 / 表情占位符（避免拼多多判定重复话术）
    - ``exact`` / ``fuzzy`` / ``regex`` 三种匹配模式
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from ..constants import REPLY_IMAGE, REPLY_TEXT
from ..utils import range_match, replace_keyword

logger = logging.getLogger("pdd_cs.rules")


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class KeywordRule:
    id: str
    patterns: list[str]
    reply: str
    match: str = "fuzzy"
    reply_type: str = REPLY_TEXT
    followup: str = ""
    priority: int = 100
    enabled: bool = True
    note: str = ""

    @property
    def sort_key(self) -> tuple[int, str]:
        return (self.priority, self.id)


@dataclass
class TransferRule:
    patterns: list[str]
    reason: str = "关键词转人工"
    match: str = "fuzzy"
    enabled: bool = True


@dataclass
class ReplaceRule:
    pattern: str
    replace: str
    match: str = "fuzzy"
    enabled: bool = True
    note: str = ""


@dataclass
class FilterRule:
    condition_type: str = "contains"  # contains | regex | msg_type
    condition_value: str = ""
    enabled: bool = True
    note: str = ""


@dataclass
class BlacklistEntry:
    """黑名单：按客户 uid 或消息关键词静默（不自动回复）。"""

    uid: str = ""
    keyword: str = ""
    note: str = ""
    enabled: bool = True


@dataclass
class GoodsRule:
    goods_id: str
    reply: str
    reply_type: str = REPLY_TEXT
    enabled: bool = True


@dataclass
class RuleSet:
    keywords: list[KeywordRule] = field(default_factory=list)
    transfers: list[TransferRule] = field(default_factory=list)
    replaces: list[ReplaceRule] = field(default_factory=list)
    filters: list[FilterRule] = field(default_factory=list)
    goods: list[GoodsRule] = field(default_factory=list)
    blacklist: list[BlacklistEntry] = field(default_factory=list)

    # -- 匹配 ---------------------------------------------------------
    def match_keyword(self, text: str) -> Optional[KeywordRule]:
        for rule in sorted(self.keywords, key=lambda r: r.sort_key):
            if not rule.enabled:
                continue
            if _patterns_match(rule.patterns, text, rule.match):
                return rule
        return None

    def match_transfer(self, text: str) -> Optional[TransferRule]:
        for rule in self.transfers:
            if not rule.enabled:
                continue
            if _patterns_match(rule.patterns, text, rule.match):
                return rule
        return None

    def match_goods(self, goods_id: str) -> Optional[GoodsRule]:
        if not goods_id:
            return None
        for rule in self.goods:
            if rule.enabled and str(rule.goods_id) == str(goods_id):
                return rule
        return None

    def is_filtered(self, text: str, msg_type: str = "text") -> Optional[FilterRule]:
        for rule in self.filters:
            if not rule.enabled:
                continue
            if rule.condition_type == "contains":
                if rule.condition_value and rule.condition_value in (text or ""):
                    return rule
            elif rule.condition_type == "regex":
                import re

                try:
                    if re.search(rule.condition_value, text or ""):
                        return rule
                except re.error:
                    continue
            elif rule.condition_type == "msg_type":
                if rule.condition_value == msg_type:
                    return rule
        return None

    def is_blacklisted(self, uid: str, text: str) -> Optional[BlacklistEntry]:
        """命中黑名单（uid 或关键词）则返回条目。"""
        for entry in self.blacklist:
            if not entry.enabled:
                continue
            if entry.uid and str(entry.uid) == str(uid):
                return entry
            if entry.keyword and entry.keyword in (text or ""):
                return entry
        return None

    def apply_replace(self, text: str) -> tuple[str, list[ReplaceRule]]:
        applied: list[ReplaceRule] = []
        out = text
        for rule in self.replaces:
            if not rule.enabled or not rule.pattern:
                continue
            new_text = replace_keyword(rule.pattern, out, rule.replace, rule.match)
            if new_text != out:
                applied.append(rule)
                out = new_text
        return out, applied

    def stats(self) -> dict[str, int]:
        return {
            "keywords": len(self.keywords),
            "transfers": len(self.transfers),
            "replaces": len(self.replaces),
            "filters": len(self.filters),
            "goods": len(self.goods),
            "blacklist": len(self.blacklist),
        }


def _patterns_match(patterns: list[str], text: str, match: str) -> bool:
    if not text or not patterns:
        return False
    return any(range_match(pattern, text, match) for pattern in patterns)


# ---------------------------------------------------------------------------
# 加载
# ---------------------------------------------------------------------------
def _read_yaml(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            return yaml.safe_load(fh)
    except (OSError, yaml.YAMLError) as exc:
        logger.error("读取规则文件失败 %s: %s", path, exc)
        return None


def _to_patterns(value: Any) -> list[str]:
    """把 YAML 中的关键词字段统一成模式列表。"""
    if isinstance(value, str):
        parts = [p.strip() for p in value.split("|") if p.strip()]
        return parts or [value.strip()]
    if isinstance(value, (list, tuple)):
        patterns: list[str] = []
        for item in value:
            patterns.extend(_to_patterns(item))
        return patterns
    return []


def _load_keywords(path: Path) -> list[KeywordRule]:
    raw = _read_yaml(path) or []
    if not isinstance(raw, list):
        logger.error("%s 应为规则列表", path)
        return []
    rules: list[KeywordRule] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        patterns = _to_patterns(item.get("keywords") or item.get("keyword") or "")
        reply = str(item.get("reply") or "")
        if not patterns or not reply:
            continue
        rules.append(
            KeywordRule(
                id=str(item.get("id") or f"kw-{index + 1}"),
                patterns=patterns,
                reply=reply,
                match=str(item.get("match", "fuzzy")),
                reply_type=str(item.get("reply_type", REPLY_TEXT)).upper(),
                followup=str(item.get("followup") or ""),
                priority=int(item.get("priority", 100)),
                enabled=bool(item.get("enabled", True)),
                note=str(item.get("note", "")),
            )
        )
    return rules


def _load_transfers(path: Path) -> list[TransferRule]:
    raw = _read_yaml(path) or []
    if not isinstance(raw, list):
        return []
    rules: list[TransferRule] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        patterns = _to_patterns(item.get("keywords") or item.get("keyword") or "")
        if not patterns:
            continue
        rules.append(
            TransferRule(
                patterns=patterns,
                reason=str(item.get("reason", "关键词转人工")),
                match=str(item.get("match", "fuzzy")),
                enabled=bool(item.get("enabled", True)),
            )
        )
    return rules


def _load_replaces(path: Path) -> list[ReplaceRule]:
    raw = _read_yaml(path) or []
    if not isinstance(raw, list):
        return []
    rules: list[ReplaceRule] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        pattern = str(item.get("pattern") or item.get("keyword") or "")
        replace = str(item.get("replace") or "")
        if not pattern:
            continue
        rules.append(
            ReplaceRule(
                pattern=pattern,
                replace=replace,
                match=str(item.get("match", "fuzzy")),
                enabled=bool(item.get("enabled", True)),
                note=str(item.get("note", "")),
            )
        )
    return rules


def _load_filters(path: Path) -> list[FilterRule]:
    raw = _read_yaml(path) or []
    if not isinstance(raw, list):
        return []
    rules: list[FilterRule] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        rules.append(
            FilterRule(
                condition_type=str(item.get("condition_type", "contains")),
                condition_value=str(item.get("condition_value", "")),
                enabled=bool(item.get("enabled", True)),
                note=str(item.get("note", "")),
            )
        )
    return rules


def _load_blacklist(path: Path) -> list[BlacklistEntry]:
    raw = _read_yaml(path) or []
    if not isinstance(raw, list):
        return []
    entries: list[BlacklistEntry] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        uid = str(item.get("uid") or "")
        keyword = str(item.get("keyword") or "")
        if not uid and not keyword:
            continue
        entries.append(
            BlacklistEntry(
                uid=uid,
                keyword=keyword,
                note=str(item.get("note", "")),
                enabled=bool(item.get("enabled", True)),
            )
        )
    return entries


def _load_goods(path: Path) -> list[GoodsRule]:
    raw = _read_yaml(path) or []
    if not isinstance(raw, list):
        return []
    rules: list[GoodsRule] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        goods_id = item.get("goods_id")
        reply = str(item.get("reply") or "")
        if goods_id is None or not reply:
            continue
        rules.append(
            GoodsRule(
                goods_id=str(goods_id),
                reply=reply,
                reply_type=str(item.get("reply_type", REPLY_TEXT)).upper(),
                enabled=bool(item.get("enabled", True)),
            )
        )
    return rules


def load_rules(
    keyword_file: Path,
    transfer_file: Path,
    replace_file: Path,
    filter_file: Path,
    goods_file: Path,
    blacklist_file: Optional[Path] = None,
) -> RuleSet:
    """从 YAML 文件加载全部规则。"""
    rules = RuleSet(
        keywords=_load_keywords(keyword_file),
        transfers=_load_transfers(transfer_file),
        replaces=_load_replaces(replace_file),
        filters=_load_filters(filter_file),
        goods=_load_goods(goods_file),
        blacklist=_load_blacklist(blacklist_file) if blacklist_file else [],
    )
    logger.info("规则加载完成: %s", rules.stats())
    return rules


__all__ = [
    "BlacklistEntry",
    "KeywordRule",
    "TransferRule",
    "ReplaceRule",
    "FilterRule",
    "GoodsRule",
    "RuleSet",
    "load_rules",
    "REPLY_TEXT",
    "REPLY_IMAGE",
]
