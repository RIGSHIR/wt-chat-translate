# -*- coding: utf-8 -*-
"""
中文输入 → 译成英文 → 代打发送到游戏聊天框

注意：这一步用系统级键盘模拟（SendInput），已经不是“纯只读”了。
它不注入 DLL、不读游戏内存、不改游戏文件，等价于一个宏键盘；
但会让按键事件进入游戏进程，属于有一定风险的操作，默认可在 config 里关掉。
"""
from __future__ import annotations

import ctypes
import threading
import time
import tkinter as tk
from ctypes import wintypes as wt

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# ── SendInput ─────────────────────────────────────────────────────
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008
VK_RETURN = 0x0D
VK_CONTROL = 0x11
VK_V = 0x56
SW_RESTORE = 9


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTunion(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _INPUTunion)]


user32.SendInput.argtypes = (wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wt.UINT

# 剪贴板相关：64 位下必须声明签名，否则句柄会被截成 32 位
user32.OpenClipboard.argtypes = (wt.HWND,)
user32.OpenClipboard.restype = wt.BOOL
user32.EmptyClipboard.restype = wt.BOOL
user32.CloseClipboard.restype = wt.BOOL
user32.SetClipboardData.argtypes = (wt.UINT, wt.HANDLE)
user32.SetClipboardData.restype = wt.HANDLE
kernel32.GlobalAlloc.argtypes = (wt.UINT, ctypes.c_size_t)
kernel32.GlobalAlloc.restype = wt.HGLOBAL
kernel32.GlobalLock.argtypes = (wt.HGLOBAL,)
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = (wt.HGLOBAL,)
kernel32.GlobalUnlock.restype = wt.BOOL


def _key_events(vk: int = 0, scan: int = 0, flags: int = 0) -> list[INPUT]:
    def one(f):
        ki = KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=f, time=0, dwExtraInfo=None)
        return INPUT(type=INPUT_KEYBOARD, u=_INPUTunion(ki=ki))
    return [one(flags), one(flags | KEYEVENTF_KEYUP)]


def send_keys(events: list[INPUT]) -> bool:
    arr = (INPUT * len(events))(*events)
    return user32.SendInput(len(events), arr, ctypes.sizeof(INPUT)) == len(events)


def press(vk: int) -> None:
    send_keys(_key_events(vk=vk))
    time.sleep(0.03)


def type_unicode(text: str, chunk: int = 16) -> None:
    """按 Unicode 方式逐字注入；对支持 WM_CHAR 的聊天框最稳。"""
    for i in range(0, len(text), chunk):
        events: list[INPUT] = []
        for ch in text[i:i + chunk]:
            code = ord(ch)
            if code > 0xFFFF:              # 超出 BMP 的字符转代理对
                code -= 0x10000
                for unit in (0xD800 + (code >> 10), 0xDC00 + (code & 0x3FF)):
                    events += _key_events(scan=unit, flags=KEYEVENTF_UNICODE)
            else:
                events += _key_events(scan=code, flags=KEYEVENTF_UNICODE)
        send_keys(events)
        time.sleep(0.02)


def paste() -> None:
    down = INPUT(type=INPUT_KEYBOARD, u=_INPUTunion(ki=KEYBDINPUT(wVk=VK_CONTROL)))
    v_down = INPUT(type=INPUT_KEYBOARD, u=_INPUTunion(ki=KEYBDINPUT(wVk=VK_V)))
    v_up = INPUT(type=INPUT_KEYBOARD,
                 u=_INPUTunion(ki=KEYBDINPUT(wVk=VK_V, dwFlags=KEYEVENTF_KEYUP)))
    up = INPUT(type=INPUT_KEYBOARD,
               u=_INPUTunion(ki=KEYBDINPUT(wVk=VK_CONTROL, dwFlags=KEYEVENTF_KEYUP)))
    send_keys([down, v_down, v_up, up])


# ── 窗口 ──────────────────────────────────────────────────────────
def find_game() -> int:
    hwnd = user32.FindWindowW("DagorWClass", None)
    if not hwnd:
        hwnd = user32.FindWindowW(None, "War Thunder")
    return int(hwnd) if hwnd else 0


def activate(hwnd: int, tries: int = 3) -> bool:
    """把窗口切到前台。Windows 会拦后台抢焦点，用 AttachThreadInput 绕过。"""
    if not hwnd:
        return False
    for _ in range(tries):
        if user32.IsIconic(wt.HWND(hwnd)):
            user32.ShowWindow(wt.HWND(hwnd), SW_RESTORE)
        fg = user32.GetForegroundWindow()
        if fg == hwnd:
            return True
        tid_fg = user32.GetWindowThreadProcessId(wt.HWND(fg), None)
        tid_me = kernel32.GetCurrentThreadId()
        user32.AttachThreadInput(tid_me, tid_fg, True)
        user32.SetForegroundWindow(wt.HWND(hwnd))
        user32.AttachThreadInput(tid_me, tid_fg, False)
        time.sleep(0.08)
        if user32.GetForegroundWindow() == hwnd:
            return True
    return user32.GetForegroundWindow() == hwnd


def set_clipboard(text: str) -> bool:
    """直接走 Win32 剪贴板，不依赖 tk（可在任意线程调用）。"""
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    u = user32
    k = kernel32
    if not u.OpenClipboard(None):
        return False
    try:
        u.EmptyClipboard()
        data = (text + "\0").encode("utf-16-le")
        h = k.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h:
            return False
        p = k.GlobalLock(h)
        if not p:
            return False
        ctypes.memmove(p, data, len(data))
        k.GlobalUnlock(h)
        ok = bool(u.SetClipboardData(CF_UNICODETEXT, h))
        return ok
    finally:
        u.CloseClipboard()


