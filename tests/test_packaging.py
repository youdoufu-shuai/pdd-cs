# -*- coding: utf-8 -*-
"""打包与安装模式测试（pip install . 之后也能直接 init 使用）。"""
from __future__ import annotations

import tomllib
from pathlib import Path
from types import SimpleNamespace

import yaml

from pdd_cs import config as config_module
from pdd_cs import __main__ as cli

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = PROJECT_ROOT / "pdd_cs" / "templates" / "config"


def _config_files() -> list[Path]:
    files = [PROJECT_ROOT / "config" / "config.example.yaml", PROJECT_ROOT / "config" / "persona.md"]
    files += sorted((PROJECT_ROOT / "config" / "knowledge").glob("*.md"))
    files += sorted((PROJECT_ROOT / "config" / "rules").glob("*.yaml"))
    return files


def test_templates_are_in_sync_with_repo_config():
    """随包模板必须和仓库里的 config/ 完全一致，避免两套配置漂移。"""
    files = _config_files()
    assert len(files) >= 13
    for source in files:
        relative = source.relative_to(PROJECT_ROOT / "config")
        template = TEMPLATE_DIR / relative
        assert template.exists(), f"缺少模板: {template}"
        assert template.read_bytes() == source.read_bytes(), f"模板与仓库不一致: {relative}"


def test_project_root_detection_in_repo():
    assert config_module.PROJECT_ROOT == PROJECT_ROOT
    assert (config_module.PROJECT_ROOT / "pyproject.toml").is_file()
    assert config_module.EXAMPLE_CONFIG_PATH.exists()
    assert config_module.TEMPLATE_CONFIG_DIR.exists()


def test_pyproject_declares_console_script_and_templates():
    data = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["scripts"]["pdd-cs"] == "pdd_cs.__main__:main"
    package_data = data["tool"]["setuptools"]["package-data"]
    assert "pdd_cs.web" in package_data
    assert "pdd_cs.templates" in package_data


def test_init_scaffolds_full_config_in_installed_mode(tmp_path, monkeypatch):
    """模拟 pip 安装模式：没有仓库 config.example.yaml，init 应从包模板生成完整配置。"""
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(cli, "EXAMPLE_CONFIG_PATH", tmp_path / "missing-example.yaml")
    monkeypatch.setattr(cli, "ensure_dirs", lambda config: None)

    assert cli.cmd_init(SimpleNamespace(force=True)) == 0

    config_dir = tmp_path / "config"
    assert (config_dir / "config.yaml").exists()
    assert (config_dir / "persona.md").exists()
    assert (config_dir / "rules" / "keywords.yaml").exists()
    assert (config_dir / "rules" / "compliance.yaml").exists()
    assert (config_dir / "knowledge" / "store-policy.md").exists()
    assert (config_dir / "secrets").is_dir()

    loaded = yaml.safe_load((config_dir / "config.yaml").read_text(encoding="utf-8"))
    assert loaded["server"]["port"] == 38617
    assert "shops" in loaded


def test_init_does_not_overwrite_existing_files_without_force(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(cli, "EXAMPLE_CONFIG_PATH", tmp_path / "missing-example.yaml")
    monkeypatch.setattr(cli, "ensure_dirs", lambda config: None)
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text("# my own config\n", encoding="utf-8")

    assert cli.cmd_init(SimpleNamespace(force=False)) == 0
    assert (config_dir / "config.yaml").read_text(encoding="utf-8") == "# my own config\n"
    # 缺失的规则文件仍会补齐，保证可以运行
    assert (config_dir / "rules" / "keywords.yaml").exists()
