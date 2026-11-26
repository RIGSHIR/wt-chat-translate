#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
战争雷霆聊天实时翻译 · 入口

用法：
    python main.py                 # 置顶窗口
    python main.py --console       # 只打终端，用来验证链路
    python main.py --once          # 抓一次当前聊天就退出（不翻译）
    python main.py --dry           # 全套流程但不联网（原文直显）

退出：Ctrl+C，或 Ctrl+F12
"""
from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import core  # noqa: E402

TAG_OF = {"All": "全", "Team": "队", "Squad": "小"}

_LOCK_SOCK = None


def acquire_single_instance(port: int = 47653):
    """绑定一个本地端口做单实例锁；进程退出时自动释放。"""
    global _LOCK_SOCK
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        s.listen(1)
        _LOCK_SOCK = s
        return True
    except OSError:
        return False


def fmt(rec: dict) -> str:
    head = f"[{TAG_OF.get(rec.get('mode'), '?')}] {rec['sender']}"
    t0 = rec.get("_t0")
    cost = f"  ({time.monotonic() - t0:.2f}s)" if t0 else ""
    if rec["dst"] == rec["src"]:
        return f"{head}: {rec['src']}{cost}"
    return f"{head}: {rec['src']}\n      → {rec['dst']}{cost}"


def run_once(cfg: dict) -> None:
    import requests
    s = requests.Session()
    s.trust_env = False
    r = s.get(core.WT_CHAT_URL, params={"lastId": 0}, timeout=3.0)
    data = json.loads(r.content.decode("utf-8"))
    print(f"共 {len(data)} 条")
    for m in data:
        raw = m.get("msg", "")
        norm = core.normalize(raw)
        skip = "跳过" if core.should_skip(norm) else "翻译"
        print(f"  id={m.get('id'):<5} mode={core.map_mode(m.get('mode')):<6} "
              f"enemy={str(m.get('enemy')):<5} [{skip}] {m.get('sender')}: {norm}")


def ensure_stdio() -> None:
    """pythonw 下 sys.stdout 是 None，print 会直接抛异常。改写日志文件。"""
    import os
    if sys.stdout is None or sys.stderr is None:
        log = Path(os.environ.get("TEMP", ".")) / "wt_service.log"
        try:
            f = open(log, "a", encoding="utf-8", errors="replace", buffering=1)
        except Exception:                # noqa: BLE001
            return
        if sys.stdout is None:
            sys.stdout = f
        if sys.stderr is None:
            sys.stderr = f


def ensure_api_key() -> None:
    """从注册表补读用户级环境变量。
    开机自启是由 Explorer 拉起的，它继承的环境块可能早于变量设置，
    直接读注册表最可靠。"""
    import os
    names = ("WT_LLM_KEY", "WT_ZH2EN_KEY")
    missing = [n for n in names if not os.environ.get(n)]
    if not missing:
        return
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            for name in missing:
                try:
                    os.environ[name] = winreg.QueryValueEx(k, name)[0]
                except OSError:
                    pass
    except OSError:
        pass


def main() -> int:
    ensure_stdio()
    ensure_api_key()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:                    # noqa: BLE001
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(HERE / "config.yaml"))
    ap.add_argument("--console", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    cfg = core.load_config(Path(args.config))
    if args.dry:
        cfg.setdefault("privacy", {})["allow_cloud"] = False

    if args.once:
        run_once(cfg)
        return 0

    if not acquire_single_instance():
        print("[停] 已有一个实例在运行。"
              "要先结束它，在任务管理器里关掉 pythonw.exe，或按 Ctrl+F12。")
        return 2

    print(f"\n[启动] {time.strftime('%Y-%m-%d %H:%M:%S')}")

    translator = core.Translator(cfg)
    in_q: "queue.Queue[dict]" = queue.Queue(maxsize=int(cfg["queue"].get("capacity", 30)))

    if not translator._key_of(cfg["llm"]) and cfg["privacy"].get("allow_cloud", True):
        print("[警告] 未找到 API 密钥，将原文直显。"
              "设置环境变量 WT_LLM_KEY 或在 config.yaml 里填 api_key。")

    poller = core.Poller(cfg, in_q, HERE / "state.json")

    # ── 输出端 ──
    if args.console or not cfg["ui"].get("enabled", True):
        def on_ready(recs: list[dict]) -> None:
            for r in recs:
                print(fmt(r), flush=True)
        overlay = None
    else:
        import overlay as ov
        holder: dict = {}
        app = ov.Overlay(cfg["ui"], on_quit=lambda: holder["app"]._shutdown())
        holder["app"] = app
        overlay = app

        if cfg.get("compose", {}).get("enabled", True):
            import composer as cp
            comp = cp.Composer(app.fg, translator, cfg.get("compose", {}),
                               scale=app.scale)

            def _sent(zh: str, en: str) -> None:
                app.root.after(0, lambda: app.push([
                    {"sender": "我", "src": zh, "dst": en,
                     "enemy": False, "mode": "All"}]))

            comp.on_sent = _sent
            app.on_compose = comp.toggle

        def on_ready(recs: list[dict]) -> None:   # noqa: F811
            for r in recs:
                print(fmt(r), flush=True)        # 同时写日志，便于事后回查
            app.enqueue(recs)

    worker = core.TranslatorWorker(cfg, in_q, translator, on_ready)

    poller.start()
    worker.start()
    time.sleep(1.2)
    print(f"[就绪] 8111 {'已连接' if poller.ok else '未连接（进对局后自动恢复）'} | "
          f"模型 {cfg['llm'].get('model')} | "
          f"{'终端模式' if overlay is None else '窗口模式，F12 显隐，Ctrl+F12 退出'}")
    if cfg["privacy"].get("allow_cloud", True):
        print("[提示] 云端翻译已开启 — 聊天内容会发送到远端服务。")
    else:
        print("[提示] 云端翻译已关闭 — 原文直显。")
    print()

    if cfg["privacy"].get("allow_cloud", True) and translator._key_of(cfg["llm"]):
        threading.Thread(target=translator.warmup, daemon=True).start()

    ka = None
    kc = cfg.get("keepalive", {})
    if kc.get("enabled", True) and cfg["privacy"].get("allow_cloud", True) \
            and translator._key_of(cfg["llm"]):
        ka = core.KeepAlive(translator, kc, on_log=lambda s: print(s, flush=True))
        ka.start()

    web = None
    if cfg.get("web", {}).get("enabled", False):
        try:
            import websend
            web = websend.WebSender(translator, cfg.get("web", {}),
                                    on_log=lambda s: print(s, flush=True))
            url = web.start()
            print(f"[手机] 浏览器打开 {url}")
            if web.auth:
                print(f"[手机] PIN = {web.token}（首次访问需要填）")
            else:
                print("[手机] 已关闭 PIN 校验，同网段任何设备都能发")
            print()
        except Exception as e:            # noqa: BLE001
            print(f"[手机] 启动失败：{e}")
            web = None

    try:
        if overlay is None:
            while poller.is_alive() and worker.is_alive():
                time.sleep(0.5)
        else:
            app.run()
    except KeyboardInterrupt:
        pass
    finally:
        poller.stop()
        worker.stop()
        if ka is not None:
            ka.stop()
        if web is not None:
            web.stop()
        if overlay is not None:
            overlay.destroy()
        print(f"\n[统计] {translator.stats} | 已跳过 {poller.skipped} 条 | 队列溢出丢弃 {poller.dropped} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())