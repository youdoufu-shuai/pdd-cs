"""Read-only safety check for files prepared for publication. Never print matches."""
from __future__ import annotations

import argparse
import io
import re
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = {
    "config/config.example.yaml",
    "pdd_cs/templates/config/config.example.yaml",
}
LOCAL_DIRS = {"data", "data-demo", "outputs", "chrome-shot"}
LOCAL_SUFFIXES = {".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3", ".pem", ".key", ".har"}
EXTENSION_FILES = {"README.md", "manifest.json", "popup.html", "popup.js"}
SIGNATURES = tuple(map(re.compile, (
    rb"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----",
    rb"\bgh[pousr]_[A-Za-z0-9]{36,}\b",
    rb"\bgithub_pat_[A-Za-z0-9_]{60,}\b",
    rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
    rb"\bxox[baprs]-[A-Za-z0-9-]{24,}\b",
    rb"\bsk-(?:proj-|svcacct-)[A-Za-z0-9_-]{30,}\b",
    rb"\bPDDCS[12][._:][A-Za-z0-9_-]{40,}",
)))


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(ROOT), *args])


def private_path(name: str) -> bool:
    path = PurePosixPath(name)
    lower = path.name.lower()
    return (
        name in {"config/config.yaml", "config/knowledge/product-catalog.md"}
        or name.startswith("config/secrets/") and path.name != ".gitkeep"
        or bool(LOCAL_DIRS.intersection(path.parts[:-1]))
        or any("chrome-profile" in part or part.startswith("chrome-shot") for part in path.parts)
        or lower.startswith(("pdd-login-", ".tmp_pdd_"))
        or "登录信息" in name
        or lower == ".env" or lower.startswith(".env.") and lower != ".env.example"
        or lower.endswith((".local.yaml", ".local.json", ".bak"))
        or path.suffix.lower() in LOCAL_SUFFIXES
        or ".sqlite-" in lower or ".sqlite3-" in lower
    )


def inspect(name: str, data: bytes) -> list[str]:
    if name in SAMPLES:
        for field, blank in ((b"email_to", rb"\[\]"), (b"sms_phone", rb'(?:""|\x27\x27)')):
            rows = re.findall(rb"(?m)^  " + field + rb":[ \t]*(.*)$", data)
            if len(rows) != 1 or not re.fullmatch(blank + rb"[ \t]*(?:#.*)?", rows[0]):
                return ["示例配置的通知联系方式必须为空"]
    if name == "browser-extension.zip":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = zf.namelist()
                if len(names) != len(EXTENSION_FILES) or set(names) != EXTENSION_FILES:
                    return ["浏览器扩展归档含未审核文件，或缺少发布文件"]
                if any(item.file_size > 1_000_000 for item in zf.infolist()):
                    return ["浏览器扩展归档含过大的条目"]
                if any(any(pattern.search(payload) for pattern in SIGNATURES)
                       for payload in map(zf.read, names)):
                    return ["浏览器扩展归档含疑似凭据"]
        except (OSError, RuntimeError, zipfile.BadZipFile):
            return ["浏览器扩展归档无法读取"]
    elif b"\x00" not in data[:8192] and any(pattern.search(data) for pattern in SIGNATURES):
        return ["存在疑似凭据；请核对并移除后再发布"]
    return []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true", help="只读取准备提交的文件在索引中的版本")
    args = parser.parse_args()
    names = git(*(("diff", "--cached", "--name-only", "--diff-filter=ACMRT", "-z")
                  if args.staged else ("ls-files", "-z")))
    bad = 0
    for raw in dict.fromkeys(filter(None, names.split(b"\0"))):
        name = raw.decode("utf-8", "surrogateescape")
        if private_path(name):
            problems = ["发现本地配置、运行数据或登录导出路径"]
        elif args.staged:
            problems = inspect(name, git("show", ":" + name))
        elif (ROOT / name).is_symlink():
            problems = ["不会读取符号链接目标；请人工核对"]
        else:
            path = ROOT / name
            problems = inspect(name, path.read_bytes()) if path.is_file() else []
        if problems:
            bad += 1
            print(f"{name!r}: {'；'.join(problems)}", file=sys.stderr)
    if not bad:
        print("发布检查通过（不包含图像、历史和外部副本审计）")
    return bool(bad)


if __name__ == "__main__":
    raise SystemExit(main())
