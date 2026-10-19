# -*- coding: utf-8 -*-
"""语音消息转写。

默认使用本地 Vosk 中文小模型，不需要外部 API：
    pdd_cs/data/asr/vosk-model-small-cn-0.22
也支持 OpenAI 兼容的 /audio/transcriptions（有 key 时）。

语音转写不可能 100% 准确，尤其是有口音、背景噪音、语音很短时；
调用方必须保留“没听清就请客户打字/确认”的兜底，不能假装听懂。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import urllib.request
import uuid
import wave
from pathlib import Path
from typing import Any, Optional

from .config import SpeechConfig

logger = logging.getLogger("pdd_cs.speech")

_VOSK_MODEL: Any = None


class SpeechError(RuntimeError):
    pass


class SpeechUnavailable(SpeechError):
    pass


def _download(url: str, target: Path, *, cookies: dict[str, str], user_agent: str, timeout: float) -> None:
    if not url:
        raise SpeechUnavailable("语音消息没有音频地址")
    cookie_text = "; ".join(f"{k}={v}" for k, v in (cookies or {}).items())
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent or "Mozilla/5.0",
            "Referer": "https://mms.pinduoduo.com/chat-merchant/index.html",
            **({"Cookie": cookie_text} if cookie_text else {}),
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        target.write_bytes(response.read())


def _to_wav(source: Path, target: Path, timeout: float) -> None:
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(source), "-ar", "16000", "-ac", "1", "-f", "wav", str(target),
    ]
    try:
        subprocess.run(cmd, check=True, timeout=timeout, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except FileNotFoundError as exc:
        raise SpeechUnavailable("服务器未安装 ffmpeg，无法转换语音格式") from exc
    except subprocess.CalledProcessError as exc:
        raise SpeechError(f"语音格式转换失败：{exc.stderr.decode('utf-8', 'ignore')[:200]}") from exc


def _vosk_transcribe(wav_path: Path, model_dir: Path) -> str:
    global _VOSK_MODEL
    try:
        from vosk import KaldiRecognizer, Model, SetLogLevel
    except ImportError as exc:
        raise SpeechUnavailable("服务器未安装 Vosk，语音转写不可用") from exc
    if not model_dir.exists():
        raise SpeechUnavailable(f"Vosk 中文模型不存在：{model_dir}")
    SetLogLevel(-1)
    if _VOSK_MODEL is None:
        _VOSK_MODEL = Model(str(model_dir))
    with wave.open(str(wav_path), "rb") as wf:
        if wf.getframerate() != 16000:
            raise SpeechError("语音重采样失败")
        recognizer = KaldiRecognizer(_VOSK_MODEL, wf.getframerate())
        recognizer.SetWords(False)
        while True:
            data = wf.readframes(4000)
            if not data:
                break
            recognizer.AcceptWaveform(data)
        result = json.loads(recognizer.FinalResult() or "{}")
    return str(result.get("text") or "").strip()


def _openai_transcribe(
    wav_path: Path, config: SpeechConfig
) -> str:
    if not config.openai_api_key:
        raise SpeechUnavailable("未配置语音转写 API Key")
    boundary = "----pddcs" + uuid.uuid4().hex
    parts: list[bytes] = []

    def add(name: str, value: str) -> None:
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
        )

    add("model", config.openai_model)
    add("language", config.language)
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"voice.wav\"\r\n"
        f"Content-Type: audio/wav\r\n\r\n".encode()
        + wav_path.read_bytes()
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    request = urllib.request.Request(
        f"{config.openai_base_url}/audio/transcriptions",
        data=b"".join(parts),
        headers={
            "Authorization": f"Bearer {config.openai_api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=config.timeout_seconds) as response:
        payload = json.loads(response.read().decode("utf-8") or "{}")
    return str(payload.get("text") or "").strip()


class VoiceTranscriber:
    def __init__(self, config: SpeechConfig):
        self.config = config

    @property
    def available(self) -> bool:
        if not self.config.enabled:
            return False
        if self.config.provider == "vosk":
            return self.config.model_dir.exists()
        if self.config.provider == "openai":
            return bool(self.config.openai_api_key)
        return False

    def transcribe(self, url: str, *, cookies: dict[str, str], user_agent: str = "") -> Optional[str]:
        if not self.available:
            raise SpeechUnavailable("语音转写未启用或模型不可用")
        with tempfile.TemporaryDirectory(prefix="pdd-voice-") as temp_dir:
            source = Path(temp_dir) / "voice.bin"
            wav_path = Path(temp_dir) / "voice.wav"
            _download(
                url,
                source,
                cookies=cookies,
                user_agent=user_agent,
                timeout=self.config.timeout_seconds,
            )
            _to_wav(source, wav_path, timeout=self.config.timeout_seconds)
            if self.config.provider == "vosk":
                text = _vosk_transcribe(wav_path, self.config.model_dir)
            elif self.config.provider == "openai":
                text = _openai_transcribe(wav_path, self.config)
            else:
                raise SpeechUnavailable(f"不支持的语音识别服务：{self.config.provider}")
            return text.strip() or None


__all__ = ["VoiceTranscriber", "SpeechError", "SpeechUnavailable"]
