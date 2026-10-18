# 部署到服务器 & 掉线通知

本文回答两个问题：

1. 店铺掉线 / 登录过期 / 进程挂了，**怎么通知到我**？
2. 这个项目**能不能放到服务器上**，怎么放？

---

## 零、先明确：本地电脑能不能关机？

**不能。** 这个项目是一个本地常驻程序，不是云端 SaaS：

- 电脑关机 / 睡眠 / 合盖 / 断网 / 系统更新重启 → 进程停止 → 拼多多长连接断开；
- 这段时间客户发来的消息**不会自动回复**，客户只能干等（平台侧消息不会丢，但首响、回复率、
  负体验考核会受影响）；
- 重新开机后程序会重连并继续接待，但已经错过的消息不会补发。

所以结论很直接：

| 运行位置 | 关机/断网后 | 适合 |
| --- | --- | --- |
| 自己电脑（本机） | ❌ 停止接待 | 试用、调试、白天有人值守的工位 |
| 云服务器 | ✅ 7×24 运行（除非主动关机） | **正式长期使用（推荐）** |
| 公司常开电脑 / 迷你主机 / NAS | ✅ 只要不断电断网 | 不想买云服务器、有稳定网络和 UPS |
| 树莓派等小设备 | ⚠️ 能跑，但网络稳定性和维护成本一般 | 折腾/备用，不建议做主方案 |

如果店铺是 7×24 接待（拼多多客服基本如此），**必须放在一台常开的机器上**。云服务器最低配
1C1G 一年通常几百元，比起漏消息导致的回复率/负体验考核，成本很低。

## 一、掉线了怎么通知你

### 1. 三层通知，覆盖不同故障

| 故障 | 谁来发现 | 怎么通知 |
| --- | --- | --- |
| 店铺长连接断开、登录态失效 | 程序自己（10 秒巡检） | Webhook → 钉钉 / 飞书 / 企业微信群 |
| 转人工失败、客户超时没人回 | 程序自己 | 同上（可单独开关事件） |
| **进程崩溃 / 服务器宕机 / 断网** | **程序自己发不出消息** | 外部监控收不到“服务心跳” → 打电话/发短信 |

所以正确姿势是 **Webhook（写） + 心跳（反向证明活着）** 两条腿走路。

### 2. Webhook 告警（程序还活着时）

在 `config/config.yaml` 里填一个群机器人地址即可：

```yaml
alerts:
  enabled: true
  webhook_url: https://oapi.dingtalk.com/robot/send?access_token=xxxx
  format: dingtalk              # generic | dingtalk | feishu | wecom
  events:
    - shop_offline              # 店铺掉线（重点）
    - shop_online               # 恢复通知
    - session_expired           # 登录态过期
    - connection_error
    - transfer_failed
    - sla_overdue               # 客户等待超时
  min_interval_seconds: 60
  offline_after_seconds: 120    # 掉线超过 2 分钟开始告警
  offline_repeat_seconds: 1800  # 一直没恢复：每 30 分钟再提醒一次
```

- 钉钉群：群设置 → 机器人 → 添加自定义机器人 → 复制 Webhook；
- 飞书群：群设置 → 群机器人 → 自定义机器人；
- 企业微信群：群设置 → 群机器人 → 添加；
- 自建/其它：`format: generic`，收到 `{"title","text","event","shop_id","at"}` JSON。

改完先测一下：

```bash
python -m pdd_cs alert-test
```

钉钉/飞书如果开了“签名校验”，把加签参数直接拼进 `webhook_url` 即可。

### 3. 进程级心跳（进程挂了也能通知）

程序每 5 分钟向一个外部地址报一次“我还活着”。外部监控超过 15 分钟没收到就报警——这样
进程崩溃、服务器宕机、断网都能被发现。

推荐用现成服务（都支持 Webhook 告警到钉钉/飞书/企微/短信）：

