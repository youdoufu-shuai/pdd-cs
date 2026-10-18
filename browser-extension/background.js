/**
 * PDD 客服登录同步扩展
 *
 * 功能：
 *  - 读取当前浏览器在 pinduoduo.com 下的全部 Cookie（含 HttpOnly）
 *  - 发送到私有客服面板 /api/login/cookie
 *  - 面板会校验店铺身份；如果当前是油豆腐，导入绿逸会被拒绝
 *
 * 扩展只在本地浏览器读取 Cookie，不记录、不上传到第三方。
 */

const DEFAULTS = {
  serverUrl: "https://youdoufu.xyz:38618",
  shopId: "1",
  token: "",
};

let syncing = false;

async function getConfig() {
  const stored = await chrome.storage.local.get(DEFAULTS);
  return {
    serverUrl: String(stored.serverUrl || DEFAULTS.serverUrl).replace(/\/+$/, ""),
    shopId: String(stored.shopId || DEFAULTS.shopId),
    token: String(stored.token || ""),
  };
}

function cookieDomainMatches(domain) {
  const value = String(domain || "").replace(/^\./, "").toLowerCase();
  return value === "pinduoduo.com" || value.endsWith(".pinduoduo.com");
}

async function collectCookies() {
  const all = await chrome.cookies.getAll({});
  const cookies = {};
  for (const cookie of all) {
    if (!cookieDomainMatches(cookie.domain)) continue;
    if (!cookie.name || cookie.value == null) continue;
    cookies[cookie.name] = cookie.value;
  }
  return cookies;
}

function looksLoggedIn(cookies) {
  return Boolean(
    cookies.PASS_ID ||
    cookies.SUB_PASS_ID ||
    cookies.MSA ||
    cookies.JSESSIONID,
  );
}

async function updateBadge(ok, text) {
  try {
    await chrome.action.setBadgeBackgroundColor({ color: ok ? "#0f8a5f" : "#b42318" });
    await chrome.action.setBadgeText({ text: text || (ok ? "OK" : "!") });
  } catch (err) {
    // badge 不是核心功能
  }
}

async function cookieHash(text) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(digest)).map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function syncNow({ reason, force = false } = {}) {
  if (syncing) return { ok: false, message: "正在同步，请稍候" };
  const config = await getConfig();
  if (!config.token) {
    await updateBadge(false, "配置");
    return { ok: false, message: "请先在扩展里填写面板访问令牌" };
  }
  syncing = true;
  try {
    const cookies = await collectCookies();
    if (!looksLoggedIn(cookies)) {
      await updateBadge(false, "未登录");
      return { ok: false, message: "没有找到拼多多登录态，请先登录绿逸店铺后台" };
    }
    const cookieText = Object.entries(cookies)
      .map(([name, value]) => `${name}=${value}`)
      .join("; ");
    const stored = await chrome.storage.local.get(["antiContent"]);
    const antiContent = String(stored.antiContent || "");
    const hash = await cookieHash(cookieText + "|" + antiContent);
    const previous = await chrome.storage.local.get(["lastCookieHash", "lastSyncAt"]);
    if (!force && previous.lastCookieHash === hash && previous.lastSyncAt) {
      const age = Date.now() - new Date(previous.lastSyncAt).getTime();
      if (Number.isFinite(age) && age < 10 * 60 * 1000) {
        await updateBadge(true, "已同步");
        return { ok: true, message: "登录态没有变化，已跳过重复同步" };
      }
    }
    const response = await fetch(`${config.serverUrl}/api/login/cookie`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Auth-Token": config.token,
      },
      body: JSON.stringify({
        shop_id: config.shopId,
        cookie: cookieText,
        anti_content: antiContent,
      }),
    });
    let data = {};
    try { data = await response.json(); } catch (err) { data = {}; }
    if (!response.ok || !data.ok) {
      const message = data.message || data.error || `同步失败（HTTP ${response.status}）`;
      await updateBadge(false, "失败");
      return { ok: false, message, cookies: data.cookies || 0 };
    }
    const shopName = data.shop_name || "当前店铺";
    const message = `已同步 ${data.cookies || Object.keys(cookies).length} 个 Cookie · ${shopName}`;
    await updateBadge(true, "已同步");
    await chrome.storage.local.set({
      lastSyncAt: new Date().toISOString(),
      lastCookieHash: hash,
      lastShopName: shopName,
      lastMessage: message,
      lastReason: reason || "manual",
    });
    return { ok: true, message, shopName, cookies: data.cookies };
  } catch (err) {
    const message = err && err.message ? err.message : String(err);
    await updateBadge(false, "失败");
    return { ok: false, message: `同步异常：${message}` };
  } finally {
    syncing = false;
  }
}

async function autoSync(reason) {
  const config = await getConfig();
  if (!config.token) return;
  const tabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  const url = tabs && tabs[0] && tabs[0].url ? tabs[0].url : "";
  if (!url.includes("mms.pinduoduo.com")) return;
  const result = await syncNow({ reason });
  if (!result.ok) {
    await chrome.storage.local.set({
      lastSyncAt: new Date().toISOString(),
      lastMessage: result.message,
      lastReason: reason,
    });
  }
}

chrome.runtime.onInstalled.addListener(async () => {
  await chrome.alarms.create("pdd-cs-sync", { periodInMinutes: 2 });
  await chrome.storage.local.set({ installedAt: new Date().toISOString() });
});

chrome.runtime.onStartup.addListener(async () => {
  await chrome.alarms.create("pdd-cs-sync", { periodInMinutes: 2 });
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "pdd-cs-sync") autoSync("alarm");
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status === "complete" && tab && tab.url && tab.url.includes("mms.pinduoduo.com")) {
    setTimeout(() => autoSync("tab"), 1500);
  }
});

let cookieTimer = null;
chrome.cookies.onChanged.addListener((info) => {
  const cookie = info.cookie || {};
  if (!cookieDomainMatches(cookie.domain)) return;
  if (!["PASS_ID", "SUB_PASS_ID", "JSESSIONID", "MSA"].includes(cookie.name)) return;
  clearTimeout(cookieTimer);
  cookieTimer = setTimeout(() => autoSync("cookie-change"), 2500);
});

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message && message.type === "sync-now") {
    syncNow({ reason: "popup", force: true }).then(sendResponse);
    return true;
  }
  if (message && message.type === "get-config") {
    getConfig().then(sendResponse);
    return true;
  }
  if (message && message.type === "anti-content") {
    (async () => {
      const value = String(message.value || "");
      if (value.length > 20) {
        await chrome.storage.local.set({
          antiContent: value,
          antiContentAt: new Date().toISOString(),
        });
      }
      sendResponse({ ok: true });
    })();
    return true;
  }
  if (message && message.type === "save-config") {
    (async () => {
      const next = {
        serverUrl: String(message.serverUrl || DEFAULTS.serverUrl).replace(/\/+$/, ""),
        shopId: String(message.shopId || DEFAULTS.shopId),
        token: String(message.token || ""),
      };
      await chrome.storage.local.set(next);
      sendResponse({ ok: true, config: next });
    })();
    return true;
  }
  return false;
});
