# -*- coding: utf-8 -*-
"""拼多多商家后台 HTTP 接口客户端。

为什么不用浏览器自动化？
    拼多多客服工作台本身就是「Cookie + HTTP 接口 + WebSocket 长连接」的
    前后端结构；直接走同一套接口，比模拟点击稳定、省资源，也不会因为
    页面改版（DOM 变化）而失效。

已实现的接口（均可在 config 中替换 base_url 指向 Mock 服务）：
    - POST /chats/getToken                     获取长连接 token
    - POST /plateau/chat/send_message          发送文本 / 图片
    - POST /plateau/chat/move_conversation     会话转人工
    - POST /latitude/assign/getAssignCsList    可分配人工客服列表
    - POST /plateau/chat/set_csstatus          客服在线状态
    - GET  /janus/api/new/userinfo             当前登录账号
    - POST /plateau/chat/latest_conversations  最近会话列表（可选）
    - POST /plateau/chat/list                  历史消息（可选）

说明：接口字段来自对拼多多商家后台前端行为的公开可观测建模，平台改版时
只需调整本文件；核心的「收消息 → 决策 → 发消息」链路不依赖具体页面。
"""
from __future__ import annotations

import json
import logging
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Optional

from ..constants import (
    CS_STATUS_OFFLINE,
    CS_STATUS_ONLINE,
    PDD_ASSIGN_CS_LIST_PATH,
    PDD_CHAT_HISTORY_PATH,
    PDD_CONVERSATIONS_PATH,
    PDD_CS_STATUS_PATH,
    PDD_GOODS_CARD_PATH,
    PDD_MALL_INFO_PATH,
    PDD_MOVE_CONVERSATION_PATH,
    PDD_SEND_MESSAGE_PATH,
    PDD_SEND_TEXT_ERROR_CODE,
    PDD_SESSION_EXPIRED_CODE,
    PDD_SESSION_EXPIRED_KEYWORD,
    PDD_SHOP_INFO_PATH,
    PDD_TOKEN_PATH,
    PDD_USER_INFO_PATH,
)
from .auth import cookies_to_header

logger = logging.getLogger("pdd_cs.api")

_RETRYABLE_HTTP_STATUS = {408, 425, 429, 500, 502, 503, 504}


