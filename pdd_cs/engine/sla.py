# -*- coding: utf-8 -*-
"""响应时效（SLA）统计。

拼多多对客服有响应时长类考核（具体口径与达标线以商家后台当日规则为准），
自动客服最容易出问题的地方是：机器人回了、但客户问题没被真正接住，或者
人工接手前的那几分钟被漏掉。本模块负责：

    - 记录每个会话的首响时间、平均响应、3 分钟达标率；
    - 找出「客户还在等」的会话，供面板告警；
    - 人工接管（human_takeover）暂停自动回复的时间窗。

所有时间统一用北京时间。
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from ..utils import iso, now_beijing


@dataclass
class ConversationStat:
    uid: str
    nickname: str = ""
    first_in_at: Optional[datetime] = None
    first_reply_at: Optional[datetime] = None
    last_in_at: Optional[datetime] = None
    last_reply_at: Optional[datetime] = None
    inbound_count: int = 0
    outbound_count: int = 0
    auto_reply_count: int = 0
    human_takeover_until: Optional[datetime] = None
    pending_since: Optional[datetime] = None
    response_seconds: list[float] = field(default_factory=list)
    last_result: str = ""

    @property
    def first_response_seconds(self) -> Optional[float]:
        if self.first_in_at and self.first_reply_at:
            return max(0.0, (self.first_reply_at - self.first_in_at).total_seconds())
        return None

    @property
    def waiting_seconds(self) -> Optional[float]:
        if self.pending_since:
            return max(0.0, (now_beijing() - self.pending_since).total_seconds())
        return None

    @property
    def human_active(self) -> bool:
        return bool(self.human_takeover_until and self.human_takeover_until > now_beijing())

    def to_dict(self) -> dict:
        return {
            "uid": self.uid,
            "nickname": self.nickname,
            "first_in_at": iso(self.first_in_at) if self.first_in_at else None,
            "first_reply_at": iso(self.first_reply_at) if self.first_reply_at else None,
            "last_in_at": iso(self.last_in_at) if self.last_in_at else None,
            "last_reply_at": iso(self.last_reply_at) if self.last_reply_at else None,
            "inbound_count": self.inbound_count,
            "outbound_count": self.outbound_count,
            "auto_reply_count": self.auto_reply_count,
            "first_response_seconds": round(self.first_response_seconds, 1)
            if self.first_response_seconds is not None
            else None,
            "waiting_seconds": round(self.waiting_seconds, 1) if self.waiting_seconds is not None else None,
            "human_active": self.human_active,
            "last_result": self.last_result,
        }


class SLATracker:
    """会话级响应时效跟踪（内存态，重启后由 SQLite 中的历史消息恢复统计）。"""

    def __init__(self, *, first_response_seconds: float = 180.0, warn_seconds: float = 120.0):
        self.first_response_seconds = first_response_seconds
        self.warn_seconds = warn_seconds
        self._stats: dict[str, ConversationStat] = {}
        self._finished_response: list[float] = []

    # ------------------------------------------------------------------
    def get(self, uid: str) -> ConversationStat:
        stat = self._stats.get(uid)
        if stat is None:
            stat = ConversationStat(uid=uid)
            self._stats[uid] = stat
        return stat

    def on_inbound(self, uid: str, nickname: str = "", when: Optional[datetime] = None) -> ConversationStat:
        when = when or now_beijing()
        stat = self.get(uid)
        if nickname and not stat.nickname:
            stat.nickname = nickname
        stat.inbound_count += 1
        stat.last_in_at = when
        if stat.first_in_at is None:
            stat.first_in_at = when
        stat.pending_since = when
        return stat

    def on_outbound(
        self,
        uid: str,
        *,
        auto: bool = True,
        when: Optional[datetime] = None,
        last_result: str = "",
    ) -> ConversationStat:
        when = when or now_beijing()
        stat = self.get(uid)
        stat.outbound_count += 1
        if auto:
            stat.auto_reply_count += 1
        stat.last_reply_at = when
        if last_result:
            stat.last_result = last_result
        if stat.first_reply_at is None:
            stat.first_reply_at = when
            if stat.first_in_at:
                self._finished_response.append(
                    max(0.0, (when - stat.first_in_at).total_seconds())
                )
        if stat.pending_since:
            stat.response_seconds.append(max(0.0, (when - stat.pending_since).total_seconds()))
            stat.pending_since = None
        return stat

    def pause_for_human(self, uid: str, minutes: int) -> None:
        stat = self.get(uid)
        stat.human_takeover_until = now_beijing() + timedelta(minutes=max(1, minutes))
        stat.pending_since = None

    def resume_auto(self, uid: str) -> None:
        stat = self.get(uid)
        stat.human_takeover_until = None

    def is_human_active(self, uid: str) -> bool:
        return self.get(uid).human_active

    # ------------------------------------------------------------------
    def overdue(self, threshold_seconds: Optional[float] = None) -> list[ConversationStat]:
        """客户还在等、且已超过阈值的会话。"""
        threshold = threshold_seconds if threshold_seconds is not None else self.warn_seconds
        result: list[ConversationStat] = []
        for stat in self._stats.values():
            waiting = stat.waiting_seconds
            if waiting is not None and waiting >= threshold and not stat.human_active:
                result.append(stat)
        result.sort(key=lambda s: s.waiting_seconds or 0, reverse=True)
        return result

    @property
    def conversations(self) -> dict[str, ConversationStat]:
        return self._stats

    # ------------------------------------------------------------------
    def snapshot(self) -> dict:
        """面板用的实时指标。"""
        stats = list(self._stats.values())
        first_responses = [s.first_response_seconds for s in stats if s.first_response_seconds is not None]
        if not first_responses:
            first_responses = list(self._finished_response)
        total_replies = sum(s.auto_reply_count for s in stats)
        within = sum(1 for v in first_responses if v <= self.first_response_seconds)
        waiting = [s for s in stats if s.pending_since is not None and not s.human_active]
        return {
            "conversations": len(stats),
            "auto_replies": total_replies,
            "avg_first_response_seconds": round(statistics.mean(first_responses), 1)
            if first_responses
            else None,
            "p90_first_response_seconds": round(_percentile(first_responses, 0.9), 1)
            if first_responses
            else None,
            "within_sla_rate": round(within / len(first_responses), 4) if first_responses else None,
            "sla_threshold_seconds": self.first_response_seconds,
            "waiting_count": len(waiting),
            "overdue_count": len(self.overdue()),
        }


def _percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * ratio))))
    return ordered[index]


__all__ = ["ConversationStat", "SLATracker"]
