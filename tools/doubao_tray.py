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
SW_HIDE, SW_SHOW, SW_RESTORE, SW_SHOWNOACTIVATE = 0, 5, 9, 4
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER = 0x1, 0x2, 0x4
SWP_NOACTIVATE = 0x10
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080     # 有它 = 不进任务栏、不进 Alt-Tab
WS_EX_APPWINDOW = 0x00040000
OFFSCREEN_X, OFFSCREEN_Y = -3200, -3200

_lock = threading.Lock()
_st = {"icon": None, "want_hidden": False, "guard": False}   # NotifyIcon 实例 + 静默模式 + 守场线程


# ---------------- Win32：找窗口 / 收进托盘 / 放出来 ----------------
def _pids_of(exe):
    """指定进程名的所有 pid（tasklist，免 psutil 依赖）。"""
    import subprocess
    out = set()
    try:
        # GUI 宿主（score-tool.exe/pythonw）没有控制台：tasklist 这种控制台程序
        # 每 3 秒被托盘 tick 调一次，不带 CREATE_NO_WINDOW 就会频繁闪黑窗
        #（2026-10-07 黑窗诊断报告的漏网缺口，与 agent_bridge.no_window_kwargs 同理）。
        # 2026-11-15：`text=True` 默认按 UTF-8 解码，中文 Windows 的 tasklist 输出是
        # ANSI(936)，会抛 UnicodeDecodeError —— 异常发生在 subprocess 的读线程里，
        # 外层 try 接不住，`_pids_of` 就静默返回空集合（"找不到窗口/进程"）。显式指定 mbcs。
        p = subprocess.run(["tasklist", "/FI", "IMAGENAME eq " + exe, "/FO", "CSV", "/NH"],
                           capture_output=True, text=True, encoding="mbcs", errors="ignore",
                           timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
        for ln in (p.stdout or "").splitlines():
            if exe.lower() not in ln.lower():
                continue
            parts = [x.strip().strip('"') for x in ln.split('","')]
            if len(parts) > 1 and parts[1].isdigit():
                out.add(int(parts[1]))
    except Exception:  # noqa: BLE001
        pass
    return out


def _doubao_pids():
    """所有 DoubaoWork.exe 进程的 pid。"""
    return _pids_of(_EXE)


_WB_EXES = ("HeronBo.exe", "score-tool.exe")


def _windows_of(pids):
    """给定 pid 集合的顶层可见带标题窗句柄。"""
    user32 = ctypes.windll.user32
    hits = []
    if not pids:
        return hits
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lp):
        try:
            if not user32.IsWindowVisible(hwnd):
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


def focus_back():
    """豆包自己抢了前台就把焦点还给工作台（静默启动的最后一步）。

    Electron 起来时是 `SW_SHOWNORMAL`，必然把用户从工作台切走。窗口不能藏（会被判
    遮挡冻结渲染），所以只能"显示但不占前台"。这里只在前台确实是豆包时动手，
    用户主动切到豆包的时候不会被抢。
    """
    try:
        user32 = ctypes.windll.user32
        fg = user32.GetForegroundWindow()
        if not fg:
            return False
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(fg, ctypes.byref(pid))
        if pid.value not in _doubao_pids():
            return False                       # 前台不是豆包，什么都不动
        if not _is_browser(fg):
            return False                       # 前台是用户自己的豆包主窗口 —— 那是他要用的，不许抢
        pids = set()
        for exe in _WB_EXES:
            pids |= _pids_of(exe)
        wins = _windows_of(pids)
        if not wins:
            return False
        wb = wins[0]
        if user32.IsIconic(wb):
            user32.ShowWindow(wb, SW_RESTORE)
        user32.SetForegroundWindow(wb)
        return True
    except Exception:  # noqa: BLE001
        return False


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


BROWSER_SUFFIX = "豆包工作浏览器"
# 2026-11-15 第四次（用户："你还是弄反了……在豆包生视频那个页面手动打开 cdp 或是我现在跟你
# 对话的这个魔改版豆包都被强行最小化了"）：实测这台机器上**只有一个 DoubaoWork.exe 主进程
# （24516），它自己就带 `--remote-debugging-port=9333` 启动** —— 也就是说用户日常用的魔改版
# 豆包 **就是** CDP 实例，两类窗口同进程。上一版按进程判，于是把他自己正在用的窗口一起摘了
# 任务栏（图标消失、像是被最小化）。区分只能落到窗口标题：
#   `… - 豆包工作 - 豆包工作浏览器`  → 工作台/CDP 放标签、发消息的容器窗 → 后台（不进任务栏）
#   `… - 豆包工作`                  → 用户自己那扇主窗口         → 必须留在任务栏、正常显示
def _title_of(h):
    try:
        user32 = ctypes.windll.user32
        n = user32.GetWindowTextLengthW(h)
        if n <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(n + 2)
        user32.GetWindowTextW(h, buf, n + 2)
        return buf.value
    except Exception:  # noqa: BLE001
        return ""


