# -*- coding: utf-8 -*-
"""监控面板测试（商务版面板 + 全部接口）。"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from urllib.parse import quote

from pdd_cs.server import DashboardServer, PAGE_HTML


def _get(url: str) -> str:
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.read().decode("utf-8")


def _post(url: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def test_page_is_business_style():
    """页面为浅色商务风格，含关键板块与中文文案。"""
    assert "运行中心" in PAGE_HTML
    assert "运营概览" in PAGE_HTML
    assert "会话监控" in PAGE_HTML
    assert "风控与合规事件" in PAGE_HTML
    assert "接待策略" in PAGE_HTML
    assert "决策测试台" in PAGE_HTML
    assert "待处理队列" in PAGE_HTML
    assert "运营洞察" in PAGE_HTML
    assert "会话质检" in PAGE_HTML
    assert "全部店铺" in PAGE_HTML
    assert "#f2f4f7" in PAGE_HTML  # 浅灰背景
    assert "--navy" in PAGE_HTML  # 深蓝品牌色
    assert "linear-gradient" not in PAGE_HTML  # 商务风格不用花哨渐变


def test_dashboard_endpoints(service):
    service.config.server.port = 0
    dashboard = DashboardServer(service.config, service)
    dashboard.start()
    try:
        base = dashboard.url
        html = _get(base)
        assert "运行中心" in html

        health = json.loads(_get(base + "healthz"))
        assert health["ok"] is True

        status = json.loads(_get(base + "api/status"))
        assert status["app"]
        assert "sla" in status
        assert "today" in status
        assert "policy" in status
        assert status["shops_count"] >= 1
        assert status["shops"][0]["shop_id"]
        policy = status["policy"]
        assert policy["first_response_seconds"] == 180
        assert 0 < policy["duplicate_threshold"] <= 1
        assert policy["session_reply_limit"] >= 1

        conversations = json.loads(_get(base + "api/conversations"))
        assert "data" in conversations

        risk = json.loads(_get(base + "api/risk"))
        assert "data" in risk

        # GET 测试台
        pending = json.loads(_get(base + "api/pending"))
        assert "data" in pending
        insights = json.loads(_get(base + "api/insights"))
        for key in ("hourly", "transfer_reasons", "top_rules", "knowledge_gaps"):
            assert key in insights
        qc = json.loads(_get(base + "api/qc?force=1"))
        for key in ("hits", "summary", "scanned_conversations"):
            assert key in qc
        search = json.loads(_get(base + "api/search?q=" + quote("不存在")))
        assert search["data"] == []

        simulate = json.loads(_get(base + "api/simulate?text=" + quote("什么时候发货")))
        assert simulate["action"] == "keyword"
        assert simulate["content"]
        assert simulate.get("shop_id")

        # POST 测试台
        simulate2 = _post(base + "api/simulate", {"text": "我要退货"})
        assert simulate2["action"] == "transfer"

        # 缺少参数 -> 400
        try:
            _get(base + "api/simulate")
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
        else:  # pragma: no cover
            raise AssertionError("缺少 text 参数应返回 400")

        # 未知路径 -> 404
        try:
            _get(base + "api/unknown")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
        else:  # pragma: no cover
            raise AssertionError("未知路径应返回 404")
    finally:
        dashboard.stop()


def _request(url: str, token: str = "", method: str = "GET", payload: dict | None = None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Auth-Token"] = token
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8"), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8"), dict(exc.headers)


def test_dashboard_auth_token(service):
    token = "t" * 32
    service.config.server.port = 0
    service.config.server.auth_token = token
    service.config.server.host = "127.0.0.1"
    dashboard = DashboardServer(service.config, service)
    dashboard.start()
    try:
        base = dashboard.url
        # 健康检查始终放行（给容器/监控用）
        code, body, _ = _request(base + "healthz")
        assert code == 200 and json.loads(body)["ok"] is True

        # 页面与接口未带令牌 -> 401
        code, body, _ = _request(base)
        assert code == 401 and "需要访问令牌" in body
        code, body, _ = _request(base + "api/status")
        assert code == 401
        code, body, _ = _request(base + "api/simulate", method="POST", payload={"text": "你好"})
        assert code == 401

        # ?token= 访问页面会写入 Cookie
        code, body, headers = _request(base + "?token=" + token)
        assert code == 200 and "运行中心" in body
        assert "pdd_cs_token=" in headers.get("Set-Cookie", "")

        # 请求头带令牌 -> 正常
        code, body, _ = _request(base + "api/status", token=token)
        assert code == 200
        assert json.loads(body)["app"]
        code, body, _ = _request(base + "api/simulate", token=token, method="POST", payload={"text": "什么时候发货"})
        assert code == 200
    finally:
        dashboard.stop()


def test_public_bind_requires_token(service):
    import pytest

    service.config.server.host = "0.0.0.0"
    service.config.server.port = 0
    service.config.server.auth_token = ""
    dashboard = DashboardServer(service.config, service)
    with pytest.raises(RuntimeError) as excinfo:
        dashboard.start()
    assert "auth_token" in str(excinfo.value)
