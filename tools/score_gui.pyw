# -*- coding: utf-8 -*-
"""成片六维评分 · 图形界面版（tkinter，可换主题 / 未保存离开会拦一下）

与 `score_core.py`（命令行版）、旧网页版 `评价工具.html` **同一套口径、同一份 json 结构**，
所以 `tools\\评价回收.py` 照常回收。

用法：
    双击 tools\\score_gui.cmd                 # 优先起 dist\\score-tool.exe，没有则 pythonw 起本脚本
    dist\\score-tool.exe --sample 三本书       # 打开即定位到名字含该关键词的样本
    pythonw tools\\score_gui.pyw
"""
import importlib.util
import json         # noqa: F401  （score_core 静态依赖，PyInstaller 需要看得见）
import math
import os
import re
import sys
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog
import argparse     # noqa: F401
import datetime     # noqa: F401

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = getattr(sys, "_MEIPASS", HERE)          # PyInstaller 打包后资源在 _MEIPASS

# ── 主题 ────────────────────────────────────────────────────────────────────
THEMES = {
    "浅色": dict(bg="#F4F6F9", surface="#FFFFFF", surface_hover="#EDF1F8", border="#E2E7EF",
                 text="#1E2430", muted="#7A8497", accent="#2F6FED", accent_text="#FFFFFF",
                 accent_soft="#E8F0FE", header="#FFFFFF", star_empty="#D8DEE9",
                 ok="#12A150", warn="#D98B1F", bad="#E5484D"),
    "深色": dict(bg="#14171C", surface="#1B2028", surface_hover="#252C36", border="#2C333D",
                 text="#E8EBF0", muted="#8B94A5", accent="#5B8DEF", accent_text="#0C1220",
                 accent_soft="#1F2B41", header="#1B2028", star_empty="#39414E",
                 ok="#30C08A", warn="#E5B04A", bad="#F06A6A"),
    "莫兰迪": dict(bg="#EDE9E3", surface="#FAF8F5", surface_hover="#EFEAE3", border="#DED7CE",
                  text="#3C3936", muted="#8A8178", accent="#7D8F76", accent_text="#FFFFFF",
                  accent_soft="#E4EAE0", header="#FAF8F5", star_empty="#D6CFC6",
                  ok="#6E8B7B", warn="#C79A5B", bad="#B4685F"),
    "护眼绿": dict(bg="#E7EFE5", surface="#F6FAF4", surface_hover="#E3EEDF", border="#CFDECB",
                  text="#22331F", muted="#6B7F66", accent="#2E7D32", accent_text="#FFFFFF",
                  accent_soft="#DEEDDC", header="#F6FAF4", star_empty="#C6D6C2",
                  ok="#2E7D32", warn="#B8860B", bad="#C62828"),
    "暗夜": dict(bg="#0D1524", surface="#15203A", surface_hover="#1D2B49", border="#26334F",
                 text="#E6EDF7", muted="#8AA0C0", accent="#4F8CFF", accent_text="#08122A",
                 accent_soft="#1C2C4C", header="#15203A", star_empty="#33436A",
                 ok="#35C48F", warn="#E0B34C", bad="#F27272"),
}
DEFAULT_THEME = "浅色"

_FONTS = {}


def F(name="body"):
    """字体缓存（必须在 Tk() 之后调用）。"""
    if name not in _FONTS:
        fam = "Microsoft YaHei UI"
        spec = {"title": (fam, 15, "bold"), "h2": (fam, 11, "bold"), "body": (fam, 10),
                "small": (fam, 9), "stat": (fam, 13, "bold"), "star": (fam, 19)}
        s = spec[name]
        kw = dict(family=s[0], size=s[1])
        if len(s) > 2:
            kw["weight"] = s[2]
        _FONTS[name] = tkfont.Font(**kw)
    return _FONTS[name]


def _rr(cv, x1, y1, x2, y2, r, **kw):
    """圆角矩形（Canvas polygon + smooth）。"""
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return cv.create_polygon(pts, smooth=True, **kw)


# ── 颜色工具（做立体感用：渐变 / 提亮 / 压暗）────────────────────────────────
def _hx(c):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _mix(c1, c2, t):
    """按比例混合两色，t=0 取 c1，t=1 取 c2。"""
    a, b = _hx(c1), _hx(c2)
    return "#%02X%02X%02X" % tuple(
        max(0, min(255, int(round(a[i] + (b[i] - a[i]) * t)))) for i in range(3))


def _lit(c, k):
    return _mix(c, "#FFFFFF", k)


def _dim(c, k):
    return _mix(c, "#000000", k)


def _lum(c):
    r, g, b = _hx(c)
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0


def _is_dark(color):
    return _lum(color) < 0.5