class PDDError(Exception):
    """拼多多接口调用失败。"""

    def __init__(self, message: str, code: Any = None, retryable: bool = False, raw: Any = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.retryable = retryable
        self.raw = raw


class PDDSessionExpired(PDDError):
    """登录态失效（需要重新登录或刷新 Cookie）。"""


class PDDSendRejected(PDDError):
    """消息被平台拒绝（内容违规 / 频率限制 / 风控）。"""


class PDDClient:
    """拼多多商家后台接口客户端（线程安全、带重试与节流）。"""

    def __init__(
        self,
        cookies: dict[str, str],
        *,
        base_url: str,
        user_agent: str,
        timeout: float = 20.0,
        max_retries: int = 3,
        min_send_interval: float = 1.2,
        logger_: Optional[logging.Logger] = None,
    ) -> None:
        self.cookies: dict[str, str] = dict(cookies or {})
        self.last_cs_status_error: str = ""
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent
        self.timeout = timeout
        self.max_retries = max(1, int(max_retries))
        self.min_send_interval = max(0.0, float(min_send_interval))
        self.log = logger_ or logger

        self._lock = threading.Lock()
        self._last_send_at: float = 0.0
        self.request_count = 0
        self.error_count = 0

    # ------------------------------------------------------------------
    # 基础请求
    # ------------------------------------------------------------------
    def update_cookies(self, cookies: dict[str, str]) -> None:
        with self._lock:
            self.cookies = dict(cookies or {})

    def _headers(self, extra: Optional[dict[str, str]] = None) -> dict[str, str]:
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Content-Type": "application/json;charset=UTF-8",
            "Origin": self.base_url,
            "Referer": f"{self.base_url}/chat-merchant/index.html",
            "Priority": "u=1, i",
            "Cookie": cookies_to_header(self.cookies),
        }
        if extra:
            headers.update(extra)
        return headers

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self.base_url}{path}"

    def _parse_response(self, payload: Any) -> dict[str, Any]:
        """统一解析响应并识别登录态失效。"""
        if not isinstance(payload, dict):
            raise PDDError(f"响应不是 JSON 对象: {str(payload)[:200]}", raw=payload)

        code = payload.get("error_code") or payload.get("errorCode")
        message = payload.get("error_msg") or payload.get("errorMsg") or payload.get("message") or ""
        if code is not None and (str(code) == str(PDD_SESSION_EXPIRED_CODE) or PDD_SESSION_EXPIRED_KEYWORD in str(message)):
            raise PDDSessionExpired(
                f"登录态已过期（{message or code}），请重新运行 login 更新 Cookie",
                code=code,
                raw=payload,
            )

        # 部分接口把业务错误放在 result 里
        result = payload.get("result")
        if isinstance(result, dict):
            inner_code = result.get("error_code") or result.get("errorCode")
            inner_msg = result.get("error") or result.get("error_msg") or ""
            if inner_code is not None and PDD_SESSION_EXPIRED_KEYWORD in str(inner_msg):
                raise PDDSessionExpired(f"登录态已过期（{inner_msg}）", code=inner_code, raw=payload)
        return payload

    def request(
        self,
        method: str,
        path: str,
        *,
        json_data: Any = None,
        headers: Optional[dict[str, str]] = None,
        timeout: Optional[float] = None,
        retries: Optional[int] = None,
        expect_json: bool = True,
    ) -> dict[str, Any]:
        """发起一次带重试的 HTTP 请求。"""
        url = self._url(path)
        body: Optional[bytes] = None
        if json_data is not None:
            body = json.dumps(json_data, ensure_ascii=False).encode("utf-8")

        attempts = self.max_retries if retries is None else max(1, retries)
        last_error: Optional[Exception] = None

        for attempt in range(attempts):
            request = urllib.request.Request(
                url, data=body, headers=self._headers(headers), method=method.upper()
            )
            try:
                with urllib.request.urlopen(request, timeout=timeout or self.timeout) as resp:
                    raw = resp.read()
                self.request_count += 1
                text = raw.decode("utf-8", errors="replace")
                if not expect_json:
                    return {"text": text}
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise PDDError(f"响应不是合法 JSON: {text[:200]}", raw=text) from exc
                return self._parse_response(payload)

            except urllib.error.HTTPError as exc:
                self.error_count += 1
                status = exc.code
                detail = ""
                try:
                    detail = exc.read().decode("utf-8", errors="replace")[:300]
                except Exception:  # noqa: BLE001
                    pass
                last_error = PDDError(f"HTTP {status}: {detail or exc.reason}", code=status)
                if status in _RETRYABLE_HTTP_STATUS and attempt < attempts - 1:
                    self._backoff(attempt, f"HTTP {status}")
                    continue
                raise last_error

            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as exc:
                self.error_count += 1
                last_error = PDDError(f"网络异常: {exc}", retryable=True)
                if attempt < attempts - 1:
                    self._backoff(attempt, str(exc))
                    continue
                raise last_error

        raise last_error or PDDError("请求失败（未知原因）")

    def _backoff(self, attempt: int, reason: str) -> None:
        delay = min(2.0 * (2**attempt), 15.0)
        self.log.warning("接口请求失败（%s），%.1fs 后重试", reason, delay)
        time.sleep(delay)

    # ------------------------------------------------------------------
    # 账号 / 店铺信息
    # ------------------------------------------------------------------
    def get_token(self) -> str:
        """获取 WebSocket 长连接所需的 access_token。"""
        payload = self.request("POST", PDD_TOKEN_PATH, json_data={"version": "3"})
        token = payload.get("token")
        if not token and isinstance(payload.get("result"), dict):
            token = payload["result"].get("token")
        if not token:
            raise PDDError(f"未从响应中取得 token: {str(payload)[:200]}", raw=payload)
        return str(token)

    def get_user_info(self) -> dict[str, Any]:
        """当前登录账号信息。

        拼多多 2026 版把该接口改成了 POST；旧版本/自建 Mock 仍兼容 GET。
        """
        payload: dict[str, Any]
        try:
            payload = self.request("POST", PDD_USER_INFO_PATH, json_data={}, retries=1)
        except PDDError as exc:
            text = str(exc)
            if "405" not in text and "METHOD_NOT_ALLOWED" not in text:
                raise
            payload = self.request("GET", PDD_USER_INFO_PATH)
        result = payload.get("result") if isinstance(payload.get("result"), dict) else payload
        return result if isinstance(result, dict) else {}

    def get_mall_info(self) -> dict[str, Any]:
        """当前账号所属店铺信息（包含 mall_name，用于展示真实店名）。"""
        try:
            payload = self.request("POST", PDD_MALL_INFO_PATH, json_data={}, retries=1)
        except PDDError:
            return {}
        result = payload.get("result") if isinstance(payload.get("result"), dict) else payload
        mall = result.get("mall") if isinstance(result.get("mall"), dict) else {}
        return mall if isinstance(mall, dict) else {}

    def get_shop_info(self, mall_id: str = "") -> dict[str, Any]:
        """店铺信息（需要 mall_id 时自动带上）。"""
        path = PDD_SHOP_INFO_PATH
        if mall_id:
            path = f"{path}?mallId={mall_id}"
        payload = self.request("GET", path)
        result = payload.get("result") if isinstance(payload.get("result"), dict) else payload
        return result if isinstance(result, dict) else {}

    def validate_session(self) -> dict[str, Any]:
        """校验登录态是否有效：返回账号信息；失效抛 PDDSessionExpired。"""
        info = self.get_user_info()
        if not info:
            raise PDDError("登录态校验失败：接口未返回账号信息")
        if not (info.get("mall_name") or info.get("mallName")):
            try:
                mall = self.get_mall_info()
                name = mall.get("mall_name") or mall.get("mallName") or ""
                if name:
                    info["mall_name"] = str(name)
                if not info.get("mall_id") and mall.get("mall_id"):
                    info["mall_id"] = mall.get("mall_id")
            except Exception:  # noqa: BLE001 - 店名不是关键路径
                pass
        return info

    # ------------------------------------------------------------------
    # 客服在线状态 / 人工客服
    # ------------------------------------------------------------------
    def set_cs_status(self, online: bool = True) -> bool:
        """把当前客服置为在线（在线后拼多多才会把客户消息推给长连接）。"""
        data = {
            "data": {
                "cmd": "set_csstatus",
                "status": CS_STATUS_ONLINE if online else CS_STATUS_OFFLINE,
            },
            "client": "WEB",
        }
        self.last_cs_status_error = ""
        try:
            payload = self.request("POST", PDD_CS_STATUS_PATH, json_data=data)
        except PDDError as exc:
            self.last_cs_status_error = str(exc)
            self.log.warning("设置客服在线状态失败: %s", exc)
            return False
        if payload.get("success") is True:
            return True
        self.last_cs_status_error = str(
            payload.get("error_msg")
            or payload.get("errorMsg")
            or payload.get("error_code")
            or payload.get("errorCode")
            or "客服在线状态设置失败"
        )
        return False

    def get_assign_cs_list(self) -> list[dict[str, str]]:
        """可分配的人工客服列表。"""
        data = {"wechatCheck": True}
        payload = self.request("POST", PDD_ASSIGN_CS_LIST_PATH, json_data=data)
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        raw = result.get("csList")
        items: list[dict[str, str]] = []
        if isinstance(raw, dict):
            for uid, info in raw.items():
                name = uid
                if isinstance(info, dict):
                    name = str(
                        info.get("username")
                        or info.get("cs_name")
                        or info.get("name")
                        or info.get("nickname")
                        or uid
                    )
                items.append({"cs_uid": str(uid), "cs_name": name})
        elif isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                uid = item.get("cs_uid") or item.get("csid") or item.get("uid") or item.get("id")
                if uid is None:
                    continue
                name = str(
                    item.get("cs_name")
                    or item.get("csName")
                    or item.get("username")
                    or item.get("name")
                    or uid
                )
                items.append({"cs_uid": str(uid), "cs_name": name})
        return items

    # ------------------------------------------------------------------
    # 消息发送
    # ------------------------------------------------------------------
    def _wait_send_slot(self) -> None:
        """发送节流：同一账号两次发送至少间隔 min_send_interval 秒。"""
        if self.min_send_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait = self.min_send_interval - (now - self._last_send_at)
            if wait > 0:
                time.sleep(wait)
            self._last_send_at = time.monotonic()

    def send_text(self, recipient_uid: Any, content: str) -> dict[str, Any]:
        """发送文本消息。

        Raises:
            PDDSendRejected: 平台拒绝（内容违规 / 频率限制等）
            PDDSessionExpired: 登录态失效
            PDDError: 其它失败
        """
        text = (content or "").strip()
        if not text:
            raise PDDSendRejected("回复内容为空，拒绝发送")
        self._wait_send_slot()

        data = {
            "data": {
                "cmd": "send_message",
                "request_id": uuid.uuid4().hex,
                "message": {
                    "to": {"role": "user", "uid": recipient_uid},
                    "from": {"role": "mall_cs"},
                    "content": text,
                    "msg_id": None,
                    "type": 0,
                    "is_aut": 0,
                    "manual_reply": 1,
                },
            },
            "client": "WEB",
        }
        payload = self.request("POST", PDD_SEND_MESSAGE_PATH, json_data=data)
        return self._check_send_result(payload, kind="文本")

    def send_image(self, recipient_uid: Any, image_url: str) -> dict[str, Any]:
        """发送图片消息。"""
        if not image_url:
            raise PDDSendRejected("图片地址为空")
        self._wait_send_slot()
        data = {
            "data": {
                "cmd": "send_message",
                "request_id": uuid.uuid4().hex,
                "message": {
                    "to": {"role": "user", "uid": recipient_uid},
                    "from": {"role": "mall_cs"},
                    "content": image_url,
                    "msg_id": None,
                    "chat_type": "cs",
                    "type": 1,
                    "is_aut": 0,
                    "manual_reply": 1,
                },
            },
            "client": "WEB",
        }
        payload = self.request("POST", PDD_SEND_MESSAGE_PATH, json_data=data)
        return self._check_send_result(payload, kind="图片")

    def send_goods_card(self, recipient_uid: Any, goods_id: Any, biz_type: int = 2) -> dict[str, Any]:
        """发送商品卡片（依赖 Cookie 中的 anti-content 签名，失败请降级为文本）。"""
        anti_content = self.cookies.get("anti_content") or self.cookies.get("anti-content") or ""
        if not anti_content:
            raise PDDSendRejected("缺少 anti_content 签名，无法发送商品卡片（已降级为文本回复）")
        self._wait_send_slot()
        data = {"uid": recipient_uid, "goods_id": goods_id, "biz_type": biz_type}
        headers = {
            "anti-content": anti_content,
            "Origin": self.base_url,
            "Referer": f"{self.base_url}/chat-merchant/index.html",
        }
        payload = self.request("POST", PDD_GOODS_CARD_PATH, json_data=data, headers=headers)
        return self._check_send_result(payload, kind="商品卡片")

    def _check_send_result(self, payload: dict[str, Any], kind: str = "消息") -> dict[str, Any]:
        success = payload.get("success")
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        inner_code = result.get("error_code")
        inner_msg = result.get("error") or result.get("error_msg") or ""
        if success is True and inner_code is None:
            return payload
        if inner_code is not None and str(inner_code) == str(PDD_SEND_TEXT_ERROR_CODE):
            raise PDDSendRejected(f"{kind}发送被平台拒绝: {inner_msg or inner_code}", code=inner_code, raw=payload)
        if success is True and inner_code is not None:
            raise PDDSendRejected(f"{kind}发送失败: {inner_msg or inner_code}", code=inner_code, raw=payload)
        raise PDDError(f"{kind}发送失败: {payload.get('error_msg') or payload}", raw=payload)

    def move_conversation(
        self,
        recipient_uid: Any,
        cs_uid: Any,
        remark: str = "智能客服转人工",
        need_wx: bool = False,
    ) -> dict[str, Any]:
        """把会话转给指定人工客服。"""
        data = {
            "data": {
                "cmd": "move_conversation",
                "request_id": uuid.uuid4().hex,
                "conversation": {
                    "csid": cs_uid,
                    "uid": recipient_uid,
                    "need_wx": need_wx,
                    "remark": remark,
                },
            },
            "client": "WEB",
        }
        payload = self.request("POST", PDD_MOVE_CONVERSATION_PATH, json_data=data)
        if payload.get("success") is not True:
            raise PDDError(f"转人工失败: {payload.get('error_msg') or str(payload)[:200]}", raw=payload)
        return payload

    # ------------------------------------------------------------------
    # 会话 / 历史（可选能力，失败自动降级）
    # ------------------------------------------------------------------
    def get_recent_conversations(self, page: int = 1, size: int = 50) -> list[dict[str, Any]]:
        """最近会话列表。字段缺失时返回空列表，不影响主链路。"""
        data = {
            "data": {
                "cmd": "latest_conversations",
                "request_id": uuid.uuid4().hex,
                "version": 2,
                "need_unreply_time": True,
                "page": max(1, int(page)),
                "size": max(1, min(int(size), 100)),
            },
            "client": "WEB",
        }
        try:
            payload = self.request("POST", PDD_CONVERSATIONS_PATH, json_data=data)
        except PDDError as exc:
            self.log.info("拉取最近会话失败（不影响自动回复）: %s", exc)
            return []
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        raw_list = result.get("conversations") or result.get("list") or []
        conversations: list[dict[str, Any]] = []
        if isinstance(raw_list, list):
            for raw in raw_list:
                parsed = self._parse_conversation(raw)
                if parsed.get("uid"):
                    conversations.append(parsed)
        return conversations

    @staticmethod
    def _parse_conversation(raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            return {}
        uid = raw.get("uid") or raw.get("user_id") or raw.get("from_uid") or raw.get("customer_uid")
        nickname = raw.get("nickname") or raw.get("user_name") or raw.get("userName") or ""
        last = raw.get("last_message") or raw.get("lastMessage") or {}
        content = last.get("content") if isinstance(last, dict) else last
        return {
            "uid": str(uid) if uid is not None else "",
            "nickname": str(nickname),
            "last_message": str(content or ""),
            "unreply": raw.get("unreply") or raw.get("unread") or 0,
            "time": raw.get("time") or raw.get("update_time") or raw.get("timestamp"),
        }

    def get_chat_history(self, recipient_uid: Any, start_msg_id: Any = None, size: int = 50) -> list[dict[str, Any]]:
        """单会话历史消息（用于启动时补上下文；接口失败返回空列表）。"""
        data = {
            "data": {
                "cmd": "chat_list",
                "request_id": uuid.uuid4().hex,
                "uid": recipient_uid,
                "start_msg_id": start_msg_id,
                "size": max(1, min(int(size), 100)),
                "anti_content": self.cookies.get("anti_content") or self.cookies.get("anti-content") or "",
            },
            "client": "WEB",
        }
        try:
            payload = self.request("POST", PDD_CHAT_HISTORY_PATH, json_data=data)
        except PDDError as exc:
            self.log.info("拉取历史消息失败（不影响自动回复）: %s", exc)
            return []
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        messages = result.get("messages") or []
        return messages if isinstance(messages, list) else []

    # ------------------------------------------------------------------
    @property
    def masked_cookie_keys(self) -> list[str]:
        return list(self.cookies.keys())[:8]


__all__ = [
    "PDDClient",
    "PDDError",
    "PDDSessionExpired",
    "PDDSendRejected",
]
