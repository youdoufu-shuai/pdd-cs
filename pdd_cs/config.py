# -*- coding: utf-8 -*-
"""配置加载与校验（支持多店铺）。

配置文件使用 YAML；路径相对项目根目录解析。所有配置项都有安全默认值。

多店铺配置方式（config/config.yaml）：

    shops:
      - id: shop-a
        name: A店（女装）
        cookie_file: config/secrets/shop-a.json
        enabled: true
        overrides:                # 可选，覆盖该店铺的全局配置
          behavior:
            reply_delay_seconds: 3
          compliance:
            human_handoff:
              assign_cs_name: 小美

    # 不写 shops 时，自动退化为单店铺（使用 pdd.cookie_file / pdd.shop_hint）。

用法：
    cfg = Config.load("config/config.yaml")
    for profile in cfg.shops:           # 店铺列表
        shop_cfg = cfg.for_shop(profile)  # 该店铺的完整配置视图
"""
from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

#: 面板默认端口（刻意选一个不常见的高位端口，避免与常规服务冲突）
DEFAULT_PANEL_PORT = 38617

def _detect_project_root() -> Path:
    """定位项目根目录。

    - 源码 / 仓库方式运行：包目录的上一级就是项目根（有 pyproject.toml + config/）；
    - pip 安装方式运行：使用当前工作目录，用户在哪个目录执行 init/run 就在哪里生成配置。
    """
    package_root = Path(__file__).resolve().parent.parent
    if (package_root / "pyproject.toml").is_file() and (
        package_root / "config" / "config.example.yaml"
    ).is_file():
        return package_root
    return Path.cwd()


PROJECT_ROOT = _detect_project_root()
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"
EXAMPLE_CONFIG_PATH = PROJECT_ROOT / "config" / "config.example.yaml"
#: 随包发布的内置模板（pip 安装模式下 init 用它生成配置/话术）
TEMPLATE_CONFIG_DIR = Path(__file__).resolve().parent / "templates" / "config"


