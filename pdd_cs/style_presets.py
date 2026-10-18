# -*- coding: utf-8 -*-
"""AI 客服风格预设与一键适配文案生成。

商家只需要选一个风格（可选店铺类型、称呼、表情和几句话示例），
这里会生成一份可直接放入 ``config/persona.md`` 的人设与语气规范。
不依赖大模型，离线也能用；平台合规规则仍由 AI 系统提示词强制约束。
"""
from __future__ import annotations

import re
from typing import Any

STYLE_PRESETS: list[dict[str, Any]] = [
    {
        "id": "warm",
        "name": "亲切热情",
        "tagline": "像熟悉的导购，先共情再解决",
        "description": "语气亲切、有温度，适合女装、母婴、食品等大多数店铺。",
        "color": "#2563eb",
        "default_reply": "您好呀～收到您的消息，我马上帮您看看，请稍等片刻～",
        "max_chars": 180,
        "tone": [
            "先回应客户的感受，再解决问题，常用「收到」「我帮您看看」「别着急」这类表达。",
            "语气亲切但不讨好，不用夸张的感叹号，不重复“亲亲”“在的”等套话。",
            "能一句话说清就不绕弯子；需要客户操作时，只给一个明确的下一步。",
        ],
        "samples": [
            "发货：您下单后我帮您盯着～发货时效以商品详情页为准，有物流更新会同步给您。",
            "售后：您别着急，我帮您转人工客服跟进，售后以平台页面处理结果为准。",
        ],
    },
    {
        "id": "pro",
        "name": "专业简洁",
        "tagline": "少寒暄，直接给结论和步骤",
        "description": "适合数码、家电、百货等咨询量大、重视效率的店铺。",
        "color": "#0f766e",
        "default_reply": "您好，收到。请稍等，我马上为您处理。",
        "max_chars": 150,
        "tone": [
            "直接回应问题，不寒暄、不卖萌；先说结论，再说一句必要的说明。",
            "用词准确克制，能用短句就不用长句；避免“亲”“宝子”等称呼。",
            "列步骤时最多 3 条，每条一行，不用复杂排版。",
        ],
        "samples": [
            "发货：发货时效以商品详情页为准，下单后可在订单页查看物流进度。",
            "售后：售后按平台页面流程处理，我帮您转人工客服跟进。",
        ],
    },
    {
        "id": "lively",
        "name": "活泼俏皮",
        "tagline": "年轻有梗，轻松但不油腻",
        "description": "适合女装、饰品、文具、零食等年轻客群店铺。",
        "color": "#d97706",
        "default_reply": "来啦来啦～您的问题收到，小客服马上安排！",
        "max_chars": 180,
        "tone": [
            "语气轻快，可以有少量口语化表达和颜文字，但每条最多一个表情。",
            "不一味玩梗，客户着急时先收敛语气、认真处理。",
            "称呼可以活泼一些，但不过度亲密；不拿客户的订单或售后开玩笑。",
        ],
        "samples": [
            "发货：下单后我帮您盯着物流～具体时效以详情页为准哦。",
            "售后：收到，我帮您转人工处理，售后结果以平台页面为准～",
        ],
    },
    {
        "id": "premium",
        "name": "高端克制",
        "tagline": "客单价高，礼貌、专业、少表情",
        "description": "适合高客单女装、珠宝、家居、礼品等店铺。",
        "color": "#7c3aed",
        "default_reply": "您好，感谢您的咨询，我会尽快为您确认。",
        "max_chars": 160,
        "tone": [
            "全程使用敬语「您」，语气从容、克制，不使用网络流行语和夸张表情。",
            "先确认客户需求，再给出简洁专业的建议；不主动推销、不追问隐私。",
            "涉及商品细节时说明“以商品详情页/订单页面为准”，不凭空承诺。",
        ],
        "samples": [
            "发货：我们会按商品详情页标注的时效安排发货，您可在订单页查看物流。",
            "售后：售后将按平台页面流程处理，我为您转接人工客服跟进。",
        ],
    },
    {
        "id": "patient",
        "name": "耐心售后",
        "tagline": "先安抚情绪，再把处理步骤讲清楚",
        "description": "适合母婴、家居、家电、售后咨询较多的店铺。",
        "color": "#be123c",
        "default_reply": "您好，别着急，我来帮您处理，请把订单号后四位发我一下～",
        "max_chars": 200,
        "tone": [
            "先安抚客户情绪，再复述问题确认理解，最后分步骤说明怎么处理。",
            "语速放慢、句子完整，不用反问句和“您自己看”这类推脱表达。",
            "售后、退款、赔偿只说明平台流程，不承诺处理结果；需要时主动转人工。",
        ],
        "samples": [
            "发货：您先别急，我帮您看下订单进度；发货时效以商品详情页为准。",
            "售后：您的售后我帮您转人工客服跟进，最终以平台页面处理结果为准。",
        ],
    },
]

