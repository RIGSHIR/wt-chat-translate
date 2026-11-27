# -*- coding: utf-8 -*-
"""
网址分发器：把本地发送页的局域网地址上传到云服务器，
手机访问云端静态页拿到地址后，实际通信仍走局域网。

云端只需要一个能托管静态文件的空间（nginx / apache / 对象存储静态网站均可）：
  - index.html  分发页（本目录 dispatch_page.html，上传一次即可）
  - url.json    电脑端周期性上传的地址快照

传输默认走 OpenSSH scp（Windows 自带），不需要在云服务器上跑任何服务进程。
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


class Dispatcher(threading.Thread):
    def __init__(self, cfg: dict, url_provider, on_log=print):
        super().__init__(daemon=True, name="dispatcher")
        self.cfg = cfg
        self.url_provider = url_provider      # 返回当前局域网发送页 URL
        self.on_log = on_log
        self.stop_evt = threading.Event()
        self.interval = float(cfg.get("interval", 30))
        self.last_url = None
        self.ok = False

    # ── 上传 ──
    def _scp(self, local: Path, remote_name: str) -> bool:
        host = self.cfg.get("host", "")
        user = self.cfg.get("user", "")
        port = str(self.cfg.get("port", 22))
        rdir = self.cfg.get("remote_dir", "").rstrip("/")
        if not (host and user and rdir):
            return False
        target = f"{user}@{host}:{rdir}/{remote_name}"
        cmd = ["scp", "-P", port, "-o", "BatchMode=yes",
               "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new"]
        key = self.cfg.get("key_path", "")
        if key:
            cmd += ["-i", str(Path(key).expanduser())]
        cmd += [str(local), target]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                self.on_log(f"[分发] scp 失败: {(r.stderr or '').strip()[:160]}")
                return False
            return True
        except Exception as e:                # noqa: BLE001
            self.on_log(f"[分发] scp 异常: {e}")
            return False

    def _publish_once(self) -> bool:
        url = self.url_provider()
        if not url:
            return False
        payload = {"url": url, "ts": int(time.time())}
        tmp = HERE / "_dispatch_url.json"
        tmp.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
        ok = self._scp(tmp, "url.json")
        # 分发页本身也顺带同步（内容不变时 scp 开销极小）
        page = HERE / "dispatch_page.html"
        if page.exists():
            self._scp(page, "index.html")
        try:
            tmp.unlink()
        except Exception:                     # noqa: BLE001
            pass
        if ok and url != self.last_url:
            self.on_log(f"[分发] 地址已更新: {url}")
        self.last_url = url
        self.ok = ok
        return ok

    def run(self) -> None:
        # 启动即发一次，之后按间隔；地址变化时立即补发
        self._publish_once()
        while not self.stop_evt.is_set():
            self.stop_evt.wait(self.interval)
            if self.stop_evt.is_set():
                break
            try:
                url = self.url_provider()
                if url != self.last_url or not self.ok:
                    self._publish_once()
                else:
                    self._publish_once()   # 心跳：刷新 ts，让云端页知道电脑还在线
            except Exception as e:         # noqa: BLE001
                self.on_log(f"[分发] 异常: {e}")

    def stop(self) -> None:
        self.stop_evt.set()