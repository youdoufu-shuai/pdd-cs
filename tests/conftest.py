# -*- coding: utf-8 -*-
"""pytest 公共夹具。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pdd_cs.config import PROJECT_ROOT, Config, ensure_dirs  # noqa: E402
from pdd_cs.engine.service import PDDAutoReplyService  # noqa: E402
from pdd_cs.pdd.auth import CookieStore  # noqa: E402

MOCK_COOKIE = (
    "PASS_ID=mock-pass; SUB_PASS_ID=mock-sub; MSA=mock-msa; SUB_SYS_ID=mock-sys; "
    "pdd_user_id=1; pdd_user_uin=1; anti_content=mock-anti; api_uid=mock-api"
)


@pytest.fixture()
def config(tmp_path: Path) -> Config:
    """测试配置：使用示例配置，改写数据目录 / Cookie 路径，关闭 LLM 与面板。"""
    cfg = Config.load(PROJECT_ROOT / "config" / "config.example.yaml")
    cfg.data_dir = tmp_path / "data"
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.pdd.cookie_file = tmp_path / "cookies.json"
    cfg.llm.enabled = False
    cfg.dashboard.enabled = False
    cfg.behavior.reply_delay_seconds = 0.0
    cfg.behavior.reply_delay_jitter = 0.0
    cfg.compliance.risk.min_send_interval_seconds = 0.0
    ensure_dirs(cfg)
    CookieStore(cfg.pdd.cookie_file).save_text(MOCK_COOKIE)
    return cfg


@pytest.fixture()
def service(config: Config) -> PDDAutoReplyService:
    svc = PDDAutoReplyService(config)
    yield svc
    try:
        svc.store.close()
    except Exception:  # noqa: BLE001
        pass
