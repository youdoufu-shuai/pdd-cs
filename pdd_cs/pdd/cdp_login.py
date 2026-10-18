# -*- coding: utf-8 -*-
"""扫码登录助手：用本机 Chrome + CDP 获取拼多多商家后台登录 Cookie。

思路：
    启动一个独立的 Chrome 用户目录（不影响你日常浏览器），打开拼多多商家后台，
    用户自己扫码/账号登录；程序通过 Chrome DevTools Protocol 读取登录后的 Cookie
    并保存，之后客服服务直接复用这份登录态。

为什么不做「账号密码自动登录」：
    拼多多商家后台登录通常需要短信/扫码验证，自动填账号密码既复杂又容易触发
    风控；让用户在自己的浏览器里登录一次，是最稳、也最合规的做法。
"""
from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import socket
import subprocess
import threading
import sys
import time
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from websockets.sync.client import connect as ws_connect

from .auth import CookieStore, validate_cookies

logger = logging.getLogger("pdd_cs.login")

DEFAULT_START_URL = "https://mms.pinduoduo.com/chat-merchant/index"

#: 服务器无桌面时用桌面 Chrome UA，避免拼多多把 HeadlessChrome 识别为不安全浏览器
HEADLESS_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
)


class LoginError(RuntimeError):
    """登录助手执行失败。"""


# ---------------------------------------------------------------------------
# Chrome 查找与启动
# ---------------------------------------------------------------------------
#: 支持用环境变量显式指定 Chrome / Chromium / Edge 可执行文件
_CHROME_ENV_VARS = (
    "PDD_CS_CHROME",
    "PDD_CS_CHROME_PATH",
    "CHROME_PATH",
    "CHROME_BIN",
    "GOOGLE_CHROME_BIN",
)


def _project_chrome_candidates() -> list[str]:
    """项目内 `data/chrome/` 下由 `python -m pdd_cs chrome --install` 下载的 Chrome。"""
    try:
        from ..config import PROJECT_ROOT
    except Exception:  # noqa: BLE001 - 导入失败时忽略本地 Chrome
        return []
    base = PROJECT_ROOT / "data" / "chrome"
    result: list[str] = []
    current = base / "current.json"
    if current.exists():
        try:
            data = json.loads(current.read_text(encoding="utf-8"))
            path = str(data.get("path") or "")
            if path:
                result.append(path)
        except (OSError, json.JSONDecodeError):
            pass
    if base.exists():
        for name in ("chrome.exe", "chrome", "Google Chrome for Testing"):
            try:
                result.extend(str(item) for item in base.rglob(name) if item.is_file())
            except OSError:
                continue
    return result


def _env_chrome_candidates() -> list[str]:
    result: list[str] = []
    for name in _CHROME_ENV_VARS:
        value = (os.environ.get(name) or "").strip()
        if not value:
            continue
        path = Path(value)
        if path.is_dir():
            result.extend(str(path / exe) for exe in ("chrome.exe", "chrome", "msedge.exe"))
        else:
            result.append(value)
    return result


def _windows_registry_chrome() -> list[str]:
    if not sys.platform.startswith("win"):
        return []
    result: list[str] = []
    try:
        import winreg

        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for exe in ("chrome.exe", "msedge.exe"):
                try:
                    key = winreg.OpenKey(
                        root,
                        rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}",
                    )
                    value, _ = winreg.QueryValueEx(key, "")
                    if value:
                        result.append(str(value))
                except OSError:
                    continue
    except Exception:  # noqa: BLE001 - 非 Windows / 权限受限
        return []
    return result


def _display_available() -> bool:
    """当前环境是否有图形界面（Linux 服务器通常没有 DISPLAY）。"""
    if sys.platform.startswith("win") or sys.platform == "darwin":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def display_available() -> bool:
    """公开版本：当前环境是否有图形界面。"""
    return _display_available()


def _windows_chrome_version() -> str:
    """Windows 上不要执行 chrome --version（会拉起窗口），改读注册表/文件版本。"""
    try:
        import winreg

        for root, sub in (
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Google\Chrome\BLBeacon"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Google\Chrome\BLBeacon"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Google\Chrome\BLBeacon"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Edge\BLBeacon"),
        ):
            try:
                key = winreg.OpenKey(root, sub)
                value, _ = winreg.QueryValueEx(key, "version")
                if value:
                    return str(value)
            except OSError:
                continue
    except Exception:  # noqa: BLE001
        pass
    return ""


