# -*- coding: utf-8 -*-
"""
置顶半透明消息窗（tkinter，零额外依赖）

分层设计：tkinter 的 -alpha 只能整体生效，做不到「背景半透明、文字不透明」。
所以拆成两个叠放的窗口：
  bg  —— 底部黑色色带，alpha = ui.alpha（默认 0.41，对齐游戏聊天框实测值）
  fg  —— 文字层，alpha = 1.0，背景用色键做全透明，只有字形是实心的

其他实测要点：
- 游戏窗口类名 DagorWClass，用它判定 overlay 该贴在哪块屏（本机有虚拟显示器）
- 必须鼠标穿透，否则窗口会吃掉视角移动的右键
- 刷新时不要重建控件、不要改窗口几何，否则会闪
"""
from __future__ import annotations

import ctypes
import threading
import tkinter as tk
from ctypes import wintypes as wt

user32 = ctypes.windll.user32

KEY = "#010101"          # 色键：凡这个颜色的像素一律透明


def _enable_dpi_awareness() -> None:
    """必须在创建任何窗口之前调用。
    不开的话 Windows 会按 96 DPI 渲染再整体拉伸 1.5 倍，
    等于对位图做双线性缩放，ClearType 直接被抹平，字就虚。"""
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))   # PER_MONITOR_AWARE_V2
        return
    except Exception:            # noqa: BLE001
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)              # PER_MONITOR_DPI_AWARE
        return
    except Exception:            # noqa: BLE001
        pass
    try:
        user32.SetProcessDPIAware()                                 # 旧系统兜底
    except Exception:            # noqa: BLE001
        pass


def _dpi_scale() -> float:
    """当前 DPI 相对 96 的倍数。未启用感知时系统会返回 96，即 1.0。"""
    try:
        hdc = user32.GetDC(None)
        dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)             # LOGPIXELSY
        user32.ReleaseDC(None, hdc)
        return round(dpi / 96.0, 3) if dpi else 1.0
    except Exception:            # noqa: BLE001
        return 1.0


_enable_dpi_awareness()

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080

SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010
HWND_TOPMOST = -1

VK_F12 = 0x7B
VK_F9 = 0x78
VK_CONTROL = 0x11

COLOR_ALL = "#8FE6A8"
COLOR_TEAM = "#6FB6FF"
COLOR_ENEMY = "#FF7272"


def _top_hwnd(win) -> int:
    h = win.winfo_id()
    top = user32.GetAncestor(wt.HWND(h), 2)      # GA_ROOT
    return int(top) if top else int(h)


def _game_monitor() -> tuple[int, int, int, int] | None:
    """找游戏窗口所在显示器的矩形；找不到返回 None（用主屏兜底）。"""
    hwnd = user32.FindWindowW("DagorWClass", None)
    if not hwnd:
        hwnd = user32.FindWindowW(None, "War Thunder")
    if not hwnd or not user32.IsWindowVisible(wt.HWND(hwnd)):
        return None
    MONITOR_DEFAULTTONEAREST = 2
    mon = user32.MonitorFromWindow(wt.HWND(hwnd), MONITOR_DEFAULTTONEAREST)
    if not mon:
        return None

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT),
                    ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]

    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    if not user32.GetMonitorInfoW(wt.HMONITOR(mon), ctypes.byref(mi)):
        return None
    r = mi.rcMonitor
    return (r.left, r.top, r.right - r.left, r.bottom - r.top)


