# -*- coding: utf-8 -*-
"""告警通知：把关键事件推送到钉钉 / 飞书 / 企业微信 / 自定义 Webhook。

日常客服最怕的不是机器人回错，而是**没人知道出事了**：
登录态过期、长连接断开、转人工失败、客户超时没人回。本模块把这些事件
统一格式化后发到团队群，并做节流去重，避免刷屏。

支持格式：
    - generic   自定义 JSON：{"title", "text", "event", "shop_id", "at"}
    - dingtalk  钉钉机器人：{"msgtype":"text","text":{"content":...}}
    - feishu    飞书机器人：{"msg_type":"text","content":{"text":...}}
    - wecom     企业微信群机器人：{"msgtype":"text","text":{"content":...}}
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional

from .config import AlertsConfig, MonitorConfig
from .utils import iso, now_beijing

logger = logging.getLogger("pdd_cs.alerts")

# 事件类型（与 config.alerts.events 对应）
EVENT_CONNECTION_ERROR = "connection_error"
EVENT_SESSION_EXPIRED = "session_expired"
EVENT_TRANSFER = "transfer"
EVENT_TRANSFER_FAILED = "transfer_failed"
EVENT_SLA_OVERDUE = "sla_overdue"
EVENT_SEND_FAILED = "send_failed"
EVENT_RISK_BLOCKED = "risk_blocked"
EVENT_STARTUP = "startup"
EVENT_SHOP_OFFLINE = "shop_offline"
EVENT_SHOP_ONLINE = "shop_online"

EVENT_LABELS = {
    EVENT_CONNECTION_ERROR: "连接异常",
    EVENT_SESSION_EXPIRED: "登录态过期",
    EVENT_TRANSFER: "转人工",
    EVENT_TRANSFER_FAILED: "转人工失败",
    EVENT_SLA_OVERDUE: "客户等待超时",
    EVENT_SEND_FAILED: "发送失败",
    EVENT_RISK_BLOCKED: "内容被平台拦截",
    EVENT_STARTUP: "服务启动",
    EVENT_SHOP_OFFLINE: "店铺掉线",
    EVENT_SHOP_ONLINE: "店铺恢复",
}

EVENT_LEVELS = {
    EVENT_CONNECTION_ERROR: "warning",
    EVENT_SESSION_EXPIRED: "error",
    EVENT_TRANSFER: "info",
    EVENT_TRANSFER_FAILED: "error",
    EVENT_SLA_OVERDUE: "warning",
    EVENT_SEND_FAILED: "warning",
    EVENT_RISK_BLOCKED: "warning",
    EVENT_STARTUP: "info",
    EVENT_SHOP_OFFLINE: "error",
    EVENT_SHOP_ONLINE: "info",
}


@dataclass
class AlertEvent:
    event: str
    title: str
    text: str
    shop_id: str = ""
    shop_name: str = ""
    level: str = "info"


class AlertNotifier:
    """Webhook 告警（线程安全、失败不影响主流程、按事件+店铺节流）。"""

    def __init__(self, config: AlertsConfig):
        self.config = config
        self._lock = threading.Lock()
        self._last_sent: dict[str, float] = {}
        self.sent_count = 0
        self.failed_count = 0
        self.last_error = ""

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled and self.config.webhook_url)

    def should_send(self, event: str) -> bool:
        return self.enabled and event in self.config.events

    # ------------------------------------------------------------------
    def notify(
        self,
        event: str,
        title: str,
        text: str,
        *,
        shop_id: str = "",
        shop_name: str = "",
        level: Optional[str] = None,
        force: bool = False,
        dedupe_key: str = "",
    ) -> bool:
        """异步语义：内部同步发送但失败只记日志，不抛异常。"""
        if not self.should_send(event):
            return False
        key = dedupe_key or f"{event}:{shop_id}"
        now = time.monotonic()
        with self._lock:
            last = self._last_sent.get(key, 0.0)
            if not force and now - last < self.config.min_interval_seconds:
                return False
            self._last_sent[key] = now

        payload = AlertEvent(
            event=event,
            title=title,
            text=text,
            shop_id=shop_id,
            shop_name=shop_name,
            level=level or EVENT_LEVELS.get(event, "info"),
        )
        try:
            body = self._build_payload(payload)
            self._post(body)
            with self._lock:
                self.sent_count += 1
            logger.info("已发送告警通知 [%s] %s", event, title)
            return True
        except Exception as exc:  # noqa: BLE001 - 告警失败不能影响客服主流程
            with self._lock:
                self.failed_count += 1
                self.last_error = str(exc)
            logger.warning("发送告警失败（已忽略）: %s", exc)
            return False

    # ------------------------------------------------------------------
    def _build_payload(self, event: AlertEvent) -> dict[str, Any]:
        prefix = self.config.title_prefix or "拼多多智能客服"
        shop = f"[{event.shop_name or event.shop_id}]" if (event.shop_id or event.shop_name) else ""
        title = f"{prefix}{shop} {event.title}"
        label = event.title or EVENT_LABELS.get(event.event, event.event)
        lines = [
            f"【{prefix}】{shop}{label}",
            f"时间：{iso(now_beijing())}",
        ]
        if event.text:
            lines.append(event.text)
        content = "\n".join(lines)

        fmt = self.config.format
        if fmt == "dingtalk":
            return {"msgtype": "text", "text": {"content": content}}
        if fmt == "feishu":
            return {"msg_type": "text", "content": {"text": content}}
        if fmt == "wecom":
            return {"msgtype": "text", "text": {"content": content}}
        return {
            "title": title,
            "text": content,
            "event": event.event,
            "level": event.level,
            "shop_id": event.shop_id,
            "shop_name": event.shop_name,
            "at": iso(now_beijing()),
        }

    def _post(self, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.config.webhook_url,
            data=data,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as resp:
            response = resp.read().decode("utf-8", errors="replace")
        if response:
            logger.debug("告警响应: %s", response[:300])

    # ------------------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "format": self.config.format,
            "sent": self.sent_count,
            "failed": self.failed_count,
            "last_error": self.last_error,
        }


class HeartbeatReporter:
    """进程存活心跳（dead-man switch）。

    本地进程如果整个挂掉/服务器宕机，Webhook 告警是发不出去的。
    解决办法是反过来做：本进程定期向外部监控（Healthchecks.io、Uptime Kuma、
    云函数、自建接口）报一次“我还活着”，外部监控在超过 N 分钟没收到时报警。
    """

    def __init__(self, config: MonitorConfig):
        self.config = config
        self.started = time.monotonic()
        self.last_ok_at: float = 0.0
        self.sent_count = 0
        self.failed_count = 0
        self.last_error = ""

    @property
    def enabled(self) -> bool:
        return bool(self.config.heartbeat_url)

    def next_due(self) -> float:
        return self.last_ok_at + max(30, self.config.heartbeat_interval_seconds)

    def send(self, status: dict[str, Any]) -> bool:
        if not self.enabled:
            return False
        shops = status.get("shops") or []
        disconnected = [shop.get("shop_id") for shop in shops if not shop.get("channel", {}).get("connected")]
        payload = {
            "app": "pdd-cs",
            "event": "heartbeat",
            "at": iso(now_beijing()),
            "uptime_seconds": int(time.monotonic() - self.started),
            "shops_total": len(shops),
            "shops_connected": len(shops) - len(disconnected),
            "disconnected": disconnected,
            "today": status.get("today", {}),
            "ok": not disconnected and not status.get("startup_errors"),
        }
        try:
            if self.config.heartbeat_method == "GET":
                sep = "&" if "?" in self.config.heartbeat_url else "?"
                query = (
                    f"{sep}ok={int(payload['ok'])}&shops={payload['shops_total']}"
                    f"&connected={payload['shops_connected']}"
                )
                request = urllib.request.Request(
                    self.config.heartbeat_url + query, method="GET"
                )
            else:
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                request = urllib.request.Request(
                    self.config.heartbeat_url,
                    data=data,
                    headers={"Content-Type": "application/json; charset=utf-8"},
                    method="POST",
                )
            with urllib.request.urlopen(request, timeout=self.config.heartbeat_timeout_seconds):
                pass
            self.last_ok_at = time.monotonic()
            self.sent_count += 1
            logger.debug("心跳上报成功: %s", payload)
            return True
        except Exception as exc:  # noqa: BLE001 - 心跳失败不能影响客服
            self.failed_count += 1
            self.last_error = str(exc)
            logger.warning("心跳上报失败（已忽略）: %s", exc)
            return False

    def stats(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "url": self.config.heartbeat_url,
            "interval_seconds": self.config.heartbeat_interval_seconds,
            "sent": self.sent_count,
            "failed": self.failed_count,
            "last_error": self.last_error,
        }


__all__ = [
    "AlertNotifier",
    "HeartbeatReporter",
    "EVENT_SHOP_OFFLINE",
    "EVENT_SHOP_ONLINE",
    "AlertEvent",
    "EVENT_LABELS",
    "EVENT_CONNECTION_ERROR",
    "EVENT_SESSION_EXPIRED",
    "EVENT_TRANSFER",
    "EVENT_TRANSFER_FAILED",
    "EVENT_SLA_OVERDUE",
    "EVENT_SEND_FAILED",
    "EVENT_RISK_BLOCKED",
    "EVENT_STARTUP",
]
