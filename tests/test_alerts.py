# -*- coding: utf-8 -*-
"""告警通知测试：Webhook 格式、节流、失败不影响主流程。"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pdd_cs.alerts import (
    EVENT_SLA_OVERDUE,
    EVENT_TRANSFER,
    AlertNotifier,
)
from pdd_cs.config import Config, PROJECT_ROOT


class _CaptureHandler(BaseHTTPRequestHandler):
    received: list[dict] = []

    def log_message(self, *args) -> None:  # noqa: A003
        pass

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8")
        try:
            _CaptureHandler.received.append(json.loads(raw))
        except json.JSONDecodeError:
            _CaptureHandler.received.append({"raw": raw})
        body = b"{}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _CaptureServer:
    def __init__(self) -> None:
        _CaptureHandler.received = []
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _CaptureHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}/webhook"

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    @property
    def received(self) -> list[dict]:
        return _CaptureHandler.received


def _config(url: str, **overrides):
    cfg = Config.load(PROJECT_ROOT / "config" / "config.example.yaml")
    alerts = cfg.alerts
    alerts.enabled = True
    alerts.webhook_url = url
    alerts.events = overrides.pop("events", [EVENT_TRANSFER, EVENT_SLA_OVERDUE])
    alerts.min_interval_seconds = overrides.pop("min_interval_seconds", 60)
    for key, value in overrides.items():
        setattr(alerts, key, value)
    return cfg


def test_webhook_generic_and_throttle():
    server = _CaptureServer()
    server.start()
    try:
        cfg = _config(server.url)
        notifier = AlertNotifier(cfg.alerts)
        assert notifier.enabled
        assert notifier.notify(
            EVENT_TRANSFER, "已转人工", "客户 uid=1", shop_id="shop-a", shop_name="A店"
        )
        assert server.received, "未收到 Webhook"
        payload = server.received[-1]
        assert payload["event"] == EVENT_TRANSFER
        assert "已转人工" in payload["title"]
        assert payload["shop_id"] == "shop-a"

        # 同事件同店铺在节流窗口内不再发送
        assert notifier.notify(EVENT_TRANSFER, "已转人工", "客户 uid=2", shop_id="shop-a") is False
        # 不同店铺可以发送
        assert notifier.notify(EVENT_TRANSFER, "已转人工", "客户 uid=3", shop_id="shop-b")
        # force 可以突破节流
        assert notifier.notify(
            EVENT_TRANSFER, "已转人工", "客户 uid=4", shop_id="shop-a", force=True
        )
        assert len(server.received) == 3
        assert notifier.stats()["sent"] == 3
    finally:
        server.stop()


def test_webhook_formats():
    server = _CaptureServer()
    server.start()
    try:
        for fmt, expected_key in (
            ("dingtalk", "msgtype"),
            ("feishu", "msg_type"),
            ("wecom", "msgtype"),
            ("generic", "title"),
        ):
            cfg = _config(server.url, format=fmt)
            notifier = AlertNotifier(cfg.alerts)
            assert notifier.notify(
                EVENT_TRANSFER, "格式测试", "内容", shop_id=f"shop-{fmt}", force=True
            )
            payload = server.received[-1]
            assert expected_key in payload, f"{fmt} 格式缺少 {expected_key}: {payload}"
    finally:
        server.stop()


def test_webhook_disabled_and_failure():
    cfg = _config("", events=[EVENT_TRANSFER])
    notifier = AlertNotifier(cfg.alerts)
    assert not notifier.enabled
    assert notifier.notify(EVENT_TRANSFER, "标题", "内容") is False

    # 不可达地址：失败只记录，不抛异常
    cfg2 = _config("http://127.0.0.1:1/not-exist", events=[EVENT_TRANSFER])
    cfg2.alerts.timeout_seconds = 1
    notifier2 = AlertNotifier(cfg2.alerts)
    assert notifier2.notify(EVENT_TRANSFER, "失败测试", "内容", force=True) is False
    assert notifier2.failed_count == 1
    assert notifier2.last_error
