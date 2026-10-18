# -*- coding: utf-8 -*-
"""拼多多平台规则合规层（出站检查 / 入站转人工信号 / 重复话术 / 披露语）。

本模块把「平台规则」变成机器可执行的检查，覆盖四类最新规则要求：

A. 出站内容合规（发出去的话）
   1. 站外导流：微信 / QQ / 手机号 / 外链 / 二维码等（平台禁止导流）。
   2. 乱承诺：发货时效、物流到达、退换货、赔付金额——拼多多「负体验」判罚口径原文：
      “消费者咨询发货、物流问题时，商家提供不符合实际情况的信息”。
   3. 平台规则冒充：把平台规则说成店铺能决定（如“仅退款我说了算”）。
   4. 敏感 / 违禁词：拼多多实测会被拦截的词（如“链接”“专业人员”），以及刷单、
      好评返现、诱导好评等。
   5. 绝对化用语 / 违规宣称（最、第一、100%、保证……）。
   6. 隐私索取：身份证、银行卡、验证码等。

B. 入站转人工信号
   - 人工请求、投诉/监管（12315/工商/起诉/媒体）、人身安全（过敏/受伤）、
     退款赔偿、强烈负面情绪、大额订单、反复追问。
   - 对齐 GB/T 47746—2026（2026-09-01 实施，推荐性）中“复杂个性化需求及涉及
     人身财产安全的场景优先接入人工”的方向。

C. 重复话术治理
   - 拼多多对重复/机械回复有限制，需要让同一会话的每次回复有差异。

D. 智能客服身份披露
   - 首条自动回复声明智能客服身份 + 指引转人工，避免“假装人工”带来的投诉风险。

规则来源与边界见 docs/platform-rules.md；所有阈值与词表均可在 config 中调整。
"""
from __future__ import annotations

import logging
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

from ..config import ComplianceConfig
from ..utils import contains_contact, max_similarity, similarity, truncate

logger = logging.getLogger("pdd_cs.compliance")

# 平台内商品链接：不是站外导流，允许出现在商品介绍里
_PDD_PRODUCT_LINK_RE = re.compile(
    r"https?://(?:(?:mobile\.)?yangkeduo\.com|(?:mobile\.)?pinduoduo\.com)/"
    r"goods(?:\.html|2\.html)?\?goods_id=\d+"
)
_PDD_LINK_PLACEHOLDER = "__PDD_GOODS_LINK_"


def _mask_pdd_product_links(text: str) -> tuple[str, list[str]]:
    links: list[str] = []

    def repl(match: re.Match[str]) -> str:
        links.append(match.group(0))
        return f"{_PDD_LINK_PLACEHOLDER}{len(links) - 1}__"

    return _PDD_PRODUCT_LINK_RE.sub(repl, text), links


def _restore_pdd_product_links(text: str, links: list[str]) -> str:
    for index, link in enumerate(links):
        text = text.replace(f"{_PDD_LINK_PLACEHOLDER}{index}__", link)
    return text


# 语音 / 图片等非文本消息不参与关键词匹配，但这里保留常量便于扩展
_SEVERITY_BLOCK = "block"
_SEVERITY_REWRITE = "rewrite"
_SEVERITY_WARN = "warn"


@dataclass
class Violation:
    category: str
    severity: str
    matched: str
    note: str
    suggestion: str = ""


@dataclass
class OutboundResult:
    original: str
    text: str
    violations: list[Violation] = field(default_factory=list)
    blocked: bool = False
    needs_human: bool = False

    @property
    def clean(self) -> bool:
        return not self.violations

    def summary(self) -> str:
        if not self.violations:
            return "通过"
        items = "、".join(f"{v.category}({v.matched})" for v in self.violations)
        return f"{'已拦截' if self.blocked else '已改写'}: {items}"