def _rr_vgrad(cv, x1, y1, x2, y2, r, top, bottom, tag="grad", steps=0):
    """竖向渐变的圆角矩形：逐行画短线，圆角处按半径公式内缩。

    tkinter 的 Canvas 没有渐变填充，用「一行一色 + 圆角内缩」逼近，
    视觉上与真渐变几乎无差，且填充边界仍然圆角。
    """
    hh, ww = y2 - y1, x2 - x1
    if hh <= 0 or ww <= 0:
        return
    steps = steps or max(3, int(hh))
    step_h = hh / float(steps)
    r = max(0.0, min(r, ww / 2.0, hh / 2.0))
    for i in range(steps):
        yc = y1 + step_h * (i + 0.5)
        inset = 0.0
        for d in (yc - y1, y2 - yc):                 # 上圆角 / 下圆角
            if d < r:
                inset = max(inset, r - math.sqrt(max(0.0, r * r - (r - d) ** 2)))
        cv.create_line(x1 + inset, yc, x2 - inset, yc,
                       fill=_mix(top, bottom, (i + 0.5) / float(steps)),
                       width=step_h + 1.2, tags=tag)


def _load_core():
    for d in (BASE, HERE):
        p = os.path.join(d, "score_core.py")
        if os.path.isfile(p):
            spec = importlib.util.spec_from_file_location("score_core", p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    raise RuntimeError("找不到 score_core.py（应与本脚本同在 tools\\ 目录）")


core = _load_core()
DIMS = [(k, opts) for k, opts, _s in core.DIMS]
FORBID = [k for k, _s in core.FORBID]
CONCL = ["可用", "可改", "作废"]


def _theme_file():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "HeronBoScoreTool")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        return None
    return os.path.join(d, "theme.txt")


def load_theme_name():
    p = _theme_file()
    if p and os.path.isfile(p):
        try:
            n = open(p, encoding="utf-8").read().strip()
            if n in THEMES:
                return n
        except OSError:
            pass
    return DEFAULT_THEME


def save_theme_name(name):
    p = _theme_file()
    if not p:
        return
    try:
        open(p, "w", encoding="utf-8").write(name)
    except OSError:
        pass


# ── 自绘控件 ────────────────────────────────────────────────────────────────
class Segmented(tk.Canvas):
    """分段选择器（立体：投影 + 渐变 + 悬停 / 选中高亮）。"""

    def __init__(self, master, options, value=None, on_change=None, theme=None,
                 font=None, padx=14, pady=6, gap=6, radius=9, bg_key="surface", depth=3):
        super().__init__(master, highlightthickness=0, bd=0, takefocus=0)
        self.options = list(options)
        self.value = value if value in self.options else (self.options[0] if self.options else None)
        self.on_change = on_change
        self.theme = theme
        self.font = font or F("body")
        self.padx, self.pady, self.gap, self.radius = padx, pady, gap, radius
        self.bg_key = bg_key
        self.depth = max(2, int(depth))
        self._boxes, self._hover, self._press_i = [], None, None
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Motion>", self._on_motion)
        self.bind("<Leave>", self._on_leave)
        self._build()

    def _build(self):
        self._boxes = []
        x = 2
        h = self.font.metrics("linespace") + self.pady * 2 + 6 + self.depth
        for opt in self.options:
            w = self.font.measure(opt) + self.padx * 2
            self._boxes.append((x, w))
            x += w + self.gap
        self._cw = max(0, x - self.gap - 2) + 2
        self._ch = h
        self.configure(width=self._cw, height=h)
        self._recolor()

    def _recolor(self):
        t = self.theme
        dark = _is_dark(t[self.bg_key])
        self.delete("all")
        self.configure(bg=t[self.bg_key])
        sh = _dim(t[self.bg_key], 0.60) if dark else _dim(t[self.bg_key], 0.24)
        for i, (x, w) in enumerate(self._boxes):
            sel = self.options[i] == self.value
            d = self.depth - 2 if i == self._press_i else self.depth
            y0 = 1.5 + (self.depth - d)
            y1 = self._ch - 1.5 - d
            if sel:
                base, fg = t["accent"], t["accent_text"]
                top = _lit(base, 0.22 if not dark else 0.15)
                bottom = _dim(base, 0.18 if not dark else 0.24)
                edge = _mix(bottom, "#000000", 0.20)
            else:
                base = t["surface_hover"] if i == self._hover else t["surface"]
                fg, edge = t["text"], t["border"]
                top = _lit(base, 0.14) if dark else _lit(base, 0.20)
                bottom = _dim(base, 0.22) if dark else _dim(base, 0.15)
                if i == self._hover:
                    top = _lit(top, 0.06)
            _rr(self, x, y0 + d, x + w, self._ch - 1.5, self.radius, outline="", fill=sh)
            _rr_vgrad(self, x, y0, x + w, y1, self.radius, top, bottom)
            _rr(self, x, y0, x + w, y1, self.radius, outline=edge, fill="", width=1)
            self.create_line(x + self.radius + 1.5, y0 + 1.6, x + w - self.radius - 1.5, y0 + 1.6,
                             fill=_lit(top, 0.30 if dark else 0.55), width=1.3)
            self.create_text(x + w / 2.0, (y0 + y1) / 2.0, text=self.options[i],
                             font=self.font, anchor="center", fill=fg)

    def _hit(self, x, y):
        if y > self._ch - 1.5:
            return None
        for i, (bx, w) in enumerate(self._boxes):
            if bx <= x <= bx + w:
                return i
        return None

    def _on_motion(self, e):
        i = self._hit(e.x, e.y)
        self.configure(cursor="hand2" if i is not None else "")
        self._set_hover(i)

    def _on_leave(self, _e):
        self._press_i = None
        self._set_hover(None)

    def _set_hover(self, i):
        if i != self._hover:
            self._hover = i
            self._recolor()

    def _on_press(self, e):
        self._press_i = self._hit(e.x, e.y)
        self._recolor()

    def _on_release(self, e):
        i = self._hit(e.x, e.y)
        was, self._press_i = self._press_i, None
        self._recolor()
        if i is None or i != was:
            return
        v = self.options[i]
        if v != self.value:
            self.value = v
            self._recolor()
            if self.on_change:
                self.on_change(v)

    def get(self):
        return self.value

    def set(self, v):
        if v in self.options and v != self.value:
            self.value = v
            self._recolor()

    def apply_theme(self, theme):
        self.theme = theme
        self._recolor()


