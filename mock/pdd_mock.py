# -*- coding: utf-8 -*-
"""拼多多 Mock 服务（HTTP + WebSocket）。

用途：没有真实店铺账号时，也能把「收消息 → 决策 → 合规 → 发送 / 转人工」全链路
跑通并做自动化测试。接口形状按拼多多商家后台客服接口建模：

    POST /chats/getToken
    POST /plateau/chat/send_message
    POST /plateau/chat/move_conversation
    POST /latitude/assign/getAssignCsList
    POST /plateau/chat/set_csstatus
    GET  /janus/api/new/userinfo
    GET  /earth/api/merchant/queryMerchantInfoByMallId
    POST /plateau/chat/latest_conversations
    POST /plateau/chat/list
    WS   ws://127.0.0.1:<ws_port>/  推送买家消息

额外调试接口：
    GET  /_mock/state        查看已发送消息 / 转人工记录
    POST /_mock/push         手动推送一条买家消息
    POST /_mock/human        模拟人工客服回复（触发人工接管）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import urlparse

from websockets.asyncio.server import serve as ws_serve
DEFAULT_CS = {"1001": {"username": "人工客服小美"}, "1002": {"username": "人工客服小刚"}}


def make_push(
    content: str,
    *,
    uid: str = "buyer-1",
    nickname: str = "测试买家",
    msg_id: Optional[str] = None,
    msg_type: int = 0,
    sub_type: int = 0,
    time_ms: Optional[int] = None,
    from_role: str = "user",
    extra: Optional[dict] = None,
) -> dict:
    """构造一条拼多多 push 报文。"""
    message: dict[str, Any] = {
        "type": msg_type,
        "sub_type": sub_type,
        "content": content,
        "msg_id": msg_id or f"mock-{int(time.time() * 1000)}-{uid}",
        "time": time_ms if time_ms is not None else int(time.time() * 1000),
        "nickname": nickname,
        "from": {"role": from_role, "uid": uid if from_role == "user" else "cs-1"},
        "to": {"role": "mall_cs" if from_role == "user" else "user", "uid": "cs-1" if from_role == "user" else uid},
    }
    if extra:
        message["info"] = extra
    return {"response": "push", "message": message}


class MockState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.sent: list[dict[str, Any]] = []
        self.transfers: list[dict[str, Any]] = []
        self.cs_status: Optional[int] = None
        self.token_requests = 0
        self.ws_connections = 0


class MockPDDServer:
    """同时提供 HTTP 与 WebSocket 的拼多多 Mock。"""

    def __init__(
        self,
        http_port: int = 0,
        ws_port: int = 0,
        host: str = "127.0.0.1",
        shop_name: str = "测试店铺",
        mall_id: str = "mock-mall-1",
    ):
        self.host = host
        self.http_port = http_port
        self.ws_port = ws_port
        self.shop_name = shop_name
        self.mall_id = mall_id
        self.state = MockState()
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._http_thread: Optional[threading.Thread] = None
        self._ws_server = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._clients: set = set()
        self._pushed: list[dict] = []

    # ------------------------------------------------------------------
    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.http_port}"

    @property
    def ws_url(self) -> str:
        return f"ws://{self.host}:{self.ws_port}/"

    # ------------------------------------------------------------------
    async def start(self) -> "MockPDDServer":
        self._loop = asyncio.get_running_loop()

        # HTTP 服务（线程）
        handler = self._make_handler()
        self._httpd = ThreadingHTTPServer((self.host, self.http_port), handler)
        self._httpd.daemon_threads = True
        self.http_port = self._httpd.server_address[1]
        self._http_thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._http_thread.start()

        # WebSocket 服务（事件循环内）
        self._ws_server = await ws_serve(
            self._ws_handler, self.host, self.ws_port, max_size=8 * 1024 * 1024
        )
        sockets = getattr(self._ws_server, "sockets", None)
        if sockets:
            self.ws_port = sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        if self._ws_server is not None:
            self._ws_server.close()
            await self._ws_server.wait_closed()
            self._ws_server = None
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    # ------------------------------------------------------------------
    async def _ws_handler(self, websocket) -> None:
        request_path = getattr(getattr(websocket, "request", None), "path", "")
        if "access_token=" not in (request_path or "") and "mock-token" not in str(request_path):
            # 不做强校验，但记录一下，方便排查参数是否拼对
            pass
        with self.state.lock:
            self.state.ws_connections += 1
        self._clients.add(websocket)
        try:
            async for message in websocket:
                # 客户端一般不会经长连接发消息；收到就记录
                self._pushed.append({"direction": "client", "data": message})
        except ConnectionClosed:
            pass
        finally:
            self._clients.discard(websocket)

    async def push_raw(self, payload: Any) -> int:
        """向所有已连接客户端推送一条原始报文，返回成功推送数。"""
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        sent = 0
        for websocket in list(self._clients):
            try:
                await websocket.send(text)
                sent += 1
            except Exception:  # noqa: BLE001
                self._clients.discard(websocket)
        return sent

    async def push(self, message: dict) -> int:
        return await self.push_raw(message)

    async def push_text(self, content: str, **kwargs) -> int:
        return await self.push(make_push(content, **kwargs))

    # ------------------------------------------------------------------
    def _make_handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "mock-pdd/1.0"

            def log_message(self, fmt: str, *args) -> None:  # noqa: A003
                pass

            def _json(self, payload: Any, status: int = 200) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _body(self) -> dict:
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    raw = self.rfile.read(length) if length else b"{}"
                    data = json.loads(raw.decode("utf-8") or "{}")
                    return data if isinstance(data, dict) else {}
                except Exception:  # noqa: BLE001
                    return {}

            # -- GET --
            def do_GET(self) -> None:  # noqa: N802
                path = urlparse(self.path).path.rstrip("/") or "/"
                if path == "/janus/api/new/userinfo":
                    self._json({
                        "success": True,
                        "result": {"username": "mock-cs", "mall_name": server.shop_name, "mall_id": server.mall_id},
                    })
                elif path == "/earth/api/merchant/queryMerchantInfoByMallId":
                    self._json({
                        "success": True,
                        "result": {"mall_id": server.mall_id, "mall_name": server.shop_name},
                    })
                elif path == "/_mock/state":
                    with server.state.lock:
                        self._json({
                            "sent": list(server.state.sent),
                            "transfers": list(server.state.transfers),
                            "cs_status": server.state.cs_status,
                            "token_requests": server.state.token_requests,
                            "ws_connections": server.state.ws_connections,
                        })
                else:
                    self._json({"success": False, "error_msg": f"mock 未实现 {path}"}, 404)

            # -- POST --
            def do_POST(self) -> None:  # noqa: N802
                path = urlparse(self.path).path.rstrip("/") or "/"
                body = self._body()

                if path == "/janus/api/new/userinfo":
                    self._json({
                        "success": True,
                        "result": {"username": "mock-cs", "mall_name": server.shop_name, "mall_id": server.mall_id},
                    })
                elif path == "/janus/api/userinfo":
                    self._json({
                        "success": True,
                        "result": {"mall": {"mall_name": server.shop_name, "mall_id": server.mall_id}},
                    })
                elif path == "/chats/getToken":
                    server.state.token_requests += 1
                    self._json({"success": True, "token": "mock-token"})
                elif path == "/plateau/chat/send_message":
                    data = body.get("data") or {}
                    message = data.get("message") or {}
                    to_uid = str((message.get("to") or {}).get("uid") or "")
                    content = message.get("content") or ""
                    with server.state.lock:
                        server.state.sent.append(
                            {
                                "uid": to_uid,
                                "content": content,
                                "type": message.get("type", 0),
                                "at": time.time(),
                            }
                        )
                    self._json({"success": True, "result": {}})
                elif path == "/plateau/chat/move_conversation":
                    conv = (body.get("data") or {}).get("conversation") or {}
                    with server.state.lock:
                        server.state.transfers.append(
                            {
                                "uid": str(conv.get("uid") or ""),
                                "cs_uid": str(conv.get("csid") or ""),
                                "remark": conv.get("remark"),
                                "at": time.time(),
                            }
                        )
                    self._json({"success": True, "result": {}})
                elif path == "/latitude/assign/getAssignCsList":
                    self._json({"success": True, "result": {"csList": DEFAULT_CS}})
                elif path == "/plateau/chat/set_csstatus":
                    status = ((body.get("data") or {}).get("status"))
                    with server.state.lock:
                        server.state.cs_status = status
                    self._json({"success": True})
                elif path == "/plateau/chat/latest_conversations":
                    self._json({"success": True, "result": {"conversations": []}})
                elif path == "/plateau/chat/list":
                    self._json({"success": True, "result": {"messages": [], "has_more": False}})
                elif path == "/_mock/push":
                    raw = body.get("payload") or make_push(
                        str(body.get("content") or "你好"),
                        uid=str(body.get("uid") or "buyer-1"),
                        nickname=str(body.get("nickname") or "测试买家"),
                    )
                    if server._loop:
                        future = asyncio.run_coroutine_threadsafe(server.push(raw), server._loop)
                        try:
                            future.result(timeout=5)
                        except Exception:  # noqa: BLE001
                            pass
                    self._json({"success": True})
                elif path == "/_mock/human":
                    raw = make_push(
                        str(body.get("content") or "您好，我是人工客服，我来为您处理"),
                        uid=str(body.get("uid") or "buyer-1"),
                        from_role="mall_cs",
                    )
                    if server._loop:
                        future = asyncio.run_coroutine_threadsafe(server.push(raw), server._loop)
                        try:
                            future.result(timeout=5)
                        except Exception:  # noqa: BLE001
                            pass
                    self._json({"success": True})
                else:
                    self._json({"success": False, "error_msg": f"mock 未实现 {path}"}, 404)

        return Handler


# ---------------------------------------------------------------------------
# 独立运行（手工联调）
# ---------------------------------------------------------------------------
async def _serve_forever(http_port: int, ws_port: int) -> None:
    server = await MockPDDServer(http_port=http_port, ws_port=ws_port).start()
    print(f"Mock 拼多多服务已启动")
    print(f"  HTTP: {server.base_url}")
    print(f"  WS  : {server.ws_url}")
    print(f"  状态: {server.base_url}/_mock/state")
    print("按 Ctrl+C 停止")
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await server.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description="拼多多 Mock 服务")
    parser.add_argument("--http-port", type=int, default=9080)
    parser.add_argument("--ws-port", type=int, default=9081)
    args = parser.parse_args()
    try:
        asyncio.run(_serve_forever(args.http_port, args.ws_port))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

