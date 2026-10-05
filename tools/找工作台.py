# -*- coding: utf-8 -*-
r"""找工作台：这台机器上 HeronBo Workbench 装在哪（位置无关的探测，agent 直接跑它）。

为什么要有它
    **安装位置每台机器都不一样**（有装 `D:\`、有装 `F:\新建文件夹 (3)\` 的）。
    文档原来让 agent 自己探——「右键桌面快捷方式」+「查 HKCU 卸载键」——
    这两条在真实机器上都会断（有机器只建了开始菜单项、桌面是空的；Inno Setup 装到 HKLM
    且键名带 `{GUID}_is1` 后缀、不叫 HeronBo），agent 于是误报「本机尚未安装工作台」。
    `agent_bridge.py` 为「找豆包工作」早就用过「扫盘 + 注册表」这套（`_app_roots()` /
    `_glob_first()`，注释里记着 2026-09-16 为同类误判踩过一次）。这里把同一套思路用在
    工作台自己身上，并**把进程放在第一位**：进程的 exe 路径就是铁证，跟装在哪无关。

判据（从强到弱，命中即停）
    ⓪ 正在跑的进程：内核 = `score-tool.exe`；启动器 = `HeronBo.exe`（约 9KB）
    ① 监听默认端口 17979 的进程（源码运行 / 自定义端口时也认得出）
    ② 本机扫盘：常见根目录 + 各盘根，1~3 层通配找 `HeronBo.exe` / `score-tool.exe`
    ③ 便宜兜底：开始菜单快捷方式 → 卸载注册表键(HKLM + HKCU) → 桌面快捷方式

⚠️ **产品名 ≠ 进程名**：界面/产品叫「HeronBo Workbench」，但**进程映像名不叫这个**——
它是 `score-tool.exe`（内核，界面进程）和 `HeronBo.exe`（约 9KB 的启动器）。
只按「HeronBo Workbench」去进程列表或注册表**子键名**里精确匹配，必然找不到。

用法
    python 找工作台.py              # 人看的报告
    python 找工作台.py --json       # 机读（agent 用）
    python 找工作台.py --no-scan    # 跳过扫盘（很快：只查进程/端口/快捷方式/注册表）

退出码：0 = 找到了；1 = 没找到（并打印该让用户做什么）。
"""
import argparse
import ctypes
import glob
import json
import os
import subprocess
import sys

DEFAULT_PORT = 17979           # 与 workbench_server.py 的 DEFAULT_PORT 一致（固定端口，偏好才留得住）
LAUNCHER = "HeronBo.exe"       # 约 9KB 的启动器（安装目录根）
CORE = "score-tool.exe"        # 真正的工作台内核（安装目录 \skill\tools\dist\）
PRODUCT = "HeronBo Workbench"  # 只用于注册表 DisplayName / 快捷方式名的**模糊**匹配

# 扫盘时跳过的目录：系统目录 + 明显不可能装工作台的海量目录
SKIP_DIRS = {
    "windows", "winsxs", "servicing", "assembly", "$recycle.bin",
    "system volume information", "recovery", "perflogs",
    "node_modules", "__pycache__", ".git", "temp", "tmp",
}


def _say(m):
    print(m, flush=True)


def _no_window():
    return {"creationflags": 0x08000000} if os.name == "nt" else {}   # CREATE_NO_WINDOW


def _run(args, timeout=30):
    try:
        return subprocess.run(args, capture_output=True, text=True, errors="replace",
                              timeout=timeout, **_no_window()).stdout or ""
    except (OSError, subprocess.SubprocessError):
        return ""


