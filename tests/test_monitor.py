# -*- coding: utf-8 -*-
"""掉线告警 / 心跳上报 / 严格健康检查测试。"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pdd_cs.alerts import EVENT_SHOP_OFFLINE, EVENT_SHOP_ONLINE, HeartbeatReporter
from pdd_cs.config import Config, PROJECT_ROOT
from pdd_cs.engine.service import PDDAutoReplyService
from pdd_cs.server import DashboardServer


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
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        _CaptureHandler.received.append({"method": "GET", "path": self.path})
        body = b"ok"
        self.send_response(200)
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
        return f"http://{host}:{port}/hook"

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    @property
    def received(self) -> list[dict]:
        return _CaptureHandler.received


def _wait(condition, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


def _config(tmp_path, **overrides) -> Config:
    cfg = Config.load(PROJECT_ROOT / "config" / "config.example.yaml")
    cfg.data_dir = tmp_path / "data"
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


class _FakeChannel:
    def __init__(self, connected: bool) -> None:
        self.connected = connected
        self.state = "connected" if connected else "error"

    def status_dict(self) -> dict:
        return {"state": self.state, "connected": self.connected}


# ---------------------------------------------------------------------------
# 心跳
# ---------------------------------------------------------------------------
def test_heartbeat_reporter():
    server = _CaptureServer()
    server.start()
    try:
        cfg = Config.load(PROJECT_ROOT / "config" / "config.example.yaml")
        cfg.monitor.heartbeat_url = server.url
        cfg.monitor.heartbeat_method = "POST"
        reporter = HeartbeatReporter(cfg.monitor)
        assert reporter.enabled
        status = {
            "shops": [
                {"shop_id": "shop-a", "channel": {"connected": True}},
                {"shop_id": "shop-b", "channel": {"connected": False}},
            ],
            "today": {"inbound_messages": 3},
            "startup_errors": {},
        }
        assert reporter.send(status)
        assert _wait(lambda: server.received)
        payload = server.received[-1]
        assert payload["event"] == "heartbeat"
        assert payload["shops_total"] == 2
        assert payload["shops_connected"] == 1
        assert payload["disconnected"] == ["shop-b"]
        assert payload["ok"] is False
        assert reporter.stats()["sent"] == 1
    finally:
        server.stop()


def test_heartbeat_get_and_disabled():
    server = _CaptureServer()
    server.start()
    try:
        cfg = Config.load(PROJECT_ROOT / "config" / "config.example.yaml")
        cfg.monitor.heartbeat_url = server.url
        cfg.monitor.heartbeat_method = "GET"
        reporter = HeartbeatReporter(cfg.monitor)
        assert reporter.send({"shops": [], "today": {}, "startup_errors": {}})
        assert _wait(lambda: server.received)
        assert server.received[-1]["method"] == "GET"
        assert "ok=1" in server.received[-1]["path"]
    finally:
        server.stop()

    cfg2 = Config.load(PROJECT_ROOT / "config" / "config.example.yaml")
    reporter2 = HeartbeatReporter(cfg2.monitor)
    assert not reporter2.enabled
    assert reporter2.send({"shops": []}) is False


# ---------------------------------------------------------------------------
# 掉线告警
# ---------------------------------------------------------------------------
def test_offline_and_recovery_alerts(tmp_path):
    server = _CaptureServer()
    server.start()
    try:
        cfg = _config(tmp_path)
        cfg.alerts.enabled = True
        cfg.alerts.webhook_url = server.url
        cfg.alerts.events = [EVENT_SHOP_OFFLINE, EVENT_SHOP_ONLINE]
        cfg.alerts.offline_after_seconds = 30
        cfg.alerts.offline_repeat_seconds = 3600
        service = PDDAutoReplyService(cfg)
        try:
            worker = service.default_worker
            assert worker is not None
            # 未启动：第一次只是记录掉线开始，不告警
            assert service.check_offline_alerts(now=100.0) == []
            # 超过阈值：告警
            events = service.check_offline_alerts(now=140.0)
            assert any(e["event"] == "shop_offline" and e["sent"] for e in events)
            assert _wait(lambda: server.received)
            payload = server.received[-1]
            assert payload["event"] == EVENT_SHOP_OFFLINE
            assert "掉线" in payload["title"]

            # 未恢复且未到重复间隔：不重复告警
            assert service.check_offline_alerts(now=200.0) == []
            # 到重复间隔：再次告警
            events = service.check_offline_alerts(now=140.0 + 3601)
            assert any(e["event"] == "shop_offline" for e in events)

            # 恢复：发恢复通知
            worker.channel = _FakeChannel(True)
            events = service.check_offline_alerts(now=4000.0)
            assert any(e["event"] == "shop_online" and e["sent"] for e in events)
            assert _wait(lambda: any(p["event"] == EVENT_SHOP_ONLINE for p in server.received))
            assert service.status()["monitor"]["offline_shops"] == []
        finally:
            service.store.close()
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# 严格健康检查
# ---------------------------------------------------------------------------
def _http_get(url: str):
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        return exc.code, json.loads(body) if body else {}


def test_healthz_strict(tmp_path):
    cfg = _config(tmp_path)
    cfg.server.host = "127.0.0.1"
    cfg.server.port = 0
    cfg.dashboard.enabled = False
    service = PDDAutoReplyService(cfg)
    dashboard = DashboardServer(cfg, service)
    dashboard.start()
    try:
        base = dashboard.url
        code, payload = _http_get(base + "healthz")
        assert code == 200 and payload["ok"] is True
        # 服务未启动 / 店铺未连接：strict 必须是 503
        code, payload = _http_get(base + "healthz?strict=1")
        assert code == 503
        assert payload["ok"] is False
        assert payload["degraded"], "应明确指出掉线的店铺"
        assert payload["shops"]
    finally:
        dashboard.stop()
        service.store.close()


def test_monitor_config_defaults():
    cfg = Config.load(PROJECT_ROOT / "config" / "config.example.yaml")
    assert cfg.monitor.heartbeat_interval_seconds == 300
    assert cfg.monitor.heartbeat_method == "POST"
    assert cfg.monitor.heartbeat_url == ""
    assert cfg.alerts.offline_after_seconds >= 30
    assert cfg.alerts.offline_repeat_seconds >= 300
