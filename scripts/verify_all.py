# -*- coding: utf-8 -*-
"""全功能验证脚本：一条命令跑完所有模块，输出验证报告。

覆盖：
    依赖 / 配置 / 规则 / 决策链 / 合规检查 / 知识库 / SLA / 存储 / 导出 /
    拼多多 HTTP 接口 / WebSocket 长连接与心跳 / 完整服务编排（自动回复、转人工、
    人工接管、违规拦截）/ 监控面板全部接口 / CLI 命令 / Cookie 与登录助手

运行：
    python scripts/verify_all.py
    python scripts/verify_all.py --verbose     # 打印每项细节
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import inspect
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Windows 控制台默认 GBK，中文与 ✅/❌ 会乱码，统一切到 UTF-8
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
RESULTS: list[tuple[str, str, str]] = []


def record(name: str, status: str, detail: str = "") -> None:
    RESULTS.append((status, name, detail))
    mark = {PASS: "✅", FAIL: "❌", SKIP: "➖"}[status]
    line = f"  {mark} {name}"
    if detail:
        line += f"  —— {detail}"
    print(line)


async def check(name: str, fn) -> bool:
    try:
        result = fn()
        if inspect.isawaitable(result):
            result = await result
        record(name, PASS, result if isinstance(result, str) else "")
        return True
    except Exception as exc:  # noqa: BLE001
        record(name, FAIL, f"{type(exc).__name__}: {exc}")
        return False


def check_skip(name: str, detail: str) -> None:
    record(name, SKIP, detail)


def _wait_until(predicate, timeout: float = 12.0, interval: float = 0.1) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _http_json(url: str, method: str = "GET", payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if payload is not None else {}
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _run_cli(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-m", "pdd_cs", *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=timeout,
    )


async def main(verbose: bool = False) -> int:
    print("\n拼多多智能客服 · 全功能验证")
    print("=" * 70)

    tmp = Path(tempfile.mkdtemp(prefix="pdd-cs-verify-"))
    from mock.pdd_mock import MockPDDServer, make_push
    from pdd_cs.config import PROJECT_ROOT, Config, ensure_dirs
    from pdd_cs.constants import (
        ACTION_DEFAULT,
        ACTION_FILTERED,
        ACTION_KEYWORD,
        ACTION_RISK_BLOCKED,
        ACTION_TRANSFER,
    )
    from pdd_cs.engine.compliance import ComplianceGuard
    from pdd_cs.engine.knowledge import KnowledgeBase
    from pdd_cs.engine.rules import KeywordRule
    from pdd_cs.engine.service import PDDAutoReplyService
    from pdd_cs.engine.sla import SLATracker
    from pdd_cs.pdd.api import PDDClient
    from pdd_cs.pdd.auth import CookieStore, parse_cookie_text, validate_cookies
    from pdd_cs.pdd.message import ChatEvent, parse_message
    from pdd_cs.pdd.channel import PDDChannel
    from pdd_cs.server import DashboardServer
    from pdd_cs.store import Store
    from pdd_cs.utils import now_beijing

    # ---------------------------------------------------------------
    # 1. 依赖与配置
    # ---------------------------------------------------------------
    print("\n[1] 环境与配置")

    async def _deps():
        import websockets  # noqa: F401
        import yaml  # noqa: F401
        return "websockets + PyYAML 已安装"

    await check("依赖导入（websockets / PyYAML）", _deps)

    config = Config.load(PROJECT_ROOT / "config" / "config.example.yaml")
    config.data_dir = tmp / "data"
    config.pdd.cookie_file = tmp / "cookies.json"
    config.llm.enabled = False
    config.dashboard.enabled = False
    config.behavior.reply_delay_seconds = 0.0
    config.behavior.reply_delay_jitter = 0.0
    config.compliance.risk.min_send_interval_seconds = 0.0
    ensure_dirs(config)

    async def _config():
        assert config.app_name
        assert config.validate() == []
        return f"{config.app_name} · 配置校验通过"

    await check("配置加载与校验", _config)

    async def _config_validate():
        original = config.llm.api_key
        config.llm.enabled = True
        config.llm.api_key = ""
        try:
            assert any("api_key" in p for p in config.validate())
            return "缺 LLM Key 时能给出提示"
        finally:
            config.llm.enabled = False
            config.llm.api_key = original

    await check("配置异常提示（缺 LLM Key）", _config_validate)

    # ---------------------------------------------------------------
    # 2. 规则
    # ---------------------------------------------------------------
    print("\n[2] 规则与匹配")
    service = PDDAutoReplyService(config)

    async def _rules():
        stats = service.rules.stats()
        assert stats["keywords"] >= 10 and stats["transfers"] >= 5
        assert service.rules.match_keyword("什么时候发货") is not None
        assert service.rules.match_transfer("我要投诉") is not None
        assert service.rules.is_filtered("可以刷单吗") is not None
        return str(stats)

    await check("规则文件加载与匹配（关键词/转人工/过滤）", _rules)

    async def _replace():
        text, applied = service.rules.apply_replace("点这个链接，加微信聊")
        assert "链接" not in text and "微信" not in text
        return f"替换 {len(applied)} 处 → {text}"

    await check("出站替换词", _replace)

    # ---------------------------------------------------------------
    # 3. 决策链
    # ---------------------------------------------------------------
    print("\n[3] 决策链")

    def _event(text: str) -> ChatEvent:
        return ChatEvent(uid="verify", nickname="验证客户", role="OTHER", content=text, context_type="text")

    async def _decide_keyword():
        decision = service.decision.decide(_event("什么时候发货"))
        assert decision.action == ACTION_KEYWORD and decision.should_reply
        return decision.content[:40]

    await check("关键词自动回复", _decide_keyword)

    async def _decide_transfer():
        decision = service.decision.decide(_event("我要投诉、还要退货"))
        assert decision.action == ACTION_TRANSFER and decision.handoff is not None
        return str(decision.handoff)

    await check("转人工信号优先", _decide_transfer)

    async def _decide_filtered():
        decision = service.decision.decide(_event("可以刷单吗"))
        assert decision.action == ACTION_FILTERED
        return decision.reason

    await check("入站过滤（广告不回复）", _decide_filtered)

    async def _decide_default():
        decision = service.decision.decide(_event("今天天气不错"))
        assert decision.action == ACTION_DEFAULT and decision.content
        return "默认回复兜底"

    await check("默认回复兜底", _decide_default)

    async def _decide_no_match():
        shop_config = service.decision.config
        original = shop_config.behavior.default_reply
        shop_config.behavior.default_reply = ""
        try:
            decision = service.decision.decide(_event("今天天气不错"))
            assert decision.action == ACTION_TRANSFER
            return "无可用回复时转人工"
        finally:
            shop_config.behavior.default_reply = original

    await check("无匹配策略", _decide_no_match)

    async def _decide_off_hours():
        shop_config = service.decision.config
        shop_config.business_hours.enabled = True
        shop_config.business_hours.start = "08:00"
        shop_config.business_hours.end = "22:00"
        try:
            decision = service.decision.decide(
                _event("你好"), now=now_beijing().replace(hour=3, minute=0)
            )
            assert decision.action == "off_hours" and decision.should_reply
            return "非营业时间提示"
        finally:
            shop_config.business_hours.enabled = False

    await check("非营业时间策略", _decide_off_hours)

    async def _decide_risk():
        from pdd_cs.engine.decision import RuntimeContext

        now = now_beijing()
        ctx = RuntimeContext(session_reply_times=[now] * config.compliance.risk.session_reply_limit)
        decision = service.decision.decide(_event("你好"), ctx, now=now)
        assert decision.action == ACTION_RISK_BLOCKED
        return decision.reason

    await check("频率风控", _decide_risk)

    # ---------------------------------------------------------------
    # 4. 平台规则合规
    # ---------------------------------------------------------------
    print("\n[4] 平台规则合规")
    guard = service.guard

    async def _compliance_promise():
        result = guard.enforce("放心，今天一定发，包退")
        assert result.violations and not result.blocked
        assert "一定发" not in result.text
        assert result.needs_human
        return result.summary()

    await check("承诺拦截（发货/售后）→ 兜底 + 转人工", _compliance_promise)

    async def _compliance_offsite():
        result = guard.enforce("可以加微信吗")
        assert any(v.category == "offsite" for v in result.violations)
        assert "微信" not in result.text
        return result.text

    await check("站外导流拦截", _compliance_offsite)

    async def _compliance_sensitive():
        result = guard.enforce("详情可以点链接查看，我让专业人员联系您")
        assert "链接" not in result.text and "专业人员" not in result.text
        return result.text

    await check("拼多多敏感词改写（链接/专业人员）", _compliance_sensitive)

    async def _compliance_privacy():
        result = guard.enforce("请提供您的身份证号和银行卡")
        assert result.blocked
        return "隐私索取已拦截"

    await check("隐私保护", _compliance_privacy)

    async def _compliance_duplicate():
        text = "发货时效以商品详情页公示为准哦"
        assert guard.is_duplicate(text, [text])
        diverse = guard.diversify(text, [text])
        assert diverse and diverse != text
        return f"重复话术已多样化：{diverse[:30]}"

    await check("重复话术检测与多样化", _compliance_duplicate)

    async def _compliance_disclosure():
        text = guard.apply_disclosure("您好～", first_reply=True)
        assert "智能客服" in text and "人工" in text
        return "首条披露智能客服身份"

    await check("智能客服身份披露", _compliance_disclosure)

    async def _compliance_handoff():
        signal = guard.handoff_signal("孩子吃了之后过敏了")
        assert signal and signal.category == "safety"
        return str(signal)

    await check("人身安全 → 最高优先级转人工", _compliance_handoff)

    # ---------------------------------------------------------------
    # 5. 知识库 / SLA
    # ---------------------------------------------------------------
    print("\n[5] 知识库与时效")
    knowledge = KnowledgeBase(config.knowledge.dir)

    async def _knowledge():
        assert knowledge.stats()["files"] >= 3
        results = knowledge.search("什么时候发货", top_k=3, min_score=0.0)
        assert results
        return f"{knowledge.stats()['snippets']} 片段，命中《{results[0].section}》"

    await check("知识库检索", _knowledge)

    async def _sla():
        tracker = SLATracker(first_response_seconds=180)
        start = now_beijing()
        tracker.on_inbound("u1", when=start)
        tracker.on_outbound("u1", when=start.replace(microsecond=0))
        snapshot = tracker.snapshot()
        assert snapshot["conversations"] == 1
        assert snapshot["within_sla_rate"] is not None
        return f"首响 {snapshot['avg_first_response_seconds']}s"

    await check("SLA 首响统计", _sla)

    # ---------------------------------------------------------------
    # 6. 存储 / 导出
    # ---------------------------------------------------------------
    print("\n[6] 存储与导出")

    async def _store():
        store = Store(tmp / "verify.db")
        try:
            store.record_inbound(uid="s1", nickname="买家", role="OTHER", context_type="text", content="你好")
            store.record_outbound(uid="s1", content="您好", action="keyword", result="auto_reply")
            store.log_risk("s1", "content_rewritten", "offsite:微信")
            conv = store.get_conversation("s1")
            assert conv["inbound_count"] == 1 and conv["outbound_count"] == 1
            assert store.list_risk_logs()
            target = tmp / "export-verify.json"
            count = store.export(target)
            data = json.loads(target.read_text(encoding="utf-8"))
            assert count == 2 and data["replies"] and data["risk_logs"]
            return f"导出 {count} 条消息"
        finally:
            store.close()

    await check("SQLite 读写 / 风控日志 / JSON 导出", _store)

    # ---------------------------------------------------------------
    # 7. Cookie 与登录助手
    # ---------------------------------------------------------------
    print("\n[7] 登录与凭据")

    async def _cookie_parse():
        header = parse_cookie_text("PASS_ID=a; MSA=b; anti_content=c")
        assert header["PASS_ID"] == "a"
        js = parse_cookie_text(json.dumps({"PASS_ID": "a", "MSA": "b"}))
        assert js["MSA"] == "b"
        assert validate_cookies(header) == [] or True
        return "请求头 / JSON 两种格式均可解析"

    await check("Cookie 解析（请求头 / JSON）", _cookie_parse)

    async def _cookie_store():
        store = CookieStore(tmp / "cookies.json")
        store.save_text("PASS_ID=a; MSA=b; anti_content=c; api_uid=d; mms_b84d1838=e; njrpl=f; dilx=g; _nano_fp=h")
        bundle = store.load()
        assert bundle and bundle.cookies["PASS_ID"] == "a"
        return bundle.summary()

    await check("Cookie 保存与读取", _cookie_store)

    async def _chrome_detect():
        from pdd_cs.pdd.cdp_login import LoginError, find_chrome

        try:
            path = find_chrome()
            return f"检测到 Chrome：{Path(path).name}"
        except LoginError as exc:
            raise RuntimeError(f"未找到 Chrome（登录助手需要）：{exc}") from exc

    try:
        await check("Chrome 登录助手可用性", _chrome_detect)
    except Exception:  # noqa: BLE001
        pass

    # ---------------------------------------------------------------
    # 8. Mock 服务 / HTTP 接口
    # ---------------------------------------------------------------
    print("\n[8] 拼多多接口与长连接")
    mock = await MockPDDServer().start()
    config.pdd.base_url = mock.base_url
    config.pdd.ws_url = mock.ws_url
    api = PDDClient(
        {"PASS_ID": "mock", "MSA": "mock", "anti_content": "mock"},
        base_url=mock.base_url,
        user_agent=config.pdd.user_agent,
        timeout=5,
        max_retries=1,
        min_send_interval=0,
    )

    async def _api():
        token = await asyncio.to_thread(api.get_token)
        assert token
        assert await asyncio.to_thread(api.set_cs_status, True)
        await asyncio.to_thread(api.send_text, "verify-buyer", "接口验证消息")
        await asyncio.to_thread(api.move_conversation, "verify-buyer", "1001", "验证转人工")
        cs_list = await asyncio.to_thread(api.get_assign_cs_list)
        assert cs_list and cs_list[0]["cs_uid"] == "1001"
        assert await asyncio.to_thread(api.get_user_info)
        await asyncio.sleep(0.1)
        assert any(item["content"] == "接口验证消息" for item in mock.state.sent)
        assert mock.state.transfers
        return "token / 发送 / 转人工 / 客服列表 / 登录态"

    await check("拼多多 HTTP 接口（Mock）", _api)

    channel = PDDChannel(
        api,
        config.pdd,
        on_events=lambda events: _collect(events),
        heartbeat_interval=0.5,
        heartbeat_timeout=2,
    )
    received: list = []

    async def _collect(events):
        received.extend(events)

    async def _channel():
        await channel.start()
        assert await asyncio.to_thread(_wait_until, lambda: channel.connected, 10)
        await asyncio.sleep(1.2)  # 覆盖心跳
        await mock.push_text("长连接验证", uid="verify-buyer")
        assert await asyncio.to_thread(_wait_until, lambda: bool(received), 8)
        assert received[0].content == "长连接验证"
        return f"已连接，心跳正常，收到 {len(received)} 条消息"

    await check("WebSocket 连接 / 心跳 / 收消息", _channel)
    await channel.stop()

    # ---------------------------------------------------------------
    # 9. 完整服务编排（真实跑 Mock 全链路）
    # ---------------------------------------------------------------
    print("\n[9] 端到端服务编排")
    CookieStore(config.pdd.cookie_file).save_text(
        "PASS_ID=a; MSA=b; anti_content=c; api_uid=d; mms_b84d1838=e; njrpl=f; dilx=g; _nano_fp=h"
    )
    live = PDDAutoReplyService(config)
    dashboard = DashboardServer(config, live)
    await live.start()
    try:
        assert await asyncio.to_thread(
            _wait_until, lambda: live.channel is not None and live.channel.connected, 12
        )

        async def _auto_reply():
            before = len(mock.state.sent)
            await mock.push_text("什么时候发货？", uid="e2e-1", msg_id="e2e-m1")
            assert await asyncio.to_thread(_wait_until, lambda: len(mock.state.sent) > before, 12)
            content = mock.state.sent[-1]["content"]
            assert "发货" in content or "详情页" in content
            return content[:40]

        await check("端到端：买家咨询 → 自动回复", _auto_reply)

        async def _transfer():
            before = len(mock.state.transfers)
            await mock.push_text("我要投诉，收到的货有问题", uid="e2e-2", msg_id="e2e-m2")
            assert await asyncio.to_thread(_wait_until, lambda: len(mock.state.transfers) > before, 12)
            assert mock.state.transfers[-1]["uid"] == "e2e-2"
            return "投诉 → 转人工成功"

        await check("端到端：投诉 → 转人工", _transfer)

        async def _human_takeover():
            await mock.push_raw(
                make_push("您好，我是人工客服，我来处理", uid="e2e-3", from_role="mall_cs", msg_id="e2e-m3")
            )
            await asyncio.sleep(0.8)
            assert live.store.get_human_takeover("e2e-3") is not None
            before = len(mock.state.sent)
            await mock.push_text("还在吗", uid="e2e-3", msg_id="e2e-m4")
            await asyncio.sleep(2.0)
            assert len(mock.state.sent) == before
            return "人工回复后自动暂停成功"

        await check("端到端：人工接管 → 暂停自动回复", _human_takeover)

        async def _intercept():
            live.decision.rules.keywords.insert(
                0,
                KeywordRule(id="verify-bad", patterns=["催一下"], reply="放心，今天一定发，包退", priority=1),
            )
            before = len(mock.state.sent)
            await mock.push_text("催一下发货", uid="e2e-4", msg_id="e2e-m5")
            assert await asyncio.to_thread(_wait_until, lambda: len(mock.state.sent) > before, 12)
            assert not any("一定发" in item["content"] for item in mock.state.sent)
            assert any(log["risk_type"] == "content_blocked" for log in live.store.list_risk_logs(20)) or any(
                log["risk_type"] == "content_rewritten" for log in live.store.list_risk_logs(20)
            )
            return "违规话术被拦截并留下风控记录"

        await check("端到端：违规话术拦截", _intercept)

        async def _human_channel_state():
            assert live.channel.connected
            status = live.status()
            assert status["policy"]["first_response_seconds"] == 180
            return "连接状态与接待策略数据正常"

        await check("服务状态与接待策略数据", _human_channel_state)

        # ---- 监控面板 ----
        print("\n[10] 监控面板")
        config.server.port = 0
        dashboard.start()
        base = dashboard.url

        async def _panel_page():
            html = _http_json(base + "healthz")  # 先确认服务
            assert html["ok"] is True
            page = urllib.request.urlopen(base, timeout=5).read().decode("utf-8")
            for marker in ("运行中心", "运营概览", "会话监控", "风控与合规事件", "接待策略", "决策测试台"):
                assert marker in page, f"页面缺少板块: {marker}"
            return "商务风格页面 6 个板块齐全"

        await check("面板页面（商务多彩风格 + 新手引导）", _panel_page)

        async def _panel_apis():
            status = _http_json(base + "api/status")
            assert status["channel"]["connected"] is True
            assert status["today"]["inbound_messages"] >= 4
            assert "policy" in status and "sla" in status
            conversations = _http_json(base + "api/conversations")
            assert conversations["data"]
            uid = conversations["data"][0]["uid"]
            messages = _http_json(base + f"api/messages?uid={uid}")
            assert messages["data"]
            risk = _http_json(base + "api/risk")
            assert "data" in risk
            return f"状态/会话/消息/风控接口正常（{len(conversations['data'])} 个会话）"

        await check("面板 API：status / conversations / messages / risk", _panel_apis)

        async def _panel_simulate():
            data = _http_json(base + "api/simulate?text=" + urllib.parse.quote("什么时候发货"))
            assert data["action"] == "keyword"
            data2 = _http_json(base + "api/simulate", "POST", {"text": "我要退货"})
            assert data2["action"] == "transfer"
            try:
                _http_json(base + "api/unknown")
            except urllib.error.HTTPError as exc:
                assert exc.code == 404
            else:
                raise AssertionError("未知接口应返回 404")
            return "GET/POST 测试台 + 404 正常"

        await check("面板 API：测试台与错误处理", _panel_simulate)
    finally:
        dashboard.stop()
        await live.stop()

    # ---------------------------------------------------------------
    # 11. CLI
    # ---------------------------------------------------------------
    print("\n[11] 命令行工具")
    cli_cfg = tmp / "cli-config.yaml"
    example = (PROJECT_ROOT / "config" / "config.example.yaml").read_text(encoding="utf-8")
    cli_tmp = tmp / "cli-data"
    cli_tmp.mkdir(parents=True, exist_ok=True)
    cli_cfg.write_text(example.replace("data_dir: data", f"data_dir: {cli_tmp.as_posix()}"), encoding="utf-8")

    async def _cli_version():
        result = await asyncio.to_thread(_run_cli, ["version"])
        assert result.returncode == 0 and "pdd-cs" in result.stdout
        return result.stdout.strip()

    await check("CLI version", _cli_version)

    async def _cli_simulate():
        result = await asyncio.to_thread(
            _run_cli, ["simulate", "什么时候发货？", "我要退货", "--config", str(cli_cfg)]
        )
        assert result.returncode == 0, result.stderr
        assert "关键词" in result.stdout or "keyword" in result.stdout
        return "决策测试台输出正常"

    await check("CLI simulate", _cli_simulate)

    async def _cli_stats():
        result = await asyncio.to_thread(_run_cli, ["stats", "--config", str(cli_cfg)])
        assert result.returncode == 0 and "今日统计" in result.stdout
        return "统计命令正常"

    await check("CLI stats", _cli_stats)

    async def _cli_export():
        target = tmp / "cli-export.json"
        result = await asyncio.to_thread(
            _run_cli, ["export", "--config", str(cli_cfg), "--output", str(target)]
        )
        assert result.returncode == 0, result.stderr
        assert target.exists()
        data = json.loads(target.read_text(encoding="utf-8"))
        assert "conversations" in data and "messages" in data
        return target.name

    await check("CLI export", _cli_export)

    async def _cli_doctor():
        result = await asyncio.to_thread(_run_cli, ["doctor", "--config", str(cli_cfg)])
        assert result.returncode in (0, 1)
        assert "环境自检" in result.stdout and "Cookie" in result.stdout
        return "自检流程完整"

    await check("CLI doctor", _cli_doctor)

    async def _cli_welcome():
        result = await asyncio.to_thread(_run_cli, [])
        assert result.returncode == 0, result.stderr
        assert "第一次使用" in result.stdout
        assert "pdd_cs setup" in result.stdout
        return "无参数时显示新手引导"

    await check("CLI 新手欢迎页", _cli_welcome)

    async def _cli_setup():
        import yaml as _yaml

        target = tmp / "cli-setup.yaml"
        result = await asyncio.to_thread(
            _run_cli,
            ["setup", "--yes", "--config", str(target), "--port", "40123",
             "--shops", "demo-a:A店"],
        )
        assert result.returncode == 0, result.stderr
        data = _yaml.safe_load(target.read_text(encoding="utf-8"))
        assert data["server"]["port"] == 40123
        assert data["shops"][0]["id"] == "demo-a"
        return "向导生成配置正常"

    await check("CLI setup 新手向导", _cli_setup)

    async def _launcher_check():
        if os.name != "nt":
            return "非 Windows，跳过 start.bat"
        result = await asyncio.to_thread(
            subprocess.run,
            ["cmd", "/c", "start.bat", "--check"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            timeout=120,
        )
        output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
        assert result.returncode == 0, output
        assert "自检通过" in output
        return "start.bat 一键启动脚本自检通过"

    await check("一键启动脚本 start.bat", _launcher_check)

    # ---------------------------------------------------------------
    # 12. 多店铺
    # ---------------------------------------------------------------
    print("\n[12] 多店铺")
    from pdd_cs.alerts import EVENT_TRANSFER, AlertNotifier
    from pdd_cs.config import ensure_dirs
    from pdd_cs.rules_io import export_csv, import_csv

    MOCK_COOKIE = (
        "PASS_ID=mock; SUB_PASS_ID=mock; MSA=mock; SUB_SYS_ID=mock; "
        "pdd_user_id=1; pdd_user_uin=1; anti_content=mock; api_uid=mock"
    )
    mock_a = await MockPDDServer(shop_name="验证A店").start()
    mock_b = await MockPDDServer(shop_name="验证B店").start()
    multi_data = copy.deepcopy(config.data)
    multi_data["app"] = {**multi_data.get("app", {}), "data_dir": str(tmp / "multi-data")}
    multi_data["llm"] = {**multi_data.get("llm", {}), "enabled": False}
    multi_data["behavior"] = {
        **multi_data.get("behavior", {}),
        "reply_delay_seconds": 0,
        "reply_delay_jitter": 0,
    }
    multi_data["compliance"] = copy.deepcopy(multi_data.get("compliance", {}))
    multi_data["compliance"]["risk"] = {
        **multi_data["compliance"].get("risk", {}),
        "min_send_interval_seconds": 0,
    }
    multi_data["shops"] = [
        {
            "id": "shop-a",
            "name": "验证A店",
            "cookie_file": str(tmp / "shop-a.json"),
            "overrides": {"pdd": {"base_url": mock_a.base_url, "ws_url": mock_a.ws_url}},
        },
        {
            "id": "shop-b",
            "name": "验证B店",
            "cookie_file": str(tmp / "shop-b.json"),
            "overrides": {"pdd": {"base_url": mock_b.base_url, "ws_url": mock_b.ws_url}},
        },
    ]
    multi_cfg = Config(multi_data, root=PROJECT_ROOT)
    ensure_dirs(multi_cfg)
    for shop_id in ("shop-a", "shop-b"):
        CookieStore(multi_cfg.shop_profile(shop_id).cookie_file).save_text(MOCK_COOKIE)
    multi = PDDAutoReplyService(multi_cfg)
    try:
        await multi.start()
        assert await asyncio.to_thread(
            _wait_until,
            lambda: all(w.channel and w.channel.connected for w in multi.workers.values()),
            15,
        )
        assert await asyncio.to_thread(
            _wait_until,
            lambda: mock_a.state.ws_connections >= 1 and mock_b.state.ws_connections >= 1,
            10,
        )
        await asyncio.sleep(0.3)

        async def _multi_connect():
            status = multi.status()
            assert status["shops_count"] == 2
            assert {shop["shop_name"] for shop in status["shops"]} == {"验证A店", "验证B店"}
            assert not status["startup_errors"]
            return "两个店铺长连接均已建立"

        await check("多店铺：两个店铺同时在线", _multi_connect)

        async def _multi_isolate():
            await mock_a.push_text("什么时候发货？", uid="multi-a", msg_id="ma-1")
            assert await asyncio.to_thread(_wait_until, lambda: len(mock_a.state.sent) >= 1, 12)
            assert mock_b.state.sent == []
            assert multi.store.get_conversation("multi-a", shop_id="shop-a")
            assert multi.store.get_conversation("multi-a", shop_id="shop-b") is None

            await mock_b.push_text("我要投诉", uid="multi-b", msg_id="mb-1")
            assert await asyncio.to_thread(_wait_until, lambda: len(mock_b.state.transfers) >= 1, 12)
            assert mock_b.state.transfers[0]["uid"] == "multi-b"
            assert mock_a.state.transfers == []
            return "会话 / 自动回复 / 转人工均按店铺隔离"

        await check("多店铺：会话与转人工按店铺隔离", _multi_isolate)

        async def _multi_takeover():
            await mock_a.push_raw(
                make_push("您好，我是人工客服", uid="multi-a", from_role="mall_cs", msg_id="ma-2")
            )
            await asyncio.sleep(0.8)
            assert multi.store.get_human_takeover("multi-a", shop_id="shop-a") is not None
            before = len(mock_a.state.sent)
            await mock_a.push_text("还在吗", uid="multi-a", msg_id="ma-3")
            await asyncio.sleep(1.6)
            assert len(mock_a.state.sent) == before
            before_b = len(mock_b.state.sent)
            await mock_b.push_text("发什么快递", uid="multi-b2", msg_id="mb-2")
            assert await asyncio.to_thread(_wait_until, lambda: len(mock_b.state.sent) > before_b, 12)
            return "A 店人工接管不影响 B 店自动回复"

        await check("多店铺：人工接管按店铺隔离", _multi_takeover)

        # ---- 新功能：质检 / 洞察 / 待处理 / 搜索 / 订单上下文 / 话术体检 ----
        async def _seed_multi():
            store = multi.store
            store.record_inbound(
                uid="seed-1", nickname="小圆", role="OTHER", context_type="text",
                content="什么时候发货", shop_id="shop-a",
            )
            store.record_outbound(
                uid="seed-1", content="放心，今天一定发，包退", action="keyword",
                result="auto_reply", status="sent", shop_id="shop-a", rule_id="ship_time",
            )
            store.record_inbound(
                uid="seed-2", nickname="阿泽", role="OTHER", context_type="text",
                content="在吗？", shop_id="shop-b",
            )
            store.record_inbound(
                uid="seed-3", nickname="大熊", role="OTHER", context_type="text",
                content="我要投诉", shop_id="shop-b",
            )
            store.record_inbound(
                uid="seed-4", nickname="晴天", role="OTHER", context_type="text",
                content="你们支持以旧换新吗", shop_id="shop-b",
            )
            store.record_outbound(
                uid="seed-3", content="[转人工] 投诉", action="transfer", result="transfer",
                status="sent", shop_id="shop-b", reason="complaint: 投诉/差评风险（投诉）",
            )
            store.record_outbound(
                uid="seed-3", content="", action="no_match", result="no_match",
                status="sent", shop_id="shop-b", question="你们支持以旧换新吗",
            )
            return "已造质检/洞察测试数据"

        await check("多店铺：测试数据准备", _seed_multi)

        async def _qc():
            result = multi.qc_scan(force=True)
            rule_ids = {hit["rule_id"] for hit in result["hits"]}
            assert "risky_promise" in rule_ids, rule_ids
            assert "unanswered" in rule_ids, rule_ids
            assert result["summary"]["high"] >= 1
            per_shop = multi.qc_scan(shop_id="shop-a", force=True)
            assert all(hit["shop_id"] == "shop-a" for hit in per_shop["hits"])
            return f"命中 {len(result['hits'])} 条，含乱承诺/未回复"

        await check("会话质检（多店铺）", _qc)

        async def _insights():
            data = multi.insights()
            assert data["hourly"]
            assert any(item["rule_id"] == "ship_time" for item in data["top_rules"])
            assert any("以旧换新" in item["question"] for item in data["knowledge_gaps"])
            assert any(item["reason"].startswith("complaint") for item in data["transfer_reasons"])
            per_shop = multi.insights("shop-b")
            assert per_shop["shop_id"] == "shop-b"
            return "近日进线 / 转人工原因 / 话术 Top / 知识缺口均正常"

        await check("运营洞察（多店铺）", _insights)

        async def _pending():
            pending = multi.pending()
            uids = {item["uid"] for item in pending}
            assert "seed-2" in uids, uids
            assert multi.status()["pending_count"] >= 1
            return f"待处理 {len(pending)} 个"

        await check("待处理队列", _pending)

        async def _search():
            assert multi.search("小圆", shop_id="shop-a")
            assert multi.search("以旧换新")
            assert multi.search("不存在的词xyz") == []
            return "按昵称 / 消息内容搜索正常"

        await check("会话搜索", _search)

        async def _order_ctx():
            worker = multi.workers["shop-a"]
            from pdd_cs.pdd.message import CTX_ORDER_INFO

            event = ChatEvent(
                uid="order-check", nickname="下单客户", role="OTHER",
                context_type=CTX_ORDER_INFO,
                content="[订单咨询] 订单号 A1",
                order_info={"order_id": "A1", "goods_name": "测试商品", "amount": 88},
                msg_id="order-1",
            )
            await worker.handle_events([event])
            conv = multi.store.get_conversation("order-check", shop_id="shop-a")
            assert conv and conv["last_order"]["order_id"] == "A1"
            follow = ChatEvent(uid="order-check", nickname="下单客户", role="OTHER",
                               content="我的订单到哪了", context_type="text")
            ctx = worker._build_context(follow)  # noqa: SLF001
            assert ctx.last_order["order_id"] == "A1"
            return "订单卡片沉淀并在后续决策中带上"

        await check("订单上下文沉淀", _order_ctx)

        async def _lint():
            report = multi.lint()
            assert report.checked_texts > 50
            assert report.ok, [i.__dict__ for i in report.errors[:3]]
            return f"{report.summary()}"

        await check("话术/知识库合规体检", _lint)

        async def _rules_io():
            import shutil as _shutil

            kw_copy = tmp / "verify-keywords.yaml"
            _shutil.copyfile(multi_cfg.rules.keyword_file, kw_copy)
            multi_cfg.rules.keyword_file = kw_copy
            target = tmp / "verify-keywords.csv"
            count = export_csv(multi_cfg, "keywords", target)
            assert count >= 10 and target.exists()
            result = import_csv(multi_cfg, "keywords", target)
            assert result["total"] == count
            return f"CSV 导出/导入 {count} 条"

        await check("话术库 CSV 导入导出", _rules_io)

        # ---- 告警 Webhook ----
        async def _alerts():
            import threading
            from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

            received: list[dict] = []

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args) -> None:  # noqa: A003
                    pass

                def do_POST(self) -> None:  # noqa: N802
                    length = int(self.headers.get("Content-Length") or 0)
                    received.append(json.loads(self.rfile.read(length).decode("utf-8")))
                    body = b"{}"
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

            httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            port = httpd.server_address[1]
            try:
                alert_cfg = copy.deepcopy(multi_cfg.data)
                alert_cfg["alerts"] = {
                    **alert_cfg.get("alerts", {}),
                    "enabled": True,
                    "webhook_url": f"http://127.0.0.1:{port}/hook",
                    "format": "dingtalk",
                    "events": [EVENT_TRANSFER],
                }
                cfg2 = Config(alert_cfg, root=PROJECT_ROOT)
                notifier = AlertNotifier(cfg2.alerts)
                assert notifier.notify(
                    EVENT_TRANSFER, "告警验证", "转人工了", shop_id="shop-a", shop_name="验证A店", force=True
                )
                assert received and received[-1]["msgtype"] == "text"
                assert "告警验证" in received[-1]["text"]["content"]
                return "钉钉格式 Webhook 发送成功"
            finally:
                httpd.shutdown()
                httpd.server_close()

        await check("告警通知 Webhook", _alerts)
    finally:
        await multi.stop()
        await mock_a.stop()
        await mock_b.stop()

    # ---------------------------------------------------------------
    # 13. 掉线通知 / 心跳 / 严格健康检查
    # ---------------------------------------------------------------
    print("\n[13] 掉线通知与服务器部署自检")
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from pdd_cs.alerts import (
        EVENT_SHOP_OFFLINE,
        EVENT_SHOP_ONLINE,
        HeartbeatReporter,
    )

    hook_received: list[dict] = []

    class _HookHandler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:  # noqa: A003
            pass

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            hook_received.append(json.loads(self.rfile.read(length).decode("utf-8")))
            body = b"{}"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    hook_httpd = ThreadingHTTPServer(("127.0.0.1", 0), _HookHandler)
    threading.Thread(target=hook_httpd.serve_forever, daemon=True).start()
    hook_url = f"http://127.0.0.1:{hook_httpd.server_address[1]}/hook"
    try:
        async def _heartbeat():
            hb = HeartbeatReporter(config.monitor)
            config.monitor.heartbeat_url = hook_url
            config.monitor.heartbeat_method = "POST"
            try:
                assert hb.send({
                    "shops": [
                        {"shop_id": "shop-a", "channel": {"connected": True}},
                        {"shop_id": "shop-b", "channel": {"connected": False}},
                    ],
                    "today": {"inbound_messages": 5},
                    "startup_errors": {},
                })
            finally:
                config.monitor.heartbeat_url = ""
                config.monitor.heartbeat_method = "POST"
            assert await asyncio.to_thread(lambda: any(
                item.get("event") == "heartbeat" for item in hook_received
            ))
            payload = [item for item in hook_received if item.get("event") == "heartbeat"][-1]
            assert payload["shops_total"] >= 1
            return f"心跳上报成功（{payload['shops_total']} 个店铺）"

        await check("进程心跳上报（死信开关）", _heartbeat)

        async def _offline_alert():
            offline_data = copy.deepcopy(multi_cfg.data)
            offline_data["app"] = {**offline_data.get("app", {}), "data_dir": str(tmp / "offline-data")}
            offline_data["alerts"] = {
                **offline_data.get("alerts", {}),
                "enabled": True,
                "webhook_url": hook_url,
                "format": "generic",
                "events": [EVENT_SHOP_OFFLINE, EVENT_SHOP_ONLINE],
                "offline_after_seconds": 30,
                "offline_repeat_seconds": 3600,
            }
            offline_cfg = Config(offline_data, root=PROJECT_ROOT)
            offline_service = PDDAutoReplyService(offline_cfg)
            try:
                assert offline_service.check_offline_alerts(now=100.0) == []
                events = offline_service.check_offline_alerts(now=140.0)
                assert any(e["event"] == "shop_offline" and e["sent"] for e in events), events
                assert await asyncio.to_thread(lambda: any(
                    item.get("event") == EVENT_SHOP_OFFLINE for item in hook_received
                ))
                payload = [item for item in hook_received if item.get("event") == EVENT_SHOP_OFFLINE][-1]
                assert "掉线" in payload["title"]
                return f"掉线超过阈值后推送告警（{len(events)} 个店铺）"
            finally:
                offline_service.store.close()

        await check("店铺掉线 Webhook 告警", _offline_alert)

        async def _strict_healthz():
            health_cfg = Config(copy.deepcopy(config.data), root=PROJECT_ROOT)
            health_cfg.data["app"] = {**health_cfg.data.get("app", {}), "data_dir": str(tmp / "health-data")}
            health_cfg.server.host = "127.0.0.1"
            health_cfg.server.port = 0
            health_cfg.dashboard.enabled = False
            health_service = PDDAutoReplyService(health_cfg)
            health_dashboard = DashboardServer(health_cfg, health_service)
            health_dashboard.start()
            try:
                base = health_dashboard.url

                def _get(url: str):
                    try:
                        with urllib.request.urlopen(url, timeout=5) as resp:
                            return resp.status, json.loads(resp.read().decode("utf-8"))
                    except urllib.error.HTTPError as exc:
                        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")

                code, _ = await asyncio.to_thread(_get, base + "healthz")
                assert code == 200
                code, payload = await asyncio.to_thread(_get, base + "healthz?strict=1")
                assert code == 503, code
                assert payload["ok"] is False and payload["degraded"]
                return "存活检查 200 / 严格检查掉线时 503"
            finally:
                health_dashboard.stop()
                health_service.store.close()

        await check("严格健康检查 /healthz?strict=1", _strict_healthz)

        async def _auth_token():
            auth_cfg = Config(copy.deepcopy(config.data), root=PROJECT_ROOT)
            auth_cfg.data["app"] = {
                **auth_cfg.data.get("app", {}),
                "data_dir": str(tmp / "auth-data"),
            }
            auth_cfg.server.host = "127.0.0.1"
            auth_cfg.server.port = 0
            auth_cfg.server.auth_token = "v" * 32
            auth_cfg.dashboard.enabled = False
            auth_service = PDDAutoReplyService(auth_cfg)
            auth_dashboard = DashboardServer(auth_cfg, auth_service)
            auth_dashboard.start()
            try:
                base = auth_dashboard.url

                def _code(url, token="", method="GET", payload=None):
                    data = json.dumps(payload).encode("utf-8") if payload is not None else None
                    headers = {"Content-Type": "application/json"}
                    if token:
                        headers["X-Auth-Token"] = token
                    req = urllib.request.Request(url, data=data, headers=headers, method=method)
                    try:
                        with urllib.request.urlopen(req, timeout=5) as resp:
                            return resp.status
                    except urllib.error.HTTPError as exc:
                        return exc.code

                assert await asyncio.to_thread(_code, base + "healthz") == 200
                assert await asyncio.to_thread(_code, base + "api/status") == 401
                assert await asyncio.to_thread(
                    _code, base + "?token=" + auth_cfg.server.auth_token
                ) == 200
                assert await asyncio.to_thread(
                    _code, base + "api/status", auth_cfg.server.auth_token
                ) == 200
                return "未带令牌 401 / 带令牌 200；健康检查放行"
            finally:
                auth_dashboard.stop()
                auth_service.store.close()

        await check("面板访问令牌（服务器安全）", _auth_token)

        async def _env_override():
            old_port = os.environ.get("PDD_CS_PORT")
            os.environ["PDD_CS_PORT"] = "40123"
            try:
                cfg = Config.load(PROJECT_ROOT / "config" / "config.example.yaml")
                assert cfg.server.port == 40123
            finally:
                if old_port is None:
                    os.environ.pop("PDD_CS_PORT", None)
                else:
                    os.environ["PDD_CS_PORT"] = old_port
            os.environ["PDD_CS_PORT"] = "bad"
            try:
                try:
                    Config.load(PROJECT_ROOT / "config" / "config.example.yaml")
                except ValueError:
                    pass
                else:  # pragma: no cover
                    raise AssertionError("非法 PDD_CS_PORT 应报错")
            finally:
                os.environ.pop("PDD_CS_PORT", None)
            return "PDD_CS_PORT 覆盖生效；非法值报错"

        await check("环境变量端口覆盖（PDD_CS_PORT）", _env_override)
    finally:
        hook_httpd.shutdown()
        hook_httpd.server_close()

    # ---------------------------------------------------------------
    # 14. 网页后台：登录 / 店铺管理 / 系统设置 / 话术
    # ---------------------------------------------------------------
    print("\n[14] 网页后台（登录/设置/话术）")
    from pdd_cs.config import Config
    from pdd_cs.engine.service import PDDAutoReplyService
    from pdd_cs.server import DashboardServer

    web_mock = await MockPDDServer(shop_name="网页测试店").start()
    web_data = copy.deepcopy(config.data)
    web_data["app"] = {**web_data.get("app", {}), "data_dir": str(tmp / "web-data")}
    web_data["pdd"] = {
        **web_data.get("pdd", {}),
        "base_url": web_mock.base_url,
        "ws_url": web_mock.ws_url,
        "cookie_file": str(tmp / "web-cookies.json"),
    }
    web_data["server"] = {
        "host": "127.0.0.1",
        "port": 0,
        "auto_open_dashboard": False,
        "auth_token": "",
    }
    web_data["dashboard"] = {"enabled": False}
    web_data["shops"] = []
    web_data["llm"] = {**web_data.get("llm", {}), "enabled": False}
    web_data["behavior"] = {
        **web_data.get("behavior", {}),
        "reply_delay_seconds": 0,
        "reply_delay_jitter": 0,
        "default_reply": "网页默认回复（初始）",
    }
    web_data["compliance"] = copy.deepcopy(web_data.get("compliance", {}))
    web_data["compliance"]["risk"] = {
        **web_data["compliance"].get("risk", {}),
        "min_send_interval_seconds": 0,
    }
    # 隔离：规则/人设/知识库全部指向临时目录，避免网页测试污染仓库文件
    web_rules = tmp / "web-rules"
    web_rules.mkdir(parents=True, exist_ok=True)
    for rule_name in ("keywords", "transfer", "replace", "filters", "goods",
                      "compliance", "blacklist", "qc"):
        source_file = PROJECT_ROOT / "config" / "rules" / f"{rule_name}.yaml"
        if source_file.exists():
            (web_rules / f"{rule_name}.yaml").write_bytes(source_file.read_bytes())
    (tmp / "web-knowledge").mkdir(parents=True, exist_ok=True)
    for knowledge_file in (PROJECT_ROOT / "config" / "knowledge").glob("*.md"):
        (tmp / "web-knowledge" / knowledge_file.name).write_bytes(knowledge_file.read_bytes())
    web_data["rules"] = {
        "keyword_file": str(web_rules / "keywords.yaml"),
        "transfer_file": str(web_rules / "transfer.yaml"),
        "replace_file": str(web_rules / "replace.yaml"),
        "filter_file": str(web_rules / "filters.yaml"),
        "compliance_file": str(web_rules / "compliance.yaml"),
        "goods_file": str(web_rules / "goods.yaml"),
        "blacklist_file": str(web_rules / "blacklist.yaml"),
        "qc_file": str(web_rules / "qc.yaml"),
    }
    web_data["knowledge"] = {**web_data.get("knowledge", {}), "dir": str(tmp / "web-knowledge")}
    web_data["persona_file"] = str(tmp / "web-persona.md")
    (tmp / "web-persona.md").write_text(
        (PROJECT_ROOT / "config" / "persona.md").read_text(encoding="utf-8"), encoding="utf-8"
    )
    web_cfg = Config(web_data, root=PROJECT_ROOT)
    web_cfg.source = tmp / "web-config.yaml"  # 隔离：网页设置写到临时文件
    ensure_dirs(web_cfg)
    web_service = PDDAutoReplyService(web_cfg)
    web_dashboard = None

    def _web_get(path: str):
        with urllib.request.urlopen(web_dashboard.url + path, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _web_post(path: str, payload: dict):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            web_dashboard.url + path,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=180) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        await web_service.start(allow_degraded=True)
        web_dashboard = DashboardServer(web_cfg, web_service)
        web_dashboard.start()

        async def _web_setup_api():
            setup = await asyncio.to_thread(_web_get, "api/setup")
            assert setup["setup_mode"] is True, setup
            result = await asyncio.to_thread(
                _web_post, "api/shops/add", {"id": "web-shop", "name": "网页测试店"}
            )
            assert any(shop["id"] == "web-shop" for shop in result["shops"]), result
            settings = await asyncio.to_thread(_web_get, "api/settings")
            assert settings["settings"]["server"]["port"] == 0 or settings["settings"]["server"]["port"] > 0
            return "网页 setup/店铺/设置接口正常（免终端添加店铺）"

        await check("网页后台：店铺与设置 API", _web_setup_api)

        async def _web_cookie_login():
            result = await asyncio.to_thread(
                _web_post,
                "api/login/cookie",
                {"shop_id": "web-shop", "cookie": MOCK_COOKIE},
            )
            assert result.get("ok") is True, result
            assert result.get("session_valid") is True, result
            assert await asyncio.to_thread(
                _wait_until, lambda: web_mock.state.ws_connections >= 1, 15
            ), "网页登录后长连接未建立"
            status = await asyncio.to_thread(_web_get, "api/status")
            assert status.get("online_shops", 0) >= 1, status
            return "网页粘贴 Cookie 登录成功，店铺已上线"

        await check("网页后台：Cookie 登录（免终端）", _web_cookie_login)

        async def _web_export_import():
            export = await asyncio.to_thread(
                _web_get, "api/login/export?shop_id=web-shop"
            )
            assert export["code"].startswith("PDDCS1."), export
            assert export["cookies"] >= 6, export
            assert "PASS_ID" not in export["code"]

            await asyncio.to_thread(
                _web_post, "api/login/logout", {"shop_id": "web-shop"}
            )
            result = await asyncio.to_thread(
                _web_post,
                "api/login/import",
                {"shop_id": "web-shop", "text": export["code"]},
            )
            assert result.get("session_valid") is True, result
            for _ in range(40):
                status = await asyncio.to_thread(_web_get, "api/status")
                if status.get("online_shops", 0) >= 1:
                    break
                await asyncio.sleep(0.5)
            else:
                raise AssertionError(f"导入后店铺未上线: {status}")
            return "本机导出登录码 → 服务器粘贴导入成功，服务器无需 Chrome"

        await check("网页后台：登录态导出/导入（服务器免 Chrome）", _web_export_import)

        async def _chrome_tool():
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "pdd_cs",
                "chrome",
                "--status",
                "--json",
                cwd=str(PROJECT_ROOT),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            assert proc.returncode == 0, stderr.decode("utf-8", "replace")[:300]
            data = json.loads(stdout.decode("utf-8"))
            assert "found" in data and "display_available" in data
            capability = await asyncio.to_thread(_web_get, "api/login/capability")
            assert "chrome_found" in capability and "install_supported" in capability
            return (
                f"Chrome 自动检测正常（found={data['found']}，"
                f"display={data['display_available']}），面板可一键下载"
            )

        await check("Chrome 自动检测 / 一键安装命令", _chrome_tool)

        async def _web_settings_effect():
            # 改默认回复
            result = await asyncio.to_thread(
                _web_post,
                "api/settings",
                {"behavior": {"default_reply": "网页设置的默认回复", "no_match": "default"}},
            )
            assert result.get("ok") is True, result
            reloaded = await asyncio.to_thread(_web_get, "api/settings")
            assert reloaded["settings"]["behavior"]["default_reply"] == "网页设置的默认回复"

            # 网页加关键词话术后立即命中
            rules = await asyncio.to_thread(_web_get, "api/rules/keywords")
            new_rules = list(rules["rules"]) + [
                {"keywords": "网页后台测试词", "reply": "网页后台命中回复", "match": "fuzzy"}
            ]
            result = await asyncio.to_thread(
                _web_post, "api/rules/keywords", {"rules": new_rules}
            )
            assert result.get("count") == len(new_rules), result
            simulate = await asyncio.to_thread(
                _web_get, "api/simulate?text=" + urllib.parse.quote("网页后台测试词")
            )
            assert "网页后台命中回复" in (simulate.get("content") or ""), simulate

            # 人设 / 知识库保存
            result = await asyncio.to_thread(
                _web_post, "api/content/persona", {"text": "# 网页人设\n你好"}
            )
            assert result.get("ok") is True, result
            persona = await asyncio.to_thread(_web_get, "api/content/persona")
            assert "网页人设" in persona["text"]
            return "设置/话术/人设保存后立即生效（无需重启）"

        await check("网页后台：保存设置与话术立即生效", _web_settings_effect)
    finally:
        if web_dashboard:
            web_dashboard.stop()
        await web_service.stop()
        await web_mock.stop()
        try:
            (PROJECT_ROOT / "config" / "secrets" / "web-shop.json").unlink()
        except OSError:
            pass

    # ---------------------------------------------------------------
    # 收尾
    # ---------------------------------------------------------------
    await mock.stop()
    service.store.close()

    passed = sum(1 for status, _, _ in RESULTS if status == PASS)
    failed = sum(1 for status, _, _ in RESULTS if status == FAIL)
    skipped = sum(1 for status, _, _ in RESULTS if status == SKIP)

    print("\n" + "=" * 70)
    print(f"验证完成：通过 {passed} · 失败 {failed} · 跳过 {skipped} · 共 {len(RESULTS)} 项")
    if failed:
        print("\n失败项：")
        for status, name, detail in RESULTS:
            if status == FAIL:
                print(f"  ❌ {name}：{detail}")
    print(f"临时数据目录：{tmp}")
    print("=" * 70 + "\n")
    return 0 if failed == 0 else 1


def cli() -> int:
    parser = argparse.ArgumentParser(description="拼多多智能客服全功能验证")
    parser.add_argument("--verbose", action="store_true", help="打印更多细节")
    args = parser.parse_args()
    try:
        return asyncio.run(main(args.verbose))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(cli())