class Overlay:
    def __init__(self, cfg: dict, on_quit):
        self.cfg = cfg
        self.on_quit = on_quit
        self.records: list[dict] = []

        # 配置里的尺寸按 96 DPI 的「逻辑像素」写，这里换算成物理像素
        self.scale = _dpi_scale()
        self.w = int(round(int(cfg.get("width", 460)) * self.scale))
        self.max_lines = int(cfg.get("max_lines", 6))
        self.margin_x = int(round(int(cfg.get("margin_x", 40)) * self.scale))
        self.margin_y = int(round(int(cfg.get("margin_y", 120)) * self.scale))
        self.anchor = str(cfg.get("anchor", "right")).lower()
        self.gap = int(round(int(cfg.get("line_gap", 6)) * self.scale))
        _lim = int(cfg.get("max_height", 0))
        self._limit = int(round(_lim * self.scale)) if _lim else None
        self.idle_hide = float(cfg.get("idle_hide", 8.0))
        self._bg_alpha = float(cfg.get("alpha", 0.41))       # 只管底层黑带
        self._font = ("Microsoft YaHei UI", -int(cfg.get("font_px", 17)))  # 负值=像素
        self._trim = bool(cfg.get("trim_background", True))

        self._hidden = True
        self._hide_job = None
        self._pending: list[dict] = []
        self._plock = threading.Lock()
        self._rows: list[dict] = []
        self._seq = 0
        self.on_compose = None           # 由 main 注入：Ctrl+F9 输入中译英

        # ── 底层：黑色色带（可整体调透明度）──
        self.bg = tk.Tk()
        self.bg.overrideredirect(True)
        self.bg.attributes("-topmost", True)
        self.bg.attributes("-alpha", 0.0)
        self.bg.configure(bg=KEY)
        if self._trim:
            try:
                self.bg.attributes("-transparentcolor", KEY)
            except tk.TclError:
                self._trim = False

        self._bg_box = tk.Frame(self.bg, bg=KEY)
        self._bg_box.pack(fill="both", expand=True)
        self._bg_spacer = tk.Frame(self._bg_box, bg=KEY)
        self._bg_spacer.pack(side="top", fill="both", expand=True)
        self._band = tk.Frame(self._bg_box, bg="#000000", height=0)
        self._band.pack(side="bottom", fill="x")

        # ── 顶层：文字（完全不透明）──
        self.fg = tk.Toplevel(self.bg)
        self.fg.overrideredirect(True)
        self.fg.attributes("-topmost", True)
        self.fg.attributes("-alpha", 1.0)
        self.fg.configure(bg=KEY)
        if self._trim:
            try:
                self.fg.attributes("-transparentcolor", KEY)
            except tk.TclError:
                pass

        self._inset = int(round(8 * self.scale))
        self._pad = 0
        self.box = tk.Frame(self.fg, bg=KEY)
        self.box.pack(fill="both", expand=True)
        self._spacer = tk.Frame(self.box, bg=KEY)
        self._spacer.pack(side="top", fill="both", expand=True)

        self.bg.update_idletasks()
        self.fg.update_idletasks()
        self.hwnd = _top_hwnd(self.bg)
        self.fg_hwnd = _top_hwnd(self.fg)
        for h in (self.hwnd, self.fg_hwnd):
            self._make_click_through(h)

        self._screen = self._resolve_screen()
        self._fixed_h = self._limit
        self._place()

        self.bg.after(120, self._tick)
        self.bg.after(4000, self._keep_topmost)
        self.bg.after(100, self._poll_keys)
        self.bg.after(80, self._drain)

    def _make_click_through(self, hwnd: int) -> None:
        ex = user32.GetWindowLongW(wt.HWND(hwnd), GWL_EXSTYLE)
        ex |= WS_EX_TOOLWINDOW
        if self.cfg.get("click_through", True):
            ex |= WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE
        user32.SetWindowLongW(wt.HWND(hwnd), GWL_EXSTYLE, ex)

    # ── 屏幕 ──
    def _resolve_screen(self) -> tuple[int, int, int]:
        mon = _game_monitor()
        if mon:
            x, y, w, h = mon
            return (w, h, x)
        return (self.bg.winfo_screenwidth(), self.bg.winfo_screenheight(), 0)

    def _place(self, h: int | None = None) -> None:
        """只在启动和尺寸变化时调用；消息刷新不走这里。"""
        sw, sh, sx = self._screen
        hh = h or self._fixed_h or 1
        x = sx + self.margin_x if self.anchor == "left" else sx + sw - self.w - self.margin_x
        y = self.margin_y + (sh - self.margin_y * 2 - hh) if hh < sh else 0
        geo = f"{self.w}x{hh}+{x}+{y}"
        self.bg.geometry(geo)
        self.fg.geometry(geo)

    # ── 行控件（建在文字层，背景用色键做透明）──
    def _make_row(self, rec: dict) -> dict:
        if rec.get("enemy"):
            color, tag = COLOR_ENEMY, "敌"
        elif rec.get("mode") == "Team":
            color, tag = COLOR_TEAM, "队"
        elif rec.get("mode") == "Squad":
            color, tag = COLOR_TEAM, "小"
        else:
            color, tag = COLOR_ALL, "全"

        row = tk.Frame(self.box, bg=KEY)
        row.pack(side="top", fill="x", pady=(0, self.gap))
        head = tk.Label(
            row, bg=KEY, fg=color, anchor="w", justify="left",
            padx=self._inset, pady=0, bd=0, highlightthickness=0, font=self._font,
            text=f"[{tag}] {rec.get('sender', '?')}: ")
        head.pack(side="left", anchor="n")
        body = tk.Label(
            row, bg=KEY, fg="#FFFFFF", anchor="w", justify="left",
            padx=self._inset, pady=0, bd=0, highlightthickness=0,
            font=self._font, text=rec.get("dst", ""), wraplength=self.w)
        body.pack(side="left", fill="x", expand=True, anchor="n")
        return {"seq": rec["_seq"], "row": row, "head": head, "body": body}

    def _render(self) -> None:
        desired = self.records[-self.max_lines:]
        want = [r["_seq"] for r in desired]
        by_seq = {r["_seq"]: r for r in desired}

        # 1) 只删掉滚出窗口的行，其余控件原样复用
        kept = []
        for item in self._rows:
            if item["seq"] in by_seq:
                kept.append(item)
            else:
                item["row"].destroy()
        self._rows = kept

        # 2) 新行追加到底部；pack 顺序天然就是时间顺序
        have = {it["seq"] for it in self._rows}
        for seq in want:
            if seq not in have:
                self._rows.append(self._make_row(by_seq[seq]))

        self.fg.update_idletasks()

        # 3) 正文换行宽度按前缀实际宽度算
        usable = self.w - self._inset * 2 - int(round(20 * self.scale))
        for it in self._rows:
            wl = max(80, usable - it["head"].winfo_width() - int(round(8 * self.scale)))
            try:
                cur = int(float(it["body"].cget("wraplength")))
            except Exception:            # noqa: BLE001
                cur = -1
            if cur != wl:
                it["body"].configure(wraplength=wl)
        self.fg.update_idletasks()

        # 4) 超过高度上限时从最旧一条开始丢
        if self._limit:
            cap = self._limit - 2
            while len(self._rows) > 1 and self.box.winfo_reqheight() > cap:
                self._rows.pop(0)["row"].destroy()
                self.fg.update_idletasks()

        # 5) 让底层黑带的高度跟上文字块的高度
        block = sum(it["row"].winfo_height() for it in self._rows)
        if int(self._band.cget("height")) != block:
            self._band.configure(height=block)

        # 6) 没设高度上限时才动态改窗口大小
        if not self._fixed_h:
            h = min(self.box.winfo_reqheight() + 2, self._screen[1])
            self._place(h)

    # ── 对外 ──
    def enqueue(self, recs: list[dict]) -> None:
        """可由任意线程调用；真正的 tk 操作在主线程的 _drain 里做。"""
        with self._plock:
            self._pending.extend(recs)

    def _drain(self) -> None:
        with self._plock:
            got, self._pending = self._pending, []
        if got:
            self.push(got)
        self.bg.after(80, self._drain)

    def push(self, recs: list[dict]) -> None:
        for r in recs:
            self._seq += 1
            r["_seq"] = self._seq
        self.records = (self.records + recs)[-self.max_lines * 2:]
        self._render()
        if self._hidden:
            self._show()
        if self._hide_job:
            self.bg.after_cancel(self._hide_job)
        self._hide_job = self.bg.after(int(self.idle_hide * 1000), self._auto_hide)

    def _show(self) -> None:
        self._hidden = False
        self.bg.attributes("-alpha", self._bg_alpha)
        self.fg.attributes("-alpha", 1.0)
        self._raise_fg()

    def _auto_hide(self) -> None:
        self._hidden = True
        self.bg.attributes("-alpha", 0.0)
        self.fg.attributes("-alpha", 0.0)

    def _raise_fg(self) -> None:
        try:
            user32.SetWindowPos(wt.HWND(self.fg_hwnd), wt.HWND(HWND_TOPMOST), 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        except Exception:            # noqa: BLE001
            pass

    def _shutdown(self) -> None:
        self._auto_hide()
        self.bg.quit()

    def _tick(self) -> None:
        self.bg.after(120, self._tick)

    def _keep_topmost(self) -> None:
        """有些游戏会抢 topmost，定时重申；文字层必须压在黑带之上。"""
        try:
            user32.SetWindowPos(wt.HWND(self.hwnd), wt.HWND(HWND_TOPMOST), 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
            self._raise_fg()
        except Exception:            # noqa: BLE001
            pass
        self.bg.after(5000, self._keep_topmost)

    def _poll_keys(self) -> None:
        if user32.GetAsyncKeyState(VK_F12) & 0x8000:
            ctrl = user32.GetAsyncKeyState(0x11) & 0x8000
            if ctrl:
                self.on_quit()
                return
            if self._hidden:
                self._render()
                self._show()
            else:
                self._auto_hide()
            ctypes.windll.kernel32.Sleep(200)
        if self.on_compose and (user32.GetAsyncKeyState(VK_F9) & 0x8000) \
                and (user32.GetAsyncKeyState(VK_CONTROL) & 0x8000):
            self.on_compose()
            ctypes.windll.kernel32.Sleep(250)
        self.bg.after(100, self._poll_keys)

    # 兼容旧调用
    @property
    def root(self):
        return self.bg

    def run(self) -> None:
        self.bg.mainloop()

    def destroy(self) -> None:
        for w in (self.fg, self.bg):
            try:
                w.destroy()
            except Exception:        # noqa: BLE001
                pass