# ---------------------------------------------------------------------------
# 配置默认值：与 config/config.example.yaml 保持一致
# ---------------------------------------------------------------------------
DEFAULTS: dict[str, Any] = {
    "app": {
        "name": "拼多多智能客服",
        "timezone": "Asia/Shanghai",
        "log_level": "INFO",
        "data_dir": "data",
    },
    "server": {
        "host": "127.0.0.1",
        "port": 38617,  # 默认使用不常见的高位端口，避免和别人/系统服务冲突
        "auto_open_dashboard": False,
        "auth_token": "",  # 非本机访问时必填；页面 ?token=xxx 或 X-Auth-Token
    },
    "pdd": {
        "base_url": "https://mms.pinduoduo.com",
        "ws_url": "wss://m-ws.pinduoduo.com/",
        "ws_api_version": "202506091557",
        "cookie_file": "config/secrets/cookies.json",
        "shop_hint": "",
        "cs_uid": "",
        "set_online": True,
        "poll_conversations": True,
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
        ),
        "request_timeout_seconds": 20,
        "max_retries": 3,
    },
    "shops": [],  # 多店铺列表（为空 = 单店铺）
    "llm": {
        "enabled": True,
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "api_key_env": "PDD_CS_LLM_KEY",
        "model": "deepseek-chat",
        "temperature": 0.6,
        "max_tokens": 400,
        "timeout_seconds": 30,
        "max_history_messages": 12,
    },
    "behavior": {
        "reply_delay_seconds": 2.0,
        "reply_delay_jitter": 2.0,
        "context_count": 10,
        "max_reply_chars": 180,
        "default_reply": "您好，收到您的消息～客服正在为您核实，稍后回复您。",
        "no_match": "human",
        "human_takeover_minutes": 15,
        "max_auto_replies_per_conversation": 12,
        "typing_simulation": False,
    },
    "compliance": {
        "disclosure": {
            "enabled": True,
            "first_message": "您好，我是本店智能客服助手～人工客服稍后为您服务。",
            "every_message_suffix": "",
            "transfer_hint": "如需人工客服，请直接回复“人工”。",
            "first_message_only_once": True,
        },
        "sla": {
            "first_response_seconds": 180,
            "warn_seconds": 120,
            "repeat_ask_threshold": 3,
        },
        "anti_duplicate": {
            "enabled": True,
            "min_unique_ratio": 0.86,
            "history_size": 6,
            "max_attempts": 3,
            "random_tokens": True,
        },
        "risk": {
            "session_reply_limit": 6,
            "shop_reply_limit": 40,
            "window_seconds": 300,
            "min_send_interval_seconds": 1.2,
        },
        "human_handoff": {
            "enabled": True,
            "no_reason_remark": "智能客服转人工",
            "assign_cs_name": "",
            "fallback_to_manual": True,
            "notify_message": "已为您转接人工客服，请稍等片刻～",
        },
        "transfer_keywords": [
            "人工", "转人工", "真人", "客服人工", "投诉", "举报", "12315", "工商局",
            "起诉", "律师", "曝光", "媒体", "差评", "仅退款", "退款", "退货", "赔偿",
            "发票", "开发票", "过敏", "受伤", "质量问题", "漏发", "错发", "假货",
        ],
        "negative_emotion_keywords": [
            "垃圾", "骗子", "恶心", "气死", "无语", "什么破", "退钱", "骗人",
            "投诉你", "曝光你", "不想要了", "失望", "太差", "坑人",
        ],
        "amount_handoff_threshold": 100,
    },
    "business_hours": {
        "enabled": False,
        "start": "08:30",
        "end": "22:30",
        "off_hours_reply": "您好，现在是非工作时间，客服上线后会第一时间回复您。可先留言，感谢理解～",
        "off_hours_ai": True,
    },
    "knowledge": {
        "enabled": True,
        "dir": "config/knowledge",
        "top_k": 3,
        "min_score": 1.0,
        "max_chars_per_snippet": 500,
    },
    "rules": {
        "keyword_file": "config/rules/keywords.yaml",
        "transfer_file": "config/rules/transfer.yaml",
        "replace_file": "config/rules/replace.yaml",
        "filter_file": "config/rules/filters.yaml",
        "compliance_file": "config/rules/compliance.yaml",
        "goods_file": "config/rules/goods.yaml",
        "blacklist_file": "config/rules/blacklist.yaml",
        "qc_file": "config/rules/qc.yaml",
    },
    "persona_file": "config/persona.md",
    "dashboard": {"enabled": True, "refresh_seconds": 5},
    # 告警通知（钉钉/飞书/企业微信/自定义 Webhook）
    "alerts": {
        "enabled": False,
        "webhook_url": "",
        "format": "generic",  # generic | dingtalk | feishu | wecom
        "events": [
            "shop_offline", "shop_online", "connection_error", "session_expired",
            "transfer", "transfer_failed", "sla_overdue", "send_failed",
        ],
        "min_interval_seconds": 60,
        "timeout_seconds": 10,
        "title_prefix": "拼多多智能客服",
        "offline_after_seconds": 120,
        "offline_repeat_seconds": 1800,
    },
    # 进程存活心跳（配合 Uptime Kuma / Healthchecks.io / 云监控的“死信开关”）
    "monitor": {
        "heartbeat_url": "",
        "heartbeat_interval_seconds": 300,
        "heartbeat_timeout_seconds": 10,
        "heartbeat_method": "POST",
    },
    # 会话质检
    "qc": {"enabled": True, "days": 7, "max_conversations": 300, "min_messages": 2},
    # 运营洞察
    "insights": {"enabled": True, "days": 7, "top_n": 8},
    # 待处理队列
    "pending": {"overdue_seconds": 120, "max_items": 50},
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "y"}
    if value is None:
        return default
    return bool(value)


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# 子配置
# ---------------------------------------------------------------------------
@dataclass
class ShopProfile:
    """一个拼多多店铺的接入配置。"""

    id: str
    name: str
    cookie_file: Path
    enabled: bool = True
    overrides: dict[str, Any] = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        return self.name or self.id


@dataclass
class ServerConfig:
    host: str
    port: int
    auto_open_dashboard: bool
    auth_token: str