@dataclass
class HandoffSignal:
    reason: str
    category: str
    priority: int = 5  # 数字越小优先级越高

    def __str__(self) -> str:
        return f"{self.category}: {self.reason}"


@dataclass
class CompliancePattern:
    pattern: str
    match: str = "fuzzy"
    category: str = ""
    note: str = ""
    severity: str = _SEVERITY_REWRITE
    suggestion: str = ""


def _match(pattern: str, text: str, mode: str) -> Optional[str]:
    if not pattern or not text:
        return None
    try:
        if mode == "regex":
            m = re.search(pattern, text)
            return m.group(0) if m else None
        if mode == "exact":
            return pattern if text.strip() == pattern else None
        return pattern if pattern in text else None
    except re.error:
        return None


class ComplianceGuard:
    """出站 / 入站的平台规则守卫。"""

    def __init__(
        self,
        config: ComplianceConfig,
        *,
        offsite: Optional[list[CompliancePattern]] = None,
        promises: Optional[list[CompliancePattern]] = None,
        sensitive: Optional[list[CompliancePattern]] = None,
        absolute: Optional[list[CompliancePattern]] = None,
        privacy: Optional[list[CompliancePattern]] = None,
        blocked: Optional[list[CompliancePattern]] = None,
        rewrite_map: Optional[dict[str, str]] = None,
        fallbacks: Optional[dict[str, str]] = None,
    ) -> None:
        self.config = config
        self.offsite = offsite or []
        self.promises = promises or []
        self.sensitive = sensitive or []
        self.absolute = absolute or []
        self.privacy = privacy or []
        self.blocked = blocked or []
        self.rewrite_map = rewrite_map or {}
        self.fallbacks = fallbacks or {}

        # 转人工关键词（配置 + 默认合并，去重）
        self.transfer_keywords = _dedupe(
            list(config.transfer_keywords) + self._patterns_of(self.blocked)
        )
        self.emotion_keywords = _dedupe(list(config.negative_emotion_keywords))

        # 安全 / 法律 / 投诉类信号（优先级更靠前）
        self.safety_keywords = [
            "过敏", "过敏了", "受伤", "伤了", "食品安全", "中毒", "异物", "漏电",
            "起火", "爆炸", "孩子吃了", "宝宝吃了", "人身", "医院",
        ]
        self.legal_keywords = [
            "12315", "工商", "市监", "消费者协会", "消协", "起诉", "律师",
            "法院", "报警", "媒体", "曝光", "黑猫投诉",
        ]
        self.complaint_keywords = ["投诉", "举报", "差评", "曝光", "骗子", "欺诈"]
        self.after_sale_keywords = [
            "退款", "退货", "仅退款", "换货", "补发", "赔偿", "赔付", "少发",
            "漏发", "错发", "质量问题", "假货", "坏了", "破损",
        ]

    # ------------------------------------------------------------------
    # 规则加载
    # ------------------------------------------------------------------
    @classmethod
    def from_file(cls, config: ComplianceConfig, path: Path) -> "ComplianceGuard":
        data: dict[str, Any] = {}
        if path.exists():
            try:
                with path.open("r", encoding="utf-8") as fh:
                    loaded = yaml.safe_load(fh) or {}
                if isinstance(loaded, dict):
                    data = loaded
            except (OSError, yaml.YAMLError) as exc:
                logger.error("读取合规规则失败 %s: %s", path, exc)
        else:
            logger.warning("合规规则文件不存在，使用内置默认规则: %s", path)
        return cls(
            config,
            offsite=_parse_patterns(data.get("offsite")),
            promises=_parse_patterns(data.get("promises")),
            sensitive=_parse_patterns(data.get("sensitive")),
            absolute=_parse_patterns(data.get("absolute")),
            privacy=_parse_patterns(data.get("privacy")),
            blocked=_parse_patterns(data.get("blocked")),
            rewrite_map={str(k): str(v) for k, v in (data.get("rewrite_map") or {}).items()},
            fallbacks={str(k): str(v) for k, v in (data.get("fallbacks") or {}).items()},
        )

    @staticmethod
    def _patterns_of(patterns: Iterable[CompliancePattern]) -> list[str]:
        words: list[str] = []
        for item in patterns:
            if item.match != "regex":
                words.extend(p.strip() for p in item.pattern.split("|") if p.strip())
        return words

    # ------------------------------------------------------------------
    # 出站检查
    # ------------------------------------------------------------------
    def check_outbound(self, text: str) -> OutboundResult:
        """检查一条准备发送的回复。"""
        result = OutboundResult(original=text, text=text)
        if not text:
            result.blocked = True
            result.violations.append(
                Violation("empty", _SEVERITY_BLOCK, "", "回复内容为空")
            )
            return result

        # 1) 站外导流；平台内商品链接先遮蔽，避免误判为站外链接
        scan_text, _links = _mask_pdd_product_links(text)
        for pattern in self.offsite:
            matched = _match(pattern.pattern, scan_text, pattern.match)
            if matched:
                result.violations.append(
                    Violation(
                        "offsite",
                        pattern.severity,
                        matched,
                        f"{pattern.category}：{pattern.note}" if pattern.category else pattern.note,
                        pattern.suggestion,
                    )
                )
        if contains_contact(scan_text):
            result.violations.append(
                Violation(
                    "offsite",
                    _SEVERITY_REWRITE,
                    _first_contact(scan_text),
                    "检测到手机号 / 网址 / 站外联系方式；平台禁止导流站外",
                    "改为在平台内沟通",
                )
            )

        # 2) 乱承诺 / 规则冒充
        for pattern in self.promises:
            matched = _match(pattern.pattern, text, pattern.match)
            if matched:
                result.violations.append(
                    Violation(
                        "promise",
                        pattern.severity,
                        matched,
                        f"{pattern.category}：{pattern.note}" if pattern.category else pattern.note,
                        pattern.suggestion,
                    )
                )

        # 3) 平台敏感 / 违禁词
        for pattern in self.sensitive:
            matched = _match(pattern.pattern, text, pattern.match)
            if matched:
                result.violations.append(
                    Violation(
                        "sensitive",
                        pattern.severity,
                        matched,
                        f"{pattern.category}：{pattern.note}" if pattern.category else pattern.note,
                        pattern.suggestion,
                    )
                )
        for pattern in self.blocked:
            matched = _match(pattern.pattern, text, pattern.match)
            if matched:
                result.violations.append(
                    Violation("blocked", _SEVERITY_BLOCK, matched, pattern.note, pattern.suggestion)
                )

        # 4) 绝对化用语
        for pattern in self.absolute:
            matched = _match(pattern.pattern, text, pattern.match)
            if matched:
                result.violations.append(
                    Violation("absolute", pattern.severity, matched, pattern.note, pattern.suggestion)
                )

        # 5) 隐私索取
        for pattern in self.privacy:
            matched = _match(pattern.pattern, text, pattern.match)
            if matched:
                result.violations.append(
                    Violation("privacy", _SEVERITY_BLOCK, matched, pattern.note, pattern.suggestion)
                )

        # 6) 态度类（辱骂 / 反问 / 不耐烦）
        rude = _match_rude(text)
        if rude:
            result.violations.append(
                Violation("rude", _SEVERITY_REWRITE, rude, "客服话术不得带情绪或反问客户")
            )

        # 同一类别 + 同一命中词只保留一条，避免日志重复
        deduped: list[Violation] = []
        seen: set[tuple[str, str]] = set()
        for violation in result.violations:
            key = (violation.category, violation.matched)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(violation)
        result.violations = deduped

        # 汇总处理策略
        if any(v.severity == _SEVERITY_BLOCK for v in result.violations):
            result.blocked = True
            result.needs_human = True
        elif any(v.category in {"promise", "offsite"} for v in result.violations):
            result.needs_human = any(v.category == "promise" for v in result.violations)
        return result

    def enforce(self, text: str, *, max_chars: int = 0) -> OutboundResult:
        """检查并尽量改写成可安全发送的文本。

        策略：
            - 站外联系方式 / 链接：直接删除；
            - 敏感词：按 rewrite_map 替换；
            - 承诺类：无法安全改写时，改用兜底话术（以页面为准 / 转人工确认）；
            - 仍然无法通过：拦截（由上层决定转人工或默认回复）。
        """
        checked = self.check_outbound(text)
        if checked.clean:
            return _apply_length(checked, max_chars)

        safe = text
        # 1) 删除联系方式与链接
        safe = _strip_contact(safe)
        # 2) 敏感词 / 绝对化用语替换
        for word, replacement in self.rewrite_map.items():
            if word and word in safe:
                safe = safe.replace(word, replacement)
        # 3) 再检查一次
        rechecked = self.check_outbound(safe)
        hard_block = any(v.severity == _SEVERITY_BLOCK for v in rechecked.violations)
        has_promise = any(v.category == "promise" for v in rechecked.violations)
        has_offsite = any(v.category == "offsite" for v in rechecked.violations)

        if hard_block or has_promise or has_offsite:
            fallback = self._pick_fallback(rechecked)
            if fallback:
                rechecked.text = fallback
                rechecked.blocked = False
                rechecked.needs_human = True
            else:
                rechecked.blocked = True
                rechecked.needs_human = True

        merged = OutboundResult(
            original=checked.original,
            text=rechecked.text,
            violations=checked.violations,
            blocked=rechecked.blocked,
            needs_human=rechecked.needs_human or checked.needs_human,
        )
        return _apply_length(merged, max_chars)

    def _pick_fallback(self, result: OutboundResult) -> str:
        categories = {v.category for v in result.violations}
        if "promise" in categories:
            return self.fallbacks.get(
                "promise",
                "具体时效和售后处理以商品详情页与平台页面为准，我帮您转人工确认一下～",
            )
        if "offsite" in categories:
            return self.fallbacks.get(
                "offsite", "咱们在平台内沟通就可以，有任何问题我都可以帮您处理～"
            )
        if "sensitive" in categories or "blocked" in categories:
            return self.fallbacks.get("sensitive", "")
        return ""

    # ------------------------------------------------------------------
    # 入站转人工判断
    # ------------------------------------------------------------------
    def handoff_signal(
        self,
        text: str,
        *,
        order_amount: float = 0.0,
        repeat_count: int = 0,
        has_price_negotiation: bool = False,
    ) -> Optional[HandoffSignal]:
        """判断一条买家消息是否需要转人工。

        Returns:
            HandoffSignal（优先级从高到低）；无需转人工返回 None。
        """
        if not self.config.human_handoff.enabled:
            return None
        content = text or ""

        # 1) 人身安全
        hit = _first_keyword(content, self.safety_keywords)
        if hit:
            return HandoffSignal(f"涉及人身安全（{hit}）", "safety", 1)

        # 2) 法律 / 监管
        hit = _first_keyword(content, self.legal_keywords)
        if hit:
            return HandoffSignal(f"涉及监管/法律渠道（{hit}）", "legal", 1)

        # 3) 明确要求人工
        hit = _first_keyword(content, ["转人工", "人工客服", "真人", "叫你们负责人", "找店长", "人工"])
        if hit:
            return HandoffSignal(f"客户要求人工（{hit}）", "human_request", 2)

        # 4) 投诉 / 差评
        hit = _first_keyword(content, self.complaint_keywords)
        if hit:
            return HandoffSignal(f"投诉/差评风险（{hit}）", "complaint", 2)

        # 5) 售后 / 退款 / 赔偿
        hit = _first_keyword(content, self.after_sale_keywords)
        if hit:
            return HandoffSignal(f"售后处理（{hit}）", "after_sales", 3)

        # 6) 价格谈判
        if has_price_negotiation or _first_keyword(content, ["便宜点", "少点", "优惠", "讲价", "最低多少"]):
            return HandoffSignal("价格谈判", "price", 4)

        # 7) 大额订单
        if order_amount and order_amount >= self.config.amount_handoff_threshold:
            return HandoffSignal(f"订单金额较大（{order_amount:.2f} 元）", "amount", 4)

        # 8) 强烈负面情绪
        hit = _first_keyword(content, self.emotion_keywords)
        if hit:
            return HandoffSignal(f"客户情绪激动（{hit}）", "emotion", 3)

        # 9) 反复追问同一问题
        if repeat_count >= max(2, self.config.sla.repeat_ask_threshold):
            return HandoffSignal(f"客户已重复询问 {repeat_count} 次", "repeat", 4)

        # 10) 自定义转人工关键词
        hit = _first_keyword(content, self.transfer_keywords)
        if hit:
            return HandoffSignal(f"命中转人工关键词（{hit}）", "keyword", 5)

        return None

    # ------------------------------------------------------------------
    # 重复话术治理
    # ------------------------------------------------------------------
    def is_duplicate(self, text: str, history: Iterable[str]) -> bool:
        if not self.config.anti_duplicate.enabled or not text:
            return False
        recent = list(history)[-self.config.anti_duplicate.history_size :]
        if not recent:
            return False
        threshold = self.config.anti_duplicate.min_unique_ratio
        return max_similarity(text, recent) >= threshold or text in recent

    def diversify(self, text: str, history: Iterable[str], *, max_chars: int = 0) -> str:
        """在不改变语义的前提下，让重复的回复产生差异。

        手段（按顺序尝试）：
            1. 若话术含 "[or]" 候选，换一条候选；
            2. 若含 "[~]" 占位符，随机化；
            3. 追加自然语气词 / 引导语；
            4. 替换标点与表情。
        """
        if not text:
            return text
        recent = list(history)[-self.config.anti_duplicate.history_size :]
        candidate = text
        if not self.is_duplicate(candidate, recent):
            return _apply_length(OutboundResult(candidate, candidate), max_chars).text

        # 1) [or] 候选
        variants = [p.strip() for p in text.split("[or]") if p.strip()]
        if len(variants) > 1:
            for _ in range(self.config.anti_duplicate.max_attempts * 2):
                candidate = random.choice(variants)
                if not self.is_duplicate(candidate, recent):
                    return _apply_length(OutboundResult(candidate, candidate), max_chars).text

        # 2) [~] 随机化 / 3) 语气与句式变体（长后缀才能有效降低相似度）
        suffixes = [
            "～",
            "呀",
            "哦～",
            "😊",
            "🌸",
            "～您看还有其他可以帮您的吗？",
            "，有问题随时找我哦～",
            "～您先看看，我随时在的",
            "哈～需要的随时跟我说",
            "，您还有其他需要吗？",
            "",
        ]
        prefixes = ["", "", "", "嗯嗯，", "好的～"]
        for _ in range(self.config.anti_duplicate.max_attempts * 4):
            base = re.sub(r"\[~\]", lambda _m: random.choice("abcdefghijkmnpqrstuvwxyz🌸😊🌷"), text)
            candidate = f"{random.choice(prefixes)}{base}{random.choice(suffixes)}"
            if not self.is_duplicate(candidate, recent):
                return _apply_length(OutboundResult(candidate, candidate), max_chars).text

        # 4) 兜底：换标点
        candidate = text.replace("～", "~").replace("，", ",")
        if candidate != text and not self.is_duplicate(candidate, recent):
            return _apply_length(OutboundResult(candidate, candidate), max_chars).text
        return _apply_length(OutboundResult(text, text), max_chars).text

    # ------------------------------------------------------------------
    # 身份披露
    # ------------------------------------------------------------------
    def apply_disclosure(self, text: str, *, first_reply: bool) -> str:
        """按配置拼接智能客服身份披露语。"""
        disclosure = self.config.disclosure
        if not disclosure.enabled or not text:
            return text
        parts: list[str] = []
        if first_reply and disclosure.first_message:
            parts.append(disclosure.first_message)
        parts.append(text)
        if disclosure.every_message_suffix:
            parts.append(disclosure.every_message_suffix)
        if first_reply and disclosure.transfer_hint:
            parts.append(disclosure.transfer_hint)
        return "\n".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------