def _is_browser(h):
    return _title_of(h).rstrip().endswith(BROWSER_SUFFIX)


def _browser_windows(only_visible=True):
    """只取「豆包工作浏览器」容器窗（后台那扇），主窗口一概不碰。"""
    return [h for h in _doubao_windows(only_visible) if _is_browser(h)]


def _restore_taskbar(h):
    """把用户自己的豆包主窗口放回任务栏（摘 TOOLWINDOW + 补 APPWINDOW）。

    单独成函数是为了守场线程每轮都能"纠正"：任何一次误加 TOOLWINDOW 都会在 2 秒内被摘掉，
    用户不会再出现"图标不见了 / 像被最小化"。

    2026-11-16（用户报「最小化魔改版豆包会自己拉起来抢焦点」）：这里原来只要
    `IsIconic` 就 `SW_RESTORE`，而守场线程每 2 秒跑一轮 —— 用户刚最小化，下一轮就被
    还原并置前。**只修样式位，绝不改显示状态**：最小化的窗口一律不碰，
    `SW_SHOWNOACTIVATE` 也只在没最小化时才用来刷新任务栏图标。
    """
    try:
        user32 = ctypes.windll.user32
        h = int(h)
        # 2026-11-16 实测：对最小化窗口调用 SetWindowLongW(GWL_EXSTYLE)，1.5 秒内它就会被
        # 唤醒并抢回前台（Chromium 收到样式变更会重新显示窗口）。所以最小化时**连样式都不改**，
        # 等窗口自己恢复后，下一轮再纠正样式位。
        if user32.IsIconic(h):
            return True
        ex = user32.GetWindowLongW(h, GWL_EXSTYLE)
        fixed = False
        if (ex & WS_EX_TOOLWINDOW) or not (ex & WS_EX_APPWINDOW):
            user32.SetWindowLongW(h, GWL_EXSTYLE,
                                  (ex & ~WS_EX_TOOLWINDOW) | WS_EX_APPWINDOW)
            fixed = True
        if fixed:
            user32.ShowWindow(h, SW_SHOWNOACTIVATE)   # 让任务栏重建图标，但不抢前台
        return True
    except Exception:  # noqa: BLE001
        return False


