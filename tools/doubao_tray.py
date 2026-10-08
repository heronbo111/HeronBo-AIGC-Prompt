# -*- coding: utf-8 -*-
"""doubao_tray.py —— 豆包工作窗口 → 任务栏托盘（隐藏图标区）。

背景（2026-10-07 用户裁定）：工作台驱动豆包时，桥开的窗口是「离屏 normal」——桌面看不见，
但任务栏/Alt-Tab 一直挂着窗，点又点不出来。用户要求把豆包收进任务栏角落的**隐藏图标弹层**
（和微信/QQ 那些后台图标一个待遇）。

为什么不走 CDP：Browser.setWindowBounds 只有 normal/minimized/maximized/fullscreen，
而 minimized 实测会冻结豆包（2026-09-29，7 分 22 秒不干活）；何况"最小化"本身仍留在任务栏，
不是用户要的效果。

为什么不用 ShowWindow(SW_HIDE)（2026-10-08 换掉的旧做法）：真藏之后 Chromium 认为"这个窗口
没了"，桥下一次要挂标签时 `ensureBrowserWindow` 会判定"没有窗口能挂标签"，于是
`Target.createTarget{newWindow:true}` **新开一扇屏幕内的可见窗**——托盘白设，任务栏图标照样
回来。实测：SW_HIDE 之后 CDP 连 windowId 都读不到（`-32000 Browser window not found`）。

现在的做法（2026-10-08 实测通过）：
  · 给豆包窗口加 **WS_EX_TOOLWINDOW**：窗口照样"可见"（Chromium 完全无感、CDP 读得到窗口），
    但被 Windows 从任务栏和 Alt-Tab 里摘掉；再 SetWindowPos 挪到屏外，桌面上就彻底看不见。
    实测：只 SetWindowLong 即生效；CDP 的 Browser.setWindowBounds 不会重置这个样式。
  · 一个 WinForms NotifyIcon 常驻托盘（图标取自 DoubaoWork.exe 本体），左键 = 显示/隐藏切换；
    显示时去掉 TOOLWINDOW 并把离屏窗口挪回屏幕内，不然点出来也看不见。
  · 3 秒 tick 兼当守卫：处于"收进托盘"模式时，把桥新冒出来的窗口也一并收进去
    （否则 `newWindow:true` 新建的那扇会挂在任务栏上）。
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
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER = 0x1, 0x2, 0x4
SWP_NOACTIVATE = 0x10
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080     # 有它 = 不进任务栏、不进 Alt-Tab
WS_EX_APPWINDOW = 0x00040000
OFFSCREEN_X, OFFSCREEN_Y = -3200, -3200

_lock = threading.Lock()
_st = {"icon": None, "want_hidden": False}   # NotifyIcon 实例 + 是否处于"收进托盘"模式


# ---------------- Win32：找窗口 / 收进托盘 / 放出来 ----------------
def _doubao_pids():
    """所有 DoubaoWork.exe 进程的 pid（tasklist，免 psutil 依赖）。"""
    import subprocess
    out = set()
    try:
        # GUI 宿主（score-tool.exe/pythonw）没有控制台：tasklist 这种控制台程序
        # 每 3 秒被托盘 tick 调一次，不带 CREATE_NO_WINDOW 就会频繁闪黑窗
        #（2026-10-07 黑窗诊断报告的漏网缺口，与 agent_bridge.no_window_kwargs 同理）。
        p = subprocess.run(["tasklist", "/FI", "IMAGENAME eq " + _EXE, "/FO", "CSV", "/NH"],
                           capture_output=True, text=True, timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
        for ln in (p.stdout or "").splitlines():
            if _EXE.lower() not in ln.lower():
                continue
            parts = [x.strip().strip('"') for x in ln.split('","')]
            if len(parts) > 1 and parts[1].isdigit():
                out.add(int(parts[1]))
    except Exception:  # noqa: BLE001
        pass
    return out


def _doubao_windows(only_visible=True, pids=None):
    """豆包的顶层窗口句柄（带标题的才算——renderer/crashpad 那些隐形助手窗一律跳过）。"""
    user32 = ctypes.windll.user32
    pids = _doubao_pids() if pids is None else pids
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


def _apply_tool(user32, h):
    """给一扇窗加 TOOLWINDOW（去任务栏/Alt-Tab）并挪到屏外。窗口保持"可见"。"""
    ex = user32.GetWindowLongW(h, GWL_EXSTYLE)
    if not (ex & WS_EX_TOOLWINDOW):
        user32.SetWindowLongW(h, GWL_EXSTYLE, (ex | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW)
    user32.SetWindowPos(h, 0, OFFSCREEN_X, OFFSCREEN_Y, 0, 0,
                        SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
    return True


def hide_all():
    """把豆包收进托盘：加 WS_EX_TOOLWINDOW + 挪离屏；返回收掉的扇数。

    注意**不隐藏**窗口——真藏（SW_HIDE）会让桥以为没窗口可挂标签，转手 newWindow
    新开一扇可见窗，任务栏图标又回来。
    """
    n = 0
    try:
        user32 = ctypes.windll.user32
        for h in _doubao_windows(True):
            if _apply_tool(user32, int(h)):
                n += 1
        _st["want_hidden"] = True
    except Exception:  # noqa: BLE001
        pass
    return n


def show_all():
    """把豆包放出来：去掉 TOOLWINDOW + 屏外的挪回屏幕内 + 置前。

    `--visible`（跳转豆包界面、请用户手动确认）调它：那一轮窗口就是要给用户看的。
    注意只搬**屏外**的窗，用户自己摆在桌面上的豆包窗不动，免得每次都被怼到左上角。
    """
    _st["want_hidden"] = False
    try:
        user32 = ctypes.windll.user32
        for h in _doubao_windows(True):
            h = int(h)
            ex = user32.GetWindowLongW(h, GWL_EXSTYLE)
            if ex & WS_EX_TOOLWINDOW:
                # 去样式要 hide/show 刷一下，shell 才会把任务栏图标加回来
                user32.ShowWindow(h, SW_HIDE)
                user32.SetWindowLongW(h, GWL_EXSTYLE, ex & ~WS_EX_TOOLWINDOW)
                user32.ShowWindow(h, SW_SHOW)
            r = wintypes.RECT()
            if user32.GetWindowRect(h, ctypes.byref(r)) and r.left <= -3000:
                user32.SetWindowPos(h, 0, 60, 60, 0, 0, SWP_NOSIZE | SWP_NOZORDER)
            user32.SetForegroundWindow(h)
    except Exception:  # noqa: BLE001
        pass


def _taskbar_windows():
    """还挂在任务栏/Alt-Tab 上的豆包窗口（可见 + 无 TOOLWINDOW + 无 owner）。"""
    user32 = ctypes.windll.user32
    out = []
    for h in _doubao_windows(False):
        h = int(h)
        try:
            if not user32.IsWindowVisible(h):
                continue
            ex = user32.GetWindowLongW(h, GWL_EXSTYLE)
            if (ex & WS_EX_TOOLWINDOW) == 0 and user32.GetWindow(h, 4) == 0:
                out.append(h)
        except Exception:  # noqa: BLE001
            pass
    return out


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
        return False          # 没有 pythonnet：退化成只收窗不设托盘，主流程不受影响

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
                if _taskbar_windows():
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
                    return
                if _st.get("want_hidden"):
                    # 守卫：桥新开的窗口（newWindow:true）没带 TOOLWINDOW，会挂在任务栏上
                    user32 = ctypes.windll.user32
                    for h in _taskbar_windows():
                        _apply_tool(user32, h)
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
    """起个短命线程：等豆包窗口出现就收进托盘+设托盘图标（桥拉起豆包有延迟，轮询到 wait_secs）。

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