# ── ⓪ 进程：路径就是铁证 ────────────────────────────────────────────────────
def procs():
    """[(pid, 映像名, exe 完整路径)]；拿不到路径的跳过。必须用完整路径才能定位安装目录。"""
    if os.name != "nt":
        return []
    try:
        k32 = ctypes.windll.kernel32
        psapi = ctypes.windll.psapi
        from ctypes import wintypes
    except (AttributeError, OSError):
        return []
    QUERY_LIMITED = 0x1000
    buf = (wintypes.DWORD * 8192)()
    need = wintypes.DWORD()
    if not psapi.EnumProcesses(ctypes.byref(buf), ctypes.sizeof(buf), ctypes.byref(need)):
        return []
    out = []
    for i in range(need.value // ctypes.sizeof(wintypes.DWORD)):
        pid = int(buf[i])
        if not pid:
            continue
        h = k32.OpenProcess(QUERY_LIMITED, False, pid)
        if not h:
            continue
        try:
            size = wintypes.DWORD(32768)
            p = ctypes.create_unicode_buffer(size.value)
            if k32.QueryFullProcessImageNameW(h, 0, p, ctypes.byref(size)):
                path = p.value or ""
                out.append((pid, os.path.basename(path), path))
        finally:
            k32.CloseHandle(h)
    return out


def from_process():
    hits = [(pid, name, path) for pid, name, path in procs()
            if name.lower() in (CORE.lower(), LAUNCHER.lower())]
    if not hits:
        return None
    hits.sort(key=lambda r: 0 if r[1].lower() == CORE.lower() else 1)
    pid, name, path = hits[0]
    return {"by": "⓪ 正在运行的进程", "detail": "%s (PID %d) → %s" % (name, pid, path),
            "exe": path, "running": True}


# ── ① 监听端口：源码运行 / 自定义端口也认得出 ────────────────────────────────
def from_port(port=DEFAULT_PORT):
    out = _run(["netstat", "-ano", "-p", "TCP"])
    pids = []
    for line in out.splitlines():
        f = line.split()
        if len(f) >= 5 and f[0].upper() == "TCP" and f[3].upper() == "LISTENING":
            if f[1].rsplit(":", 1)[-1] == str(port):
                try:
                    pids.append(int(f[4]))
                except ValueError:
                    pass
    if not pids:
        return None
    table = {pid: (name, path) for pid, name, path in procs()}
    for pid in pids:
        name, path = table.get(pid, ("", ""))
        if path:
            return {"by": "① 监听端口 %d 的进程" % port,
                    "detail": "%s (PID %d) → %s" % (name or "?", pid, path),
                    "exe": path, "running": True}
    return {"by": "① 监听端口 %d 的进程" % port,
            "detail": "PID %s 在监听，但拿不到 exe 路径" % ",".join(map(str, pids)),
            "exe": "", "running": True}


# ── ② 扫盘：常见根目录 + 各盘根，1~3 层通配 ─────────────────────────────────
def app_roots():
    """可能装着桌面应用的根目录。

    与 agent_bridge._app_roots() 同口径。**系统盘放最后**：C:\\ 一层就几十万项，
    先去非系统盘（工作台这类国内工具常被装到 D:/F:）能用更少的预算命中。
    盘根写死成 D/E/F/G 是因为真机上出现过 `F:\\新建文件夹 (3)\\ZCode\\` 这种
    多一层的形状（2026-09-16 踩过），不能只按厂商名猜。
    """
    roots = []
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                 os.environ.get("ProgramW6432"),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"),
                 "D:\\", "E:\\", "F:\\", "G:\\", "C:\\"):
        if base and os.path.isdir(base) and base not in roots:
            roots.append(base)
    return roots


def _walk_hits(root, want, max_depth=3, budget=250000):
    """在 root 下最多 max_depth 层找名字在 want 里的文件；budget 是本根的目录项上限。

    用显式栈而不是 os.walk，是为了能在预算耗尽时**干净地停下**——扫盘最怕在
    超大分区上卡住，宁可漏也不能挂。
    """
    found, stack = [], [(root, 0)]
    while stack and budget > 0:
        d, depth = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    budget -= 1
                    if budget <= 0:
                        break
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if depth + 1 <= max_depth and e.name.lower() not in SKIP_DIRS:
                                stack.append((e.path, depth + 1))
                        elif e.name.lower() in want:
                            found.append(e.path)
                    except OSError:
                        continue
        except OSError:
            continue
    return found


