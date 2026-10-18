# -*- coding: utf-8 -*-
"""Chrome 一键下载 / 检测（Chrome for Testing，无需 root）。

服务器没有 Chrome 时，可以在面板点「一键下载 Chrome」或运行：

    python -m pdd_cs chrome --install

下载的是 Google 官方 Chrome for Testing 压缩包，解压到项目 `data/chrome/` 下，
`find_chrome()` 会自动识别，不需要改动系统、不需要管理员权限。

也支持离线安装：

    PDD_CS_CHROME_ZIP=/path/to/chrome-linux64.zip python -m pdd_cs chrome --install
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable, Optional

from .config import PROJECT_ROOT

CHROME_FOR_TESTING_META = (
    "https://googlechromelabs.github.io/chrome-for-testing/"
    "last-known-good-versions-with-downloads.json"
)
CHROME_ZIP_ENV = "PDD_CS_CHROME_ZIP"
CHROME_URL_ENV = "PDD_CS_CHROME_URL"
CHROME_META_ENV = "PDD_CS_CHROME_META_URL"

Progress = Callable[[str, float, str], None]


class ChromeInstallError(RuntimeError):
    """Chrome 下载/解压/安装失败。"""


def platform_key() -> str:
    """Chrome for Testing 的平台标识：win64/win32/mac-arm64/mac-x64/linux64。"""
    machine = platform.machine().lower()
    if sys.platform.startswith("win"):
        return "win64" if sys.maxsize > 2**32 else "win32"
    if sys.platform == "darwin":
        return "mac-arm64" if machine in ("arm64", "aarch64") else "mac-x64"
    if sys.platform.startswith("linux"):
        if machine not in ("x86_64", "amd64"):
            raise ChromeInstallError(
                f"Chrome for Testing 暂不支持当前 Linux 架构（{machine or 'unknown'}），"
                "请用系统包管理器安装 Chromium，或改用本机登录后导入登录态。"
            )
        return "linux64"
    raise ChromeInstallError(f"暂不支持当前系统：{sys.platform}")


def install_supported() -> bool:
    try:
        platform_key()
        return True
    except ChromeInstallError:
        return False


def default_target_dir() -> Path:
    return PROJECT_ROOT / "data" / "chrome"


def _report(progress: Optional[Progress], stage: str, percent: float, message: str) -> None:
    if progress:
        try:
            progress(stage, max(0.0, min(100.0, percent)), message)
        except Exception:  # noqa: BLE001 - 进度回调不影响安装
            pass


def _read_current(target: Path) -> Optional[dict[str, Any]]:
    current = target / "current.json"
    if not current.exists():
        return None
    try:
        data = json.loads(current.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    path = str(data.get("path") or "")
    if path and Path(path).exists():
        data["found"] = True
        return data
    return None


def find_executable(root: Path) -> Optional[Path]:
    """在解压目录里找到 Chrome 可执行文件。"""
    if sys.platform.startswith("win"):
        names = ("chrome.exe", "msedge.exe")
    elif sys.platform == "darwin":
        names = ("Google Chrome for Testing", "Google Chrome", "Chromium")
    else:
        names = ("chrome", "chromium")
    found: list[Path] = []
    try:
        for item in root.rglob("*"):
            if not item.is_file() or item.name not in names:
                continue
            if sys.platform == "darwin" and "Contents/MacOS" not in str(item):
                continue
            found.append(item)
    except OSError:
        return None
    if not found:
        return None
    found.sort(key=lambda item: (len(str(item)), str(item)))
    return found[0]


def install_status(target_dir: Optional[Path] = None) -> dict[str, Any]:
    """读取当前项目内 Chrome 安装状态。"""
    target = Path(target_dir) if target_dir else default_target_dir()
    current = _read_current(target)
    if current:
        return {**current, "found": True, "target": str(target)}
    if target.exists():
        executable = find_executable(target)
        if executable:
            return {
                "found": True,
                "path": str(executable),
                "version": "",
                "platform": platform_key() if install_supported() else "",
                "source": "project",
                "target": str(target),
            }
    return {
        "found": False,
        "path": "",
        "version": "",
        "platform": platform_key() if install_supported() else "",
        "target": str(target),
    }


def _http_json(url: str, timeout: float = 30.0) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "pdd-cs-chrome-installer"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _download(url: str, dest: Path, progress: Optional[Progress], timeout: float = 600.0) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "pdd-cs-chrome-installer"})
    _report(progress, "download", 3, "正在下载 Chrome（约 150MB）…")
    with urllib.request.urlopen(request, timeout=timeout) as response, dest.open("wb") as handle:
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            handle.write(chunk)
            done += len(chunk)
            if total:
                percent = 3 + done / total * 70
                _report(
                    progress,
                    "download",
                    percent,
                    f"正在下载 Chrome：{done / 1048576:.1f} / {total / 1048576:.1f} MB",
                )
            else:
                _report(progress, "download", 40, f"正在下载 Chrome：{done / 1048576:.1f} MB")


def _safe_extract(zip_file: Path, dest: Path) -> None:
    dest = dest.resolve()
    try:
        with zipfile.ZipFile(zip_file) as archive:
            for member in archive.infolist():
                name = member.filename
                if not name or name.startswith("/") or name.startswith("\\"):
                    raise ChromeInstallError("Chrome 压缩包包含非法路径，已停止解压")
                target = (dest / name).resolve()
                if target != dest and dest not in target.parents:
                    raise ChromeInstallError("Chrome 压缩包包含非法路径，已停止解压")
            archive.extractall(dest)
    except zipfile.BadZipFile as exc:
        raise ChromeInstallError(f"Chrome 压缩包损坏或格式不正确：{exc}") from exc


def _resolve_download(version: str = "") -> tuple[str, str]:
    """返回 (版本号, 下载 URL)；支持环境变量指定镜像/离线直链。"""
    direct = (os.environ.get(CHROME_URL_ENV) or "").strip()
    if direct:
        if version:
            return version, direct
        import re
        match = re.search(r"/(\d+\.\d+\.\d+\.\d+)/", direct)
        return (match.group(1) if match else "custom"), direct
    meta_url = (os.environ.get(CHROME_META_ENV) or "").strip() or CHROME_FOR_TESTING_META
    meta = _http_json(meta_url)
    try:
        stable = meta["channels"]["Stable"]
        version = str(stable["version"])
        platform_id = platform_key()
        downloads = stable["downloads"]["chrome"]
        for item in downloads:
            if item.get("platform") == platform_id:
                return version, str(item["url"])
    except (KeyError, TypeError) as exc:
        raise ChromeInstallError(f"无法解析 Chrome for Testing 下载信息：{exc}") from exc
    raise ChromeInstallError(
        f"Chrome for Testing 没有当前平台的下载包（{platform_key()}），"
        "请改用系统包管理器安装 Chromium。"
    )


def _make_executable(path: Path) -> None:
    try:
        path.chmod(path.stat().st_mode | 0o755)
    except OSError:
        pass
    if sys.platform == "darwin":
        try:
            subprocess.run(
                ["xattr", "-dr", "com.apple.quarantine", str(path.parent.parent)],
                capture_output=True,
                timeout=15,
            )
        except Exception:  # noqa: BLE001
            pass


def _probe_launch(path: Path) -> str:
    """尝试运行 chrome --version，返回可用性提示（失败不阻断安装）。"""
    if sys.platform.startswith("win"):
        return ""
    try:
        proc = subprocess.run([str(path), "--version"], capture_output=True, text=True, timeout=8)
        text = ((proc.stdout or "") + (proc.stderr or "")).strip()
        return text
    except Exception as exc:  # noqa: BLE001 - 缺少系统库等
        return f"安装完成但暂时无法启动：{exc}"


def install_chrome(
    *,
    target_dir: Optional[Path] = None,
    zip_path: Optional[Path] = None,
    force: bool = False,
    progress: Optional[Progress] = None,
    timeout: float = 600.0,
) -> dict[str, Any]:
    """下载/解压 Chrome for Testing 到项目内，返回安装结果。"""
    target = Path(target_dir) if target_dir else default_target_dir()
    target.mkdir(parents=True, exist_ok=True)

    existing = install_status(target)
    if existing.get("found") and not force:
        _report(progress, "done", 100, "已检测到可用的 Chrome，无需重复下载")
        return {**existing, "ok": True, "already_installed": True}

    local_zip = Path(zip_path) if zip_path else Path(os.environ.get(CHROME_ZIP_ENV) or "")
    version = "local"
    source = "local-zip"
    zip_file: Path
    if zip_path or (os.environ.get(CHROME_ZIP_ENV) or "").strip():
        if not local_zip.exists():
            raise ChromeInstallError(f"离线安装包不存在：{local_zip}")
        zip_file = local_zip
        _report(progress, "prepare", 2, f"使用离线安装包：{zip_file.name}")
    else:
        _report(progress, "metadata", 1, "正在获取 Chrome 下载信息…")
        version, url = _resolve_download()
        source = url
        filename = url.rsplit("/", 1)[-1] or f"chrome-{platform_key()}.zip"
        zip_file = target / ".download" / filename
        if not zip_file.exists():
            _download(url, zip_file, progress, timeout=timeout)

    final_dir = target / (version or "local")
    if final_dir.exists():
        _report(progress, "extract", 76, "正在清理旧版本…")
        shutil.rmtree(final_dir, ignore_errors=True)
    _report(progress, "extract", 78, "正在解压 Chrome…")
    try:
        _safe_extract(zip_file, final_dir)
    except Exception:
        shutil.rmtree(final_dir, ignore_errors=True)
        raise
    executable = find_executable(final_dir)
    if not executable:
        shutil.rmtree(final_dir, ignore_errors=True)
        raise ChromeInstallError("解压完成但没有找到 Chrome 可执行文件，请确认压缩包是否正确")
    _make_executable(executable)
    # zipfile.extractall 不保留 Unix 可执行位；Chrome 的 crashpad handler 无执行权限会直接崩溃
    for sibling in ("chrome_crashpad_handler", "chrome_sandbox", "chromedriver"):
        candidate = executable.parent / sibling
        if candidate.exists():
            _make_executable(candidate)
    probe = _probe_launch(executable)

    info = {
        "ok": True,
        "found": True,
        "version": version,
        "platform": platform_key(),
        "path": str(executable),
        "source": source,
        "target": str(target),
        "installed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if probe:
        info["probe"] = probe
    current = target / "current.json"
    current.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    _report(progress, "done", 100, f"Chrome 已安装：{executable}")
    return info


__all__ = [
    "ChromeInstallError",
    "CHROME_ZIP_ENV",
    "platform_key",
    "install_supported",
    "default_target_dir",
    "install_status",
    "install_chrome",
    "find_executable",
]