class Stars(tk.Canvas):
    """五星评分（点击、hover 预览）。"""

    def __init__(self, master, value=4, on_change=None, theme=None, gap=4, bg_key="surface"):
        super().__init__(master, highlightthickness=0, bd=0, takefocus=0)
        self.value, self.on_change, self.theme = value, on_change, theme
        self.gap, self.bg_key, self._hover, self._items = gap, bg_key, None, []
        self.font = F("star")
        self.bind("<Button-1>", self._on_click)
        self.bind("<Motion>", self._on_motion)
        self.bind("<Leave>", lambda e: self._set_hover(None))
        self._build()

    def _build(self):
        self.delete("all")
        self._items, self._shadows = [], []
        w = self.font.measure("★") + self.gap
        h = self.font.metrics("linespace") + 7
        for i in range(5):
            cx, cy = w * i + w / 2.0, h / 2.0
            self._shadows.append(self.create_text(cx + 1.0, cy + 1.5, text="★",
                                                  font=self.font, anchor="center"))
            self._items.append(self.create_text(cx, cy - 0.5, text="★",
                                                font=self.font, anchor="center"))
        self.configure(width=w * 5, height=h)
        self._recolor()

    def _recolor(self):
        t = self.theme
        self.configure(bg=t[self.bg_key])
        sh = _dim(t[self.bg_key], 0.55) if _is_dark(t[self.bg_key]) else _dim(t[self.bg_key], 0.30)
        shown = self._hover if self._hover else self.value
        for i in range(5):
            self.itemconfig(self._shadows[i], fill=sh)
            self.itemconfig(self._items[i], fill=t["accent"] if i < shown else t["star_empty"])

    def _on_motion(self, e):
        i = int(e.x // max(1, self.font.measure("★") + self.gap)) + 1
        self.configure(cursor="hand2")
        self._set_hover(max(1, min(5, i)))

    def _set_hover(self, v):
        if v != self._hover:
            self._hover = v
            self._recolor()

    def _on_click(self, e):
        i = int(e.x // max(1, self.font.measure("★") + self.gap)) + 1
        self.value = max(1, min(5, i))
        self._recolor()
        if self.on_change:
            self.on_change(self.value)

    def get(self):
        return self.value

    def set(self, v):
        try:
            self.value = max(1, min(5, int(v)))
        except (TypeError, ValueError):
            pass
        self._recolor()

    def apply_theme(self, theme):
        self.theme = theme
        self._recolor()


class Pill(tk.Canvas):
    """立体圆角按钮。kind: primary / ghost / danger。

    立体感是四层叠出来的：①投影 → ②竖向渐变主体 → ③顶部高光 → ④描边；
    按下时主体下沉、投影变薄，做出"按进去"的手感。
    """

    def __init__(self, master, text, command=None, theme=None, font=None,
                 kind="primary", padx=18, pady=8, radius=9, bg_key="bg", depth=4):
        super().__init__(master, highlightthickness=0, bd=0, takefocus=0)
        self.text, self.command, self.theme = text, command, theme
        self.font = font or F("body")
        self.kind, self.padx, self.pady, self.radius, self.bg_key = kind, padx, pady, radius, bg_key
        self.depth = max(2, int(depth))
        self._hover = self._press = False
        self.bind("<Enter>", lambda e: self._set(hover=True))
        self.bind("<Leave>", lambda e: self._set(hover=False))
        self.bind("<Button-1>", lambda e: self._set(press=True))
        self.bind("<ButtonRelease-1>", self._release)
        self._build()

    # ---- 几何 -------------------------------------------------------------
    # 注意：不能用 self._w / self._h —— _w 是 tkinter 内部存 Tcl 控件名的属性，
    #       覆盖它会让控件直接失效（invalid command name）。
    def _build(self):
        self._cw = self.font.measure(self.text) + self.padx * 2
        # 高度要多留 depth 给投影，否则阴影会被 canvas 裁掉
        self._ch = self.font.metrics("linespace") + self.pady * 2 + 6 + self.depth
        self.configure(width=self._cw, height=self._ch)
        self._draw()

    def _draw(self):
        self.delete("all")
        w, h = self._cw, self._ch
        d = self.depth - 2 if self._press else self.depth
        y0 = 1.5 + (self.depth - d)             # 主体顶（按下时下移）
        y1 = h - 1.5 - d                        # 主体底
        top, bottom, edge, fg = self._colors()
        # ① 投影
        _rr(self, 1.5, y0 + d, w - 1.5, h - 1.5, self.radius, outline="", fill=self._shadow())
        # ② 渐变主体
        _rr_vgrad(self, 1.5, y0, w - 1.5, y1, self.radius, top, bottom)
        # ③ 描边（补回渐变行在圆角处内缩留下的毛边）
        _rr(self, 1.5, y0, w - 1.5, y1, self.radius, outline=edge, fill="", width=1)
        # ④ 顶部高光——"凸起"的关键一笔
        self.create_line(2.5 + self.radius, y0 + 1.7, w - 2.5 - self.radius, y0 + 1.7,
                         fill=self._gloss(top), width=1.4)
        # ⑤ 文字（彩色按钮加一层淡投影，字更"浮"得起来）
        cy = (y0 + y1) / 2.0
        if self.kind in ("primary", "danger") and not self._press:
            self.create_text(w / 2.0 + 1, cy + 1.2, text=self.text, font=self.font,
                             anchor="center", fill=self._shadow())
        self.create_text(w / 2.0, cy, text=self.text, font=self.font,
                         anchor="center", fill=fg)

    def _colors(self):
        t = self.theme
        dark = _is_dark(t[self.bg_key])
        if self.kind == "primary":
            base, fg = t["accent"], t["accent_text"]
        elif self.kind == "danger":
            base, fg = t["bad"], t["accent_text"]
        else:
            base, fg = t["surface"], t["text"]
            if self._hover and not self._press:
                base = t["surface_hover"]
        if self._press:                                  # 按下：整体压暗、上下反差收紧
            top, bottom = _dim(base, 0.08), _dim(base, 0.22)
        elif dark:
            top, bottom = _lit(base, 0.14), _dim(base, 0.22)
        else:
            top, bottom = _lit(base, 0.20), _dim(base, 0.15)
            if self.kind == "ghost":                     # 白底按钮：顶部略灰才看得出渐变
                top = _mix(base, t["border"], 0.32)
                bottom = _mix(base, t["border"], 1.0)
        if self._hover and not self._press:
            top = _lit(top, 0.07)
        if self.kind == "ghost":
            edge = t["accent"] if self._hover else t["border"]
        else:
            edge = _mix(bottom, "#000000", 0.16)
        return top, bottom, edge, fg

    def _shadow(self):
        bg = self.theme[self.bg_key]
        return _dim(bg, 0.60) if _is_dark(bg) else _dim(bg, 0.26)

    def _gloss(self, top):
        bg = self.theme[self.bg_key]
        return _lit(top, 0.30 if _is_dark(bg) else 0.55)

    def _recolor(self):
        self.configure(bg=self.theme[self.bg_key])
        self._draw()

    def _set(self, hover=None, press=None):
        if hover is not None:
            self._hover = hover
            self.configure(cursor="hand2" if hover else "")
        if press is not None:
            self._press = press
        self._recolor()

    def _release(self, e):
        was = self._press
        self._press = False
        self._recolor()
        if was and 0 <= e.x <= self.winfo_width() and 0 <= e.y <= self.winfo_height():
            if self.command:
                self.command()

    def apply_theme(self, theme):
        self.theme = theme
        self._recolor()


class Modal(tk.Toplevel):
    """主题化模态对话框；ask() 返回所按按钮的 key（关闭/Esc 返回 None）。"""

    @classmethod
    def ask(cls, parent, theme, title, message, buttons):
        d = cls(parent, theme, title, message, buttons)
        parent.wait_window(d)
        return d.result

    def __init__(self, parent, theme, title, message, buttons):
        super().__init__(parent)
        self.result = None
        self.theme = theme
        self.title(title)
        self.configure(bg=theme["surface"])
        self.resizable(False, False)
        self.transient(parent)

        box = tk.Frame(self, bg=theme["surface"])
        box.pack(fill="both", expand=True, padx=24, pady=20)
        tk.Label(box, text=title, bg=theme["surface"], fg=theme["text"],
                 font=F("h2"), anchor="w").pack(fill="x")
        tk.Label(box, text=message, bg=theme["surface"], fg=theme["muted"], font=F("body"),
                 anchor="w", justify="left", wraplength=360).pack(fill="x", pady=(8, 18))
        row = tk.Frame(box, bg=theme["surface"])
        row.pack(fill="x")
        for label, key, kind in buttons:
            Pill(row, label, lambda k=key: self._choose(k), theme=theme, font=F("body"),
                 kind=kind, bg_key="surface").pack(side="right", padx=(8, 0))

        self.protocol("WM_DELETE_WINDOW", lambda: self._choose(None))
        self.bind("<Escape>", lambda e: self._choose(None))
        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        self.geometry("%dx%d+%d+%d" % (w, h, px + max(0, (pw - w) // 2),
                                       py + max(0, (ph - h) // 3)))
        self.grab_set()
        self.focus_force()

    def _choose(self, key):
        self.result = key
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()


# ── 主界面 ──────────────────────────────────────────────────────────────────
class App:
    def __init__(self, root, preselect=None):
        self.root = root
        self.theme = THEMES[load_theme_name()]
        self._loading = False
        self.dirty = False
        self.drafts = {}

        root.title("Seedance 成片六维评分")
        root.geometry("1040x726")
        root.minsize(940, 640)
        root.configure(bg=self.theme["bg"])
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.samples_root = self._detect_root()
        self.samples = core.scan(self.samples_root) if self.samples_root else []

        self._build()
        self.reload()
        self._preselect(preselect)
        self.apply_theme(load_theme_name(), first=True)

        root.bind("<Control-s>", lambda e: self.save())
        root.lift()
        try:
            root.attributes("-topmost", True)
            root.after(500, lambda: root.attributes("-topmost", False))
        except tk.TclError:
            pass
        try:
            root.focus_force()
        except tk.TclError:
            pass

    # ---- 样本库根 ---------------------------------------------------------
    def _ref_dirs(self):
        """从本脚本/exe 所在目录逐级向上，列出可能的 references 目录。"""
        out, d = [], HERE
        for _ in range(5):
            out.append(os.path.join(d, "references"))
            nd = os.path.dirname(d)
            if nd == d:
                break
            d = nd
        extra = os.path.join(BASE, "references")
        if extra not in out:
            out.append(extra)
        return out

    @staticmethod
    def _root_from_file(path):
        try:
            txt = open(path, encoding="utf-8-sig").read()
        except OSError:
            return None
        for line in txt.splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            if k.strip() != "SAMPLES_ROOT":
                continue
            v = v.strip().strip("`").strip('"').strip()
            if v and os.path.isdir(v):
                return os.path.abspath(v)
        return None

    def _detect_root(self):
        try:
            return core.detect_root(None)
        except SystemExit:
            pass
        for d in self._ref_dirs():                      # exe 在 tools\dist\ 也能找到技能根的 references
            p = self._root_from_file(os.path.join(d, "paths.local.md"))
            if p:
                return p
        picked = filedialog.askdirectory(title="请选择样本库根目录（各项目目录的上一级）")
        if not picked:
            return None
        picked = os.path.abspath(picked)
        self._remember_root(picked)
        return picked

    def _remember_root(self, root_dir):
        """把样本库根写回就近的 paths.local.md；写不进去就只本次生效。"""
        dirs = self._ref_dirs()
        target = None
        for d in dirs:                                  # 优先改已有的那份
            if os.path.isfile(os.path.join(d, "paths.local.md")):
                target = d
                break
        if target is None:                              # 都没有 → 建在技能根的 references
            target = dirs[min(1, len(dirs) - 1)]
        c = os.path.join(target, "paths.local.md")
        try:
            os.makedirs(target, exist_ok=True)
            if os.path.isfile(c):
                txt = open(c, encoding="utf-8-sig").read()
                if re.search(r"(?m)^\s*SAMPLES_ROOT\s*=", txt):
                    txt = re.sub(r"(?m)^\s*SAMPLES_ROOT\s*=.*$", "SAMPLES_ROOT=" + root_dir, txt)
                else:
                    txt = "SAMPLES_ROOT=%s\n" % root_dir + txt
            else:
                txt = "SAMPLES_ROOT=%s\nAI_CREATE_ROOT=\nPLATFORM=\nCLI=\n" % root_dir
            open(c, "w", encoding="utf-8", newline="").write(txt)
        except OSError:
            pass

    # ---- 界面骨架 ---------------------------------------------------------
    def _build(self):
        t = self.theme
        self._frames, self._labels, self._dyn = [], [], []

        header = tk.Frame(self.root, bg=t["header"])
        header.pack(fill="x")
        self._frames.append((header, "header", "bg"))
        hl = tk.Frame(header, bg=t["header"])
        hl.pack(side="left", padx=(20, 0), pady=14)
        self._frames.append((hl, "header", "bg"))
        self._lab(hl, "Seedance 成片六维评分", "title").pack(anchor="w")
        self._lab(hl, "点分 → 保存 → agent 按 json 优化规则", "small", "muted").pack(anchor="w", pady=(2, 0))

        hr = tk.Frame(header, bg=t["header"])
        hr.pack(side="right", padx=(0, 20), pady=14)
        self._frames.append((hr, "header", "bg"))
        self._lab(hr, "主题", "small", "muted").pack(side="left", padx=(0, 8))
        self.theme_seg = Segmented(hr, list(THEMES.keys()), value=load_theme_name(),
                                   on_change=self.apply_theme, theme=t, font=F("small"),
                                   padx=11, pady=5, bg_key="header", depth=3)
        self.theme_seg.pack(side="left")
        self._dyn.append(self.theme_seg)

        self.root_path_lab = self._lab(self.root, "", "small", "muted")
        self.root_path_lab.pack(anchor="w", padx=22, pady=(10, 0))

        body = tk.Frame(self.root, bg=t["bg"])
        body.pack(fill="both", expand=True, padx=20, pady=12)
        self._frames.append((body, "bg", "bg"))

        # 左：样本列表
        leftcard = tk.Frame(body, bg=t["border"])
        leftcard.pack(side="left", fill="y")
        self._frames.append((leftcard, "border", "bg"))
        left = tk.Frame(leftcard, bg=t["surface"])
        left.pack(fill="both", expand=True, padx=1, pady=1)
        self._frames.append((left, "surface", "bg"))
        self._lab(left, "样本", "h2").pack(anchor="w", padx=12, pady=(12, 6))
        lrow = tk.Frame(left, bg=t["surface"])
        lrow.pack(fill="both", expand=True, padx=(12, 6), pady=(0, 12))
        self._frames.append((lrow, "surface", "bg"))
        self.lb = tk.Listbox(lrow, width=26, activestyle="none", bd=0, highlightthickness=0,
                             exportselection=False, font=F("body"))
        self.lb.pack(side="left", fill="both", expand=True)
        self.lb.bind("<<ListboxSelect>>", lambda e: self.on_sample())
        self.lb.bind("<Motion>", self._list_hover)
        self.lb.bind("<Leave>", lambda e: self._list_hover(None))
        self._hover_idx = None
        sb = tk.Scrollbar(lrow, orient="vertical", command=self.lb.yview, width=9,
                          bd=0, highlightthickness=0, relief="flat",
                          elementborderwidth=0, activerelief="flat")
        sb.pack(side="right", fill="y", padx=(2, 0))
        self.lb.config(yscrollcommand=sb.set)
        self._sb = sb

        rrow = tk.Frame(left, bg=t["surface"])
        rrow.pack(fill="x", padx=12, pady=(0, 12))
        self._frames.append((rrow, "surface", "bg"))
        self.refresh_pill = Pill(rrow, "刷新样本", self.reload, theme=t, font=F("small"),
                                 kind="ghost", padx=12, pady=6, bg_key="surface", depth=3)
        self.refresh_pill.pack(side="left")
        self._dyn.append(self.refresh_pill)

        # 右：评分表单
        rightcard = tk.Frame(body, bg=t["border"])
        rightcard.pack(side="left", fill="both", expand=True, padx=(14, 0))
        self._frames.append((rightcard, "border", "bg"))
        right = tk.Frame(rightcard, bg=t["surface"])
        right.pack(fill="both", expand=True, padx=1, pady=1)
        self._frames.append((right, "surface", "bg"))
        form = tk.Frame(right, bg=t["surface"])
        form.pack(fill="both", expand=True, padx=18, pady=16)
        self._frames.append((form, "surface", "bg"))
        form.columnconfigure(1, weight=1)
        self._row = 0

        self._lab(form, "成片", "body").grid(row=self._row, column=0, sticky="w", pady=(0, 10))
        self.video_var = tk.StringVar()
        self.video_var.trace_add("write", lambda *a: self._touch())
        self.video_menu = tk.OptionMenu(form, self.video_var, "")
        self.video_menu.config(anchor="w", relief="flat", bd=0, highlightthickness=1, width=46,
                               font=F("body"), activebackground=t["surface_hover"])
        self.video_menu.grid(row=self._row, column=1, sticky="w", pady=(0, 10))
        self.video_menu["menu"].config(bd=0, activeborderwidth=0, font=F("body"))
        self._row += 1

        self.vars = {}
        for key, opts in DIMS:
            self._lab(form, key, "body").grid(row=self._row, column=0, sticky="w", pady=5)
            seg = Segmented(form, opts, value=opts[0], on_change=lambda v: self._touch(),
                            theme=t, font=F("body"))
            seg.grid(row=self._row, column=1, sticky="w", pady=5)
            self.vars[key] = seg
            self._dyn.append(seg)
            self._row += 1

        self._lab(form, "违禁项", "body").grid(row=self._row, column=0, sticky="w", pady=8)
        fbox = tk.Frame(form, bg=t["surface"])
        fbox.grid(row=self._row, column=1, sticky="w", pady=8)
        self._frames.append((fbox, "surface", "bg"))
        for key in FORBID:
            cell = tk.Frame(fbox, bg=t["surface"])
            cell.pack(side="left", padx=(0, 18))
            self._frames.append((cell, "surface", "bg"))
            self._lab(cell, key, "small", "muted").pack(anchor="w")
            seg = Segmented(cell, ["无", "有"], value="无", on_change=lambda v: self._touch(),
                            theme=t, font=F("small"), padx=11, pady=4)
            seg.pack(anchor="w")
            self.vars[key] = seg
            self._dyn.append(seg)
        self._row += 1

        self._lab(form, "整体评分", "body").grid(row=self._row, column=0, sticky="w", pady=8)
        self.stars = Stars(form, value=4, on_change=lambda v: self._touch(), theme=t)
        self.stars.grid(row=self._row, column=1, sticky="w", pady=8)
        self._dyn.append(self.stars)
        self._row += 1

        self._lab(form, "结论", "body").grid(row=self._row, column=0, sticky="w", pady=6)
        self.concl = Segmented(form, CONCL, value="可改", on_change=lambda v: self._touch(),
                               theme=t, font=F("body"))
        self.concl.grid(row=self._row, column=1, sticky="w", pady=6)
        self.vars["结论"] = self.concl
        self._dyn.append(self.concl)
        self._row += 1

        self._lab(form, "备注", "body").grid(row=self._row, column=0, sticky="w", pady=(8, 0))
        self.note = tk.Entry(form, relief="flat", bd=0, highlightthickness=1, font=F("body"),
                             insertwidth=2)
        self.note.grid(row=self._row, column=1, sticky="ew", ipady=6, pady=(8, 0))
        self.note.bind("<KeyRelease>", lambda e: self._touch())
        self._row += 1

        # 底栏
        footer = tk.Frame(self.root, bg=t["bg"])
        footer.pack(fill="x", padx=20, pady=(0, 16))
        self._frames.append((footer, "bg", "bg"))
        self.status = self._lab(footer, "选一个样本 → 逐项点分 → 保存", "small", "muted")
        self.status.pack(side="left")
        self.save_pill = Pill(footer, "保存评分", self.save, theme=t, font=F("h2"),
                              kind="primary", padx=24, pady=9, bg_key="bg", depth=5)
        self.save_pill.pack(side="right")
        self._dyn.append(self.save_pill)

    def _lab(self, parent, text, font="body", color="text"):
        w = tk.Label(parent, text=text, font=F(font), anchor="w")
        self._labels.append((w, color, parent))
        return w

    # ---- 主题 -------------------------------------------------------------
    def apply_theme(self, name, first=False):
        if name not in THEMES:
            return
        self.theme = THEMES[name]
        t = self.theme
        for w, bg_key, _ in self._frames:
            try:
                w.config(bg=t[bg_key])
            except tk.TclError:
                pass
        for w, color, parent in self._labels:
            pkey = "bg"
            for pw, pk, _ in self._frames:
                if pw is parent:
                    pkey = pk
                    break
            try:
                w.config(bg=t[pkey], fg=t[color])
            except tk.TclError:
                pass
        self.root.configure(bg=t["bg"])
        self.lb.config(bg=t["surface"], fg=t["text"], selectbackground=t["accent_soft"],
                       selectforeground=t["text"])
        try:
            self.lb.config(inactiveselectbackground=t["accent_soft"])
        except tk.TclError:
            pass
        self._sb.config(bg=t["surface"], troughcolor=t["bg"], activebackground=t["muted"],
                        highlightbackground=t["surface"])
        self.video_menu.config(bg=t["surface"], fg=t["text"], highlightbackground=t["border"],
                               activebackground=t["surface_hover"], activeforeground=t["text"])
        self.video_menu["menu"].config(bg=t["surface"], fg=t["text"],
                                       activebackground=t["accent_soft"],
                                       activeforeground=t["text"])
        self.note.config(bg=t["surface"], fg=t["text"], insertbackground=t["accent"],
                         highlightbackground=t["border"], highlightcolor=t["accent"],
                         disabledbackground=t["surface"])
        for d in self._dyn:
            d.apply_theme(t)
        self.theme_seg.set(name)
        self._recolor_list()
        if not first:
            save_theme_name(name)

    def _recolor_list(self):
        t = self.theme
        for i in range(self.lb.size()):
            if self.lb.selection_includes(i):
                continue
            self.lb.itemconfig(i, background=t["surface_hover"] if i == self._hover_idx
                               else t["surface"], foreground=t["text"])
        for i in self.lb.curselection():
            self.lb.itemconfig(i, background=t["accent_soft"], foreground=t["text"])

    def _list_hover(self, e):
        if e is None or self.lb.size() == 0:
            idx = None
        else:
            idx = self.lb.nearest(e.y)
            if idx is not None and (e.y < 0 or e.y > self.lb.winfo_height() or idx >= self.lb.size()):
                idx = None
        if idx != self._hover_idx:
            self._hover_idx = idx
            self._recolor_list()
            self.lb.config(cursor="hand2" if idx is not None else "")

    # ---- 数据 -------------------------------------------------------------
    def reload(self):
        if not self.samples_root:
            self.status.config(text="没有样本库根目录——点「刷新样本」可重新选择")
            return
        self.samples = core.scan(self.samples_root)
        keep = self.cur_name()
        self.lb.delete(0, tk.END)
        for s in self.samples:
            if s["reviews"]:
                mark = "✓"
            elif s["videos"]:
                mark = "●"
            else:
                mark = "○"
            self.lb.insert(tk.END, "%s %s   (%d片/%d评)" % (mark, s["name"], len(s["videos"]), len(s["reviews"])))
        self.root_path_lab.config(text="样本库：%s    ● 待评分（有片无评）  ✓ 已评分" % self.samples_root)
        self.status.config(text="共 %d 个样本" % len(self.samples))
        self._select_by_name(keep)

    def cur_name(self):
        s = self.cur()
        return s["name"] if s else None

    def cur(self):
        sel = self.lb.curselection()
        return self.samples[sel[0]] if sel else None

    def _select_by_name(self, name):
        if not name:
            return
        for i, s in enumerate(self.samples):
            if s["name"] == name:
                self.lb.selection_clear(0, tk.END)
                self.lb.selection_set(i)
                self.lb.see(i)
                self.on_sample()
                return

    def _preselect(self, hint):
        """打开时定位：--sample 关键词 > 最新一个待评分样本 > 第一个样本。"""
        idx = 0
        if hint:
            for i, s in enumerate(self.samples):
                if hint in s["name"]:
                    idx = i
                    break
        else:
            for i, s in enumerate(self.samples):
                if s["videos"] and not s["reviews"]:
                    idx = i
                    break
        if self.samples:
            self.lb.selection_clear(0, tk.END)
            self.lb.selection_set(idx)
            self.lb.see(idx)
            self.on_sample()

    def _videos_of(self, s):
        return s["videos"] or ["（无成片）"]

    def on_sample(self):
        s = self.cur()
        if not s:
            return
        self._loading = True
        try:
            menu = self.video_menu["menu"]
            menu.delete(0, "end")
            vids = self._videos_of(s)
            for v in vids:
                menu.add_command(label=v, command=lambda vv=v: self.video_var.set(vv))
            self.video_var.set(vids[0])

            draft = self.drafts.get(s["name"])
            src = draft or (s["reviews"][-1][1] if s["reviews"] else {})
            for key, _o in DIMS:
                self.vars[key].set((src.get("六维") or {}).get(key, self.vars[key].options[0]))
            for key in FORBID:
                self.vars[key].set((src.get("违禁项") or {}).get(key, "无"))
            self.stars.set(src.get("整体评分") or 4)
            self.concl.set(src.get("结论") or "可改")
            self.note.delete(0, tk.END)
            self.note.insert(0, src.get("备注") or "")
        finally:
            self._loading = False
        self.dirty = bool(self.drafts)      # 别的样本有未保存草稿也算「有未保存」
        if s["reviews"]:
            name, _j = s["reviews"][-1]
            self.status.config(text="已载入上次评价（%s），改完记得保存" % name)
        else:
            self.status.config(text="%s：还没有评价，打完分点右下角「保存评分」" % s["name"])
        self._recolor_list()

    def _touch(self):
        if not self._loading:
            self.dirty = True
            self._stash_draft()

    def _stash_draft(self):
        s = self.cur()
        if not s:
            return
        self.drafts[s["name"]] = {
            "六维": {k: self.vars[k].get() for k, _o in DIMS},
            "违禁项": {k: self.vars[k].get() for k in FORBID},
            "整体评分": self.stars.get(),
            "结论": self.concl.get(),
            "备注": self.note.get(),
        }

    # ---- 保存 / 关闭 ------------------------------------------------------
    def save(self):
        s = self.cur()
        if not s:
            Modal.ask(self.root, self.theme, "还没选样本", "先在左边点一个样本，再保存。",
                      [("知道了", "ok", "primary")])
            return False
        video = self.video_var.get() if s["videos"] else ""
        if not s["videos"] and video == "（无成片）":
            video = ""
        dims = {k: self.vars[k].get() for k, _o in DIMS}
        forb = {k: self.vars[k].get() for k in FORBID}
        try:
            fn = core.save(self.samples_root, s["name"], video, dims, forb,
                           self.stars.get(), self.concl.get(), self.note.get())
        except Exception as e:                                  # noqa: BLE001
            Modal.ask(self.root, self.theme, "保存失败", str(e), [("知道了", "ok", "danger")])
            return False
        self.drafts.pop(s["name"], None)
        self.dirty = bool(self.drafts)
        name = s["name"]
        self.reload()
        self._select_by_name(name)
        self.status.config(text="已保存 → %s" % fn)
        return True

    def on_close(self):
        if not self.dirty:
            self.root.destroy()
            return
        choice = Modal.ask(
            self.root, self.theme, "还有没保存的评分",
            "你改了评分但还没点「保存评分」。现在离开的话，这次打分就丢了，"
            "agent 也拿不到 json 来优化规则。",
            [("返回继续", "cancel", "ghost"), ("直接关闭", "discard", "danger"),
             ("保存并关闭", "save", "primary")])
        if choice == "cancel" or choice is None:
            return
        if choice == "save":
            if not self.save():
                return
        self.root.destroy()


def _parse_args():
    a, out = sys.argv[1:], None
    for i, x in enumerate(a):
        if x in ("--sample", "-s") and i + 1 < len(a):
            out = a[i + 1]
        elif x.startswith("--sample="):
            out = x.split("=", 1)[1]
    return out


def main():
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:                                            # noqa: BLE001
        pass
    root = tk.Tk()
    try:
        dpi = root.winfo_fpixels("1i")
        root.tk.call("tk", "scaling", dpi / 72.0)
    except tk.TclError:
        pass
    try:
        App(root, _parse_args())
    except Exception:                                            # noqa: BLE001
        import traceback
        detail = traceback.format_exc()
        path = os.path.join(os.environ.get("TEMP", "."), "score_gui_crash.log")
        try:
            open(path, "w", encoding="utf-8").write(detail)
        except OSError:
            path = "(日志写入失败)"
        try:
            from tkinter import messagebox
            messagebox.showerror("评分工具启动失败",
                                 "%s\n\n完整日志：%s" % (detail.strip().splitlines()[-1], path))
        except Exception:                                        # noqa: BLE001
            pass
        return
    root.mainloop()


if __name__ == "__main__":
    main()