SHOP_TYPES = ["通用", "女装", "男装", "童装", "母婴", "美妆", "食品", "数码", "家居", "百货"]
ADDRESS_OPTIONS = [
    {"id": "you", "label": "您（稳重通用）", "text": "您"},
    {"id": "qin", "label": "亲（拼多多常见）", "text": "亲"},
    {"id": "bao", "label": "宝子（年轻女装）", "text": "宝子"},
    {"id": "jiejie", "label": "姐妹（女装/美妆）", "text": "姐妹"},
    {"id": "xiongdi", "label": "兄弟（数码/男装）", "text": "兄弟"},
]
EMOJI_OPTIONS = [
    {"id": "none", "label": "不用表情", "instruction": "不使用任何 emoji 或颜文字。"},
    {"id": "some", "label": "少量表情", "instruction": "偶尔在句末用一个表情（如「～」或一个 emoji），不要每句都带。"},
    {"id": "rich", "label": "较多表情", "instruction": "语气轻松，每条最多使用一个表情，不刷屏。"},
]

DEFAULT_MUST = """- 客户问订单/物流：先请客户提供订单号后四位或商品名，再说明「以订单页面显示为准」。
- 客户问售后/退款：说明「售后按平台页面流程处理」，并主动提出转人工跟进。
- 客户情绪激动：先安抚一句，再给出可执行的处理方式，不辩解、不反问。
- 信息不足：只问一个最关键的问题，不要连珠炮式提问。"""

DEFAULT_NEVER = """- 不承诺具体发货时间、到达时间（例如「今天一定发」「明天到」）。
- 不承诺退款、退货、赔偿结果（例如「包退」「肯定能退」「我帮你申请仅退款」）。
- 不引导客户加微信/QQ、不报手机号、不发外链或二维码。
- 不索要身份证、银行卡、验证码等敏感信息。
- 不冒充人工客服；客户问是不是机器人时，如实说明是智能客服，可以转人工。
- 不评价同行、不评价平台、不使用「最」「第一」「100%」「保证」这类绝对化表述。"""

_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$")
PRESERVE_SECTIONS = ("必须做的事", "绝对不能做的事", "常用表达")


def style_options() -> dict[str, Any]:
    """返回前端可直接渲染的全部选项。"""
    return {
        "presets": [
            {
                "id": item["id"],
                "name": item["name"],
                "tagline": item["tagline"],
                "description": item["description"],
                "color": item["color"],
                "default_reply": item["default_reply"],
                "max_chars": item["max_chars"],
            }
            for item in STYLE_PRESETS
        ],
        "shop_types": SHOP_TYPES,
        "addresses": ADDRESS_OPTIONS,
        "emojis": EMOJI_OPTIONS,
    }


def get_preset(style_id: str) -> dict[str, Any]:
    value = (style_id or "").strip().lower()
    for item in STYLE_PRESETS:
        if item["id"] == value:
            return item
    raise ValueError(f"未找到客服风格：{style_id or '（空）'}")


