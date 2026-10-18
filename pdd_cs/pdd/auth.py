# -*- coding: utf-8 -*-
"""拼多多登录凭据（Cookie）的解析、保存与校验。

拼多多商家后台使用 Cookie 维持登录态；`chats/getToken` 等接口都依赖它。
本模块支持三种常见来源：
1. 在浏览器 F12 → Network 里复制请求头 `Cookie:` 的整行文本；
2. 浏览器插件导出的 JSON 字典；
3. Netscape 格式（curl / EditThisCookie 导出）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from ..utils import mask_secret, now_beijing

#: 只要出现其中任意一个，基本可以认定是拼多多商家后台的登录态
_KEY_COOKIE_NAMES = (
    "PASS_ID",
    "SUB_PASS_ID",
    "MSA",
    "SUB_SYS_ID",
    "pdd_user_id",
    "pdd_user_uin",
)

#: 建议同时存在的风控 / 会话 Cookie（缺失不阻断，但部分接口可能被风控）
_RECOMMENDED = ("anti_content", "api_uid", "mms_b84d1838", "njrpl", "dilx", "_nano_fp")


class CookieError(ValueError):
    """Cookie 解析或校验失败。"""


def extract_cookie_header(text: str) -> str:
    """从 cURL / 浏览器请求头文本中提取 Cookie 值。

    支持 ``-H 'Cookie: a=b; c=d'``、``--header "cookie: ..."`` 以及
    浏览器 Network 面板复制的 ``Cookie: a=b; c=d`` 单行。
    """
    raw = (text or "").strip()
    if not raw:
        return raw
    # cURL / 浏览器请求头中直接找 Cookie: 后面那一行；同时兼容 -b / --cookie 参数
    match = re.search(r"(?is)\bcookie:\s*([^'\"\r\n]+)", raw)
    if match:
        candidate = match.group(1).strip()
        if "=" in candidate:
            return candidate
    match = re.search(r"(?is)(?:-b|--cookie)\s+['\"]?([^'\"\r\n]+)", raw)
    if match:
        candidate = match.group(1).strip()
        if "=" in candidate:
            return candidate
    return raw


def parse_cookie_text(text: str) -> dict[str, str]:
    """解析多种格式的 Cookie 文本为字典。

    支持：
        - JSON：``{"PASS_ID": "...", "MSA": "..."}``
        - 请求头：``PASS_ID=xxx; MSA=yyy``
        - 请求头行：``Cookie: PASS_ID=xxx; MSA=yyy``
        - cURL：``curl ... -H 'Cookie: PASS_ID=xxx; MSA=yyy'``
        - Netscape：每行 ``domain \\t flag \\t path \\t secure \\t expiry \\t name \\t value``
    """
    if not text or not text.strip():
        raise CookieError("Cookie 为空")

    raw = extract_cookie_header(text).strip()

    # 1) JSON 字典
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CookieError(f"Cookie JSON 解析失败: {exc}") from exc
        result = {
            str(k): str(v)
            for k, v in data.items()
            if isinstance(v, (str, int, float)) and str(k)
        }
        if not result:
            raise CookieError("Cookie JSON 中没有可用的键值")
        return result

    # 2) Netscape 格式（含制表符 / 7 列）
    if "\t" in raw and len(raw.splitlines()) >= 1:
        result: dict[str, str] = {}
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            cols = line.split("\t")
            if len(cols) >= 7:
                result[cols[5].strip()] = cols[6].strip()
            elif len(cols) == 6:  # 有些导出工具省略 domain
                result[cols[4].strip()] = cols[5].strip()
        if result:
            return result

    # 3) 请求头 "k=v; k=v"
    result = {}
    for chunk in re.split(r";\s*", raw):
        if "=" not in chunk:
            continue
        key, value = chunk.split("=", 1)
        key, value = key.strip(), value.strip().strip('"')
        if key:
            result[key] = value
    if not result:
        raise CookieError("无法识别的 Cookie 格式（可粘贴 F12 请求头里的 Cookie 整行）")
    return result


def cookies_to_header(cookies: dict[str, str]) -> str:
    """字典转 Cookie 请求头。"""
    return "; ".join(f"{k}={v}" for k, v in cookies.items() if v is not None)


def validate_cookies(cookies: dict[str, str]) -> list[str]:
    """校验 Cookie 完整性，返回问题列表（空表示通过）。"""
    problems: list[str] = []
    if not cookies:
        problems.append("Cookie 为空")
        return problems
    if not any(name in cookies for name in _KEY_COOKIE_NAMES):
        problems.append(
            "未找到登录态 Cookie（PASS_ID / SUB_PASS_ID / MSA 等），"
            "请确认是在 mms.pinduoduo.com 已登录状态下复制的 Cookie"
        )
    missing = [name for name in _RECOMMENDED if name not in cookies]
    if missing:
        problems.append(
            f"缺少部分会话/风控 Cookie（{', '.join(missing)}），"
            "可能导致部分接口被风控；建议在客服工作台页面刷新后重新复制整行 Cookie"
        )
    return problems


@dataclass
class CookieBundle:
    """一份持久化的登录态。"""

    cookies: dict[str, str]
    saved_at: str = ""
    shop_name: str = ""
    user_name: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "cookies": self.cookies,
            "saved_at": self.saved_at or now_beijing().strftime("%Y-%m-%d %H:%M:%S"),
            "shop_name": self.shop_name,
            "user_name": self.user_name,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "CookieBundle":
        cookies = data.get("cookies") or {}
        if not isinstance(cookies, dict):
            raise CookieError("Cookie 文件中 cookies 字段格式错误")
        return cls(
            cookies={str(k): str(v) for k, v in cookies.items()},
            saved_at=str(data.get("saved_at", "")),
            shop_name=str(data.get("shop_name", "")),
            user_name=str(data.get("user_name", "")),
        )

    def summary(self) -> str:
        keys = [k for k in _KEY_COOKIE_NAMES if k in self.cookies]
        return (
            f"保存时间={self.saved_at or '未知'} 店铺={self.shop_name or '未知'} "
            f"登录态={','.join(keys) or '缺失'} 共{len(self.cookies)}项"
        )


class CookieStore:
    """Cookie 文件读写（默认 config/secrets/cookies.json，权限尽量收窄）。"""

    def __init__(self, path: Path):
        self.path = Path(path)

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> Optional[CookieBundle]:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CookieError(f"读取 Cookie 文件失败: {exc}") from exc
        return CookieBundle.from_json(data)

    def save(self, bundle: CookieBundle) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(bundle.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)
        try:  # Windows 下尽量设置为仅本人可读
            import os

            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def save_text(
        self, cookie_text: str, shop_name: str = "", user_name: str = ""
    ) -> CookieBundle:
        """解析文本并保存；返回可用的 CookieBundle。"""
        cookies = parse_cookie_text(cookie_text)
        problems = validate_cookies(cookies)
        fatal = [p for p in problems if "未找到登录态 Cookie" in p or "为空" in p]
        if fatal:
            raise CookieError("；".join(fatal))
        bundle = CookieBundle(
            cookies=cookies,
            saved_at=now_beijing().strftime("%Y-%m-%d %H:%M:%S"),
            shop_name=shop_name,
            user_name=user_name,
        )
        self.save(bundle)
        return bundle

    def describe(self) -> str:
        try:
            bundle = self.load()
        except CookieError as exc:
            return f"Cookie 文件异常: {exc}"
        if not bundle:
            return "尚未保存 Cookie（请先运行：python -m pdd_cs login）"
        return bundle.summary()


def cookie_age_hours(saved_at: str) -> Optional[float]:
    """Cookie 保存了多久（小时）；无法解析返回 None。"""
    try:
        dt = datetime.strptime(saved_at, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=now_beijing().tzinfo
        )
    except (ValueError, TypeError):
        return None
    return (now_beijing() - dt).total_seconds() / 3600.0


__all__ = [
    "CookieError",
    "CookieBundle",
    "CookieStore",
    "parse_cookie_text",
    "cookies_to_header",
    "validate_cookies",
    "cookie_age_hours",
    "mask_secret",
]
