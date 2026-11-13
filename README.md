# 拼多多智能客服 · pdd-cs

面向自有店铺的多店铺客服助手。用一个面板查看进线、待人工处理的会话和回复质检；有明确规则的咨询交给自动回复，售后、投诉和不确定的问题留给人工。

[快速开始](#快速开始) · [先看模拟演示](#先看模拟演示) · [服务器部署](#服务器部署) · [升级与备份](#升级与备份) · [隐私与安全](#隐私与安全)

当前版本：**2.2.0** · Python 3.10+ · Windows / macOS / Linux · [AGPL-3.0](LICENSE)

## 快速开始

1. 下载并解压项目，或用 Git 克隆：

   ```bash
   git clone https://github.com/youdoufu-shuai/pdd-cs.git
   cd pdd-cs
   ```

2. Windows 双击 `start.bat`；macOS / Linux 运行 `bash start.sh`。首次启动会创建虚拟环境、安装依赖、生成本地配置。
3. 打开面板（默认 `http://127.0.0.1:38617/`），在「店铺与登录」添加自有店铺；到「话术与知识库」配置业务规则，先用「决策测试台」检查典型问题，再接入真实会话。没有真实店铺也可以先跑下面的模拟演示。

**AI 是可选项。** 不填 API Key 仍可用关键词规则、默认回复、待处理队列和人工转接。需要 AI 时，在「系统设置」配置支持 Chat Completions 的服务地址、模型和凭据。现有接口按静态 Bearer 凭据配置；仅支持 Google Cloud ADC / 短时 OAuth 的端点不能直接当作长期 API Key 使用。

## 先看模拟演示

演示使用本机 Mock 店铺，不需要真实账号，不向真实店铺发消息。

```bash
python -m pip install -r requirements.txt
python scripts/demo.py --port 38619 --no-browser
```

打开 `http://127.0.0.1:38619/`。默认运行 180 秒，添加 `--seconds 0` 可持续运行。正式服务和模拟演示使用不同的数据目录。

## 功能

| 场景 | 可以做什么 |
| --- | --- |
| 多店铺管理 | 汇总查看连接、登录与运营状态，也可按店铺筛选 |
| 日常接待 | 配置关键词、商品规则、营业时间、默认回复及可选 AI 知识库 |
| 人工处理 | 查看超时、转接失败和人工接管会话；售后、投诉等问题按规则转交 |
| 运营复盘 | 查看首响指标、话术命中、风险记录和会话质检；导出会话供内部复盘 |
| 异常告警 | 按需配置 Webhook、邮件和外部心跳，关注掉线、转接失败等事件 |

自动规则不能替代人工判断，也不代表平台对第三方工具的认可。接入自有店铺前，请核对商家后台的现行规则并安排人工值守。

## 服务器部署

建议使用 [Docker 或 systemd 部署指南](docs/deployment.md)。面板默认仅在回环地址访问，远程管理优先用 SSH 隧道：

```bash
ssh -N -L 38617:127.0.0.1:38617 user@example.com
```

随后在本机浏览器打开 `http://127.0.0.1:38617/`。如果本机端口已被占用，可以修改 `-L` 的第一个端口。面板启用了访问令牌时，请在登录表单输入，不要拼进 URL。

| 检查 | 含义 |
| --- | --- |
| `GET /healthz` | 服务进程能够响应，返回 HTTP 200 |
| `GET /healthz?strict=1` | 至少有一个店铺、店铺均已连接且无启动错误时返回 200；未就绪返回 503 |
| `python -m pdd_cs doctor --offline` | 检查本机配置、依赖、规则、端口及本地登录文件，不向平台验证登录态 |

健康探针不查询会话数据库。启用面板令牌后，匿名探针只返回 `ok` 和对应状态码，原有监控仍可按状态码判断；已登录或使用请求头认证的监控可读取店铺诊断。

## 升级与备份

- **先备份，再升级。** 保留运行环境的 `config/`、`data/` 和部署环境变量；备份不得上传公开仓库。停服务后再复制 SQLite 文件，或使用 SQLite 的在线备份方式。
- 更新代码和依赖，按原有方式启动；Docker 部署需重新构建镜像。不要把公开示例配置覆盖到正在使用的生产配置。
- 先跑 `doctor --offline`，再看严格健康检查和面板状态。离线自检通过不代表店铺登录仍有效。
- 如果还装着旧版「登录态自动同步」扩展，请先卸载或禁用。现有 [浏览器扩展](browser-extension/README.md) 只是面板快捷入口，不读取或上传店铺 Cookie。

## 隐私与安全

- 店铺 Cookie、登录码、二维码、面板访问令牌和 API Key 都是凭据。只放在自己的运行环境，不发到聊天、Issue、截图或提交记录。
- 会话与日志默认保存在部署机器。若启用外部 AI、Webhook、邮件或监控，相关内容会发送给你配置的服务；请按业务需要控制发送范围和访问权限。
- 启用面板令牌后仍建议保留回环监听，不直接开放面板端口。若有反向代理，同时检查代理日志是否记录请求查询参数。
- 开发者在公开前运行 `python scripts/check-publication.py --staged`，并使用 GitHub `noreply` 提交邮箱。自动扫描不替代图像人工脱敏与历史检查。

## 开发与验证

```bash
python -m pytest
node --test tests/test_browser_extension_privacy.cjs
python scripts/check-publication.py
python scripts/package-browser-extension.py --check
```

仓库导读：[新手指南](docs/beginner.md) · [运营手册](docs/operations.md) · [部署指南](docs/deployment.md) · [架构说明](docs/architecture.md) · [平台规则与实现对照](docs/compliance-matrix.md)

本项目的开源来源和归属见 [NOTICE](NOTICE)，许可见 [LICENSE](LICENSE)。
