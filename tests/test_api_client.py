# -*- coding: utf-8 -*-
"""拼多多接口客户端测试（对 Mock 服务）。"""
from __future__ import annotations

import asyncio

from mock.pdd_mock import MockPDDServer
from pdd_cs.pdd.api import PDDClient


def _run(coro):
    return asyncio.run(coro)


def test_api_against_mock():
    async def scenario():
        mock = await MockPDDServer().start()
        try:
            client = PDDClient(
                {"PASS_ID": "mock", "MSA": "mock"},
                base_url=mock.base_url,
                user_agent="test-agent",
                timeout=5,
                max_retries=1,
                min_send_interval=0,
            )
            assert client.get_token() == "mock-token"
            assert client.set_cs_status(True)
            assert mock.state.cs_status == 1

            result = client.send_text("buyer-1", "您好，测试消息")
            assert result.get("success") is True
            client.send_image("buyer-1", "https://img.example/1.jpg")
            client.move_conversation("buyer-1", "1001", "测试转人工")

            cs_list = client.get_assign_cs_list()
            assert cs_list and cs_list[0]["cs_uid"] == "1001"
            assert cs_list[0]["cs_name"] == "人工客服小美"

            info = client.get_user_info()
            assert info.get("mall_name") == "测试店铺"
            assert client.get_recent_conversations() == []

            await asyncio.sleep(0.05)
            contents = [item["content"] for item in mock.state.sent]
            assert "您好，测试消息" in contents
            assert mock.state.transfers[0]["uid"] == "buyer-1"
            assert mock.state.transfers[0]["cs_uid"] == "1001"
        finally:
            await mock.stop()

    _run(scenario())


def test_empty_text_rejected():
    client = PDDClient(
        {"PASS_ID": "mock"},
        base_url="http://127.0.0.1:1",
        user_agent="test",
        timeout=1,
        max_retries=1,
        min_send_interval=0,
    )
    try:
        client.send_text("buyer-1", "  ")
    except Exception as exc:  # noqa: BLE001
        from pdd_cs.pdd.api import PDDSendRejected

        assert isinstance(exc, PDDSendRejected)
    else:  # pragma: no cover
        raise AssertionError("空消息应被拒绝")
