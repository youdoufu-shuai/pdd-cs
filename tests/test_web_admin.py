# -*- coding: utf-8 -*-
"""网页管理后台测试：设置保存、店铺管理、Cookie 登录、话术/知识库、热重载、HTTP API。"""
from __future__ import annotations

import asyncio
import json
import shutil
import urllib.request
from pathlib import Path

import yaml

from pdd_cs import web_admin
from pdd_cs.config import PROJECT_ROOT, Config
from pdd_cs.engine.service import PDDAutoReplyService
from pdd_cs.pdd.api import PDDSessionExpired
from pdd_cs.pdd.cdp_login import LoginError
from pdd_cs.server import DashboardServer

MOCK_COOKIE = (
    "PASS_ID=mock-pass; SUB_PASS_ID=mock-sub; MSA=mock-msa; SUB_SYS_ID=mock-sys; "
    "pdd_user_id=1; pdd_user_uin=1; anti_content=mock-anti; api_uid=mock-api"
)


def _temp_config(tmp_path: Path, *, shops_empty: bool = True) -> Config:
    """生成一份完全隔离的临时配置（数据/规则/知识库/人设都在 tmp）。"""
    text = (PROJECT_ROOT / "config" / "config.example.yaml").read_text(encoding="utf-8")
    text = text.replace("data_dir: data", f"data_dir: {(tmp_path / 'data').as_posix()}")
    text = text.replace("persona_file: config/persona.md", f"persona_file: {(tmp_path / 'persona.md').as_posix()}")
    text = text.replace("dir: config/knowledge", f"dir: {(tmp_path / 'knowledge').as_posix()}")
    for name in ("keywords", "transfer", "replace", "filters", "goods", "compliance", "blacklist", "qc"):
        text = text.replace(f"config/rules/{name}.yaml", str(tmp_path / "rules" / f"{name}.yaml"))
    text = text.replace(
        "cookie_file: config/secrets/cookies.json",
        f"cookie_file: {(tmp_path / 'cookies.json').as_posix()}",
    )
    text = text.replace("shops: []", "shops: []")
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")

    (tmp_path / "rules").mkdir(parents=True, exist_ok=True)
    for name in ("keywords", "transfer", "replace", "filters", "goods", "compliance", "blacklist", "qc"):
        source = PROJECT_ROOT / "config" / "rules" / f"{name}.yaml"
        if source.exists():
            shutil.copyfile(source, tmp_path / "rules" / f"{name}.yaml")
    shutil.copyfile(PROJECT_ROOT / "config" / "persona.md", tmp_path / "persona.md")
    shutil.copytree(PROJECT_ROOT / "config" / "knowledge", tmp_path / "knowledge", dirs_exist_ok=True)
    (tmp_path / "data").mkdir(exist_ok=True)
    return Config.load(path)


# ---------------------------------------------------------------------------
# 设置
# ---------------------------------------------------------------------------
def test_settings_snapshot_masks_secrets(tmp_path):
    config = _temp_config(tmp_path)
    config.llm.api_key = "sk-test-secret-1234"
    config.alerts.webhook_url = "https://example.com/hook/abcdef"
    snapshot = web_admin.settings_snapshot(config)
    assert snapshot["llm"]["api_key_set"] is True
    assert snapshot["llm"]["api_key_hint"].endswith("1234")
    assert "sk-test-secret" not in json.dumps(snapshot)
    assert snapshot["alerts"]["webhook_set"] is True
    assert "abcdef" not in snapshot["alerts"]["webhook_hint"]


def test_apply_settings_updates_and_restart_flag(tmp_path):
    config = _temp_config(tmp_path)
    result = web_admin.apply_settings(
        config,
        {
            "behavior": {"default_reply": "您好，客服马上为您处理", "no_match": "default"},
            "llm": {"enabled": True, "base_url": "https://api.example.com/v1", "model": "test-model",
                    "api_key": "sk-new-key-0001"},
            "business_hours": {"enabled": True, "start": "09:00", "end": "22:00"},
            "alerts": {"enabled": True, "format": "feishu",
                       "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/xxx"},
            "server": {"port": 40123},
        },
    )
    assert result["restart_required"] is True
    assert "behavior.default_reply" in result["changed"]

    reloaded = Config.load(config.source)
    assert reloaded.behavior.default_reply == "您好，客服马上为您处理"
    assert reloaded.behavior.no_match == "default"
    assert reloaded.llm.api_key == "sk-new-key-0001"
    assert reloaded.llm.model == "test-model"
    assert reloaded.business_hours.start == "09:00"
    assert reloaded.alerts.format == "feishu"
    assert reloaded.server.port == 40123


