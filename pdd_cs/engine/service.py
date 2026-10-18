# -*- coding: utf-8 -*-
"""多店铺智能客服服务编排。

职责：
    - 一个进程管理多个拼多多店铺（每个店铺一个 ShopWorker，各自独立长连接）；
    - 共用规则、知识库、SQLite 存储、告警通知与质检扫描；
    - 对外提供聚合状态、运营洞察、待处理队列、测试台、质检结果、话术体检等能力。

单店铺配置会自动退化为「default」店铺，保持原有用法不变。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any, Optional

from ..alerts import (
    EVENT_SHOP_OFFLINE,
    EVENT_SHOP_ONLINE,
    EVENT_STARTUP,
    AlertNotifier,
    HeartbeatReporter,
)
from ..config import Config, ensure_dirs
from ..store import Store
from ..utils import iso, now_beijing
from .compliance import ComplianceGuard
from .knowledge import KnowledgeBase
from .lint import LintReport, lint_all
from .qc import QCScanner
from .rules import RuleSet, load_rules
from .worker import ShopStartError, ShopWorker

logger = logging.getLogger("pdd_cs.service")

# 状态聚合时用到的数值字段
_SUM_FIELDS = ("conversations", "auto_replies", "waiting_count", "overdue_count")


def _aggregate_sla(workers: list[ShopWorker]) -> dict[str, Any]:
    """把各店铺的 SLA 快照合成一份（面板顶部 KPI 用）。"""
    if not workers:
        return {
            "conversations": 0,
            "auto_replies": 0,
            "avg_first_response_seconds": None,
            "p90_first_response_seconds": None,
            "within_sla_rate": None,
            "sla_threshold_seconds": 180.0,
            "waiting_count": 0,
            "overdue_count": 0,
        }
    snapshots = [worker.sla.snapshot() for worker in workers]
    conversations = sum(snapshot.get("conversations", 0) for snapshot in snapshots)
    auto_replies = sum(snapshot.get("auto_replies", 0) for snapshot in snapshots)
    waiting = sum(snapshot.get("waiting_count", 0) for snapshot in snapshots)
    overdue = sum(snapshot.get("overdue_count", 0) for snapshot in snapshots)

    weighted_avg: Optional[float] = None
    weights = [s for s in snapshots if s.get("avg_first_response_seconds") is not None]
    if weights:
        total = sum(
            (s["avg_first_response_seconds"] or 0) * max(1, s.get("conversations", 0))
            for s in weights
        )
        weight_sum = sum(max(1, s.get("conversations", 0)) for s in weights)
        weighted_avg = round(total / weight_sum, 1) if weight_sum else None

    rates = [s["within_sla_rate"] for s in snapshots if s.get("within_sla_rate") is not None]
    p90s = [
        s["p90_first_response_seconds"]
        for s in snapshots
        if s.get("p90_first_response_seconds") is not None
    ]
    return {
        "conversations": conversations,
        "auto_replies": auto_replies,
        "avg_first_response_seconds": weighted_avg,
        "p90_first_response_seconds": max(p90s) if p90s else None,
        "within_sla_rate": round(sum(rates) / len(rates), 4) if rates else None,
        "sla_threshold_seconds": workers[0].sla.first_response_seconds,
        "waiting_count": waiting,
        "overdue_count": overdue,
    }


class PDDAutoReplyService:
    """多店铺自动客服服务。"""

    def __init__(self, config: Config):
        self.config = config
        ensure_dirs(config)
        self.started_at = now_beijing()
        self.started = False

        self.store = Store(config.data_dir / "pdd_cs.db")
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.rules: RuleSet = RuleSet([], [], [], [], [], [])
        self.knowledge: Optional[KnowledgeBase] = None
        self.guard = ComplianceGuard.from_file(config.compliance, config.rules.compliance_file)
        self.notifier = AlertNotifier(config.alerts)
        self.heartbeat = HeartbeatReporter(config.monitor)
        self.qc = QCScanner(config, self.guard, self.store, config.rules.qc_file)
        self._init_shared()

        self.workers: dict[str, ShopWorker] = {}
        self.startup_errors: dict[str, str] = {}
        self._background: set[asyncio.Task] = set()
        self._alert_task: Optional[asyncio.Task] = None
        # 掉线监测：shop_id -> 掉线开始/上次告警的 monotonic 时间
        self._offline_since: dict[str, float] = {}
        self._offline_alerted: dict[str, float] = {}
        self._build_workers()

    # ------------------------------------------------------------------
    # 共享组件初始化（启动时 / 网页保存设置后重建）
    # ------------------------------------------------------------------
    def _init_shared(self) -> None:
        config = self.config
        self.rules = load_rules(
            config.rules.keyword_file,
            config.rules.transfer_file,
            config.rules.replace_file,
            config.rules.filter_file,
            config.rules.goods_file,
            config.rules.blacklist_file,
        )
        self.knowledge = None
        if config.knowledge.enabled:
            self.knowledge = KnowledgeBase(
                config.knowledge.dir,
                max_chars_per_snippet=config.knowledge.max_chars_per_snippet,
            )
        self.guard = ComplianceGuard.from_file(config.compliance, config.rules.compliance_file)
        self.notifier = AlertNotifier(config.alerts)
        self.heartbeat = HeartbeatReporter(config.monitor)
        self.qc = QCScanner(config, self.guard, self.store, config.rules.qc_file)

    # ------------------------------------------------------------------
    # 构建 / 生命周期
    # ------------------------------------------------------------------
    def _build_workers(self) -> None:
        self.workers = {}
        for profile in self.config.shops:
            if not profile.enabled:
                logger.info("店铺 %s 已在配置中禁用，跳过", profile.id)
                continue
            self.workers[profile.id] = ShopWorker(
                self.config,
                profile,
                store=self.store,
                rules=self.rules,
                knowledge=self.knowledge,
                notifier=self.notifier,
            )

    def shop_ids(self) -> list[str]:
        return list(self.workers.keys())

    def worker(self, shop_id: Optional[str] = None) -> Optional[ShopWorker]:
        if shop_id:
            return self.workers.get(shop_id)
        if not self.workers:
            return None
        return next(iter(self.workers.values()))

    async def _start_workers(self, *, allow_degraded: bool = False) -> list[ShopWorker]:
        """启动所有已建 worker；返回成功启动的 worker 列表。"""
        self.startup_errors = {}
        if not self.workers:
            if allow_degraded:
                return []
            raise RuntimeError("没有启用的店铺，请检查 config/config.yaml 的 shops 配置")

        results = await asyncio.gather(
            *(worker.start() for worker in self.workers.values()), return_exceptions=True
        )
        for worker, result in zip(self.workers.values(), results):
            if isinstance(result, ShopStartError):
                self.startup_errors[worker.shop_id] = str(result)
                logger.error("店铺 %s 启动失败：%s", worker.shop_id, result)
            elif isinstance(result, Exception):
                self.startup_errors[worker.shop_id] = f"{type(result).__name__}: {result}"
                logger.exception("店铺 %s 启动异常", worker.shop_id)

        started = [worker for worker in self.workers.values() if worker.shop_id not in self.startup_errors]
        if not started and not allow_degraded:
            details = "；".join(f"{shop}: {err}" for shop, err in self.startup_errors.items())
            raise RuntimeError(f"所有店铺均启动失败：{details}")
        return started

    def _start_monitor(self) -> None:
        if self._alert_task and not self._alert_task.done():
            return
        self._alert_task = asyncio.create_task(self._monitor_loop(), name="pdd-cs-monitor")

    async def start(self, *, allow_degraded: bool = False) -> None:
        """启动所有启用店铺；单个店铺失败不影响其它店铺。

        allow_degraded=True 时即使所有店铺都登录失败也继续运行面板，
        方便小白在网页里完成登录/设置（网页后台模式）。
        """
        self._loop = asyncio.get_running_loop()
        started = await self._start_workers(allow_degraded=allow_degraded)
        self.started = True
        now = asyncio.get_event_loop().time()
        for shop_id in self.startup_errors:
            self._offline_since[shop_id] = now
        self._start_monitor()
        await self._maybe_heartbeat(force=True)
        if self.notifier.enabled and started:
            shops = "、".join(worker.shop_display_name for worker in started)
            self.notifier.notify(
                EVENT_STARTUP,
                "服务已启动",
                f"店铺：{shops}\n规则：{self.rules.stats()}",
                force=True,
            )
        logger.info(
            "多店铺服务已启动：%d 个店铺（%s）",
            len(started),
            "、".join(worker.shop_id for worker in started) or "等待网页登录",
        )

    async def reload(self, new_config: Config, *, allow_degraded: bool = True) -> dict[str, Any]:
        """网页保存设置后热重载：重建规则/知识库/通知与所有店铺 worker。"""
        self._loop = asyncio.get_running_loop()
        logger.info("正在应用新配置（会短暂重连店铺）…")
        for worker in list(self.workers.values()):
            try:
                await worker.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning("停止店铺 %s 时出错：%s", worker.shop_id, exc)
        self.config = new_config
        ensure_dirs(new_config)
        self._init_shared()
        self._offline_since.clear()
        self._offline_alerted.clear()
        self._build_workers()
        started = await self._start_workers(allow_degraded=allow_degraded)
        self.started = True
        self._start_monitor()
        await self._maybe_heartbeat(force=True)
        logger.info("配置已生效：%d 个店铺在线", len(started))
        return self.status()

    async def reload_from_disk(self, *, allow_degraded: bool = True) -> dict[str, Any]:
        new_config = Config.load(self.config.source)
        return await self.reload(new_config, allow_degraded=allow_degraded)

    def submit(self, coro, timeout: float = 120.0):
        """把协程提交到服务的事件循环（供网页 HTTP 线程调用）。"""
        loop = self._loop
        if loop and loop.is_running():
            return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)
        return asyncio.run(coro)

    async def stop(self) -> None:
        if self._alert_task:
            self._alert_task.cancel()
            try:
                await self._alert_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._alert_task = None
        for task in list(self._background):
            task.cancel()
        for worker in self.workers.values():
            try:
                await worker.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning("停止店铺 %s 失败: %s", worker.shop_id, exc)
        self.started = False
        self._loop = None
        self.store.close()
        logger.info("多店铺服务已停止")

    async def _monitor_loop(self) -> None:
        """周期巡检：掉线告警、超时未回告警、清理过期人工接管、心跳上报。"""
        while True:
            try:
                await asyncio.sleep(10)
                self.store.expire_human_takeovers()
                for worker in self.workers.values():
                    worker.check_sla_alerts()
                self.check_offline_alerts()
                await self._maybe_heartbeat()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("巡检异常: %s", exc)

    # ------------------------------------------------------------------
    # 掉线监测 / 心跳
    # ------------------------------------------------------------------
    @staticmethod
    def _worker_connected(worker: ShopWorker) -> bool:
        channel = getattr(worker, "channel", None)
        return bool(channel is not None and getattr(channel, "connected", False))

    def check_offline_alerts(self, now: Optional[float] = None, *, force: bool = False) -> list[dict[str, Any]]:
        """检查店铺连接状态：掉线超过阈值发告警，恢复后发恢复通知。

        返回本次产生的通知记录（便于测试 / 日志）。
        """
        clock = asyncio.get_event_loop().time() if now is None else now
        config = self.config.alerts
        events: list[dict[str, Any]] = []
        for shop_id, worker in self.workers.items():
            connected = self._worker_connected(worker)
            if not connected:
                since = self._offline_since.setdefault(shop_id, clock)
                offline_seconds = clock - since
                if offline_seconds < config.offline_after_seconds:
                    continue
                last = self._offline_alerted.get(shop_id)
                if not force and last is not None and clock - last < config.offline_repeat_seconds:
                    continue
                channel = getattr(worker, "channel", None)
                state = getattr(channel, "state", "未启动")
                state_text = getattr(state, "value", state)
                error = getattr(worker, "last_error", "") or ""
                text = (
                    f"店铺：{worker.shop_display_name}（{shop_id}）\n"
                    f"已断线：{int(offline_seconds)} 秒\n"
                    f"连接状态：{state_text}\n"
                    f"请检查网络、登录态，或重新运行：python -m pdd_cs login --shop {shop_id}"
                )
                if error:
                    text += f"\n最近错误：{error}"
                sent = self.notifier.notify(
                    EVENT_SHOP_OFFLINE,
                    "店铺掉线",
                    text,
                    shop_id=shop_id,
                    shop_name=worker.shop_display_name,
                    dedupe_key=f"shop_offline:{shop_id}",
                )
                # 成功则按“重复提醒间隔”重试；失败（如 Webhook 暂时不可用）60 秒后再试
                self._offline_alerted[shop_id] = clock if sent else clock - config.offline_repeat_seconds + 60
                events.append({"event": "shop_offline", "shop_id": shop_id, "sent": sent,
                               "offline_seconds": int(offline_seconds)})
                logger.warning("[%s] 店铺掉线 %.0f 秒（告警%s）", shop_id, offline_seconds,
                               "已发送" if sent else "未发送")
            else:
                if shop_id in self._offline_since:
                    offline_seconds = clock - self._offline_since[shop_id]
                    sent = self.notifier.notify(
                        EVENT_SHOP_ONLINE,
                        "店铺已恢复",
                        f"店铺：{worker.shop_display_name}（{shop_id}）\n"
                        f"断线时长：{int(offline_seconds)} 秒",
                        shop_id=shop_id,
                        shop_name=worker.shop_display_name,
                        dedupe_key=f"shop_online:{shop_id}",
                    )
                    events.append({"event": "shop_online", "shop_id": shop_id, "sent": sent,
                                   "offline_seconds": int(offline_seconds)})
                    logger.info("[%s] 店铺已恢复连接（断线 %.0f 秒）", shop_id, offline_seconds)
                    self._offline_since.pop(shop_id, None)
                    self._offline_alerted.pop(shop_id, None)
        return events

    async def _maybe_heartbeat(self, *, force: bool = False) -> None:
        if not self.heartbeat.enabled:
            return
        now = asyncio.get_event_loop().time()
        if not force and now < self.heartbeat.next_due():
            return
        payload = {
            "shops": self.shops_summary(),
            "today": self.store.today_stats(),
            "startup_errors": self.startup_errors,
        }
        await asyncio.to_thread(self.heartbeat.send, payload)

    # ------------------------------------------------------------------
    # 兼容单店铺的访问器（旧代码 / 测试 / CLI 用）
    # ------------------------------------------------------------------
    @property
    def default_worker(self) -> Optional[ShopWorker]:
        return self.worker()

    @property
    def channel(self):
        worker = self.default_worker
        return worker.channel if worker else None

    @property
    def sla(self):
        worker = self.default_worker
        return worker.sla if worker else None

    @property
    def decision(self):
        worker = self.default_worker
        return worker.decision if worker else None

    @property
    def ai(self):
        worker = self.default_worker
        return worker.ai if worker else None

    @property
    def api(self):
        worker = self.default_worker
        return worker.api if worker else None

    @property
    def cookie_store(self):
        worker = self.default_worker
        return worker.cookie_store if worker else None

    @property
    def last_error(self) -> str:
        errors = [worker.last_error for worker in self.workers.values() if worker.last_error]
        errors.extend(self.startup_errors.values())
        return errors[-1] if errors else ""

    # ------------------------------------------------------------------
    # 测试台
    # ------------------------------------------------------------------
    def simulate(self, text: str, *, shop_id: Optional[str] = None) -> dict[str, Any]:
        worker = self.worker(shop_id)
        if worker is None:
            return {"error": "没有可用的店铺工作单元", "shops": self.shop_ids()}
        result = worker.simulate(text)
        result["shops"] = self.shop_ids()
        return result

    def resume_ai(self, uid: str, *, shop_id: Optional[str] = None) -> dict[str, Any]:
        """手动结束人工接管，让该会话下一条客户消息交回 AI。"""
        worker = self.worker(shop_id)
        if worker is None:
            raise ValueError("没有可用的店铺工作单元")
        if not uid.strip():
            raise ValueError("缺少客户 uid")
        worker.resume_ai(uid.strip())
        return {"ok": True, "shop_id": worker.shop_id, "uid": uid.strip()}

    # ------------------------------------------------------------------
    # 状态 / 洞察 / 待处理 / 质检
    # ------------------------------------------------------------------
    def _global_policy(self) -> dict[str, Any]:
        config = self.config
        return {
            "first_response_seconds": config.compliance.sla.first_response_seconds,
            "warn_seconds": config.compliance.sla.warn_seconds,
            "duplicate_threshold": config.compliance.anti_duplicate.min_unique_ratio,
            "session_reply_limit": config.compliance.risk.session_reply_limit,
            "window_seconds": config.compliance.risk.window_seconds,
            "human_takeover_minutes": config.behavior.human_takeover_minutes,
            "disclosure_enabled": config.compliance.disclosure.enabled,
        }

    def status(self) -> dict[str, Any]:
        workers = list(self.workers.values())
        default = self.default_worker
        shops = [worker.status() for worker in workers]
        channel = default.channel.status_dict() if (default and default.channel) else {"state": "stopped"}
        overdue: list[dict[str, Any]] = []
        for worker in workers:
            overdue.extend(worker.status()["overdue"])
        pending = self.pending()
        return {
            "app": self.config.app_name,
            "shop": default.shop_name if len(workers) == 1 and default else f"{len(workers)} 个店铺",
            "shops_count": len(workers),
            "started_at": iso(self.started_at),
            "started": self.started,
            "setup_mode": (not self.workers)
            or (bool(self.workers) and len(self.startup_errors) == len(self.workers)),
            "online_shops": sum(1 for worker in self.workers.values() if self._worker_connected(worker)),
            "channel": channel,
            "llm": {
                "enabled": bool(default and default.ai and default.ai.ready),
                "model": self.config.llm.model if self.config.llm.ready else "",
                "calls": sum(
                    (worker.ai.client.call_count if worker.ai and worker.ai.client else 0)
                    for worker in workers
                ),
                "errors": sum(
                    (worker.ai.client.error_count if worker.ai and worker.ai.client else 0)
                    for worker in workers
                ),
            },
            "rules": self.rules.stats(),
            "knowledge": self.knowledge.stats() if self.knowledge else {"files": 0, "snippets": 0},
            "sla": _aggregate_sla(workers),
            "today": self.store.today_stats(),
            "reply_breakdown": self.store.reply_result_breakdown(),
            "overdue": overdue,
            "pending_count": len(pending),
            "pending": pending[:10],
            "cookie": default.cookie_summary if default else "",
            "last_error": self.last_error,
            "startup_errors": self.startup_errors,
            "business_hours": {
                "enabled": self.config.business_hours.enabled,
                "start": self.config.business_hours.start,
                "end": self.config.business_hours.end,
            },
            "policy": self._global_policy(),
            "alerts": {
                **self.notifier.stats(),
                "offline_after_seconds": self.config.alerts.offline_after_seconds,
                "offline_repeat_seconds": self.config.alerts.offline_repeat_seconds,
            },
            "server": {
                "host": self.config.server.host,
                "port": self.config.server.port,
                "auth_required": bool(self.config.server.auth_token),
                "local_only": self.config.server.host in {"127.0.0.1", "localhost", "::1", "[::1]"},
            },
            "monitor": {
                **self.heartbeat.stats(),
                "offline_shops": [
                    shop_id for shop_id, worker in self.workers.items()
                    if not self._worker_connected(worker)
                ],
            },
            "qc": {
                "enabled": self.config.qc.enabled,
                "rules": len(self.qc.rules),
                "days": self.config.qc.days,
            },
            "shops": shops,
        }

    def shops_summary(self) -> list[dict[str, Any]]:
        return [worker.status() for worker in self.workers.values()]

    def pending(self, shop_id: Optional[str] = None) -> list[dict[str, Any]]:
        rows = self.store.pending_conversations(
            shop_id=shop_id,
            overdue_seconds=self.config.pending.overdue_seconds,
            limit=self.config.pending.max_items,
        )
        for row in rows:
            shop = self.workers.get(str(row.get("shop_id")))
            row["shop_name"] = shop.shop_display_name if shop else row.get("shop_id")
        return rows

    def insights(self, shop_id: Optional[str] = None) -> dict[str, Any]:
        top_n = self.config.insights.top_n
        days = self.config.insights.days
        return {
            "generated_at": iso(now_beijing()),
            "shop_id": shop_id or "all",
            "days": days,
            "hourly": self.store.hourly_counts(shop_id=shop_id, days=1),
            "transfer_reasons": self.store.transfer_reasons(
                shop_id=shop_id, days=days, limit=top_n
            ),
            "top_rules": self.store.top_rules(shop_id=shop_id, days=days, limit=top_n),
            "knowledge_gaps": self.store.knowledge_gaps(
                shop_id=shop_id, days=days, limit=top_n
            ),
            "shops_summary": self.store.shop_today_summary(),
        }

    def qc_scan(
        self,
        shop_id: Optional[str] = None,
        days: Optional[int] = None,
        force: bool = False,
    ) -> dict[str, Any]:
        return self.qc.scan(shop_id=shop_id, days=days, force=force)

    def lint(self) -> LintReport:
        return lint_all(self.config, self.guard)

    def search(self, term: str, shop_id: Optional[str] = None, limit: int = 30) -> list[dict[str, Any]]:
        rows = self.store.search_conversations(term, limit=limit, shop_id=shop_id)
        for row in rows:
            shop = self.workers.get(str(row.get("shop_id")))
            row["shop_name"] = shop.shop_display_name if shop else row.get("shop_id")
        return rows

    # ------------------------------------------------------------------
    # 便捷：单店铺人工接管（供测试 / 运维脚本）
    # ------------------------------------------------------------------
    async def _activate_human_takeover(self, uid: str, *, reason: str = "手动接管") -> None:
        worker = self.default_worker
        if worker is not None:
            await worker._activate_human_takeover(uid, reason=reason)  # noqa: SLF001


__all__ = ["PDDAutoReplyService", "ShopWorker", "ShopStartError"]
