# -*- coding: utf-8 -*-
"""多店铺端到端测试：两个店铺各自独立长连接、独立会话与转人工。"""
from __future__ import annotations

import asyncio
import time

from mock.pdd_mock import MockPDDServer, make_push
from pdd_cs.config import PROJECT_ROOT, Config, ensure_dirs
from pdd_cs.engine.service import PDDAutoReplyService
from pdd_cs.pdd.auth import CookieStore

MOCK_COOKIE = (
    "PASS_ID=mock; SUB_PASS_ID=mock; MSA=mock; SUB_SYS_ID=mock; "
    "pdd_user_id=1; pdd_user_uin=1; anti_content=mock; api_uid=mock"
)


def _wait_until(predicate, timeout: float = 12.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def _build_config(tmp_path, mock_a: MockPDDServer, mock_b: MockPDDServer) -> Config:
    cfg = Config.load(PROJECT_ROOT / "config" / "config.example.yaml")
    data = dict(cfg.data)
    data["app"] = {**cfg.data.get("app", {}), "data_dir": str(tmp_path / "data")}
    data["shops"] = [
        {
            "id": "shop-a",
            "name": "A店",
            "cookie_file": str(tmp_path / "a.json"),
            "overrides": {"pdd": {"base_url": mock_a.base_url, "ws_url": mock_a.ws_url}},
        },
        {
            "id": "shop-b",
            "name": "B店",
            "cookie_file": str(tmp_path / "b.json"),
            "overrides": {"pdd": {"base_url": mock_b.base_url, "ws_url": mock_b.ws_url}},
        },
    ]
    data["llm"] = {**cfg.data.get("llm", {}), "enabled": False}
    data["behavior"] = {**cfg.data.get("behavior", {}), "reply_delay_seconds": 0, "reply_delay_jitter": 0}
    data["compliance"] = {
        **cfg.data.get("compliance", {}),
        "risk": {**cfg.data["compliance"]["risk"], "min_send_interval_seconds": 0},
    }
    data["dashboard"] = {"enabled": False}
    return Config(data, root=PROJECT_ROOT)


def test_multi_shop_end_to_end(tmp_path):
    asyncio.run(_scenario(tmp_path))


async def _scenario(tmp_path) -> None:
    mock_a = await MockPDDServer(shop_name="A店").start()
    mock_b = await MockPDDServer(shop_name="B店").start()
    cfg = _build_config(tmp_path, mock_a, mock_b)
    ensure_dirs(cfg)
    CookieStore(cfg.shop_profile("shop-a").cookie_file).save_text(MOCK_COOKIE)
    CookieStore(cfg.shop_profile("shop-b").cookie_file).save_text(MOCK_COOKIE)

    service = PDDAutoReplyService(cfg)
    try:
        await service.start()
        assert len(service.workers) == 2
        assert await asyncio.to_thread(
            _wait_until,
            lambda: all(w.channel and w.channel.connected for w in service.workers.values()),
            15,
        ), "两个店铺的长连接未全部建立"
        assert service.startup_errors == {}
        # 等服务端把 WS 连接真正登记（避免“已连接但还没登记”的竞态）
        assert await asyncio.to_thread(
            _wait_until,
            lambda: mock_a.state.ws_connections >= 1 and mock_b.state.ws_connections >= 1,
            10,
        )
        await asyncio.sleep(0.3)

        status = service.status()
        assert status["shops_count"] == 2
        assert {s["shop_id"] for s in status["shops"]} == {"shop-a", "shop-b"}
        assert {s["shop_name"] for s in status["shops"]} == {"A店", "B店"}

        # ---- A 店咨询：自动回复，且只落在 shop-a ----
        await mock_a.push_text("什么时候发货？", uid="a-buyer", msg_id="a-1")
        assert await asyncio.to_thread(_wait_until, lambda: len(mock_a.state.sent) >= 1)
        assert await asyncio.to_thread(_wait_until, lambda: len(mock_b.state.sent) == 0)
        assert "发货" in mock_a.state.sent[0]["content"]
        assert service.store.get_conversation("a-buyer", shop_id="shop-a") is not None
        assert service.store.get_conversation("a-buyer", shop_id="shop-b") is None

        # ---- B 店投诉：转人工，只落在 B 店 ----
        await mock_b.push_text("我要投诉，收到的货有问题", uid="b-buyer", msg_id="b-1")
        assert await asyncio.to_thread(_wait_until, lambda: len(mock_b.state.transfers) >= 1)
        assert mock_b.state.transfers[0]["uid"] == "b-buyer"
        assert mock_a.state.transfers == []

        # ---- 人工接管按店铺隔离：A 店人工回复后，A 店暂停；B 店仍自动回复 ----
        await mock_a.push_raw(
            make_push("您好，我是人工客服，我来处理", uid="a-buyer", from_role="mall_cs", msg_id="a-2")
        )
        await asyncio.sleep(0.8)
        assert service.store.get_human_takeover("a-buyer", shop_id="shop-a") is not None
        before_a = len(mock_a.state.sent)
        await mock_a.push_text("还在吗", uid="a-buyer", msg_id="a-3")
        await asyncio.sleep(1.6)
        assert len(mock_a.state.sent) == before_a, "A 店人工接管期间不应自动回复"

        before_b = len(mock_b.state.sent)
        await mock_b.push_text("发什么快递", uid="b-buyer-2", msg_id="b-2")
        assert await asyncio.to_thread(_wait_until, lambda: len(mock_b.state.sent) > before_b)
        content = mock_b.state.sent[-1]["content"]
        assert any(word in content for word in ("中通", "快递", "详情页", "仓库"))

        # ---- 店铺级统计与洞察 ----
        today_a = service.store.today_stats("shop-a")
        today_b = service.store.today_stats("shop-b")
        assert today_a["inbound_messages"] >= 2
        assert today_b["inbound_messages"] >= 2
        insights = service.insights("shop-b")
        assert insights["shop_id"] == "shop-b"
        assert service.search("b-buyer", shop_id="shop-b")

        # ---- 单店铺失败不影响其它店铺 ----
        broken = Config(data={**cfg.data, "shops": [
            {"id": "bad-shop", "name": "未登录店", "cookie_file": str(tmp_path / "missing.json")},
        ]}, root=PROJECT_ROOT)
        bad_service = PDDAutoReplyService(broken)
        try:
            try:
                await bad_service.start()
            except RuntimeError as exc:
                assert "所有店铺均启动失败" in str(exc)
            else:  # pragma: no cover
                raise AssertionError("无有效店铺时应启动失败")
        finally:
            await bad_service.stop()
    finally:
        await service.stop()
        await mock_a.stop()
        await mock_b.stop()