def _parse_patterns(raw: Any) -> list[CompliancePattern]:
    if not raw:
        return []
    patterns: list[CompliancePattern] = []
    if isinstance(raw, str):
        raw = [{"pattern": raw}]
    if not isinstance(raw, list):
        return []
    for item in raw:
        if isinstance(item, str):
            patterns.append(CompliancePattern(pattern=item))
        elif isinstance(item, dict):
            pattern = str(item.get("pattern") or item.get("word") or "")
            if not pattern:
                continue
            base = CompliancePattern(
                pattern=pattern,
                match=str(item.get("match", "fuzzy")),
                category=str(item.get("category", "")),
                note=str(item.get("note", "")),
                severity=str(item.get("severity", _SEVERITY_REWRITE)),
                suggestion=str(item.get("suggestion", "")),
            )
            # 非正则模式下 "a|b|c" 表示多个候选词，需要拆开匹配
            if base.match != "regex" and "|" in base.pattern:
                for part in base.pattern.split("|"):
                    part = part.strip()
                    if part:
                        patterns.append(
                            CompliancePattern(
                                pattern=part,
                                match=base.match,
                                category=base.category,
                                note=base.note,
                                severity=base.severity,
                                suggestion=base.suggestion,
                            )
                        )
            else:
                patterns.append(base)
    return patterns


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = (item or "").strip()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _first_keyword(text: str, keywords: Iterable[str]) -> Optional[str]:
    for word in keywords:
        if word and word in text:
            return word
    return None


