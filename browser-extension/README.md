# PDD 客服登录同步扩展

作用：在**你自己的 Chrome** 登录拼多多商家后台后，一键把当前登录态同步到私有客服面板，避免手动复制 Cookie 或二维码过期。

## 安装

1. 下载并解压 `pdd-extension.zip`（服务器面板地址：`https://youdoufu.xyz:38618/pdd-extension.zip`）
2. Chrome 打开 `chrome://extensions`
3. 右上角开启「开发者模式」
4. 点「加载已解压的扩展程序」，选择解压后的 `browser-extension` 文件夹
5. 点浏览器右上角扩展图标，填写：
   - 面板地址：`https://youdoufu.xyz:38618`
   - 面板访问令牌：服务器令牌（`/etc/pdd-cs.env` 里的 `PDD_CS_AUTH_TOKEN`）
   - 店铺 ID：绿逸填 `1`
6. 先在浏览器登录绿逸服饰旗舰店后台，再点「立即同步」

同步成功后，面板「店铺与登录」会显示绿逸的登录态；服务器会自动重启该店铺的客服连接，随后同步后台会话并按设置回复。

## 注意

- 扩展会读取 `pinduoduo.com` 下的 Cookie，包括 HttpOnly 项；只发送到你自己填写的面板地址。
- 店铺身份校验：如果当前浏览器登录的是油豆腐，而店铺 ID 填的是绿逸，面板会拒绝并提示，不会误覆盖。
- 只在 Chrome/Edge 等 Chromium 浏览器使用。
