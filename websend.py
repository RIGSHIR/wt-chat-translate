# -*- coding: utf-8 -*-
"""
局域网发送页：手机浏览器输入中文 → 译成英文 → 代打发送到游戏聊天

用标准库 http.server，不引入任何依赖。
安全：绑定令牌 + 默认每次启动随机生成，令牌不对一律拒绝。
"""
from __future__ import annotations

import json
import random
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import composer as cp

PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<meta name="theme-color" content="#101010">
<title>战雷聊天</title>
<style>
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;padding:14px;background:#0d0d0d;color:#e8e8e8;
     font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
h1{font-size:15px;font-weight:500;color:#8a8a8a;margin:0 0 10px}
textarea{width:100%;height:110px;padding:12px;font-size:18px;line-height:1.4;
  background:#1a1a1a;color:#fff;border:1px solid #333;border-radius:10px;
  resize:none;outline:none;font-family:inherit}
textarea:focus{border-color:#4a7}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0}
.chip{padding:8px 12px;font-size:14px;background:#1e1e1e;border:1px solid #333;
  border-radius:20px;color:#bbb}
.chip:active{background:#2a2a2a}
button{width:100%;padding:16px;font-size:18px;font-weight:600;color:#0b0b0b;
  background:#7fd3a0;border:0;border-radius:10px}
button:disabled{background:#3a3a3a;color:#777}
.status{margin-top:12px;font-size:14px;color:#7fd3a0;min-height:20px;word-break:break-word}
.log{margin-top:14px;font-size:13px;color:#666;line-height:1.7}
.log div{border-top:1px solid #222;padding:6px 0}
.en{color:#9ad}
.pin{margin-bottom:10px}
.pin input{width:100%;padding:10px;font-size:16px;background:#1a1a1a;color:#fff;
  border:1px solid #333;border-radius:8px;outline:none}
</style></head><body>
<h1>战争雷霆 · 手机发送</h1>
<div class="pin" id="pinBox" style="display:none">
  <input id="pin" placeholder="输入 PIN（电脑端日志里有）" inputmode="numeric">
</div>
<textarea id="txt" placeholder="输入中文，回车发送……"></textarea>
<div class="chips" id="chips"></div>
<button id="send">发送</button>
<div class="status" id="st"></div>
<div class="log" id="log"></div>
<script>
const CHIPS=["掩护我","我来了","小心后面","干得好","别挡道","收到","先占点","我被打坏了","需要支援","谢谢"];
const st=document.getElementById('st'), log=document.getElementById('log');
const txt=document.getElementById('txt'), btn=document.getElementById('send');
const pinBox=document.getElementById('pinBox'), pin=document.getElementById('pin');
let busy=false;

let token=localStorage.getItem('wtkey')||'';
if(__AUTH__===1&&!token) pinBox.style.display='block';
pin.addEventListener('input',()=>{token=pin.value.trim();localStorage.setItem('wtkey',token);});

const chips=document.getElementById('chips');
CHIPS.forEach(t=>{
  const d=document.createElement('div');
  d.className='chip'; d.textContent=t;
  d.onclick=()=>{txt.value=t;txt.focus();};
  chips.appendChild(d);
});

function addLog(zh,en,ok){
  const d=document.createElement('div');
  d.innerHTML='<span style="color:#ccc">'+zh.replace(/</g,'&lt;')+'</span><br>'+
              '<span class="en">'+(ok?en.replace(/</g,'&lt;'):'✗ '+en)+'</span>';
  log.prepend(d);
}

async function send(){
  const t=txt.value.trim();
  if(!t||busy) return;
  busy=true; btn.disabled=true; st.textContent='翻译并发送中……'; st.style.color='#888';
  try{
    const r=await fetch('/send',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({text:t,token:token})});
    const j=await r.json();
    if(j.ok){ st.textContent=j.en+(j.msg==='ok'?'':'　·　'+j.msg); st.style.color='#7fd3a0';
              addLog(t,j.en,true); txt.value=''; }
    else   { st.textContent='失败：'+j.msg; st.style.color='#ff8a6b';
              addLog(t,j.msg,false); }
  }catch(e){ st.textContent='网络错误：'+e; st.style.color='#ff8a6b'; }
  busy=false; btn.disabled=false;
}
btn.onclick=send;
txt.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send();}});
setTimeout(()=>txt.focus(),300);
</script></body></html>
"""


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:            # noqa: BLE001
        return "127.0.0.1"
    finally:
        s.close()


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    token = ""
    auth = True                  # False = 不验 PIN，同网段任何人都能发
    send = None                  # 由外部注入：(text) -> (ok, en, msg)
    _lock = threading.Lock()


class _Handler(BaseHTTPRequestHandler):
    server_version = "WTChat/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):        # 不往 stderr 刷访问日志
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: dict, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self):                        # noqa: N802
        if self.path in ("/", "/index.html"):
            body = PAGE.replace("__AUTH__", "1" if self.server.auth else "0")
            self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")
        else:
            self._json({"ok": False, "msg": "not found"}, 404)

    def do_POST(self):                       # noqa: N802
        if self.path != "/send":
            self._json({"ok": False, "msg": "not found"}, 404)
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception:                    # noqa: BLE001
            self._json({"ok": False, "msg": "请求格式错误"}, 400)
            return
        if self.server.auth and str(body.get("token", "")) != self.server.token:
            self._json({"ok": False, "msg": "PIN 不正确"}, 403)
            return
        text = str(body.get("text", "")).strip()[:200]
        if not text:
            self._json({"ok": False, "msg": "内容为空"}, 400)
            return
        try:
            ok, en, msg = self.server.send(text)
        except Exception as e:               # noqa: BLE001
            self._json({"ok": False, "msg": f"服务异常：{e}"}, 500)
            return
        self._json({"ok": ok, "en": en, "msg": msg})


class WebSender:
    def __init__(self, translator, cfg: dict, on_log=print, context_provider=None):
        self.tr = translator
        self.cfg = cfg
        self.on_log = on_log
        self.context_provider = context_provider   # 返回当前对局上下文文本
        self.srv: _Server | None = None
        self.thread: threading.Thread | None = None
        self.auth = bool(cfg.get("auth", True))
        self.token = (str(cfg.get("token", "")).strip() or f"{random.randint(1000, 9999)}")
        if not self.auth:
            self.token = ""

    # ── 发送流程 ──
    def _do_send(self, zh: str) -> tuple[bool, str, str]:
        with _Server._lock:                  # 串行，避免两条消息交错
            ctx = None
            if self.context_provider:
                try:
                    ctx = self.context_provider()
                except Exception:              # noqa: BLE001
                    pass
            try:
                en = self.tr.to_english(zh, context=ctx)
            except Exception as e:           # noqa: BLE001
                return False, "", f"翻译失败：{e}"
            if not en:
                return False, "", "翻译结果为空"
            game = cp.find_game()
            if not game:
                return False, en, "游戏未运行"

            if self.cfg.get("mode", "clipboard") == "clipboard":
                # 实测游戏会丢弃合成按键，改走剪贴板
                if not cp.set_clipboard(en):
                    return False, en, "写入剪贴板失败"
                cp.activate(game)
                self.on_log(f"[手机] {zh}  →  {en}（已复制）")
                return True, en, "已复制，请在游戏里按 Enter → Ctrl+V → Enter"

            if not cp.activate(game):
                return False, en, "无法激活游戏窗口"
            time.sleep(0.15)
            cp.press(cp.VK_RETURN)           # 打开聊天输入
            time.sleep(0.12)
            cp.type_unicode(en)
            if self.cfg.get("press_enter", True):
                time.sleep(0.08)
                cp.press(cp.VK_RETURN)       # 发送
            self.on_log(f"[手机] {zh}  →  {en}")
            return True, en, "ok"

    # ── 生命周期 ──
    def start(self) -> str:
        host = str(self.cfg.get("host", "0.0.0.0"))
        port = int(self.cfg.get("port", 8765))
        srv = _Server((host, port), _Handler)
        srv.token = self.token
        srv.auth = self.auth
        srv.send = self._do_send
        self.srv = srv
        self.thread = threading.Thread(target=srv.serve_forever, daemon=True,
                                       name="websend")
        self.thread.start()
        return f"http://{lan_ip()}:{port}/"

    def stop(self) -> None:
        if self.srv:
            self.srv.shutdown()
            self.srv.server_close()
            self.srv = None