- **Healthchecks.io**（最省事）：新建 Check，周期 5 分钟，复制 ping URL 填到配置；
- **Uptime Kuma**（自建，免费）：新建 HTTP(s) 监控，URL 填 `http://你的服务器:38617/healthz?strict=1`；
- 阿里云云监控 / 腾讯云拨测：拨测 `/healthz?strict=1`，状态码非 200 即告警。

配置：

```yaml
monitor:
  heartbeat_url: https://hc-ping.com/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
  heartbeat_interval_seconds: 300
  heartbeat_timeout_seconds: 10
  heartbeat_method: POST        # Healthchecks.io 用 POST 或 GET 都行
```

### 4. 健康检查接口

| 接口 | 行为 |
| --- | --- |
| `GET /healthz` | 进程活着就 200（liveness） |
| `GET /healthz?strict=1` | 所有店铺都连接才 200；有店铺掉线/启动失败返回 **503**（readiness） |

外部监控建议监控 **strict** 版本，这样“进程活着但店铺全掉线”也能报警。
`docker ps` / Docker healthcheck 默认就用了 strict 版本。

### 5. 进程守护（挂了自动拉起）

- systemd：`deploy/pdd-cs.service`，`Restart=always`，5 秒后自动重启；
- Docker：`restart: unless-stopped`；
- 所有店铺登录都失效时 `run` 会以非 0 退出，配合守护进程会自动重启，日志里能看到原因，
  同时你会收到 `session_expired` 告警。

---

## 二、能不能放服务器上

**可以，而且推荐。** 但有几个和本地运行不一样的坑，按下面做就行。

### 1. 先选部署方式

| 方式 | 适合 | 优点 | 注意 |
| --- | --- | --- | --- |
| 本地电脑 / 公司常开电脑 | 1–2 个店先试水 | 登录方便、IP 稳定 | 断电断网就停，需开机自启 |
| Windows 服务器（带桌面） | 习惯 Windows 的团队 | 可以直接 RDP 里扫码登录 | 占资源，建议 2C4G 起 |
| Linux 服务器 + Docker | 多店铺、正式长期运行 | 稳定、省资源、易备份升级 | 登录在本机完成后导出登录态，导入服务器；服务器免 Chrome |

资源参考（本机实测）：**服务进程 1 店约 40 MB、5 店约 42 MB**（含面板与 SQLite），
**512 MB 内存 / 1 核 / 10G 磁盘**就能跑 1–5 个店铺；推荐 1 GB 留余量。
扫码登录时会临时启动 Chrome（约 300–500 MB），登录完自动关闭；服务器通常不需要装 Chrome：
在本机登录后「导出登录态」，再到服务器「导入登录态」（或直接粘贴 Cookie）即可。
如果服务器本身有桌面/RDP，也可以运行 `python -m pdd_cs chrome --install` 一键下载 Chrome 后直接扫码；
Linux 无桌面环境即使装了 Chrome 也无法弹窗，仍建议用登录态迁移。
网络要求：服务器能稳定访问 `*.pinduoduo.com`（建议国内机房），**固定公网 IP 更稳**。

### 2. 登录问题（服务器没有桌面怎么办）

拼多多商家后台登录是扫码/账号登录。v1.5 起推荐 **本机登录 → 导出登录态 → 服务器导入**，
服务器全程不需要安装 Chrome。

**方案 A（最推荐）：本机登录，导出登录态，服务器导入**

1. 在你自己电脑上启动本项目并打开面板（本地 `start.bat` / `bash start.sh`）；
2. 「店铺与登录」→ 对目标店铺点「扫码登录」（或用「粘贴 Cookie」）；
3. 登录成功后，店铺卡片点「导出登录态」→ 复制登录码，或下载 `pdd-login-店铺id.json`；
4. 打开服务器面板（SSH 隧道访问）→ 同一店铺点「导入登录态」→ 粘贴登录码/上传文件；
5. 保存后服务器自动连接店铺，几秒内上线。

> 登录态等同于账号密码：请通过自己的加密通道/内网传输，导入后删除导出文件；
> 服务器导入成功后不需要保留 Chrome，也不需要手动改配置文件。

