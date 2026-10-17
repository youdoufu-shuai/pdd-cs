#!/usr/bin/env bash
# ===========================================================================
# 拼多多多店铺智能客服 · 一键启动（macOS / Linux）
# ---------------------------------------------------------------------------
# 用法：
#   bash start.sh            # 自动装依赖、生成配置、启动面板
#   bash start.sh --check    # 只检查 Python 环境
# ===========================================================================
set -euo pipefail
cd "$(dirname "$0")"

echo "============================================================"
echo "   拼多多多店铺智能客服 · 一键启动"
echo "============================================================"

# ---------- 1. 找 Python ----------
PY=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    PY="$candidate"
    break
  fi
done
if [ -z "$PY" ]; then
  echo "[错误] 没有找到 Python。"
  echo "请先安装 Python 3.10+：https://www.python.org/downloads/"
  exit 1
fi
if ! "$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)'; then
  echo "[错误] Python 版本过低，需要 3.10 以上。"
  "$PY" --version
  exit 1
fi
echo "[OK] Python：$PY ($("$PY" --version 2>&1))"

if [ "${1:-}" = "--check" ]; then
  echo "[OK] start.sh 自检通过"
  exit 0
fi

# ---------- 2. 虚拟环境 + 依赖 ----------
if [ ! -x ".venv/bin/python" ]; then
  echo "[1/3] 首次运行：创建虚拟环境..."
  "$PY" -m venv .venv
fi
VPY=".venv/bin/python"

echo "[2/3] 检查依赖（首次需要下载，请稍等）..."
if ! "$VPY" -m pip install --disable-pip-version-check -q -r requirements.txt; then
  echo "[提示] 直连下载失败，改用国内镜像重试..."
  "$VPY" -m pip install --disable-pip-version-check -q -r requirements.txt \
    -i https://pypi.tuna.tsinghua.edu.cn/simple
fi

# ---------- 3. 配置 ----------
if [ ! -f "config/config.yaml" ]; then
  echo "[3/3] 生成默认配置..."
  "$VPY" -m pdd_cs setup --yes
fi
mkdir -p config/secrets

# ---------- 启动 ----------
PORT="$("$VPY" -c 'from pdd_cs.config import Config; print(Config.load().server.port)')"
echo
echo "面板地址：http://127.0.0.1:${PORT}"
echo "按 Ctrl+C 可以停止程序。"
echo
exec "$VPY" -m pdd_cs run
