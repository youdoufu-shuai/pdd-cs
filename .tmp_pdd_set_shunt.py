# -*- coding: utf-8 -*-
"""临时：把拼多多主账号设置为各页面的分流客服（店铺自己的账号，属于正常设置）。"""
import asyncio
import base64
import json
import os
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

import paramiko
from websockets.asyncio.client import connect

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PORT = 9359


def cookies():
    c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect("118.145.114.87", 22, "root", os.environ["PDD_SSH_PASS"], timeout=20, look_for_keys=False, allow_agent=False)
    try:
        sftp = c.open_sftp()
        with sftp.open("/opt/pdd-cs/config/secrets/main.json", "rb") as f:
            return (json.loads(f.read().decode("utf-8")) or {}).get("cookies") or {}
    finally:
        c.close()


async def main():
    jar = cookies()
    proc = subprocess.Popen([CHROME, "--headless=new", "--disable-gpu", "--no-first-run",
                             "--window-size=1440,1200", f"--remote-debugging-port={PORT}",
                             f"--user-data-dir={tempfile.mkdtemp()}", "about:blank"])
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=1).read(); break
            except Exception:
                time.sleep(0.2)
        page = [p for p in json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json", timeout=3).read()) if p.get("type") == "page"][0]
        async with connect(page["webSocketDebuggerUrl"], max_size=20_000_000) as ws:
            seq = 0
            async def send(method, params=None):
                nonlocal seq
                seq += 1
                await ws.send(json.dumps({"id": seq, "method": method, "params": params or {}}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == seq:
                        return msg
            async def ev(expr):
                r = await send("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
                return r.get("result", {}).get("result", {}).get("value")
            await send("Runtime.enable"); await send("Page.enable")
            for name, value in jar.items():
                for domain in (".pinduoduo.com", "mms.pinduoduo.com"):
                    try:
                        await send("Network.setCookie", {"name": name, "value": str(value), "domain": domain, "path": "/", "secure": True, "httpOnly": False})
                    except Exception:
                        pass
            await send("Page.navigate", {"url": "https://mms.pinduoduo.com/mms-chat/tools/shunt/basicShunt"})
            await asyncio.sleep(10)
            print("URL:", await ev("location.href"))
            print("TAIL:", (await ev("document.body.innerText || \"\""))[-1500:])
            pages = ["店铺首页", "商品详情页", "订单详情页", "退款详情页", "物流投诉页"]
            for name in pages:
                result = await ev(f"""(async () => {{
                  const rows=[...document.querySelectorAll('tr')];
                  const row=rows.find(r => (r.innerText||'').includes({json.dumps(name)}));
                  if(!row) return 'row missing';
                  const btn=[...row.querySelectorAll('a,button,span')].find(e => (e.innerText||'').trim()==='设置');
                  if(!btn) return 'set btn missing';
                  btn.click();
                  await new Promise(r=>setTimeout(r,1200));
                  const dialog=[...document.querySelectorAll('[role=dialog],[class*=MDL_modal]')].find(e => (e.innerText||'').includes('选择分流客服'));
                  if(!dialog) return 'dialog missing';
                  const cb=dialog.querySelector('input[type=checkbox]');
                  if(!cb) return 'checkbox missing';
                  cb.click();
                  await new Promise(r=>setTimeout(r,300));
                  const ok=[...dialog.querySelectorAll('button')].find(e => (e.innerText||'').trim()==='确认');
                  if(!ok) return 'confirm missing';
                  ok.click();
                  await new Promise(r=>setTimeout(r,1800));
                  return 'ok';
                }})()""")
                print(name, "=>", result)
                await asyncio.sleep(0.5)
            print("PAGE ROWS:", await ev("""JSON.stringify([...document.querySelectorAll('tr')].map(r=>(r.innerText||'').trim()).filter(t=>t.includes('分流')||t.includes('主账号')||t.includes('未设置')||t.includes('设置')))"""))
            shot = await send("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True, "clip": {"x": 0, "y": 0, "width": 1440, "height": 1200, "scale": 1}})
            Path(r"C:\Users\24942\Documents\deepseek-harness\default-workspace\pdd-cs\docs\pdd-shunt-set.png").write_bytes(base64.b64decode(shot["result"]["data"]))
    finally:
        proc.terminate()


asyncio.run(main())
