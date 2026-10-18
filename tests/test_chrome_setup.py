# -*- coding: utf-8 -*-
"""Chrome 检测 / 一键下载测试（不依赖网络：使用本地假压缩包）。"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

from pdd_cs import chrome_setup
from pdd_cs import web_admin
from pdd_cs.pdd import cdp_login


def _make_fake_chrome_zip(path: Path) -> None:
    is_win = sys.platform.startswith("win")
    root = "chrome-win64" if is_win else "chrome-linux64"
    name = "chrome.exe" if is_win else "chrome"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{root}/{name}", "fake chrome binary")
        archive.writestr(f"{root}/resources.pak", "fake resource")


def test_install_from_local_zip(tmp_path):
    zip_path = tmp_path / "chrome.zip"
    _make_fake_chrome_zip(zip_path)
    target = tmp_path / "chrome"
    result = chrome_setup.install_chrome(
        target_dir=target, zip_path=zip_path, force=True
    )
    assert result["ok"] is True and result["found"] is True
    assert Path(result["path"]).exists()
    assert Path(result["path"]).name in ("chrome", "chrome.exe")

    status = chrome_setup.install_status(target)
    assert status["found"] is True and status["path"] == result["path"]

    again = chrome_setup.install_chrome(target_dir=target, zip_path=zip_path)
    assert again.get("already_installed") is True


def test_install_rejects_bad_zip(tmp_path):
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not a zip file")
    try:
        chrome_setup.install_chrome(target_dir=tmp_path / "chrome", zip_path=bad, force=True)
    except chrome_setup.ChromeInstallError as exc:
        assert "压缩包" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("损坏的压缩包应被拒绝")


def test_platform_key_supported():
    key = chrome_setup.platform_key()
    assert key in {"win64", "win32", "mac-arm64", "mac-x64", "linux64"}
    assert chrome_setup.install_supported() is True


def test_find_chrome_env_override(tmp_path, monkeypatch):
    executable = tmp_path / ("chrome.exe" if sys.platform.startswith("win") else "chrome")
    executable.write_text("fake", encoding="utf-8")
    monkeypatch.setenv("PDD_CS_CHROME", str(executable))
    assert cdp_login.find_chrome() == str(executable)


def test_capability_reports_broken_chrome(monkeypatch):
    monkeypatch.setattr(web_admin, "find_chrome", lambda: "/opt/google/chrome/chrome")
    monkeypatch.setattr(
        web_admin, "chrome_version",
        lambda path: "error while loading shared libraries: libnss3.so: cannot open shared object file",
    )
    monkeypatch.setattr(web_admin, "display_available", lambda: True)
    capability = web_admin.LoginManager(None).capability()  # type: ignore[arg-type]
    assert capability["chrome_found"] is True
    assert capability["chrome_runnable"] is False
    assert capability["browser_login"] is False
    assert "启动失败" in capability["message"]


def test_capability_reports_no_display(monkeypatch):
    monkeypatch.setattr(web_admin, "find_chrome", lambda: "/opt/google/chrome/chrome")
    monkeypatch.setattr(web_admin, "chrome_version", lambda path: "Chrome/120.0.0.0")
    monkeypatch.setattr(web_admin, "display_available", lambda: False)
    manager = web_admin.LoginManager(None)  # type: ignore[arg-type]
    capability = manager.capability()
    assert capability["chrome_found"] is True
    assert capability["browser_login"] is False
    assert capability["chrome_version"] == "Chrome/120.0.0.0"
    assert "图形界面" in capability["message"]
