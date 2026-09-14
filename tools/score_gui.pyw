# -*- coding: utf-8 -*-
"""成片六维评分 · 图形界面版（tkinter，可换主题 / 未保存离开会拦一下）

与 `score_core.py`（命令行版）、旧网页版 `评价工具.html` **同一套口径、同一份 json 结构**，
所以 `tools\\评价回收.py` 照常回收。

用法：
    双击 tools\\score_gui.cmd                 # 优先起 dist\\score-tool.exe，没有则 pythonw 起本脚本
    dist\\score-tool.exe --sample 三本书       # 打开即定位到名字含该关键词的样本
    pythonw tools\\score_gui.pyw
"""
import argparse     # noqa: F401  （score_core 静态依赖，PyInstaller 需要看得见）
import base64
import datetime     # noqa: F401
import importlib.util
import json         # noqa: F401
import math
import os
import re
import struct
import sys
import tkinter as tk
import tkinter.font as tkfont
import zlib
from tkinter import filedialog

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


# ── 抗锯齿圆角贴图（纯 Python 生成 PNG，零第三方依赖）─────────────────────────
# 为什么要自己画：Tk 的**文字**走系统渲染、本来就抗锯齿，但 Canvas 画出来的
# 圆角、圆形是硬锯齿——这正是"不像网页"的根因。这里用 SDF 距离场算 1px 边缘
# 过渡（抗锯齿），把柔和投影一起按背景色合成好，再编码成 PNG 交给 PhotoImage。
_IMG_CACHE = {}
_AA = 0.5          # 边缘过渡宽度（像素）


def _sdf_rr(px, py, x1, y1, x2, y2, r):
    """点到圆角矩形边界的距离（矩形内部为负、外部为正）。"""
    dx = max(x1 + r - px, 0.0, px - (x2 - r))
    dy = max(y1 + r - py, 0.0, py - (y2 - r))
    return math.hypot(dx, dy) - r


def _png_bytes(w, h, rows):
    """RGB 行数据 → PNG 字节流（8bit truecolor）。"""
    raw = b"".join(b"\x00" + bytes(r) for r in rows)

    def chunk(tag, data):
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def _clamp8(v):
    return 0 if v < 0 else (255 if v > 255 else int(v + 0.5))


