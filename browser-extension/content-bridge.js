/** 隔离世界：把页面主世界捕获的 anti_content 转交给扩展后台。 */
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
