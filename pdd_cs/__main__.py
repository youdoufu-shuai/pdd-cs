# -*- coding: utf-8 -*-
"""命令行入口。

    python -m pdd_cs init                  初始化配置
    python -m pdd_cs login [--shop ID]     扫码登录并保存 Cookie（多店铺逐个登录）
    python -m pdd_cs run                   启动自动客服（多店铺长连接 + 面板）
    python -m pdd_cs doctor                环境自检（逐店铺检查登录态）
    python -m pdd_cs simulate [...]        决策测试台
    python -m pdd_cs stats [--shop ID]     运营统计
    python -m pdd_cs export                导出聊天记录
    python -m pdd_cs lint [--strict]       话术/知识库合规体检
    python -m pdd_cs qc [--days N]         会话质检（事后抽检）
    python -m pdd_cs alert-test            测试告警 Webhook
    python -m pdd_cs rules export/import   话术库 CSV 导入导出
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import logging.handlers
import os
import shutil
import signal
import socket
import sys
import time
import webbrowser
from pathlib import Path

from . import __version__
from .alerts import EVENT_STARTUP, AlertNotifier
from .config import EXAMPLE_CONFIG_PATH, PROJECT_ROOT, Config, ShopProfile, ensure_dirs
from .engine.service import PDDAutoReplyService
from .engine.qc import QCScanner
from .pdd.api import PDDClient, PDDSessionExpired
from .pdd.auth import CookieBundle, CookieError, CookieStore, cookie_age_hours
from .pdd.cdp_login import LoginError, login_with_browser, save_cookie_text
from .rules_io import RULE_TYPES, export_csv, import_csv
from .server import DashboardServer
from .utils import now_beijing, truncate

logger = logging.getLogger("pdd_cs")

BANNER = r"""
  ___  ___  ___        ___ ___
  | _ \|   \|   \  ___ / __/ __|   拼多多智能客服 · 多店铺版
  |  _/| |) | |) |/ _ \\__ \__ \
  |_|  |___/|___/ \___/|___/___/   v{version}
