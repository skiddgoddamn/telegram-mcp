#!/usr/bin/env python3
"""Telegram QR login on a localhost page (auto-refresh + 2FA field).

Telethon's QR link expires (~1 min) and the stock generator renders an ASCII QR
(crashes on a Windows cp1251 console) and prompts for the 2FA password on stdin
(hangs in a headless run). This serves the whole flow on
http://127.0.0.1:<TG_QR_PORT> and writes TELEGRAM_SESSION_STRING to .env on
success. The 2FA password is POSTed to localhost only.
"""
import asyncio
import http.server
import io
import os
import sys
import threading
import time
import urllib.parse
import webbrowser
from datetime import datetime, timezone

from dotenv import load_dotenv
from telethon import TelegramClient, errors
from telethon.sessions import StringSession

load_dotenv()
API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
HOST = "127.0.0.1"
PORT = int(os.environ.get("TG_QR_PORT", "5200"))
DEADLINE_S = int(os.environ.get("TG_QR_TIMEOUT", "600"))

_state = {"qr": None, "status": "waiting", "hint": None}
_password = {"value": None}
_lock = threading.Lock()

_PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Telegram — привязка устройства</title>
<style>
 :root{color-scheme:dark}
 body{font-family:system-ui,Segoe UI,Roboto,sans-serif;background:#0e1621;color:#e9eef3;
   margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center}
 .card{background:#17212b;padding:32px 36px;border-radius:18px;text-align:center;
   box-shadow:0 12px 48px #000a;max-width:420px}
 h1{font-size:17px;font-weight:600;margin:0 0 20px;line-height:1.4}
 .frame{width:320px;min-height:320px;margin:0 auto;background:#fff;border-radius:14px;
   display:flex;align-items:center;justify-content:center;overflow:hidden}
 .frame img{width:300px;height:300px}
 .ok{font-size:72px;line-height:320px}
 .st{color:#8fa3b4;font-size:14px;margin:18px 0 0}
 .hint{color:#6b7c8c;font-size:12px;margin:8px 0 0}
 form{display:flex;flex-direction:column;gap:12px;padding:22px;width:82%}
 form .lbl{color:#333;font-size:14px}
 form input{padding:11px;font-size:16px;border:1px solid #bbb;border-radius:8px}
 form button{padding:11px;font-size:15px;border:0;border-radius:8px;background:#3390ec;color:#fff;cursor:pointer}
</style></head>
<body><div class="card">
 <h1>Telegram &rarr; Настройки &rarr; Устройства &rarr; Подключить устройство &rarr; сканировать</h1>
 <div class="frame" id="frame"><span class="st">Ожидание QR…</span></div>
 <p class="st" id="st">Ожидание QR…</p>
 <p class="hint">QR обновляется автоматически. Пароль (если запросит) уходит только на localhost.</p>
</div>
<script>
let mode=null;
function esc(s){return (s||'').replace(/[<>&"]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c]));}
async function tick(){
 try{
  const s=await (await fetch('/state',{cache:'no-store'})).json();
  const frame=document.getElementById('frame'), st=document.getElementById('st');
  if(s.status==='open'){ if(mode!=='open'){mode='open';frame.innerHTML='<div class="ok">✅</div>';} st.textContent='Подключено — можно закрыть вкладку.'; return; }
  if(s.status==='password'){
   if(mode!=='password'){ mode='password';
    frame.innerHTML='<form id="pf"><div class="lbl">Облачный пароль (2FA Telegram)'+(s.hint?': '+esc(s.hint):'')+'</div><input id="pw" type="password" autocomplete="off" autofocus><button>Отправить</button></form>';
    document.getElementById('pf').onsubmit=async e=>{e.preventDefault();const v=document.getElementById('pw').value;
     await fetch('/password',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'password='+encodeURIComponent(v)});
     mode='verifying';document.getElementById('frame').innerHTML='<span class="st">Проверка пароля…</span>';};
   }
   st.textContent='Требуется облачный пароль (2FA).';
  }
  else if(s.status==='verifying'){ if(mode!=='verifying'){mode='verifying';frame.innerHTML='<span class="st">Проверка…</span>';} st.textContent='Проверка…'; }
  else if(s.hasQr){ mode='qr'; frame.innerHTML='<img alt="QR" src="/qr.svg?t='+Date.now()+'">'; st.textContent='QR активен, обновляется автоматически.'; }
  else{ st.textContent='Ожидание QR…'; }
 }catch(e){}
 setTimeout(tick,2000);
}
tick();
</script></body></html>""".encode("utf-8")


def _render_svg(link: str) -> bytes:
    import qrcode
    import qrcode.image.svg

    img = qrcode.make(link, image_factory=qrcode.image.svg.SvgImage, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue()


class _H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        r = self.path.split("?", 1)[0]
        if r == "/":
            self._s(200, "text/html; charset=utf-8", _PAGE)
        elif r == "/state":
            with _lock:
                has = "true" if _state["qr"] else "false"
                hint, st = _state["hint"], _state["status"]
            hj = "null" if hint is None else '"' + hint.replace('"', "'") + '"'
            self._s(200, "application/json", f'{{"status":"{st}","hasQr":{has},"hint":{hj}}}'.encode())
        elif r == "/qr.svg":
            with _lock:
                link = _state["qr"]
            if not link:
                self._s(204, "text/plain", b"")
                return
            try:
                self._s(200, "image/svg+xml", _render_svg(link))
            except Exception:
                self._s(500, "text/plain", b"")
        else:
            self._s(404, "text/plain", b"")

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/password":
            self._s(404, "text/plain", b"")
            return
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n).decode("utf-8", "replace") if n else ""
        pw = urllib.parse.parse_qs(raw).get("password", [""])[0]
        with _lock:
            _password["value"] = pw
            _state["status"] = "verifying"
        self._s(200, "application/json", b'{"ok":true}')

    def _s(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)


_httpd = None
_opened = [False]


def _ensure():
    global _httpd
    if _httpd:
        return
    try:
        http.server.ThreadingHTTPServer.allow_reuse_address = True
        _httpd = http.server.ThreadingHTTPServer((HOST, PORT), _H)
    except OSError as e:
        print(f"server err {e}", file=sys.stderr)
        return
    threading.Thread(target=_httpd.serve_forever, daemon=True).start()
    print(f"Login page: http://{HOST}:{PORT}", file=sys.stderr, flush=True)


def show_qr(url: str) -> None:
    with _lock:
        _state["qr"] = url
        _state["status"] = "pending"
        _state["hint"] = None
    _ensure()
    print(f"TG login url: {url}", file=sys.stderr, flush=True)
    if not _opened[0]:
        _opened[0] = True
        try:
            webbrowser.open(f"http://{HOST}:{PORT}")
        except Exception:
            pass


async def wait_password() -> str:
    with _lock:
        _state["status"] = "password"
        _state["hint"] = "введите пароль из настроек Telegram"
        _state["qr"] = None
        _password["value"] = None
    deadline = time.time() + DEADLINE_S
    while time.time() < deadline:
        with _lock:
            v = _password["value"]
            _password["value"] = None
        if v:
            return v
        await asyncio.sleep(0.5)
    raise RuntimeError("2FA password not entered in time")


def _secs(qr) -> float:
    e = qr.expires
    if e.tzinfo is None:
        e = e.replace(tzinfo=timezone.utc)
    return max(5.0, (e - datetime.now(timezone.utc)).total_seconds() - 1.0)


def _write_env(key: str, val: str) -> None:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        lines = []
    for i, l in enumerate(lines):
        if l.startswith(key + "="):
            lines[i] = f"{key}={val}\n"
            break
    else:
        lines.append(f"{key}={val}\n")
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)


async def main() -> None:
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        qr = await client.qr_login()
        show_qr(qr.url)
        for _ in range(30):
            try:
                await qr.wait(timeout=_secs(qr))
                break
            except asyncio.TimeoutError:
                await qr.recreate()
                show_qr(qr.url)
            except errors.SessionPasswordNeededError:
                pw = await wait_password()
                await client.sign_in(password=pw)
                break
        else:
            print("QR expired too many times; rerun.", file=sys.stderr)
            await client.disconnect()
            sys.exit(1)

    with _lock:
        _state["status"] = "open"
        _state["qr"] = None
    ss = StringSession.save(client.session)
    _write_env("TELEGRAM_SESSION_STRING", ss)
    me = await client.get_me()
    uname = f"@{me.username}" if me.username else "(no username)"
    print(f"TG_LOGIN_OK id={me.id} user={uname}", flush=True)
    await asyncio.sleep(2)
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
