# -*- coding: utf-8 -*-
"""单个拼多多店铺的接待工作单元（多店铺架构的核心）。

每个启用的店铺有一个 ShopWorker：
    - 自己的 Cookie、API 客户端、WebSocket 长连接；
    - 自己的决策引擎（店铺级配置覆盖）与 SLA 跟踪；
    - 共用全局规则、知识库、SQLite 存储与告警通知。

所有落库操作都带 shop_id，面板因此可以按店铺聚合与切换。
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import timedelta
from typing import Any, Optional

from ..alerts import (
    EVENT_CONNECTION_ERROR,
    EVENT_RISK_BLOCKED,
    EVENT_SEND_FAILED,
    EVENT_SESSION_EXPIRED,
    EVENT_SLA_OVERDUE,
    EVENT_TRANSFER,
    EVENT_TRANSFER_FAILED,
    AlertNotifier,
)
from ..config import Config, ShopProfile
from ..constants import (
    ACTION_NO_MATCH,
    ACTION_OFF_HOURS,
    ACTION_RISK_BLOCKED,
    ACTION_TRANSFER,
    RESULT_FAILED,
    RESULT_HUMAN_TAKEOVER,
    RESULT_RISK_PAUSED,
    RESULT_TRANSFER,
)
from ..pdd.api import PDDClient, PDDError, PDDSendRejected, PDDSessionExpired
from ..pdd.auth import CookieError, CookieStore, cookie_age_hours
from ..pdd.channel import ConnState, PDDChannel
from ..pdd.message import ChatEvent, CTX_ORDER_INFO, CTX_SYSTEM, CTX_WITHDRAW
from ..store import Store
from ..utils import iso, jitter, now_beijing, safe_float
from .ai import AIReplyGenerator
from .compliance import ComplianceGuard
from .decision import Decision, DecisionEngine, RuntimeContext
from .knowledge import KnowledgeBase
from .rules import RuleSet
from .sla import SLATracker

logger = logging.getLogger("pdd_cs.worker")


class ShopStartError(RuntimeError):
    """单个店铺启动失败（其它店铺可继续运行）。"""

    def __init__(self, shop_id: str, message: str):
        super().__init__(message)
        self.shop_id = shop_id


class ShopWorker:
    """一个拼多多店铺的完整接待单元。"""

    def __init__(
        self,
        base_config: Config,
        profile: ShopProfile,
        *,
        store: Store,
        rules: RuleSet,
        knowledge: Optional[KnowledgeBase],
        notifier: Optional[AlertNotifier] = None,
    ) -> None:
        self.base_config = base_config
        self.profile = profile
        self.config: Config = base_config.for_shop(profile)
        self.shop_id = profile.id
        self.shop_name = profile.display_name
        self.store = store
        self.rules = rules
        self.knowledge = knowledge
        self.notifier = notifier

        self.sla = SLATracker(
            first_response_seconds=self.config.compliance.sla.first_response_seconds,
            warn_seconds=self.config.compliance.sla.warn_seconds,
        )
        self.guard = ComplianceGuard.from_file(self.config.compliance, self.config.rules.compliance_file)
        self.ai = AIReplyGenerator(self.config, knowledge)
        self.decision = DecisionEngine(self.config, rules, self.guard, self.ai, knowledge=knowledge)

        self.cookie_store = CookieStore(self.config.pdd.cookie_file)
        self.api: Optional[PDDClient] = None
        self.channel: Optional[PDDChannel] = None

        self.state = "stopped"
        self.last_error = ""
        self.started_at = now_beijing()
        self.cookie_summary = ""
        self.shop_display_name = profile.display_name

        self._locks: dict[str, asyncio.Lock] = {}
        self._sequence: dict[str, int] = {}
        self._off_hours_notified: set[str] = set()
        self._assign_cs_cache: list[dict[str, str]] = []
        self._background: set[asyncio.Task] = set()
        self._sla_alerted: dict[str, float] = {}

    # ------------------------------------------------------------------
    # 启动 / 停止
    # ------------------------------------------------------------------
    async def start(self) -> None:
        try:
            bundle = self.cookie_store.load()
        except CookieError as exc:
            raise ShopStartError(self.shop_id, f"读取 Cookie 失败：{exc}") from exc
        if not bundle or not bundle.cookies:
            raise ShopStartError(
                self.shop_id,
                f"店铺「{self.shop_name}」尚未登录，请运行：python -m pdd_cs login --shop {self.shop_id}",
            )

        self.api = PDDClient(
            bundle.cookies,
            base_url=self.config.pdd.base_url,
            user_agent=self.config.pdd.user_agent,
            timeout=self.config.pdd.request_timeout_seconds,
            max_retries=self.config.pdd.max_retries,
            min_send_interval=self.config.compliance.risk.min_send_interval_seconds,
        )

        try:
            info = await asyncio.to_thread(self.api.validate_session)
            name = str(info.get("mall_name") or info.get("mallName") or info.get("username") or "")
            if name:
                self.shop_display_name = name
                self.config.pdd.shop_hint = name
            self.store.set_kv(f"shop:{self.shop_id}:name", self.shop_display_name)
            self.store.set_kv(f"shop:{self.shop_id}:last_login_check", iso(now_beijing()))
        except PDDSessionExpired as exc:
            await self._notify(EVENT_SESSION_EXPIRED, "登录态已过期", str(exc))
            raise ShopStartError(self.shop_id, f"店铺「{self.shop_name}」登录态已失效，请重新登录") from exc
        except Exception as exc:  # noqa: BLE001
            logger.warning("[%s] 登录态校验失败（继续尝试连接长连接）: %s", self.shop_id, exc)
            self.last_error = f"登录态校验失败: {exc}"

        age_hours = cookie_age_hours(bundle.saved_at)
        self.cookie_summary = (
            f"已保存 {len(bundle.cookies)} 项 · "
            + (f"{age_hours:.1f} 小时前更新" if age_hours is not None else "保存时间未知")
        )
        self.store.set_kv(f"shop:{self.shop_id}:cookie_summary", self.cookie_summary)

        self.channel = PDDChannel(
            self.api,
            self.config.pdd,
            on_events=self.handle_events,
            on_state=self._on_channel_state,
        )
        await self.channel.start()
        self.state = "starting"
        if self.config.pdd.poll_conversations:
            self._spawn(self._sync_conversations())
        logger.info("[%s] 店铺工作单元已启动：%s", self.shop_id, self.shop_display_name)

    async def stop(self) -> None:
        for task in list(self._background):
            task.cancel()
        if self.channel:
            await self.channel.stop()
        self.state = "stopped"
        logger.info("[%s] 店铺工作单元已停止", self.shop_id)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _sync_conversations(self) -> None:
        if not self.api:
            return
        try:
            conversations = await asyncio.to_thread(self.api.get_recent_conversations)
            for conv in conversations:
                uid = conv.get("uid")
                if not uid:
                    continue
                if not self.store.get_conversation(str(uid), shop_id=self.shop_id):
                    self.store.record_inbound(
                        uid=str(uid),
                        nickname=str(conv.get("nickname") or ""),
                        role="OTHER",
                        context_type="sync",
                        content=str(conv.get("last_message") or ""),
                        shop_id=self.shop_id,
                    )
            logger.info("[%s] 已同步最近会话 %d 个", self.shop_id, len(conversations))
        except Exception as exc:  # noqa: BLE001
            logger.info("[%s] 同步最近会话失败（不影响自动回复）: %s", self.shop_id, exc)

    async def _on_channel_state(self, state: str, detail: str) -> None:
        self.state = state
        self.store.set_kv(
            f"shop:{self.shop_id}:channel_state",
            {"state": state, "detail": detail, "at": iso(now_beijing())},
        )
        if state == ConnState.ERROR.value:
            self.last_error = detail
            await self._notify(
                EVENT_CONNECTION_ERROR,
                "长连接异常",
                f"店铺：{self.shop_display_name}\n原因：{detail}",
            )

    # ------------------------------------------------------------------
    # 告警
    # ------------------------------------------------------------------
    async def _notify(
        self,
        event: str,
        title: str,
        text: str,
        *,
        dedupe_key: str = "",
        force: bool = False,
    ) -> None:
        if self.notifier is None:
            return
        await asyncio.to_thread(
            self.notifier.notify,
            event,
            title,
            text,
            shop_id=self.shop_id,
            shop_name=self.shop_display_name,
            dedupe_key=dedupe_key or f"{event}:{self.shop_id}",
            force=force,
        )

    def check_sla_alerts(self, *, interval_seconds: float = 120.0) -> int:
        """检查超时未回会话并告警（由服务层周期调用），返回新增告警数。"""
        if self.notifier is None or not self.notifier.should_send(EVENT_SLA_OVERDUE):
            return 0
        import time as _time

        count = 0
        now = _time.monotonic()
        for stat in self.sla.overdue():
            last = self._sla_alerted.get(stat.uid, 0.0)
            if now - last < max(60.0, interval_seconds):
                continue
            self._sla_alerted[stat.uid] = now
            self.notifier.notify(
                EVENT_SLA_OVERDUE,
                "客户等待超时",
                f"店铺：{self.shop_display_name}\n客户：{stat.nickname or stat.uid}\n"
                f"等待：{int(stat.waiting_seconds or 0)} 秒",
                shop_id=self.shop_id,
                shop_name=self.shop_display_name,
                dedupe_key=f"sla:{self.shop_id}:{stat.uid}",
            )
            count += 1
        return count

    # ------------------------------------------------------------------
    # 消息入口
    # ------------------------------------------------------------------
    async def handle_events(self, events: list[ChatEvent]) -> None:
        for event in events:
            try:
                await self._handle_event(event)
            except Exception as exc:  # noqa: BLE001
                logger.exception("[%s] 处理消息异常: %s", self.shop_id, exc)
                self.last_error = str(exc)

    async def _handle_event(self, event: ChatEvent) -> None:
        if event.context_type in {CTX_SYSTEM, CTX_WITHDRAW} or not event.uid:
            return
        if event.msg_id and self.store.is_duplicate_message(
            event.uid, event.msg_id, shop_id=self.shop_id
        ):
            logger.info("[%s] 忽略重复消息 msg_id=%s", self.shop_id, event.msg_id)
            return

        if event.role == "SELF":
            await self._handle_self_message(event)
            return
        if event.role != "OTHER":
            return

        self.store.record_inbound(
            uid=event.uid,
            nickname=event.nickname,
            role=event.role,
            context_type=event.context_type,
            content=event.content,
            msg_id=event.msg_id,
            platform_time=event.timestamp,
            shop_id=self.shop_id,
        )
        if event.context_type == CTX_ORDER_INFO and event.order_info:
            self.store.set_last_order(event.uid, event.order_info, shop_id=self.shop_id)
        self.sla.on_inbound(event.uid, event.nickname)

        if self.sla.is_human_active(event.uid):
            self.store.mark_result(event.uid, RESULT_HUMAN_TAKEOVER, shop_id=self.shop_id)
            logger.info("[%s] 人工接管中，跳过自动回复: uid=%s", self.shop_id, event.uid)
            return

        lock = self._locks.setdefault(event.uid, asyncio.Lock())
        async with lock:
            self._sequence[event.uid] = self._sequence.get(event.uid, 0) + 1
            my_seq = self._sequence[event.uid]
            await asyncio.sleep(0.6)
            if self._sequence.get(event.uid, 0) != my_seq:
                logger.info("[%s] 会话有新消息，合并处理: uid=%s", self.shop_id, event.uid)
                return
            ctx = self._build_context(event)
            decision = self.decision.decide(event, ctx)
            await self._execute(event, decision)

    async def _handle_self_message(self, event: ChatEvent) -> None:
        if self._is_own_echo(event.uid, event.content):
            return
        self.store.record_outbound(
            uid=event.uid,
            content=event.content or "[客服消息]",
            action="human",
            result="human",
            shop_id=self.shop_id,
        )
        await self._activate_human_takeover(event.uid, reason="检测到人工客服回复")

    def _is_own_echo(self, uid: str, content: str) -> bool:
        if not content:
            return True
        recent = self.store.get_messages(uid, limit=8, shop_id=self.shop_id)
        now = now_beijing()
        for msg in reversed(recent):
            if msg.get("direction") != "out":
                continue
            if str(msg.get("content") or "").strip() != content.strip():
                continue
            try:
                from datetime import datetime

                when = datetime.strptime(
                    msg.get("created_at") or "", "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=now.tzinfo)
            except (ValueError, TypeError):
                return True
            if (now - when).total_seconds() <= 180:
                return True
        return False

    def resume_ai(self, uid: str) -> None:
        """清除某会话的人工接管状态，下一条客户消息重新交给 AI。"""
        self.store.set_human_takeover(uid, None, shop_id=self.shop_id)
        self.sla.resume_auto(uid)
        logger.info("[%s] 已恢复 AI 自动回复：uid=%s", self.shop_id, uid)

    # ------------------------------------------------------------------
    # 决策上下文
    # ------------------------------------------------------------------
    def _build_context(self, event: ChatEvent) -> RuntimeContext:
        uid = event.uid
        order_amount = 0.0
        if event.context_type == CTX_ORDER_INFO:
            order_amount = safe_float(event.order_info.get("amount"), 0.0)
        repeat_count = self.store.count_repeat_asks(
            uid, event.content or "", shop_id=self.shop_id
        )
        conv = self.store.get_conversation(uid, shop_id=self.shop_id)
        first_reply = not conv or int(conv.get("outbound_count") or 0) == 0
        blacklist = self.rules.is_blacklisted(uid, event.content or "")
        window = timedelta(seconds=max(1, self.config.compliance.risk.window_seconds))
        since = now_beijing() - window

        last_order = (conv or {}).get("last_order") or {}
        if event.context_type == CTX_ORDER_INFO and event.order_info:
            last_order = event.order_info
        return RuntimeContext(
            recent_replies=self.store.recent_replies(
                uid, limit=self.config.compliance.anti_duplicate.history_size, shop_id=self.shop_id
            ),
            history=self.store.history_for_ai(
                uid, limit=self.config.llm.max_history_messages, shop_id=self.shop_id
            ),
            session_reply_times=self.store.reply_times(uid=uid, since=since, shop_id=self.shop_id),
            shop_reply_times=self.store.reply_times(since=since, shop_id=self.shop_id),
            order_amount=order_amount,
            repeat_count=repeat_count,
            blacklisted=bool(blacklist),
            first_reply=first_reply,
            off_hours_notified=uid in self._off_hours_notified,
            last_order=last_order,
        )

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------
    async def _execute(self, event: ChatEvent, decision: Decision) -> None:
        uid = event.uid
        if decision.action == ACTION_TRANSFER:
            if decision.violations:
                self.store.log_risk(
                    uid, "content_blocked", "；".join(decision.violations), shop_id=self.shop_id
                )
                await self._notify(
                    EVENT_RISK_BLOCKED,
                    "内容被合规拦截",
                    f"店铺：{self.shop_display_name}\n客户：{event.nickname or uid}\n"
                    f"原因：{'；'.join(decision.violations)[:200]}",
                )
            await self._do_transfer(uid, decision, question=event.content or "")
            return
        if decision.action == ACTION_RISK_BLOCKED:
            self.store.log_risk(uid, "frequency_limit", decision.reason, shop_id=self.shop_id)
            self.store.mark_result(uid, RESULT_RISK_PAUSED, shop_id=self.shop_id)
            logger.warning("[%s] 风控暂停自动回复 uid=%s: %s", self.shop_id, uid, decision.reason)
            return
        if decision.action == ACTION_NO_MATCH:
            self.store.mark_result(uid, decision.log_result, shop_id=self.shop_id)
            return
        if decision.action == "blacklisted":
            self.store.mark_result(uid, "blacklisted", shop_id=self.shop_id)
            logger.info("[%s] 客户命中黑名单，不自动回复: uid=%s", self.shop_id, uid)
            return
        if decision.should_reply and decision.content:
            if decision.action == ACTION_OFF_HOURS:
                self._off_hours_notified.add(uid)
            await self._do_reply(uid, decision, question=event.content or "")
            return
        self.store.mark_result(uid, decision.log_result, shop_id=self.shop_id)

    _GOODS_LINK_RE = re.compile(
        r"https?://(?:(?:mobile\.)?yangkeduo\.com|(?:mobile\.)?pinduoduo\.com)/"
        r"goods(?:\.html|2\.html)?\?goods_id=(\d+)"
    )

    @classmethod
    def _extract_goods_id(cls, text: str) -> str:
        matched = cls._GOODS_LINK_RE.search(text or "")
        return matched.group(1) if matched else ""

    @classmethod
    def _strip_goods_link(cls, text: str) -> str:
        return cls._GOODS_LINK_RE.sub("", text or "").strip(" \n：:，,。.")

    async def _do_reply(self, uid: str, decision: Decision, *, question: str = "") -> None:
        if not self.api:
            return
        content = decision.content
        conv = self.store.get_conversation(uid, shop_id=self.shop_id)
        first_reply = not conv or int(conv.get("outbound_count") or 0) == 0
        if decision.reply_type != "IMAGE":
            content = self.guard.apply_disclosure(content, first_reply=first_reply)

        goods_id = ""
        text_content = content
        if decision.reply_type != "IMAGE" and self.config.behavior.send_goods_card:
            goods_id = self._extract_goods_id(content)
            if goods_id:
                text_content = self._strip_goods_link(content) or "给您找到这款商品，您看下是不是需要的～"

        if decision.action != ACTION_OFF_HOURS:
            delay = jitter(
                self.config.behavior.reply_delay_seconds,
                self.config.behavior.reply_delay_jitter,
            )
            if delay > 0:
                await asyncio.sleep(delay)

        started = now_beijing()
        card_sent = False
        try:
            if decision.reply_type == "IMAGE":
                await asyncio.to_thread(self.api.send_image, uid, decision.content)
            else:
                if goods_id:
                    try:
                        await asyncio.to_thread(self.api.send_goods_card, uid, goods_id)
                        card_sent = True
                        logger.info(
                            "[%s] 已发送商品卡片 uid=%s goods_id=%s",
                            self.shop_id, uid, goods_id,
                        )
                    except PDDSendRejected as exc:
                        # 没有 anti_content 或平台拒绝时，降级为原来的文字链接
                        logger.warning(
                            "[%s] 商品卡片发送失败，降级为文字链接 uid=%s: %s",
                            self.shop_id, uid, exc,
                        )
                        text_content = content
                    except PDDSessionExpired:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "[%s] 商品卡片异常，降级为文字链接 uid=%s: %s",
                            self.shop_id, uid, exc,
                        )
                        text_content = content
                if text_content.strip():
                    await asyncio.to_thread(self.api.send_text, uid, text_content)
                if card_sent:
                    content = f"【商品卡片 goods_id={goods_id}】\n{text_content}"
                else:
                    content = text_content
        except PDDSendRejected as exc:
            elapsed = int((now_beijing() - started).total_seconds() * 1000)
            self._record_reply(
                uid, content, decision, status="rejected", error=str(exc),
                latency_ms=elapsed, question=question,
            )
            self.store.log_risk(uid, "send_rejected", str(exc), shop_id=self.shop_id)
            await self._notify(
                EVENT_SEND_FAILED,
                "消息发送被平台拒绝",
                f"店铺：{self.shop_display_name}\n客户：{uid}\n原因：{exc}",
            )
            logger.warning("[%s] 消息被平台拒绝 uid=%s: %s", self.shop_id, uid, exc)
            await self._send_fallback(uid, decision, question=question)
            return
        except PDDSessionExpired as exc:
            self.last_error = str(exc)
            self.store.log_risk(uid, "session_expired", str(exc), shop_id=self.shop_id)
            await self._notify(
                EVENT_SESSION_EXPIRED,
                "登录态已失效",
                f"店铺：{self.shop_display_name}\n请重新运行：python -m pdd_cs login --shop {self.shop_id}",
            )
            logger.error("[%s] 登录态失效，需要重新登录: %s", self.shop_id, exc)
            return
        except (PDDError, Exception) as exc:  # noqa: BLE001
            elapsed = int((now_beijing() - started).total_seconds() * 1000)
            self._record_reply(
                uid, content, decision, status="failed", error=str(exc),
                latency_ms=elapsed, question=question,
            )
            await self._notify(
                EVENT_SEND_FAILED,
                "消息发送失败",
                f"店铺：{self.shop_display_name}\n客户：{uid}\n原因：{exc}",
            )
            logger.error("[%s] 发送失败 uid=%s: %s", self.shop_id, uid, exc)
            return

        elapsed = int((now_beijing() - started).total_seconds() * 1000)
        self._record_reply(
            uid, content, decision, status="sent", latency_ms=elapsed, question=question
        )
        if decision.violations:
            self.store.log_risk(
                uid, "content_rewritten", "；".join(decision.violations), shop_id=self.shop_id
            )
        self.sla.on_outbound(uid, auto=True, last_result=decision.log_result)
        if self.channel:
            self.channel.stats.sent_replies += 1
        logger.info("[%s] 已回复 uid=%s [%s] %s", self.shop_id, uid, decision.action, content[:80])

    def _record_reply(
        self,
        uid: str,
        content: str,
        decision: Decision,
        *,
        status: str,
        error: str = "",
        latency_ms: int = 0,
        question: str = "",
    ) -> None:
        self.store.record_outbound(
            uid=uid,
            content=content,
            action=decision.action,
            result=decision.log_result,
            status=status,
            error=error,
            latency_ms=latency_ms,
            shop_id=self.shop_id,
            rule_id=decision.rule_id,
            reason=decision.reason,
            question=question,
        )

    async def _send_fallback(self, uid: str, decision: Decision, *, question: str = "") -> None:
        if not self.api:
            return
        fallback = self.guard.fallbacks.get(
            "send_rejected", "您好，您的问题已收到，我帮您转人工客服跟进，请稍等～"
        )
        try:
            await asyncio.to_thread(self.api.send_text, uid, fallback)
            self.store.record_outbound(
                uid=uid,
                content=fallback,
                action=decision.action,
                result=decision.log_result,
                status="sent",
                shop_id=self.shop_id,
                rule_id=decision.rule_id,
                reason=decision.reason,
                question=question,
            )
            self.sla.on_outbound(uid, auto=True, last_result=decision.log_result)
        except Exception as exc:  # noqa: BLE001
            logger.error("[%s] 兜底回复也失败 uid=%s: %s", self.shop_id, uid, exc)
            self.store.mark_result(uid, RESULT_FAILED, shop_id=self.shop_id)

    # ------------------------------------------------------------------
    # 转人工
    # ------------------------------------------------------------------
    async def _do_transfer(self, uid: str, decision: Decision, *, question: str = "") -> None:
        reason = decision.reason or "转人工"
        logger.info("[%s] 转人工 uid=%s 原因=%s", self.shop_id, uid, reason)
        notify = self.config.compliance.human_handoff.notify_message
        if notify:
            checked = self.guard.enforce(notify, max_chars=self.config.behavior.max_reply_chars)
            notify = checked.text if not checked.blocked else "已为您转接人工客服，请稍等片刻～"

        if notify and self.api:
            try:
                await asyncio.to_thread(self.api.send_text, uid, notify)
                self.store.record_outbound(
                    uid=uid,
                    content=notify,
                    action="transfer",
                    result="transfer_notice",
                    status="sent",
                    shop_id=self.shop_id,
                    reason=reason,
                    question=question,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("[%s] 转人工提示发送失败: %s", self.shop_id, exc)

        moved = False
        error = ""
        if self.api:
            try:
                cs_list = await self._get_assign_cs()
                target = self._pick_cs(cs_list)
                if target:
                    await asyncio.to_thread(
                        self.api.move_conversation,
                        uid,
                        target["cs_uid"],
                        self.config.compliance.human_handoff.no_reason_remark,
                    )
                    moved = True
                    logger.info(
                        "[%s] 已转接人工客服 %s(%s)",
                        self.shop_id, target.get("cs_name"), target.get("cs_uid"),
                    )
                else:
                    error = "没有可用的人工客服账号（请确认已在拼多多后台添加客服子账号）"
            except PDDSessionExpired as exc:
                error = str(exc)
                self.last_error = error
                await self._notify(
                    EVENT_SESSION_EXPIRED,
                    "登录态已失效",
                    f"店铺：{self.shop_display_name}\n请重新登录后再转人工",
                )
            except Exception as exc:  # noqa: BLE001
                error = str(exc)

        if moved:
            self.store.record_outbound(
                uid=uid,
                content=f"[转人工] {reason}",
                action="transfer",
                result=RESULT_TRANSFER,
                status="sent",
                shop_id=self.shop_id,
                reason=reason,
                question=question,
            )
            self.sla.pause_for_human(uid, self.config.behavior.human_takeover_minutes)
            self.store.set_human_takeover(
                uid,
                now_beijing() + timedelta(minutes=self.config.behavior.human_takeover_minutes),
                shop_id=self.shop_id,
            )
            await self._notify(
                EVENT_TRANSFER,
                "已转人工",
                f"店铺：{self.shop_display_name}\n客户：{uid}\n原因：{reason}",
                dedupe_key=f"transfer:{self.shop_id}:{uid}",
            )
        else:
            self.store.record_outbound(
                uid=uid,
                content=f"[转人工失败] {reason}",
                action="transfer",
                result=RESULT_TRANSFER,
                status="failed",
                error=error,
                shop_id=self.shop_id,
                reason=reason,
                question=question,
            )
            self.store.log_risk(
                uid, "transfer_failed", f"{reason}；{error}", shop_id=self.shop_id
            )
            await self._notify(
                EVENT_TRANSFER_FAILED,
                "转人工失败",
                f"店铺：{self.shop_display_name}\n客户：{uid}\n原因：{reason}\n错误：{error}",
                dedupe_key=f"transfer_failed:{self.shop_id}:{uid}",
            )
            if self.config.compliance.human_handoff.fallback_to_manual:
                self.sla.pause_for_human(uid, self.config.behavior.human_takeover_minutes)
                self.store.set_human_takeover(
                    uid,
                    now_beijing() + timedelta(minutes=self.config.behavior.human_takeover_minutes),
                    shop_id=self.shop_id,
                )
                logger.warning(
                    "[%s] 转人工失败，已暂停该会话自动回复 %s 分钟: %s",
                    self.shop_id, self.config.behavior.human_takeover_minutes, error,
                )
        self.store.mark_result(uid, RESULT_TRANSFER, shop_id=self.shop_id)

    async def _get_assign_cs(self) -> list[dict[str, str]]:
        if self._assign_cs_cache:
            return self._assign_cs_cache
        if not self.api:
            return []
        try:
            self._assign_cs_cache = await asyncio.to_thread(self.api.get_assign_cs_list)
        except Exception as exc:  # noqa: BLE001
            logger.info("[%s] 获取人工客服列表失败: %s", self.shop_id, exc)
            self._assign_cs_cache = []
        return self._assign_cs_cache

    def _pick_cs(self, cs_list: list[dict[str, str]]) -> Optional[dict[str, str]]:
        if not cs_list:
            return None
        wanted = self.config.compliance.human_handoff.assign_cs_name
        if wanted:
            for item in cs_list:
                if wanted in (item.get("cs_name") or "") or wanted == item.get("cs_uid"):
                    return item
        return cs_list[0]

    async def _activate_human_takeover(self, uid: str, *, reason: str) -> None:
        minutes = self.config.behavior.human_takeover_minutes
        if minutes <= 0:
            return
        self.sla.pause_for_human(uid, minutes)
        self.store.set_human_takeover(
            uid, now_beijing() + timedelta(minutes=minutes), shop_id=self.shop_id
        )
        self.store.mark_result(uid, RESULT_HUMAN_TAKEOVER, shop_id=self.shop_id)
        logger.info("[%s] 人工接管 %s 分钟 uid=%s（%s）", self.shop_id, minutes, uid, reason)

    # ------------------------------------------------------------------
    # 状态 / 测试台
    # ------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        channel_status = self.channel.status_dict() if self.channel else {"state": self.state}
        snapshot = self.sla.snapshot()
        overdue = [
            {
                "shop_id": self.shop_id,
                "uid": stat.uid,
                "nickname": stat.nickname,
                "waiting_seconds": round(stat.waiting_seconds or 0, 1),
            }
            for stat in self.sla.overdue()
        ]
        return {
            "shop_id": self.shop_id,
            "shop_name": self.shop_display_name,
            "enabled": self.profile.enabled,
            "state": channel_status.get("state", self.state),
            "channel": channel_status,
            "started_at": iso(self.started_at),
            "cookie": self.cookie_summary,
            "last_error": self.last_error,
            "sla": snapshot,
            "today": self.store.today_stats(shop_id=self.shop_id),
            "overdue": overdue,
            "llm": {
                "enabled": bool(self.ai and self.ai.ready),
                "model": self.config.llm.model if self.config.llm.ready else "",
                "calls": self.ai.client.call_count if (self.ai and self.ai.client) else 0,
                "errors": self.ai.client.error_count if (self.ai and self.ai.client) else 0,
            },
            "rules": self.rules.stats(),
            "business_hours": {
                "enabled": self.config.business_hours.enabled,
                "start": self.config.business_hours.start,
                "end": self.config.business_hours.end,
            },
            "policy": {
                "first_response_seconds": self.config.compliance.sla.first_response_seconds,
                "warn_seconds": self.config.compliance.sla.warn_seconds,
                "duplicate_threshold": self.config.compliance.anti_duplicate.min_unique_ratio,
                "session_reply_limit": self.config.compliance.risk.session_reply_limit,
                "window_seconds": self.config.compliance.risk.window_seconds,
                "human_takeover_minutes": self.config.behavior.human_takeover_minutes,
                "disclosure_enabled": self.config.compliance.disclosure.enabled,
            },
        }

    def simulate(self, text: str, *, uid: str = "simulate-user") -> dict[str, Any]:
        event = ChatEvent(
            uid=uid, nickname="测试客户", role="OTHER", content=text, context_type="text"
        )
        ctx = self._build_context(event)
        decision = self.decision.decide(event, ctx)
        result = decision.to_dict()
        result["compliance"] = (
            self.guard.check_outbound(decision.content or "").summary()
            if decision.content
            else "无内容"
        )
        result["shop_id"] = self.shop_id
        result["shop_name"] = self.shop_display_name
        return result


__all__ = ["ShopWorker", "ShopStartError"]
