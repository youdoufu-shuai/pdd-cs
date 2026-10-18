const $ = (id) => document.getElementById(id);

function setStatus(text, kind) {
  const el = $("status");
  el.textContent = text;
  el.className = "status" + (kind ? " " + kind : "");
}

async function loadConfig() {
  const config = await chrome.runtime.sendMessage({ type: "get-config" });
  if (!config) return;
  $("serverUrl").value = config.serverUrl || "";
  $("token").value = config.token || "";
  $("shopId").value = config.shopId || "1";
  const stored = await chrome.storage.local.get(["lastMessage", "lastShopName", "lastSyncAt"]);
  if (stored.lastMessage) {
    setStatus(stored.lastMessage, stored.lastShopName ? "ok" : "");
  }
}

async function saveConfig() {
  const config = {
    serverUrl: $("serverUrl").value.trim(),
    token: $("token").value.trim(),
    shopId: $("shopId").value.trim() || "1",
  };
  if (!config.serverUrl || !config.token) {
    setStatus("请填写面板地址和访问令牌", "bad");
    return null;
  }
  await chrome.runtime.sendMessage({ type: "save-config", ...config });
  setStatus("配置已保存", "ok");
  return config;
}

$("saveBtn").addEventListener("click", saveConfig);

$("syncBtn").addEventListener("click", async () => {
  const config = await saveConfig();
  if (!config) return;
  $("syncBtn").disabled = true;
  $("syncBtn").textContent = "同步中…";
  setStatus("正在读取当前浏览器登录态并发送到面板…");
  try {
    const result = await chrome.runtime.sendMessage({ type: "sync-now" });
    if (result && result.ok) {
      setStatus(result.message || "同步成功", "ok");
    } else {
      setStatus((result && result.message) || "同步失败", "bad");
    }
  } finally {
    $("syncBtn").disabled = false;
    $("syncBtn").textContent = "立即同步";
  }
});

loadConfig();