def from_scan(max_depth=3):
    want = {LAUNCHER.lower(), CORE.lower()}
    launchers, cores = [], []
    for r in app_roots():
        for p in _walk_hits(r, want, max_depth=max_depth):
            (launchers if os.path.basename(p).lower() == LAUNCHER.lower() else cores).append(p)
    if launchers:
        p = sorted(launchers, key=len)[0]
        return {"by": "② 本机扫盘", "detail": "扫到启动器 %s" % p, "exe": p, "running": False}
    if cores:
        p = sorted(cores, key=len)[0]
        return {"by": "② 本机扫盘",
                "detail": "扫到内核 %s（没见启动器，可能是源码 / 绿色副本）" % p,
                "exe": p, "running": False}
    return None


# ── ③ 便宜的兜底：开始菜单 / 注册表 / 桌面 ───────────────────────────────────
def _shortcuts_via_powershell():
    """一次 PowerShell 读完开始菜单 + 桌面的 HeronBo*.lnk（含目标），返回 [(lnk, target)]。

    走 WScript.Shell 而不是自己解 .lnk 结构：目标可能带环境变量、可能被 OneDrive 重定向，
    交给系统自己的解析器最省事。
    """
    dirs = [
        r"$env:ProgramData\Microsoft\Windows\Start Menu\Programs",
        r"$env:APPDATA\Microsoft\Windows\Start Menu\Programs",
        r"$env:USERPROFILE\Desktop",
        r"$env:PUBLIC\Desktop",
    ]
    ps = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        "$sh=New-Object -ComObject WScript.Shell;"
        "foreach($d in @(%s)){"
        "  if(Test-Path $d){"
        "    Get-ChildItem $d -Recurse -Filter *.lnk -ErrorAction SilentlyContinue |"
        "      Where-Object { $_.Name -like '*HeronBo*' } |"
        "      ForEach-Object { $t=$sh.CreateShortcut($_.FullName).TargetPath;"
        "                      Write-Output ($_.FullName + '|' + $t) } } }"
    ) % ",".join('"%s"' % d for d in dirs)
    out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps])
    rows = []
    for line in out.splitlines():
        if "|" in line:
            lnk, _, target = line.partition("|")
            rows.append((lnk.strip(), target.strip()))
    return rows


def from_shortcut():
    """开始菜单 + 桌面里的 .lnk。开始菜单优先级最高（桌面常常压根没有）。"""
    if os.name != "nt":
        return None
    for lnk, target in _shortcuts_via_powershell():
        low = target.lower()
        if low.endswith(LAUNCHER.lower()) or low.endswith(CORE.lower()):
            return {"by": "③ 快捷方式", "detail": "%s → %s" % (lnk, target),
                    "exe": target, "running": False}
    return None


def from_registry():
    """卸载注册表键：**HKLM 与 HKCU 都要查**，而且按 DisplayName **模糊**匹配。

    安装器是 Inno Setup → 子键名形如 `{8F3A2C71-…}_is1`，**不叫 HeronBo**；
    管理员安装写进 HKLM。只查 HKCU 或按子键名精确匹配，都会漏。
    """
    if os.name != "nt":
        return None
    try:
        import winreg
    except ImportError:
        return None
    places = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for hive, sub in places:
        try:
            with winreg.OpenKey(hive, sub) as k:
                n = winreg.QueryInfoKey(k)[0]
        except OSError:
            continue
        for i in range(n):
            try:
                name = winreg.EnumKey(k, i)
            except OSError:
                continue
            try:
                with winreg.OpenKey(hive, sub + "\\" + name) as sub_k:
                    def _v(key):
                        try:
                            return str(winreg.QueryValueEx(sub_k, key)[0])
                        except OSError:
                            return ""
                    disp = _v("DisplayName")
                    if PRODUCT.lower() not in disp.lower():
                        continue
                    loc = _v("InstallLocation")
                    icon = _v("DisplayIcon").strip('"')
                    exe = icon.split(",")[0] if icon else ""
                    if not exe and loc:
                        exe = os.path.join(loc, LAUNCHER)
                    return {"by": "③ 卸载注册表键",
                            "detail": "%s\\%s  DisplayName=%s" % (sub, name, disp),
                            "exe": exe, "version": disp, "running": False}
            except OSError:
                continue
    return None