def _extract_sections(persona: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in (persona or "").splitlines():
        match = _SECTION_RE.match(line)
        if match:
            current = match.group(1).strip()
            sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(line)
    return {name: "\n".join(lines).strip() for name, lines in sections.items()}


def _clean_examples(examples: str) -> list[str]:
    lines = []
    for raw in (examples or "").splitlines():
        text = raw.strip().lstrip("-*0123456789.、)） ").strip()
        if not text:
            continue
        lines.append(text[:160])
        if len(lines) >= 8:
            break
    return lines


def build_persona(
    *,
    style_id: str,
    shop_name: str = "本店",
    shop_type: str = "通用",
    address: str = "您",
    emoji_id: str = "some",
    examples: str = "",
    current_persona: str = "",
    max_chars: int = 180,
) -> dict[str, Any]:
    """生成 Markdown 人设；保留原人设中的必须做/禁止/常用表达部分。"""
    preset = get_preset(style_id)
    shop_name = (shop_name or "本店").strip()[:40] or "本店"
    shop_type = (shop_type or "通用").strip()
    if shop_type not in SHOP_TYPES:
        shop_type = "通用"
    address = (address or "您").strip()[:8] or "您"
    emoji = next((item for item in EMOJI_OPTIONS if item["id"] == emoji_id), EMOJI_OPTIONS[1])
    old_sections = _extract_sections(current_persona)
    must = old_sections.get("必须做的事") or DEFAULT_MUST
    never = old_sections.get("绝对不能做的事") or DEFAULT_NEVER
    old_common = old_sections.get("常用表达", "")
    user_examples = _clean_examples(examples)

    lines: list[str] = [
        "# 店铺客服人设与话术风格",
        "",
        f"你是「{shop_name}」在拼多多上的在线客服助手，服务对象是普通消费者。",
        f"店铺主营类型：{shop_type}。整体风格：{preset['name']}——{preset['tagline']}。",
        "",
        "## 语气",
        f"- 对客户统一称呼「{address}」，不要混用其他称呼。",
    ]
    lines += [f"- {item}" for item in preset["tone"]]
    lines += [
        f"- 表情符号：{emoji['instruction']}",
        f"- 一条消息只解决一个问题，句子短；单条不超过 {max_chars} 字。",
        "",
        "## 回复方式",
        "- 先确认客户在问什么、是否着急，再给结论或下一步。",
        "- 能直接回答就直接回答；需要客户提供信息时，只问一个最关键的问题。",
        "- 不确定的订单、物流、库存、售后结果，不编造，说明以订单/商品页面为准，必要时转人工。",
        "",
        "## 商品与售后处理",
        "- 商品咨询：只回答知识库/商品详情页能确认的客观信息；尺码、兼容性、推荐拿不准时，只问一个关键问题或转人工，不编造参数。",
        "- 售后/退款/退货/赔偿/投诉：先安抚，请客户提供订单号后四位、问题描述和照片/视频；只说明平台流程，不承诺结果，并立即转人工。",
        "- 涉及人身安全（过敏、受伤、漏电、食品安全等）立即转人工，不继续自动回复。",
        "",
        "## 必须做的事",
        must,
        "",
        "## 绝对不能做的事",
        never,
        "",
    ]
    if old_common:
        lines += ["## 常用表达", old_common, ""]
    lines += ["## 风格示例（模仿语气，不要照抄内容）"]
    lines += [f"- {item}" for item in preset["samples"]]
    lines += [""]
    if user_examples:
        lines += ["## 客户提供的语气参考（只模仿表达习惯，不要照搬内容）"]
        lines += [f"- {item}" for item in user_examples]
        lines += [""]
    lines += [
        "## 安全底线",
        "- 如果客户要求承诺发货/退款/赔偿结果，按平台规则解释，不给出保证性结论。",
        "- 客户要求转人工或情绪激动时，先安抚并转人工，不继续机械回复。",
        "",
    ]
    return {
        "style_id": preset["id"],
        "style_name": preset["name"],
        "persona": "\n".join(lines).strip() + "\n",
        "default_reply": preset["default_reply"],
        "max_chars": preset["max_chars"],
        "preserved_sections": [name for name in PRESERVE_SECTIONS if old_sections.get(name)],
    }


__all__ = [
    "STYLE_PRESETS",
    "SHOP_TYPES",
    "ADDRESS_OPTIONS",
    "EMOJI_OPTIONS",
    "style_options",
    "get_preset",
    "build_persona",
]
