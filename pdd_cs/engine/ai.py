# -*- coding: utf-8 -*-
"""AI 回复生成（OpenAI 兼容接口）+ 提示词约束。

只依赖标准库（urllib），可接 DeepSeek / 通义千问 / 豆包 / OpenAI 等任何
OpenAI 兼容的 ``/chat/completions`` 接口。

提示词把平台规则写成硬约束（见 ``HARD_RULES``），并要求模型：
    - 不承诺发货时效 / 到达时间 / 退款赔付；
    - 不导流站外、不索要隐私；
    - 不确定就引导人工，不编造；
    - 使用知识库内容回答，短句、纯文本、适合拼多多聊天窗口。
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
from typing import Any, Iterable, Optional

from ..config import Config
from ..utils import truncate
from .knowledge import KnowledgeBase, KnowledgeSnippet

logger = logging.getLogger("pdd_cs.ai")

# 平台规则硬约束：这是"按最新平台规则回复"的核心提示词
HARD_RULES = """你是拼多多店铺的在线客服助手。必须遵守以下硬性规则，任何一条都不能违反：

【不能说的话】
1. 不得引导站外沟通：不发微信号/QQ/手机号/网址/二维码，不说“加微信”“私下聊”“去别的平台”。
2. 不得承诺具体发货时间或到达时间（如“今天一定发”“明天到”“保证三天到”）。
   只能回答：“发货时效以商品详情页公示为准”，并把具体单号/进度问题引导到订单页面查看或转人工。
3. 不得承诺退款/退货/赔偿结果，也不得说“包退”“肯定能退”“我帮你申请仅退款”“我说了算”。
   售后处理以平台页面和平台判定为准，店铺没有最终决定权。
4. 不得声称能影响平台判定、不得冒充平台官方、不得评价其他商家或平台。
5. 不得索要身份证、银行卡、验证码、密码等敏感信息。
6. 不辱骂、不反问客户、不阴阳怪气。

【要怎么说】
7. 先回应客户的具体问题，再给下一步动作（查订单页 / 提供订单号 / 转人工）。
8. 信息不足时，用一句话问清关键信息（如商品、订单号后四位、具体问题），不要一次问多个问题。
9. 知识库里没有依据的内容不要编造；拿不准就说“我帮您转人工确认一下”。
10. 回复用纯文本，不用 Markdown、不用项目符号，不超过 120 个汉字，语气自然、礼貌、口语化。
11. 客户要求人工、投诉、退款、售后、涉及人身安全或金额较大时，回复里说明“正在为您转接人工客服”，
    不要继续替客户做决定。
12. 你是智能客服，不要自称“人工客服”；如客户问你是不是机器人，如实说明是智能客服助手。

【输出格式】
只输出要发送给客户的一段话，不要输出解释、前缀、引号或思考过程。"""


class LLMError(RuntimeError):
    """模型调用失败。"""


class LLMClient:
    """OpenAI 兼容 Chat Completions 客户端。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        temperature: float = 0.6,
        max_tokens: int = 400,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.call_count = 0
        self.error_count = 0

    def chat(self, messages: list[dict[str, str]], *, temperature: Optional[float] = None) -> str:
        """调用模型，返回回复文本。"""
        if not self.api_key:
            raise LLMError("未配置 LLM api_key")
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }

        last_error: Optional[Exception] = None
        for attempt in range(2):
            request = urllib.request.Request(url, data=body, headers=headers, method="POST")
            try:
                started = time.monotonic()
                with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                elapsed = time.monotonic() - started
                self.call_count += 1
                data = json.loads(raw)
                content = _extract_content(data)
                if not content:
                    raise LLMError(f"模型返回空内容: {raw[:200]}")
                logger.info("LLM 回复生成成功（%.1fs，%d 字）", elapsed, len(content))
                return content
            except urllib.error.HTTPError as exc:
                self.error_count += 1
                detail = ""
                try:
                    detail = exc.read().decode("utf-8", errors="replace")[:300]
                except Exception:  # noqa: BLE001
                    pass
                last_error = LLMError(f"LLM HTTP {exc.code}: {detail}")
                if exc.code in (429, 500, 502, 503, 504) and attempt == 0:
                    time.sleep(1.5)
                    continue
                break
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                self.error_count += 1
                last_error = LLMError(f"LLM 调用失败: {exc}")
                if attempt == 0:
                    time.sleep(1.0)
                    continue
                break
        raise last_error or LLMError("LLM 调用失败（未知原因）")


def _extract_content(data: dict[str, Any]) -> str:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message") if isinstance(first.get("message"), dict) else {}
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):  # 部分兼容实现返回分段内容
        parts = [str(part.get("text", "")) for part in content if isinstance(part, dict)]
        return "".join(parts).strip()
    return ""


