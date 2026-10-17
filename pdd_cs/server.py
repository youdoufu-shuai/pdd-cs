# -*- coding: utf-8 -*-
"""本地监控面板（零依赖，只用标准库 http.server）。

多店铺：所有列表接口支持 ``?shop=<店铺id>`` 过滤；``/api/status`` 返回聚合状态与
每个店铺的独立状态；另有运营洞察、待处理队列、质检命中、会话搜索等接口。

页面在 ``pdd_cs/web/dashboard.html``，浅色商务风格，无外部前端依赖。

接口：
    GET  /                          面板页面
    GET  /healthz                   健康检查
    GET  /api/status                聚合状态 + 各店铺状态
    GET  /api/conversations         会话列表（?shop=&limit=&search=）
    GET  /api/messages              会话消息（?shop=&uid=）
    GET  /api/risk                  风控日志（?shop=&limit=）
    GET  /api/pending               待处理队列（?shop=）
    GET  /api/insights              运营洞察（?shop=）
    GET  /api/qc                    会话质检（?shop=&days=&force=1）
    GET  /api/search                会话搜索（?q=&shop=）
    GET  /api/simulate              决策测试台（?text=&shop=）
    POST /api/simulate              {"text": "...", "shop": "..."}
"""
from __future__ import annotations

import hmac
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from .config import Config
from .engine.service import PDDAutoReplyService

logger = logging.getLogger("pdd_cs.server")

_WEB_DIR = Path(__file__).resolve().parent / "web"
_PAGE_PATH = _WEB_DIR / "dashboard.html"

_FALLBACK_PAGE = (
    "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
    "<title>拼多多智能客服</title></head><body style='font-family:sans-serif;padding:40px'>"
    "<h1>拼多多智能客服</h1><p>面板页面文件缺失，请检查 pdd_cs/web/dashboard.html。</p>"
    "<p><a href='/api/status'>/api/status</a></p></body></html>"
)


def _load_page() -> str:
    try:
        return _PAGE_PATH.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover
        logger.error("读取面板页面失败: %s", exc)
        return _FALLBACK_PAGE


PAGE_HTML = _load_page()


