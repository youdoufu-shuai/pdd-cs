# -*- coding: utf-8 -*-
"""拼多多客服长连接（WebSocket）客户端。

职责：
    1. 用 access_token 连接 ``wss://m-ws.pinduoduo.com/``；
    2. 连接建立后把客服置为「在线」（否则拼多多不会推送客户消息）；
    3. 心跳保活 + 断线指数退避重连（重连前自动刷新 token）；
    4. 把收到的原始帧解析成 :class:`ChatEvent` 交给上层（引擎）；
    5. 统计连接状态，供监控面板展示。

注意：发送消息走 HTTP 接口（见 :mod:`pdd_cs.pdd.api`），不走本长连接。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable, Optional

from websockets.asyncio.client import ClientConnection, connect as ws_connect
from websockets.exceptions import ConnectionClosed

from ..config import PDDConfig
from ..constants import PDD_WS_URL
from .api import PDDClient, PDDError, PDDSessionExpired
from .message import ChatEvent, parse_ws_frame

logger = logging.getLogger("pdd_cs.channel")

EventCallback = Callable[[list[ChatEvent]], Awaitable[None]]
StateCallback = Callable[[str, str], Awaitable[None]]


class ConnState(str, Enum):
    STOPPED = "stopped"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    ERROR = "error"


@dataclass
class ChannelStats:
    received_frames: int = 0
    received_events: int = 0
    sent_replies: int = 0
    reconnects: int = 0
    connected_at: Optional[str] = None
    last_frame_at: Optional[str] = None
    last_error: str = ""
    extra: dict = field(default_factory=dict)


class PDDChannel:
    """拼多多客服消息长连接。"""

    def __init__(
        self,
        api: PDDClient,
        config: PDDConfig,
        *,
        on_events: Optional[EventCallback] = None,
        on_state: Optional[StateCallback] = None,
        ws_url: Optional[str] = None,
        heartbeat_interval: float = 30.0,
        heartbeat_timeout: float = 10.0,
        max_heartbeat_failures: int = 3,
        max_reconnect_attempts: int = 8,
        base_backoff: float = 2.0,
        max_backoff: float = 60.0,
    ) -> None:
        self.api = api
        self.config = config
        self.ws_url = (ws_url or config.ws_url or PDD_WS_URL).rstrip("/") + "/"
        self.on_events = on_events
        self.on_state = on_state

        self.heartbeat_interval = heartbeat_interval
        self.heartbeat_timeout = heartbeat_timeout
        self.max_heartbeat_failures = max_heartbeat_failures
        self.max_reconnect_attempts = max_reconnect_attempts
        self.base_backoff = base_backoff
        self.max_backoff = max_backoff

        self.state: ConnState = ConnState.STOPPED
        self.stats = ChannelStats()
        self._stop_event: Optional[asyncio.Event] = None
        self._task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._ws: Optional[ClientConnection] = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """启动长连接（后台任务，立即返回）。"""
        if self._task and not self._task.done():
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run_forever(), name="pdd-channel")
        logger.info("拼多多长连接服务已启动")

    async def stop(self) -> None:
        """停止长连接并清理资源。"""
        if self._stop_event:
            self._stop_event.set()
        await self._cancel_heartbeat()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws = None
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        await self._set_state(ConnState.STOPPED, "已停止")
        logger.info("拼多多长连接服务已停止")

    @property
    def connected(self) -> bool:
        return self.state == ConnState.CONNECTED and self._ws is not None

    async def _set_state(self, state: ConnState, detail: str = "") -> None:
        changed = state != self.state
        self.state = state
        if changed:
            logger.info("连接状态: %s %s", state.value, detail)
        if self.on_state:
            try:
                await self.on_state(state.value, detail)
            except Exception as exc:  # noqa: BLE001
                logger.warning("状态回调异常: %s", exc)

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    async def _run_forever(self) -> None:
        attempt = 0
        while self._stop_event and not self._stop_event.is_set():
            try:
                await self._set_state(
                    ConnState.CONNECTING if attempt == 0 else ConnState.RECONNECTING,
                    f"第 {attempt + 1} 次连接",
                )
                await self._connect_once()
                attempt = 0  # 正常断开（非异常）后重置
            except asyncio.CancelledError:
                raise
            except PDDSessionExpired as exc:
                self.stats.last_error = str(exc)
                await self._set_state(ConnState.ERROR, "登录态失效，需要重新登录")
                logger.error("%s", exc)
                return
            except Exception as exc:  # noqa: BLE001
                self.stats.last_error = str(exc)
                logger.warning("长连接断开: %s", exc)
            finally:
                await self._cancel_heartbeat()

            if self._stop_event is None or self._stop_event.is_set():
                break

            attempt += 1
            if attempt > self.max_reconnect_attempts:
                await self._set_state(ConnState.ERROR, "重连次数已达上限")
                logger.error("重连次数已达上限（%s 次），停止重连", self.max_reconnect_attempts)
                return

            self.stats.reconnects += 1
            delay = min(self.base_backoff * (2 ** (attempt - 1)), self.max_backoff)
            await self._set_state(ConnState.RECONNECTING, f"{delay:.0f}s 后重连")
            logger.info("将在 %.0f 秒后重连（第 %s 次）", delay, attempt)
            try:
                if self._stop_event is not None:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def _connect_once(self) -> None:
        """建立一次连接并持续接收，直到断开或停止。"""
        token = await asyncio.to_thread(self.api.get_token)
        url = self._build_url(str(token))
        logger.info("正在连接拼多多客服消息通道: %s", self._safe_url(url))

        async with ws_connect(
            url,
            ping_interval=None,  # 使用自定义心跳，便于记录与排障
            ping_timeout=None,
            max_size=8 * 1024 * 1024,
            compression=None,
            close_timeout=10,
            open_timeout=15,
        ) as ws:
            self._ws = ws
            await self._set_state(ConnState.CONNECTED, "长连接已建立")
            self.stats.connected_at = _now_text()

            # 置为在线：拼多多只向在线客服推送客户消息
            if self.config.set_online:
                try:
                    ok = await asyncio.to_thread(self.api.set_cs_status, True)
                    if not ok:
                        logger.warning("客服在线状态设置失败（不影响收消息，可稍后在后台手动上线）")
                except Exception as exc:  # noqa: BLE001
                    logger.warning("客服在线状态设置异常: %s", exc)

            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(ws))
            try:
                async for raw in ws:
                    await self._handle_frame(raw)
            except ConnectionClosed as exc:
                logger.info("长连接关闭: %s", exc)
            finally:
                self._ws = None
                await self._cancel_heartbeat()

    def _build_url(self, token: str) -> str:
        params = {
            "access_token": token,
            "role": "mall_cs",
            "client": "web",
            "version": self.config.ws_api_version,
        }
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{self.ws_url}?{query}"

    @staticmethod
    def _safe_url(url: str) -> str:
        if "access_token=" not in url:
            return url
        head, _, tail = url.partition("access_token=")
        token, sep, rest = tail.partition("&")
        return f"{head}access_token={token[:6]}***{sep}{rest}"

    # ------------------------------------------------------------------
    # 收消息 / 心跳
    # ------------------------------------------------------------------
    async def _handle_frame(self, raw: object) -> None:
        self.stats.received_frames += 1
        self.stats.last_frame_at = _now_text()
        events = parse_ws_frame(raw)
        if not events:
            return
        self.stats.received_events += len(events)
        for event in events:
            logger.info("收到消息 %s", event.display_text())
        if self.on_events:
            try:
                await self.on_events(events)
            except Exception as exc:  # noqa: BLE001
                logger.exception("消息处理回调异常: %s", exc)

    async def _heartbeat_loop(self, ws) -> None:
        failures = 0
        while self._stop_event and not self._stop_event.is_set():
            try:
                await asyncio.sleep(self.heartbeat_interval)
                await asyncio.wait_for(ws.ping(), timeout=self.heartbeat_timeout)
                failures = 0
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                failures += 1
                logger.warning("长连接心跳超时（连续 %s 次）", failures)
            except ConnectionClosed:
                return
            except Exception as exc:  # noqa: BLE001
                failures += 1
                logger.warning("长连接心跳异常: %s", exc)
            if failures >= self.max_heartbeat_failures:
                logger.error("心跳连续失败 %s 次，主动断开等待重连", failures)
                try:
                    await ws.close()
                except Exception:  # noqa: BLE001
                    pass
                return

    async def _cancel_heartbeat(self) -> None:
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._heartbeat_task = None

    # ------------------------------------------------------------------
    def status_dict(self) -> dict:
        return {
            "state": self.state.value,
            "connected": self.connected,
            "connected_at": self.stats.connected_at,
            "last_frame_at": self.stats.last_frame_at,
            "received_frames": self.stats.received_frames,
            "received_events": self.stats.received_events,
            "reconnects": self.stats.reconnects,
            "last_error": self.stats.last_error,
        }


def _now_text() -> str:
    from ..utils import now_beijing

    return now_beijing().strftime("%Y-%m-%d %H:%M:%S")


__all__ = ["PDDChannel", "ConnState", "ChannelStats"]

