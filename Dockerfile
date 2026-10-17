# syntax=docker/dockerfile:1
# 拼多多多店铺智能客服：生产镜像
#
# 默认端口 38617（少见高位端口）；可用环境变量覆盖：
#   docker run -e PDD_CS_PORT=38618 -e PDD_CS_AUTH_TOKEN=xxxx ...
# 注意：容器内绑定 0.0.0.0，必须设置 PDD_CS_AUTH_TOKEN（或用反向代理鉴权）。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    PDD_CS_HOST=0.0.0.0 \
    PDD_CS_PORT=38617

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY pdd_cs ./pdd_cs
COPY config ./config
COPY mock ./mock
COPY pyproject.toml README.md ./

RUN mkdir -p /app/data /app/config/secrets \
    && useradd --create-home --uid 1000 pddcs \
    && chown -R pddcs:pddcs /app

USER pddcs

VOLUME ["/app/data", "/app/config/secrets"]
EXPOSE 38617

# SIGINT 让 Python 走优雅退出（关闭长连接、落库、停面板）
STOPSIGNAL SIGINT

# strict 健康检查：任一店铺掉线即 unhealthy
HEALTHCHECK --interval=60s --timeout=8s --start-period=40s --retries=3 \
    CMD python -c "import os,urllib.request;port=os.environ.get('PDD_CS_PORT','38617');urllib.request.urlopen('http://127.0.0.1:'+port+'/healthz?strict=1', timeout=6)"

CMD ["python", "-m", "pdd_cs", "run"]