class DashboardServer:
    """把服务状态通过本地 HTTP 暴露给浏览器面板。"""

    def __init__(self, config: Config, service: PDDAutoReplyService):
        self.config = config
        self.service = service
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def url(self) -> str:
        if self._httpd is not None:
            host, port = self._httpd.server_address[:2]
            return f"http://{host}:{port}/"
        return f"http://{self.config.server.host}:{self.config.server.port}/"

    def start(self) -> None:
        host = (self.config.server.host or "").strip()
        token = (self.config.server.auth_token or "").strip()
        if host not in {"127.0.0.1", "localhost", "::1", "[::1]"} and not token:
            raise RuntimeError(
                f"server.host={host} 不是本机地址，必须设置 server.auth_token"
                "（或环境变量 PDD_CS_AUTH_TOKEN）后再启动面板；"
                "更安全的做法是保持 127.0.0.1，用 SSH 隧道访问。"
            )
        if token and len(token) < 16:
            logger.warning("server.auth_token 长度不足 16 位，建议改用更长的随机字符串")
        handler = self._make_handler()
        self._httpd = ThreadingHTTPServer(
            (self.config.server.host, self.config.server.port), handler
        )
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="dashboard", daemon=True)
        self._thread.start()
        logger.info("监控面板已启动: %s", self.url)

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    # ------------------------------------------------------------------
    def _make_handler(self):
        service = self.service
        config = self.config
        auth_token = (config.server.auth_token or "").strip()

        class Handler(BaseHTTPRequestHandler):
            server_version = "pdd-cs/1.3"

            def log_message(self, fmt: str, *args) -> None:  # noqa: A003
                logger.debug("dashboard: " + fmt, *args)

            def _send_json(self, payload: Any, status: int = 200) -> None:
                body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_html(self, html: str, status: int = 200, headers: list | None = None) -> None:
                body = html.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                for name, value in headers or []:
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _token_ok(self, query: dict[str, list[str]]) -> bool:
                if not auth_token:
                    return True
                supplied = (self.headers.get("X-Auth-Token") or "").strip()
                if not supplied:
                    for part in (self.headers.get("Cookie") or "").split(";"):
                        name, _, value = part.strip().partition("=")
                        if name == "pdd_cs_token":
                            supplied = value.strip()
                            break
                if not supplied:
                    supplied = (query.get("token", [""])[0] or "").strip()
                return bool(supplied) and hmac.compare_digest(supplied, auth_token)

            def _reject(self) -> None:
                if self.path.startswith("/api/"):
                    self._send_json(
                        {
                            "error": "unauthorized",
                            "message": "需要访问令牌：在地址后加 ?token=...，或使用 X-Auth-Token 请求头",
                        },
                        status=401,
                    )
                    return
                html = (
                    "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
                    "<title>需要访问令牌</title></head>"
                    "<body style='font-family:system-ui,sans-serif;background:#f2f4f7;color:#152238;"
                    "max-width:640px;margin:12vh auto;padding:0 24px;line-height:1.7'>"
                    "<h2 style='color:#0f2742'>需要访问令牌</h2>"
                    "<p>面板已启用访问令牌。请在地址后加 <code>?token=你的令牌</code>，"
                    "首次访问成功后浏览器会记住。</p>"
                    "<p style='color:#6b7a90'>令牌在 <code>config/config.yaml</code> 的 "
                    "<code>server.auth_token</code>，或服务器环境变量 "
                    "<code>PDD_CS_AUTH_TOKEN</code>。</p></body></html>"
                )
                self._send_html(html, status=401)

            def _read_json(self) -> dict:
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    raw = self.rfile.read(length) if length else b"{}"
                    data = json.loads(raw.decode("utf-8") or "{}")
                    return data if isinstance(data, dict) else {}
                except (ValueError, json.JSONDecodeError):
                    return {}

            @staticmethod
            def _shop(query: dict[str, list[str]]) -> Optional[str]:
                value = (query.get("shop", [""])[0] or "").strip()
                return value or None

            # -- GET ----------------------------------------------------
            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                path = parsed.path.rstrip("/") or "/"
                query = parse_qs(parsed.query)
                shop = self._shop(query)

                if path != "/healthz" and not self._token_ok(query):
                    self._reject()
                    return

                if path == "/":
                    headers: list[tuple[str, str]] = []
                    query_token = (query.get("token", [""])[0] or "").strip()
                    if auth_token and query_token:
                        secure = (
                            "; Secure"
                            if (self.headers.get("X-Forwarded-Proto") or "").lower() == "https"
                            else ""
                        )
                        headers.append(
                            (
                                "Set-Cookie",
                                f"pdd_cs_token={auth_token}; Path=/; HttpOnly; SameSite=Lax; "
                                f"Max-Age=31536000{secure}",
                            )
                        )
                    self._send_html(PAGE_HTML, headers=headers)
                    return
                if path == "/healthz":
                    status = service.status()
                    strict = query.get("strict", ["0"])[0] in {"1", "true", "yes"}
                    shops = [
                        {
                            "shop_id": shop["shop_id"],
                            "connected": bool(shop["channel"].get("connected")),
                            "state": shop["channel"].get("state"),
                        }
                        for shop in status["shops"]
                    ]
                    degraded = [shop["shop_id"] for shop in shops if not shop["connected"]]
                    if strict:
                        ok = not degraded and not status.get("startup_errors") and status["shops_count"] > 0
                        payload = {
                            "ok": ok,
                            "shops": shops,
                            "degraded": degraded,
                            "startup_errors": status.get("startup_errors", {}),
                        }
                        self._send_json(payload, status=200 if ok else 503)
                        return
                    self._send_json(
                        {
                            "ok": True,
                            "state": status["channel"].get("state"),
                            "shops": status["shops_count"],
                            "connected": status["shops_count"] - len(degraded),
                        }
                    )
                    return
                if path == "/api/status":
                    status = service.status()
                    status["refresh_seconds"] = config.dashboard.refresh_seconds
                    self._send_json(status)
                    return
                if path == "/api/conversations":
                    limit = _int(query.get("limit", ["50"])[0], 50)
                    search = query.get("search", [""])[0]
                    self._send_json(
                        {"data": service.store.list_conversations(limit, shop_id=shop, search=search)}
                    )
                    return
                if path == "/api/messages":
                    uid = query.get("uid", [""])[0]
                    limit = _int(query.get("limit", ["50"])[0], 50)
                    target_shop = shop or (service.default_worker.shop_id if service.default_worker else "default")
                    self._send_json(
                        {"data": service.store.get_messages(uid, limit, shop_id=target_shop) if uid else []}
                    )
                    return
                if path == "/api/risk":
                    limit = _int(query.get("limit", ["50"])[0], 50)
                    self._send_json({"data": service.store.list_risk_logs(limit, shop_id=shop)})
                    return
                if path == "/api/pending":
                    self._send_json({"data": service.pending(shop_id=shop)})
                    return
                if path == "/api/insights":
                    self._send_json(service.insights(shop_id=shop))
                    return
                if path == "/api/qc":
                    days = _int(query.get("days", ["0"])[0], 0) or None
                    force = query.get("force", ["0"])[0] in {"1", "true", "yes"}
                    self._send_json(service.qc_scan(shop_id=shop, days=days, force=force))
                    return
                if path == "/api/search":
                    term = query.get("q", [""])[0]
                    limit = _int(query.get("limit", ["30"])[0], 30)
                    self._send_json({"data": service.search(term, shop_id=shop, limit=limit)})
                    return
                if path == "/api/simulate":
                    text = query.get("text", [""])[0]
                    if not text:
                        self._send_json({"error": "缺少 text 参数"}, status=400)
                        return
                    self._send_json(service.simulate(text, shop_id=shop))
                    return
                self._send_json({"error": "not found"}, status=404)

            # -- POST ---------------------------------------------------
            def do_POST(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                path = parsed.path.rstrip("/") or "/"
                query = parse_qs(parsed.query)
                if not self._token_ok(query):
                    # 先读完请求体，避免客户端收到 connection aborted
                    try:
                        length = int(self.headers.get("Content-Length") or 0)
                        if length:
                            self.rfile.read(length)
                    except (ValueError, OSError):
                        pass
                    self._reject()
                    return
                if path == "/api/simulate":
                    data = self._read_json()
                    text = str(data.get("text") or "").strip()
                    if not text:
                        self._send_json({"error": "缺少 text"}, status=400)
                        return
                    self._send_json(service.simulate(text, shop_id=data.get("shop") or None))
                    return
                self._send_json({"error": "not found"}, status=404)

        return Handler


def _int(value: str, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


__all__ = ["DashboardServer", "PAGE_HTML"]