@dataclass
class PDDConfig:
    base_url: str
    ws_url: str
    ws_api_version: str
    cookie_file: Path
    shop_hint: str
    cs_uid: str
    set_online: bool
    poll_conversations: bool
    user_agent: str
    request_timeout_seconds: float
    max_retries: int


@dataclass
class LLMConfig:
    enabled: bool
    base_url: str
    api_key: str
    model: str
    temperature: float
    max_tokens: int
    timeout_seconds: float
    max_history_messages: int

    @property
    def ready(self) -> bool:
        return bool(self.enabled and self.base_url and self.api_key and self.model)


@dataclass
class BehaviorConfig:
    reply_delay_seconds: float
    reply_delay_jitter: float
    context_count: int
    max_reply_chars: int
    default_reply: str
    no_match: str
    human_takeover_minutes: int
    max_auto_replies_per_conversation: int
    typing_simulation: bool


@dataclass
class DisclosureConfig:
    enabled: bool
    first_message: str
    every_message_suffix: str
    transfer_hint: str
    first_message_only_once: bool


@dataclass
class SLAConfig:
    first_response_seconds: float
    warn_seconds: float
    repeat_ask_threshold: int


@dataclass
class AntiDuplicateConfig:
    enabled: bool
    min_unique_ratio: float
    history_size: int
    max_attempts: int
    random_tokens: bool


@dataclass
class RiskConfig:
    session_reply_limit: int
    shop_reply_limit: int
    window_seconds: int
    min_send_interval_seconds: float


@dataclass
class HandoffConfig:
    enabled: bool
    no_reason_remark: str
    assign_cs_name: str
    fallback_to_manual: bool
    notify_message: str


@dataclass
class ComplianceConfig:
    disclosure: DisclosureConfig
    sla: SLAConfig
    anti_duplicate: AntiDuplicateConfig
    risk: RiskConfig
    human_handoff: HandoffConfig
    transfer_keywords: list[str]
    negative_emotion_keywords: list[str]
    amount_handoff_threshold: float


@dataclass
class BusinessHoursConfig:
    enabled: bool
    start: str
    end: str
    off_hours_reply: str
    off_hours_ai: bool


@dataclass
class KnowledgeConfig:
    enabled: bool
    dir: Path
    top_k: int
    min_score: float
    max_chars_per_snippet: int


@dataclass
class DashboardConfig:
    enabled: bool
    refresh_seconds: int


@dataclass
class RuleFiles:
    keyword_file: Path
    transfer_file: Path
    replace_file: Path
    filter_file: Path
    compliance_file: Path
    goods_file: Path
    blacklist_file: Path
    qc_file: Path


@dataclass
class AlertsConfig:
    enabled: bool
    webhook_url: str
    format: str
    events: list[str]
    min_interval_seconds: int
    timeout_seconds: float
    title_prefix: str
    offline_after_seconds: int
    offline_repeat_seconds: int


@dataclass
class MonitorConfig:
    heartbeat_url: str
    heartbeat_interval_seconds: int
    heartbeat_timeout_seconds: float
    heartbeat_method: str


@dataclass
class QCConfig:
    enabled: bool
    days: int
    max_conversations: int
    min_messages: int


@dataclass
class InsightsConfig:
    enabled: bool
    days: int
    top_n: int


@dataclass
class PendingConfig:
    overdue_seconds: int
    max_items: int


