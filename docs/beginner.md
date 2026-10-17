# 小白使用指南（3 分钟上手）

> 这份指南假设你**完全没写过代码**。照着做就行，遇到问题先看最后的「常见问题」。

---

## 一、你要准备什么

| 准备项 | 说明 |
| --- | --- |
| 一台 Windows 电脑（或 Mac/Linux） | 家用电脑即可，正式 24 小时接待建议放服务器，见 [部署指南](deployment.md) |
| Python 3.10 以上 | Windows 安装时**务必勾选 “Add Python to PATH”**：[下载地址](https://www.python.org/downloads/) |
| Chrome 浏览器 | 扫码登录拼多多商家后台时用 |
| 拼多多商家账号 | 能登录商家后台的账号（店员账号也可以，需有客服权限） |

---

## 二、最快方式：双击一键启动

### Windows

1. 下载项目（GitHub 右侧 **Code → Download ZIP**），解压到任意目录；
2. 双击 **`start.bat`**；
3. 第一次会自动创建环境、安装依赖、生成配置（约 1–2 分钟，不要关窗口）；
4. 看到“面板地址：http://127.0.0.1:38617”后，程序已经在运行；
5. 按提示完成登录（见下面第五节）。

### macOS / Linux

在项目目录打开终端，执行：

```bash
bash start.sh
```

### 不想用一键脚本？

```bash
pip install -r requirements.txt
python -m pdd_cs setup     # 交互式向导，一路回车即可
python -m pdd_cs login     # 扫码登录
python -m pdd_cs run       # 启动
```

---

## 三、新手上手向导（推荐）

在项目目录执行：

```bash
python -m pdd_cs setup
```

按提示回答几个问题（全部可以直接回车用默认值）：

1. 单店铺还是多店铺（默认单店铺）；
2. 是否启用 AI 回复（不填也能用，只会用关键词回复）；
3. 面板端口（默认 `38617`，不用改）。

生成后的配置文件在 `config/config.yaml`，常用改动：

```yaml
shops:
  - id: shop-a
    name: A店（女装）
    cookie_file: config/secrets/shop-a.json

llm:
  enabled: true
  api_key: "你的大模型 Key"   # 不想用 AI 就保持 false
```

> 说明：API Key 只写在本地 `config/config.yaml`，这个文件不会被 git 提交。
> 也可以不写进配置，改用环境变量 `PDD_CS_LLM_KEY`。

---

## 四、登录拼多多店铺

```bash
python -m pdd_cs login                    # 单店铺
python -m pdd_cs login --shop shop-a      # 多店铺：逐个登录
```

- 程序会弹出一个**独立的 Chrome 窗口**，用手机扫码或账号密码登录；
- 登录成功后自动保存 Cookie，窗口可以关闭；
- 登录态会过期，过期后程序会告警，重新执行一次 `login` 即可。

---

## 五、启动并打开面板

```bash
python -m pdd_cs run
```

浏览器打开 **http://127.0.0.1:38617**（端口以启动日志为准），就能看到运行面板：

- **运营概览**：今日进线、自动回复、转人工、首响达标率；
- **新手引导**：三步状态（登录 / 配置 / 运行），照着看就知道还差什么；
- **店铺状态**：每个店铺连接是否正常；
- **待处理队列**：客户在等、转接失败的会话；
- **会话监控**：点任意一行可以看完整聊天记录；
- **风控与合规事件**：被平台规则拦截/改写的内容；
- **运营洞察 / 会话质检**：哪些话术命中最多、哪些问题没答好。

按 `Ctrl + C` 停止程序。

---

## 六、自检与常见问题

### 启动前先自检

```bash
python -m pdd_cs doctor
```

它会逐项告诉你：Python 依赖、配置、Cookie、AI、规则、知识库、端口是否正常。

### 常见问题

| 现象 | 原因 | 解决 |
| --- | --- | --- |
| 双击 start.bat 一闪而过 | 没装 Python 或没勾 PATH | 重装 Python，勾选 “Add Python to PATH” |
| 提示 `pip` 下载失败 | 网络问题 | start.bat 会自动用国内镜像重试；仍失败可手动执行 `pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple` |
| 登录时没有弹出 Chrome | 没装 Chrome / 路径特殊 | 安装 Chrome；或手动粘贴 Cookie：`python -m pdd_cs login --cookie "PASS_ID=...; MSA=..."` |
| 提示“所有店铺均启动失败” | Cookie 没登录/已过期 | 运行 `python -m pdd_cs login` 重新登录 |
| 面板打不开 | 端口被占用/程序没启动 | 看启动日志里的端口；`doctor` 会检查端口；可改 `server.port` |
| 中文显示乱码 | 终端编码问题 | 用 Windows Terminal；或先执行 `chcp 65001` |
| 需要 24 小时接待 | 电脑关机程序就停 | 部署到服务器：[部署与掉线通知](deployment.md) |
| 想先看效果 | 没有店铺账号 | `python scripts/demo.py`，自带两个 Mock 店铺 |

### 想改话术

- 关键词回复：`config/rules/keywords.yaml`
- 转人工关键词：`config/rules/transfer.yaml`
- 店主人设：`config/persona.md`
- 知识库：`config/knowledge/*.md`
- 改完运行 `python -m pdd_cs lint` 检查有没有平台规则风险，再重启生效。

---

## 七、下一步

- 想多店铺：在 `config/config.yaml` 的 `shops` 里加多个店铺，然后逐个 `login --shop <id>`；
- 想接 AI：在 `config/config.yaml` 填 `llm.api_key`，或设环境变量 `PDD_CS_LLM_KEY`；
- 想接收掉线告警：配置 `alerts.webhook_url`（钉钉/飞书/企微），详见 [部署指南](deployment.md)；
- 想正式跑：按 [部署指南](deployment.md) 放到服务器，配好心跳与告警。
