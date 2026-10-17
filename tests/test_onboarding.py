# -*- coding: utf-8 -*-
"""小白引导测试：setup 向导 / 欢迎页 / 一键启动脚本。"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import yaml

from pdd_cs import __main__ as cli

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _setup_args(**overrides) -> SimpleNamespace:
    args = {
        "config": None,
        "force": True,
        "yes": True,
        "shops": None,
        "port": None,
        "llm_key": None,
        "no_llm": False,
    }
    args.update(overrides)
    return SimpleNamespace(**args)


def test_setup_noninteractive_single_shop(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    target = tmp_path / "custom" / "config.yaml"
    args = _setup_args(config=str(target), port=40123)

    assert cli.cmd_setup(args) == 0

    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert data["server"]["port"] == 40123
    assert data["shops"] == []
    assert data["llm"]["enabled"] is False
    assert (target.parent / "secrets").is_dir()


def test_setup_multi_shop_and_llm(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    target = tmp_path / "config" / "config.yaml"
    args = _setup_args(
        config=str(target),
        shops="shop-a:A店,shop-b:B店",
        llm_key="sk-test-123",
    )

    assert cli.cmd_setup(args) == 0

    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert [shop["id"] for shop in data["shops"]] == ["shop-a", "shop-b"]
    assert data["shops"][1]["name"] == "B店"
    assert data["shops"][0]["cookie_file"] == "config/secrets/shop-a.json"
    assert data["llm"]["enabled"] is True
    assert data["llm"]["api_key"] == "sk-test-123"

    # 店铺 Cookie 目录已创建
    assert (target.parent / "secrets").is_dir()


def test_setup_refuses_existing_config_without_force(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    target = tmp_path / "config" / "config.yaml"
    target.parent.mkdir(parents=True)
    target.write_text("# 已有配置\n", encoding="utf-8")

    assert cli.cmd_setup(_setup_args(config=str(target), force=False)) == 1
    assert target.read_text(encoding="utf-8") == "# 已有配置\n"


def test_cli_welcome_page(capsys):
    assert cli.main([]) == 0
    output = capsys.readouterr().out
    assert "第一次使用" in output
    assert "pdd_cs setup" in output
    assert "pdd_cs run" in output


def test_launchers_present():
    bat = (PROJECT_ROOT / "start.bat").read_text(encoding="utf-8")
    sh = (PROJECT_ROOT / "start.sh").read_text(encoding="utf-8")
    for content in (bat, sh):
        assert "pdd_cs setup --yes" in content
        assert "pdd_cs run" in content
        assert "requirements.txt" in content
    assert sh.startswith("#!/usr/bin/env bash")


def test_windows_launcher_check():
    if os.name != "nt":
        return
    result = subprocess.run(
        ["cmd", "/c", "start.bat", "--check"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        timeout=120,
    )
    output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
    assert result.returncode == 0, output
    assert "自检通过" in output