# ── 组装结果 ────────────────────────────────────────────────────────────────
def install_dir_of(exe):
    """从 exe 路径推安装目录：启动器就在根；内核在 \\skill\\tools\\dist\\。"""
    if not exe:
        return ""
    p = os.path.abspath(exe)
    d = os.path.dirname(p)
    if os.path.basename(p).lower() == CORE.lower():
        for _ in range(3):                       # dist → tools → skill → 安装目录
            d = os.path.dirname(d)
    return d


def core_exe(app_dir):
    if not app_dir:
        return ""
    c = os.path.join(app_dir, "skill", "tools", "dist", CORE)
    return c if os.path.isfile(c) else ""


def _version_json(app_dir):
    for rel in (os.path.join("skill", "tools", "_version.json"),
                os.path.join("skill", "tools", "dist", "version.json")):
        p = os.path.join(app_dir, rel)
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as f:
                    return str(json.load(f).get("version") or "")
            except (OSError, ValueError):
                pass
    return ""


def finish(hit):
    exe = hit.get("exe") or ""
    app = install_dir_of(exe)
    hit["appDir"] = app
    hit["core"] = core_exe(app)
    if not hit.get("version") and app:
        v = _version_json(app)
        if v:
            hit["version"] = v
    return hit


def detect(scan=True):
    for fn in (from_process, from_port, from_shortcut, from_registry):
        try:
            hit = fn()
        except Exception as e:                                       # noqa: BLE001
            hit = None
            if os.environ.get("HERONBO_DEBUG"):
                _say("  [debug] %s 抛异常：%r" % (getattr(fn, "__name__", fn), e))
        if hit:
            return finish(hit)
    if scan:
        hit = from_scan()
        if hit:
            return finish(hit)
    return None


def main():
    ap = argparse.ArgumentParser(description="找工作台：位置无关地探测 HeronBo Workbench 装在哪")
    ap.add_argument("--json", action="store_true", help="输出 JSON（agent 用）")
    ap.add_argument("--no-scan", action="store_true", help="跳过扫盘（很快）")
    ap.add_argument("--scan-only", action="store_true", help="只扫盘")
    a = ap.parse_args()

    if a.scan_only:
        hit = from_scan()
        hit = finish(hit) if hit else None
    else:
        hit = detect(scan=not a.no_scan)

    if a.json:
        print(json.dumps({"ok": bool(hit), "result": hit}, ensure_ascii=False, indent=2))
        return 0 if hit else 1

    if not hit:
        _say("没找到工作台。")
        _say("")
        _say("按顺序补一下：")
        _say("  1) 确认这台机器真的装过：到")
        _say("     https://github.com/heronbo111/HeronBo-AIGC-Prompt/releases/latest")
        _say("     下 HeronBo-Workbench-Setup 装一份，再回来重跑本脚本。")
        _say("  2) 装在非默认位置也认得出：本脚本查 进程 → 端口 %d → 扫各盘根，"
             % DEFAULT_PORT)
        _say("     不需要你告诉它路径；只要它在跑，⓪ 那一路必中。")
        _say("  3) 别用「右键桌面快捷方式」当唯一判据——有机器只建开始菜单项、桌面是空的；")
        _say("     也别只查 HKCU——管理员安装写进 HKLM，键名还带 {GUID}_is1 后缀。")
        return 1

    launcher = hit["exe"] if hit["exe"].lower().endswith(LAUNCHER.lower()) else \
        os.path.join(hit.get("appDir") or "", LAUNCHER)
    _say("工作台安装位置：%s" % (hit.get("appDir") or "(推不出来，只拿到 exe)"))
    _say("  启动器 exe ：%s" % launcher)
    _say("  内核 exe   ：%s" % (hit.get("core") or "(没找到 skill\\tools\\dist\\%s)" % CORE))
    _say("  判据       ：%s" % hit["by"])
    _say("  明细       ：%s" % hit["detail"])
    if hit.get("version"):
        _say("  版本       ：%s" % hit["version"])
    _say("  当前状态   ：%s" % ("正在运行" if hit.get("running") else "没在跑"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
