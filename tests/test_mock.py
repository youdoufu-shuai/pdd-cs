# -*- coding: utf-8 -*-
"""拼多多 Mock 服务测试：HTTP 调试接口 + WebSocket 推送。"""
from __future__ import annotations

import asyncio
import json
import urllib.request

from websockets.asyncio.client import connect

from mock.pdd_mock import MockPDDServer
from pdd_cs.pdd.api import PDDClient


def _http_post(url: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def test_mock_state_and_ws_push():
    async def scenario():
        mock = await MockPDDServer().start()
        try:
            client = PDDClient(
                {"PASS_ID": "mock"},
                base_url=mock.base_url,
                user_agent="test",
                timeout=3,
                max_retries=1,
                min_send_interval=0,
            )
            client.send_text("buyer-1", "测试消息")
            state = json.loads(
                urllib.request.urlopen(mock.base_url + "/_mock/state", timeout=5).read()
            )
            assert state["sent"][0]["content"] == "测试消息"
            assert state["token_requests"] >= 0

            async with connect(mock.ws_url + "?access_token=mock-token") as ws:
                await asyncio.sleep(0.2)
                await asyncio.to_thread(
                    _http_post, mock.base_url + "/_mock/push", {"content": "你好", "uid": "buyer-9"}
                )
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
                payload = json.loads(raw)
                assert payload["response"] == "push"
                assert payload["message"]["content"] == "你好"
                assert payload["message"]["from"]["uid"] == "buyer-9"

                await asyncio.to_thread(
                    _http_post, mock.base_url + "/_mock/human", {"uid": "buyer-9"}
                )
                raw2 = await asyncio.wait_for(ws.recv(), timeout=5)
                payload2 = json.loads(raw2)
                assert payload2["message"]["from"]["role"] == "mall_cs"
        finally:
            await mock.stop()

    asyncio.run(scenario())


def test_make_push_helpers():
    from mock.pdd_mock import make_push

    payload = make_push("测试", uid="u1", msg_id="m1")
    assert payload["response"] == "push"
    message = payload["message"]
    assert message["msg_id"] == "m1"
    assert message["from"] == {"role": "user", "uid": "u1"}
    assert message["to"]["role"] == "mall_cs"

    # 构造结构化消息（订单）
    order = make_push("", uid="u2", sub_type=1, extra={"order_sequence_no": "A1", "goods_name": "商品"})
    assert order["message"]["info"]["order_sequence_no"] == "A1"
