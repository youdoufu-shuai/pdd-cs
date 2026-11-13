# -*- coding: utf-8 -*-
"""CLI 命令端到端测试（子进程运行真实命令）。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _make_config(tmp_path: Path) -> Path:
    """基于示例配置生成一份测试配置（数据目录指向临时目录）。"""
    text = (ROOT / "config" / "config.example.yaml").read_text(encoding="utf-8")
    text = text.replace("data_dir: data", f"data_dir: {tmp_path.as_posix()}")
    config = tmp_path / "config.yaml"
    config.write_text(text, encoding="utf-8")
    return config


def _run(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
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


def test_cli_version():
    result = _run(["version"])
    assert result.returncode == 0
    assert "pdd-cs" in result.stdout


def test_cli_simulate(tmp_path):
    config = _make_config(tmp_path)
    result = _run(["simulate", "什么时候发货？", "我要退货", "--config", str(config)])
    assert result.returncode == 0, result.stderr
    assert "关键词" in result.stdout or "keyword" in result.stdout
    assert "转人工" in result.stdout or "transfer" in result.stdout


def test_cli_stats(tmp_path):
    config = _make_config(tmp_path)
    result = _run(["stats", "--config", str(config)])
    assert result.returncode == 0, result.stderr
    assert "今日统计" in result.stdout


def test_cli_export(tmp_path):
    config = _make_config(tmp_path)
    target = tmp_path / "export.json"
    result = _run(["export", "--config", str(config), "--output", str(target)])
    assert result.returncode == 0, result.stderr
    assert target.exists()
    data = json.loads(target.read_text(encoding="utf-8"))
    assert "messages" in data and "conversations" in data and "replies" in data


def test_cli_doctor(tmp_path):
    config = _make_config(tmp_path)
    result = _run(["doctor", "--config", str(config)])
    # 没有真实 Cookie 时 doctor 返回 1，但自检流程必须完整跑完
    assert result.returncode in (0, 1)
    assert "环境自检" in result.stdout
    assert "Cookie" in result.stdout
    assert "规则" in result.stdout


def test_cli_doctor_offline_does_not_construct_platform_client(tmp_path, monkeypatch, capsys):
    from argparse import Namespace
    from pdd_cs import __main__ as cli
    from pdd_cs.config import Config
    from pdd_cs.pdd.auth import CookieStore

    cfg = Config.load(_make_config(tmp_path))
    cfg.shops[0].cookie_file = tmp_path / "mock-cookies.json"
    CookieStore(cfg.shops[0].cookie_file).save_text("PASS_ID=synthetic-test")
    monkeypatch.setattr(cli, "_load_config", lambda _args: cfg)

    def unexpected_network(*_args, **_kwargs):
        raise AssertionError("offline doctor must not create a platform client")

    monkeypatch.setattr(cli, "PDDClient", unexpected_network)
    cli.cmd_doctor(Namespace(offline=True))
    assert "离线模式：未请求平台接口" in capsys.readouterr().out


def test_cli_lint(tmp_path):
    config = _make_config(tmp_path)
    result = _run(["lint", "--config", str(config)])
    assert result.returncode == 0, result.stderr
    assert "合规体检" in result.stdout


def test_cli_qc(tmp_path):
    config = _make_config(tmp_path)
    result = _run(["qc", "--days", "3", "--config", str(config)])
    assert result.returncode == 0, result.stderr
    assert "会话质检" in result.stdout


def test_cli_rules_export_import(tmp_path):
    import shutil

    from pdd_cs.config import PROJECT_ROOT

    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    for name in ("keywords.yaml", "transfer.yaml", "replace.yaml"):
        shutil.copyfile(PROJECT_ROOT / "config" / "rules" / name, rules_dir / name)
    text = (PROJECT_ROOT / "config" / "config.example.yaml").read_text(encoding="utf-8")
    text = text.replace("data_dir: data", f"data_dir: {(tmp_path / 'data').as_posix()}")
    for name in ("keywords.yaml", "transfer.yaml", "replace.yaml"):
        text = text.replace(f"config/rules/{name}", str(rules_dir / name))
    config = tmp_path / "rules-config.yaml"
    config.write_text(text, encoding="utf-8")

    csv_path = tmp_path / "keywords.csv"
    result = _run(["rules", "export", "--type", "keywords", "--output", str(csv_path), "--config", str(config)])
    assert result.returncode == 0, result.stderr
    assert csv_path.exists()

    result = _run(["rules", "import", "--type", "keywords", "--input", str(csv_path), "--config", str(config)])
    assert result.returncode == 0, result.stderr
    assert "导入完成" in result.stdout


def test_cli_alert_test_disabled(tmp_path):
    config = _make_config(tmp_path)
    result = _run(["alert-test", "--config", str(config)])
    assert result.returncode == 1
    assert "未启用" in result.stdout or "未配置" in result.stdout