class Composer:
    """按热键弹出的小输入框：中文进去，英文发到游戏里。"""

    def __init__(self, master: tk.Misc, translator, cfg: dict,
                 on_sent=None, on_status=None, scale: float = 1.0,
                 context_provider=None):
        self.master = master
        self.tr = translator
        self.cfg = cfg
        self.on_sent = on_sent
        self.on_status = on_status
        self.scale = scale
        self.context_provider = context_provider   # 返回当前对局上下文文本
        self.busy = False

        self.top = tk.Toplevel(master)
        self.top.withdraw()
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", True)
        self.top.configure(bg="#1B1B1B")

        box = tk.Frame(self.top, bg="#1B1B1B",
                       highlightbackground="#5A5A5A", highlightthickness=1)
        box.pack(fill="both", expand=True)

        tk.Label(box, text="中文 → 英文，回车发送（Esc 取消）", bg="#1B1B1B",
                 fg="#8A8A8A", anchor="w",
                 font=("Microsoft YaHei UI", -int(12 * scale))).pack(
            fill="x", padx=10, pady=(8, 2))

        self.entry = tk.Entry(
            box, bg="#101010", fg="#FFFFFF", insertbackground="#FFFFFF",
            relief="flat", font=("Microsoft YaHei UI", -int(18 * scale)),
            highlightthickness=0, bd=0)
        self.entry.pack(fill="x", padx=10, pady=(2, 4))

        self.status = tk.Label(box, text="", bg="#1B1B1B", fg="#7FD3A0",
                               anchor="w",
                               font=("Microsoft YaHei UI", -int(12 * scale)))
        self.status.pack(fill="x", padx=10, pady=(0, 8))

        self.entry.bind("<Return>", self._submit)
        self.entry.bind("<Escape>", lambda e: self.hide())
        self.entry.bind("<KP_Enter>", self._submit)

        self.top.geometry(f"{int(self.cfg.get('width', 520) * scale)}x"
                          f"{int(self.cfg.get('height', 92) * scale)}")

    # ── 显示 / 隐藏 ──
    def show(self) -> None:
        if self.busy:
            return
        w = int(self.cfg.get("width", 520) * self.scale)
        h = int(self.cfg.get("height", 92) * self.scale)
        x = int(self.cfg.get("x", 9) * self.scale)
        y = int(self.cfg.get("y", 470) * self.scale)
        self.top.geometry(f"{w}x{h}+{x}+{y}")
        self.entry.delete(0, "end")
        self.status.configure(text="")
        self.top.deiconify()
        self.top.lift()
        self.top.update_idletasks()
        hwnd = _top_hwnd(self.top)
        if hwnd and not activate(hwnd):
            self.status.configure(text="无法取得焦点，请手动点一下输入框", fg="#FF9A6B")
        self.entry.focus_force()

    def hide(self) -> None:
        self.top.withdraw()

    def toggle(self) -> None:
        if self.top.winfo_viewable():
            self.hide()
        else:
            self.show()

    # ── 发送 ──
    def _submit(self, event=None) -> None:
        if self.busy:
            return
        text = self.entry.get().strip()
        if not text:
            self.hide()
            return
        self.busy = True
        self.status.configure(text="正在翻译……", fg="#7FD3A0")
        self.hide()
        threading.Thread(target=self._work, args=(text,), daemon=True).start()

    def _work(self, zh: str) -> None:
        try:
            ctx = None
            if self.context_provider:
                try:
                    ctx = self.context_provider()
                except Exception:              # noqa: BLE001
                    pass
            en = self.tr.to_english(zh, context=ctx)
            if not en:
                self._report("翻译失败，未发送", ok=False)
                return
            game = find_game()
            if not game:
                self._report("找不到游戏窗口，未发送", ok=False)
                return

            if self.cfg.get("mode", "clipboard") == "clipboard":
                # 注入按键被游戏过滤（实测），改走剪贴板：用户只需按三下键
                if not set_clipboard(en):
                    self._report("写入剪贴板失败", ok=False)
                    return
                activate(game)
                self._report(f"已复制：{en}")
                if self.on_sent:
                    self.on_sent(zh, en)
                return

            if not activate(game):
                self._report("无法激活游戏窗口，未发送", ok=False)
                return
            time.sleep(0.15)
            press(VK_RETURN)                       # 打开聊天输入
            time.sleep(0.12)
            if self.cfg.get("method", "type") == "paste":
                set_clipboard(en)
                time.sleep(0.05)
                paste()
            else:
                type_unicode(en)
            if self.cfg.get("press_enter", True):
                time.sleep(0.08)
                press(VK_RETURN)                   # 发送
            self._report(f"已发送：{en}")
            if self.on_sent:
                self.on_sent(zh, en)
        except Exception as e:                     # noqa: BLE001
            self._report(f"出错：{e}", ok=False)
        finally:
            self.busy = False

    def _set_clipboard(self, s: str) -> None:
        def _do():
            self.master.clipboard_clear()
            self.master.clipboard_append(s)
        self.master.after(0, _do)
        time.sleep(0.06)

    def _report(self, msg: str, ok: bool = True) -> None:
        def _do():
            self.top.deiconify()
            self.status.configure(text=msg, fg="#7FD3A0" if ok else "#FF8A6B")
            self.top.after(2500, self.hide)
        self.master.after(0, _do)
        if self.on_status:
            self.on_status(msg)


def _top_hwnd(win) -> int:
    h = win.winfo_id()
    top = user32.GetAncestor(wt.HWND(h), 2)
    return int(top) if top else int(h)