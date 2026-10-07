# -*- coding: utf-8 -*-
"""doubao_tray.py —— 豆包工作窗口 → 任务栏托盘（隐藏图标区）。

背景（2026-10-07 用户裁定）：工作台驱动豆包时，桥开的窗口是「离屏 normal」——桌面看不见，
但任务栏/Alt-Tab 一直挂着窗，点又点不出来。用户要求把豆包收进任务栏角落的**隐藏图标弹层**
（和微信/QQ 那些后台图标一个待遇）。

为什么不走 CDP：Browser.setWindowBounds 只有 normal/minimized/maximized/fullscreen，
而 minimized 实测会冻结豆包（2026-09-29，7 分 22 秒不干活）。所以走 Win32：

  · ShowWindow(SW_HIDE) 把豆包所有可见顶层窗口藏掉——屏幕和任务栏同时消失；
    桥启动参数里已带三个 --disable-*throttling 开关，隐藏态照常渲染（与离屏 normal 同理）；
  · 一个 WinForms NotifyIcon 常驻托盘（图标取自 DoubaoWork.exe 本体），左键 = 显示/隐藏切换；
    显示时顺手把离屏窗口挪回屏幕内，不然点出来也看不见；
  · pythonnet 在载荷里现成有（win_drag 在用）；真缺了就整段退化成「离屏不藏」，不影响主流程。

单例：本模块会被 agent_bridge / wb_video 各自 import，靠 sys.modules 天然复用同一份。
"""
import os
import sys
import time
import ctypes
import threading
from ctypes import wintypes

_EXE = "DoubaoWork.exe"
SW_HIDE, SW_SHOW = 0, 5
SWP_NOSIZE, SWP_NOZORDER = 0x1, 0x4

_lock = threading.Lock()
_st = {"icon": None}          # NotifyIcon 实例（托盘线程内创建/销毁）


# ---------------- Win32：找窗口 / 藏 / 显 ----------------
def _doubao_pids():
    """所有 DoubaoWork.exe 进程的 pid（tasklist，免 psutil 依赖）。"""
    import subprocess
    out = set()
    try:
        p = subprocess.run(["tasklist", "/FI", "IMAGENAME eq " + _EXE, "/FO", "CSV", "/NH"],
                           capture_output=True, text=True, timeout=10)
        for ln in (p.stdout or "").splitlines():
            if _EXE.lower() not in ln.lower():
                continue
            parts = [x.strip().strip('"') for x in ln.split('","')]
            if len(parts) > 1 and parts[1].isdigit():
                out.add(int(parts[1]))
    except Exception:  # noqa: BLE001
        pass
    return out


def _doubao_windows(only_visible=True):
    """豆包的顶层窗口句柄（带标题的才算——renderer/crashpad 那些隐形助手窗一律跳过）。"""
    user32 = ctypes.windll.user32
    pids = _doubao_pids()
    hits = []
    if not pids:
        return hits
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lp):
        try:
            if only_visible and not user32.IsWindowVisible(hwnd):
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in pids and user32.GetWindowTextLengthW(hwnd) > 0:
                hits.append(hwnd)
        except Exception:  # noqa: BLE001
            pass
        return True

    user32.EnumWindows(proto(_cb), 0)
    return hits


def hide_all():
    """藏掉豆包所有可见窗口；返回藏掉的扇数。"""
    n = 0
    try:
        user32 = ctypes.windll.user32
        for h in _doubao_windows(True):
            if user32.ShowWindow(int(h), SW_HIDE):
                n += 1
    except Exception:  # noqa: BLE001
        pass
    return n


def show_all():
    """把藏着的豆包窗口显回来：SW_SHOW + 挪回屏幕内（离屏窗不挪等于没显）+ 置前。"""
    try:
        user32 = ctypes.windll.user32
        for h in _doubao_windows(False):
            if user32.IsWindowVisible(int(h)):
                continue
            user32.ShowWindow(int(h), SW_SHOW)
            user32.SetWindowPos(int(h), 0, 60, 60, 0, 0, SWP_NOSIZE | SWP_NOZORDER)
            user32.SetForegroundWindow(int(h))
    except Exception:  # noqa: BLE001
        pass


# ---------------- 托盘图标（WinForms NotifyIcon，专用 STA 线程 + 消息泵） ----------------
def _ensure_icon(exe_path=""):
    with _lock:
        if _st["icon"] is not None:
            return True
    try:
        import clr
        clr.AddReference("System.Windows.Forms")
        clr.AddReference("System.Drawing")
        import System.Drawing as SD
        import System.Windows.Forms as WF
    except Exception:  # noqa: BLE001
        return False          # 没有 pythonnet：退化成只藏窗不设托盘，主流程不受影响

    def _pump():
        ni = None
        try:
            icon = None
            try:
                if exe_path and os.path.isfile(exe_path):
                    icon = SD.Icon.ExtractAssociatedIcon(exe_path)
            except Exception:  # noqa: BLE001
                icon = None
            ni = WF.NotifyIcon()
            ni.Icon = icon or SD.SystemIcons.Application
            ni.Text = "豆包工作（后台）· 点击 显示/隐藏"
            ni.Visible = True

            def _on_click(sender, ev):
                if _doubao_windows(True):
                    hide_all()
                else:
                    show_all()

            ni.MouseClick += _on_click

            t = WF.Timer()
            t.Interval = 3000

            def _tick(_s, _e):
                if not _doubao_pids():      # 豆包退了：托盘图标跟着撤
                    try:
                        ni.Visible = False
                        ni.Dispose()
                    except Exception:  # noqa: BLE001
                        pass
                    with _lock:
                        _st["icon"] = None
                    t.Stop()
            t.Tick += _tick
            t.Start()

            with _lock:
                _st["icon"] = ni
            WF.Application.Run()        # 消息泵：让托盘事件活起来
        except Exception:  # noqa: BLE001
            try:
                if ni is not None:
                    ni.Dispose()
            except Exception:  # noqa: BLE001
                pass
            with _lock:
                _st["icon"] = None

    threading.Thread(target=_pump, daemon=True, name="doubao-tray").start()
    return True


def hide_async(exe_path="", wait_secs=60):
    """起个短命线程：等豆包窗口出现就藏掉+设托盘（桥拉起豆包有延迟，轮询到 wait_secs 为止）。

    挂点：① agent_bridge 走豆包通道起一轮 ask 时；② wb_video prepare/confirm 后台任务启动时。
    --visible（跳转豆包界面手动模式）**不要调这个**——那扇窗就是要给用户看的。
    """
    def _job():
        deadline = time.time() + max(5, int(wait_secs))
        while time.time() < deadline:
            if _doubao_windows(True):
                hide_all()
                _ensure_icon(exe_path)
                return
            time.sleep(0.6)
    threading.Thread(target=_job, daemon=True, name="doubao-tray-hide").start()
