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
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from . import __version__
from .config import Config
from .engine.service import PDDAutoReplyService
from .knowledge_templates import ensure_knowledge_templates
from .web_admin import (
    LoginManager,
    config_path,
    SettingsError,
    add_shop,
    apply_settings,
    apply_style,
    delete_shop,
    delete_shop_cookie,
    list_knowledge,
    load_keywords,
    read_knowledge,
    read_persona,
    save_keywords,
    settings_snapshot,
    shop_snapshots,
    style_options_snapshot,
    test_alert,
    test_llm,
    update_shop,
    write_knowledge,
    write_persona,
)

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

_LOGIN_PAGE_PATH = _WEB_DIR / "login.html"
_LOGIN_FALLBACK = (
    "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
    "<title>登录 · 拼多多智能客服</title></head>"
    "<body style='font-family:Sans-Serif;background:#0a1a33;color:#fff;display:grid;"
    "place-items:center;height:100vh;margin:0'><div><h1>安全登录</h1>"
    "<p>登录页文件缺失，请检查 pdd_cs/web/login.html。</p></div></body></html>"
)


def _load_login_page() -> str:
    try:
        html = _LOGIN_PAGE_PATH.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover
        logger.error("读取登录页面失败: %s", exc)
        return _LOGIN_FALLBACK
    return html.replace("{{VERSION}}", __version__)


LOGIN_HTML = _load_login_page()

#: 登录失败限速（内存；服务重启后清零）
_LOGIN_MAX_FAILURES = 8
_LOGIN_WINDOW_SECONDS = 300
_LOGIN_LOCK = threading.Lock()
_LOGIN_FAILURES: dict[str, list[float]] = {}


def _normalized_origin(value: str) -> tuple[str, str, int] | None:
    """解析一个 HTTP Origin，拒绝 Origin 不允许携带的路径/参数等。"""
    if not value or "?" in value or "#" in value or any(char.isspace() for char in value):
        return None
    try:
        parsed = urlparse(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.params
            or parsed.query
            or parsed.fragment
            or parsed.netloc.endswith(":")
        ):
            return None
        port = parsed.port
        return (
            parsed.scheme,
            parsed.hostname.lower(),
            port if port is not None else (443 if parsed.scheme == "https" else 80),
        )
    except ValueError:
        return None


