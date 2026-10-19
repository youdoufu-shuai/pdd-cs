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
from ..utils import in_business_hours, now_beijing, truncate
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
        include_catalog = bool(
            re.search(
                r"商品|产品|哪款|推荐|链接|价格|多少钱|尺码|颜色|库存|有货|背心|保暖|马甲|款式|规格",
                question or "",
            )
        )
        system_prompt = self._build_system_prompt(
            snippets, handoff_hint=handoff_hint, include_catalog=include_catalog
        )
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
        cleaned = self._sanitize(content)
        cleaned = self._ensure_product_link(cleaned, question=question, history=recent)
        return self._append_human_hours_notice(cleaned)

    # ------------------------------------------------------------------
    def _build_system_prompt(
        self,
        snippets: list[KnowledgeSnippet],
        *,
        handoff_hint: bool,
        include_catalog: bool = False,
    ) -> str:
        persona = self.config.persona.strip()
        parts: list[str] = [HARD_RULES]
        if persona:
            parts.append("【店铺人设与话术风格】\n" + persona)
        if include_catalog:
            catalog = self._product_catalog()
            if catalog:
                parts.append(
                    "【店铺商品目录（客户问商品/推荐/价格/规格时必须使用这里的信息）】\n"
                    "规则：\n"
                    "1. 只推荐目录里真实存在的商品，不要编造商品名、价格、库存或链接。\n"
                    "2. 客户问某个商品时，先给 1 款最匹配的：商品名 + 价格口径 + 1-2 个卖点 + 商品链接。\n"
                    "3. 商品链接必须原样使用目录里的链接，不要改写；不要给站外链接。\n"
                    "4. 一次只推 1 款，客户明确要更多再列第 2 款。\n\n"
                    + catalog
                )
        if snippets:
            parts.append("【知识库（只使用与问题直接相关的内容，不要逐字照抄）】\n" + self._format_snippets(snippets))
        else:
            parts.append("【知识库】本次没有检索到直接相关的资料；不确定的信息不要编造，必要时引导人工。")
        human = self.config.human_hours
        if human.enabled:
            active = self._human_hours_active()
            end_label = f"次日{human.end}" if human.end <= human.start else human.end
            parts.append(
                f"【人工客服在线时间】{human.start}-{end_label}；"
                f"当前{'人工在线' if active else '非人工在线时段'}。"
            )
            if not active:
                parts.append(
                    "【非人工在线说明】客户要求人工、售后、投诉时，必须先说明："
                    + human.notice
                    + " 不要把“正在转人工”说成马上有人接待。"
                )
        parts.append(
            "【语音/口音容错】客户可能使用语音输入、方言或口音，转写文本可能有同音错字。"
            "请结合商品、尺码、订单上下文理解；不确定时只问一个澄清问题，不要猜错后乱承诺。"
        )
        if handoff_hint:
            parts.append("【本次情况】客户这条消息涉及售后/投诉/情绪/安全等敏感场景，回复中请说明“正在为您转接人工客服”，不要代替人工做承诺或决定。")
        parts.append(
            f"【店铺信息】店铺名称：{self.config.pdd.shop_hint or '本店'}；"
            f"当前时间：见系统时间；回复不超过 {self.config.behavior.max_reply_chars} 字。"
        )
        return "\n\n".join(parts)

    def _human_hours_active(self) -> bool:
        human = self.config.human_hours
        if not human.enabled:
            return False
        return in_business_hours(now_beijing(), human.start, human.end)

    def _append_human_hours_notice(self, content: str) -> str:
        """非人工在线时段，在 AI 回复末尾补一句明确说明。"""
        human = self.config.human_hours
        if not human.enabled or self._human_hours_active() or not human.notice:
            return content
        notice = human.notice.strip()
        text = (content or "").strip()
        if not text:
            return notice
        if notice in text:
            return text
        limit = max(0, int(self.config.behavior.max_reply_chars))
        # 商品链接必须完整保留，否则会破坏商品卡片识别；说明放在链接前
        link_match = re.search(
            r"https?://(?:(?:mobile\.)?yangkeduo\.com|(?:mobile\.)?pinduoduo\.com)/"
            r"goods(?:\.html|2\.html)?\?goods_id=\d+",
            text,
        )
        if link_match:
            before = text[: link_match.start()].rstrip()
            link = text[link_match.start():].strip()
            room = limit - len(link) - len(notice) - 2 if limit else len(before)
            before = truncate(before, max(0, room)) if room > 0 else ""
            return f"{before}\n{notice}\n{link}".strip()
        if limit and len(text) + len(notice) + 1 > limit:
            room = max(0, limit - len(notice) - 1)
            text = truncate(text, room) if room else ""
        return f"{text}\n{notice}".strip()

    def _format_snippets(self, snippets: list[KnowledgeSnippet]) -> str:
        return "\n\n".join(
            s.to_prompt_block(self.config.knowledge.max_chars_per_snippet) for s in snippets
        )

    def _product_catalog(self) -> str:
        """读取商品目录；目录不存在时返回空，不编造商品。"""
        path = self.config.knowledge.dir / "product-catalog.md"
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
        return truncate(text, 6000)

    def _product_entries(self) -> list[dict[str, str]]:
        """从商品目录 Markdown 解析商品名和商品地址。"""
        catalog = self._product_catalog()
        if not catalog:
            return []
        entries: list[dict[str, str]] = []
        for block in re.split(r"\n##\s*商品[：:]", catalog)[1:]:
            lines = [line.strip() for line in block.splitlines() if line.strip()]
            if not lines:
                continue
            name = lines[0].strip()
            if not name:
                continue
            link = ""
            for line in lines[1:]:
                matched = re.search(r"https?://\S+goods_id=\d+", line)
                if matched:
                    link = matched.group(0)
                    break
            if link:
                entries.append({"name": name, "link": link})
        return entries

    def _ensure_product_link(
        self, content: str, *, question: str, history: Iterable[dict[str, Any]]
    ) -> str:
        """AI 有时会忽略商品链接；在商品类回复末尾确定性地补上对应链接。"""
        text = (content or "").strip()
        if not text or re.search(r"https?://\S+goods_id=\d+", text):
            return text
        product_terms = ("背心", "马甲", "保暖", "羊羔绒", "抗菌", "贴片", "坎肩", "打底")
        trigger = any(term in text or term in (question or "") for term in product_terms)
        if not trigger:
            return text
        history_text = " ".join(
            str(item.get("content") or "") for item in list(history)[-4:] if isinstance(item, dict)
        )
        context = " ".join(filter(None, [question, text, history_text]))
        entries = self._product_entries()
        if not entries:
            return text
        best = None
        best_score = 0
        for entry in entries:
            score = sum(1 for term in product_terms if term in entry["name"] and term in context)
            # 商品名中的关键人群也参与匹配，避免男女款推错
            score += sum(1 for term in ("男士", "女士", "妈妈") if term in entry["name"] and term in context)
            if score > best_score:
                best, best_score = entry, score
        if not best:
            return text
        link = best["link"]
        limit = max(0, int(self.config.behavior.max_reply_chars))
        if limit:
            room = limit - len(link) - 1
            if room <= 0:
                return link
            if len(text) > room:
                text = truncate(text, room)
        return f"{text.rstrip()}\n{link}"

    def _sanitize(self, text: str) -> str:
        """清洗模型输出：去 Markdown、去站外链接，保留拼多多商品链接。"""
        cleaned = (text or "").strip()
        # 商品链接是平台内页面，不属于站外导流；先保护再清理 Markdown/其他 URL
        allowed = re.findall(
            r"https?://(?:(?:mobile\.)?yangkeduo\.com|(?:mobile\.)?pinduoduo\.com)/"
            r"goods(?:\.html|2\.html)?\?goods_id=\d+",
            cleaned,
        )
        for index, url in enumerate(allowed):
            cleaned = cleaned.replace(url, f"PDDLINKPLACEHOLDER{index}TOKEN")
        cleaned = re.sub(r"```.*?```", "", cleaned, flags=re.S)
        cleaned = re.sub(r"[*_#>`]+", "", cleaned)
        cleaned = re.sub(r"https?://\S+", "", cleaned)
        for index, url in enumerate(allowed):
            cleaned = cleaned.replace(f"PDDLINKPLACEHOLDER{index}TOKEN", url)
        cleaned = re.sub(r"^\s*(回复|回答|客服)[:：]\s*", "", cleaned)
        cleaned = re.sub(r"[ \t]+", " ", cleaned)
        cleaned = cleaned.strip(" \n\"'“”")
        return truncate(cleaned, self.config.behavior.max_reply_chars)


__all__ = ["AIReplyGenerator", "LLMClient", "LLMError", "HARD_RULES"]
