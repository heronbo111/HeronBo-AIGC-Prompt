# -*- coding: utf-8 -*-
r"""拖动/缩放自动化验证（真实鼠标事件走全链路，2026-10-06 排"彻底拖不动"用）。

用法：
    python tools\drag_selftest.py            # 拖动 + 右下角缩放各试一轮，打印结果

前置：工作台已在跑（源码或 exe 都行）。脚本会：
  1. 找到工作台窗口（标题前缀 HeronBo）；
  2. 把窗口挪到已知位置、还原尺寸；
  3. SendInput 真实按下标题栏 → 移动 → 松开（模拟用户拖动）；
  4. 同样模拟右下角边缘缩放；
  5. 对比窗口矩形变化，并给出结论。窗口若在最大化会先还原。
"""
import ctypes
import ctypes.wintypes as wt
import subprocess
import sys
import time

u32 = ctypes.windll.user32

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_ABSOLUTE = 0x8000


def find_win():
    """按标题前缀找工作台顶层窗口。"""
    hit = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(h, l):
        n = u32.GetWindowTextLengthW(h)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            u32.GetWindowTextW(h, buf, n + 1)
            t = buf.value
            if "AI 视频工作台" in t and u32.IsWindowVisible(h):
                hit.append((h, t))
        return True
    u32.EnumWindows(cb, 0)
    return hit[0] if hit else (None, None)


def rect_of(h):
    r = wt.RECT()
    u32.GetWindowRect(h, ctypes.byref(r))
    return (r.left, r.top, r.right, r.bottom)


class _MI(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG),
                ("mouseData", wt.DWORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _INPUT(ctypes.Structure):
    class U(ctypes.Union):
        _fields_ = [("mi", _MI)]
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", U)]


def _inp(dx, dy, flags):
    mi = _MI(dx, dy, 0, flags, 0, None)
    inp = _INPUT(0, _INPUT.U(mi))
    u32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))


def move_to(x, y):
    vx = u32.GetSystemMetrics(76); vy = u32.GetSystemMetrics(77)
    vw = u32.GetSystemMetrics(78); vh = u32.GetSystemMetrics(79)
    ax = int((x - vx) * 65535 / max(1, vw - 1))
    ay = int((y - vy) * 65535 / max(1, vh - 1))
    _inp(ax, ay, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE)


def down():
    _inp(0, 0, MOUSEEVENTF_LEFTDOWN)


def up():
    _inp(0, 0, MOUSEEVENTF_LEFTUP)


def restore(h):
    if u32.IsZoomed(h):
        u32.ShowWindow(h, 9)            # SW_RESTORE
        time.sleep(0.5)


def place(h, x, y, w, hh):
    u32.SetWindowPos(h, 0, x, y, w, hh, 0x0004)   # SWP_NOZORDER
    time.sleep(0.4)


def drag(h, fx, fy, dx, dy, steps=12, label=""):
    """在屏幕物理坐标 (fx,fy) 按下，拖 (dx,dy) 后松开。"""
    before = rect_of(h)
    move_to(fx, fy); time.sleep(0.25)
    down(); time.sleep(0.30)                       # 按住，给 JS→桥→BeginInvoke 时间
    for i in range(1, steps + 1):
        move_to(fx + dx * i // steps, fy + dy * i // steps)
        time.sleep(0.03)
    time.sleep(0.2)
    up(); time.sleep(0.6)
    after = rect_of(h)
    moved = (after[0] - before[0], after[1] - before[1])
    size = (after[2] - after[0] - (before[2] - before[0]),
            after[3] - after[1] - (before[3] - before[1]))
    print("%s 位移=%s 尺寸增量=%s" % (label, moved, size))
    return after


def main():
    # 测试进程自己要 DPI 感知：否则 GetWindowRect/SendInput 的坐标被系统虚拟化，点了别处
    try:
        u32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))   # PER_MONITOR_AWARE_V2
    except Exception:                                            # noqa: BLE001
        try:
            u32.shcore.SetProcessDpiAwareness(2)
        except Exception:                                        # noqa: BLE001
            pass
    h, title = find_win()
    if not h:
        print("✗ 没找到工作台窗口（先把它跑起来）")
        return 1
    print("窗口: %s  hwnd=%s  rect=%s" % (title, h, rect_of(h)))
    restore(h)
    place(h, 100, 100, 1280, 800)

    # ① 拖动：按住标题栏（窗内顶部 20px 处）
    r = rect_of(h)
    dpi = u32.GetDpiForWindow(h) / 96.0
    drag(h, r[0] + int(300 * dpi), r[1] + int(20 * dpi), 160, 100,
         label="① 标题栏拖动")
    r2 = rect_of(h)
    ok_move = abs(r2[0] - r[0]) > 60 and abs(r2[1] - r[1]) > 40

    # ② 缩放：抓右下角 (边缘 3px 内)
    r = rect_of(h)
    drag(h, r[2] - 2, r[3] - 2, -200, -150, label="② 右下角缩放")
    r3 = rect_of(h)
    ok_size = (r[2] - r3[2]) > 100 and (r[3] - r3[3]) > 60

    print("=" * 50)
    print("拖动: %s ｜ 缩放: %s" % ("✓ 通" if ok_move else "✗ 没动",
                                    "✓ 通" if ok_size else "✗ 没变"))
    return 0 if (ok_move and ok_size) else 2


if __name__ == "__main__":
    sys.exit(main())
