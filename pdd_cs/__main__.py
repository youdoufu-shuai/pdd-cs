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
import copy
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

import yaml

from . import __version__
from .alerts import EVENT_STARTUP, AlertNotifier
from .config import (
    DEFAULTS,
    EXAMPLE_CONFIG_PATH,
    PROJECT_ROOT,
    TEMPLATE_CONFIG_DIR,
    Config,
    ShopProfile,
    ensure_dirs,
)
from .engine.service import PDDAutoReplyService
from .engine.qc import QCScanner
from .pdd.api import PDDClient, PDDSessionExpired
from .pdd.auth import CookieBundle, CookieError, CookieStore, cookie_age_hours
from .chrome_setup import ChromeInstallError, install_chrome, install_supported
from .pdd.cdp_login import (
    LoginError,
    chrome_status,
    login_with_browser,
    save_cookie_text,
)
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
def _scaffold_from_templates(config_dir: Path, *, overwrite: bool = False) -> None:
    """pip 安装模式：把随包模板复制成用户目录下的 config/。"""
    config_dir.mkdir(parents=True, exist_ok=True)
    target = config_dir / "config.yaml"
    example = TEMPLATE_CONFIG_DIR / "config.example.yaml"
    if example.exists() and (overwrite or not target.exists()):
        shutil.copyfile(example, target)

    persona = TEMPLATE_CONFIG_DIR / "persona.md"
    if persona.exists():
        dest = config_dir / "persona.md"
        if overwrite or not dest.exists():
            shutil.copyfile(persona, dest)

    for sub in ("knowledge", "rules"):
        source = TEMPLATE_CONFIG_DIR / sub
        if not source.is_dir():
            continue
        dest_dir = config_dir / sub
        dest_dir.mkdir(parents=True, exist_ok=True)
        for item in sorted(source.iterdir()):
            if not item.is_file():
                continue
            dest = dest_dir / item.name
            if overwrite or not dest.exists():
                shutil.copyfile(item, dest)


def cmd_init(args) -> int:
    print(BANNER)
    ensure_dirs(Config.load())
    config_dir = PROJECT_ROOT / "config"
    target = config_dir / "config.yaml"
    if target.exists() and not args.force:
        print(f"配置已存在：{target}")
        # pip 安装模式下可能只缺规则/知识库：补齐缺失文件，但不覆盖用户已有内容
        if TEMPLATE_CONFIG_DIR.exists() and not EXAMPLE_CONFIG_PATH.exists():
            _scaffold_from_templates(config_dir, overwrite=False)
    elif EXAMPLE_CONFIG_PATH.exists():
        # 仓库 / 源码方式：直接复制带完整注释的示例配置
        config_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(EXAMPLE_CONFIG_PATH, target)
        print(f"✅ 已生成配置：{target}")
    elif TEMPLATE_CONFIG_DIR.exists():
        # pip 安装方式：从包内置模板生成配置、人设、知识库与规则
        _scaffold_from_templates(config_dir, overwrite=args.force)
        print(f"✅ pip 安装模式：已从内置模板生成配置与话术：{config_dir}")
    else:
        # 最后兜底：至少生成一份可解析的基础配置
        config_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "# pdd-cs 基础配置（未找到内置模板，建议 git clone 仓库获取完整话术库）\n"
            + yaml.safe_dump(DEFAULTS, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        print(f"✅ 已生成基础配置：{target}")
        print("⚠️  未找到内置话术模板，建议从仓库获取完整 config/ 目录")
    (PROJECT_ROOT / "config" / "secrets").mkdir(parents=True, exist_ok=True)
    print("\n下一步：")
    print("  1. 编辑 config/config.yaml：店铺列表 shops、LLM Key、告警 Webhook")
    print("  2. 编辑 config/persona.md、config/knowledge/*.md、config/rules/*.yaml")
    print("  3. 逐个店铺登录：python -m pdd_cs login --shop <店铺id>")
    print("  4. 运行：python -m pdd_cs run")
    print("\n不会配置？运行交互式向导：python -m pdd_cs setup")
    print("Windows 用户也可以双击 start.bat 一键启动")
    print("\n多店铺配置示例（config/config.yaml）：")
    print("  shops:")
    print("    - id: shop-a")
    print("      name: A店（女装）")
    print("      cookie_file: config/secrets/shop-a.json")
    return 0


def _ask_text(prompt: str, default: str = "") -> str:
    """交互式提问；非交互环境返回默认值，不阻塞。"""
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"{prompt}{suffix}：").strip()
    except (EOFError, KeyboardInterrupt):
        return default
    return value or default