# ---------------------------------------------------------------------------
# 主配置
# ---------------------------------------------------------------------------
class Config:
    """项目配置。属性式访问 + 原始字典访问 + 多店铺视图。"""

    def __init__(
        self,
        data: dict[str, Any],
        root: Path,
        source: Optional[Path] = None,
        *,
        shop_id: str = "default",
        shop_name: str = "",
    ):
        self.root = root
        self.source = source
        self.data = data
        self.shop_id = shop_id
        self.shop_name = shop_name or str(data.get("pdd", {}).get("shop_hint", ""))

        app = data.get("app", {})
        self.app_name: str = str(app.get("name", "拼多多智能客服"))
        self.timezone: str = str(app.get("timezone", "Asia/Shanghai"))
        self.log_level: str = str(app.get("log_level", "INFO")).upper()
        self.data_dir: Path = self._path(app.get("data_dir", "data"))
        self.data_dir.mkdir(parents=True, exist_ok=True)

        server = data.get("server", {})
        self.server = ServerConfig(
            host=str(server.get("host", "127.0.0.1")),
            port=_as_int(server.get("port"), DEFAULT_PANEL_PORT),
            auto_open_dashboard=_as_bool(server.get("auto_open_dashboard"), False),
            auth_token=str(server.get("auth_token", "") or os.getenv("PDD_CS_AUTH_TOKEN", "") or ""),
        )

        pdd = data.get("pdd", {})
        self.pdd = PDDConfig(
            base_url=str(pdd.get("base_url", "https://mms.pinduoduo.com")).rstrip("/"),
            ws_url=str(pdd.get("ws_url", "wss://m-ws.pinduoduo.com/")).rstrip("/") + "/",
            ws_api_version=str(pdd.get("ws_api_version", "202506091557")),
            cookie_file=self._path(pdd.get("cookie_file", "config/secrets/cookies.json")),
            shop_hint=str(pdd.get("shop_hint", "")),
            cs_uid=str(pdd.get("cs_uid", "")),
            set_online=_as_bool(pdd.get("set_online"), True),
            poll_conversations=_as_bool(pdd.get("poll_conversations"), True),
            user_agent=str(pdd.get("user_agent") or DEFAULTS["pdd"]["user_agent"]),
            request_timeout_seconds=_as_float(pdd.get("request_timeout_seconds"), 20),
            max_retries=_as_int(pdd.get("max_retries"), 3),
        )

        llm = data.get("llm", {})
        api_key = os.getenv(str(llm.get("api_key_env") or "PDD_CS_LLM_KEY") or "", "") or str(
            llm.get("api_key", "") or ""
        )
        self.llm = LLMConfig(
            enabled=_as_bool(llm.get("enabled"), True),
            base_url=str(llm.get("base_url", "https://api.deepseek.com/v1")).rstrip("/"),
            api_key=api_key.strip(),
            model=str(llm.get("model", "deepseek-chat")),
            temperature=_as_float(llm.get("temperature"), 0.6),
            max_tokens=_as_int(llm.get("max_tokens"), 400),
            timeout_seconds=_as_float(llm.get("timeout_seconds"), 30),
            max_history_messages=_as_int(llm.get("max_history_messages"), 12),
        )

        behavior = data.get("behavior", {})
        self.behavior = BehaviorConfig(
            reply_delay_seconds=_as_float(behavior.get("reply_delay_seconds"), 2.0),
            reply_delay_jitter=_as_float(behavior.get("reply_delay_jitter"), 2.0),
            context_count=_as_int(behavior.get("context_count"), 10),
            max_reply_chars=_as_int(behavior.get("max_reply_chars"), 180),
            default_reply=str(behavior.get("default_reply", "")),
            no_match=str(behavior.get("no_match", "human")),
            human_takeover_minutes=_as_int(behavior.get("human_takeover_minutes"), 15),
            max_auto_replies_per_conversation=_as_int(
                behavior.get("max_auto_replies_per_conversation"), 12
            ),
            typing_simulation=_as_bool(behavior.get("typing_simulation"), False),
        )

        comp = data.get("compliance", {})
        disclosure = comp.get("disclosure", {})
        sla = comp.get("sla", {})
        dup = comp.get("anti_duplicate", {})
        risk = comp.get("risk", {})
        handoff = comp.get("human_handoff", {})
        self.compliance = ComplianceConfig(
            disclosure=DisclosureConfig(
                enabled=_as_bool(disclosure.get("enabled"), True),
                first_message=str(disclosure.get("first_message", "")),
                every_message_suffix=str(disclosure.get("every_message_suffix", "")),
                transfer_hint=str(disclosure.get("transfer_hint", "")),
                first_message_only_once=_as_bool(disclosure.get("first_message_only_once"), True),
            ),
            sla=SLAConfig(
                first_response_seconds=_as_float(sla.get("first_response_seconds"), 180),
                warn_seconds=_as_float(sla.get("warn_seconds"), 120),
                repeat_ask_threshold=_as_int(sla.get("repeat_ask_threshold"), 3),
            ),
            anti_duplicate=AntiDuplicateConfig(
                enabled=_as_bool(dup.get("enabled"), True),
                min_unique_ratio=_as_float(dup.get("min_unique_ratio"), 0.86),
                history_size=_as_int(dup.get("history_size"), 6),
                max_attempts=_as_int(dup.get("max_attempts"), 3),
                random_tokens=_as_bool(dup.get("random_tokens"), True),
            ),
            risk=RiskConfig(
                session_reply_limit=_as_int(risk.get("session_reply_limit"), 6),
                shop_reply_limit=_as_int(risk.get("shop_reply_limit"), 40),
                window_seconds=_as_int(risk.get("window_seconds"), 300),
                min_send_interval_seconds=_as_float(risk.get("min_send_interval_seconds"), 1.2),
            ),
            human_handoff=HandoffConfig(
                enabled=_as_bool(handoff.get("enabled"), True),
                no_reason_remark=str(handoff.get("no_reason_remark", "智能客服转人工")),
                assign_cs_name=str(handoff.get("assign_cs_name", "")),
                fallback_to_manual=_as_bool(handoff.get("fallback_to_manual"), True),
                notify_message=str(handoff.get("notify_message", "")),
            ),
            transfer_keywords=[str(x) for x in comp.get("transfer_keywords", [])],
            negative_emotion_keywords=[str(x) for x in comp.get("negative_emotion_keywords", [])],
            amount_handoff_threshold=_as_float(comp.get("amount_handoff_threshold"), 100),
        )

        hours = data.get("business_hours", {})
        self.business_hours = BusinessHoursConfig(
            enabled=_as_bool(hours.get("enabled"), False),
            start=str(hours.get("start", "08:30")),
            end=str(hours.get("end", "22:30")),
            off_hours_reply=str(hours.get("off_hours_reply", "")),
            off_hours_ai=_as_bool(hours.get("off_hours_ai"), True),
        )
        if self.business_hours.enabled:
            from .utils import parse_hhmm

            parse_hhmm(self.business_hours.start)
            parse_hhmm(self.business_hours.end)

        knowledge = data.get("knowledge", {})
        self.knowledge = KnowledgeConfig(
            enabled=_as_bool(knowledge.get("enabled"), True),
            dir=self._path(knowledge.get("dir", "config/knowledge")),
            top_k=_as_int(knowledge.get("top_k"), 3),
            min_score=_as_float(knowledge.get("min_score"), 1.0),
            max_chars_per_snippet=_as_int(knowledge.get("max_chars_per_snippet"), 500),
        )

        rules = data.get("rules", {})
        self.rules = RuleFiles(
            keyword_file=self._path(rules.get("keyword_file", "config/rules/keywords.yaml")),
            transfer_file=self._path(rules.get("transfer_file", "config/rules/transfer.yaml")),
            replace_file=self._path(rules.get("replace_file", "config/rules/replace.yaml")),
            filter_file=self._path(rules.get("filter_file", "config/rules/filters.yaml")),
            compliance_file=self._path(rules.get("compliance_file", "config/rules/compliance.yaml")),
            goods_file=self._path(rules.get("goods_file", "config/rules/goods.yaml")),
            blacklist_file=self._path(rules.get("blacklist_file", "config/rules/blacklist.yaml")),
            qc_file=self._path(rules.get("qc_file", "config/rules/qc.yaml")),
        )
        self.persona_file = self._path(data.get("persona_file", "config/persona.md"))

        dashboard = data.get("dashboard", {})
        self.dashboard = DashboardConfig(
            enabled=_as_bool(dashboard.get("enabled"), True),
            refresh_seconds=max(1, _as_int(dashboard.get("refresh_seconds"), 5)),
        )

        alerts = data.get("alerts", {})
        self.alerts = AlertsConfig(
            enabled=_as_bool(alerts.get("enabled"), False),
            webhook_url=str(alerts.get("webhook_url", "")),
            format=str(alerts.get("format", "generic")),
            events=[str(x) for x in alerts.get("events", [])],
            min_interval_seconds=max(0, _as_int(alerts.get("min_interval_seconds"), 60)),
            timeout_seconds=_as_float(alerts.get("timeout_seconds"), 10),
            title_prefix=str(alerts.get("title_prefix", "拼多多智能客服")),
            offline_after_seconds=max(30, _as_int(alerts.get("offline_after_seconds"), 120)),
            offline_repeat_seconds=max(300, _as_int(alerts.get("offline_repeat_seconds"), 1800)),
        )

        monitor = data.get("monitor", {})
        self.monitor = MonitorConfig(
            heartbeat_url=str(monitor.get("heartbeat_url", "")),
            heartbeat_interval_seconds=max(60, _as_int(monitor.get("heartbeat_interval_seconds"), 300)),
            heartbeat_timeout_seconds=_as_float(monitor.get("heartbeat_timeout_seconds"), 10),
            heartbeat_method=str(monitor.get("heartbeat_method", "POST")).upper(),
        )

        qc = data.get("qc", {})
        self.qc = QCConfig(
            enabled=_as_bool(qc.get("enabled"), True),
            days=max(1, _as_int(qc.get("days"), 7)),
            max_conversations=max(1, _as_int(qc.get("max_conversations"), 300)),
            min_messages=max(1, _as_int(qc.get("min_messages"), 2)),
        )

        insights = data.get("insights", {})
        self.insights = InsightsConfig(
            enabled=_as_bool(insights.get("enabled"), True),
            days=max(1, _as_int(insights.get("days"), 7)),
            top_n=max(1, _as_int(insights.get("top_n"), 8)),
        )

        pending = data.get("pending", {})
        self.pending = PendingConfig(
            overdue_seconds=max(30, _as_int(pending.get("overdue_seconds"), 120)),
            max_items=max(1, _as_int(pending.get("max_items"), 50)),
        )

        # 店铺列表（为空 = 单店铺，使用 pdd.cookie_file；默认店铺动态生成，
        # 这样运行期修改 pdd.cookie_file 也能立即生效）
        self._shops_explicit: bool = bool(data.get("shops"))
        self._shops: list[ShopProfile] = self._parse_shops(data.get("shops") or [])

    # ------------------------------------------------------------------
    def _path(self, value: str | Path) -> Path:
        p = Path(value)
        return p if p.is_absolute() else (self.root / p)

    def _parse_shops(self, raw: Any) -> list[ShopProfile]:
        profiles: list[ShopProfile] = []
        if not raw:
            return profiles
        if not isinstance(raw, list):
            raise ValueError("shops 配置必须是列表")
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                raise ValueError(f"shops[{index}] 必须是字典")
            shop_id = str(item.get("id") or "").strip()
            if not shop_id:
                raise ValueError(f"shops[{index}] 缺少 id")
            if not shop_id.replace("-", "").replace("_", "").isalnum():
                raise ValueError(f"shops[{index}].id 只能包含字母、数字、- 和 _：{shop_id}")
            cookie = item.get("cookie_file") or f"config/secrets/{shop_id}.json"
            profiles.append(
                ShopProfile(
                    id=shop_id,
                    name=str(item.get("name") or shop_id),
                    cookie_file=self._path(cookie),
                    enabled=_as_bool(item.get("enabled"), True),
                    overrides=item.get("overrides") or {},
                )
            )
        ids = [p.id for p in profiles]
        if len(ids) != len(set(ids)):
            raise ValueError("shops 中存在重复的 id")
        return profiles

    @property
    def shops(self) -> list[ShopProfile]:
        """店铺列表；单店铺模式时按当前 pdd 配置动态生成默认店铺。"""
        if self._shops_explicit:
            return self._shops
        return [
            ShopProfile(
                id="default",
                name=self.shop_name or self.pdd.shop_hint or "默认店铺",
                cookie_file=self.pdd.cookie_file,
                enabled=True,
            )
        ]

    def shop_profile(self, shop_id: str) -> ShopProfile:
        for profile in self.shops:
            if profile.id == shop_id:
                return profile
        raise KeyError(f"未找到店铺: {shop_id}")

    def _live_data(self) -> dict[str, Any]:
        """把当前解析后的配置（含运行时被修改的字段）转回字典，供 for_shop 合并。"""
        from dataclasses import asdict

        base = copy.deepcopy(self.data)
        base["pdd"] = {
            **copy.deepcopy(base.get("pdd", {})),
            **{k: v for k, v in asdict(self.pdd).items() if k not in {"cookie_file"}},
        }
        base["llm"] = asdict(self.llm)
        base["behavior"] = asdict(self.behavior)
        base["business_hours"] = asdict(self.business_hours)
        base["compliance"] = asdict(self.compliance)
        base["knowledge"] = asdict(self.knowledge)
        base["server"] = asdict(self.server)
        base["dashboard"] = asdict(self.dashboard)
        base["alerts"] = asdict(self.alerts)
        base["qc"] = asdict(self.qc)
        base["insights"] = asdict(self.insights)
        base["pending"] = asdict(self.pending)
        base["monitor"] = asdict(self.monitor)
        return base

    def for_shop(self, profile: ShopProfile) -> "Config":
        """生成某个店铺的完整配置视图（全局配置 + 店铺覆盖）。"""
        overrides = copy.deepcopy(profile.overrides or {})
        pdd_override = overrides.setdefault("pdd", {})
        pdd_override["cookie_file"] = str(profile.cookie_file)
        pdd_override["shop_hint"] = profile.display_name
        merged = _deep_merge(self._live_data(), overrides)
        merged["shops"] = []  # 避免递归
        return Config(
            merged,
            root=self.root,
            source=self.source,
            shop_id=profile.id,
            shop_name=profile.display_name,
        )

    # ------------------------------------------------------------------
    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    @property
    def persona(self) -> str:
        try:
            return self.persona_file.read_text(encoding="utf-8")
        except OSError:
            return ""

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not (1 <= self.server.port <= 65535):
            problems.append(f"server.port 非法: {self.server.port}")
        elif self.server.port < 1024:
            problems.append(f"server.port={self.server.port} 属于特权端口，建议改用 1024-65535 之间的高位端口")
        if self.server.auth_token and len(self.server.auth_token) < 16:
            problems.append("server.auth_token 太短，建议至少 16 位随机字符串")
        if not _is_loopback(self.server.host) and not self.server.auth_token:
            problems.append(
                f"server.host={self.server.host} 不是本机地址，必须设置 server.auth_token 或环境变量 PDD_CS_AUTH_TOKEN"
            )
        if self.behavior.no_match not in {"human", "default", "silent"}:
            problems.append("behavior.no_match 只能是 human / default / silent")
        if self.llm.enabled and not self.llm.api_key:
            problems.append(
                "llm.api_key 未配置：可在 config/config.yaml 填写，或设置环境变量 PDD_CS_LLM_KEY；"
                "未配置时将自动降级为仅关键词 / 默认回复"
            )
        if self.compliance.risk.window_seconds <= 0:
            problems.append("compliance.risk.window_seconds 必须大于 0")
        if not (0 <= self.compliance.anti_duplicate.min_unique_ratio <= 1):
            problems.append("compliance.anti_duplicate.min_unique_ratio 必须在 0~1 之间")
        if self.alerts.enabled and not self.alerts.webhook_url:
            problems.append("alerts.enabled=true 但未配置 alerts.webhook_url")
        if self.alerts.format not in {"generic", "dingtalk", "feishu", "wecom"}:
            problems.append("alerts.format 只能是 generic / dingtalk / feishu / wecom")
        if self.monitor.heartbeat_url and not self.monitor.heartbeat_url.startswith(("http://", "https://")):
            problems.append("monitor.heartbeat_url 必须是 http(s) 地址")
        if self.monitor.heartbeat_method not in {"POST", "GET"}:
            problems.append("monitor.heartbeat_method 只能是 POST 或 GET")
        for profile in self.shops:
            try:
                profile.cookie_file.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                problems.append(f"店铺 {profile.id} 的 Cookie 目录不可用: {exc}")
            if not profile.cookie_file.parent.exists():
                problems.append(f"店铺 {profile.id} 的 Cookie 目录不存在: {profile.cookie_file.parent}")
        return problems

    def describe(self) -> str:
        if len(self.shops) == 1:
            profile = self.shops[0]
            return (
                f"{self.app_name} | 店铺: {profile.display_name} | "
                f"LLM: {'已启用 ' + self.llm.model if self.llm.ready else '未启用（关键词/默认回复）'} | "
                f"面板: http://{self.server.host}:{self.server.port}/"
            )
        names = "、".join(p.display_name for p in self.shops if p.enabled)
        return (
            f"{self.app_name} | {len(self.shops)} 个店铺（{names}） | "
            f"LLM: {'已启用 ' + self.llm.model if self.llm.ready else '未启用'} | "
            f"面板: http://{self.server.host}:{self.server.port}/"
        )

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        root = PROJECT_ROOT
        if path is None:
            env_path = os.getenv("PDD_CS_CONFIG", "").strip()
            if env_path:
                path = env_path
            else:
                path = DEFAULT_CONFIG_PATH
                if not path.exists() and EXAMPLE_CONFIG_PATH.exists():
                    path = EXAMPLE_CONFIG_PATH
        config_path = Path(path)
        if not config_path.is_absolute():
            config_path = root / config_path

        raw: dict[str, Any] = {}
        if config_path.exists():
            with config_path.open("r", encoding="utf-8") as fh:
                loaded = yaml.safe_load(fh) or {}
            if not isinstance(loaded, dict):
                raise ValueError(f"配置文件格式错误（应为 YAML 映射）: {config_path}")
            raw = loaded
        data = _deep_merge(DEFAULTS, raw)
        config = cls(data, root=root, source=config_path if config_path.exists() else None)
        return apply_env_overrides(config)