def _apply_tool(user32, h, sink=True):
    """加 TOOLWINDOW（去任务栏/Alt-Tab），默认把窗口**沉到所有窗口之下**。

    2026-11-15 第三次（用户截图：任务栏还剩一颗「豆包工作浏览器」蓝圈，而 CDP 恰恰是用
    那个窗口发消息的）：旧版这里除了加 TOOLWINDOW 还会把窗挪到 (-3200,-3200) 屏外，
    于是 CDP 认为"没有浏览器"，agent 自己补开一个新窗口、还抢焦点两次。现在一律**留在屏内**，
    只沉底 —— 可见、能渲染、CDP 能往里放标签，但不会挡在用户正在用的界面前面。

    遮挡冻结不用担心：这版豆包实例启动时本来就带
    `--disable-features=CalculateNativeWinOcclusion` 与 `--disable-backgrounding-occluded-windows`
    （见 doubao_cdp.mjs 的 launch 参数），被盖住也不会降速。
    """
    ex = user32.GetWindowLongW(h, GWL_EXSTYLE)
    if not (ex & WS_EX_TOOLWINDOW):
        user32.SetWindowLongW(h, GWL_EXSTYLE, (ex | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW)
    if sink:
        HWND_BOTTOM = 1
        user32.SetWindowPos(h, HWND_BOTTOM, 0, 0, 0, 0,
                            SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)
    return True


def hide_all():
    """把豆包放回「能看见、能点」的状态，**不再收窗**。

    2026-11-15 用户报「工作台跑着，豆包缩在后台，点击唤不起来」：
    旧做法（2026-10-07 立）是加 WS_EX_TOOLWINDOW 把窗口从任务栏/Alt-Tab 摘掉，
    再 SetWindowPos 挪到 (-3200,-3200)。`agent_bridge` 走的是 `icon=False`，
    于是既不建托盘图标、又把任务栏图标摘了 —— 用户手上没有任何入口能把它叫回来，
    这就是「点不动」的成因。

    现在只把**已经跑出屏幕、或被摘掉任务栏**的窗口放回屏幕内，不激活、不抢焦点：
    窗口留在任务栏里，用户随时点得回来，CDP 也照常用。
    """
    return _restore_visible(False)


def _restore_visible(activate=False):
    """把窗口挪回屏幕内并显示（屏外→屏内），默认**不抢焦点**。

    2026-11-15 用户要求「静默启动、只在后台，需要时再唤到前台」：窗口必须保持
    **可见**（最小化/隐藏会让 Chromium 判定遮挡、冻结渲染，HOME 那边出不了片就是
    这么来的），但绝不能把用户从正在用的窗口切走 —— 所以一律用 NOACTIVATE 系列，
    只有托盘点击 / 前端「打开豆包窗口」才真的置前。

    2026-11-15 第二次（用户截图：豆包窗口又回到任务栏、还挡在工作台前面）：
    **显示时不再摘掉 TOOLWINDOW**。TOOLWINDOW 只管「进不进任务栏 / Alt-Tab」，
    跟可见性无关 —— 保留它，窗口照样画得出来（Chromium 不冻结），但任务栏里
    再多也不会冒出那颗豆包图标。要见它就点托盘或前端那颗按钮。
    """
    _st["want_hidden"] = False
    # 唤回后这段时间别沉底，不然刚叫出来又被压下去。
    # activate=True（托盘/前端「打开豆包窗口」）给足 120 秒：用户要在里面看清、点按钮，
    # 15 秒不够，实测会出现"点了唤回、过一会儿又自己缩下去"。
    _st["wake_until"] = time.time() + (120 if activate else 15)
    n = 0
    try:
        user32 = ctypes.windll.user32
        for h in _browser_windows(True):          # 只唤「豆包工作浏览器」，主窗口一直摆在任务栏
            h = int(h)
            try:
                # 补上 TOOLWINDOW（原来只在 hide 时加）：显示后也不回任务栏。
                ex = user32.GetWindowLongW(h, GWL_EXSTYLE)
                if not (ex & WS_EX_TOOLWINDOW):
                    user32.SetWindowLongW(h, GWL_EXSTYLE, (ex | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW)
                if user32.IsIconic(h):                    # 最小化会冻结渲染，先还原
                    user32.ShowWindow(h, SW_RESTORE)
                user32.ShowWindow(h, SW_SHOWNOACTIVATE)   # 显示但不激活
                r = wintypes.RECT()
                if user32.GetWindowRect(h, ctypes.byref(r)) and r.left <= -3000:
                    user32.SetWindowPos(h, 0, 80, 80, 0, 0,
                                        SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
                if activate:
                    HWND_TOP = 0
                    user32.SetWindowPos(h, HWND_TOP, 0, 0, 0, 0,
                                        SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)
                    user32.SetForegroundWindow(h)
                n += 1
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
    return n


def show_all():
    """把豆包放出来并置前（`--visible` 手动确认那轮用；2026-11-15 后 hide 也一样放出来，只是不置前）。"""
    return _restore_visible(True)


def _taskbar_windows():
    """还"摆在用户面前"的豆包窗口（可见 + 没最小化 + 无 TOOLWINDOW + 无 owner）。

    2026-10-09：把 **IsIconic（最小化）也算作"用户看不见"**。此前只看 IsWindowVisible，
    用户自己最小化豆包后这里仍返回句柄，于是点托盘的判据 `if _taskbar_windows(): hide_all()`
    走了"收起来"的分支——用户点一下反而更看不见了，这就是"缩小到后台就打不开"的成因。
    """
    user32 = ctypes.windll.user32
    out = []
    for h in _doubao_windows(False):
        h = int(h)
        try:
            if not _is_browser(h):           # 用户主窗口不算"该收起来的豆包"，永不收
                continue
            if not user32.IsWindowVisible(h):
                continue
            if user32.IsIconic(h):          # 最小化 = 用户看不见 = 该弹出来，不算"还在任务栏摆着"
                continue
            # 2026-11-15：显示后也保留 TOOLWINDOW（见 _restore_visible），所以这里不能再要求
            # "无 TOOLWINDOW"，只看"可见 + 没最小化"——那才是用户真的能在桌面上看见。
            if user32.GetWindow(h, 4) == 0:
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
                # 2026-11-15：托盘点击只做「放出来」。旧逻辑是「已经在任务栏 → 收起来」，
                # 收进屏外就再也没入口了（用户报「点唤不起来」）。
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


def ensure_roles():
    """工作台一启动就叫：守场常驻，豆包两类窗口被持续纠正。

    - `… - 豆包工作 - 豆包工作浏览器`  → TOOLWINDOW + 沉底（后台，不进任务栏）
    - `… - 豆包工作`（用户主窗口）      → 摘 TOOLWINDOW + 补 APPWINDOW（回任务栏）
    """
    return _guard_start()


def _guard_start():
    """拉起常驻守场（幂等）。"""
    with _lock:
        if _st.get("guard"):
            return False
        _st["guard"] = True
        _st["guard_stop"] = False
    threading.Thread(target=_silent_guard, daemon=True, name="doubao-guard").start()
    return True


def _silent_guard():
    """静默守场：豆包后续再冒新窗（CDP newWindow / 重启）也补 TOOLWINDOW + 沉底，
    并把"新窗户刚抢走的前台"还给工作台。只调 Win32，不建 NotifyIcon、不闪任何 GUI。

    2026-11-15 第三次修：原来只在 `want_hidden` 为真时才跑，而 hide_async 早已不设那个
    标记，守卫等于从没启动过 —— 这就是「新开的豆包工作浏览器窗口照样挂在任务栏、还抢
    焦点」的成因。现在常驻（由 hide_async 拉起），且只抢回**新出现**窗口抢走的焦点：
    用户自己点豆包切过去时不会被抢回来。
    """
    seen = set()
    try:
        while not _st.get("guard_stop"):
            try:
                user32 = ctypes.windll.user32
                allw = [int(h) for h in _doubao_windows(True)]
                brs = set(h for h in allw if _is_browser(h))   # 只对「豆包工作浏览器」动手
                fresh = brs - seen
                wake = time.time() < float(_st.get("wake_until") or 0)
                for h in brs:
                    _apply_tool(user32, h, sink=not wake)
                for h in allw:
                    if h not in brs:
                        _restore_taskbar(h)                    # 用户主窗口：每轮纠正回任务栏
                if fresh and int(user32.GetForegroundWindow() or 0) in fresh:
                    focus_back()
                seen = brs
            except Exception:  # noqa: BLE001
                pass
            time.sleep(2.0)
    finally:
        _st["guard"] = False



def _yield_focus(seconds=8):
    """豆包刚起来那几秒会反复抢前台（Electron 显示窗口、页面加载完成都会 activate）。
    在这个启动窗口期内持续把焦点还给工作台，窗口期一过就停 —— 用户之后主动点豆包
    不会被抢回来。"""
    def _run():
        end = time.time() + max(1, int(seconds))
        while time.time() < end:
            try:
                focus_back()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1.0)
    threading.Thread(target=_run, daemon=True, name="doubao-yield-focus").start()


def hide_async(exe_path="", wait_secs=60, icon=False):
    """起个短命线程：等豆包窗口出现就把它**摆回屏幕内**（不藏、不摘任务栏、不抢焦点）。

    挂点：① agent_bridge 走豆包通道起一轮 ask 时；② wb_video prepare/confirm 后台任务启动时。
    --visible（跳转豆包界面手动模式）**不要调这个**——那扇窗就是要给用户看的。

    2026-11-15 用户报「豆包缩在后台，点击唤不起来」：此前这里会把窗口收进屏外并从
    任务栏摘掉，`icon=False` 时还没有托盘图标 —— 用户彻底没有入口。现在改成
    「只恢复可见、不抢前台」，窗口留在任务栏里全程可点。
    同日追加：用户要求「静默启动、只在后台」——窗口出来后 8 秒内持续把被抢的焦点
    还给工作台（详见 focus_back）。
    """
    def _job():
        deadline = time.time() + max(5, int(wait_secs))
        while time.time() < deadline:
            if _doubao_windows(True):
                hide_all()               # 现在是「摆回屏幕内」，语义见 hide_all
                if icon:
                    _ensure_icon(exe_path)
                _guard_start()           # 常驻守场：之后新冒的窗也不进任务栏、不抢焦点
                _yield_focus(20)         # CDP 开新窗会 activate，窗口期给足
                return
            time.sleep(0.6)
    threading.Thread(target=_job, daemon=True, name="doubao-tray-hide").start()