**方案 B：网页「粘贴 Cookie」登录**

1. 启动服务并打开面板（服务器上通过 SSH 隧道访问）；
2. 顶部「店铺与登录」→ 点「粘贴 Cookie」；
3. 在已登录拼多多商家后台的浏览器按 F12 → Network → 任意请求 → 复制请求头里的 Cookie 整行；
4. 粘贴保存，几秒内店铺自动上线。

有桌面环境的 Windows 云服务器还可以直接点「扫码登录」。

**方案 C：本机扫码登录，手动上传 Cookie 文件（旧方式，仍可用）**

```bash
# 1) 在你自己的电脑上（有 Chrome 桌面）登录
python -m pdd_cs login --shop shop-a
python -m pdd_cs login --shop shop-b

# 2) 把 Cookie 文件传到服务器（Cookie 等同账号密码，务必走 scp/内网）
scp config/secrets/shop-a.json user@server:/opt/pdd-cs/config/secrets/
scp config/secrets/shop-b.json user@server:/opt/pdd-cs/config/secrets/

# 3) 服务器上改权限并自检
chmod 600 /opt/pdd-cs/config/secrets/*.json
python -m pdd_cs doctor
```

**方案 D：Windows 服务器 RDP**——直接在远程桌面里跑 `login`，最接近本地体验，但要注意
远程桌面断开后程序要作为服务/计划任务常驻（不要依赖登录会话）。

**方案 E：Linux + Xvfb + Chrome**——技术上可行，但维护成本高，不推荐日常用。

> 提醒：服务器 IP 与常用登录地差异过大时，拼多多可能判定异地登录并踢下线。
> 建议用固定 IP、先在非高峰小流量验证；被踢后程序会告警，重新在常用网络登录即可。

### 2.0 Chrome 检测与一键安装（可选）

- 检测：`python -m pdd_cs chrome --status`，面板「店铺与登录 → 运行环境」也会显示；
- 一键下载：`python -m pdd_cs chrome --install`（下载到 `data/chrome/`，约 150MB，免 root）；
- 离线/镜像：`PDD_CS_CHROME_ZIP=/path/chrome-linux64.zip python -m pdd_cs chrome --install`，
  或用 `PDD_CS_CHROME_URL` 指定镜像直链；
- Windows 桌面/RDP 服务器：下载后可以直接在面板「扫码登录」；
- **Linux 运行库**：Chrome for Testing 需要系统库，Debian/Ubuntu 可先安装：
  `sudo apt-get install -y libnss3 libatk-bridge2.0-0 libgtk-3-0 libgbm1 libasound2 libxss1 libxdamage1 libxrandr2 libxcomposite1 libxfixes3`；
  缺少时面板会提示「已检测到 Chrome，但启动失败」；
- **Linux 无桌面服务器**：Chrome 可以装，但没有 `DISPLAY` 时无法弹出扫码窗口，
  请继续使用「本机登录 → 导出登录态 → 服务器导入」，或自行配置 Xvfb/VNC 后把二维码显示出来；
- 也可以完全跳过 Chrome：用「导入登录态」或「粘贴 Cookie」。

### 2.0.1 登录界面（v1.7+）

面板启用访问令牌后，浏览器首次访问会看到安全登录页（不再出现浏览器 401/基本认证弹窗）：

- 令牌来源：`PDD_CS_AUTH_TOKEN` 环境变量，或 `config/config.yaml` 的 `server.auth_token`；
  systemd 部署见 `/etc/pdd-cs.env`，建议同时记到 `/opt/pdd-cs/access-credentials.txt`；
- 登录成功写入 HttpOnly Cookie，HTTPS 下为 Secure；面板右上角可「退出」；
- 登录失败 8 次/5 分钟会限速 429；登录接口本身要求 JSON + 同源，避免被跨站滥用；
- Nginx 反代只需：