""".format(version=__version__)


# ---------------------------------------------------------------------------
# 公共
# ---------------------------------------------------------------------------
def _setup_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass


def setup_logging(config: Config, *, verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else getattr(logging, config.log_level, logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(name)s | %(message)s", "%H:%M:%S"))
    root.addHandler(console)
    try:
        log_dir = config.data_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / "pdd_cs.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-5s %(name)s | %(message)s")
        )
        root.addHandler(file_handler)
    except OSError:
        pass
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _load_config(args) -> Config:
    try:
        config = Config.load(getattr(args, "config", None))
    except ValueError as exc:
        raise SystemExit(f"❌ 配置错误：{exc}") from exc
    if not config.source:
        print("⚠️  未找到 config/config.yaml，正在使用默认配置。建议先运行：python -m pdd_cs init")
    return config


def _print_config_problems(config: Config) -> None:
    for problem in config.validate():
        print(f"⚠️  {problem}")


def _resolve_shop(config: Config, shop_id: str | None) -> ShopProfile:
    if shop_id:
        try:
            return config.shop_profile(shop_id)
        except KeyError as exc:
            names = "、".join(p.id for p in config.shops)
            raise SystemExit(f"未找到店铺 {shop_id}；可用店铺：{names}") from exc
    if len(config.shops) == 1:
        return config.shops[0]
    names = "、".join(f"{p.id}({p.display_name})" for p in config.shops)
    raise SystemExit(f"当前配置了多个店铺，请用 --shop 指定：{names}")


def _shop_table(config: Config) -> str:
    lines = []
    for profile in config.shops:
        status = "启用" if profile.enabled else "停用"
        exists = "已保存" if profile.cookie_file.exists() else "未登录"
        lines.append(f"  · {profile.id:<10} {profile.display_name:<14} {status}  Cookie: {exists}  ({profile.cookie_file})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# init / login
# ---------------------------------------------------------------------------
def cmd_init(args) -> int:
    print(BANNER)
    ensure_dirs(Config.load())
    target = PROJECT_ROOT / "config" / "config.yaml"
    if target.exists() and not args.force:
        print(f"配置已存在：{target}")
    else:
        shutil.copyfile(EXAMPLE_CONFIG_PATH, target)
        print(f"✅ 已生成配置：{target}")
    (PROJECT_ROOT / "config" / "secrets").mkdir(parents=True, exist_ok=True)
    print("\n下一步：")
    print("  1. 编辑 config/config.yaml：店铺列表 shops、LLM Key、告警 Webhook")
    print("  2. 编辑 config/persona.md、config/knowledge/*.md、config/rules/*.yaml")
    print("  3. 逐个店铺登录：python -m pdd_cs login --shop <店铺id>")
    print("  4. 运行：python -m pdd_cs run")
    print("\n多店铺配置示例（config/config.yaml）：")
    print("  shops:")
    print("    - id: shop-a")
    print("      name: A店（女装）")
    print("      cookie_file: config/secrets/shop-a.json")
    return 0


def cmd_login(args) -> int:
    print(BANNER)
    config = _load_config(args)

    if len(config.shops) > 1 and not args.shop and not args.cookie and not args.cookie_file:
        print("检测到多个店铺，请用 --shop 指定要登录的店铺：\n" + _shop_table(config))
        return 2
    profile = _resolve_shop(config, args.shop)
    print(f"店铺：{profile.display_name}（{profile.id}）")
    store = CookieStore(profile.cookie_file)

    try:
        if args.cookie:
            save_cookie_text(store, args.cookie, shop_name=profile.display_name)
            print("✅ Cookie 已保存")
        elif args.cookie_file:
            text = Path(args.cookie_file).read_text(encoding="utf-8")
            save_cookie_text(store, text, shop_name=profile.display_name)
            print("✅ Cookie 已保存")
        else:
            login_with_browser(
                store,
                chrome_path=args.chrome,
                timeout_seconds=args.timeout,
                keep_open=args.keep_open,
                user_data_dir=PROJECT_ROOT / "data" / "chrome-profile",
            )
    except LoginError as exc:
        print(f"❌ 登录失败：{exc}")
        return 1
    except CookieError as exc:
        print(f"❌ Cookie 无效：{exc}")
        return 1
    except KeyboardInterrupt:
        print("\n已取消登录")
        return 130

    bundle = store.load()
    if not bundle:
        return 1
    shop_cfg = config.for_shop(profile)
    api = PDDClient(
        bundle.cookies,
        base_url=shop_cfg.pdd.base_url,
        user_agent=shop_cfg.pdd.user_agent,
        timeout=shop_cfg.pdd.request_timeout_seconds,
        max_retries=1,
    )
    try:
        info = api.get_user_info()
        shop_name = info.get("mall_name") or info.get("mallName") or info.get("username") or ""
        print(f"✅ 登录态校验通过{'：' + str(shop_name) if shop_name else ''}")
        if shop_name:
            store.save(
                CookieBundle(
                    cookies=bundle.cookies, saved_at=bundle.saved_at, shop_name=str(shop_name)
                )
            )
    except PDDSessionExpired:
        print("❌ 登录态校验失败：Cookie 已过期，请重新登录")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️  登录态校验请求失败（网络问题可稍后重试）：{exc}")
    return 0


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------
async def _run_service(config: Config, args) -> int:
    service = PDDAutoReplyService(config)
    dashboard: DashboardServer | None = None
    try:
        await service.start()
    except RuntimeError as exc:
        print(f"\n❌ 启动失败：{exc}")
        print("\n店铺登录状态：\n" + _shop_table(config))
        await service.stop()
        return 1

    if config.dashboard.enabled and not args.no_dashboard:
        dashboard = DashboardServer(config, service)
        try:
            dashboard.start()
        except RuntimeError as exc:
            print(f"❌ 监控面板未启动：{exc}")
            dashboard = None
        except OSError as exc:
            print(
                f"⚠️  监控面板启动失败：{config.server.host}:{config.server.port} 不可用（{exc}）\n"
                f"   换个端口即可，例如：PDD_CS_PORT=38618 python -m pdd_cs run"
                f"（或在 config/config.yaml 改 server.port）"
            )
            dashboard = None

    status = service.status()
    print(BANNER)
    print(f"店铺（{status['shops_count']} 个）：")
    for shop in status["shops"]:
        state = shop["channel"].get("state", shop["state"])
        print(f"  · {shop['shop_name']:<14} 连接={state:<10} 进线={shop['today']['inbound_messages']:<4} 自动回复={shop['today']['auto_replies']}")
    if status.get("startup_errors"):
        print("\n⚠️  以下店铺启动失败（其它店铺继续运行）：")
        for shop_id, error in status["startup_errors"].items():
            print(f"  · {shop_id}: {error}")
    print(f"\nAI：{'已启用 ' + config.llm.model if config.llm.ready else '未启用（仅关键词/默认回复）'}")
    print(f"规则：{service.rules.stats()}")
    print(f"质检：{len(service.qc.rules)} 条规则 · 告警：{'已开启 ' + config.alerts.format if service.notifier.enabled else '未开启'}")
    token = (config.server.auth_token or "").strip()
    if dashboard:
        print(f"监控面板：{dashboard.url}")
        if token:
            print("访问令牌：已启用（地址后加 ?token=<server.auth_token>，首次访问后会记住）")
        if config.server.host not in {"127.0.0.1", "localhost", "::1", "[::1]"}:
            print(f"⚠️  面板绑定在 {config.server.host}，请确认已设置访问令牌与防火墙白名单")
        if config.server.auto_open_dashboard:
            try:
                webbrowser.open(dashboard.url)
            except Exception:  # noqa: BLE001
                pass
    else:
        print("监控面板：未启用")
    print("\n按 Ctrl+C 停止\n")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        while not stop_event.is_set():
            await asyncio.sleep(1)
            for shop in service.shops_summary():
                overdue = shop.get("overdue") or []
                if overdue:
                    names = ", ".join((item.get("nickname") or item.get("uid")) for item in overdue[:5])
                    logger.warning("[%s] 有 %d 个会话等待超时（%s）", shop["shop_name"], len(overdue), names)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        print("\n正在停止…")
        if dashboard:
            dashboard.stop()
        await service.stop()
        print("已停止")
    return 0


def cmd_run(args) -> int:
    config = _load_config(args)
    if getattr(args, "host", None):
        config.server.host = args.host
    if getattr(args, "port", None):
        config.server.port = int(args.port)
        if not (1 <= config.server.port <= 65535):
            raise SystemExit(f"❌ --port 非法：{config.server.port}")
    setup_logging(config, verbose=args.verbose)
    _print_config_problems(config)
    print(BANNER)
    try:
        return asyncio.run(_run_service(config, args))
    except KeyboardInterrupt:
        print("\n已停止")
        return 0


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------
def cmd_doctor(args) -> int:
    config = _load_config(args)
    print(BANNER)
    print("环境自检\n" + "=" * 72)
    ok = True

    def line(status: str, name: str, detail: str = "") -> None:
        nonlocal ok
        if status == "❌":
            ok = False
        print(f"  {status}  {name}" + (f"：{detail}" if detail else ""))

    line("✅" if sys.version_info >= (3, 10) else "❌", "Python", sys.version.split()[0])
    for module in ("websockets", "yaml"):
        try:
            __import__(module)
            line("✅", f"依赖 {module}", "已安装")
        except ImportError:
            line("❌", f"依赖 {module}", "未安装，请运行 pip install -r requirements.txt")

    line("✅" if config.source else "⚠️", "配置文件", str(config.source) if config.source else "未找到（使用默认配置）")
    for problem in config.validate():
        if "api_key" in problem:
            line("⚠️", "LLM", "未配置 API Key，将只使用关键词/默认回复")
        else:
            line("⚠️", "配置", problem)

    # 逐店铺检查登录态
    print("\n  店铺登录态：")
    for profile in config.shops:
        tag = f"[{profile.id}]"
        store = CookieStore(profile.cookie_file)
        try:
            bundle = store.load()
        except CookieError as exc:
            line("❌", f"{tag} Cookie", str(exc))
            continue
        if not bundle or not bundle.cookies:
            line("❌", f"{tag} Cookie", "尚未登录")
            ok = False
            continue
        age = cookie_age_hours(bundle.saved_at)
        age_text = f"{age:.1f} 小时前保存" if age is not None else "保存时间未知"
        line("✅", f"{tag} Cookie", f"{len(bundle.cookies)} 项，{age_text}")
        shop_cfg = config.for_shop(profile)
        api = PDDClient(
            bundle.cookies,
            base_url=shop_cfg.pdd.base_url,
            user_agent=shop_cfg.pdd.user_agent,
            timeout=shop_cfg.pdd.request_timeout_seconds,
            max_retries=1,
        )
        try:
            info = api.get_user_info()
            name = info.get("username") or info.get("mall_name") or ""
            line("✅", f"{tag} 登录态", "有效" + (f"（{name}）" if name else ""))
        except PDDSessionExpired:
            line("❌", f"{tag} 登录态", "已过期，请重新登录")
            ok = False
        except Exception as exc:  # noqa: BLE001
            line("⚠️", f"{tag} 登录态", f"接口请求失败（可能网络受限）：{exc}")

    # 规则 / 知识库 / 质检 / 告警
    try:
        service = PDDAutoReplyService(config)
        line("✅", "规则", str(service.rules.stats()))
        if service.knowledge:
            line("✅", "知识库", str(service.knowledge.stats()))
        else:
            line("⚠️", "知识库", "未启用")
        line("✅" if service.qc.rules else "⚠️", "质检规则", f"{len(service.qc.rules)} 条")
        line("✅" if service.notifier.enabled else "⚠️", "告警通知", "已启用" if service.notifier.enabled else "未配置 Webhook")
        line(
            "✅" if service.heartbeat.enabled else "⚠️",
            "进程心跳",
            "已配置" if service.heartbeat.enabled else "未配置（进程崩溃/服务器宕机时无法通知，见 docs/deployment.md）",
        )
        line("✅" if service.ai.ready else "⚠️", "AI 回复", "可用" if service.ai.ready else "未启用")
        report = service.lint()
        line("✅" if report.ok else "❌", "话术体检", f"{len(report.errors)} 个错误 / {len(report.warnings)} 个提醒")
        ok = ok and report.ok
        service.store.close()
    except Exception as exc:  # noqa: BLE001
        line("❌", "内部组件", str(exc))

    line(
        "✅" if config.server.auth_token else "⚠️",
        "面板访问令牌",
        "已设置" if config.server.auth_token else "未设置（仅本机访问时可不设；绑 0.0.0.0 必填）",
    )
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((config.server.host, config.server.port))
        line("✅", "面板端口", f"{config.server.host}:{config.server.port} 可用")
    except OSError as exc:
        line(
            "⚠️",
            "面板端口",
            f"{config.server.host}:{config.server.port} 不可用（{exc}）；"
            f"可用 PDD_CS_PORT=38618 或 config server.port 更换",
        )

    print("=" * 72)
    print("结论：" + ("全部通过，可以运行 python -m pdd_cs run" if ok else "存在需要处理的问题，见上方 ❌"))
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# simulate / stats / export
# ---------------------------------------------------------------------------
def cmd_simulate(args) -> int:
    config = _load_config(args)
    config.dashboard.enabled = False
    service = PDDAutoReplyService(config)
    texts = args.text or ["什么时候发货？", "我要退货", "能便宜点吗", "你好"]
    shop_name = "(默认店铺)"
    if args.shop:
        try:
            shop_name = config.shop_profile(args.shop).display_name
        except KeyError:
            print(f"未找到店铺 {args.shop}")
            service.store.close()
            return 2
    elif len(config.shops) > 1:
        print(f"多店铺模式，使用第一个店铺；可用 --shop 指定：{'、'.join(p.id for p in config.shops)}")
    print(BANNER)
    print(f"决策测试台 · 店铺：{shop_name}（AI：{'启用' if service.ai.ready else '未启用'}）\n" + "=" * 72)
    for text in texts:
        result = service.simulate(text, shop_id=args.shop)
        print(f"\n客户：{text}")
        print(f"  → 动作：{result.get('action')}  结果：{result.get('log_result')}")
        print(f"  → 原因：{result.get('reason')}")
        if result.get("content"):
            print(f"  → 回复：{truncate(result['content'], 200)}")
        if result.get("violations"):
            print(f"  → 合规：{', '.join(result['violations'])}")
        if result.get("handoff"):
            print(f"  → 转人工：{result['handoff']}")
    print("=" * 72)
    service.store.close()
    return 0


def cmd_stats(args) -> int:
    config = _load_config(args)
    service = PDDAutoReplyService(config)
    store = service.store
    print(BANNER)
    shop_id = args.shop
    label = shop_id or "全部店铺"
    print(f"今日统计 · {label}\n" + "=" * 72)
    stats = store.today_stats(shop_id)
    for key, value in stats.items():
        print(f"  {key:22s} {value}")
    print("\n各店铺今日\n" + "=" * 72)
    if len(config.shops) > 1:
        for profile in config.shops:
            s = store.today_stats(profile.id)
            print(
                f"  {profile.display_name:<14} 进线={s['inbound_messages']:<5} 自动回复={s['auto_replies']:<5}"
                f" 转人工={s['transfers']:<4} 风控={s['risk_events']}"
            )
    else:
        print("  （单店铺模式）")
    print("\n待处理队列\n" + "=" * 72)
    pending = service.pending(shop_id)
    if not pending:
        print("  （无）")
    for item in pending[:10]:
        print(
            f"  [{item.get('shop_name') or item.get('shop_id')}] {item.get('nickname') or item.get('uid')}"
            f"  等待={item.get('waiting_seconds')}s  结果={item.get('last_result')}"
        )
    print("\n最近风控日志\n" + "=" * 72)
    logs = store.list_risk_logs(10, shop_id=shop_id)
    if not logs:
        print("  （无）")
    for item in logs:
        print(f"  {item['created_at']}  [{item.get('shop_id')}] {item['risk_type']:16s} {truncate(item['detail'] or '', 70)}")
    service.store.close()
    return 0


def cmd_export(args) -> int:
    config = _load_config(args)
    service = PDDAutoReplyService(config)
    target = Path(args.output) if args.output else (
        config.data_dir / f"export-{now_beijing().strftime('%Y%m%d-%H%M%S')}.json"
    )
    count = service.store.export(target, days=args.days, shop_id=args.shop)
    service.store.close()
    print(f"✅ 已导出 {count} 条消息到：{target}")
    return 0


# ---------------------------------------------------------------------------
# lint / qc / alert-test
# ---------------------------------------------------------------------------
def cmd_lint(args) -> int:
    config = _load_config(args)
    service = PDDAutoReplyService(config)
    report = service.lint()
    service.store.close()
    print(BANNER)
    print(f"话术与知识库合规体检\n" + "=" * 72)
    print(f"  检查文件 {report.checked_files} 个，文本 {report.checked_texts} 段")
    if not report.issues:
        print("  ✅ 未发现平台规则风险")
    for issue in report.issues[:50]:
        mark = "❌" if issue.level == "error" else "⚠️"
        print(f"  {mark} [{issue.source}] {issue.category}: {issue.matched}")
        print(f"       {truncate(issue.text, 100)}")
    if len(report.issues) > 50:
        print(f"  ... 另有 {len(report.issues) - 50} 条未显示")
    print("=" * 72)
    print(f"结论：{'✅ 通过' if report.ok else '❌ 存在高危话术，建议修改后再上线'}")
    if args.strict and not report.ok:
        return 1
    return 0


def cmd_qc(args) -> int:
    config = _load_config(args)
    service = PDDAutoReplyService(config)
    result = service.qc_scan(shop_id=args.shop, days=args.days, force=True)
    summary = result.get("summary", {})
    print(BANNER)
    print(f"会话质检 · 最近 {result.get('days')} 天 · {args.shop or '全部店铺'}\n" + "=" * 72)
    print(
        f"  扫描 {result.get('scanned_conversations')} 个会话 / {result.get('scanned_messages')} 条消息"
        f"  → 高 {summary.get('high',0)} · 中 {summary.get('medium',0)} · 低 {summary.get('low',0)}"
    )
    for hit in result.get("hits", [])[:30]:
        mark = {"high": "❌", "medium": "⚠️", "low": "·"}.get(hit["severity"], "·")
        print(
            f"  {mark} [{hit['shop_id']}] {hit['rule_name']}: {truncate(hit.get('snippet') or hit.get('detail') or '', 70)}"
            f"  ({hit.get('created_at') or hit.get('uid')})"
        )
    if len(result.get("hits", [])) > 30:
        print(f"  ... 另有 {len(result['hits']) - 30} 条未显示")
    if args.output:
        target = Path(args.output)
        target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n✅ 质检报告已写入：{target}")
    print("=" * 72)
    service.store.close()
    return 0


def cmd_alert_test(args) -> int:
    config = _load_config(args)
    notifier = AlertNotifier(config.alerts)
    print(BANNER)
    print("告警 Webhook 测试\n" + "=" * 72)
    if not notifier.enabled:
        print("  ⚠️  告警未启用或未配置 alerts.webhook_url")
        print("  请在 config/config.yaml 中设置：alerts.enabled: true / alerts.webhook_url")
        return 1
    ok = notifier.notify(
        EVENT_STARTUP,
        "告警测试",
        f"这是一条测试消息。店铺：{config.shop_hint or '默认店铺'}",
        force=True,
    )
    print(f"  发送结果：{'✅ 成功' if ok else '❌ 失败'}")
    if notifier.last_error:
        print(f"  错误：{notifier.last_error}")
    print("=" * 72)
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# rules csv
# ---------------------------------------------------------------------------
def cmd_rules(args) -> int:
    config = _load_config(args)
    if args.rules_command == "export":
        count = export_csv(config, args.type, Path(args.output))
        print(f"✅ 已导出 {count} 条 {args.type} 规则到：{args.output}")
        return 0
    if args.rules_command == "import":
        try:
            result = import_csv(config, args.type, Path(args.input), replace=args.replace)
        except (ValueError, OSError) as exc:
            print(f"❌ 导入失败：{exc}")
            return 1
        print(
            f"✅ 导入完成：新增 {result['added']}，更新 {result['updated']}，"
            f"跳过 {result['skipped']}，当前共 {result['total']} 条"
        )
        return 0
    print("用法：python -m pdd_cs rules export|import --type keywords|transfer|replace")
    return 2


def cmd_version(args) -> int:
    print(f"pdd-cs {__version__}")
    return 0


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pdd-cs",
        description="拼多多智能客服（多店铺）：基于拼多多商家后台实时消息的 AI 客服",
    )
    parser.add_argument("--version", action="version", version=f"pdd-cs {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    init_parser = subparsers.add_parser("init", help="初始化配置")
    init_parser.add_argument("--force", action="store_true", help="覆盖已有配置")
    init_parser.set_defaults(func=cmd_init)

    login_parser = subparsers.add_parser("login", help="扫码登录并保存 Cookie")
    login_parser.add_argument("--shop", help="店铺 id（多店铺时必填）")
    login_parser.add_argument("--config", help="配置文件路径")
    login_parser.add_argument("--chrome", help="Chrome 可执行文件路径")
    login_parser.add_argument("--cookie", help="直接粘贴 Cookie 文本")
    login_parser.add_argument("--cookie-file", help="从文件读取 Cookie 文本")
    login_parser.add_argument("--timeout", type=int, default=300, help="等待登录秒数")
    login_parser.add_argument("--keep-open", action="store_true", help="保存后保持浏览器打开")
    login_parser.set_defaults(func=cmd_login)

    run_parser = subparsers.add_parser("run", help="启动自动客服（多店铺）")
    run_parser.add_argument("--config", help="配置文件路径")
    run_parser.add_argument("--host", help="面板绑定地址（默认取 config/server.host）")
    run_parser.add_argument("--port", type=int, help="面板端口（默认取 config/server.port，当前 38617）")
    run_parser.add_argument("--no-dashboard", action="store_true", help="不启动监控面板")
    run_parser.add_argument("--verbose", action="store_true", help="输出调试日志")
    run_parser.set_defaults(func=cmd_run)

    doctor_parser = subparsers.add_parser("doctor", help="环境自检")
    doctor_parser.add_argument("--config", help="配置文件路径")
    doctor_parser.set_defaults(func=cmd_doctor)

    sim_parser = subparsers.add_parser("simulate", help="决策测试台")
    sim_parser.add_argument("text", nargs="*", help="客户消息，可多条")
    sim_parser.add_argument("--shop", help="店铺 id")
    sim_parser.add_argument("--config", help="配置文件路径")
    sim_parser.set_defaults(func=cmd_simulate)

    stats_parser = subparsers.add_parser("stats", help="运营统计")
    stats_parser.add_argument("--shop", help="店铺 id")
    stats_parser.add_argument("--config", help="配置文件路径")
    stats_parser.set_defaults(func=cmd_stats)

    export_parser = subparsers.add_parser("export", help="导出聊天记录")
    export_parser.add_argument("--output", help="输出 JSON 路径")
    export_parser.add_argument("--days", type=int, default=30, help="导出最近多少天")
    export_parser.add_argument("--shop", help="店铺 id")
    export_parser.add_argument("--config", help="配置文件路径")
    export_parser.set_defaults(func=cmd_export)

    lint_parser = subparsers.add_parser("lint", help="话术/知识库合规体检")
    lint_parser.add_argument("--strict", action="store_true", help="有高危问题时返回非 0")
    lint_parser.add_argument("--config", help="配置文件路径")
    lint_parser.set_defaults(func=cmd_lint)

    qc_parser = subparsers.add_parser("qc", help="会话质检")
    qc_parser.add_argument("--days", type=int, default=0, help="检查最近多少天（默认配置）")
    qc_parser.add_argument("--shop", help="店铺 id")
    qc_parser.add_argument("--output", help="质检报告输出 JSON 路径")
    qc_parser.add_argument("--config", help="配置文件路径")
    qc_parser.set_defaults(func=cmd_qc)

    alert_parser = subparsers.add_parser("alert-test", help="测试告警 Webhook")
    alert_parser.add_argument("--config", help="配置文件路径")
    alert_parser.set_defaults(func=cmd_alert_test)

    rules_parser = subparsers.add_parser("rules", help="话术库 CSV 导入导出")
    rules_parser.add_argument("rules_command", choices=["export", "import"], help="export 或 import")
    rules_parser.add_argument("--type", choices=list(RULE_TYPES), default="keywords", help="规则类型")
    rules_parser.add_argument("--output", help="导出 CSV 路径")
    rules_parser.add_argument("--input", help="导入 CSV 路径")
    rules_parser.add_argument("--replace", action="store_true", help="导入时整表替换")
    rules_parser.add_argument("--config", help="配置文件路径")
    rules_parser.set_defaults(func=cmd_rules)

    version_parser = subparsers.add_parser("version", help="版本号")
    version_parser.set_defaults(func=cmd_version)
    return parser


def main(argv: list[str] | None = None) -> int:
    _setup_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
