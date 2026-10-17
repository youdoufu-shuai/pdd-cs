#!/usr/bin/env bash
# ===========================================================================
# 拼多多多店铺智能客服 · 服务器一键部署（Docker）
# ---------------------------------------------------------------------------
# 用法（在服务器项目根目录）：
#   bash deploy/install.sh
#   PDD_CS_PORT=41000 bash deploy/install.sh    # 自定义端口
#
# 脚本会：
#   1. 检查 Docker / docker compose；
#   2. 首次运行时从 config.example.yaml 生成 config/config.yaml；
#   3. 生成 deploy/.env（含随机访问令牌和少见端口 38617，权限 600）；
#   4. 构建并启动容器，打印访问地址与 SSH 隧道命令。
#
# 登录 Cookie 需要先在本地电脑登录后上传到 config/secrets/（详见 docs/deployment.md）。
# ===========================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "==> 检查 Docker"
if ! command -v docker >/dev/null 2>&1; then
  echo "❌ 未安装 Docker。Ubuntu 可执行：sudo apt install -y docker.io docker-compose-v2"
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "❌ 需要 docker compose v2（插件形式）。"
  exit 1
fi

echo "==> 准备配置目录"
mkdir -p config/secrets data
if [ ! -f config/config.yaml ]; then
  cp config/config.example.yaml config/config.yaml
  echo "已生成 config/config.yaml（请按需修改 shops / llm / alerts / monitor）"
fi

if [ ! -f deploy/.env ]; then
  cp deploy/.env.example deploy/.env
  TOKEN="$(python3 -c "import secrets;print(secrets.token_urlsafe(32))" 2>/dev/null || openssl rand -hex 24)"
  PORT="${PDD_CS_PORT:-38617}"
  sed -i "s|^PDD_CS_PORT=.*|PDD_CS_PORT=${PORT}|" deploy/.env
  sed -i "s|^PDD_CS_AUTH_TOKEN=.*|PDD_CS_AUTH_TOKEN=${TOKEN}|" deploy/.env
  chmod 600 deploy/.env
  echo "已生成 deploy/.env（端口 ${PORT}，随机访问令牌）"
else
  echo "复用已有 deploy/.env"
fi

# shellcheck disable=SC1091
set -a
. deploy/.env
set +a
PORT="${PDD_CS_PORT:-38617}"
TOKEN="${PDD_CS_AUTH_TOKEN:-}"
if [ -z "$TOKEN" ]; then
  echo "❌ deploy/.env 里 PDD_CS_AUTH_TOKEN 为空（容器内绑定 0.0.0.0 时必须设置）"
  exit 1
fi
if [ -d config/secrets ] && [ -z "$(ls -A config/secrets 2>/dev/null)" ]; then
  echo "⚠️  config/secrets/ 为空：还没有上传店铺 Cookie，启动后会提示所有店铺未登录。"
fi

echo "==> 构建并启动（首次构建约 1-2 分钟）"
cd deploy
docker compose up -d --build
sleep 2
docker compose ps || true

cat <<EOF

============================================================
 启动完成
------------------------------------------------------------
 面板端口：${PORT}
 访问令牌：${TOKEN}

 本地电脑先建 SSH 隧道：
   ssh -L ${PORT}:127.0.0.1:${PORT} 用户@服务器

 然后浏览器打开：
   http://127.0.0.1:${PORT}/?token=${TOKEN}

 看日志：
   cd deploy && docker compose logs -f
 健康检查：
   curl -s "http://127.0.0.1:${PORT}/healthz?strict=1"
============================================================
EOF
