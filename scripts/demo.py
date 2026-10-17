# -*- coding: utf-8 -*-
"""无店铺演示：启动两个 Mock 拼多多店铺 + 多店铺智能客服 + 监控面板。

用途：
    - 没有真实拼多多店铺账号时，体验多店铺完整链路；
    - 截图 / 录制演示视频；
    - 验证决策、转人工、质检与面板是否正常。

运行：
    python scripts/demo.py                 # 默认 180 秒后退出
    python scripts/demo.py --seconds 0     # 一直运行，Ctrl+C 停止
    python scripts/demo.py --port 38617    # 指定面板端口
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from datetime import timedelta  # noqa: E402

from mock.pdd_mock import MockPDDServer  # noqa: E402
from pdd_cs.config import PROJECT_ROOT, Config, ensure_dirs  # noqa: E402
from pdd_cs.engine.rules import KeywordRule  # noqa: E402
from pdd_cs.engine.service import PDDAutoReplyService  # noqa: E402
from pdd_cs.pdd.auth import CookieStore  # noqa: E402
from pdd_cs.server import DashboardServer  # noqa: E402
from pdd_cs.utils import now_beijing  # noqa: E402

DEMO_COOKIE = (
    "PASS_ID=demo-pass; SUB_PASS_ID=demo-sub; MSA=demo-msa; SUB_SYS_ID=demo-sys; "
    "pdd_user_id=1; pdd_user_uin=1; anti_content=demo-anti; api_uid=demo-api"
)

# (店铺, uid, 昵称, 文本, 等待秒数)
SCENARIO = [
    ("shop-a", "demo-a1", "小圆", "你好，请问什么时候发货呀？", 1.0),
    ("shop-b", "demo-b1", "阿泽", "发什么快递呢？", 1.6),
    ("shop-a", "demo-a2", "Momo", "能便宜点吗？", 1.6),
    ("shop-b", "demo-b2", "大熊", "收到的衣服有点大，我要退货", 1.8),
    ("shop-a", "demo-a3", "小鹿", "返现吗？", 1.8),
    ("shop-b", "demo-b3", "晴天", "多久能到呀", 1.6),
    ("shop-a", "demo-a4", "豆豆", "你好在吗", 1.4),
]


def _build_config(port: int, mock_a: MockPDDServer, mock_b: MockPDDServer) -> Config:
    cfg = Config.load(PROJECT_ROOT / "config" / "config.example.yaml")
    data = dict(cfg.data)
    data["app"] = {**data.get("app", {}), "data_dir": str(PROJECT_ROOT / "data-demo")}
    data["llm"] = {**data.get("llm", {}), "enabled": False}
    data["behavior"] = {
        **data.get("behavior", {}),
        "reply_delay_seconds": 0.3,
        "reply_delay_jitter": 0.4,
    }
    data["compliance"] = {
        **data.get("compliance", {}),
        "risk": {**data["compliance"]["risk"], "min_send_interval_seconds": 0.2},
    }
    data["server"] = {**data.get("server", {}), "port": port}
    data["dashboard"] = {"enabled": True, "refresh_seconds": 5}
    data["shops"] = [
        {
            "id": "shop-a",
            "name": "A店 · 女装",
            "cookie_file": str(PROJECT_ROOT / "data-demo" / "cookies-a.json"),
            "overrides": {
                "pdd": {"base_url": mock_a.base_url, "ws_url": mock_a.ws_url},
                "compliance": {"human_handoff": {"assign_cs_name": "小美"}},
            },
        },
        {
            "id": "shop-b",
            "name": "B店 · 百货",
            "cookie_file": str(PROJECT_ROOT / "data-demo" / "cookies-b.json"),
            "overrides": {"pdd": {"base_url": mock_b.base_url, "ws_url": mock_b.ws_url}},
        },
    ]
    return Config(data, root=PROJECT_ROOT)


async def run_demo(port: int, seconds: int, open_browser: bool) -> int:
    data_dir = PROJECT_ROOT / "data-demo"
    data_dir.mkdir(parents=True, exist_ok=True)
    # 每次演示用全新数据库，避免旧数据干扰截图
    for suffix in ("", "-wal", "-shm"):
        (data_dir / f"pdd_cs.db{suffix}").unlink(missing_ok=True)

    mock_a = await MockPDDServer(shop_name="A店 · 女装").start()
    mock_b = await MockPDDServer(shop_name="B店 · 百货").start()
    config = _build_config(port, mock_a, mock_b)
    ensure_dirs(config)
    for profile in config.shops:
        CookieStore(profile.cookie_file).save_text(DEMO_COOKIE)

    service = PDDAutoReplyService(config)
    # 演示平台规则拦截：一条包含「好评返现 / 加微信」的违规话术，
    # 引擎会在发送前拦截并改为兜底话术 + 转人工，同时写入风控/质检。
    service.rules.keywords.insert(
        0,
        KeywordRule(
            id="demo-risky",
            patterns=["返现吗"],
            reply="可以的，好评返现～加微信给您返现哦",
            priority=1,
        ),
    )
    dashboard = DashboardServer(config, service)
    mocks = {"shop-a": mock_a, "shop-b": mock_b}
    try:
        await service.start()

        # 造一点“历史会话”，让会话质检 / 洞察 / 知识缺口在演示里有内容可看
        base = now_beijing() - timedelta(hours=2)
        seed = service.store
        seed.record_inbound(uid="demo-qc-1", nickname="历史客户A", role="OTHER",
                            context_type="text", content="什么时候发货", created_at=base,
                            shop_id="shop-a")
        seed.record_outbound(uid="demo-qc-1", content="放心，今天一定发，包退", action="keyword",
                             result="auto_reply", status="sent", created_at=base + timedelta(seconds=20),
                             shop_id="shop-a")
        seed.record_inbound(uid="demo-qc-2", nickname="历史客户B", role="OTHER",
                            context_type="text", content="在吗？怎么没人理", created_at=base,
                            shop_id="shop-b")
        seed.record_inbound(uid="demo-qc-3", nickname="历史客户C", role="OTHER",
                            context_type="text", content="你们支持以旧换新吗", created_at=base,
                            shop_id="shop-b")
        seed.record_outbound(uid="demo-qc-3", content="", action="no_match", result="no_match",
                             status="sent", question="你们支持以旧换新吗",
                             created_at=base + timedelta(seconds=5), shop_id="shop-b")

        for _ in range(80):
            if all(w.channel and w.channel.connected for w in service.workers.values()):
                break
            await asyncio.sleep(0.15)
        dashboard.start()
        print("\n" + "=" * 68)
        print("  多店铺演示已启动（Mock 拼多多 × 2 + 智能客服）")
        print(f"  监控面板: {dashboard.url}")
        print(f"  A 店 Mock: {mock_a.base_url}")
        print(f"  B 店 Mock: {mock_b.base_url}")
        print("=" * 68)
        if open_browser:
            try:
                webbrowser.open(dashboard.url)
            except Exception:  # noqa: BLE001
                pass

        started = time.time()
        for shop_id, uid, nickname, text, wait in SCENARIO:
            await asyncio.sleep(wait)
            await mocks[shop_id].push_text(text, uid=uid, nickname=nickname)
            print(f"  [{shop_id}] 买家({nickname}): {text}")
        print("\n  场景已推送完成，等待自动回复…")

        deadline = started + seconds if seconds > 0 else None
        while True:
            await asyncio.sleep(1)
            if deadline and time.time() > deadline:
                break
        return 0
    finally:
        dashboard.stop()
        await service.stop()
        await mock_a.stop()
        await mock_b.stop()
        print("演示已结束")


def main() -> int:
    parser = argparse.ArgumentParser(description="拼多多多店铺智能客服演示（无需真实店铺）")
    parser.add_argument("--port", type=int, default=38617, help="监控面板端口（默认 38617，少见端口）")
    parser.add_argument("--seconds", type=int, default=180, help="运行秒数，0 表示一直运行")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()
    try:
        return asyncio.run(run_demo(args.port, args.seconds, not args.no_browser))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
