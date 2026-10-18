# -*- coding: utf-8 -*-
"""临时：打开拼多多子账号新增弹窗，检查需要填写哪些字段（不提交）。"""
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
PORT = 9356


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
            await send("Runtime.enable"); await send("Page.enable"); await send("Network.enable")
            for name, value in jar.items():
                for domain in (".pinduoduo.com", "mms.pinduoduo.com"):
                    try:
                        await send("Network.setCookie", {"name": name, "value": str(value), "domain": domain, "path": "/", "secure": True, "httpOnly": False})
                    except Exception:
                        pass
            await send("Page.navigate", {"url": "https://mms.pinduoduo.com/mallcenter/sub/account"})
            await asyncio.sleep(8)
            print("before text tail:", (await ev("document.body.innerText || ''"))[-400:])
            print("click:", await ev("""(() => {
              const el=[...document.querySelectorAll('button,a,div,span')].find(e => (e.innerText||'').trim() === '+新增子账号');
              if(!el) return 'not found';
              el.click(); return 'clicked';
            })()"""))
            await asyncio.sleep(3)
            print("dialog text:", await ev("""(() => {
              const d=[...document.querySelectorAll('[role=dialog],[class*=dialog],[class*=Dialog],[class*=modal]')].find(e => (e.innerText||'').length > 50);
              return d ? d.innerText.slice(0,2000) : 'no dialog';
            })()"""))
            print("inputs:", await ev("""JSON.stringify([...document.querySelectorAll('input,select,textarea')].map(e=>({tag:e.tagName,type:e.type||'',placeholder:e.placeholder||'',name:e.name||'',value:(e.value||'').slice(0,40),outer:e.outerHTML.slice(0,220)})))"""))
            print("buttons:", await ev("""JSON.stringify([...document.querySelectorAll('button')].map(e=>({text:(e.innerText||'').trim(),disabled:e.disabled,cls:(e.className||'').toString().slice(0,80)})).filter(x=>x.text).slice(0,40))"""))
            print("dialog html:", await ev("""(() => {
              const d=[...document.querySelectorAll('[role=dialog],[class*=dialog],[class*=Dialog],[class*=modal]')].find(e => (e.innerText||'').length > 50);
              return d ? d.outerHTML.slice(0,5000) : 'no dialog';
            })()"""))
            shot = await send("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True, "clip": {"x": 0, "y": 0, "width": 1440, "height": 1200, "scale": 1}})
            Path(r"C:\Users\24942\Documents\deepseek-harness\default-workspace\pdd-cs\docs\pdd-subaccount-form.png").write_bytes(base64.b64decode(shot["result"]["data"]))
    finally:
        proc.terminate()


asyncio.run(main())