class DashboardServer:
    """把服务状态通过本地 HTTP 暴露给浏览器面板。"""

    def __init__(self, config: Config, service: PDDAutoReplyService):
        self.config = config
        self.service = service
        self.admin = LoginManager(config)
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
        admin = self.admin
        auth_token = (config.server.auth_token or "").strip()

        class Handler(BaseHTTPRequestHandler):
            server_version = "pdd-cs/2.1"

            def log_message(self, fmt: str, *args) -> None:  # noqa: A003
                logger.debug("dashboard: " + fmt, *args)

            def _send_json(
                self, payload: Any, status: int = 200, headers: list | None = None
            ) -> None:
                body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                for name, value in headers or []:
                    self.send_header(name, value)
                origin = (self.headers.get("Origin") or "").strip()
                if origin.startswith("chrome-extension://"):
                    self.send_header("Access-Control-Allow-Origin", origin)
                    self.send_header("Vary", "Origin")
                    self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Auth-Token")
                    self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_OPTIONS(self) -> None:  # noqa: N802
                origin = (self.headers.get("Origin") or "").strip()
                self.send_response(204)
                if origin.startswith("chrome-extension://"):
                    self.send_header("Access-Control-Allow-Origin", origin)
                    self.send_header("Vary", "Origin")
                    self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Auth-Token")
                    self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Content-Length", "0")
                self.end_headers()

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

            def _cookie_header(self, token: str) -> str:
                secure = (
                    "; Secure"
                    if (self.headers.get("X-Forwarded-Proto") or "").strip().lower() == "https"
                    else ""
                )
                return (
                    f"pdd_cs_token={token}; Path=/; HttpOnly; SameSite=Lax; "
                    f"Max-Age=31536000{secure}"
                )

            def _clear_cookie_header(self) -> str:
                secure = (
                    "; Secure"
                    if (self.headers.get("X-Forwarded-Proto") or "").strip().lower() == "https"
                    else ""
                )
                return f"pdd_cs_token=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0{secure}"

            def _serve_login(self) -> None:
                self._send_html(
                    LOGIN_HTML,
                    headers=[
                        ("X-Frame-Options", "DENY"),
                        ("X-Content-Type-Options", "nosniff"),
                        ("Referrer-Policy", "no-referrer"),
                        (
                            "Content-Security-Policy",
                            "default-src 'self'; style-src 'unsafe-inline'; "
                            "script-src 'unsafe-inline'; img-src 'self' data:; "
                            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
                        ),
                    ],
                )

            def _client_ip(self) -> str:
                ip = (self.headers.get("X-Real-IP") or "").strip()
                if not ip:
                    ip = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
                return ip or (self.client_address[0] if self.client_address else "unknown")

            def _login_state(self) -> tuple[str, int, int]:
                now = time.monotonic()
                ip = self._client_ip()
                with _LOGIN_LOCK:
                    times = [
                        item
                        for item in _LOGIN_FAILURES.get(ip, [])
                        if now - item < _LOGIN_WINDOW_SECONDS
                    ]
                    if times:
                        _LOGIN_FAILURES[ip] = times
                    else:
                        _LOGIN_FAILURES.pop(ip, None)
                    retry = 0
                    if len(times) >= _LOGIN_MAX_FAILURES:
                        retry = max(1, int(_LOGIN_WINDOW_SECONDS - (now - min(times)) + 0.999))
                    return ip, len(times), retry

            def _record_login_failure(self, ip: str) -> None:
                now = time.monotonic()
                with _LOGIN_LOCK:
                    times = [
                        item
                        for item in _LOGIN_FAILURES.get(ip, [])
                        if now - item < _LOGIN_WINDOW_SECONDS
                    ]
                    times.append(now)
                    _LOGIN_FAILURES[ip] = times

            def _clear_login_failures(self, ip: str) -> None:
                with _LOGIN_LOCK:
                    _LOGIN_FAILURES.pop(ip, None)

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
                            "message": "需要登录：请打开面板登录页输入访问令牌，或使用 X-Auth-Token 请求头",
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

            def _discard_post_body(self) -> None:
                """拒绝请求时安全排空请求体，避免连接被重置。"""
                self.close_connection = True
                previous_timeout = self.connection.gettimeout()
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    if 0 < length <= 128 * 1024:
                        self.connection.settimeout(1.0)
                        self.rfile.read(length)
                except (ValueError, OSError):
                    pass
                finally:
                    try:
                        self.connection.settimeout(previous_timeout)
                    except OSError:
                        pass

            def _post_origin_ok(self) -> bool:
                # 反向代理的访问凭据也是浏览器的隐式权限，必须阻止跨站 POST。
                origin = self.headers.get("Origin")
                # 浏览器扩展登录同步：仍要求 X-Auth-Token，只允许扩展来源。
                if origin and origin.strip().startswith("chrome-extension://"):
                    return True
                if (self.headers.get("Sec-Fetch-Site") or "").strip().lower() == "cross-site":
                    return False
                if origin is None:
                    return True  # 非浏览器 JSON 客户端不发送 Origin。
                scheme = (self.headers.get("X-Forwarded-Proto") or "http").strip().lower()
                host = (self.headers.get("Host") or "").strip()
                expected = _normalized_origin(f"{scheme}://{host}")
                supplied = _normalized_origin(origin)
                return expected is not None and supplied == expected

            def _handle_login(self) -> None:
                ip, failures, retry = self._login_state()
                if retry:
                    self._discard_post_body()
                    self._send_json(
                        {
                            "error": "too_many_attempts",
                            "message": f"尝试次数过多，请 {retry} 秒后再试",
                        },
                        status=429,
                        headers=[("Retry-After", str(retry))],
                    )
                    return
                media_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
                if media_type != "application/json":
                    self._discard_post_body()
                    self._send_json(
                        {"error": "unsupported_media_type", "message": "登录请求必须是 JSON"},
                        status=415,
                    )
                    return
                if not self._post_origin_ok():
                    self._discard_post_body()
                    self._send_json(
                        {"error": "cross_origin", "message": "跨站登录请求已拒绝"},
                        status=403,
                    )
                    return
                data = self._read_json()
                supplied = str(data.get("token") or "").strip()
                if not auth_token:
                    self._send_json({"ok": True, "auth_required": False})
                    return
                if supplied and hmac.compare_digest(supplied, auth_token):
                    self._clear_login_failures(ip)
                    logger.info("dashboard: 登录成功 ip=%s", ip)
                    self._send_json(
                        {"ok": True, "auth_required": True},
                        headers=[("Set-Cookie", self._cookie_header(auth_token))],
                    )
                    return
                self._record_login_failure(ip)
                logger.warning("dashboard: 登录失败 ip=%s attempts=%s", ip, failures + 1)
                self._send_json(
                    {"error": "invalid_token", "message": "访问令牌不正确，请检查后重试"},
                    status=401,
                )

            @staticmethod
            def _shop(query: dict[str, list[str]]) -> Optional[str]:
                value = (query.get("shop", [""])[0] or "").strip()
                return value or None

            def _sync_config(self) -> None:
                service.config = service.config
                admin.config = service.config
                self.admin_sync()

            def admin_sync(self) -> None:
                admin.config = service.config

            def _reload(self) -> dict:
                """应用磁盘上的最新配置（网页保存后调用）。"""
                result = service.submit(service.reload_from_disk(allow_degraded=True), timeout=180)
                admin.config = service.config
                return {
                    "ok": True,
                    "setup_mode": result.get("setup_mode"),
                    "online_shops": result.get("online_shops"),
                    "shops": shop_snapshots(service.config, service.workers),
                    "warnings": result.get("startup_errors", {}),
                }

            def _reload_with(self, new_config) -> dict:
                result = service.submit(service.reload(new_config, allow_degraded=True), timeout=180)
                admin.config = service.config
                return {
                    "ok": True,
                    "setup_mode": result.get("setup_mode"),
                    "online_shops": result.get("online_shops"),
                    "shops": shop_snapshots(service.config, service.workers),
                    "warnings": result.get("startup_errors", {}),
                }

            # -- GET ----------------------------------------------------
            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                path = parsed.path.rstrip("/") or "/"
                query = parse_qs(parsed.query)
                shop = self._shop(query)

                if path != "/healthz" and not self._token_ok(query):
                    if path in {"/", ""}:
                        self._serve_login()
                    else:
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
                if path == "/api/setup":
                    status = service.status()
                    self._send_json(
                        {
                            "config_path": str(service.config.source or ""),
                            "setup_mode": status.get("setup_mode"),
                            "shops": shop_snapshots(service.config, service.workers),
                            "capability": admin.capability(),
                            "settings": settings_snapshot(service.config),
                            "status": {
                                "started": status.get("started"),
                                "online_shops": status.get("online_shops"),
                                "shops_count": status.get("shops_count"),
                            },
                        }
                    )
                    return
                if path == "/api/shops":
                    self._send_json(
                        {
                            "ok": True,
                            "shops": shop_snapshots(service.config, service.workers),
                            "capability": admin.capability(),
                        }
                    )
                    return
                if path == "/api/settings":
                    self._send_json(
                        {
                            "ok": True,
                            "settings": settings_snapshot(service.config),
                            "capability": admin.capability(),
                        }
                    )
                    return
                if path == "/api/login/capability":
                    self._send_json({"ok": True, **admin.capability()})
                    return
                if path == "/api/login/chrome/session":
                    self._send_json({"ok": True, **admin.get_chrome_install()})
                    return
                if path == "/api/login/export":
                    shop_id = (query.get("shop_id", [""])[0] or "").strip()
                    if not shop_id:
                        self._send_json({"error": "缺少 shop_id 参数"}, status=400)
                        return
                    try:
                        self._send_json(admin.export_bundle(shop_id))
                    except SettingsError as exc:
                        self._send_json({"error": str(exc)}, status=400)
                    except Exception as exc:  # noqa: BLE001
                        logger.exception("导出登录态失败")
                        self._send_json({"error": f"导出失败：{exc}"}, status=500)
                    return
                if path.startswith("/api/login/sessions/"):
                    session_id = path.rsplit("/", 1)[-1]
                    try:
                        self._send_json({"ok": True, **admin.get_session(session_id)})
                    except SettingsError as exc:
                        self._send_json({"error": str(exc)}, status=404)
                    return
                if path == "/api/content/style/options":
                    self._send_json({"ok": True, **style_options_snapshot(service.config)})
                    return
                if path == "/api/content/persona":
                    self._send_json({"ok": True, "text": read_persona(service.config)})
                    return
                if path == "/api/content/knowledge":
                    file_name = (query.get("file", [""])[0] or "").strip()
                    if file_name:
                        self._send_json(
                            {
                                "ok": True,
                                "name": file_name,
                                "text": read_knowledge(service.config, file_name),
                            }
                        )
                    else:
                        self._send_json({"ok": True, "files": list_knowledge(service.config)})
                    return
                if path == "/api/rules/keywords":
                    self._send_json({"ok": True, "rules": load_keywords(service.config)})
                    return
                if path == "/api/service/lint":
                    report = service.lint()
                    self._send_json(
                        {
                            "ok": report.ok,
                            "summary": report.summary(),
                            "issues": [
                                {
                                    "level": issue.level,
                                    "source": issue.source,
                                    "category": issue.category,
                                    "matched": issue.matched,
                                    "text": issue.text,
                                }
                                for issue in report.issues[:50]
                            ],
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
                if path == "/api/auth/login":
                    self._handle_login()
                    return
                if not self._token_ok(query):
                    self._discard_post_body()
                    self._reject()
                    return
                media_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
                if media_type != "application/json":
                    self._discard_post_body()
                    self._send_json(
                        {"error": "POST requests require application/json"}, status=415
                    )
                    return
                if not self._post_origin_ok():
                    self._discard_post_body()
                    self._send_json(
                        {"error": "Cross-origin POST requests are not allowed"}, status=403
                    )
                    return
                data = self._read_json()
                try:
                    self._dispatch_post(path, data)
                except SettingsError as exc:
                    self._send_json({"error": str(exc)}, status=400)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("网页后台操作失败: %s", path)
                    self._send_json({"error": f"操作失败：{exc}"}, status=500)

            def _dispatch_post(self, path: str, data: dict) -> None:
                if path == "/api/auth/logout":
                    self._send_json(
                        {"ok": True},
                        headers=[("Set-Cookie", self._clear_cookie_header())],
                    )
                    return
                if path == "/api/simulate":
                    text = str(data.get("text") or "").strip()
                    if not text:
                        self._send_json({"error": "缺少 text"}, status=400)
                        return
                    self._send_json(service.simulate(text, shop_id=data.get("shop") or None))
                    return
                if path == "/api/human/resume":
                    try:
                        result = service.resume_ai(
                            str(data.get("uid") or ""),
                            shop_id=data.get("shop") or data.get("shop_id") or None,
                        )
                    except ValueError as exc:
                        self._send_json({"error": str(exc)}, status=400)
                        return
                    self._send_json(result)
                    return
                if path == "/api/settings":
                    apply_settings(service.config, data)
                    new_config = Config.load(config_path(service.config))
                    result = self._reload_with(new_config)
                    result["settings"] = settings_snapshot(service.config)
                    result["restart_required"] = bool(data.get("server"))
                    self._send_json(result)
                    return
                if path == "/api/settings/test-llm":
                    self._send_json({"ok": True, **test_llm(service.config)})
                    return
                if path == "/api/settings/test-alert":
                    self._send_json({"ok": True, **test_alert(service.config)})
                    return
                if path == "/api/shops/add":
                    add_shop(service.config, str(data.get("id") or ""), str(data.get("name") or ""))
                    self._send_json(self._reload())
                    return
                if path == "/api/shops/update":
                    update_shop(
                        service.config,
                        str(data.get("shop_id") or ""),
                        name=data.get("name") if "name" in data else None,
                        enabled=bool(data["enabled"]) if "enabled" in data else None,
                    )
                    self._send_json(self._reload())
                    return
                if path == "/api/shops/delete":
                    delete_shop(service.config, str(data.get("shop_id") or ""))
                    self._send_json(self._reload())
                    return
                if path == "/api/login/chrome/install":
                    session = admin.start_chrome_install()
                    self._send_json({"ok": True, **session})
                    return
                if path == "/api/login/qr/cancel":
                    result = admin.cancel_qr_login(str(data.get("session_id") or ""))
                    self._send_json({"ok": True, **result})
                    return
                if path == "/api/login/qr":
                    session = admin.start_qr_login(str(data.get("shop_id") or ""))
                    self._send_json({"ok": True, **session})
                    return
                if path == "/api/login/browser":
                    session = admin.start_browser_login(str(data.get("shop_id") or ""))
                    self._send_json({"ok": True, **session})
                    return
                if path == "/api/login/cookie":
                    result = admin.cookie_login(
                        str(data.get("shop_id") or ""), str(data.get("cookie") or "")
                    )
                    reload_result = self._reload()
                    self._send_json({"ok": True, **result, "reload": reload_result})
                    return
                if path == "/api/login/import":
                    result = admin.import_bundle(
                        str(data.get("shop_id") or ""), str(data.get("text") or "")
                    )
                    reload_result = self._reload()
                    self._send_json({"ok": True, **result, "reload": reload_result})
                    return
                if path == "/api/login/logout":
                    removed = delete_shop_cookie(service.config, str(data.get("shop_id") or ""))
                    self._send_json({"ok": True, "removed": removed, **self._reload()})
                    return
                if path == "/api/content/style/apply":
                    style_result = apply_style(service.config, data)
                    reload_result = self._reload()
                    self._send_json({**style_result, "reload": reload_result})
                    return
                if path == "/api/content/persona":
                    write_persona(service.config, str(data.get("text") or ""))
                    result = self._reload()
                    result["message"] = "人设已保存并生效"
                    self._send_json(result)
                    return
                if path == "/api/content/knowledge/templates":
                    result = ensure_knowledge_templates(service.config)
                    reload_result = self._reload()
                    self._send_json(
                        {
                            **result,
                            "ok": True,
                            "files": list_knowledge(service.config),
                            "reload": reload_result,
                        }
                    )
                    return
                if path == "/api/content/knowledge":
                    write_knowledge(
                        service.config,
                        str(data.get("name") or ""),
                        str(data.get("text") or ""),
                    )
                    result = self._reload()
                    result["message"] = "知识库已保存并生效"
                    self._send_json(result)
                    return
                if path == "/api/rules/keywords":
                    rules = data.get("rules") or []
                    count = save_keywords(service.config, rules)
                    reload_result = self._reload()
                    lint_report = service.lint()
                    self._send_json(
                        {
                            **reload_result,
                            "ok": True,
                            "count": count,
                            "lint": {
                                "ok": lint_report.ok,
                                "summary": lint_report.summary(),
                                "issues": [
                                    {
                                        "level": issue.level,
                                        "source": issue.source,
                                        "category": issue.category,
                                        "matched": issue.matched,
                                    }
                                    for issue in lint_report.issues[:20]
                                ],
                            },
                        }
                    )
                    return
                if path == "/api/service/reload":
                    self._send_json(self._reload())
                    return
                self._send_json({"error": "not found"}, status=404)

        return Handler


def _int(value: str, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


__all__ = ["DashboardServer", "PAGE_HTML", "LOGIN_HTML"]