def _first_contact(text: str) -> str:
    for pattern in (
        r"1[3-9]\d{9}",
        r"(?:微信|weixin|wechat|vx|v信|qq|QQ)",
        r"https?://\S+",
        r"www\.\S+",
    ):
        m = re.search(pattern, text)
        if m:
            return m.group(0)
    return "联系方式"


def _strip_contact(text: str) -> str:
    masked, links = _mask_pdd_product_links(text)
    out = re.sub(r"https?://\S+", "", masked)
    out = re.sub(r"www\.\S+", "", out)
    out = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "", out)
    out = re.sub(r"(微信|weixin|wechat|vx|v信|qq|QQ|加V|加v|加微)\s*[:：]?\s*[A-Za-z0-9_\-]{4,}", "", out)
    out = re.sub(r"\s{2,}", " ", out).strip(" ，,。.；;")
    return _restore_pdd_product_links(out, links)


_RUDE_PATTERNS = [
    r"你(自己)?(不会|不懂|不知道)吗",
    r"爱买不买",
    r"关我什么事",
    r"随便你",
    r"别(烦|催)",
    r"看不懂(字|话)",
]


def _match_rude(text: str) -> Optional[str]:
    for pattern in _RUDE_PATTERNS:
        m = re.search(pattern, text)
        if m:
            return m.group(0)
    return None


def _apply_length(result: OutboundResult, max_chars: int) -> OutboundResult:
    if max_chars and result.text and len(result.text) > max_chars:
        result.text = truncate(result.text, max_chars)
    return result


__all__ = [
    "ComplianceGuard",
    "CompliancePattern",
    "Violation",
    "OutboundResult",
    "HandoffSignal",
    "similarity",
]