def test_apply_settings_rejects_bad_values(tmp_path):
    config = _temp_config(tmp_path)
    for payload in (
        {"behavior": {"no_match": "xxx"}},
        {"server": {"port": 80}},
        {"alerts": {"format": "sms"}},
        {"business_hours": {"start": "9点"}},
    ):
        try:
            web_admin.apply_settings(config, payload)
        except web_admin.SettingsError:
            continue
        raise AssertionError(f"非法设置未被拦截: {payload}")


# ---------------------------------------------------------------------------
# 店铺管理
# ---------------------------------------------------------------------------
def test_shop_crud(tmp_path):
    config = _temp_config(tmp_path)
    shops = web_admin.shop_snapshots(config)
    assert len(shops) == 1 and shops[0]["id"] == "default"

    web_admin.add_shop(config, "shop-a", "A店")
    config = Config.load(config.source)
    shops = web_admin.shop_snapshots(config)
    assert [item["id"] for item in shops] == ["shop-a"]

    web_admin.add_shop(config, "shop-b", "B店")
    config = Config.load(config.source)
    assert [item["id"] for item in web_admin.shop_snapshots(config)] == ["shop-a", "shop-b"]

    web_admin.update_shop(config, "shop-b", name="B店（百货）", enabled=False)
    config = Config.load(config.source)
    shop = next(item for item in web_admin.shop_snapshots(config) if item["id"] == "shop-b")
    assert shop["name"] == "B店（百货）" and shop["enabled"] is False

    web_admin.delete_shop(config, "shop-b")
    config = Config.load(config.source)
    assert [item["id"] for item in web_admin.shop_snapshots(config)] == ["shop-a"]

    try:
        web_admin.delete_shop(config, "shop-a")
    except web_admin.SettingsError:
        pass
    else:  # pragma: no cover
        raise AssertionError("删除最后一个店铺应被拒绝")

    try:
        web_admin.add_shop(config, "bad id!", "X")
    except web_admin.SettingsError:
        pass
    else:  # pragma: no cover
        raise AssertionError("非法店铺 ID 应被拒绝")


# ---------------------------------------------------------------------------
# Cookie 登录
# ---------------------------------------------------------------------------
class _FakeClient:
    def __init__(self, *args, **kwargs):
        self.args = args

    def validate_session(self):
        return {"mall_name": "测试店铺"}


class _FakeExpiredClient(_FakeClient):
    def validate_session(self):
        raise PDDSessionExpired("登录态已失效")


def test_cookie_login_success(tmp_path, monkeypatch):
    config = _temp_config(tmp_path)
    web_admin.add_shop(config, "shop-a", "A店")
    config = Config.load(config.source)
    monkeypatch.setattr(web_admin, "PDDClient", _FakeClient)

    result = web_admin.LoginManager(config).cookie_login("shop-a", MOCK_COOKIE)
    assert result["ok"] is True and result["shop_name"] == "测试店铺"
    cookie_path = Path(config.shop_profile("shop-a").cookie_file)
    assert cookie_path.exists()


def test_cookie_login_invalid_session_removes_cookie(tmp_path, monkeypatch):
    config = _temp_config(tmp_path)
    web_admin.add_shop(config, "shop-a", "A店")
    config = Config.load(config.source)
    monkeypatch.setattr(web_admin, "PDDClient", _FakeExpiredClient)
    try:
        web_admin.LoginManager(config).cookie_login("shop-a", MOCK_COOKIE)
    except web_admin.SettingsError as exc:
        assert "Cookie 校验失败" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("无效登录态应报错")
    assert not Path(config.shop_profile("shop-a").cookie_file).exists()


def test_browser_login_requires_chrome(tmp_path, monkeypatch):
    config = _temp_config(tmp_path)

    def _no_chrome(*args, **kwargs):
        raise LoginError("未找到 Chrome")

    monkeypatch.setattr(web_admin, "find_chrome", _no_chrome)
    manager = web_admin.LoginManager(config)
    assert manager.capability()["browser_login"] is False
    try:
        manager.start_browser_login("default")
    except web_admin.SettingsError as exc:
        assert "Chrome" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("没有 Chrome 时应提示改用 Cookie 登录")


# ---------------------------------------------------------------------------
# 话术 / 知识库 / 人设
# ---------------------------------------------------------------------------
def test_content_and_keywords(tmp_path):
    config = _temp_config(tmp_path)
    web_admin.write_persona(config, "# 测试人设\n欢迎光临")
    assert "测试人设" in web_admin.read_persona(config)

    files = web_admin.list_knowledge(config)
    assert any(item["name"] == "store-policy.md" for item in files)
    text = web_admin.read_knowledge(config, "store-policy.md")
    assert text
    web_admin.write_knowledge(config, "new-faq.md", "# 新 FAQ\n- 测试")
    assert any(item["name"] == "new-faq.md" for item in web_admin.list_knowledge(config))
    try:
        web_admin.read_knowledge(config, "../etc/passwd")
    except web_admin.SettingsError:
        pass
    else:  # pragma: no cover
        raise AssertionError("非法知识库文件名应被拒绝")

    rules = web_admin.load_keywords(config)
    assert len(rules) >= 10
    rules.append({"keywords": "测试关键字", "reply": "这是网页添加的回复", "match": "fuzzy"})
    count = web_admin.save_keywords(config, rules)
    assert count == len(rules)
    saved = web_admin.load_keywords(config)
    assert any(item["keywords"] == "测试关键字" for item in saved)


