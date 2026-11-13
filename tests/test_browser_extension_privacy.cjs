const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const extension = path.join(__dirname, "..", "browser-extension");
const manifest = JSON.parse(fs.readFileSync(path.join(extension, "manifest.json"), "utf8"));
const script = fs.readFileSync(path.join(extension, "popup.js"), "utf8");
const addressKey = "pdd-cs-panel-address-v2";

function popup(savedAddress, { failCreate = false } = {}) {
  const elements = Object.fromEntries(["serverUrl", "status", "launcher", "openBtn", "forgetBtn"].map(id => [
    id, {
      value: "", className: "", textContent: "", disabled: false, focused: false,
      focus() { this.focused = true; },
      addEventListener(event, handler) { this[event] = handler; },
    },
  ]));
  const local = new Map(savedAddress ? [[addressKey, savedAddress]] : []);
  const opened = [];
  vm.runInNewContext(script, {
    URL,
    document: { getElementById: id => elements[id] },
    localStorage: {
      getItem: key => local.get(key),
      setItem: (key, value) => local.set(key, value),
      removeItem: key => local.delete(key),
    },
    chrome: { tabs: { async create({url}) {
      if (failCreate) throw new Error("mock failure");
      opened.push(url);
    } } },
  });
  return {
    elements, local, opened,
    async open(value) {
      elements.serverUrl.value = value;
      await elements.launcher.submit({preventDefault() {}});
    },
  };
}

test("manifest has only a popup, no requested permissions or capture scripts", () => {
  for (const key of ["permissions", "optional_permissions", "host_permissions",
    "optional_host_permissions", "background", "content_scripts", "web_accessible_resources"]) {
    assert.equal(Object.hasOwn(manifest, key), false, key);
  }
  assert.equal(manifest.action.default_popup, "popup.html");
  assert.doesNotMatch(script, /fetch\(|\.cookies\b|chrome\.storage\b|\banti_content\b/);
});

test("starts blank and never opens a tab just by reading a saved address", () => {
  const fresh = popup();
  assert.equal(fresh.elements.serverUrl.value, "");
  assert.equal(fresh.opened.length, 0);
  const saved = popup("https://panel.example.com/dashboard");
  assert.equal(saved.elements.serverUrl.value, "https://panel.example.com/dashboard");
  assert.equal(saved.opened.length, 0);
  saved.elements.forgetBtn.click();
  assert.equal(saved.elements.serverUrl.value, "");
  assert.equal(saved.local.size, 0);
});

test("opens only after submitting a user-provided secure or local panel URL", async () => {
  const app = popup();
  for (const url of ["https://panel.example.com/dashboard", "http://127.0.0.1:38617/", "http://[::1]:38617/"]) {
    await app.open(url);
    assert.equal(app.opened.at(-1), url);
    assert.equal(app.local.get(addressKey), url);
  }
});

test("rejects plain HTTP remote URLs, credentials, parameters and non-web URLs", async () => {
  const app = popup();
  for (const url of [
    "http://panel.example.com/",
    "http://localhost.example.com/",
    "https://user:secret@panel.example.com/",
    "https://panel.example.com/?example=value",
    "https://panel.example.com/#example",
    "https://panel.example.com/?",
    "javascript:alert(1)",
    "data:text/html,test",
    "file:///tmp/local",
  ]) {
    await app.open(url);
    assert.equal(app.elements.status.className, "status bad", url);
  }
  assert.equal(app.opened.length, 0);
  assert.equal(app.local.size, 0);
});

test("a failed navigation does not save a new address", async () => {
  const app = popup(null, {failCreate: true});
  await app.open("https://panel.example.com/");
  assert.equal(app.elements.status.className, "status bad");
  assert.equal(app.elements.openBtn.disabled, false);
  assert.equal(app.local.size, 0);
});
