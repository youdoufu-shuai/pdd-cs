# -*- coding: utf-8 -*-
"""通用工具：时间、文本匹配、话术随机化、相似度。"""
from __future__ import annotations

import hashlib
import random
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

# 北京时间（拼多多商家后台的考核口径均为 UTC+8）
BEIJING_TZ = timezone(timedelta(hours=8))


def now_beijing() -> datetime:
    """当前北京时间（带时区）。"""
    return datetime.now(BEIJING_TZ)


def ts_to_beijing(ts: float | int | None) -> datetime:
    """秒 / 毫秒时间戳转北京时间；异常值回退到当前时间。"""
    if ts is None:
        return now_beijing()
    value = float(ts)
    if value > 10_000_000_000:  # 毫秒
        value /= 1000.0
    try:
        return datetime.fromtimestamp(value, BEIJING_TZ)
    except (OverflowError, OSError, ValueError):
        return now_beijing()


def iso(dt: datetime) -> str:
    """北京时间 ISO 字符串（秒级）。"""
    return dt.astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 关键词匹配（对齐 ChatGPT-On-CS 的关键词语义）
#   - "|"      多个候选写法，任一命中即可
#   - "[and]"  多个片段需按顺序全部出现
#   - match=exact / fuzzy / regex
# ---------------------------------------------------------------------------
def match_keyword(pattern: str, text: str, match: str = "fuzzy") -> bool:
    """判断单条模式是否命中文本。

    Args:
        pattern: 关键词模式。
        text: 待匹配文本。
        match: exact（完全相等）/ fuzzy（包含）/ regex（正则）。
    """
    if text is None:
        return False
    pattern = (pattern or "").strip()
    if not pattern:
        return False
    try:
        if match == "regex":
            return re.search(pattern, text) is not None
        if match == "exact":
            return text.strip() == pattern
        return pattern in text
    except re.error:
        return False


def range_match(pattern: str, text: str, match: str = "fuzzy") -> bool:
    """支持 "[and]" 的顺序复合匹配。

    Examples:
        >>> range_match("发货[and]几天", "大概几天能发货呀")
        False
        >>> range_match("发货[and]几天", "发货要几天")
        True
    """
    if not pattern:
        return False
    parts = pattern.split("[and]")
    if len(parts) == 1:
        return match_keyword(parts[0], text, match)
    cursor = 0
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if match == "regex":
            m = re.search(part, text[cursor:])
            if not m:
                return False
            cursor += m.end()
            continue
        idx = text.find(part, cursor)
        if idx < 0:
            return False
        cursor = idx + len(part)
    return True


def replace_keyword(pattern: str, text: str, replace: str, match: str = "fuzzy") -> str:
    """把命中的关键词替换为指定文本（支持 "[and]" 复合）。"""
    if not pattern or text is None:
        return text
    parts = [p.strip() for p in pattern.split("[and]") if p.strip()]
    if not parts:
        return text
    out = text
    for part in parts:
        try:
            if match == "regex":
                out = re.sub(part, replace, out)
            elif match == "exact":
                if out.strip() == part:
                    out = replace
            else:
                out = out.replace(part, replace)
        except re.error:
            continue
    return out


# ---------------------------------------------------------------------------
# 话术随机化
#   拼多多对"每次重复同一回答"有限制，需要让同一句话每次略有不同：
#   - "[or]"    在候选回复中随机选一条（ChatGPT-On-CS 同款）
#   - "[~]"     随机插入一个字符 / 表情（ChatGPT-On-CS 同款）
# ---------------------------------------------------------------------------
_RANDOM_TOKENS = list("abcdefghijkmnpqrstuvwxyz") + ["🌸", "😊", "🌷", "🌹", "💖", "🍀", "✨", "🌼"]


def split_variants(reply: str) -> list[str]:
    """按 "[or]" 拆分候选话术。"""
    if not reply:
        return []
    return [part.strip() for part in reply.split("[or]") if part.strip()]


def randomize_text(text: str, use_random_tokens: bool = True) -> str:
    """替换 "[~]" 占位符为随机字符；未包含占位符时原样返回。"""
    if not text:
        return text
    if not use_random_tokens:
        return text.replace("[~]", "")
    return re.sub(r"\[~\]", lambda _m: random.choice(_RANDOM_TOKENS), text)


def choose_reply(reply: str, use_random_tokens: bool = True) -> str:
    """从 "[or]" 候选中随机选一条，并做 "[~]" 随机化。"""
    variants = split_variants(reply)
    if not variants:
        return ""
    return randomize_text(random.choice(variants), use_random_tokens=use_random_tokens)


