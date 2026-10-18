/** 隔离世界：把页面主世界捕获的 anti_content 转交给扩展后台。 */
(function injectMainWorldPatch() {
  function inject() {
    try {
      if (window.__PDD_CS_ANTI_BRIDGE_INJECTED__) return;
      window.__PDD_CS_ANTI_BRIDGE_INJECTED__ = true;
      const script = document.createElement("script");
      script.src = chrome.runtime.getURL("content-main.js");
      script.onload = function () { script.remove(); };
      (document.head || document.documentElement).appendChild(script);
    } catch (err) {
      // 页面 CSP 或 DOM 尚未就绪时由 MAIN world 的 content script 兜底
    }
  }
  if (document.documentElement) inject();
  else document.addEventListener("DOMContentLoaded", inject, { once: true });
})();

window.addEventListener("message", function (event) {
  if (event.source !== window) return;
  const data = event.data || {};
  if (data.source !== "pdd-cs-anti-content" || !data.value) return;
  try {
    chrome.runtime.sendMessage({ type: "anti-content", value: String(data.value) });
  } catch (err) {
    // 扩展被重载时可能暂时不可用
  }
});
