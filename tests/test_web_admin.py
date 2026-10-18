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
    # 模板文件可能是线上/本地已生成的；测试里清掉，确保「一键生成」从空开始
    for generated in ("product-catalog.md", "after-sales-playbook.md"):
        (tmp_path / "knowledge" / generated).unlink(missing_ok=True)
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


def test_qr_login_session(tmp_path, monkeypatch):
    import time

    config = _temp_config(tmp_path)
    web_admin.add_shop(config, "shop-a", "A店")
    config = Config.load(config.source)
    monkeypatch.setattr(web_admin, "find_chrome", lambda: "/usr/bin/google-chrome")
    monkeypatch.setattr(web_admin, "chrome_version", lambda path: "Google Chrome 155.0.0.0")
    monkeypatch.setattr(web_admin, "display_available", lambda: False)
    monkeypatch.setattr(web_admin, "PDDClient", _FakeClient)

    def fake_headless(store, **kwargs):
        kwargs["on_qr"](b"\x89PNG\r\n\x1a\n fake qr")
        return {
            "PASS_ID": "mock-pass",
            "SUB_PASS_ID": "mock-sub",
            "MSA": "mock-msa",
            "SUB_SYS_ID": "mock-sys",
            "anti_content": "mock-anti",
            "api_uid": "mock-api",
        }

    monkeypatch.setattr(web_admin, "login_with_browser_headless", fake_headless)
    manager = web_admin.LoginManager(config)
    session = manager.start_qr_login("shop-a")
    assert session["mode"] == "qr"
    session_id = session["session_id"]
    for _ in range(50):
        current = manager.get_session(session_id)
        if current["state"] in {"success", "failed"}:
            break
        time.sleep(0.05)
    assert current["state"] == "success", current
    assert current["qr_image"]
    assert current["shop_name"] == "测试店铺"


def test_qr_login_reuse_and_cancel(tmp_path, monkeypatch):
    import time

    config = _temp_config(tmp_path)
    web_admin.add_shop(config, "shop-a", "A店")
    config = Config.load(config.source)
    monkeypatch.setattr(web_admin, "find_chrome", lambda: "/usr/bin/google-chrome")
    monkeypatch.setattr(web_admin, "chrome_version", lambda path: "Google Chrome 155.0.0.0")
    monkeypatch.setattr(web_admin, "display_available", lambda: False)

    def fake_headless(store, **kwargs):
        kwargs["on_qr"](b"\x89PNG fake")
        cancel = kwargs["cancel_event"]
        while not cancel.is_set():
            time.sleep(0.02)
        raise web_admin.LoginError("登录已取消")

    monkeypatch.setattr(web_admin, "login_with_browser_headless", fake_headless)
    manager = web_admin.LoginManager(config)
    first = manager.start_qr_login("shop-a")
    session_id = first["session_id"]
    for _ in range(50):
        current = manager.get_session(session_id)
        if current["state"] == "waiting":
            break
        time.sleep(0.02)
    assert current["state"] == "waiting"
    assert current["qr_image"]

    again = manager.start_qr_login("shop-a")
    assert again["reused"] is True
    assert again["session_id"] == session_id

    cancelled = manager.cancel_qr_login(session_id)
    assert cancelled["state"] == "cancelled"
    for _ in range(50):
        if manager.get_session(session_id)["state"] == "cancelled":
            break
        time.sleep(0.02)


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


def test_login_export_import_bundle(tmp_path, monkeypatch):
    config = _temp_config(tmp_path)
    web_admin.add_shop(config, "shop-a", "A店")
    config = Config.load(config.source)
    monkeypatch.setattr(web_admin, "PDDClient", _FakeClient)
    manager = web_admin.LoginManager(config)

    # 未登录时导出应给出明确提示
    try:
        manager.export_bundle("shop-a")
    except web_admin.SettingsError as exc:
        assert "还没有登录态" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("未登录时不应允许导出")

    manager.cookie_login("shop-a", MOCK_COOKIE)
    export = manager.export_bundle("shop-a")
    assert export["cookies"] == 8
    assert export["code"].startswith("PDDCS1.")
    assert "PASS_ID" not in export["code"]  # 压缩编码后不应直接暴露 Cookie
    assert "cookies" in export["file_text"]

    cookie_path = Path(config.shop_profile("shop-a").cookie_file)
    cookie_path.unlink()
    imported = manager.import_bundle("shop-a", export["code"])
    assert imported["imported"] is True and imported["session_valid"] is True
    assert imported["shop_name"] == "测试店铺"
    assert cookie_path.exists()

    # 也支持直接粘贴导出的 JSON 文件内容、或纯 Cookie 文本
    cookie_path.unlink()
    assert manager.import_bundle("shop-a", export["file_text"])["session_valid"] is True
    assert manager.import_bundle("shop-a", MOCK_COOKIE)["session_valid"] is True

    # 跨店铺导入给出提醒
    web_admin.add_shop(config, "shop-b", "B店")
    config = Config.load(config.source)
    manager = web_admin.LoginManager(config)
    imported = manager.import_bundle("shop-b", export["code"])
    assert imported["source_shop"] == "shop-a"
    assert any("shop-a" in warning for warning in imported["warnings"])

    for bad in ("PDDCS1.broken", '{"foo": "bar"}', ""):
        try:
            manager.import_bundle("shop-b", bad)
        except web_admin.SettingsError:
            continue
        raise AssertionError(f"非法登录态未被拒绝: {bad!r}")


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


