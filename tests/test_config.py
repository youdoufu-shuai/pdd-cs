# -*- coding: utf-8 -*-
"""配置与工具测试。"""
from __future__ import annotations

from pdd_cs.config import Config, PROJECT_ROOT
from pdd_cs.utils import (
    choose_reply,
    in_business_hours,
    match_keyword,
    normalize_text,
    range_match,
    similarity,
)


def test_config_loads_example():
    config = Config.load(PROJECT_ROOT / "config" / "config.example.yaml")
    assert config.app_name
    assert config.server.port == 38617
    assert config.compliance.sla.first_response_seconds == 180
    assert config.pdd.base_url.startswith("https://")


def test_config_validate_reports_llm_key(config):
    config.llm.enabled = True
    config.llm.api_key = ""
    problems = config.validate()
    assert any("api_key" in p for p in problems)


def test_config_validate_ok(config):
    config.llm.enabled = False
    assert config.validate() == []


def test_match_keyword_semantics():
    assert match_keyword("发货", "什么时候发货", "fuzzy")
    assert not match_keyword("发货", "什么时候发货", "exact")
    assert match_keyword("什么时候发货", "什么时候发货", "exact")
    assert match_keyword(r"\d{6}", "验证码123456", "regex")


def test_range_match_and():
    assert range_match("发货[and]几天", "发货要几天")
    assert not range_match("发货[and]几天", "几天发货")


def test_choose_reply_variants():
    text = choose_reply("回应A[or]回应B")
    assert text in {"回应A", "回应B"}
    randomized = choose_reply("你好[~]")
    assert len(randomized) == 3


def test_similarity():
    assert similarity("发货时效以详情页为准", "发货时效以详情页为准") == 1.0
    assert similarity("发货时效", "完全不同的内容") < 0.2


def test_normalize_text():
    assert normalize_text("你好　世界") == "你好 世界"
    assert normalize_text("ＡＢＣ１２３") == "ABC123"


def test_business_hours():
    from pdd_cs.utils import now_beijing

    now = now_beijing().replace(hour=10, minute=0)
    assert in_business_hours(now, "08:30", "22:30")
    late = now_beijing().replace(hour=23, minute=0)
    assert not in_business_hours(late, "08:30", "22:30")
    # 跨零点
    assert in_business_hours(late, "22:00", "06:00")


def test_panel_port_env_override(monkeypatch, tmp_path):
    from pdd_cs.config import EXAMPLE_CONFIG_PATH, Config

    monkeypatch.setenv("PDD_CS_PORT", "40123")
    monkeypatch.setenv("PDD_CS_AUTH_TOKEN", "x" * 32)
    monkeypatch.setenv("PDD_CS_HOST", "127.0.0.1")
    config = Config.load(EXAMPLE_CONFIG_PATH)
    assert config.server.port == 40123
    assert config.server.auth_token == "x" * 32

    monkeypatch.setenv("PDD_CS_PORT", "not-a-port")
    try:
        Config.load(EXAMPLE_CONFIG_PATH)
    except ValueError as exc:
        assert "PDD_CS_PORT" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("非法 PDD_CS_PORT 应抛出 ValueError")


def test_panel_config_file_and_validation(tmp_path):
    from pdd_cs.config import EXAMPLE_CONFIG_PATH, Config

    text = EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8")
    text = text.replace('data_dir: data', f'data_dir: {(tmp_path / "data").as_posix()}')
    text = text.replace("port: 38617", "port: 45231")
    text = text.replace('auth_token: ""', 'auth_token: "' + "y" * 32 + '"')
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    config = Config.load(path)
    assert config.server.port == 45231
    assert config.server.auth_token
    assert config.validate() == [] or all("api_key" in item for item in config.validate())
