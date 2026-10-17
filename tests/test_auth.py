# -*- coding: utf-8 -*-
"""Cookie 解析 / 保存 / 校验测试。"""
from __future__ import annotations

import json

import pytest

from pdd_cs.config import PROJECT_ROOT
from pdd_cs.pdd.auth import (
    CookieBundle,
    CookieError,
    CookieStore,
    cookie_age_hours,
    cookies_to_header,
    parse_cookie_text,
    validate_cookies,
)


def test_parse_header_format():
    cookies = parse_cookie_text("PASS_ID=abc; MSA=def; anti_content=xyz")
    assert cookies["PASS_ID"] == "abc"
    assert cookies["MSA"] == "def"
    assert cookies["anti_content"] == "xyz"


def test_parse_json_format():
    cookies = parse_cookie_text(json.dumps({"PASS_ID": "abc", "SUB_SYS_ID": 123}))
    assert cookies["PASS_ID"] == "abc"
    assert cookies["SUB_SYS_ID"] == "123"


def test_parse_netscape_format():
    text = (
        "# Netscape HTTP Cookie File\n"
        ".pinduoduo.com\tTRUE\t/\tFALSE\t0\tPASS_ID\tabc\n"
        ".pinduoduo.com\tTRUE\t/\tFALSE\t0\tMSA\tdef\n"
    )
    cookies = parse_cookie_text(text)
    assert cookies["PASS_ID"] == "abc"
    assert cookies["MSA"] == "def"


def test_parse_empty_raises():
    with pytest.raises(CookieError):
        parse_cookie_text("   ")


def test_validate_cookies():
    good = {"PASS_ID": "x", "MSA": "y", "anti_content": "z", "api_uid": "1",
            "mms_b84d1838": "a", "njrpl": "b", "dilx": "c", "_nano_fp": "d"}
    assert validate_cookies(good) == []

    problems = validate_cookies({"foo": "bar"})
    assert any("未找到登录态 Cookie" in p for p in problems)

    partial = validate_cookies({"PASS_ID": "x"})
    assert any("缺少部分会话" in p for p in partial)


def test_cookies_to_header():
    assert cookies_to_header({"A": "1", "B": "2"}) == "A=1; B=2"


def test_cookie_store_roundtrip(tmp_path):
    store = CookieStore(tmp_path / "cookies.json")
    assert not store.exists()
    assert store.load() is None

    store.save_text("PASS_ID=abc; MSA=def; anti_content=xyz")
    assert store.exists()
    bundle = store.load()
    assert bundle is not None
    assert bundle.cookies["PASS_ID"] == "abc"
    assert "PASS_ID" in store.describe()

    # 再次保存覆盖
    store.save(CookieBundle(cookies={"PASS_ID": "new"}, shop_name="测试店铺"))
    assert store.load().cookies["PASS_ID"] == "new"
    assert "测试店铺" in store.load().summary()


def test_cookie_age_hours(tmp_path):
    store = CookieStore(tmp_path / "cookies.json")
    store.save_text("PASS_ID=abc; MSA=def")
    bundle = store.load()
    age = cookie_age_hours(bundle.saved_at)
    assert age is not None and 0 <= age < 1


def test_project_cookie_store_not_required():
    """默认 Cookie 路径可以不存在（首次使用前）。"""
    store = CookieStore(PROJECT_ROOT / "config" / "secrets" / "not-exists.json")
    assert store.load() is None