def _rr_photo(w, h, r, fill, bg, border=None, border_w=1.0,
              shadow=None, shadow_dy=2.0, shadow_blur=6.0, shadow_alpha=0.28,
              box=None, fill2=None, glow=None, glow_blur=9.0, glow_alpha=0.55,
              inner_top=None, inner_alpha=0.5):
    """生成「抗锯齿圆角矩形 + 柔和外投影」贴图。

    贴图已按 `bg` 合成、完全不透明，所以直接铺在 Canvas 上即可，不需要 alpha。
    `box=(x1,y1,x2,y2)` 指定矩形在贴图内的位置（默认铺满），留白用来容纳投影。
    `fill2` 给竖向渐变（上 fill → 下 fill2）。
    `glow` 给彩色外发光（游戏感来源：同色相、无偏移、大范围低透明）。
    `inner_top` 给顶部内高光（模拟受光的立体面）。
    返回 tk.PhotoImage；生成失败返回 None（调用方回退到 Canvas 直绘）。
    """
    w, h = int(round(w)), int(round(h))
    if w <= 0 or h <= 0:
        return None
    if box is None:
        x1, y1, x2, y2 = 0.0, 0.0, float(w), float(h)
    else:
        x1, y1, x2, y2 = [float(v) for v in box]
    r = max(0.0, min(float(r), (x2 - x1) / 2.0, (y2 - y1) / 2.0))
    key = (w, h, round(r, 2), fill, bg, border, round(border_w, 2), shadow,
           round(shadow_dy, 2), round(shadow_blur, 2), round(shadow_alpha, 3),
           round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1),
           fill2, glow, round(glow_blur, 2), round(glow_alpha, 3),
           inner_top, round(inner_alpha, 3))
    if key in _IMG_CACHE:
        return _IMG_CACHE[key]
    img = None
    try:
        bgc, fillc = _hx(bg), _hx(fill)
        fill2c = _hx(fill2) if fill2 else None
        bdc = _hx(border) if border else None
        shc = _hx(shadow) if shadow else None
        glc = _hx(glow) if glow else None
        itc = _hx(inner_top) if inner_top else None
        span = max(1.0, y2 - y1)
        rows = []
        for y in range(h):
            row = bytearray()
            for x in range(w):
                px, py = x + 0.5, y + 0.5
                a_sh = min(1.0, max(0.0, _AA - _sdf_rr(px, py, x1, y1, x2, y2, r)))
                cr, cg, cb = bgc
                if glc:                      # 彩色外发光：无偏移、大范围、低透明
                    gd = _sdf_rr(px, py, x1, y1, x2, y2, r)
                    a = max(0.0, min(1.0, (glow_blur - gd) / glow_blur))
                    a = a * a * (3 - 2 * a) * glow_alpha
                    if a > 0.002:
                        cr += (glc[0] - cr) * a
                        cg += (glc[1] - cg) * a
                        cb += (glc[2] - cb) * a
                if shc:                      # 柔和投影：SDF 距离做平滑衰减
                    sd = _sdf_rr(px, py - shadow_dy, x1, y1, x2, y2, r)
                    a = max(0.0, min(1.0, (shadow_blur - sd) / shadow_blur))
                    a = a * a * (3 - 2 * a) * shadow_alpha
                    if a > 0.002:
                        cr += (shc[0] - cr) * a
                        cg += (shc[1] - cg) * a
                        cb += (shc[2] - cb) * a
                if a_sh > 0.002:
                    if fill2c:               # 竖向渐变
                        k = max(0.0, min(1.0, (py - y1) / span))
                        fc = (fillc[0] + (fill2c[0] - fillc[0]) * k,
                              fillc[1] + (fill2c[1] - fillc[1]) * k,
                              fillc[2] + (fill2c[2] - fillc[2]) * k)
                    else:
                        fc = fillc
                    cr += (fc[0] - cr) * a_sh
                    cg += (fc[1] - cg) * a_sh
                    cb += (fc[2] - cb) * a_sh
                    if itc:                  # 顶部内高光：只在上缘 2px 内叠加
                        tt = max(0.0, 1.0 - (py - y1) / 2.0) * inner_alpha
                        if tt > 0.002:
                            cr += (itc[0] - cr) * tt * a_sh
                            cg += (itc[1] - cg) * tt * a_sh
                            cb += (itc[2] - cb) * tt * a_sh
                if bdc and border_w > 0:
                    bw = float(border_w)
                    a_in = min(1.0, max(0.0, _AA - _sdf_rr(
                        px, py, x1 + bw, y1 + bw, x2 - bw, y2 - bw, max(0.0, r - bw))))
                    a_bd = max(0.0, a_sh - a_in)
                    if a_bd > 0.002:
                        cr += (bdc[0] - cr) * a_bd
                        cg += (bdc[1] - cg) * a_bd
                        cb += (bdc[2] - cb) * a_bd
                row += bytes((_clamp8(cr), _clamp8(cg), _clamp8(cb)))
            rows.append(row)
        img = tk.PhotoImage(data=base64.b64encode(_png_bytes(w, h, rows)).decode("ascii"))
    except Exception:                                    # noqa: BLE001
        img = None
    _IMG_CACHE[key] = img
    return img


def _ease(t):
    """ease-out cubic：滑条/开关的缓动，比线性"贵"一点但更像网页。"""
    return 1 - (1 - t) ** 3


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


LEFT_MIN, LEFT_MAX = 210, 560          # 样本栏可拖范围


def load_left_width():
    """样本栏宽度（记忆到 layout.txt，与 theme.txt 同目录）。"""
    p = _theme_file()
    if p:
        lp = os.path.join(os.path.dirname(p), "layout.txt")
        try:
            if os.path.isfile(lp):
                return max(LEFT_MIN, min(LEFT_MAX, int(open(lp, encoding="utf-8").read().strip())))
        except (OSError, ValueError):
            pass
    return 300


def save_left_width(w):
    p = _theme_file()
    if not p:
        return
    try:
        open(os.path.join(os.path.dirname(p), "layout.txt"), "w",
             encoding="utf-8").write(str(int(w)))
    except OSError:
        pass


