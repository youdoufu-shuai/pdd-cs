# -*- coding: utf-8 -*-
"""回复决策链。

优先级（命中即返回，不再向下判断）：

    黑名单 → 入站过滤 → 频率风控 → 转人工信号 → 非营业时间
    → 关键词 → 商品专属 → AI → 默认回复 → 无匹配

这与 ChatGPT-On-CS 的决策顺序一致（其默认工作流为：转人工 → 关键词 → GPT →
默认回复 → 替换词），并按拼多多最新规则补上：

    - 转人工信号前移到关键词之前：投诉 / 售后 / 人身安全 / 大额订单优先给人工；
    - 所有出站文本统一过合规检查（导流 / 承诺 / 敏感词 / 绝对化用语）；
    - 重复话术自动多样化，避免拼多多对重复回复的限制；
    - 智能客服身份披露由上层统一拼接。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional

from ..config import Config
from ..constants import (
    ACTION_AI,
    ACTION_BLACKLISTED,
    ACTION_DEFAULT,
    ACTION_FILTERED,
    ACTION_GOODS,
    ACTION_KEYWORD,
    ACTION_NO_MATCH,
    ACTION_OFF_HOURS,
    ACTION_RISK_BLOCKED,
    ACTION_TRANSFER,
    REPLY_IMAGE,
    REPLY_NO_REPLY,
    REPLY_TEXT,
    REPLY_TRANSFER,
    RESULT_AI_REPLY,
    RESULT_AUTO_REPLY,
    RESULT_BLACKLISTED,
    RESULT_FILTERED,
    RESULT_HUMAN_TAKEOVER,
    RESULT_NO_MATCH,
    RESULT_NON_BUSINESS_HOURS,
    RESULT_RISK_PAUSED,
    RESULT_TRANSFER,
    RISK_DUPLICATE_REPLY,
    RISK_FREQUENCY_LIMIT,
)
from ..pdd.message import ChatEvent, CTX_GOODS_INQUIRY, CTX_GOODS_SPEC, CTX_IMAGE, CTX_ORDER_INFO, CTX_SYSTEM, CTX_WITHDRAW
from ..utils import (
    choose_reply,
    in_business_hours,
    max_similarity,
    now_beijing,
)
from .ai import AIReplyGenerator
from .compliance import ComplianceGuard, HandoffSignal, OutboundResult
from .rules import RuleSet

logger = logging.getLogger("pdd_cs.decision")


@dataclass
class RuntimeContext:
    """一次决策所需的运行时状态（由编排层从存储 / 跟踪器取出）。"""

    recent_replies: list[str] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)
    session_reply_times: list[datetime] = field(default_factory=list)
    shop_reply_times: list[datetime] = field(default_factory=list)
    order_amount: float = 0.0
    repeat_count: int = 0
    blacklisted: bool = False
    first_reply: bool = False
    off_hours_notified: bool = False
    last_order: dict[str, Any] = field(default_factory=dict)


@dataclass
class Decision:
    action: str
    reply_type: str = REPLY_NO_REPLY
    content: str = ""
    log_result: str = RESULT_NO_MATCH
    should_reply: bool = False
    reason: str = ""
    handoff: Optional[HandoffSignal] = None
    risk: Optional[dict[str, Any]] = None
    violations: list[str] = field(default_factory=list)
    rule_id: str = ""

    @property
    def is_transfer(self) -> bool:
        return self.action == ACTION_TRANSFER

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "reply_type": self.reply_type,
            "content": self.content,
            "log_result": self.log_result,
            "should_reply": self.should_reply,
            "reason": self.reason,
            "handoff": str(self.handoff) if self.handoff else None,
            "violations": self.violations,
            "rule_id": self.rule_id,
        }


class DecisionEngine:
    """规则 + 合规 + AI 的决策引擎。"""

    def __init__(
        self,
        config: Config,
        rules: RuleSet,
        guard: ComplianceGuard,
        ai: Optional[AIReplyGenerator] = None,
        *,
        knowledge=None,
    ) -> None:
        self.config = config
        self.rules = rules
        self.guard = guard
        self.ai = ai
        self.knowledge = knowledge

    # ------------------------------------------------------------------
    # 风控：频率限制
    # ------------------------------------------------------------------
    def check_frequency(
        self,
        *,
        session_times: list[datetime],
        shop_times: list[datetime],
        now: Optional[datetime] = None,
    ) -> Optional[dict[str, Any]]:
        """检查窗口内回复次数是否达到上限。"""
        risk = self.config.compliance.risk
        if risk.session_reply_limit <= 0 and risk.shop_reply_limit <= 0:
            return None
        now = now or now_beijing()
        window_start = now - timedelta(seconds=max(1, risk.window_seconds))

        def _count(times: list[datetime]) -> int:
            return sum(1 for t in times if t and t >= window_start)

        session_count = _count(session_times)
        if risk.session_reply_limit > 0 and session_count >= risk.session_reply_limit:
            return {
                "type": RISK_FREQUENCY_LIMIT,
                "scope": "session",
                "count": session_count,
                "limit": risk.session_reply_limit,
                "window_seconds": risk.window_seconds,
                "detail": f"单会话 {risk.window_seconds}s 内回复已达上限（{session_count}/{risk.session_reply_limit}）",
            }
        shop_count = _count(shop_times)
        if risk.shop_reply_limit > 0 and shop_count >= risk.shop_reply_limit:
            return {
                "type": RISK_FREQUENCY_LIMIT,
                "scope": "shop",
                "count": shop_count,
                "limit": risk.shop_reply_limit,
                "window_seconds": risk.window_seconds,
                "detail": f"单店铺 {risk.window_seconds}s 内回复已达上限（{shop_count}/{risk.shop_reply_limit}）",
            }
        return None

    # ------------------------------------------------------------------
    # 主决策
    # ------------------------------------------------------------------
    def decide(
        self,
        event: ChatEvent,
        ctx: Optional[RuntimeContext] = None,
        *,
        now: Optional[datetime] = None,
    ) -> Decision:
        ctx = ctx or RuntimeContext()
        now = now or now_beijing()

        # 0) 系统消息 / 撤回 / 非买家消息不处理
        if event.context_type in {CTX_SYSTEM, CTX_WITHDRAW} or event.role != "OTHER":
            return Decision(
                action=ACTION_NO_MATCH,
                log_result=RESULT_NO_MATCH,
                reason="非买家消息，忽略",
            )

        text = event.content or ""

        # 1) 黑名单
        if ctx.blacklisted:
            return Decision(
                action=ACTION_BLACKLISTED,
                log_result=RESULT_BLACKLISTED,
                reason="客户在黑名单中",
            )

        # 2) 入站过滤（广告 / 刷单 / 代运营等，不自动回复）
        filtered = self.rules.is_filtered(text, msg_type=event.context_type)
        if filtered:
            return Decision(
                action=ACTION_FILTERED,
                log_result=RESULT_FILTERED,
                reason=f"命中过滤规则: {filtered.note or filtered.condition_value}",
            )

        # 3) 频率风控
        risk = self.check_frequency(
            session_times=ctx.session_reply_times,
            shop_times=ctx.shop_reply_times,
            now=now,
        )
        if risk:
            return Decision(
                action=ACTION_RISK_BLOCKED,
                log_result=RESULT_RISK_PAUSED,
                reason=risk["detail"],
                risk=risk,
            )

        # 4) 转人工信号（投诉 / 售后 / 安全 / 法律 / 情绪 / 大额 / 人工请求）
        signal = self.guard.handoff_signal(
            text,
            order_amount=ctx.order_amount,
            repeat_count=ctx.repeat_count,
        )
        if signal:
            return Decision(
                action=ACTION_TRANSFER,
                reply_type=REPLY_TRANSFER,
                log_result=RESULT_TRANSFER,
                reason=str(signal),
                handoff=signal,
            )

        # 5) 非营业时间
        hours = self.config.business_hours
        if hours.enabled and not in_business_hours(now, hours.start, hours.end):
            if ctx.off_hours_notified:
                return Decision(
                    action=ACTION_NO_MATCH,
                    log_result=RESULT_NON_BUSINESS_HOURS,
                    reason="非营业时间（已提示过）",
                )
            content = hours.off_hours_reply
            if hours.off_hours_ai and not content:
                content = "您好，现在是非工作时间，客服上线后会第一时间回复您～"
            return Decision(
                action=ACTION_OFF_HOURS,
                reply_type=REPLY_TEXT,
                content=content,
                log_result=RESULT_NON_BUSINESS_HOURS,
                should_reply=bool(content),
                reason="非营业时间提示",
            )

        # 6) 关键词
        keyword_rule = self.rules.match_keyword(text)
        if keyword_rule:
            raw = choose_reply(keyword_rule.reply, use_random_tokens=True)
            decision = self._finalize_text(
                action=ACTION_KEYWORD,
                raw_text=raw,
                reply_type=keyword_rule.reply_type,
                log_result=RESULT_AUTO_REPLY,
                reason=f"命中关键词规则 {keyword_rule.id}",
                ctx=ctx,
                rule_id=keyword_rule.id,
            )
            if decision:
                return decision

        # 7) 商品专属回复
        goods_id = str(event.goods_info.get("goods_id") or "") if event.goods_info else ""
        goods_rule = self.rules.match_goods(goods_id)
        if goods_rule:
            raw = choose_reply(goods_rule.reply, use_random_tokens=True)
            decision = self._finalize_text(
                action=ACTION_GOODS,
                raw_text=raw,
                reply_type=goods_rule.reply_type,
                log_result=RESULT_AUTO_REPLY,
                reason=f"命中商品专属回复 goods_id={goods_id}",
                ctx=ctx,
                rule_id=f"goods-{goods_id}",
            )
            if decision:
                return decision

        # 8) AI 回复
        if self.ai and self.ai.ready and self.config.llm.enabled:
            decision = self._ai_decision(event, ctx)
            if decision:
                return decision
            logger.info("AI 未生成有效回复，继续降级到默认回复")

        # 9) 默认回复
        default_text = self.config.behavior.default_reply
        if default_text:
            return self._finalize_text(
                action=ACTION_DEFAULT,
                raw_text=choose_reply(default_text, use_random_tokens=True),
                reply_type=REPLY_TEXT,
                log_result=RESULT_AUTO_REPLY,
                reason="使用默认回复",
                ctx=ctx,
            ) or Decision(
                action=ACTION_NO_MATCH,
                log_result=RESULT_NO_MATCH,
                reason="默认回复未通过合规检查",
            )

        # 10) 无匹配：按配置交给人工 / 静默
        mode = self.config.behavior.no_match
        if mode == "human":
            return Decision(
                action=ACTION_TRANSFER,
                reply_type=REPLY_TRANSFER,
                log_result=RESULT_TRANSFER,
                reason="无可用自动回复，转人工",
                handoff=HandoffSignal("无匹配回复", "no_match", 5),
            )
        return Decision(action=ACTION_NO_MATCH, log_result=RESULT_NO_MATCH, reason="无可用回复")

    # ------------------------------------------------------------------
    # 文本收尾：合规检查 + 重复治理 + 长度
    # ------------------------------------------------------------------
    def _finalize_text(
        self,
        *,
        action: str,
        raw_text: str,
        reply_type: str,
        log_result: str,
        reason: str,
        ctx: RuntimeContext,
        rule_id: str = "",
    ) -> Optional[Decision]:
        if not raw_text:
            return None
        if reply_type == REPLY_IMAGE:
            return Decision(
                action=action,
                reply_type=REPLY_IMAGE,
                content=raw_text.strip(),
                log_result=log_result,
                should_reply=True,
                reason=reason,
                rule_id=rule_id,
            )

        enforced: OutboundResult = self.guard.enforce(
            raw_text, max_chars=self.config.behavior.max_reply_chars
        )
        # 替换词在合规检查之后仍可能命中；这里再跑一遍替换，确保发出的文本干净
        if enforced.text:
            enforced.text, _applied = self.rules.apply_replace(enforced.text)
            if _applied:
                logger.info("已执行替换词: %s", [r.pattern for r in _applied])
        violations = [f"{v.category}:{v.matched}" for v in enforced.violations]
        if enforced.blocked:
            logger.warning("回复被合规拦截（%s），升级为转人工: %s", action, enforced.summary())
            return Decision(
                action=ACTION_TRANSFER,
                reply_type=REPLY_TRANSFER,
                log_result=RESULT_TRANSFER,
                reason=f"待发送内容未通过平台规则检查（{reason}）",
                handoff=HandoffSignal("出站内容风险", "compliance", 3),
                violations=violations,
            )

        # 重复话术治理
        text = enforced.text
        if self.guard.is_duplicate(text, ctx.recent_replies):
            diversified = self.guard.diversify(
                text, ctx.recent_replies, max_chars=self.config.behavior.max_reply_chars
            )
            if diversified and diversified != text:
                logger.info("检测到重复话术，已做多样化处理")
                text = diversified
            if self.guard.is_duplicate(text, ctx.recent_replies):
                logger.warning("多样化后仍与历史话术高度相似（继续发送但已记录风控）")

        if enforced.needs_human:
            return Decision(
                action=ACTION_TRANSFER,
                reply_type=REPLY_TRANSFER,
                log_result=RESULT_TRANSFER,
                reason=f"内容涉及承诺/导流，改为转人工（{reason}）",
                handoff=HandoffSignal("出站内容风险", "compliance", 3),
                violations=violations,
            )

        return Decision(
            action=action,
            reply_type=REPLY_TEXT,
            content=text,
            log_result=log_result,
            should_reply=True,
            reason=reason,
            violations=violations,
            rule_id=rule_id,
        )

    # ------------------------------------------------------------------
    def _ai_decision(self, event: ChatEvent, ctx: RuntimeContext) -> Optional[Decision]:
        if self.ai is None:
            return None
        history = ctx.history
        extra = self._event_context_text(event)
        if ctx.last_order:
            order_text = " ".join(
                filter(
                    None,
                    [
                        f"历史订单={ctx.last_order.get('order_id')}" if ctx.last_order.get("order_id") else "",
                        f"商品={ctx.last_order.get('goods_name')}" if ctx.last_order.get("goods_name") else "",
                        f"规格={ctx.last_order.get('spec')}" if ctx.last_order.get("spec") else "",
                        f"售后状态={ctx.last_order.get('after_sales_status')}"
                        if ctx.last_order.get("after_sales_status") else "",
                    ],
                )
            )
            if order_text:
                extra = f"{extra}；{order_text}" if extra else order_text
        handoff_hint = bool(
            self.guard.handoff_signal(event.content or "", order_amount=ctx.order_amount)
        )
        try:
            content = self.ai.generate(
                question=event.content or "",
                history=history,
                nickname=event.nickname,
                extra_context=extra,
                handoff_hint=handoff_hint,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("AI 生成异常: %s", exc)
            return None
        if not content:
            return None
        return self._finalize_text(
            action=ACTION_AI,
            raw_text=content,
            reply_type=REPLY_TEXT,
            log_result=RESULT_AI_REPLY,
            reason="AI 生成回复",
            ctx=ctx,
        )

    @staticmethod
    def _event_context_text(event: ChatEvent) -> str:
        parts: list[str] = []
        if event.context_type == CTX_ORDER_INFO and event.order_info:
            info = event.order_info
            parts.append(
                "订单咨询：" + " ".join(
                    filter(
                        None,
                        [
                            f"订单号={info.get('order_id')}" if info.get("order_id") else "",
                            f"商品={info.get('goods_name')}" if info.get("goods_name") else "",
                            f"规格={info.get('spec')}" if info.get("spec") else "",
                            f"售后状态={info.get('after_sales_status')}" if info.get("after_sales_status") else "",
                        ],
                    )
                )
            )
        if event.context_type in {CTX_GOODS_INQUIRY, CTX_GOODS_SPEC} and event.goods_info:
            info = event.goods_info
            parts.append(
                "商品咨询：" + " ".join(
                    filter(
                        None,
                        [
                            f"商品ID={info.get('goods_id')}" if info.get("goods_id") else "",
                            f"商品名={info.get('goods_name')}" if info.get("goods_name") else "",
                            f"规格={info.get('spec')}" if info.get("spec") else "",
                        ],
                    )
                )
            )
        if event.context_type == CTX_IMAGE:
            parts.append("客户发来一张图片，看不到内容，请按文字沟通并请客户描述问题。")
        return "；".join(parts)


__all__ = ["DecisionEngine", "Decision", "RuntimeContext"]
