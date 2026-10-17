# -*- coding: utf-8 -*-
"""话术与知识库合规体检（lint）。

上线前 / 改完话术后的自检：把关键词回复、知识库、人设里所有会发给客户的文字
过一遍平台规则检查，提前发现“今天一定发”“包退”“加微信”“链接”这类风险表述。

CLI：
    python -m pdd_cs lint            # 人类可读报告
    python -m pdd_cs lint --strict   # 有高危问题时返回非 0（可接 CI）
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from ..config import Config
from ..utils import split_variants
from .compliance import ComplianceGuard

logger = logging.getLogger("pdd_cs.lint")

LEVEL_ERROR = "error"
LEVEL_WARN = "warn"


@dataclass
class LintIssue:
    source: str      # 文件或规则
    level: str       # error / warn
    category: str
    matched: str
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "level": self.level,
            "category": self.category,
            "matched": self.matched,
            "text": self.text,
        }


@dataclass
class LintReport:
    issues: list[LintIssue]
    checked_files: int = 0
    checked_texts: int = 0

    @property
    def errors(self) -> list[LintIssue]:
        return [i for i in self.issues if i.level == LEVEL_ERROR]

    @property
    def warnings(self) -> list[LintIssue]:
        return [i for i in self.issues if i.level == LEVEL_WARN]

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": len(self.errors),
            "warnings": len(self.warnings),
            "checked_files": self.checked_files,
            "checked_texts": self.checked_texts,
        }


def _iter_reply_texts(reply: str) -> list[str]:
    """把 "[or]" 候选拆开逐个检查。"""
    variants = split_variants(reply) or [reply]
    return [v for v in variants if v.strip()]


#: 这些行是在写“禁止/风险提示”，不是要发给客户的话，跳过检查
_GUARDRAIL_MARKERS = (
    "不得", "不能", "不要", "禁止", "避免", "风险", "违规", "不允许",
    "不承诺", "不许", "不做", "严禁", "注意", "红线",
)


def _is_guardrail_line(text: str) -> bool:
    import re

    stripped = re.sub(r"^[-*•\d.、\s]+", "", text or "")
    if stripped.startswith("不"):
        return True
    return any(marker in text for marker in _GUARDRAIL_MARKERS)


def _check_text(
    guard: ComplianceGuard, text: str, source: str, issues: list[LintIssue]
) -> None:
    result = guard.check_outbound(text)
    for violation in result.violations:
        level = LEVEL_ERROR if violation.severity in {"block", "rewrite"} else LEVEL_WARN
        # 绝对化用语属于提醒级别，不阻断上线
        if violation.category in {"absolute", "rude"}:
            level = LEVEL_WARN
        issues.append(
            LintIssue(
                source=source,
                level=level,
                category=violation.category,
                matched=violation.matched,
                text=text.strip(),
            )
        )


def lint_rules(config: Config, guard: ComplianceGuard) -> LintReport:
    """检查关键词回复、商品专属回复、替换词（替换词本身不检查）。"""
    from .rules import load_rules

    rules = load_rules(
        config.rules.keyword_file,
        config.rules.transfer_file,
        config.rules.replace_file,
        config.rules.filter_file,
        config.rules.goods_file,
        config.rules.blacklist_file,
    )
    issues: list[LintIssue] = []
    checked = 0
    for rule in rules.keywords:
        for text in _iter_reply_texts(rule.reply):
            checked += 1
            _check_text(guard, text, f"keywords.yaml#{rule.id}", issues)
    for rule in rules.goods:
        for text in _iter_reply_texts(rule.reply):
            checked += 1
            _check_text(guard, text, f"goods.yaml#{rule.goods_id}", issues)
    return LintReport(issues=issues, checked_files=2, checked_texts=checked)


def lint_files(config: Config, guard: ComplianceGuard) -> LintReport:
    """检查人设与知识库 Markdown。"""
    issues: list[LintIssue] = []
    checked_files = 0
    checked_texts = 0
    paths: list[Path] = [config.persona_file]
    if config.knowledge.dir.exists():
        paths.extend(sorted(config.knowledge.dir.glob("*.md")))
    for path in paths:
        if not path.exists():
            continue
        checked_files += 1
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            issues.append(LintIssue(str(path.name), LEVEL_ERROR, "read_error", str(exc), ""))
            continue
        for lineno, line in enumerate(content.splitlines(), start=1):
            text = line.strip()
            if len(text) < 4 or text.startswith("#") or text.startswith(">"):
                continue
            if _is_guardrail_line(text):
                continue
            checked_texts += 1
            _check_text(guard, text, f"{path.name}:{lineno}", issues)
    return LintReport(issues=issues, checked_files=checked_files, checked_texts=checked_texts)


def lint_all(config: Config, guard: ComplianceGuard) -> LintReport:
    rules_report = lint_rules(config, guard)
    files_report = lint_files(config, guard)
    merged = LintReport(
        issues=rules_report.issues + files_report.issues,
        checked_files=rules_report.checked_files + files_report.checked_files,
        checked_texts=rules_report.checked_texts + files_report.checked_texts,
    )
    # 同一条文本的同类问题去重
    seen: set[tuple[str, str, str]] = set()
    deduped: list[LintIssue] = []
    for issue in merged.issues:
        key = (issue.source, issue.category, issue.matched)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(issue)
    merged.issues = deduped
    return merged


__all__ = ["lint_all", "lint_rules", "lint_files", "LintIssue", "LintReport"]
