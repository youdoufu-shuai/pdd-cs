# -*- coding: utf-8 -*-
"""网页端管理后台：登录、店铺管理、系统设置、话术/知识库。

终端只负责把程序跑起来；日常所有配置与登录都可以在浏览器面板完成。
本模块只做数据与业务，不依赖 HTTP，方便单元测试。
"""
from __future__ import annotations

import base64
import copy
import json
import logging
import re
import threading
import time
import uuid
import zlib
from pathlib import Path
from typing import Any, Optional

import yaml

from .alerts import EVENT_STARTUP, AlertNotifier
from .config import Config, PROJECT_ROOT
from .engine.ai import LLMClient, LLMError
from .pdd.api import PDDClient, PDDSessionExpired
from .pdd.auth import (
    CookieBundle,
    CookieError,
    CookieStore,
    cookie_age_hours,
    parse_cookie_text,
    validate_cookies,
)
from .pdd.cdp_login import LoginError, find_chrome, login_with_browser
from .utils import now_beijing

logger = logging.getLogger("pdd_cs.web_admin")

SHOP_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,32}$")
KNOWLEDGE_FILE_RE = re.compile(r"^[\w\u4e00-\u9fa5-]{1,48}\.md$")
MASK = "******"

#: 登录态导出码前缀（单行文本，方便从本机复制到服务器）
LOGIN_CODE_PREFIX = "PDDCS1."
LOGIN_BUNDLE_FORMAT = "pdd-cs-login"
LOGIN_BUNDLE_VERSION = 1
MAX_LOGIN_IMPORT_CHARS = 200_000


def encode_login_bundle(payload: dict[str, Any]) -> str:
    """把登录态 payload 压缩成单行「登录码」。"""
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    packed = zlib.compress(raw, 9)
    body = base64.urlsafe_b64encode(packed).decode("ascii").rstrip("=")
    return LOGIN_CODE_PREFIX + body


def decode_login_code(code: str) -> dict[str, Any]:
    text = (code or "").strip()
    if not text.startswith(LOGIN_CODE_PREFIX):
        raise SettingsError("登录码格式不正确：应以 PDDCS1. 开头")
    body = text[len(LOGIN_CODE_PREFIX):]
    try:
        padded = body + "=" * (-len(body) % 4)
        raw = zlib.decompress(base64.urlsafe_b64decode(padded.encode("ascii")))
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - 登录码来自用户粘贴，统一转成可读错误
        raise SettingsError("登录码无法解析，请重新从本机面板导出") from exc
    if not isinstance(payload, dict):
        raise SettingsError("登录码内容格式不正确")
    return payload


def decode_login_text(text: str) -> tuple[dict[str, str], dict[str, Any]]:
    """解析「登录码 / 导出文件 / Cookie 文件 / Cookie 文本」，返回 (cookies, 元信息)。"""
    raw = (text or "").strip()
    if not raw:
        raise SettingsError("请粘贴登录码，或选择从本机导出的登录态文件")
    if len(raw) > MAX_LOGIN_IMPORT_CHARS:
        raise SettingsError("登录态内容过大（超过 200KB），请确认文件是否正确")

    payload: dict[str, Any] = {}
    if raw.startswith(LOGIN_CODE_PREFIX):
        payload = decode_login_code(raw)
    else:
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError:
            try:
                return parse_cookie_text(raw), {}
            except CookieError as exc:
                raise SettingsError(str(exc)) from exc
        if not isinstance(loaded, dict):
            raise SettingsError("无法识别登录态内容：请粘贴登录码、导出的 JSON 文件或 Cookie")
        if isinstance(loaded.get("cookies"), dict):
            payload = loaded
        elif all(isinstance(value, (str, int, float)) for value in loaded.values()):
            # 直接是 {"PASS_ID": "...", "MSA": "..."} 形式的 Cookie 字典
            try:
                return parse_cookie_text(raw), {}
            except CookieError as exc:
                raise SettingsError(str(exc)) from exc
        else:
            raise SettingsError("无法识别登录态内容：文件里没有 cookies 字段")

    version = payload.get("version")
    if version is not None and int(version) > LOGIN_BUNDLE_VERSION:
        raise SettingsError("登录态版本比当前程序新，请先升级服务器上的 pdd-cs")
    cookies = payload.get("cookies")
    if not isinstance(cookies, dict) or not cookies:
        raise SettingsError("登录态里没有可用的 Cookie，请重新从本机导出")
    normalized = {str(key): str(value) for key, value in cookies.items() if str(key)}
    if not normalized:
        raise SettingsError("登录态里没有可用的 Cookie，请重新从本机导出")
    return normalized, payload