def test_style_presets_one_click_apply(tmp_path):
    config = _temp_config(tmp_path)
    options = web_admin.style_options_snapshot(config)
    assert len(options["presets"]) == 5
    assert any(item["id"] == "warm" for item in options["presets"])

    web_admin.write_persona(
        config,
        "# 旧人设\n\n## 必须做的事\n- 自定义：先确认店铺信息\n\n## 绝对不能做的事\n- 不承诺结果\n",
    )
    result = web_admin.apply_style(
        config,
        {
            "style": "pro",
            "shop_name": "数码小店",
            "shop_type": "数码",
            "address": "you",
            "emoji": "none",
            "examples": "您好，收到，我马上处理\n请提供订单号后四位",
        },
    )
    assert result["style_name"] == "专业简洁"
    assert "数码小店" in result["persona"]
    assert "官方客服" not in result["persona"]
    assert "您好，收到，我马上处理" in result["persona"]
    assert "自定义：先确认店铺信息" in result["persona"]  # 保留原合规段落
    assert result["backup"] and Path(result["backup"]).exists()

    reloaded = Config.load(config.source)
    assert reloaded.behavior.default_reply == result["default_reply"]
    assert "专业简洁" in web_admin.read_persona(reloaded)

    try:
        web_admin.apply_style(config, {"style": "not-exists"})
    except web_admin.SettingsError:
        pass
    else:  # pragma: no cover
        raise AssertionError("未知风格应被拒绝")


def test_knowledge_templates_one_click(tmp_path):
    config = _temp_config(tmp_path)
    from pdd_cs.knowledge_templates import ensure_knowledge_templates

    first = ensure_knowledge_templates(config)
    assert "product-catalog.md" in first["created"]
    assert "after-sales-playbook.md" in first["created"]
    product = config.knowledge.dir / "product-catalog.md"
    assert product.exists() and "售后" in product.read_text(encoding="utf-8")
    second = ensure_knowledge_templates(config)
    assert second["created"] == []
    assert set(second["skipped"]) == {"product-catalog.md", "after-sales-playbook.md"}


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
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # type: ignore[name-defined]
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


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


def test_web_admin_http_api(tmp_path, monkeypatch):
    asyncio.run(_http_scenario(tmp_path, monkeypatch))


async def _http_scenario(tmp_path, monkeypatch):
    monkeypatch.setattr(web_admin, "PDDClient", _FakeClient)
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

        # Chrome 检测 / 一键下载状态接口
        status, capability = await asyncio.to_thread(_get, base + "api/login/capability")
        assert status == 200 and "chrome_found" in capability
        assert "install_supported" in capability and "display_available" in capability
        status, chrome_session = await asyncio.to_thread(
            _get, base + "api/login/chrome/session"
        )
        assert status == 200 and chrome_session["state"] == "idle" 

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

        # 网页 Cookie 登录 -> 导出登录码 -> 退出 -> 导入登录码
        status, result = await asyncio.to_thread(
            _post, base + "api/login/cookie", {"shop_id": "shop-a", "cookie": MOCK_COOKIE}
        )
        assert status == 200 and result["session_valid"] is True, result
        status, exported = await asyncio.to_thread(
            _get, base + "api/login/export?shop_id=shop-a"
        )
        assert status == 200 and exported["code"].startswith("PDDCS1."), exported
        status, result = await asyncio.to_thread(
            _post, base + "api/login/logout", {"shop_id": "shop-a"}
        )
        assert status == 200, result
        status, result = await asyncio.to_thread(
            _post,
            base + "api/login/import",
            {"shop_id": "shop-a", "text": exported["code"]},
        )
        assert status == 200 and result["session_valid"] is True, result
        status, missing = await asyncio.to_thread(
            _get, base + "api/login/export?shop_id=not-exist"
        )
        assert status == 400

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

        # AI 客服风格一键适配
        status, options = await asyncio.to_thread(_get, base + "api/content/style/options")
        assert status == 200 and len(options["presets"]) >= 5, options
        status, applied = await asyncio.to_thread(
            _post,
            base + "api/content/style/apply",
            {
                "style": "warm",
                "shop_name": "网页小店",
                "shop_type": "女装",
                "address": "bao",
                "emoji": "rich",
                "examples": "宝子，在的～我帮您看下物流",
            },
        )
        assert status == 200, applied
        assert applied["style_name"] == "亲切热情"
        assert "网页小店" in applied["persona"] and "我帮您看下物流" in applied["persona"]
        status, persona = await asyncio.to_thread(_get, base + "api/content/persona")
        assert "网页小店" in persona["text"]

        status, result = await asyncio.to_thread(
            _post, base + "api/content/knowledge",
            {"name": "web-faq.md", "text": "# 网页知识\n- 测试"},
        )
        assert status == 200, result
        status, knowledge = await asyncio.to_thread(_get, base + "api/content/knowledge")
        assert any(item["name"] == "web-faq.md" for item in knowledge["files"])

        # 一键生成商品/售后模板
        status, result = await asyncio.to_thread(
            _post, base + "api/content/knowledge/templates", {}
        )
        assert status == 200, result
        assert "product-catalog.md" in result["created"]
        assert "after-sales-playbook.md" in result["created"]
        assert any(item["name"] == "after-sales-playbook.md" for item in result["files"])

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
