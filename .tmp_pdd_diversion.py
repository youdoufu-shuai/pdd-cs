# -*- coding: utf-8 -*-
"""临时：打开拼多多客服工具的分流设置，检查是否可把主账号加入客服。"""
import asyncio
import json
import os
import subprocess
import tempfile
import time
import urllib.request

import paramiko
from websockets.asyncio.client import connect

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PORT = 9357


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
        async with connect(page["webSocketDebuggerUrl"], max_size=30_000_000) as ws:
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
            await send("Page.navigate", {"url": "https://mms.pinduoduo.com/mms-chat/tools"})
            await asyncio.sleep(8)
            print("ELEM:", await ev("""(() => { const el=[...document.querySelectorAll('*')].find(e=>(e.innerText||'').trim()==='分流设置'); return el ? el.parentElement.outerHTML.slice(0,1200) : 'not found'; })()"""))
            print("click:", await ev("""(() => { const el=[...document.querySelectorAll('*')].find(e=>(e.innerText||'').trim()==='分流设置'); if(!el) return 'not found'; (el.closest('a')||el).click(); return 'clicked'; })()"""))
            await asyncio.sleep(6)
            print("URL:", await ev("location.href"))
            print("TEXT:", (await ev("document.body.innerText || ''"))[-2000:])
    finally:
        proc.terminate()


asyncio.run(main())
