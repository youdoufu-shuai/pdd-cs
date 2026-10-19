# -*- coding: utf-8 -*-
"""语音消息解析与转写兜底测试。"""
from __future__ import annotations

from pdd_cs.pdd.message import parse_message


def test_voice_message_extracts_audio_url():
    payload = {
        "response": "push",
        "message": {
            "type": 2,
            "content": "https://example.com/voice.amr",
            "msg_id": "voice-1",
            "from": {"role": "user", "uid": 123},
            "to": {"role": "mall_cs", "uid": 456},
        },
    }
    event = parse_message(payload)
    assert event is not None
    assert event.context_type == "voice"
    assert event.audio_url.endswith("voice.amr")
    assert not event.content


def test_voice_with_inline_transcript_is_text():
    payload = {
        "response": "push",
        "message": {
            "type": 2,
            "content": '{"voice_text": "你好，我想问一下尺码"}',
            "from": {"role": "user", "uid": 123},
            "to": {"role": "mall_cs", "uid": 456},
        },
    }
    event = parse_message(payload)
    assert event is not None
    assert event.context_type == "text"
    assert "尺码" in event.content