def _is_loopback(host: str) -> bool:
    host = (host or "").strip().lower()
    return host in {"127.0.0.1", "localhost", "::1", "[::1]", "0:0:0:0:0:0:0:1"}


def apply_env_overrides(config: Config) -> Config:
    """服务器部署常用的环境变量覆盖（Docker / systemd / CI 友好）。

    支持：
        PDD_CS_CONFIG       配置文件路径（未显式传 path 时生效）
        PDD_CS_HOST         面板绑定地址（默认 127.0.0.1）
        PDD_CS_PORT         面板端口（默认 38617）
        PDD_CS_AUTH_TOKEN   面板访问令牌（绑非本机地址时必填）
        PDD_CS_DATA_DIR     数据目录（默认 data/）
        PDD_CS_LOG_LEVEL    日志级别
    """
    host = os.getenv("PDD_CS_HOST", "").strip()
    if host:
        config.server.host = host

    port_text = os.getenv("PDD_CS_PORT", "").strip()
    if port_text:
        try:
            port = int(port_text)
        except ValueError as exc:
            raise ValueError(f"环境变量 PDD_CS_PORT 不是合法整数: {port_text!r}") from exc
        if not (1 <= port <= 65535):
            raise ValueError(f"环境变量 PDD_CS_PORT 超出范围: {port}")
        config.server.port = port

    token = os.getenv("PDD_CS_AUTH_TOKEN", "").strip()
    if token:
        config.server.auth_token = token

    data_dir = os.getenv("PDD_CS_DATA_DIR", "").strip()
    if data_dir:
        config.data_dir = config._path(data_dir)
        config.data_dir.mkdir(parents=True, exist_ok=True)

    level = os.getenv("PDD_CS_LOG_LEVEL", "").strip().upper()
    if level:
        config.log_level = level
    return config


def ensure_dirs(config: Config) -> None:
    """确保运行所需目录存在。"""
    for path in (config.data_dir, config.knowledge.dir):
        path.mkdir(parents=True, exist_ok=True)
    for profile in config.shops:
        profile.cookie_file.parent.mkdir(parents=True, exist_ok=True)


__all__ = [
    "Config",
    "ShopProfile",
    "AlertsConfig",
    "QCConfig",
    "InsightsConfig",
    "PendingConfig",
    "ensure_dirs",
    "PROJECT_ROOT",
    "DEFAULT_PANEL_PORT",
    "apply_env_overrides",
    "DEFAULT_CONFIG_PATH",
    "EXAMPLE_CONFIG_PATH",
    "TEMPLATE_CONFIG_DIR",
    "DEFAULTS",
]