class SettingsError(ValueError):
    """网页设置校验失败（返回给前端展示）。"""


# ---------------------------------------------------------------------------
# 配置文件读写
# ---------------------------------------------------------------------------
def config_path(config: Config) -> Path:
    if config.source:
        return Path(config.source)
    return PROJECT_ROOT / "config" / "config.yaml"


def read_config_raw(config: Config) -> dict:
    path = config_path(config)
    if path.exists():
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise SettingsError(f"读取配置文件失败：{exc}") from exc
        if isinstance(loaded, dict):
            return loaded
    return copy.deepcopy(config.data)


def write_config_raw(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# 由网页设置保存（pdd-cs）。完整注释说明见同目录 config.example.yaml\n"
        "# 也可以继续手工编辑本文件\n"
    )
    path.write_text(
        header + yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _secret_hint(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if len(value) <= 4:
        return MASK
    return "…" + value[-4:]


# ---------------------------------------------------------------------------
# 设置快照 / 保存
# ---------------------------------------------------------------------------
def settings_snapshot(config: Config) -> dict[str, Any]:
    """返回可直接渲染到网页表单的设置（密钥做脱敏）。"""
    llm = config.llm
    alerts = config.alerts
    server = config.server
    return {
        "config_path": str(config_path(config)),
        "llm": {
            "enabled": llm.enabled,
            "base_url": llm.base_url,
            "model": llm.model,
            "temperature": llm.temperature,
            "max_tokens": llm.max_tokens,
            "timeout_seconds": llm.timeout_seconds,
            "max_history_messages": llm.max_history_messages,
            "api_key_set": bool(llm.api_key),
            "api_key_hint": _secret_hint(llm.api_key),
        },
        "behavior": {
            "default_reply": config.behavior.default_reply,
            "no_match": config.behavior.no_match,
            "reply_delay_seconds": config.behavior.reply_delay_seconds,
            "reply_delay_jitter": config.behavior.reply_delay_jitter,
            "max_reply_chars": config.behavior.max_reply_chars,
            "human_takeover_minutes": config.behavior.human_takeover_minutes,
            "max_auto_replies_per_conversation": config.behavior.max_auto_replies_per_conversation,
        },
        "business_hours": {
            "enabled": config.business_hours.enabled,
            "start": config.business_hours.start,
            "end": config.business_hours.end,
            "off_hours_reply": config.business_hours.off_hours_reply,
        },
        "alerts": {
            "enabled": alerts.enabled,
            "format": alerts.format,
            "events": list(alerts.events),
            "webhook_set": bool(alerts.webhook_url),
            "webhook_hint": _secret_hint(alerts.webhook_url),
            "offline_after_seconds": alerts.offline_after_seconds,
            "offline_repeat_seconds": alerts.offline_repeat_seconds,
        },
        "server": {
            "host": server.host,
            "port": server.port,
            "auth_required": bool(server.auth_token),
            "auth_token_hint": _secret_hint(server.auth_token),
        },
        "monitor": {
            "heartbeat_set": bool(config.monitor.heartbeat_url),
            "heartbeat_hint": _secret_hint(config.monitor.heartbeat_url),
            "heartbeat_interval_seconds": config.monitor.heartbeat_interval_seconds,
        },
        "qc": {"enabled": config.qc.enabled, "days": config.qc.days},
        "shops_count": len(config.shops),
        "knowledge": {"enabled": config.knowledge.enabled, "dir": str(config.knowledge.dir)},
    }


def _as_bool(value: Any, label: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "是"}:
        return True
    if text in {"0", "false", "no", "off", "否"}:
        return False
    raise SettingsError(f"{label} 应该是布尔开关")


def _as_int(value: Any, label: str, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise SettingsError(f"{label} 必须是整数") from exc
    if not (minimum <= number <= maximum):
        raise SettingsError(f"{label} 必须在 {minimum}~{maximum} 之间")
    return number


def _as_float(value: Any, label: str, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise SettingsError(f"{label} 必须是数字") from exc
    if not (minimum <= number <= maximum):
        raise SettingsError(f"{label} 必须在 {minimum}~{maximum} 之间")
    return number


def _as_text(value: Any, label: str, max_length: int, *, allow_empty: bool = True) -> str:
    text = str(value or "").strip()
    if not text and not allow_empty:
        raise SettingsError(f"{label} 不能为空")
    if len(text) > max_length:
        raise SettingsError(f"{label} 太长（最多 {max_length} 字）")
    return text


def _time_text(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", text):
        raise SettingsError(f"{label} 格式应为 HH:MM（例如 09:00）")
    return text


def apply_settings(config: Config, payload: dict[str, Any]) -> dict[str, Any]:
    """保存网页设置到 config.yaml，返回新配置与是否需要重启。"""
    if not isinstance(payload, dict):
        raise SettingsError("设置内容格式不对")
    raw = read_config_raw(config)
    changed: list[str] = []
    restart_required = False

    llm = payload.get("llm")
    if isinstance(llm, dict):
        section = raw.setdefault("llm", {})
        if "enabled" in llm:
            section["enabled"] = _as_bool(llm["enabled"], "AI 开关")
            changed.append("llm.enabled")
        if "base_url" in llm:
            section["base_url"] = _as_text(llm["base_url"], "AI 接口地址", 200, allow_empty=False)
            changed.append("llm.base_url")
        if "model" in llm:
            section["model"] = _as_text(llm["model"], "AI 模型", 80, allow_empty=False)
            changed.append("llm.model")
        if "temperature" in llm:
            section["temperature"] = _as_float(llm["temperature"], "AI 温度", 0, 2)
            changed.append("llm.temperature")
        if "max_tokens" in llm:
            section["max_tokens"] = _as_int(llm["max_tokens"], "AI 最大回复长度", 50, 2000)
            changed.append("llm.max_tokens")
        api_key = str(llm.get("api_key") or "").strip()
        if api_key and api_key != MASK and not api_key.startswith("…"):
            section["api_key"] = api_key
            changed.append("llm.api_key")
        if llm.get("clear_api_key"):
            section["api_key"] = ""
            changed.append("llm.api_key(clear)")

    behavior = payload.get("behavior")
    if isinstance(behavior, dict):
        section = raw.setdefault("behavior", {})
        if "default_reply" in behavior:
            section["default_reply"] = _as_text(behavior["default_reply"], "默认回复", 300)
            changed.append("behavior.default_reply")
        if "no_match" in behavior:
            value = str(behavior["no_match"]).strip()
            if value not in {"human", "default", "silent"}:
                raise SettingsError("没有匹配答案时只能是 转人工 / 默认回复 / 不回复")
            section["no_match"] = value
            changed.append("behavior.no_match")
        if "reply_delay_seconds" in behavior:
            section["reply_delay_seconds"] = _as_float(behavior["reply_delay_seconds"], "回复延迟", 0, 30)
            changed.append("behavior.reply_delay_seconds")
        if "reply_delay_jitter" in behavior:
            section["reply_delay_jitter"] = _as_float(behavior["reply_delay_jitter"], "延迟抖动", 0, 30)
            changed.append("behavior.reply_delay_jitter")
        if "max_reply_chars" in behavior:
            section["max_reply_chars"] = _as_int(behavior["max_reply_chars"], "单条回复字数", 20, 500)
            changed.append("behavior.max_reply_chars")
        if "human_takeover_minutes" in behavior:
            section["human_takeover_minutes"] = _as_int(
                behavior["human_takeover_minutes"], "人工接管分钟数", 0, 240
            )
            changed.append("behavior.human_takeover_minutes")
        if "max_auto_replies_per_conversation" in behavior:
            section["max_auto_replies_per_conversation"] = _as_int(
                behavior["max_auto_replies_per_conversation"], "单会话自动回复上限", 1, 100
            )
            changed.append("behavior.max_auto_replies_per_conversation")

    business = payload.get("business_hours")
    if isinstance(business, dict):
        section = raw.setdefault("business_hours", {})
        if "enabled" in business:
            section["enabled"] = _as_bool(business["enabled"], "营业时间开关")
            changed.append("business_hours.enabled")
        if "start" in business:
            section["start"] = _time_text(business["start"], "开始时间")
            changed.append("business_hours.start")
        if "end" in business:
            section["end"] = _time_text(business["end"], "结束时间")
            changed.append("business_hours.end")

    alerts = payload.get("alerts")
    if isinstance(alerts, dict):
        section = raw.setdefault("alerts", {})
        if "enabled" in alerts:
            section["enabled"] = _as_bool(alerts["enabled"], "告警开关")
            changed.append("alerts.enabled")
        if "format" in alerts:
            value = str(alerts["format"]).strip()
            if value not in {"generic", "dingtalk", "feishu", "wecom"}:
                raise SettingsError("告警格式只能是 generic / dingtalk / feishu / wecom")
            section["format"] = value
            changed.append("alerts.format")
        if "events" in alerts and isinstance(alerts["events"], list):
            section["events"] = [str(item).strip() for item in alerts["events"] if str(item).strip()]
            changed.append("alerts.events")
        webhook = str(alerts.get("webhook_url") or "").strip()
        if webhook and webhook != MASK and not webhook.startswith("…"):
            if not webhook.startswith(("http://", "https://")):
                raise SettingsError("告警 Webhook 必须是 http(s) 地址")
            section["webhook_url"] = webhook
            changed.append("alerts.webhook_url")
        if alerts.get("clear_webhook"):
            section["webhook_url"] = ""
            changed.append("alerts.webhook_url(clear)")
        if "offline_after_seconds" in alerts:
            section["offline_after_seconds"] = _as_int(
                alerts["offline_after_seconds"], "掉线告警阈值", 30, 3600
            )
            changed.append("alerts.offline_after_seconds")

    server = payload.get("server")
    if isinstance(server, dict):
        section = raw.setdefault("server", {})
        if "port" in server:
            port = _as_int(server["port"], "面板端口", 1024, 65535)
            if port != int(section.get("port", 38617)):
                section["port"] = port
                changed.append("server.port")
                restart_required = True
        if "host" in server:
            host = _as_text(server["host"], "绑定地址", 80, allow_empty=False)
            if host != section.get("host", "127.0.0.1"):
                section["host"] = host
                changed.append("server.host")
                restart_required = True
        auth_token = str(server.get("auth_token") or "").strip()
        if auth_token and auth_token != MASK and not auth_token.startswith("…"):
            if len(auth_token) < 16:
                raise SettingsError("访问令牌至少 16 位")
            section["auth_token"] = auth_token
            changed.append("server.auth_token")
            restart_required = True
        if server.get("clear_auth_token"):
            section["auth_token"] = ""
            changed.append("server.auth_token(clear)")
            restart_required = True

    if changed:
        write_config_raw(config_path(config), raw)
    new_config = Config.load(config_path(config))
    problems = new_config.validate()
    return {
        "ok": True,
        "changed": changed,
        "restart_required": restart_required,
        "warnings": [problem for problem in problems if "api_key" not in problem],
        "settings": settings_snapshot(new_config),
        "shops": shop_snapshots(new_config),
    }


# ---------------------------------------------------------------------------
# 店铺管理
# ---------------------------------------------------------------------------
def _raw_shops(raw: dict[str, Any]) -> list[dict[str, Any]]:
    shops = raw.get("shops")
    if isinstance(shops, list) and shops:
        return [dict(item) for item in shops if isinstance(item, dict)]
    pdd = raw.get("pdd", {}) or {}
    return [
        {
            "id": "default",
            "name": pdd.get("shop_hint") or "默认店铺",
            "cookie_file": pdd.get("cookie_file") or "config/secrets/cookies.json",
            "enabled": True,
        }
    ]


def _resolve_cookie_file(config: Config, shop: dict[str, Any]) -> Path:
    value = Path(str(shop.get("cookie_file") or f"config/secrets/{shop.get('id')}.json"))
    return value if value.is_absolute() else PROJECT_ROOT / value


def _find_shop(raw_shops: list[dict[str, Any]], shop_id: str) -> dict[str, Any]:
    for shop in raw_shops:
        if str(shop.get("id")) == shop_id:
            return shop
    raise SettingsError(f"店铺不存在：{shop_id}")


def shop_snapshots(config: Config, workers: Optional[dict] = None) -> list[dict[str, Any]]:
    """配置里的店铺 + 运行时状态合并，供网页展示。"""
    result: list[dict[str, Any]] = []
    workers = workers or {}
    for profile in config.shops:
        cookie_path = Path(profile.cookie_file)
        exists = cookie_path.exists()
        age = None
        if exists:
            try:
                bundle = CookieStore(cookie_path).load()
                age = cookie_age_hours(bundle.saved_at) if bundle else None
            except CookieError:
                age = None
        worker = workers.get(profile.id)
        result.append(
            {
                "id": profile.id,
                "name": profile.display_name,
                "enabled": bool(profile.enabled),
                "cookie_file": str(cookie_path),
                "cookie_saved": exists,
                "cookie_age_hours": round(age, 1) if age is not None else None,
                "runtime": worker.status() if worker else None,
            }
        )
    return result


def add_shop(config: Config, shop_id: str, name: str) -> Config:
    shop_id = (shop_id or "").strip()
    name = (name or "").strip() or shop_id
    if not SHOP_ID_RE.match(shop_id):
        raise SettingsError("店铺 ID 只能包含字母、数字、短横线、下划线（1-32 位）")
    if len(name) > 30:
        raise SettingsError("店铺名称最多 30 个字")
    raw = read_config_raw(config)
    shops = _raw_shops(raw)
    if any(str(item.get("id")) == shop_id for item in shops):
        raise SettingsError(f"店铺 ID 已存在：{shop_id}")
    # 单店铺默认模式下，如果还没登录过，直接替换成新店铺，避免出现两个“默认店铺”
    if len(shops) == 1 and shops[0].get("id") == "default":
        cookie_path = _resolve_cookie_file(config, shops[0])
        if not cookie_path.exists():
            shops = []
    shops.append(
        {
            "id": shop_id,
            "name": name,
            "cookie_file": f"config/secrets/{shop_id}.json",
            "enabled": True,
        }
    )
    raw["shops"] = shops
    write_config_raw(config_path(config), raw)
    return Config.load(config_path(config))


def update_shop(
    config: Config,
    shop_id: str,
    *,
    name: Optional[str] = None,
    enabled: Optional[bool] = None,
) -> Config:
    raw = read_config_raw(config)
    shops = _raw_shops(raw)
    shop = _find_shop(shops, shop_id)
    if name is not None:
        new_name = str(name).strip()
        if not new_name:
            raise SettingsError("店铺名称不能为空")
        if len(new_name) > 30:
            raise SettingsError("店铺名称最多 30 个字")
        shop["name"] = new_name
    if enabled is not None:
        shop["enabled"] = bool(enabled)
    raw["shops"] = shops
    write_config_raw(config_path(config), raw)
    return Config.load(config_path(config))


def delete_shop(config: Config, shop_id: str) -> Config:
    raw = read_config_raw(config)
    shops = _raw_shops(raw)
    if len(shops) <= 1:
        raise SettingsError("至少保留一个店铺；不想接待可以「停用」")
    shops = [item for item in shops if str(item.get("id")) != shop_id]
    if not shops:
        raise SettingsError(f"店铺不存在：{shop_id}")
    raw["shops"] = shops
    write_config_raw(config_path(config), raw)
    return Config.load(config_path(config))


def delete_shop_cookie(config: Config, shop_id: str) -> bool:
    raw = read_config_raw(config)
    shop = _find_shop(_raw_shops(raw), shop_id)
    path = _resolve_cookie_file(config, shop)
    if path.exists():
        path.unlink()
        return True
    return False


# ---------------------------------------------------------------------------
# 网页登录
# ---------------------------------------------------------------------------
class LoginManager:
    """管理浏览器扫码登录会话与 Cookie 粘贴登录。"""

    def __init__(self, config: Config):
        self.config = config
        self._lock = threading.Lock()
        self._sessions: dict[str, dict[str, Any]] = {}
        self._active_browser: Optional[str] = None

    # -- 能力检测 ---------------------------------------------------
    def capability(self) -> dict[str, Any]:
        try:
            chrome = find_chrome()
            return {"browser_login": True, "chrome": chrome}
        except LoginError as exc:
            return {"browser_login": False, "message": str(exc), "chrome": ""}

    def _profile(self, shop_id: str):
        for profile in self.config.shops:
            if profile.id == shop_id:
                return profile
        raise SettingsError(f"店铺不存在：{shop_id}")

    def _session(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                raise SettingsError("登录会话不存在或已过期，请重新点击登录")
            return dict(session)

    def get_session(self, session_id: str) -> dict[str, Any]:
        session = self._session(session_id)
        return {
            "session_id": session["session_id"],
            "shop_id": session["shop_id"],
            "state": session["state"],
            "message": session["message"],
            "cookies": session.get("cookies", 0),
            "shop_name": session.get("shop_name", ""),
            "elapsed_seconds": round(time.time() - session["started_at"], 1),
        }

    # -- 浏览器扫码登录 ---------------------------------------------
    def start_browser_login(self, shop_id: str) -> dict[str, Any]:
        profile = self._profile(shop_id)
        capability = self.capability()
        if not capability["browser_login"]:
            raise SettingsError(
                "当前环境没有可用的 Chrome，无法打开登录窗口。"
                "请在本地电脑登录后使用「粘贴 Cookie」，或把 Cookie 文件传到服务器。"
            )
        with self._lock:
            if self._active_browser:
                raise SettingsError("已有登录窗口在等待操作，请先完成或等待超时")
            session_id = uuid.uuid4().hex[:12]
            self._sessions[session_id] = {
                "session_id": session_id,
                "shop_id": shop_id,
                "state": "pending",
                "message": "已打开 Chrome 登录窗口，请在窗口里完成扫码登录",
                "started_at": time.time(),
                "cookies": 0,
                "shop_name": "",
            }
            self._active_browser = session_id
        thread = threading.Thread(
            target=self._run_browser_login,
            args=(session_id, shop_id, profile),
            name=f"login-{shop_id}",
            daemon=True,
        )
        thread.start()
        return self.get_session(session_id)

    def _update_session(self, session_id: str, **fields: Any) -> None:
        with self._lock:
            if session_id in self._sessions:
                self._sessions[session_id].update(fields)

    def _run_browser_login(self, session_id: str, shop_id: str, profile) -> None:
        store = CookieStore(profile.cookie_file)
        try:
            cookies = login_with_browser(
                store,
                timeout_seconds=600,
                keep_open=False,
                user_data_dir=PROJECT_ROOT / "data" / "chrome-profile" / shop_id,
            )
            shop_name = ""
            try:
                shop_cfg = self.config.for_shop(profile)
                client = PDDClient(
                    cookies,
                    base_url=shop_cfg.pdd.base_url,
                    user_agent=shop_cfg.pdd.user_agent,
                    timeout=15,
                    max_retries=1,
                )
                info = client.validate_session()
                shop_name = str(info.get("mall_name") or info.get("mallName") or "")
            except Exception as exc:  # noqa: BLE001 - 能保存 Cookie 就算登录成功
                logger.info("登录后校验接口失败（不影响保存）：%s", exc)
            self._update_session(
                session_id,
                state="success",
                message=f"登录成功，已保存 {len(cookies)} 个 Cookie",
                cookies=len(cookies),
                shop_name=shop_name,
            )
        except LoginError as exc:
            self._update_session(session_id, state="failed", message=str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.exception("浏览器登录异常")
            self._update_session(session_id, state="failed", message=f"登录失败：{exc}")
        finally:
            with self._lock:
                if self._active_browser == session_id:
                    self._active_browser = None

    # -- Cookie 粘贴登录 ---------------------------------------------
    def cookie_login(self, shop_id: str, cookie_text: str) -> dict[str, Any]:
        profile = self._profile(shop_id)
        try:
            cookies = parse_cookie_text(cookie_text)
        except CookieError as exc:
            raise SettingsError(str(exc)) from exc
        problems = validate_cookies(cookies)
        fatal = [item for item in problems if "未找到登录态" in item]
        if fatal:
            raise SettingsError("；".join(fatal))
        store = CookieStore(profile.cookie_file)
        store.save_text(cookie_text)

        result: dict[str, Any] = {
            "ok": True,
            "cookies": len(cookies),
            "warnings": problems,
            "shop_name": "",
            "session_valid": False,
        }
        shop_cfg = self.config.for_shop(profile)
        client = PDDClient(
            cookies,
            base_url=shop_cfg.pdd.base_url,
            user_agent=shop_cfg.pdd.user_agent,
            timeout=15,
            max_retries=1,
        )
        try:
            info = client.validate_session()
            result["session_valid"] = True
            result["shop_name"] = str(info.get("mall_name") or info.get("mallName") or "")
        except PDDSessionExpired as exc:
            try:
                profile.cookie_file.unlink()
            except OSError:
                pass
            raise SettingsError(f"Cookie 校验失败：{exc}") from exc
        except Exception as exc:  # noqa: BLE001 - 网络受限时保留 Cookie，让用户稍后重试
            result["warnings"] = problems + [f"登录态暂时无法校验（网络/风控原因）：{exc}"]
        return result

    # -- 登录态导出 / 导入（本机登录 → 服务器免 Chrome） ----------------
    def export_bundle(self, shop_id: str) -> dict[str, Any]:
        profile = self._profile(shop_id)
        store = CookieStore(profile.cookie_file)
        try:
            bundle = store.load()
        except CookieError as exc:
            raise SettingsError(f"读取登录态失败：{exc}") from exc
        if not bundle or not bundle.cookies:
            raise SettingsError(
                "该店铺还没有登录态。请先在本地电脑点「扫码登录」或粘贴 Cookie，再导出。"
            )
        payload = {
            "format": LOGIN_BUNDLE_FORMAT,
            "version": LOGIN_BUNDLE_VERSION,
            "shop_id": profile.id,
            "shop_name": bundle.shop_name or profile.display_name,
            "user_name": bundle.user_name,
            "saved_at": bundle.saved_at,
            "exported_at": now_beijing().strftime("%Y-%m-%d %H:%M:%S"),
            "cookies": bundle.cookies,
        }
        return {
            "ok": True,
            "shop_id": profile.id,
            "shop_name": payload["shop_name"],
            "saved_at": payload["saved_at"],
            "cookies": len(bundle.cookies),
            "code": encode_login_bundle(payload),
            "file_text": json.dumps(payload, ensure_ascii=False, indent=2),
            "filename": f"pdd-login-{profile.id}.json",
            "warning": "登录态等同于账号密码：请勿发给他人；导入服务器后建议删除导出的文件。",
        }

    def import_bundle(self, shop_id: str, text: str) -> dict[str, Any]:
        profile = self._profile(shop_id)
        cookies, meta = decode_login_text(text)
        problems = validate_cookies(cookies)
        fatal = [item for item in problems if "未找到登录态" in item]
        if fatal:
            raise SettingsError("；".join(fatal))
        warnings = list(problems)
        source_shop = str(meta.get("shop_id") or "").strip()
        if source_shop and source_shop != profile.id:
            warnings.append(f"这份登录态来自店铺「{source_shop}」，已导入到当前店铺「{profile.id}」")

        bundle = CookieBundle(
            cookies=cookies,
            saved_at=str(meta.get("saved_at") or now_beijing().strftime("%Y-%m-%d %H:%M:%S")),
            shop_name=str(meta.get("shop_name") or ""),
            user_name=str(meta.get("user_name") or ""),
        )
        store = CookieStore(profile.cookie_file)
        store.save(bundle)

        result: dict[str, Any] = {
            "ok": True,
            "imported": True,
            "cookies": len(cookies),
            "warnings": warnings,
            "shop_name": bundle.shop_name,
            "source_shop": source_shop,
            "session_valid": False,
        }
        shop_cfg = self.config.for_shop(profile)
        client = PDDClient(
            cookies,
            base_url=shop_cfg.pdd.base_url,
            user_agent=shop_cfg.pdd.user_agent,
            timeout=15,
            max_retries=1,
        )
        try:
            info = client.validate_session()
            result["session_valid"] = True
            result["shop_name"] = str(info.get("mall_name") or info.get("mallName") or "")
            if result["shop_name"] and result["shop_name"] != bundle.shop_name:
                bundle.shop_name = result["shop_name"]
                store.save(bundle)
        except PDDSessionExpired as exc:
            try:
                profile.cookie_file.unlink()
            except OSError:
                pass
            raise SettingsError(
                f"导入的登录态已失效（{exc}）。请在本地电脑重新扫码登录后再导出。"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - 服务器网络/风控受限时先保留，稍后重试
            result["warnings"] = warnings + [
                f"登录态暂时无法校验（服务器网络/风控原因）：{exc}"
            ]
        return result


# ---------------------------------------------------------------------------
# 话术 / 知识库 / 人设
# ---------------------------------------------------------------------------
def read_persona(config: Config) -> str:
    try:
        return config.persona_file.read_text(encoding="utf-8")
    except OSError:
        return ""


def write_persona(config: Config, text: str) -> None:
    config.persona_file.parent.mkdir(parents=True, exist_ok=True)
    config.persona_file.write_text(text, encoding="utf-8")


def list_knowledge(config: Config) -> list[dict[str, Any]]:
    directory = config.knowledge.dir
    directory.mkdir(parents=True, exist_ok=True)
    items = []
    for path in sorted(directory.glob("*.md")):
        try:
            stat = path.stat()
        except OSError:
            continue
        items.append(
            {
                "name": path.name,
                "size": stat.st_size,
                "modified_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime)),
            }
        )
    return items


def read_knowledge(config: Config, name: str) -> str:
    if not KNOWLEDGE_FILE_RE.match(name):
        raise SettingsError("知识库文件名不合法（只支持中文/字母/数字/短横线 + .md）")
    path = config.knowledge.dir / name
    if not path.exists():
        raise SettingsError(f"知识库文件不存在：{name}")
    return path.read_text(encoding="utf-8")


def write_knowledge(config: Config, name: str, text: str) -> None:
    if not KNOWLEDGE_FILE_RE.match(name):
        raise SettingsError("知识库文件名不合法（只支持中文/字母/数字/短横线 + .md）")
    if len(text) > 200_000:
        raise SettingsError("知识库单文件过大（最多 20 万字）")
    config.knowledge.dir.mkdir(parents=True, exist_ok=True)
    (config.knowledge.dir / name).write_text(text, encoding="utf-8")


def load_keywords(config: Config) -> list[dict[str, Any]]:
    path = config.rules.keyword_file
    if not path.exists():
        return []
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    except (OSError, yaml.YAMLError) as exc:
        raise SettingsError(f"读取关键词话术失败：{exc}") from exc
    return [item for item in loaded if isinstance(item, dict)] if isinstance(loaded, list) else []


def save_keywords(config: Config, rules: list[dict[str, Any]]) -> int:
    if not isinstance(rules, list):
        raise SettingsError("话术格式不对")
    cleaned: list[dict[str, Any]] = []
    for index, rule in enumerate(rules, start=1):
        if not isinstance(rule, dict):
            continue
        keywords = str(rule.get("keywords") or "").strip()
        reply = str(rule.get("reply") or "").strip()
        if not keywords and not reply:
            continue
        if not keywords or not reply:
            raise SettingsError(f"第 {index} 条话术：关键词和回复都要填")
        item: dict[str, Any] = {
            "keywords": keywords,
            "reply": reply,
            "match": str(rule.get("match") or "fuzzy").strip() or "fuzzy",
        }
        rule_id = str(rule.get("id") or "").strip()
        if rule_id:
            item["id"] = rule_id
        try:
            priority = int(rule.get("priority") or 100)
        except (TypeError, ValueError):
            priority = 100
        item["priority"] = priority
        item["enabled"] = bool(rule.get("enabled", True))
        cleaned.append(item)
    if not cleaned:
        raise SettingsError("至少保留一条关键词话术")
    config.rules.keyword_file.parent.mkdir(parents=True, exist_ok=True)
    config.rules.keyword_file.write_text(
        "# 由网页「话术与知识库」保存\n" + yaml.safe_dump(cleaned, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return len(cleaned)


# ---------------------------------------------------------------------------
# 连通性测试
# ---------------------------------------------------------------------------
def test_llm(config: Config) -> dict[str, Any]:
    if not config.llm.enabled:
        raise SettingsError("AI 回复当前是关闭状态，先打开再测试")
    if not config.llm.api_key:
        raise SettingsError("还没有填写大模型 API Key")
    client = LLMClient(
        base_url=config.llm.base_url,
        api_key=config.llm.api_key,
        model=config.llm.model,
        temperature=0.1,
        max_tokens=20,
        timeout=30,
    )
    try:
        reply = client.chat([{"role": "user", "content": "请只回复：连接成功"}])
    except LLMError as exc:
        raise SettingsError(f"AI 连接失败：{exc}") from exc
    return {"ok": True, "model": config.llm.model, "reply": reply[:100]}


def test_alert(config: Config) -> dict[str, Any]:
    notifier = AlertNotifier(config.alerts)
    if not notifier.enabled:
        raise SettingsError("告警还没启用或没填 Webhook 地址")
    sent = notifier.notify(
        EVENT_STARTUP,
        "告警测试",
        "这是一条来自网页设置中心的测试消息。",
        force=True,
    )
    if not sent:
        raise SettingsError("告警发送失败，请检查 Webhook 地址和机器人配置")
    return {"ok": True}


__all__ = [
    "SettingsError",
    "config_path",
    "settings_snapshot",
    "apply_settings",
    "shop_snapshots",
    "add_shop",
    "update_shop",
    "delete_shop",
    "delete_shop_cookie",
    "LoginManager",
    "read_persona",
    "write_persona",
    "list_knowledge",
    "read_knowledge",
    "write_knowledge",
    "load_keywords",
    "save_keywords",
    "test_llm",
    "test_alert",
]
