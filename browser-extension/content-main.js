/**
 * 页面主世界：只拦截拼多多发请求时生成的 anti_content，不读取/修改业务数据。
 * anti_content 是发商品卡片所需的签名，页面每次请求都会重新生成。
 */
(function () {
  if (window.__PDD_CS_ANTI_PATCHED__) return;
  window.__PDD_CS_ANTI_PATCHED__ = true;
  const MARK = "pdd-cs-anti-content";

  function publish(body) {
    try {
      if (typeof body !== "string" || body.indexOf("anti_content") < 0) return;
      const parsed = JSON.parse(body);
      const value =
        parsed.anti_content ||
        (parsed.data && parsed.data.anti_content) ||
        "";
      if (value && typeof value === "string" && value.length > 20) {
        window.postMessage({ source: MARK, value: value }, "*");
      }
    } catch (err) {
      // 非 JSON 请求直接忽略
    }
  }

  const originalFetch = window.fetch;
  if (typeof originalFetch === "function") {
    window.fetch = function () {
      try {
        const init = arguments[1] || {};
        publish(init.body);
      } catch (err) {}
      return originalFetch.apply(this, arguments);
    };
  }

  const originalSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function (body) {
    try { publish(body); } catch (err) {}
    return originalSend.apply(this, arguments);
  };
})();
