# -*- coding: utf-8 -*-
"""成片六维评分 · 图形界面版（tkinter，可换主题 / 未保存离开会拦一下）

与 `score_core.py`（命令行版）、旧网页版 `评价工具.html` **同一套口径、同一份 json 结构**，
所以 `tools\\评价回收.py` 照常回收。

用法：
    双击 tools\\score_gui.cmd                 # 优先起 dist\\score-tool.exe，没有则挑带 tkinter 的 Python
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
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))


def _find_tk_python():
    """找一个「带 tkinter」的解释器；找不到返回 None。

    本机 PATH 上的 python/pythonw 可能是没带 tcl/tk 的绿色版，直接 import 会崩，
    所以这里主动探测：先问 py 启动器，再扫常见安装目录。
    """
    import glob
    import subprocess
    cands = []
    launcher = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                            "Programs", "Python", "Launcher", "py.exe")
    if os.path.isfile(launcher):
        for ver in ("-3.12", "-3.13", "-3.11", "-3.10", "-3"):
            try:
                r = subprocess.run([launcher, ver, "-c",
                                    "import sys,tkinter;print(sys.executable)"],
                                   capture_output=True, text=True, timeout=20)
                if r.returncode == 0 and r.stdout.strip():
                    cands.append(r.stdout.strip())
            except Exception:                              # noqa: BLE001
                pass
    for root in (os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Python"),
                 os.environ.get("ProgramFiles", r"C:\Program Files"), "C:\\"):
        if root and os.path.isdir(root):
            cands += sorted(glob.glob(os.path.join(root, "Python3*", "python.exe")))
    seen = set()
    for p in cands:
        try:
            rp = os.path.realpath(p)
        except OSError:
            continue
        if rp in seen or not os.path.isfile(p):
            continue
        seen.add(rp)
        try:
            r = subprocess.run([p, "-c", "import tkinter"], capture_output=True, timeout=20)
            if r.returncode == 0:
                return p
        except Exception:                                  # noqa: BLE001
            pass
    return None


try:
    import tkinter as tk
except ModuleNotFoundError:                # 当前解释器没带 tkinter → 换一个再起自己
    import subprocess
    if os.environ.get("HERONBO_TK_RELAUNCH") != "1":
        _py = _find_tk_python()
        if _py:
            _exe = _py
            _w = os.path.join(os.path.dirname(_py), "pythonw.exe")
            if os.name == "nt" and os.path.isfile(_w):
                _exe = _w
            _env = dict(os.environ, HERONBO_TK_RELAUNCH="1")
            subprocess.Popen([_exe, os.path.abspath(__file__)] + sys.argv[1:],
                             cwd=os.path.dirname(os.path.abspath(__file__)), env=_env)
            sys.exit(0)
    try:                                                   # 真找不到 → 弹窗说清楚，别静默消失
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None,
            "当前 Python 没有 tkinter，本机也没找到带 tkinter 的 Python。\n\n"
            "两种情况都能解决：\n"
            "1) 直接运行 tools\\dist\\score-tool.exe（已打包好，不依赖本机 Python）；\n"
            "2) 安装 Python 3 时勾上 “tcl/tk and IDLE”。",
            "评分工具 · 环境不完整", 0x10)
    except Exception:                                      # noqa: BLE001
        pass
    sys.exit(1)

import tkinter.font as tkfont
from tkinter import filedialog

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

    # ── 游戏皮肤：game=True 会额外启用「渐变 + 外发光 + 顶部高光」──
    "霓虹": dict(bg="#080C16", surface="#131C2E", surface_hover="#1B2740", border="#28354F",
                 text="#E8EFFC", muted="#8A9CBC", accent="#38BDF8", accent_text="#04202F",
                 accent_soft="#12314A", header="#0C1424", star_empty="#33405C",
                 ok="#34D399", warn="#FBBF24", bad="#F87171", game=True),
    "熔金": dict(bg="#17100A", surface="#2A1D11", surface_hover="#3A2816", border="#4A351C",
                 text="#F8F0DE", muted="#BC9E73", accent="#F0A22E", accent_text="#2A1A02",
                 accent_soft="#3E2C12", header="#1F150C", star_empty="#4E3A21",
                 ok="#4ADE80", warn="#FBBF24", bad="#F87171", game=True),
    "极光": dict(bg="#EEF3FF", surface="#FFFFFF", surface_hover="#F4F6FF", border="#D3DEF6",
                 text="#1B2438", muted="#6B7A99", accent="#6366F1", accent_text="#FFFFFF",
                 accent_soft="#E6E8FF", header="#E4EBFF", star_empty="#D5DDF0",
                 ok="#10B981", warn="#F59E0B", bad="#EF4444", game=True),
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


def _load_core(fname, modname):
    """按名字加载同目录（或 PyInstaller 解包目录）里的模块。"""
    for d in (BASE, HERE):
        p = os.path.join(d, fname)
        if os.path.isfile(p):
            spec = importlib.util.spec_from_file_location(modname, p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    return None


core = _load_core("score_core.py", "score_core")
if core is None:
    raise RuntimeError("找不到 score_core.py（应与本脚本同在 tools\\ 目录）")

# 项目骨架 / 素材投放核心（缺了不影响评分功能，只是「素材/骨架」不可用）
pcore = _load_core("project_core.py", "project_core")
# 程序 ↔ agent 通道（缺了就只能"只记待办"，不能一键叫 agent）
abridge = _load_core("agent_bridge.py", "agent_bridge")

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


COL_DEFAULTS = (190, 260, 440, 570)          # ①项目 ②素材 ③提示词 ④评分
COL_LIMITS = ((150, 400), (220, 520), (300, 900), (430, 780))


def load_col_widths():
    """四栏宽度（记忆到 layout.txt）。兼容旧版只存一个数字的样本栏宽度。"""
    p = _theme_file()
    if p:
        lp = os.path.join(os.path.dirname(p), "layout.txt")
        try:
            if os.path.isfile(lp):
                raw = open(lp, encoding="utf-8").read().strip()
                parts = [int(x) for x in raw.replace(" ", "").split(",") if x]
                if len(parts) == 1:                      # 旧格式：就是样本栏宽度
                    return (max(160, min(420, parts[0])),) + COL_DEFAULTS[1:]
                if len(parts) >= 4:
                    return tuple(max(COL_LIMITS[i][0], min(COL_LIMITS[i][1], parts[i]))
                                 for i in range(4))
        except (OSError, ValueError):
            pass
    return COL_DEFAULTS


def save_col_widths(widths):
    p = _theme_file()
    if not p or not widths:
        return
    try:
        open(os.path.join(os.path.dirname(p), "layout.txt"), "w",
             encoding="utf-8").write(",".join(str(int(w)) for w in widths))
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
        game = bool(t.get("game"))
        tc = self._track_color()
        track = _rr_photo(self._cw, self._ch, self._ch / 2.0, fill=tc,
                          bg=t[self.bg_key], border=t["border"], border_w=1.0,
                          fill2=_mix(tc, t["text"], 0.06) if game else None)
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
        game = bool(t.get("game"))
        return _rr_photo(self._ow, self._ch, (y2 - y1) / 2.0, fill=col,
                         bg=self._track_color(),
                         fill2=_dim(col, 0.18) if game else None,
                         glow=col if game else None, glow_blur=6.0, glow_alpha=0.45,
                         shadow=_dim(col, 0.45),
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
        game = bool(t.get("game"))
        img = _rr_photo(self.tw, self.th, self.th / 2.0, fill=_lit(col, 0.08) if game else col,
                        bg=t[self.bg_key], fill2=_dim(col, 0.16) if game else None,
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
        self._glow_items = []
        w = self.font.measure("★") + self.gap
        h = self.font.metrics("linespace") + 4
        t = self.theme
        if t.get("game"):                       # 游戏皮肤：星标下垫一层柔光
            gs = int(h * 2.3)
            c0 = gs * 0.48                      # 核极小；填充用底色，只留光晕
            blob = _rr_photo(gs, gs, gs * 0.04, fill=t[self.bg_key], bg=t[self.bg_key],
                             glow=t["accent"], glow_blur=gs * 0.42, glow_alpha=0.62,
                             box=(c0, c0, gs - c0, gs - c0))
            self._glow_img = blob
            for i in range(5):
                if blob is not None:
                    self._glow_items.append(self.create_image(w * i + w / 2.0, h / 2.0,
                                                              image=blob, state="hidden"))
                else:
                    self._glow_items.append(None)
        else:
            self._glow_items = [None] * 5
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
            g = self._glow_items[i] if getattr(self, "_glow_items", None) else None
            if g is not None:
                self.itemconfig(g, state="normal" if i < shown else "hidden")

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
        game = bool(self.theme.get("game"))
        self._M = 14 if game else 0            # 游戏皮肤需要留白容纳外发光
        self._bw = self.font.measure(self.text) + self.padx * 2
        self._bh = self.font.metrics("linespace") + self.pady * 2
        self._cw = self._bw + self._M * 2
        self._ch = self._bh + self._M * 2 + (self.shadow + 2 if self.shadow else 0)
        self.configure(width=self._cw, height=self._ch)
        self.delete("all")
        self._img_i = self.create_image(0, 0, anchor="nw")
        self._txt_i = self.create_text(self._bw / 2.0 + self._M, self._bh / 2.0 + self._M,
                                       text=self.text, font=self.font, anchor="center")
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
        game = bool(t.get("game"))
        M = getattr(self, "_M", 0)
        glow = None
        if game:
            glow = t["accent"] if self.kind == "primary" else (
                t["bad"] if self.kind == "danger" else None)
        img = _rr_photo(self._cw, self._ch, self.radius, fill=fill, bg=bg,
                        border=border, border_w=1.0,
                        fill2=_dim(fill, 0.22) if game else None,
                        glow=glow, glow_blur=11.0,
                        glow_alpha=(0.20 if self._press else 0.40) if game else 0.0,
                        inner_top=_lit(fill, 0.40) if game else None,
                        inner_alpha=0.55,
                        shadow=sh if self.shadow else None,
                        shadow_dy=1.0 if self._press else 2.0,
                        shadow_blur=float(self.shadow), shadow_alpha=alpha,
                        box=(M, M, M + self._bw, M + self._bh))
        self.configure(bg=bg, height=self._ch)
        if img is not None:
            self._img = img
            self.itemconfig(self._img_i, image=img)
        else:                                    # 贴图失败 → 退化直绘
            _rr(self, M + 0.5, M + 0.5, M + self._bw - 0.5, M + self._bh - 0.5,
                self.radius, outline=border or "", fill=fill)
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


# ── 素材投放 / 建骨架 ───────────────────────────────────────────────────────
def app_root_hint():
    """样本库根的默认值：先问 project_core，再退回评分工具自己的探测结果。"""
    if pcore is not None:
        try:
            r = pcore.detect_root()
            if r:
                return r
        except Exception:                                     # noqa: BLE001
            pass
    return ""


class FeedbackWin(tk.Toplevel):
    """接收完成片/废片后弹的本轮反馈窗口。

    右上角有系统标题栏的 ×，窗口内也放一个「✕ 关闭」——用户随时能关。
    """

    def __init__(self, master, panel, project, placed, good):
        super().__init__(master)
        self.panel, self.project, self.placed = panel, project, list(placed)
        t = panel.theme
        self.title("本轮反馈 · %s" % ("成片已接收" if good else "废片已接收"))
        self.configure(bg=t["surface"])
        self.geometry("560x420+220+160")
        self.transient(master)
        head = tk.Frame(self, bg=t["header"])
        head.pack(fill="x")
        tk.Label(head, text="本轮情况怎么样？", font=F("title"), bg=t["header"],
                 fg=t["text"]).pack(side="left", padx=16, pady=(12, 0))
        tk.Label(head, text="（想跳过就点右上角 × 或下面的「✕ 关闭」）",
                 font=F("small"), bg=t["header"], fg=t["muted"]).pack(side="left",
                                                                     padx=8, pady=(14, 0))
        body = tk.Frame(self, bg=t["surface"])
        body.pack(fill="both", expand=True, padx=14, pady=12)
        tk.Label(body, text="已接收：%s" % "、".join(os.path.basename(p) for p in self.placed),
                 font=F("small"), bg=t["surface"], fg=t["ok"], anchor="w",
                 justify="left", wraplength=500).pack(anchor="w")
        tk.Label(body, text="这轮哪里好、哪里不对？（写清现象，agent 好照着改）",
                 font=F("small"), bg=t["surface"], fg=t["muted"], anchor="w").pack(
                     anchor="w", pady=(8, 4))
        self.box = tk.Text(body, height=9, font=F("small"), bd=0, relief="flat",
                           highlightthickness=1, wrap="word")
        self.box.pack(fill="both", expand=True)
        self.box.configure(bg=t["surface"], fg=t["text"], insertbackground=t["accent"],
                           highlightbackground=t["border"], highlightcolor=t["accent"])
        row = tk.Frame(self, bg=t["surface"])
        row.pack(fill="x", padx=14, pady=(0, 12))
        Pill(row, "提交并让 agent 再出一版", self._send_and_close, theme=t, font=F("small"),
             kind="primary", padx=16, pady=8, radius=9, bg_key="surface", depth=4).pack(
                 side="left")
        Pill(row, "只记待办", self._save_only, theme=t, font=F("small"), kind="ghost",
             padx=12, pady=8, radius=9, bg_key="surface", depth=2).pack(side="left", padx=6)
        Pill(row, "✕ 关闭", self.destroy, theme=t, font=F("small"), kind="ghost",
             padx=14, pady=8, radius=9, bg_key="surface", depth=2).pack(side="right")
        self.box.focus_force()

    def _take(self):
        text = self.box.get("1.0", "end").strip()
        if not text:
            return ""
        try:
            self.clipboard_clear()                    # 顺手留一份在剪贴板，万一界面关了也不丢
            self.clipboard_append(text)
        except tk.TclError:
            pass
        return text

    def _save_only(self):
        text = self._take()
        if text:
            self.panel.fb.insert("end", text)
            self.panel.wb_submit(False)
        self.destroy()

    def _send_and_close(self):
        text = self._take()
        if text:
            self.panel.fb.insert("end", text)
            self.panel.wb_submit(True)
        else:
            self.panel._log("反馈为空，没有提交")
        self.destroy()


# ── 主界面 ──────────────────────────────────────────────────────────────────
class App:
    def __init__(self, root, preselect=None):
        self.root = root
        self.theme = THEMES[load_theme_name()]
        self._loading = False
        self.dirty = False
        self.drafts = {}
        self._pending = []          # ②栏待投放的素材路径
        self._plan = None           # 软件规划出来的结果（角色 / 项目名）
        self.proj_dir = ""          # ③栏当前项目目录

        root.title("HeronBo · AI 视频工作台")
        root.geometry("1500x900")   # 四栏工作台，照 StoryVia 的 1400×900 量级
        root.minsize(1180, 700)
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
                    # 注意：不能用字符串当替换串 —— Windows 路径里的 \A \1 \g 会被当成
                    # 正则转义，直接抛 "bad escape \A"（本机样本库路径里带 \A 就会炸）。用函数替换。
                    txt = re.sub(r"(?m)^\s*SAMPLES_ROOT\s*=.*$",
                                 lambda _m: "SAMPLES_ROOT=" + root_dir, txt)
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

        # ── 四栏工作台（照 StoryVia 形制：四栏 + 4px 隐形拖缝 + 全高）──────
        # ① 项目 ② 素材 ③ 分镜/提示词 ④ 评分；每条拖缝改它左边那一栏的宽度
        self._cols = []
        self._body = body
        c1w, c2w, c3w, c4w = load_col_widths()

        # ① 项目栏（含样本列表）
        self._leftcard = self._col(body, t, c1w)
        self._left_w = c1w
        left = self._inner(self._leftcard, t)
        self._lab(left, "项目", "h2").pack(anchor="w", padx=12, pady=(12, 4))
        self.proj_lab = self._lab(left, "未选项目 · 骨架未建", "small", "muted")
        self.proj_lab.pack(anchor="w", padx=12, pady=(0, 8))
        lrow = tk.Frame(left, bg=t["surface"])
        lrow.pack(fill="both", expand=True, padx=(12, 6), pady=(0, 12))
        self._frames.append((lrow, "surface", "bg"))
        self.lb = tk.Listbox(lrow, width=22, activestyle="none", bd=0, highlightthickness=0,
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
        self.refresh_pill = Pill(rrow, "刷新", self.reload, theme=t, font=F("small"),
                                 kind="ghost", padx=12, pady=8, radius=10,
                                 bg_key="surface", depth=3)
        self.refresh_pill.pack(side="left")
        self._dyn.append(self.refresh_pill)
        self.detail_pill = Pill(rrow, "详细…", lambda: self.open_material(self.proj_dir),
                                theme=t, font=F("small"), kind="ghost", padx=12, pady=8,
                                radius=10, bg_key="surface", depth=3)
        self.detail_pill.pack(side="right")
        self._dyn.append(self.detail_pill)

        self._splitter(body, t, 0)

        # ② 素材栏：投放 + 软件自动判角色 + 一键归类
        self._col(body, t, c2w)
        mid = self._inner(self._cols[1][0], t)
        self._build_intake(mid, t)

        self._splitter(body, t, 1)

        # ③ 分镜 / 提示词栏
        self._col(body, t, c3w)
        pcol = self._inner(self._cols[2][0], t)
        self._build_prompt_col(pcol, t)

        self._splitter(body, t, 2)

        # ④ 评分栏（原有表单原样搬进来，不改逻辑）
        rightcard = self._col(body, t, c4w)
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
        # 四栏内联后控件变多：逐个护住，任何一个没建好都不该拖垮换肤
        for w, kw in (
            (getattr(self, "intake_lb", None), dict(bg=t["surface"], fg=t["text"],
                                                    selectbackground=t["accent_soft"],
                                                    selectforeground=t["text"],
                                                    highlightbackground=t["border"])),
            (getattr(self, "_isb", None), dict(bg=t["surface"], troughcolor=t["bg"],
                                               activebackground=t["muted"],
                                               highlightbackground=t["surface"])),
            (getattr(self, "prompt", None), dict(bg=t["surface"], fg=t["text"],
                                                 insertbackground=t["accent"],
                                                 highlightbackground=t["border"])),
            (getattr(self, "_psb", None), dict(bg=t["surface"], troughcolor=t["bg"],
                                               activebackground=t["muted"],
                                               highlightbackground=t["surface"])),
            (getattr(self, "fb", None), dict(bg=t["surface"], fg=t["text"],
                                             insertbackground=t["accent"],
                                             highlightbackground=t["border"],
                                             highlightcolor=t["accent"])),
            (getattr(self, "log", None), dict(bg=t["surface"], fg=t["text"],
                                              insertbackground=t["accent"],
                                              highlightbackground=t["border"])),
            (getattr(self, "_lsb", None), dict(bg=t["surface"], troughcolor=t["bg"],
                                               activebackground=t["muted"],
                                               highlightbackground=t["surface"])),
        ):
            if w is None:
                continue
            try:
                w.config(**kw)
            except tk.TclError:
                pass
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
        wp = getattr(self, "_mat_win", None)                # 素材窗口跟着换肤
        if wp is not None:
            try:
                if wp.winfo_exists():
                    wp.apply_theme(t)
            except tk.TclError:
                pass
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

    # ---- 四栏骨架 ---------------------------------------------------------
    def _col(self, body, t, w):
        """一栏：外层描边 + 固定宽度 + 登记进 _cols（拖缝就是改这里存的宽度）。"""
        card = tk.Frame(body, bg=t["border"], width=w)
        card.pack(side="left", fill="y")
        card.pack_propagate(False)
        self._frames.append((card, "border", "bg"))
        self._cols.append([card, w])
        return card

    def _inner(self, card, t):
        f = tk.Frame(card, bg=t["surface"])
        f.pack(fill="both", expand=True, padx=1, pady=1)
        self._frames.append((f, "surface", "bg"))
        return f

    def _splitter(self, body, t, i):
        """8px 命中区 / 4px 视觉 的隐形拖缝；拖它改「左边那一栏」的宽度。"""
        sp = tk.Frame(body, bg=t["bg"], width=8, cursor="sb_h_double_arrow")
        sp.pack(side="left", fill="y")
        sp.pack_propagate(False)
        self._frames.append((sp, "bg", "bg"))
        grip = tk.Canvas(sp, width=8, bg=t["bg"], highlightthickness=0, bd=0)
        grip.pack(fill="y", expand=True)
        self._frames.append((grip, "bg", "bg"))
        pill = grip.create_rectangle(3, 0, 5, 0, fill=t["border"], outline="")
        grip.bind("<Configure>", lambda e, g=grip, p=pill: self._split_configure(g, p))
        grip.bind("<Button-1>", lambda e, k=i: self._split_press(e, k))
        grip.bind("<B1-Motion>", lambda e, k=i: self._split_drag(e, k))
        grip.bind("<ButtonRelease-1>", lambda e, k=i: self._split_release(e, k))
        grip.bind("<Enter>", lambda e, g=grip, p=pill: self._split_hover(g, p, True))
        grip.bind("<Leave>", lambda e, g=grip, p=pill: self._split_hover(g, p, False))
        return sp

    # ---- ② 素材栏 ---------------------------------------------------------
    def _build_intake(self, parent, t):
        self._lab(parent, "素材", "h2").pack(anchor="w", padx=12, pady=(12, 4))
        self.intake_root_lab = self._lab(parent, "样本库：—", "small", "muted")
        self.intake_root_lab.pack(anchor="w", padx=12)

        drop = tk.Frame(parent, bg=t["surface"], highlightthickness=2,
                        highlightbackground=t["border"], highlightcolor=t["border"])
        drop.pack(fill="x", padx=12, pady=(8, 6))
        self._lab(drop, "把素材（文件或整个文件夹）丢进来", "small").pack(pady=(12, 2))
        self._lab(drop, "软件自己判角色、起项目名、建骨架", "small", "muted").pack(pady=(0, 12))

        row = tk.Frame(parent, bg=t["surface"])
        row.pack(fill="x", padx=12)
        self._frames.append((row, "surface", "bg"))
        Pill(row, "选文件", lambda: self.intake_pick(False), theme=t, font=F("small"),
             kind="ghost", padx=11, pady=7, radius=8, bg_key="surface", depth=2).pack(side="left")
        Pill(row, "选文件夹", lambda: self.intake_pick(True), theme=t, font=F("small"),
             kind="ghost", padx=11, pady=7, radius=8, bg_key="surface", depth=2).pack(
                 side="left", padx=5)
        Pill(row, "清空", self.intake_clear, theme=t, font=F("small"), kind="ghost",
             padx=11, pady=7, radius=8, bg_key="surface", depth=2).pack(side="left")

        self._lab(parent, "待投放 · 角色识别", "h2").pack(anchor="w", padx=12, pady=(12, 4))
        box = tk.Frame(parent, bg=t["surface"])
        box.pack(fill="both", expand=True, padx=12, pady=(0, 6))
        self._frames.append((box, "surface", "bg"))
        self.intake_lb = tk.Listbox(box, height=9, activestyle="none", bd=0,
                                    highlightthickness=1, font=F("small"),
                                    exportselection=False)
        self.intake_lb.pack(side="left", fill="both", expand=True)
        isb = tk.Scrollbar(box, orient="vertical", command=self.intake_lb.yview, width=9,
                           bd=0, relief="flat", elementborderwidth=0)
        isb.pack(side="right", fill="y", padx=(2, 0))
        self.intake_lb.config(yscrollcommand=isb.set)
        self._isb = isb

        self.go_pill = Pill(parent, "自动建骨架并归类", self.intake_go, theme=t,
                            font=F("small"), kind="primary", padx=14, pady=9, radius=10,
                            bg_key="surface", depth=4)
        self.go_pill.pack(fill="x", padx=12, pady=(0, 8))
        self._dyn.append(self.go_pill)
        self.intake_hint = self._lab(parent, "选定素材后点上面这颗按钮", "small", "muted")
        self.intake_hint.pack(anchor="w", padx=12, pady=(0, 8))

    # ---- ③ 分镜 / 提示词栏 ------------------------------------------------
    def _build_prompt_col(self, parent, t):
        head = tk.Frame(parent, bg=t["surface"])
        head.pack(fill="x", padx=12, pady=(12, 2))
        self._frames.append((head, "surface", "bg"))
        self._lab(head, "分镜 / 提示词", "h2").pack(side="left")
        self.wb_state_lab = self._lab(head, "—", "small", "muted")
        self.wb_state_lab.pack(side="right")

        prow = tk.Frame(parent, bg=t["surface"])
        prow.pack(fill="both", expand=True, padx=12, pady=(4, 6))
        self._frames.append((prow, "surface", "bg"))
        self.prompt = tk.Text(prow, height=9, font=F("small"), bd=0, relief="flat",
                              highlightthickness=1, wrap="word")
        self.prompt.pack(side="left", fill="both", expand=True)
        psb = tk.Scrollbar(prow, orient="vertical", command=self.prompt.yview, width=9,
                           bd=0, relief="flat", elementborderwidth=0)
        psb.pack(side="right", fill="y", padx=(2, 0))
        self.prompt.config(yscrollcommand=psb.set)
        self._psb = psb
        self.prompt.configure(state="disabled")

        pr = tk.Frame(parent, bg=t["surface"])
        pr.pack(fill="x", padx=12, pady=(0, 8))
        self._frames.append((pr, "surface", "bg"))
        Pill(pr, "读取提示词", self.wb_load_prompts, theme=t, font=F("small"), kind="ghost",
             padx=11, pady=7, radius=8, bg_key="surface", depth=2).pack(side="left")
        self.copy_pill = Pill(pr, "复制全部", self.wb_copy, theme=t, font=F("small"),
                              kind="primary", padx=13, pady=7, radius=8, bg_key="surface",
                              depth=3)
        self.copy_pill.pack(side="left", padx=5)
        self._dyn.append(self.copy_pill)

        self._lab(parent, "成片 / 废片接收（收完会自动弹反馈窗）", "h2").pack(
            anchor="w", padx=12, pady=(6, 4))
        rr = tk.Frame(parent, bg=t["surface"])
        rr.pack(fill="x", padx=12)
        self._frames.append((rr, "surface", "bg"))
        Pill(rr, "接收成片", lambda: self.wb_receive(True), theme=t, font=F("small"),
             kind="primary", padx=12, pady=7, radius=8, bg_key="surface", depth=3).pack(
                 side="left")
        Pill(rr, "接收废片", lambda: self.wb_receive(False), theme=t, font=F("small"),
             kind="danger", padx=12, pady=7, radius=8, bg_key="surface", depth=3).pack(
                 side="left", padx=5)
        self._lab(rr, "废因", "small", "muted").pack(side="left", padx=(10, 4))
        self.why_var = tk.StringVar()
        tk.Entry(rr, textvariable=self.why_var, font=F("small"), width=14, relief="flat",
                 highlightthickness=1).pack(side="left")

        self._lab(parent, "本轮反馈（提交后 agent 接着干）", "h2").pack(
            anchor="w", padx=12, pady=(10, 4))
        self.fb = tk.Text(parent, height=3, font=F("small"), bd=0, relief="flat",
                          highlightthickness=1, wrap="word")
        self.fb.pack(fill="x", padx=12)
        fr = tk.Frame(parent, bg=t["surface"])
        fr.pack(fill="x", padx=12, pady=(6, 8))
        self._frames.append((fr, "surface", "bg"))
        self.send_pill = Pill(fr, "提交反馈 · 让 agent 再出一版", lambda: self.wb_submit(True),
                              theme=t, font=F("small"), kind="primary", padx=13, pady=8,
                              radius=9, bg_key="surface", depth=4)
        self.send_pill.pack(side="left")
        self._dyn.append(self.send_pill)
        Pill(fr, "只记待办", lambda: self.wb_submit(False), theme=t, font=F("small"),
             kind="ghost", padx=10, pady=8, radius=9, bg_key="surface", depth=2).pack(
                 side="left", padx=5)
        Pill(fr, "叫 agent 出提示词", lambda: self.wb_ask("prompt"), theme=t,
             font=F("small"), kind="ghost", padx=10, pady=8, radius=9, bg_key="surface",
             depth=2).pack(side="left")

        self._lab(parent, "执行记录", "h2").pack(anchor="w", padx=12, pady=(2, 4))
        lrow = tk.Frame(parent, bg=t["surface"])
        lrow.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self._frames.append((lrow, "surface", "bg"))
        self.log = tk.Text(lrow, height=6, font=F("small"), bd=0, relief="flat",
                           highlightthickness=1, wrap="word")
        self.log.pack(side="left", fill="both", expand=True)
        lsb = tk.Scrollbar(lrow, orient="vertical", command=self.log.yview, width=9,
                           bd=0, relief="flat", elementborderwidth=0)
        lsb.pack(side="right", fill="y", padx=(2, 0))
        self.log.config(yscrollcommand=lsb.set)
        self._lsb = lsb
        self.log.configure(state="disabled")

    # ---- ② 素材栏 / ③ 提示词栏 的动作 --------------------------------------
    def _log(self, msg):
        try:
            self.log.configure(state="normal")
            self.log.insert("end", msg + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")
        except tk.TclError:
            pass

    def intake_pick(self, folder):
        if folder:
            p = filedialog.askdirectory(title="选要投放的文件夹")
            if p:
                self._pending.append(os.path.normpath(p))
        else:
            for p in filedialog.askopenfilenames(title="选要投放的文件") or ():
                self._pending.append(os.path.normpath(p))
        self.intake_refresh()

    def intake_clear(self):
        self._pending = []
        self.intake_refresh()

    def intake_refresh(self):
        """刷新待投放列表，并把软件判出来的角色先亮出来（只规划，不落盘）。"""
        try:
            self.intake_lb.delete(0, "end")
        except tk.TclError:
            return
        root = ""
        if pcore:
            try:
                root = pcore.detect_root() or ""
            except Exception:                                 # noqa: BLE001
                root = ""
        try:
            self.intake_root_lab.config(text="样本库：%s" % (root or "（未配置，会在建骨架时问你）"))
        except tk.TclError:
            pass
        if not (pcore and self._pending):
            self._plan = None
            try:
                self.intake_hint.config(text="选定素材后点上面这颗按钮")
            except tk.TclError:
                pass
            return
        try:
            plan = pcore.auto_plan(self._pending)
        except Exception as e:                                # noqa: BLE001
            self._log("!! 规划失败：%s" % e)
            return
        self._plan = plan
        for it in plan["items"]:
            try:
                self.intake_lb.insert("end", "%-5s %s" % (it["role"], it["name"]))
            except tk.TclError:
                pass
        try:
            self.intake_hint.config(text="将建成：%s  （素材 %d 个，角色已判好）"
                                         % (plan["final_name"] or "（推不出名）", plan["count"]))
        except tk.TclError:
            pass

    def intake_go(self):
        """一键：软件自动定根 / 定名 → 建骨架 → 按角色归类 → 切到该项目。"""
        if not pcore:
            self._log("!! 找不到 project_core.py")
            return
        if not self._pending:
            self._log("还没有选素材")
            return
        try:
            pdir, _plan, _log = pcore.auto_build(self._pending, on_log=self._log)
        except Exception as e:                                # noqa: BLE001
            self._log("!! 建骨架失败：%s" % e)
            return
        if not pdir:
            self._log("建骨架失败（可能还没配置样本库根目录）")
            return
        self._pending = []
        self.intake_refresh()
        self.set_project(pdir)
        self.reload()
        self._log("✅ 骨架就绪：%s" % pdir)

    def set_project(self, pdir):
        """切换当前项目：刷新项目卡 + 提示词区 + 状态栏。"""
        self.proj_dir = pdir or ""
        name = os.path.basename(os.path.normpath(pdir)) if pdir else "未选项目 · 骨架未建"
        try:
            self.proj_lab.config(text=name)
        except tk.TclError:
            pass
        self.wb_load_prompts(quiet=True)

    def wb_load_prompts(self, quiet=False):
        proj = getattr(self, "proj_dir", "")
        if not (pcore and proj) or not os.path.isdir(proj):
            if not quiet:
                self._log("先建骨架（②栏）或在①栏选中一个项目")
            return
        parts = []
        try:
            sk = pcore.load_skeleton(proj)
            if sk:
                for pr in sk["project"].get("prompts", []):
                    parts.append("【%s】\n%s" % (pr.get("name", "提示词"), pr.get("text", "")))
            wenan = os.path.join(proj, "文案")
            if os.path.isdir(wenan):
                for fn in sorted(os.listdir(wenan)):
                    if fn.lower().endswith((".txt", ".md")):
                        try:
                            body = open(os.path.join(wenan, fn),
                                        encoding="utf-8-sig").read()
                        except OSError:
                            continue
                        parts.append("── 文案/%s ──\n%s" % (fn, body.strip()))
        except Exception as e:                                # noqa: BLE001
            self._log("!! 读提示词失败：%s" % e)
            return
        txt = "\n\n".join(parts) if parts else \
            "（还没有提示词。agent 写回 骨架.json 的 prompts、或放进 文案/ 后，点「读取提示词」）"
        try:
            self.prompt.configure(state="normal")
            self.prompt.delete("1.0", "end")
            self.prompt.insert("1.0", txt)
            self.prompt.configure(state="disabled")
        except tk.TclError:
            return
        if not quiet:
            self._log("已读取提示词（%d 段）" % len(parts))
        self.wb_state()

    def wb_copy(self):
        try:
            txt = self.prompt.get("1.0", "end").strip()
        except tk.TclError:
            return
        if not txt or txt.startswith("（还没有提示词"):
            self._log("没有可复制的内容")
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(txt)
            self._log("已复制 %d 字，去即梦粘贴即可" % len(txt))
        except tk.TclError as e:
            self._log("复制失败：%s" % e)

    def wb_receive(self, good):
        proj = getattr(self, "proj_dir", "")
        if not (pcore and proj):
            self._log("先建骨架（②栏）")
            return
        ps = filedialog.askopenfilenames(
            title="选要接收的%s（可多选）" % ("成片" if good else "废片"))
        if not ps:
            return
        try:
            placed, _ = pcore.accept_deliverables(
                proj, list(ps), verdict="good" if good else "bad",
                note=self.why_var.get().strip(), on_log=self._log)
        except Exception as e:                                # noqa: BLE001
            self._log("!! 接收失败：%s" % e)
            return
        if placed:
            self._log("---- 已接收 %d 个到 %s/ ----"
                      % (len(placed), "成片" if good else "废片"))
            self.wb_state()
            FeedbackWin(self.root, self, proj, placed, good)   # 收完就弹反馈窗（可关）

    def wb_submit(self, call_agent):
        proj = getattr(self, "proj_dir", "")
        if not (pcore and proj):
            self._log("先建骨架（②栏）")
            return ""
        try:
            text = self.fb.get("1.0", "end").strip()
        except tk.TclError:
            return ""
        if not text:
            self._log("反馈还是空的，先写两句")
            return ""
        try:
            n = pcore.new_round(proj, text)
            pcore.push_todo(proj, "反馈", text)
        except Exception as e:                                # noqa: BLE001
            self._log("!! 写待办失败：%s" % e)
            return ""
        self._log("已记入第 %d 轮反馈（_会话/轮次/%03d-反馈.txt）" % (n, n))
        try:
            self.fb.delete("1.0", "end")
        except tk.TclError:
            pass
        self.wb_state()
        if call_agent:
            self.wb_ask("feedback", text)
        return text

    def wb_state(self):
        proj = getattr(self, "proj_dir", "")
        if not (pcore and proj) or not os.path.isdir(proj):
            try:
                self.wb_state_lab.config(text="—")
            except tk.TclError:
                pass
            return
        try:
            st = pcore.read_state(proj)
            pend = len(pcore.read_todos(proj))
            self.wb_state_lab.config(text="阶段 %s · 第 %s 轮 · 待办 %d"
                                          % (st.get("stage", "新建"), st.get("round", 0), pend))
        except Exception:                                     # noqa: BLE001
            pass

    def wb_ask(self, what, feedback=""):
        """叫 agent 干活：读待办 → 按反馈/首轮出提示词 → 写回执。"""
        proj = getattr(self, "proj_dir", "")
        if not (pcore and abridge and proj):
            self._log("通道不可用（缺 project_core / agent_bridge）")
            return
        ok, why = abridge.available()
        if not ok:
            self._log("叫不动 agent：%s" % why)
            return
        st = pcore.read_state(proj)
        sid = st.get("agentSession") or None
        if what == "feedback":
            todo = ("用户在界面上给了第 %s 轮反馈：\n%s\n\n"
                    "请按反馈重出一版提示词：更新 %s\\文案\\ 下的稿子与 骨架.json 的 prompts，"
                    "并把要上传的文件副本放进 即梦上传\\（含上传说明.txt）。"
                    "完成后跑 tools\\project_core.py --project \"%s\" --receipt \"改了什么\" "
                    "--todo-done 写回执。" % (st.get("round", 1), feedback, proj, proj))
        else:
            todo = ("用户点了「叫 agent 出提示词」。请扫描 %s\\ 下的 骨架.json 与 素材/、文案/，"
                    "按 skill 规则出一版提示词，写回 骨架.json 的 prompts 与 文案/，"
                    "并把要上传的文件副本按引用编号放进 即梦上传\\；"
                    "完成后跑 tools\\project_core.py --project \"%s\" --receipt \"改了什么\" "
                    "--todo-done 写回执。" % (proj, proj))
        self._log("→ 正在叫 agent …（可以继续用界面，跑完自动写回执）")
        self.wb_state()

        def worker():
            res = abridge.ask(todo, session_id=sid, cwd=proj, timeout=900,
                              permission_mode="acceptEdits")

            def done():
                try:
                    if res.get("session_id") and res["session_id"] != sid:
                        pcore.write_state(proj, agentSession=res["session_id"])
                    if res.get("ok"):
                        pcore.push_receipt(proj, res.get("text", ""), kind="出提示词")
                        pcore.mark_todos_done(proj)
                        self._log("✅ agent 回来了：%s" % (res.get("text") or "")[:300])
                        self.wb_load_prompts(quiet=True)
                    else:
                        self._log("!! agent 没跑成：%s"
                                  % (res.get("error") or (res.get("stderr") or "")[:300]))
                    self.wb_state()
                except Exception as e:                        # noqa: BLE001
                    self._log("!! 回写失败：%s" % e)
            try:
                self.root.after(10, done)
            except tk.TclError:
                pass

        try:
            import threading
            threading.Thread(target=worker, daemon=True).start()
        except Exception as e:                                # noqa: BLE001
            self._log("!! 起线程失败：%s" % e)

    # ---- 分隔条（四栏通用：每条改「它左边那一栏」的宽度）-------------------
    def _split_hover(self, grip, pill, on):
        t = self.theme
        try:
            grip.configure(bg=t["bg"], cursor="sb_h_double_arrow" if on else "")
            grip.itemconfig(pill, fill=t["accent"] if on else t["border"])
        except tk.TclError:
            pass

    def _split_configure(self, grip=None, pill=None):
        """把中央药丸画成竖直短条（高度自适应，居中）。"""
        grip = grip if grip is not None else getattr(self, "_grip", None)
        pill = pill if pill is not None else getattr(self, "_grip_pill", None)
        if grip is None or pill is None:
            return
        try:
            h = grip.winfo_height()
            grip.coords(pill, 3, max(0, h // 2 - 26), 5, min(h, h // 2 + 26))
        except tk.TclError:
            pass

    def _split_press(self, e, i=0):
        self._split_drag(e, i)

    def _split_drag(self, e, i=0):
        """按鼠标在 body 里的位置算左栏宽度（用绝对位置，不累加 dx，不会漂）。"""
        try:
            x0 = self._body.winfo_rootx()
            card = self._cols[i][0]
        except (tk.TclError, IndexError, AttributeError):
            return
        base = sum(c[1] for c in self._cols[:i]) + 8 * i      # 前面各栏 + 前面的分隔条
        lo, hi = COL_LIMITS[i]
        w = max(lo, min(hi, int(e.x_root - x0) - base))
        if w != self._cols[i][1]:
            self._cols[i][1] = w
            try:
                card.configure(width=w)
            except tk.TclError:
                pass

    def _split_release(self, _e=None, i=0):
        widths = [c[1] for c in getattr(self, "_cols", [])]
        save_col_widths(widths)
        try:
            self.status.config(text="栏宽已保存：%s" % " / ".join(str(w) for w in widths))
        except tk.TclError:
            pass

    # ---- 素材投放 / 建骨架 ------------------------------------------------
    def open_material(self, project=None):
        """切到某个项目（四栏内联后不再弹窗；保留此名以兼容命令行 --material）。"""
        if project:
            self.set_project(project)
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        except tk.TclError:
            pass
        self.intake_refresh()
        return None
        win = MaterialPanel(self.root, self, project=project)
        self._mat_win = win

        def _gone(_e, w=win):
            if getattr(self, "_mat_win", None) is w:
                self._mat_win = None

        win.bind("<Destroy>", _gone)
        return win

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
    """命令行参数。

    评分用途：
        --sample <关键词> / -s <关键词>        打开就定位到名字含该关键词的样本
    素材与骨架用途（rules.md 第 264-272 条）：
        --material                            直接打开「素材投放 · 建骨架」窗口
        --root <目录> --name <实验名>          建骨架（自动避让重名，加序号）
        --project <目录>                      指定项目目录
        --add <路径> [<路径> ...]             投放素材（文件或文件夹，可跟多个）
        --note <文字>                         给这批素材记一句备注
        --platform <名>                       平台（即梦 / 小云雀 / updream）
    """
    a, o = sys.argv[1:], {"sample": None, "material": False, "root": None, "name": None,
                          "project": None, "add": [], "note": "", "platform": ""}
    i = 0
    while i < len(a):
        x = a[i]
        if x in ("--sample", "-s") and i + 1 < len(a):
            o["sample"] = a[i + 1]
            i += 2
        elif x.startswith("--sample="):
            o["sample"] = x.split("=", 1)[1]
            i += 1
        elif x == "--material":
            o["material"] = True
            i += 1
        elif x in ("--root", "--name", "--project", "--note", "--platform") and i + 1 < len(a):
            o[x[2:]] = a[i + 1]
            i += 2
        elif x == "--add":
            i += 1
            while i < len(a) and not a[i].startswith("--"):
                o["add"].append(a[i])
                i += 1
            o["material"] = True
        else:
            i += 1
    return o


def _run_material_actions(app, opt):
    """按命令行把「建骨架 / 投素材」跑掉（结果写进 ③栏 执行记录）。"""
    if not pcore:
        return
    if opt.get("project"):
        app.set_project(opt["project"])
    if opt.get("add") or opt.get("name"):
        if opt.get("add"):
            app._pending.extend(os.path.normpath(p) for p in opt["add"])
            app.intake_refresh()
        try:
            pdir, _plan, _log = pcore.auto_build(app._pending or [],
                                                 root=opt.get("root"),
                                                 name=opt.get("name"),
                                                 on_log=app._log)
        except Exception as e:                                # noqa: BLE001
            app._log("!! 自动建骨架失败：%s" % e)
            return
        if pdir:
            app._pending = []
            app.intake_refresh()
            app.set_project(pdir)
            app.reload()
            app._log("✅ 骨架就绪：%s" % pdir)


def main():
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:                                            # noqa: BLE001
        pass
    opt = _parse_args()
    root = tk.Tk()
    try:
        dpi = root.winfo_fpixels("1i")
        root.tk.call("tk", "scaling", dpi / 72.0)
    except tk.TclError:
        pass
    try:
        app = App(root, opt.get("sample"))
        if opt.get("material") or opt.get("add") or opt.get("name"):
            root.after(120, lambda: _run_material_actions(app, opt))
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