# ---------------------------------------------------------------------------
# 相似度：用于重复话术检测
# ---------------------------------------------------------------------------
def _bigrams(text: str) -> set[str]:
    clean = re.sub(r"\s+", "", text or "")
    if len(clean) < 2:
        return {clean} if clean else set()
    return {clean[i : i + 2] for i in range(len(clean) - 1)}


def similarity(a: str, b: str) -> float:
    """字符二元组 Jaccard 相似度（0~1），对中文短句足够稳。"""
    ba, bb = _bigrams(a), _bigrams(b)
    if not ba or not bb:
        return 0.0
    inter = len(ba & bb)
    union = len(ba | bb)
    return inter / union if union else 0.0


def max_similarity(text: str, history: Iterable[str]) -> float:
    """与历史话术的最大相似度。"""
    return max((similarity(text, h) for h in history), default=0.0)


_calls: dict[str, float] = {}


def throttle_sleep(key: str, min_interval: float) -> float:
    """进程内简单节流：保证同一 key 两次调用至少间隔 min_interval 秒。

    返回实际需要等待的秒数（同步等待由调用方决定是否 asyncio 处理）。
    """
    if min_interval <= 0:
        return 0.0
    now = time.monotonic()
    last = _calls.get(key, 0.0)
    wait = min_interval - (now - last)
    _calls[key] = now
    return max(0.0, wait)


# ---------------------------------------------------------------------------
# 文本清洗 / 抽取
# ---------------------------------------------------------------------------
_RE_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_RE_URL = re.compile(r"(https?://\S+|www\.\S+|[a-zA-Z0-9-]+\.(?:com|cn|net|top|xyz|vip|shop)\b)")
_RE_WECHAT = re.compile(r"(微信|weixin|wechat|vx|v信|薇信|威信|加v|加V|加微|QQ|qq|扣扣|企微|企业微信)")
_RE_QQ_NUMBER = re.compile(r"(?<!\d)[1-9]\d{4,10}(?!\d)")


def contains_phone(text: str) -> bool:
    return bool(text and _RE_PHONE.search(text))


def contains_url(text: str) -> bool:
    return bool(text and _RE_URL.search(text))


def contains_contact(text: str) -> bool:
    """是否包含手机号 / 网址 / 微信 QQ 等站外联系方式。"""
    if not text:
        return False
    return bool(
        _RE_PHONE.search(text)
        or _RE_URL.search(text)
        or _RE_WECHAT.search(text)
        or _RE_QQ_NUMBER.search(text)
    )


def extract_phones(text: str) -> list[str]:
    return _RE_PHONE.findall(text or "")


def normalize_text(text: str) -> str:
    """全角转半角 + 去多余空白，用于更稳的关键词匹配。"""
    if not text:
        return ""
    out = []
    for ch in text:
        code = ord(ch)
        if code == 0x3000:
            code = 32
        elif 0xFF01 <= code <= 0xFF5E:
            code -= 0xFEE0
        out.append(chr(code))
    return re.sub(r"[ \t\r\n]+", " ", "".join(out)).strip()


def truncate(text: str, limit: int) -> str:
    """按字符截断（拼多多客服单条消息不宜过长）。"""
    if not text or limit <= 0:
        return text or ""
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def digest(text: str) -> str:
    """短哈希（消息去重 / 话术指纹）。"""
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:16]


def mask_secret(value: str, keep: int = 4) -> str:
    """日志脱敏：仅保留头尾。"""
    if not value:
        return ""
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}***{value[-keep:]}"


def parse_hhmm(value: str) -> tuple[int, int]:
    """解析 "HH:MM" / "HH:MM:SS" 为 (时, 分)。"""
    parts = (value or "").strip().split(":")
    if len(parts) < 2:
        raise ValueError(f"时间格式应为 HH:MM，收到: {value!r}")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"非法时间: {value!r}")
    return hour, minute


def in_business_hours(now: datetime, start: str, end: str) -> bool:
    """判断当前是否在营业时间内（支持跨零点，如 22:00~06:00）。"""
    sh, sm = parse_hhmm(start)
    eh, em = parse_hhmm(end)
    start_min, end_min = sh * 60 + sm, eh * 60 + em
    cur = now.hour * 60 + now.minute
    if start_min == end_min:
        return True
    if start_min < end_min:
        return start_min <= cur < end_min
    return cur >= start_min or cur < end_min


def safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def jitter(base: float, jitter_seconds: float) -> float:
    """在 [base, base+jitter] 内取随机延迟。"""
    return max(0.0, base + random.uniform(0, max(0.0, jitter_seconds))) if jitter_seconds > 0 else max(0.0, base)


def parse_uid(value) -> Optional[str]:
    """统一 uid 为字符串；缺失返回 None。"""
    if value is None or value == "":
        return None
    return str(value)