def _default_config_data() -> dict:
    """读取示例配置（仓库/模板），作为 setup 的基础。"""
    if EXAMPLE_CONFIG_PATH.exists():
        source = EXAMPLE_CONFIG_PATH
    elif (TEMPLATE_CONFIG_DIR / "config.example.yaml").exists():
        source = TEMPLATE_CONFIG_DIR / "config.example.yaml"
    else:
        return copy.deepcopy(DEFAULTS)
    loaded = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    return loaded if isinstance(loaded, dict) else copy.deepcopy(DEFAULTS)


def cmd_setup(args) -> int:
    """新手向导：交互式生成一份可运行的配置。"""
    print(BANNER)
    print("新手上手向导（一路回车即可使用默认值，Ctrl+C 可随时退出）")
    print("=" * 68)

    interactive = not getattr(args, "yes", False) and sys.stdin.isatty()
    if getattr(args, "config", None):
        target = Path(args.config)
        if not target.is_absolute():
            target = PROJECT_ROOT / target
    else:
        target = PROJECT_ROOT / "config" / "config.yaml"

    if target.exists() and not getattr(args, "force", False):
        print(f"配置已存在：{target}")
        print("如需重新生成，请加 --force；或者直接编辑该文件。")
        return 1

    # ---- 店铺模式 ----
    shops_spec = getattr(args, "shops", None)
    if shops_spec is None and interactive:
        mode = _ask_text("店铺模式：1=单店铺，2=多店铺", "1")
        if mode.strip() in {"2", "多", "multi", "m"}:
            shops_spec = _ask_text("店铺列表（格式 id:名称，多个用英文逗号分隔，例：shop-a:A店,shop-b:B店）")
        else:
            shops_spec = ""
    shops_spec = (shops_spec or "").strip()

    shops: list[dict[str, str]] = []
    for part in shops_spec.split(","):
        part = part.strip()
        if not part:
            continue
        shop_id, _, shop_name = part.partition(":")
        shop_id = shop_id.strip()
        if not shop_id:
            continue
        shops.append(
            {
                "id": shop_id,
                "name": (shop_name or shop_id).strip(),
                "cookie_file": f"config/secrets/{shop_id}.json",
                "enabled": True,
            }
        )

    # ---- 面板端口 ----
    port = getattr(args, "port", None)
    if port is None:
        if interactive:
            raw_port = _ask_text("面板端口", "38617")
            try:
                port = int(raw_port)
            except ValueError:
                print(f"端口 {raw_port!r} 不是数字，使用默认 38617")
                port = 38617
        else:
            port = 38617
    if not (1024 <= int(port) <= 65535):
        print(f"⚠️ 端口 {port} 不在建议范围（1024-65535），已回落为 38617")
        port = 38617

    # ---- AI 大模型（可选，小白可以跳过） ----
    llm_key = (getattr(args, "llm_key", None) or "").strip()
    if not llm_key and interactive and not getattr(args, "no_llm", False):
        answer = _ask_text("是否启用 AI 回复？输入 y 配置大模型 Key，直接回车跳过（仅关键词回复）", "")
        if answer.lower() in {"y", "yes", "是", "1"}:
            llm_key = _ask_text("大模型 API Key（DeepSeek/OpenAI 兼容）")
    use_llm = bool(llm_key) and not getattr(args, "no_llm", False)

    # ---- 生成配置 ----
    data = _default_config_data()
    data.setdefault("server", {})
    data["server"]["port"] = int(port)
    data["shops"] = shops
    llm = data.setdefault("llm", {})
    llm["enabled"] = use_llm
    llm["api_key"] = llm_key if use_llm else ""

    target.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# 由 `python -m pdd_cs setup` 生成（小白向导）\n"
        "# 完整注释说明见同目录的 config.example.yaml\n"
    )
    target.write_text(header + yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")

    config_dir = target.parent
    for sub in ("secrets", "knowledge", "rules"):
        (config_dir / sub).mkdir(parents=True, exist_ok=True)
    data_dir = Path(str(data.get("app", {}).get("data_dir", "data")))
    if not data_dir.is_absolute():
        data_dir = PROJECT_ROOT / data_dir
    data_dir.mkdir(parents=True, exist_ok=True)

    print("\n" + "=" * 68)
    print(f"✅ 配置已生成：{target}")
    if shops:
        print(f"   店铺：{'、'.join(item['name'] for item in shops)}")
        print("   登录命令：")
        for item in shops:
            print(f"     python -m pdd_cs login --shop {item['id']}")
    else:
        print("   店铺：默认单店铺")
        print("   登录命令：python -m pdd_cs login")
    print(f"   面板端口：{port}（浏览器打开 http://127.0.0.1:{port}）")
    print(f"   AI 回复：{'已启用' if use_llm else '未启用（只用关键词/默认回复，后续可加）'}")
    if use_llm:
        print(f"   ⚠️ API Key 已写入 {target}（该文件不会被 git 提交）")
    print("\n下一步（按顺序执行）：")
    print("   1) python -m pdd_cs login        # 扫码登录店铺")
    print("   2) python -m pdd_cs doctor       # 环境自检")
    print("   3) python -m pdd_cs run          # 启动自动客服")
    print("   不想敲命令？Windows 双击 start.bat，macOS/Linux 执行 bash start.sh")
    print("=" * 68)
    return 0


def _print_welcome() -> None:
    """不带子命令时的新手欢迎页。"""
    print(BANNER)
    print("第一次使用？按顺序执行下面 5 步即可：")
    print()
    print("  1) python -m pdd_cs init       # 生成默认配置")
    print("  2) 编辑 config/config.yaml     # 填店铺、可选填大模型 Key")
    print("  3) python -m pdd_cs login      # 扫码登录拼多多商家后台")
    print("  4) python -m pdd_cs doctor     # 自检（有问题会告诉你）")
    print("  5) python -m pdd_cs run        # 启动（面板 http://127.0.0.1:38617）")
    print()
    print("不会配置？运行交互式向导：python -m pdd_cs setup")
    print("没有店铺想先看效果？运行：python scripts/demo.py")
    print("全部命令：python -m pdd_cs --help")
    print()


def cmd_chrome(args) -> int:
    if args.install:
        if not install_supported():
            print("❌ 当前系统不支持自动下载 Chrome（例如 ARM Linux），请用系统包管理器安装 Chromium。")
            return 2
        print("正在下载/安装 Chrome for Testing（约 150MB，免 root）…")

        def progress(stage: str, percent: float, message: str) -> None:
            print(f"  [{percent:5.1f}%] {message}", flush=True)

        try:
            result = install_chrome(
                zip_path=Path(args.zip) if args.zip else None,
                force=bool(args.force),
                progress=progress,
            )
        except ChromeInstallError as exc:
            print(f"❌ 安装失败：{exc}")
            return 1
        except KeyboardInterrupt:
            print("\n已取消")
            return 130
        status = chrome_status()
        if args.json:
            print(json.dumps(status, ensure_ascii=False, indent=2))
        else:
            print(f"✅ Chrome 已安装：{result.get('path', '')}")
            if status.get("version"):
                print(f"   版本：{status['version']}")
            print("   下一步：python -m pdd_cs login（或直接打开网页面板登录）")
        return 0

    status = chrome_status()
    if args.json:
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0
    if status.get("found"):
        print("✅ 已检测到 Chrome/Chromium/Edge")
        print(f"   路径：{status['path']}")
        print(f"   版本：{status.get('version') or '未知'}")
        print(f"   来源：{status.get('source') or 'system'}")
        if status.get("display_available"):
            print("   图形界面：有（可以直接扫码登录）")
        else:
            print("   图形界面：无（服务器无法弹出扫码窗口）")
            print("   请在本机登录后导出登录态，再到服务器面板「导入登录态」；或配置 Xvfb/VNC。")
    else:
        print("⚠️ 未检测到 Chrome/Chromium/Edge")
        print("   一键下载：python -m pdd_cs chrome --install")
        print("   离线安装：PDD_CS_CHROME_ZIP=/path/chrome-linux64.zip python -m pdd_cs chrome --install")
        print("   或者：本机登录后导出登录态，到服务器「导入登录态」，完全不需要 Chrome。")
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
            status = chrome_status()
            if status.get("found") and not status.get("display_available"):
                print("❌ 已检测到 Chrome，但当前环境没有图形界面（DISPLAY），无法弹出扫码窗口。")
                print("   可选方案：")
                print("   1) 本机登录后导出登录态，到服务器面板「导入登录态」（推荐）")
                print("   2) 用 --cookie / --cookie-file 粘贴 Cookie")
                print("   3) 在服务器配置 Xvfb/VNC 后重试")
                return 2
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
    await service.start(allow_degraded=True)
    if not service.workers or service.startup_errors:
        print("\n⚠️  店铺还没登录或启动失败，但面板已经启动——")
        print("   请在网页里完成登录与设置：面板 →「店铺与登录」→ 扫码登录 / 粘贴 Cookie")
        for shop_id, error in service.startup_errors.items():
            print(f"   · {shop_id}: {error}")
        print()

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
        if config.server.auto_open_dashboard or getattr(args, "open", False):
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

    try:
        chrome = chrome_status()
        if chrome.get("found"):
            detail = chrome["path"]
            if chrome.get("version"):
                detail += f"（{chrome['version']}）"
            line("✅", "Chrome 登录助手", detail)
            if not chrome.get("display_available"):
                line(
                    "⚠️",
                    "图形界面",
                    "未检测到 DISPLAY：服务器无法弹出扫码窗口；请用「本机登录 → 导出登录态 → 服务器导入」",
                )
        else:
            line(
                "⚠️",
                "Chrome 登录助手",
                "未检测到 Chrome/Chromium/Edge：可运行 python -m pdd_cs chrome --install 一键下载（免 root），"
                "或使用「导入登录态」",
            )
    except Exception as exc:  # noqa: BLE001
        line("⚠️", "Chrome 登录助手", f"检测失败：{exc}")
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
    print()
    if any("Cookie" in problem for problem in config.validate()) or not config.shops:
        print("👉 下一步：先运行 python -m pdd_cs login 扫码登录店铺")
    elif not ok:
        print("👉 下一步：按上面的 ❌ 逐项修复，或运行 python -m pdd_cs setup 重新生成配置")
    else:
        print("👉 下一步：python -m pdd_cs run 启动；面板 http://127.0.0.1:%d" % config.server.port)
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

    setup_parser = subparsers.add_parser("setup", help="新手向导：交互式生成配置（推荐第一次用）")
    setup_parser.add_argument("--yes", action="store_true", help="非交互，全部使用默认值")
    setup_parser.add_argument("--force", action="store_true", help="覆盖已有配置")
    setup_parser.add_argument("--config", help="写入的配置文件路径（默认 config/config.yaml）")
    setup_parser.add_argument("--shops", help="店铺列表，如 shop-a:A店,shop-b:B店")
    setup_parser.add_argument("--port", type=int, help="面板端口（默认 38617）")
    setup_parser.add_argument("--llm-key", help="大模型 API Key（不填则只用关键词回复）")
    setup_parser.add_argument("--no-llm", action="store_true", help="不启用 AI 回复")
    setup_parser.set_defaults(func=cmd_setup)

    login_parser = subparsers.add_parser("login", help="扫码登录并保存 Cookie")
    login_parser.add_argument("--shop", help="店铺 id（多店铺时必填）")
    login_parser.add_argument("--config", help="配置文件路径")
    login_parser.add_argument("--chrome", help="Chrome 可执行文件路径")
    login_parser.add_argument("--cookie", help="直接粘贴 Cookie 文本")
    login_parser.add_argument("--cookie-file", help="从文件读取 Cookie 文本")
    login_parser.add_argument("--timeout", type=int, default=300, help="等待登录秒数")
    login_parser.add_argument("--keep-open", action="store_true", help="保存后保持浏览器打开")
    login_parser.set_defaults(func=cmd_login)

    chrome_parser = subparsers.add_parser(
        "chrome", help="检测 / 一键下载 Chrome（服务器可选；下载到项目 data/chrome）"
    )
    chrome_parser.add_argument("--status", action="store_true", help="显示检测结果（默认）")
    chrome_parser.add_argument("--install", action="store_true", help="下载并安装 Chrome for Testing")
    chrome_parser.add_argument("--force", action="store_true", help="已安装也重新下载")
    chrome_parser.add_argument("--zip", help="离线安装包路径（chrome-linux64.zip 等）")
    chrome_parser.add_argument("--json", action="store_true", help="以 JSON 输出检测结果")
    chrome_parser.set_defaults(func=cmd_chrome)

    run_parser = subparsers.add_parser("run", help="启动自动客服（多店铺）")
    run_parser.add_argument("--config", help="配置文件路径")
    run_parser.add_argument("--host", help="面板绑定地址（默认取 config/server.host）")
    run_parser.add_argument("--port", type=int, help="面板端口（默认取 config/server.port，当前 38617）")
    run_parser.add_argument("--no-dashboard", action="store_true", help="不启动监控面板")
    run_parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器面板")
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
        _print_welcome()
        return 0
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