# ── 自绘控件 ────────────────────────────────────────────────────────────────
class _Slider:
    """给 Canvas 控件复用的「滑块平移动画」。要求宿主已有 self._anim / self._tx。"""

    def _animate_to(self, target, setter, animate=True, steps=9, ms=12):
        if self._anim:
            try:
                self.after_cancel(self._anim)
            except tk.TclError:
                pass
            self._anim = None
        if not animate:
            self._tx = target
            setter(target)
            return
        start = self._tx

        def step(i):
            if not self.winfo_exists():
                return
            self._tx = start + (target - start) * _ease(min(1.0, i / float(steps)))
            setter(self._tx)
            self._anim = self.after(ms, lambda: step(i + 1)) if i < steps else None

        self._anim = self.after(ms, lambda: step(1))


class Segmented(_Slider, tk.Canvas):
    """分段滑条（Web 风：一条轨道 + 会滑过去的指示块）。

    - 选项等宽（像 iOS 分段控件），指示块按选项语义着色（好=绿 / 中=黄 / 差=红）。
    - 悬停为文字高亮，点击滑动带缓动动画。
    """

    M = 6                      # 贴图给投影留的边距；必须 >= 阴影模糊半径
    SH_DY, SH_BLUR = 1.0, 5.0

    def __init__(self, master, options, value=None, on_change=None, theme=None,
                 font=None, padx=14, height=32, bg_key="surface", colors=None,
                 thumb_key="accent"):
        super().__init__(master, highlightthickness=0, bd=0, takefocus=0)
        self.options = list(options)
        self.value = value if value in self.options else (self.options[0] if self.options else None)
        self.on_change = on_change
        self.theme = theme
        self.font = font or F("body")
        self.padx, self.h, self.bg_key = padx, int(height), bg_key
        self.colors = list(colors or [])
        while len(self.colors) < len(self.options):
            self.colors.append(None)
        self.thumb_key = thumb_key
        self._hover, self._anim = None, None
        self._tx = None
        self._track_img = self._thumb_img = None
        self._thumb_i = None
        self._texts = []
        self.bind("<Button-1>", self._on_click)
        self.bind("<Motion>", self._on_motion)
        self.bind("<Leave>", self._on_leave)
        self._measure()
        self._build()

    # ---- 几何 -------------------------------------------------------------
    def _measure(self):
        w0 = max([self.font.measure(o) for o in self.options] or [10])
        self._ow = w0 + self.padx * 2                       # 每个选项等宽
        self._cw = self._ow * max(1, len(self.options))
        self._ch = self.h

    def _index(self):
        try:
            return self.options.index(self.value)
        except ValueError:
            return 0

    def _track_color(self):
        t = self.theme
        return _mix(t[self.bg_key], t["text"], 0.07)

    def _thumb_box(self):
        m = self.M
        return (m, m, self._ow - m, self._ch - m)

    def _target_x(self):
        return float(self._index() * self._ow)

    # ---- 绘制 -------------------------------------------------------------
    def _build(self):
        self.delete("all")
        self.configure(width=self._cw, height=self._ch)
        t = self.theme
        track = _rr_photo(self._cw, self._ch, self._ch / 2.0, fill=self._track_color(),
                          bg=t[self.bg_key], border=t["border"], border_w=1.0)
        if track is not None:
            self._track_img = track
            self.create_image(0, 0, anchor="nw", image=track)
        else:                                               # 贴图失败 → 退化直绘
            _rr(self, 0.5, 0.5, self._cw - 0.5, self._ch - 0.5, self._ch / 2.0,
                outline=t["border"], fill=self._track_color())
        self._thumb_img = self._thumb_photo()
        if self._tx is None:
            self._tx = self._target_x()
        x1, y1, x2, y2 = self._thumb_box()
        self._thumb_i = self.create_image(self._tx, 0, anchor="nw", image=self._thumb_img)
        self._texts = []
        for i, opt in enumerate(self.options):
            self._texts.append(self.create_text(i * self._ow + self._ow / 2.0,
                                                self._ch / 2.0, text=opt,
                                                font=self.font, anchor="center"))
        self._recolor()

    def _thumb_photo(self):
        t = self.theme
        x1, y1, x2, y2 = self._thumb_box()
        col = t.get(self.colors[self._index()] or self.thumb_key, t["accent"])
        return _rr_photo(self._ow, self._ch, (y2 - y1) / 2.0, fill=col,
                         bg=self._track_color(), shadow=_dim(col, 0.45),
                         shadow_dy=self.SH_DY, shadow_blur=self.SH_BLUR,
                         shadow_alpha=0.32, box=(x1, y1, x2, y2))

    def _recolor(self):
        t = self.theme
        i = self._index()
        for j, item in enumerate(self._texts):
            if j == i:
                self.itemconfig(item, fill=t["accent_text"])
            else:
                self.itemconfig(item, fill=t["accent"] if j == self._hover else t["muted"])

    def _refresh_thumb(self):
        self._thumb_img = self._thumb_photo()
        if self._thumb_i is not None and self._thumb_img is not None:
            self.itemconfig(self._thumb_i, image=self._thumb_img)

    # ---- 交互 -------------------------------------------------------------
    def _hit(self, x):
        n = len(self.options)
        if n == 0 or self._ow <= 0:
            return None
        i = int(x // self._ow)
        return i if 0 <= i < n else None

    def _on_motion(self, e):
        i = self._hit(e.x)
        self.configure(cursor="hand2" if i is not None else "")
        if i != self._hover:
            self._hover = i
            self._recolor()

    def _on_leave(self, _e):
        self._hover = None
        self._recolor()

    def _on_click(self, e):
        i = self._hit(e.x)
        if i is None:
            return
        v = self.options[i]
        if v == self.value:
            return
        self.value = v
        self._refresh_thumb()
        self._recolor()
        self._animate_to(self._target_x(), lambda x: self.coords(self._thumb_i, x, 0), True)
        if self.on_change:
            self.on_change(v)

    # ---- 取值 -------------------------------------------------------------
    def get(self):
        return self.value

    def set(self, v):
        if v in self.options and v != self.value:
            self.value = v
            self._refresh_thumb()
            self._recolor()
            if self._thumb_i is not None:
                self._animate_to(self._target_x(),
                                 lambda x: self.coords(self._thumb_i, x, 0), False)

    def apply_theme(self, theme):
        self.theme = theme
        self._build()


class Switch(_Slider, tk.Canvas):
    """开关（Web 风：iOS 式滑轨 + 圆形滑块 + 缓动动画）。

    对外仍用字符串取值（默认 off="无" / on="有"），上层表单代码不用改。
    """

    TR_M = 4                   # 滑块距轨道边的内缩

    def __init__(self, master, value=None, on_change=None, theme=None, font=None,
                 on_value="有", off_value="无", on_key="bad", bg_key="surface",
                 track_w=46, track_h=26, show_text=True):
        super().__init__(master, highlightthickness=0, bd=0, takefocus=0)
        self.theme = theme
        self.on_change = on_change
        self.font = font or F("small")
        self.on_value, self.off_value, self.on_key = on_value, off_value, on_key
        self.bg_key = bg_key
        self.tw, self.th, self.show_text = int(track_w), int(track_h), show_text
        self._on = value in (True, on_value)
        self._hover, self._anim = False, None
        self._tx = None
        lw = max(self.font.measure(on_value), self.font.measure(off_value)) + 2
        self._text_w = (lw + 10) if show_text else 0
        self._tr = self.th / 2.0
        self._thr = self._tr - self.TR_M
        self._track_img = self._thumb_img = None
        self._thumb_i = None
        self.configure(width=self.tw + self._text_w, height=self.th)
        self.bind("<Button-1>", self._click)
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self._build()

    # ---- 几何 -------------------------------------------------------------
    def _off_color(self):
        t = self.theme
        return _mix(t[self.bg_key], t["text"], 0.24)

    def _on_color(self):
        return self.theme[self.on_key]

    def _cur_color(self):
        return self._on_color() if self._on else self._off_color()

    def _target_x(self):
        """滑块贴图左上角 x（贴图边长 = th）。"""
        return float(self._tr if self._on else self.TR_M)

    def _thumb_box(self):
        r, tr = self._tr, self._thr
        return (r - tr, r - tr, r + tr, r + tr)

    # ---- 绘制 -------------------------------------------------------------
    def _build(self):
        self.delete("all")
        t = self.theme
        col = self._cur_color()
        img = _rr_photo(self.tw, self.th, self.th / 2.0, fill=col, bg=t[self.bg_key],
                        border=_mix(col, "#000000", 0.10), border_w=1.0)
        if img is not None:
            self._track_img = img
            self.create_image(0, 0, anchor="nw", image=img)
        else:
            _rr(self, 0.5, 0.5, self.tw - 0.5, self.th - 0.5, self.th / 2.0,
                outline="", fill=col)
        self._thumb_img = self._thumb_photo()
        if self._tx is None:
            self._tx = self._target_x()
        self._thumb_i = self.create_image(self._tx, 0, anchor="nw", image=self._thumb_img)
        if self.show_text:
            self._label_i = self.create_text(self.tw + 10, self.th / 2.0,
                                             text=self.get(), font=self.font, anchor="w")
        self._recolor()

    def _thumb_photo(self):
        r, tr = self._tr, self._thr
        knob = "#FFFFFF"
        return _rr_photo(int(2 * r), int(2 * r), tr, fill=knob, bg=self._cur_color(),
                         shadow=_dim(self._cur_color(), 0.5), shadow_dy=1.0,
                         shadow_blur=3.0, shadow_alpha=0.38, box=self._thumb_box())

    def _recolor(self):
        t = self.theme
        self.configure(bg=t[self.bg_key])
        if self.show_text and getattr(self, "_label_i", None) is not None:
            self.itemconfig(self._label_i,
                            fill=t[self.on_key] if self._on else t["muted"])
        if getattr(self, "_track_img", None) is not None and self._hover:
            pass                      # 悬停不改轨道色，保持克制（Web 里的开关都很安静）

    # ---- 交互 -------------------------------------------------------------
    def _set_hover(self, on):
        self._hover = on
        self.configure(cursor="hand2" if on else "")

    def _click(self, _e):
        self._on = not self._on
        self._build()                 # 只重画颜色，位置沿用 self._tx（动画起点）
        self._animate_to(self._target_x(),
                         lambda x: self.coords(self._thumb_i, x, 0), True)
        if self.on_change:
            self.on_change(self.get())

    def get(self):
        return self.on_value if self._on else self.off_value

    def set(self, v):
        on = v in (True, self.on_value)
        if on != self._on:
            self._on = on
            self._build()
            self._animate_to(self._target_x(),
                             lambda x: self.coords(self._thumb_i, x, 0), False)

    def apply_theme(self, theme):
        self.theme = theme
        self._build()


class Stars(tk.Canvas):
    """五星评分（点击 / 悬停预览；扁平配色）。"""

    def __init__(self, master, value=4, on_change=None, theme=None, gap=5, bg_key="surface"):
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
        self._items = []
        w = self.font.measure("★") + self.gap
        h = self.font.metrics("linespace") + 4
        for i in range(5):
            self._items.append(self.create_text(w * i + w / 2.0, h / 2.0, text="★",
                                                font=self.font, anchor="center"))
        self.configure(width=w * 5, height=h)
        self._recolor()

    def _recolor(self):
        t = self.theme
        self.configure(bg=t[self.bg_key])
        shown = self._hover if self._hover else self.value
        for i in range(5):
            self.itemconfig(self._items[i],
                            fill=t["accent"] if i < shown else t["star_empty"])

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
        self._build()


class Pill(tk.Canvas):
    """扁平按钮（Web 风：纯色 / 描边 + 柔和投影；悬停变色、按下轻微下沉）。

    kind: primary / ghost / danger。depth 参数保留兼容（越大投影越厚）。
    """

    def __init__(self, master, text, command=None, theme=None, font=None,
                 kind="primary", padx=20, pady=10, radius=10, bg_key="bg",
                 shadow=6, depth=None):
        super().__init__(master, highlightthickness=0, bd=0, takefocus=0)
        self.text, self.command, self.theme = text, command, theme
        self.font = font or F("body")
        self.kind, self.padx, self.pady, self.radius, self.bg_key = (
            kind, padx, pady, radius, bg_key)
        self.shadow = max(3, int(depth) + 2) if depth else int(shadow)
        self._hover = self._press = False
        self._img = None
        self.bind("<Enter>", lambda e: self._set(hover=True))
        self.bind("<Leave>", lambda e: self._set(hover=False))
        self.bind("<Button-1>", lambda e: self._set(press=True))
        self.bind("<ButtonRelease-1>", self._release)
        self._build()

    # ---- 几何（注意：不能用 self._w / self._h，那是 tkinter 内部属性）----
    def _build(self):
        self._bw = self.font.measure(self.text) + self.padx * 2
        self._bh = self.font.metrics("linespace") + self.pady * 2
        self._cw = self._bw
        self._ch = self._bh + (self.shadow + 2 if self.shadow else 0)
        self.configure(width=self._cw, height=self._ch)
        self.delete("all")
        self._img_i = self.create_image(0, 0, anchor="nw")
        self._txt_i = self.create_text(self._bw / 2.0, self._bh / 2.0, text=self.text,
                                       font=self.font, anchor="center")
        self._draw()

    def _colors(self):
        t = self.theme
        if self.kind == "primary":
            base, fg = t["accent"], t["accent_text"]
        elif self.kind == "danger":
            base, fg = t["bad"], t["accent_text"]
        else:
            base, fg = t["surface"], t["text"]
        if self._press:
            fill = _dim(base, 0.10)
        elif self._hover:
            fill = _mix(base, t["text"], 0.06) if self.kind == "ghost" else _lit(base, 0.10)
        else:
            fill = base
        border = t["border"] if self.kind == "ghost" else None
        shadow = t["text"] if self.kind == "ghost" else _dim(base, 0.55)
        alpha = 0.16 if self.kind == "ghost" else 0.30
        return fill, fg, border, shadow, alpha

    def _draw(self):
        t = self.theme
        bg = t[self.bg_key]
        fill, fg, border, sh, alpha = self._colors()
        img = _rr_photo(self._cw, self._ch, self.radius, fill=fill, bg=bg,
                        border=border, border_w=1.0,
                        shadow=sh if self.shadow else None,
                        shadow_dy=1.0 if self._press else 2.0,
                        shadow_blur=float(self.shadow), shadow_alpha=alpha,
                        box=(0, 0, self._cw, self._bh))
        self.configure(bg=bg)
        if img is not None:
            self._img = img
            self.itemconfig(self._img_i, image=img)
        else:                                    # 贴图失败 → 退化直绘
            _rr(self, 0.5, 0.5, self._cw - 0.5, self._bh - 0.5, self.radius,
                outline=border or "", fill=fill)
        self.itemconfig(self._txt_i, fill=fg)

    def _set(self, hover=None, press=None):
        if hover is not None:
            self._hover = hover
            self.configure(cursor="hand2" if hover else "")
        if press is not None:
            self._press = press
        self._draw()

    def _release(self, e):
        was = self._press
        self._press = False
        self._draw()
        if was and 0 <= e.x <= self._cw and 0 <= e.y <= self._ch:
            if self.command:
                self.command()

    def apply_theme(self, theme):
        self.theme = theme
        self._build()


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
                                   padx=12, height=26, bg_key="header")
        self.theme_seg.pack(side="left")
        self._dyn.append(self.theme_seg)

        self.root_path_lab = self._lab(self.root, "", "small", "muted")
        self.root_path_lab.pack(anchor="w", padx=22, pady=(10, 0))

        body = tk.Frame(self.root, bg=t["bg"])
        body.pack(fill="both", expand=True, padx=20, pady=12)
        self._frames.append((body, "bg", "bg"))

        # 左：样本列表（宽度可拖；宽度记忆到 layout.txt）
        self._left_w = load_left_width()
        leftcard = tk.Frame(body, bg=t["border"], width=self._left_w)
        leftcard.pack(side="left", fill="y")
        leftcard.pack_propagate(False)
        self._frames.append((leftcard, "border", "bg"))
        self._leftcard = leftcard
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
                                 kind="ghost", padx=14, pady=8, radius=10,
                                 bg_key="surface", depth=3)
        self.refresh_pill.pack(side="left")
        self._dyn.append(self.refresh_pill)

        # 隐形可拖分隔条（视觉上只是一枚小药丸，命中区 8px）
        self._split = tk.Frame(body, bg=t["bg"], width=8, cursor="sb_h_double_arrow")
        self._split.pack(side="left", fill="y")
        self._split.pack_propagate(False)
        self._frames.append((self._split, "bg", "bg"))
        self._grip = tk.Canvas(self._split, width=8, bg=t["bg"], highlightthickness=0, bd=0)
        self._grip.pack(fill="y", expand=True)
        self._frames.append((self._grip, "bg", "bg"))
        self._grip_pill = self._grip.create_rectangle(3, 0, 5, 0, fill=t["border"], outline="")
        self._grip.bind("<Button-1>", self._split_press)
        self._grip.bind("<B1-Motion>", self._split_drag)
        self._grip.bind("<ButtonRelease-1>", self._split_release)
        self._grip.bind("<Enter>", lambda e: self._split_hover(True))
        self._grip.bind("<Leave>", lambda e: self._split_hover(False))
        self._grip.bind("<Configure>", self._split_configure)

        # 右：评分表单
        rightcard = tk.Frame(body, bg=t["border"])
        rightcard.pack(side="left", fill="both", expand=True)
        self._frames.append((rightcard, "border", "bg"))
        self._body = body
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
        self.video_menu.grid(row=self._row, column=1, sticky="w", pady=(0, 12), ipady=4)
        self.video_menu["menu"].config(bd=0, activeborderwidth=0, font=F("body"))
        self._row += 1

        self.vars = {}
        for key, opts in DIMS:
            self._lab(form, key, "body").grid(row=self._row, column=0, sticky="w", pady=5)
            seg = Segmented(form, opts, value=opts[0], on_change=lambda v: self._touch(),
                            theme=t, font=F("body"), height=32,
                            colors=["ok", "warn", "bad"])
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
            sw = Switch(cell, value="无", on_change=lambda v: self._touch(),
                        theme=t, font=F("small"), bg_key="surface")
            sw.pack(anchor="w", pady=(3, 0))
            self.vars[key] = sw
            self._dyn.append(sw)
        self._row += 1

        self._lab(form, "整体评分", "body").grid(row=self._row, column=0, sticky="w", pady=8)
        self.stars = Stars(form, value=4, on_change=lambda v: self._touch(), theme=t)
        self.stars.grid(row=self._row, column=1, sticky="w", pady=8)
        self._dyn.append(self.stars)
        self._row += 1

        self._lab(form, "结论", "body").grid(row=self._row, column=0, sticky="w", pady=6)
        self.concl = Segmented(form, CONCL, value="可改", on_change=lambda v: self._touch(),
                               theme=t, font=F("body"), height=32,
                               colors=["ok", "warn", "bad"])
        self.concl.grid(row=self._row, column=1, sticky="w", pady=6)
        self.vars["结论"] = self.concl
        self._dyn.append(self.concl)
        self._row += 1

        self._lab(form, "备注", "body").grid(row=self._row, column=0, sticky="w", pady=(8, 0))
        self.note = tk.Entry(form, relief="flat", bd=0, highlightthickness=1, font=F("body"),
                             insertwidth=2)
        self.note.grid(row=self._row, column=1, sticky="ew", ipady=8, pady=(8, 0))
        self.note.bind("<KeyRelease>", lambda e: self._touch())
        self._row += 1

        # 底栏
        footer = tk.Frame(self.root, bg=t["bg"])
        footer.pack(fill="x", padx=20, pady=(0, 16))
        self._frames.append((footer, "bg", "bg"))
        self.status = self._lab(footer, "选一个样本 → 逐项点分 → 保存", "small", "muted")
        self.status.pack(side="left")
        self.save_pill = Pill(footer, "保存评分", self.save, theme=t, font=F("h2"),
                              kind="primary", padx=26, pady=11, radius=11,
                              bg_key="bg", depth=4)
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
        self._grip.configure(bg=t["bg"])
        self._grip.itemconfig(self._grip_pill, fill=t["border"])
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

    # ---- 分隔条（拖拽调整样本栏宽度）-------------------------------------
    def _split_hover(self, on):
        t = self.theme
        self._grip.configure(bg=t["bg"], cursor="sb_h_double_arrow" if on else "")
        self._grip.itemconfig(self._grip_pill,
                              fill=t["accent"] if on else t["border"])

    def _split_configure(self, _e=None):
        """把中央药丸画成竖直短条（高度自适应，居中）。"""
        h = self._grip.winfo_height()
        y1 = max(0, h // 2 - 26)
        y2 = min(h, h // 2 + 26)
        self._grip.coords(self._grip_pill, 3, y1, 5, y2)

    def _split_press(self, e):
        self._split_drag(e)

    def _split_drag(self, e):
        try:
            x0 = self._body.winfo_rootx()
        except tk.TclError:
            return
        w = max(LEFT_MIN, min(LEFT_MAX, int(e.x_root - x0)))
        if w != self._left_w:
            self._left_w = w
            self._leftcard.configure(width=w)

    def _split_release(self, _e=None):
        save_left_width(self._left_w)
        self.status.config(text="样本栏宽度已保存（%d px）" % self._left_w)

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