class AIReplyGenerator:
    """把上下文、知识库、历史对话组装成提示词并生成回复。"""

    def __init__(self, config: Config, knowledge: Optional[KnowledgeBase] = None):
        self.config = config
        self.knowledge = knowledge
        self.client: Optional[LLMClient] = None
        if config.llm.ready:
            self.client = LLMClient(
                base_url=config.llm.base_url,
                api_key=config.llm.api_key,
                model=config.llm.model,
                temperature=config.llm.temperature,
                max_tokens=config.llm.max_tokens,
                timeout=config.llm.timeout_seconds,
            )

    @property
    def ready(self) -> bool:
        return self.client is not None

    # ------------------------------------------------------------------
    def generate(
        self,
        *,
        question: str,
        history: Iterable[dict[str, Any]] = (),
        nickname: str = "",
        extra_context: str = "",
        handoff_hint: bool = False,
    ) -> Optional[str]:
        """生成一条回复；失败或未启用返回 None（由上层降级）。

        Args:
            question: 客户当前这句话（已归一化的文本）。
            history: 历史消息 [{"role": "user"/"assistant", "content": "..."}]。
            nickname: 客户昵称（可空）。
            extra_context: 商品/订单等结构化上下文文本。
            handoff_hint: 本次是否需要引导转人工（投诉/售后等）。
        """
        if not self.ready or self.client is None:
            return None
        if not (question or "").strip() and not extra_context:
            return None

        snippets: list[KnowledgeSnippet] = []
        if self.knowledge and self.config.knowledge.enabled:
            query = " ".join(filter(None, [question, extra_context]))
            snippets = self.knowledge.search(
                query,
                top_k=self.config.knowledge.top_k,
                min_score=self.config.knowledge.min_score,
            )
        system_prompt = self._build_system_prompt(snippets, handoff_hint=handoff_hint)
        messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]

        max_history = max(0, self.config.llm.max_history_messages)
        recent = list(history)[-max_history:] if max_history else []
        for item in recent:
            role = item.get("role")
            content = str(item.get("content") or "").strip()
            if role in {"user", "assistant"} and content:
                messages.append({"role": role, "content": truncate(content, 300)})

        user_parts = []
        if extra_context:
            user_parts.append(f"（当前会话信息：{extra_context}）")
        if nickname:
            user_parts.append(f"（客户昵称：{nickname}）")
        user_parts.append(question or "（客户发来一条非文本消息）")
        messages.append({"role": "user", "content": "\n".join(user_parts)})

        try:
            content = self.client.chat(messages)
        except LLMError as exc:
            logger.warning("AI 回复失败: %s", exc)
            return None
        return self._sanitize(content)

    # ------------------------------------------------------------------
    def _build_system_prompt(self, snippets: list[KnowledgeSnippet], *, handoff_hint: bool) -> str:
        persona = self.config.persona.strip()
        parts: list[str] = [HARD_RULES]
        if persona:
            parts.append("【店铺人设与话术风格】\n" + persona)
        if snippets:
            parts.append("【知识库（只使用与问题直接相关的内容，不要逐字照抄）】\n" + self._format_snippets(snippets))
        else:
            parts.append("【知识库】本次没有检索到直接相关的资料；不确定的信息不要编造，必要时引导人工。")
        if handoff_hint:
            parts.append("【本次情况】客户这条消息涉及售后/投诉/情绪/安全等敏感场景，回复中请说明“正在为您转接人工客服”，不要代替人工做承诺或决定。")
        parts.append(
            f"【店铺信息】店铺名称：{self.config.pdd.shop_hint or '本店'}；"
            f"当前时间：见系统时间；回复不超过 {self.config.behavior.max_reply_chars} 字。"
        )
        return "\n\n".join(parts)

    def _format_snippets(self, snippets: list[KnowledgeSnippet]) -> str:
        return "\n\n".join(
            s.to_prompt_block(self.config.knowledge.max_chars_per_snippet) for s in snippets
        )

    def _sanitize(self, text: str) -> str:
        """清洗模型输出：去 Markdown、去链接、限长。"""
        cleaned = (text or "").strip()
        cleaned = re.sub(r"```.*?```", "", cleaned, flags=re.S)
        cleaned = re.sub(r"[*_#>`]+", "", cleaned)
        cleaned = re.sub(r"https?://\S+", "", cleaned)
        cleaned = re.sub(r"^\s*(回复|回答|客服)[:：]\s*", "", cleaned)
        cleaned = re.sub(r"[ \t]+", " ", cleaned)
        cleaned = cleaned.strip(" \n\"'“”")
        return truncate(cleaned, self.config.behavior.max_reply_chars)


__all__ = ["AIReplyGenerator", "LLMClient", "LLMError", "HARD_RULES"]