def _project_chrome_version(path: str) -> str:
    try:
        from ..config import PROJECT_ROOT

        current = PROJECT_ROOT / "data" / "chrome" / "current.json"
        if current.exists():
            data = json.loads(current.read_text(encoding="utf-8"))
            stored_path = str(data.get("path") or "")
            if stored_path and Path(stored_path).resolve() == Path(path).resolve():
                return str(data.get("version") or "")
    except Exception:  # noqa: BLE001
        pass
    return ""


def chrome_version(path: str) -> str:
    """读取 Chrome/Chromium/Edge 版本；读取失败返回空字符串。"""
    project_version = _project_chrome_version(path)
    if project_version:
        return project_version
    if sys.platform.startswith("win"):
        return _windows_chrome_version()
    try:
        proc = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=6,
        )
        lines = ((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()
        return lines[0].strip() if lines else ""
    except Exception:  # noqa: BLE001 - 版本只用于展示
        return ""


def _chrome_source(path: str) -> str:
    try:
        env_paths = {
            str(Path(value).resolve())
            for value in _env_chrome_candidates()
            if Path(value).exists()
        }
        if str(Path(path).resolve()) in env_paths:
            return "env"
        from ..config import PROJECT_ROOT

        local = (PROJECT_ROOT / "data" / "chrome").resolve()
        if local in Path(path).resolve().parents or Path(path).resolve() == local:
            return "project"
    except Exception:  # noqa: BLE001
        pass
    return "system"


def chrome_status(explicit: Optional[str] = None) -> dict:
    """Chrome 检测结果：是否找到、路径、版本、来源、是否有图形界面。"""
    display = _display_available()
    try:
        path = find_chrome(explicit)
    except LoginError as exc:
        return {
            "found": False,
            "runnable": False,
            "path": "",
            "version": "",
            "source": "",
            "display_available": display,
            "message": str(exc),
        }
    version = chrome_version(path)
    lowered = version.lower()
    broken = any(
        token in lowered
        for token in ("error while loading shared libraries", "cannot open shared object", "not found")
    )
    if broken:
        message = (
            "已检测到 Chrome，但启动失败（通常是 Linux 缺少运行库）。"
            "可安装 libnss3 等依赖后重试，或改用「导入登录态」。"
        )
    elif display:
        message = "已检测到 Chrome"
    else:
        message = (
            "已检测到 Chrome，但当前没有图形界面（DISPLAY），无法弹出扫码窗口。"
            "请在本机登录后导出登录态，再到服务器导入；或在服务器配置 Xvfb/VNC。"
        )
    return {
        "found": True,
        "runnable": not broken,
        "path": path,
        "version": version,
        "source": _chrome_source(path),
        "display_available": display,
        "message": message,
    }


def find_chrome(explicit: Optional[str] = None) -> str:
    """定位 Chrome / Chromium / Edge 可执行文件（优先环境变量与项目内下载）。"""
    if explicit:
        path = Path(explicit)
        if path.exists():
            return str(path)
        raise LoginError(f"指定的 Chrome 不存在: {explicit}")

    candidates: list[str] = []
    candidates += _env_chrome_candidates()
    # 优先使用用户通过「一键下载」装到项目内的 Chrome（可覆盖系统旧版本）
    candidates += _project_chrome_candidates()

    if sys.platform.startswith("win"):
        for env_name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(env_name)
            if base:
                candidates.append(str(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"))
                candidates.append(str(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"))
        candidates += [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ]
        candidates += _windows_registry_chrome()
    elif sys.platform == "darwin":
        candidates += [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            str(Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ]
    else:
        candidates += [
            "/opt/google/chrome/chrome",
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/snap/bin/chromium",
        ]
        for name in (
            "google-chrome",
            "google-chrome-stable",
            "chromium",
            "chromium-browser",
            "microsoft-edge",
            "microsoft-edge-stable",
        ):
            found = shutil.which(name)
            if found:
                candidates.append(found)

    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    for name in ("chrome", "google-chrome", "chromium", "msedge", "microsoft-edge"):
        found = shutil.which(name)
        if found:
            return found
    raise LoginError(
        "未找到 Chrome/Chromium/Edge：可以运行 `python -m pdd_cs chrome --install` 一键下载，"
        "或用 --chrome 指定可执行文件路径。"
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def launch_chrome(
    chrome_path: str,
    user_data_dir: Path,
    debug_port: int,
    start_url: str = DEFAULT_START_URL,
    *,
    headless: bool = False,
) -> subprocess.Popen:
    """以远程调试模式启动 Chrome（独立用户目录）。"""
    user_data_dir.mkdir(parents=True, exist_ok=True)
    args = [
        chrome_path,
        f"--remote-debugging-port={debug_port}",
        f"--user-data-dir={user_data_dir}",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-popup-blocking",
    ]
    if headless:
        args += [
            "--headless=new",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--no-zygote",
            "--hide-scrollbars",
            "--window-size=1280,900",
            f"--user-agent={HEADLESS_USER_AGENT}",
            "--disable-blink-features=AutomationControlled",
        ]
    args.append(start_url)
    logger.info("启动 Chrome: %s", chrome_path)
    creationflags = 0
    if sys.platform.startswith("win"):
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    env = os.environ.copy()
    if headless:
        # Chrome 的 crashpad 需要一个可写的 HOME；systemd 的 ProtectHome/只读 HOME 会导致启动崩溃
        home_value = env.get("HOME") or ""
        writable = False
        if home_value:
            try:
                writable = os.path.isdir(home_value) and os.access(home_value, os.W_OK)
            except OSError:
                writable = False
        if not writable:
            fallback_home = user_data_dir.parent / "chrome-home"
            fallback_home.mkdir(parents=True, exist_ok=True)
            env["HOME"] = str(fallback_home)
            env["XDG_CONFIG_HOME"] = str(fallback_home / ".config")
            env["XDG_CACHE_HOME"] = str(fallback_home / ".cache")
            (fallback_home / ".config").mkdir(exist_ok=True)
            (fallback_home / ".cache").mkdir(exist_ok=True)
    return subprocess.Popen(args, creationflags=creationflags, env=env)


def _http_json(url: str, method: str = "GET", timeout: float = 3.0):
    request = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def wait_for_debugger(port: int, timeout: float = 30.0) -> dict:
    """等待 Chrome 调试端口就绪。"""
    deadline = time.time() + timeout
    last_error: Optional[Exception] = None
    while time.time() < deadline:
        try:
            return _http_json(f"http://127.0.0.1:{port}/json/version")
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(0.5)
    raise LoginError(f"Chrome 调试端口未就绪（{last_error}）")


def find_pdd_target(port: int) -> dict:
    """找到拼多多商家后台页面目标。"""
    try:
        targets = _http_json(f"http://127.0.0.1:{port}/json")
    except Exception as exc:  # noqa: BLE001
        raise LoginError(f"读取 Chrome 页面列表失败: {exc}") from exc
    pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    for target in pages:
        if "pinduoduo.com" in str(target.get("url", "")):
            return target
    if pages:
        return pages[0]
    # 没有页面时新建一个
    try:
        return _http_json(f"http://127.0.0.1:{port}/json/new?url={DEFAULT_START_URL}", method="PUT")
    except Exception as exc:  # noqa: BLE001
        raise LoginError(f"没有可用的浏览器页面: {exc}") from exc


# ---------------------------------------------------------------------------
# CDP 交互
# ---------------------------------------------------------------------------
class CDP:
    """极简 CDP 客户端（同步）。"""

    def __init__(self, ws_url: str):
        self.ws = ws_connect(ws_url, max_size=8 * 1024 * 1024, open_timeout=10)
        self._next_id = 0

    def call(self, method: str, params: Optional[dict] = None, timeout: float = 10.0):
        self._next_id += 1
        msg_id = self._next_id
        self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            message = json.loads(self.ws.recv(timeout=max(0.1, deadline - time.time())))
            if message.get("id") == msg_id:
                if "error" in message:
                    raise LoginError(f"CDP 调用失败 {method}: {message['error']}")
                return message.get("result", {})
        raise LoginError(f"CDP 调用超时: {method}")

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001
            pass


def read_pdd_cookies(cdp: CDP) -> dict[str, str]:
    """读取浏览器里 pinduoduo.com 域下的 Cookie。"""
    result = cdp.call("Network.getAllCookies")
    cookies: dict[str, str] = {}
    for item in result.get("cookies", []):
        domain = str(item.get("domain", ""))
        if "pinduoduo.com" not in domain:
            continue
        name = item.get("name")
        if not name:
            continue
        cookies[str(name)] = str(item.get("value", ""))
    return cookies


def _looks_logged_in(cookies: dict[str, str]) -> bool:
    return any(key in cookies for key in ("PASS_ID", "SUB_PASS_ID", "MSA", "SUB_SYS_ID"))


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------
def login_with_browser(
    cookie_store: CookieStore,
    *,
    chrome_path: Optional[str] = None,
    start_url: str = DEFAULT_START_URL,
    timeout_seconds: int = 300,
    keep_open: bool = False,
    user_data_dir: Optional[Path] = None,
    poll_seconds: float = 2.0,
) -> dict[str, str]:
    """打开 Chrome 让用户登录，登录成功后保存 Cookie。

    Args:
        cookie_store: Cookie 保存位置。
        chrome_path: Chrome 路径（默认自动查找）。
        start_url: 起始页面。
        timeout_seconds: 等待登录的最长时间。
        keep_open: 保存后是否保持浏览器打开。
        user_data_dir: Chrome 用户目录（默认 data/chrome-profile）。

    Returns:
        保存的 Cookie 字典。
    """
    chrome = find_chrome(chrome_path)
    port = _free_port()
    profile = user_data_dir or (cookie_store.path.parent.parent.parent / "data" / "chrome-profile")
    process = launch_chrome(chrome, profile, port, start_url)
    cdp: Optional[CDP] = None
    try:
        wait_for_debugger(port)
        target = find_pdd_target(port)
        cdp = CDP(target["webSocketDebuggerUrl"])
        cdp.call("Network.enable", {})

        print("\n" + "=" * 62)
        print("  请在弹出的 Chrome 窗口里登录拼多多商家后台")
        print("  登录成功后程序会自动保存登录态，无需手动复制 Cookie")
        print(f"  最长等待 {timeout_seconds // 60} 分钟，完成后会自动关闭浏览器")
        print("=" * 62 + "\n")

        deadline = time.time() + timeout_seconds
        cookies: dict[str, str] = {}
        last_print = 0.0
        while time.time() < deadline:
            cookies = read_pdd_cookies(cdp)
            if _looks_logged_in(cookies):
                problems = [p for p in validate_cookies(cookies) if "缺少部分" in p]
                cookie_store.save_text("; ".join(f"{k}={v}" for k, v in cookies.items()))
                print(f"✅ 登录成功，已保存 {len(cookies)} 个 Cookie")
                for problem in problems:
                    print(f"   ⚠️  {problem}")
                return cookies
            if time.time() - last_print > 15:
                print("   ...等待登录中（如已登录请确认页面是 mms.pinduoduo.com）")
                last_print = time.time()
            time.sleep(poll_seconds)

        raise LoginError(
            "等待登录超时。请重新运行 login，并在浏览器里完成扫码/账号登录后再等待几秒。"
        )
    finally:
        if cdp:
            cdp.close()
        if not keep_open:
            try:
                process.terminate()
                process.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass


_QR_CLIP_JS = """(() => {
  const candidates = [...document.querySelectorAll('canvas')].filter(el => {
    const r = el.getBoundingClientRect();
    return el.width >= 80 && el.height >= 80 && r.width > 0 && r.height > 0;
  });
  if (!candidates.length) return null;
  const rect = candidates[0].getBoundingClientRect();
  return {x: Math.max(0, rect.x - 8), y: Math.max(0, rect.y - 8),
          width: rect.width + 16, height: rect.height + 16};
})()"""


def login_with_browser_headless(
    cookie_store: CookieStore,
    *,
    chrome_path: Optional[str] = None,
    start_url: str = DEFAULT_START_URL,
    timeout_seconds: int = 600,
    user_data_dir: Optional[Path] = None,
    poll_seconds: float = 2.0,
    on_qr: Optional[Callable[[bytes], None]] = None,
    cancel_event: Optional[threading.Event] = None,
) -> dict[str, str]:
    """无桌面服务器扫码登录：headless Chrome 打开登录页，定时回报二维码截图。

    Args:
        cookie_store: Cookie 保存位置。
        chrome_path: Chrome 路径（默认自动查找）。
        start_url: 登录页地址。
        timeout_seconds: 最长等待扫码时间。
        user_data_dir: Chrome 用户目录。
        poll_seconds: 轮询 Cookie 间隔。
        on_qr: 每次截到登录页图片时回调（PNG bytes），用于网页面板展示二维码。
        cancel_event: 外部取消信号；用户关闭弹窗/重新生成时用于尽快停止 Chrome。

    Returns:
        保存的 Cookie 字典。
    """
    chrome = find_chrome(chrome_path)
    port = _free_port()
    profile = user_data_dir or (cookie_store.path.parent.parent.parent / "data" / "chrome-profile")
    process = launch_chrome(chrome, profile, port, start_url, headless=True)
    cdp: Optional[CDP] = None
    try:
        wait_for_debugger(port)
        target = find_pdd_target(port)
        cdp = CDP(target["webSocketDebuggerUrl"])
        cdp.call("Network.enable", {})
        cdp.call("Page.enable", {})
        logger.info("无桌面扫码登录已启动，等待扫码…")

        started_at = time.time()
        deadline = started_at + timeout_seconds
        last_shot = 0.0
        while time.time() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                raise LoginError("登录已取消")
            cookies = read_pdd_cookies(cdp)
            if _looks_logged_in(cookies):
                problems = [p for p in validate_cookies(cookies) if "缺少部分" in p]
                cookie_store.save_text("; ".join(f"{k}={v}" for k, v in cookies.items()))
                logger.info("扫码登录成功，已保存 %s 个 Cookie", len(cookies))
                for problem in problems:
                    logger.warning(problem)
                return cookies
            now = time.time()
            if on_qr is not None and now - last_shot >= 1.5:
                try:
                    params = {"format": "png", "captureBeyondViewport": True}
                    try:
                        clip_result = cdp.call(
                            "Runtime.evaluate",
                            {"expression": _QR_CLIP_JS, "returnByValue": True},
                            timeout=5,
                        )
                        clip = clip_result.get("result", {}).get("value")
                    except Exception:  # noqa: BLE001
                        clip = None
                    if clip:
                        params["clip"] = {
                            "x": clip["x"],
                            "y": clip["y"],
                            "width": clip["width"],
                            "height": clip["height"],
                            "scale": 3,
                        }
                    elif time.time() - started_at < 20:
                        # 二维码还没渲染出来：先不传整页截图，等 canvas 出现
                        last_shot = now
                        continue
                    else:
                        params["captureBeyondViewport"] = False
                    shot = cdp.call("Page.captureScreenshot", params, timeout=15)
                    data = base64.b64decode(shot.get("data") or "")
                    if data:
                        on_qr(data)
                except Exception as exc:  # noqa: BLE001 - 截图失败不阻断扫码
                    logger.debug("二维码截图失败: %s", exc)
                last_shot = now
            time.sleep(poll_seconds)

        raise LoginError("等待扫码超时，请重新生成二维码再试")
    finally:
        if cdp:
            cdp.close()
        try:
            process.terminate()
            process.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass


def save_cookie_text(cookie_store: CookieStore, text: str, shop_name: str = "") -> dict[str, str]:
    """直接从粘贴的 Cookie 文本保存（浏览器登录失败时的备选方案）。"""
    bundle = cookie_store.save_text(text, shop_name=shop_name)
    problems = validate_cookies(bundle.cookies)
    warning = [p for p in problems if "缺少部分" in p]
    if warning:
        logger.warning("；".join(warning))
    return bundle.cookies


__all__ = [
    "login_with_browser",
    "login_with_browser_headless",
    "save_cookie_text",
    "find_chrome",
    "chrome_status",
    "chrome_version",
    "LoginError",
]