```nginx
location / {
    proxy_pass http://127.0.0.1:38617;
    proxy_http_version 1.1;
    proxy_set_header Host $http_host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

**不要**再配置 `auth_basic`、`proxy_set_header X-Auth-Token ...` 或 `proxy_hide_header Set-Cookie`，
否则浏览器先弹基本认证框，登录页不会显示；应用会自己完成令牌登录。保留 TLS 证书和健康检查即可。

### 2.1 端口与访问令牌（服务器必看）

**端口**：默认 **38617**，刻意避开 80/443/8080/8787 等常见端口，减少冲突和扫描。
三种改法（优先级从高到低）：

```bash
# 1) 临时/命令行
python -m pdd_cs run --port 41000

# 2) 环境变量（Docker / systemd 最常用）
PDD_CS_PORT=41000 python -m pdd_cs run
# 或写进 deploy/.env / /etc/pdd-cs.env：PDD_CS_PORT=41000

# 3) 配置文件 config/config.yaml
server:
  host: 127.0.0.1
  port: 41000
  auth_token: ""
```

`PDD_CS_HOST` / `PDD_CS_PORT` / `PDD_CS_AUTH_TOKEN` / `PDD_CS_DATA_DIR` / `PDD_CS_LOG_LEVEL`
都可以用环境变量覆盖，Docker 和 systemd 已按这套变量配好。

**访问令牌**：为了服务器安全，面板默认只监听 `127.0.0.1`（SSH 隧道访问，不需要令牌）。
如果确实要让面板监听 `0.0.0.0` 或内网 IP，**程序会强制要求设置令牌**：

```bash
# 生成一个 32 字节随机令牌
python -c "import secrets;print(secrets.token_urlsafe(32))"

# 方式 A：配置文件
server:
  host: 0.0.0.0
  auth_token: "把生成的长随机串填这里"

# 方式 B：环境变量（Docker compose 已用这个）
export PDD_CS_AUTH_TOKEN=生成的令牌
```

带令牌访问：

- 浏览器：`http://服务器:38617/?token=你的令牌`（首次访问后写 Cookie，之后不用再带）；
- 接口/脚本：请求头 `X-Auth-Token: 你的令牌`，或 URL 参数 `?token=`；
- `/healthz` 不需要令牌（供 Docker/监控使用），其余页面与 `/api/*` 都需要。

**SSH 隧道**（推荐的内网访问方式，不需要开防火墙端口）：

```bash
ssh -L 38617:127.0.0.1:38617 user@server
# 浏览器打开 http://127.0.0.1:38617/
```

**反向代理**（如果一定要公网访问）：Nginx/Caddy 只反代到 `127.0.0.1:38617`，
配 HTTPS + BasicAuth/SSO + IP 白名单；同时把 `server.auth_token` 当成第二道防线。

### 3. Linux 服务器部署（Docker，推荐）

**一键脚本（推荐）**：上传项目后执行

```bash
bash deploy/install.sh
```

脚本会自动：生成 `config/config.yaml`（没有的话）、创建 `deploy/.env` 并写入随机访问令牌
和默认少见端口 **38617**、构建并启动容器，最后打印面板地址、令牌和 SSH 隧道命令。

**手动步骤**：


```bash
# 服务器上
sudo apt install -y docker.io docker-compose-v2 git
git clone <你的仓库> /opt/pdd-cs && cd /opt/pdd-cs

# 准备配置（config.yaml 按需修改：shops、llm、alerts、monitor）
cp config/config.example.yaml config/config.yaml
mkdir -p config/secrets data
# 上传 Cookie 到 config/secrets/

cd deploy
docker compose up -d --build
docker compose logs -f          # 看日志
curl -s "http://127.0.0.1:38617/healthz?strict=1"   # 严格健康检查
```

面板只绑 `127.0.0.1:38617`，本地电脑通过 SSH 隧道访问：

```bash
ssh -L 38617:127.0.0.1:38617 user@server
# 然后浏览器打开 http://127.0.0.1:38617
```

### 4. Linux 服务器部署（不用 Docker：systemd）

