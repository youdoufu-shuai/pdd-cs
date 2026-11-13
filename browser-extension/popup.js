"use strict";

const ADDRESS_KEY = "pdd-cs-panel-address-v2";
const LOOPBACK = new Set(["127.0.0.1", "localhost", "[::1]"]);
const input = document.getElementById("serverUrl");
const status = document.getElementById("status");
const form = document.getElementById("launcher");
const openButton = document.getElementById("openBtn");
const forgetButton = document.getElementById("forgetBtn");

function panelAddress(value) {
  const raw = String(value).trim();
  if (!/^https?:\/\//i.test(raw) || /[\u0000-\u0020\u007f\\?#]/.test(raw)) {
    throw new Error("请输入完整面板网址，不要带查询参数、片段或令牌。");
  }
  let url;
  try {
    url = new URL(raw);
  } catch {
    throw new Error("面板网址格式不正确。");
  }
  if (url.username || url.password) {
    throw new Error("请勿在网址内填写账号或密码。");
  }
  if (url.protocol !== "https:" && !(url.protocol === "http:" && LOOPBACK.has(url.hostname))) {
    throw new Error("远程面板必须使用 HTTPS；HTTP 仅用于本机面板。");
  }
  return url.href;
}

function setStatus(text, kind) {
  status.textContent = text;
  status.className = "status" + (kind ? " " + kind : "");
}

try {
  const saved = localStorage.getItem(ADDRESS_KEY);
  if (saved) input.value = panelAddress(saved);
} catch {
  // The launcher remains usable when browser storage is unavailable.
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  let destination;
  try {
    destination = panelAddress(input.value);
  } catch (error) {
    setStatus(error.message, "bad");
    input.focus();
    return;
  }
  openButton.disabled = true;
  try {
    // Creating a tab does not require the Chrome "tabs" permission.
    await chrome.tabs.create({ url: destination });
    try { localStorage.setItem(ADDRESS_KEY, destination); } catch {}
    setStatus("已打开，请在面板中登录。", "ok");
  } catch {
    setStatus("无法打开面板，请复制网址到浏览器手动打开。", "bad");
  } finally {
    openButton.disabled = false;
  }
});

forgetButton.addEventListener("click", () => {
  try { localStorage.removeItem(ADDRESS_KEY); } catch {}
  input.value = "";
  input.focus();
  setStatus("已清除保存的面板地址。", "ok");
});