# ---------------------------------------------------------------------------
# 服务热重载
# ---------------------------------------------------------------------------
def test_service_reload_degraded(tmp_path):
    config = _temp_config(tmp_path)
    service = PDDAutoReplyService(config)
    try:
        asyncio.run(service.start(allow_degraded=True))
        status = service.status()
        assert status["started"] is True
        assert status["setup_mode"] is True  # 默认店铺没有 Cookie，进入待登录模式

        new_config = Config.load(config.source)
        new_config.behavior.default_reply = "热重载后的默认回复"
        result = asyncio.run(service.reload(new_config, allow_degraded=True))
        assert result["started"] is True
        assert service.config.behavior.default_reply == "热重载后的默认回复"
    finally:
        service.store.close()


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------
def _get(url: str):
    with urllib.request.urlopen(url, timeout=20) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _post(url: str, payload: dict):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # type: ignore[name-defined]
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def test_web_admin_http_api(tmp_path):
    asyncio.run(_http_scenario(tmp_path))


async def _http_scenario(tmp_path):
    config = _temp_config(tmp_path)
    config.server.host = "127.0.0.1"
    config.server.port = 0  # 随机端口，避免与本机正在运行的面板冲突
    service = PDDAutoReplyService(config)
    dashboard = None
    try:
        await service.start(allow_degraded=True)
        dashboard = DashboardServer(config, service)
        dashboard.start()
        base = dashboard.url

        status, setup = await asyncio.to_thread(_get, base + "api/setup")
        assert status == 200
        assert setup["setup_mode"] is True
        assert isinstance(setup["shops"], list)

        # 新增店铺
        status, result = await asyncio.to_thread(
            _post, base + "api/shops/add", {"id": "shop-a", "name": "A店"}
        )
        assert status == 200, result
        assert any(shop["id"] == "shop-a" for shop in result["shops"])

        # 更新店铺
        status, result = await asyncio.to_thread(
            _post, base + "api/shops/update",
            {"shop_id": "shop-a", "name": "A店（女装）", "enabled": False},
        )
        assert status == 200, result
        shop = next(item for item in result["shops"] if item["id"] == "shop-a")
        assert shop["name"] == "A店（女装）" and shop["enabled"] is False

        # 保存设置（回复行为）
        status, result = await asyncio.to_thread(
            _post, base + "api/settings",
            {"behavior": {"default_reply": "网页设置的默认回复", "no_match": "default"}},
        )
        assert status == 200, result
        settings = result["settings"]
        assert settings["behavior"]["default_reply"] == "网页设置的默认回复"

        # 话术保存 + 校验
        status, rules = await asyncio.to_thread(_get, base + "api/rules/keywords")
        assert status == 200 and rules["rules"]
        new_rules = rules["rules"] + [{"keywords": "网页关键词", "reply": "网页添加回复"}]
        status, result = await asyncio.to_thread(
            _post, base + "api/rules/keywords", {"rules": new_rules}
        )
        assert status == 200, result
        assert result["count"] == len(new_rules)

        # 人设 / 知识库
        status, result = await asyncio.to_thread(
            _post, base + "api/content/persona", {"text": "# 网页人设\n你好"}
        )
        assert status == 200, result
        status, persona = await asyncio.to_thread(_get, base + "api/content/persona")
        assert "网页人设" in persona["text"]

        status, result = await asyncio.to_thread(
            _post, base + "api/content/knowledge",
            {"name": "web-faq.md", "text": "# 网页知识\n- 测试"},
        )
        assert status == 200, result
        status, knowledge = await asyncio.to_thread(_get, base + "api/content/knowledge")
        assert any(item["name"] == "web-faq.md" for item in knowledge["files"])

        # 新增第二家店后再删除第一家（至少保留一个店铺）
        status, result = await asyncio.to_thread(
            _post, base + "api/shops/add", {"id": "shop-b", "name": "B店"}
        )
        assert status == 200, result
        status, result = await asyncio.to_thread(
            _post, base + "api/shops/delete", {"shop_id": "shop-a"}
        )
        assert status == 200, result
        assert [shop["id"] for shop in result["shops"]] == ["shop-b"]

        # 错误参数返回 400
        status, result = await asyncio.to_thread(
            _post, base + "api/shops/add", {"id": "bad id!", "name": "X"}
        )
        assert status == 400
    finally:
        if dashboard:
            dashboard.stop()
        await service.stop()
