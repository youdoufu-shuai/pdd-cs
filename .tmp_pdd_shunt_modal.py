# -*- coding: utf-8 -*-
"""临时：查看拼多多分流设置弹窗，确认可选客服账号。"""
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
PORT = 9358


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
            await asyncio.sleep(8)
            print("before click:", await ev("""JSON.stringify([...document.querySelectorAll('a,button,span,div')].filter(e=>(e.innerText||'').trim()==='设置').slice(0,5).map(e=>e.outerHTML.slice(0,300)))"""))
            print("click:", await ev("""(() => { const el=[...document.querySelectorAll('a,button,span,div')].find(e=>(e.innerText||'').trim()==='设置'); if(!el) return 'none'; el.click(); return 'clicked'; })()"""))
            await asyncio.sleep(2)
            print("dialogs:", await ev("""JSON.stringify([...document.querySelectorAll('[role=dialog],[class*=dialog],[class*=Dialog],[class*=modal],[class*=Modal]')].map(e=>({text:(e.innerText||'').slice(0,1200),display:getComputedStyle(e).display,cls:(e.className||'').toString().slice(0,100)})))"""))
            print("body tail:", (await ev("document.body.innerText || ''"))[-1800:])
            print("inputs/buttons:", await ev("""JSON.stringify({inputs:[...document.querySelectorAll('input,select')].map(e=>({type:e.type,value:e.value,placeholder:e.placeholder,outer:e.outerHTML.slice(0,200)})),buttons:[...document.querySelectorAll('button')].map(e=>({text:(e.innerText||'').trim(),cls:(e.className||'').toString().slice(0,80)})).filter(x=>x.text)})"""))
    finally:
        proc.terminate()


asyncio.run(main())