```bash
sudo useradd -r -m -d /opt/pdd-cs pdd-cs
sudo rsync -a --exclude data --exclude config/secrets ./ /opt/pdd-cs/
sudo chown -R pdd-cs:pdd-cs /opt/pdd-cs
sudo -u pdd-cs python3 -m venv /opt/pdd-cs/.venv
sudo -u pdd-cs /opt/pdd-cs/.venv/bin/pip install -r /opt/pdd-cs/requirements.txt

sudo cp /opt/pdd-cs/deploy/pdd-cs.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pdd-cs
journalctl -u pdd-cs -f
```

`restart=always` 会让进程崩溃/全部登录失效后自动拉起。

### 4.1 网页后台（v1.5+）

启动后所有设置都可以在面板完成：登录（扫码/粘贴 Cookie）、**登录态导出/导入**、店铺管理、
AI Key、回复规则、营业时间、告警、话术、知识库。保存后自动重连店铺，**不用改配置文件、不用重启**。
服务器无需安装 Chrome：本机登录后导出登录态，服务器粘贴导入即可。
面板默认只绑 `127.0.0.1`，服务器上通过 SSH 隧道访问即可；绑定公网必须设置访问令牌。

### 5. 安全清单（务必做）

- [ ] 面板只监听 `127.0.0.1`，通过 SSH 隧道/内网访问；**不要直接把 38617 暴露公网**；
- [ ] 如必须公网访问：Nginx/Caddy 反向代理 + HTTPS + BasicAuth/SSO + IP 白名单；
- [ ] `config/secrets/`、`data/` 权限 600/700，属主为运行用户；
- [ ] 不要把 `config/secrets/`、`data/`、`config/config.yaml` 提交到 git（项目 `.gitignore`
      和 `.dockerignore` 已默认排除）；
- [ ] 服务器防火墙只开 SSH 和必要的代理端口；
- [ ] 定期备份 `data/pdd_cs.db` 与 `config/`（Cookie 会过期，备份主要用于报表与话术）。

### 6. 升级与迁移

```bash
# Docker
cd /opt/pdd-cs && git pull
cd deploy && docker compose up -d --build

# systemd
cd /opt/pdd-cs && git pull
sudo -u pdd-cs .venv/bin/pip install -r requirements.txt
sudo systemctl restart pdd-cs
```

- 数据库自动迁移（单店铺旧库会迁移为 `default` 店铺，旧表保留备份）；
- 升级前建议先 `cp data/pdd_cs.db data/pdd_cs.db.bak`；
- 建议先在本机/测试店铺跑一遍 `pytest` 和 `lint` 再升级。

### 7. 常见问题

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 启动提示所有店铺登录失效 | Cookie 过期/异地被踢 | 本机重新登录，重传 Cookie 文件 |
| 面板打不开 | 只绑了 127.0.0.1 | 用 SSH 隧道；或改 `server.host` + 防火墙白名单 |
| 告警收不到 | Webhook 地址/关键词/加签不对 | `python -m pdd_cs alert-test` 测试；钉钉注意安全关键词 |
| 容器一直 unhealthy | 有店铺掉线（strict 检查） | 看 `docker compose logs` + 面板「店铺状态」 |
| 客户消息延迟大 | 服务器网络差/跨地域 | 换国内机房；看面板「最近收消息」时间 |
| 进程反复重启 | 所有店铺起不来 | 先 `doctor`；确认 Cookie 与 `config.yaml` |

---

## 三、最小上线清单

1. 服务器：国内 1C1G、固定 IP、能访问拼多多；
2. 本机登录 → 传 `config/secrets/*.json`；
3. `config.yaml`：`shops`、`llm`、`alerts`（含 `shop_offline`）、`monitor.heartbeat_url`；
4. `alert-test` 验证群机器人；
5. Docker/systemd 启动，确认 `healthz?strict=1` 返回 200；
6. 在 Uptime Kuma/Healthchecks.io 配一个监控（strict URL 或心跳），告警到手机；
7. 小流量灰度 1–2 天，看面板「会话质检 / 风控 / 待处理」再逐步放量。
