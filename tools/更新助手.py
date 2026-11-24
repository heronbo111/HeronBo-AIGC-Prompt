# -*- coding: utf-8 -*-
r"""更新助手：等工作台退出 → 换上新的工作台 → 再把它启起来。

由工作台**自己**以 `--update-helper` 参数跑起来（见 score_gui.pyw / 工作台.py 最前面那段），
人不用手敲：

    <工作台exe> --update-helper --dir <工作台目录> --src <新下载的文件> --pid <旧进程号> \
                                 [--mode apply|rollback] [--keep 2] [--timeout 60]

为什么不写成 .cmd（2026-09-22 实测，写下来免得下次又踩）：
  助手必须"脱离主程序、等它退出后再动手"，而在 Windows 上分离出来的 cmd 没有控制台——
  `timeout /t` 会卡死不返回；改用 ping 做延时后，实测仍会停在"等进程退出"那一步
  （管道 + 分离窗口这套组合不可靠：.new 与 .cmd 都留在盘上，一直不换位）。
  用 Python 做同一件事没有这些坑，而且**用的是工作台 exe 里自带的解释器**，
  装机端不需要另装 Python。

安全边界：只在"文件确实存在、新文件校验过、目标目录可写"时动手；超时（默认 60 秒）
就不换、把新文件删掉收场，**旧版本永远原样留着**（换坏了还能回滚）。
"""
import argparse
import ctypes
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

LOG_NAME = "score-tool.update.log"
KEEP_DEFAULT = 2
INTERNAL = "_internal"          # onedir 的依赖目录（2026-10-13）
# Windows 下目录/文件被占住或权限不足时，OS 给的错误号：5=拒绝访问，32=正被占用。
# 这类多半是瞬时锁（杀软实时扫描、残留子进程、句柄还没释放），隔一下重来是安全的。
_LOCK_WINERRORS = (5, 32)
_RETRY_ATTEMPTS = 5
_RETRY_WAIT = 0.7
CREATE_NO_WINDOW = 0x08000000     # 子进程不闪命令行窗


def _is_lock_err(e):
    w = getattr(e, "winerror", None)
    if w in _LOCK_WINERRORS:
        return True
    return getattr(e, "errno", None) in (13, 26, 32)


def _human_err(e):
    """把系统错误翻成大白话（界面文案纪律：不堆错误号）。"""
    w = getattr(e, "winerror", None)
    if w == 5:
        return "安装目录被后台程序占用，或当前账号没有写入权限"
    if w == 32:
        return "程序文件正被其他程序占用"
    return str(e)


def _rename_retry(src, dst, label, dir_move=False):
    """同盘改名/移动，占用或拒绝访问时隔一下重试。

    2026-11-17 实测 0.7→0.73：旧 `_internal` 备份到 `_old` 时 WinError 5——画布 node
    残留进程把目录当工作目录占住、或杀软正压在文件上。单步一次失败就整体回滚太脆，
    这里先原地重试几轮；仍不行再抛给上层回滚。
    """
    last = None
    op = os.rename if dir_move else os.replace
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            op(src, dst)
            return
        except OSError as e:
            last = e
            if not _is_lock_err(e) or attempt == _RETRY_ATTEMPTS:
                raise
            time.sleep(_RETRY_WAIT)
            log(os.path.dirname(os.path.dirname(dst)) if dir_move else os.path.dirname(dst),
                "retry %s (%d/%d): %s" % (label, attempt, _RETRY_ATTEMPTS, e))
    if last:
        raise last


def _kill_holders(d):
    """摊入前清场：把占着安装目录的残留进程收掉。

    2026-11-17 实测 0.7→0.73：画布 node 的工作目录就在 `_internal\\canvas` 下，主程序
    退出后它还在。Windows 不允许把「被进程当成工作目录」的目录树改名，备份旧 `_internal`
    那一步直接 WinError 5（拒绝访问），_rename_retry 重试 3.5 秒也等不到它自己退——
    现场表现就是「更新没完成：摊入失败」。

    两个坑，都踩过：
      * 匹配条件里写死路径，命令行本身含这个路径 → 把自己（powershell 子进程）也命中。
        所以路径只走环境变量，命令行里不出现；
      * 再显式排除 powershell / 当前进程，双保险。
    只动自家进程：exe 或命令行落在安装目录下，或路径带 HeronBo 的 node（工作台自带运行时）。
    用户全局装的 node（Program Files 那种）不动。
    """
    try:
        p = os.path.abspath(d).rstrip("\\")
        script = (
            "$d = $env:HB_UPD_DIR; $self = [int]$env:HB_UPD_PID; "
            "Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | "
            "Where-Object { "
            "  $_.ProcessId -ne $self -and $_.ProcessId -ne 0 "
            "  -and $_.Name -ne 'powershell.exe' -and $_.Name -ne 'pwsh.exe' -and ("
            "    ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($d)) -or "
            "    ($_.CommandLine -and $_.CommandLine.IndexOf($d, [StringComparison]::OrdinalIgnoreCase) -ge 0) -or "
            "    ($_.Name -eq 'node.exe' -and $_.ExecutablePath -like '*HeronBo*')"
            "  )"
            "} | ForEach-Object { "
            "  try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {} "
            "}"
        )
        env = dict(os.environ)
        env["HB_UPD_DIR"] = p
        env["HB_UPD_PID"] = str(os.getpid())
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True, timeout=30, creationflags=CREATE_NO_WINDOW, env=env)
        log(d, "摊入前清场完成（安装目录 %s）" % d)
        return True
    except Exception as e:  # noqa: BLE001
        log(d, "摊入前清场没做成（不影响继续）：%s" % e)
        return False


def _backup_ts(d, ts=None):
    """给 _old 备份目录一个不会撞车的名字。

    同一秒内连续两次换位（更新失败立刻重试、或更新后马上回滚）时，只到秒的时间戳会让
    第二次写进第一次的备份目录，把旧 exe/_internal 覆盖成半新半旧；同名就加 -01/-02。
    """
    if ts:
        return ts
    base = time.strftime("%Y%m%d-%H%M%S")
    out = base
    n = 1
    root = os.path.join(d, "_old")
    while os.path.exists(os.path.join(root, out)):
        out = "%s-%02d" % (base, n)
        n += 1
    return out


def log(d, msg):
    r"""给排障留一份轨迹。

    2026-10-21：改成 **UTF-8**。原来写 ascii + errors=replace，中文路径全变成 `?`——
    实踩排查「更新后快捷方式找不到 exe」时，日志里的路径完全不可读（`D:\AI??\???????`），
    白抓半天。UTF-8 在 Win10+ 记事本/VS Code 里都能正常显示。
    """
    try:
        p = os.path.join(d, LOG_NAME)
        with open(p, "a", encoding="utf-8", errors="replace", newline="\n") as f:
            f.write("[%s] %s\n" % (time.strftime("%Y%m%d-%H%M%S"), msg))
    except OSError:
        pass


def _alert(msg):
    """失败时给用户一个**看得见**的提示。

    2026-11-16 用户实测 0.7→0.71：更新完软件自己关掉、不重启、重开还是旧版，
    全程没有一句提示——因为失败分支只写了日志（日志他不会去翻）。更新跑在后台，
    静默失败等于"点了没反应"，必须弹出来。
    """
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None, str(msg), "HeronBo 工作台 · 更新没完成", 0x00000040 | 0x00010000)
    except Exception:  # noqa: BLE001
        pass


def tidy_old(d, keep=KEEP_DEFAULT):
    """只留最近 N 份旧备份。两种形态都清：老的单文件 exe、新的 _old\\<时间戳>\\ 目录。"""
    try:
        fs = []
        for pat in ("score-tool_旧_*.exe", "score-tool_old_*.exe"):
            fs += glob.glob(os.path.join(d, pat))
        fs = sorted(set(fs), key=lambda p: os.path.getmtime(p))
        for old in (fs[:-keep] if len(fs) > keep else []):
            try:
                os.remove(old)
            except OSError:
                pass
    except OSError:
        pass
    try:
        root = os.path.join(d, "_old")
        subs = sorted(x for x in os.listdir(root) if os.path.isdir(os.path.join(root, x)))
        for x in (subs[:-keep] if len(subs) > keep else []):
            shutil.rmtree(os.path.join(root, x), ignore_errors=True)
    except OSError:
        pass


def apply_pkg(d, src_dir, keep=KEEP_DEFAULT, ts=None, move_src=True):
    """onedir 换位：把 src_dir 里的 score-tool.exe 与 _internal\\ 摊到 d，旧的挪进 d\\_old\\<ts>\\。

    move_src=True 用于「装新版」（src 是解压出来的临时目录，摊完即弃）；
    move_src=False 用于「回滚」（src 是 _old\\<ts>\\ 里的备份，要原地留着）。

    ⚠️ 2026-10-21：**任何一步失败都必须回滚**。用户同事真机实踩——换位中途失败（杀软/占用），
    旧 exe 已被挪进 _old、新的没放上，桌面快捷方式直接报「找不到该应用程序」。
    现在失败时把旧 exe/_internal 原样挪回（新放上的先删掉），最坏也是"更新没成功、旧版还在"。
    """
    exe_new = os.path.join(src_dir, "score-tool.exe")
    if not os.path.isfile(exe_new):
        return False, "新包里没有 score-tool.exe（%s）" % src_dir
    ts = _backup_ts(d, ts)
    bak = os.path.join(d, "_old", ts)
    cur_exe = os.path.join(d, "score-tool.exe")
    cur_int = os.path.join(d, INTERNAL)
    new_int = os.path.join(src_dir, INTERNAL)
    tmp_exe = cur_exe + ".new"
    tmp_int = cur_int + ".new"
    moved_exe = moved_int = False

    def _clean_side():
        """旁路残留清掉（.new 是这一轮没换成的半成品，留着会让下次误判）。"""
        for p in (tmp_exe,):
            try:
                if os.path.isfile(p):
                    os.remove(p)
            except OSError:
                pass
        if os.path.isdir(tmp_int):
            shutil.rmtree(tmp_int, ignore_errors=True)

    def _rollback():
        """把旧 exe/_internal 挪回原位（新放上的先删/挪掉）。"""
        try:
            b_exe = os.path.join(bak, "score-tool.exe")
            if moved_exe and os.path.isfile(b_exe):
                if os.path.isfile(cur_exe):
                    try:
                        os.remove(cur_exe)
                    except OSError:
                        pass
                _rename_retry(b_exe, cur_exe, "回滚 exe", dir_move=False)
            b_int = os.path.join(bak, INTERNAL)
            if moved_int and os.path.isdir(b_int):
                if os.path.isdir(cur_int):
                    shutil.rmtree(cur_int, ignore_errors=True)
                _rename_retry(b_int, cur_int, "回滚 _internal", dir_move=True)
            _clean_side()
            log(d, "apply_pkg 失败已回滚（旧文件都在原位；备份目录 %s）" % bak)
        except OSError as e2:
            log(d, "!! 回滚也失败：%s（旧文件在 %s，请手动挪回）" % (e2, bak))

    # ⚠️ 2026-11-16：**目录不能再 shutil.move**。
    # move 目录在 Windows 上不是原子操作——_internal 里只要有一个文件被占用
    # （杀软实时扫描 / 旧进程没退干净），它会搬一半就抛错，留下"旧目录残缺 + 新目录残缺"，
    # 回滚也可能跟着失败。用户那台 0.6.8 在线更新后正是这个现场：软件自己关掉、
    # 再打开起不来。改成 **先完整复制到旁路 .new，再靠两次同盘 rename 切换**：
    # rename 是原子的，要么整块换过去，要么一个字节都不动。
    # 2026-11-17：动手前先把占着这个目录的残留进程（画布 node 等）收掉，
    # 否则备份旧 _internal 那一步必然被拒（WinError 5），怎么重试都没用。
    _kill_holders(d)
    try:
        # 1) 新件先落到旁路（此步失败时旧件完全没动）
        if os.path.isfile(tmp_exe):
            os.remove(tmp_exe)
        shutil.copy2(exe_new, tmp_exe)
        if os.path.isdir(new_int):
            if os.path.isdir(tmp_int):
                shutil.rmtree(tmp_int, ignore_errors=True)
            shutil.copytree(new_int, tmp_int)
        # 2) 备份旧件（同盘 rename；占用/拒绝访问先原地重试几轮）
        if os.path.isfile(cur_exe) or os.path.isdir(cur_int):
            os.makedirs(bak, exist_ok=True)
            if os.path.isfile(cur_exe):
                _rename_retry(cur_exe, os.path.join(bak, "score-tool.exe"),
                              "备份旧 exe", dir_move=False)
                moved_exe = True
            if os.path.isdir(cur_int):
                _rename_retry(cur_int, os.path.join(bak, INTERNAL),
                              "备份旧 _internal", dir_move=True)
                moved_int = True
        # 3) 新件到位（同盘 rename）
        _rename_retry(tmp_exe, cur_exe, "新版 exe 就位", dir_move=False)
        if os.path.isdir(tmp_int):
            _rename_retry(tmp_int, cur_int, "新版 _internal 就位", dir_move=True)
    except OSError as e:
        _rollback()
        return False, "摊入失败（%s）" % _human_err(e)
    _clean_side()
    log(d, "pkg applied: src=%s backup=%s" % (os.path.basename(src_dir), os.path.basename(bak)))
    tidy_old(d, keep)
    return True, ""


def wait_gone(pid, timeout=60.0, step=0.4):
    """等某个进程结束。返回 True＝已经退出（或打不开它＝本来就不在）。

    用 ctypes 的 OpenProcess + WaitForSingleObject，**不走 tasklist/管道**
    （本机实测那条路在分离窗口里会卡住）。
    """
    try:
        pid = int(pid or 0)
    except (TypeError, ValueError):
        return True
    if pid <= 0:
        return True
    if os.name != "nt":
        return True
    SYNCHRONIZE = 0x00100000
    WAIT_OBJECT_0 = 0x00000000
    WAIT_FAILED = 0xFFFFFFFF
    k = ctypes.windll.kernel32
    h = k.OpenProcess(SYNCHRONIZE, False, pid)
    if not h:
        return True                      # 打不开＝进程已经不在（或权限不允许，视为已走）
    try:
        t0 = time.time()
        while time.time() - t0 < timeout:
            r = k.WaitForSingleObject(h, int(step * 1000))
            if r == WAIT_OBJECT_0 or r == WAIT_FAILED:
                return True
        return False
    finally:
        try:
            k.CloseHandle(h)
        except OSError:
            pass


def backups(d):
    """旧备份清单（两种命名都算：开发机 换exe.py 的中文名、自更新的 ASCII 名）。"""
    fs = []
    for pat in ("score-tool_旧_*.exe", "score-tool_old_*.exe"):
        fs += glob.glob(os.path.join(d, pat))
    return sorted(set(fs), key=lambda p: os.path.getmtime(p))


def tidy(d, keep=KEEP_DEFAULT):
    """只留最近 N 个旧备份（对齐 换exe.py 的 KEEP_OLD）。"""
    fs = backups(d)
    for old in (fs[:-keep] if len(fs) > keep else []):
        try:
            os.remove(old)
        except OSError:
            pass


def swap(d, src, keep=KEEP_DEFAULT, ts=None):
    """换位：现役 exe 改名成旧备份 → 新文件顶上来。返回 (ok, 说明)。

    Windows 上**改名一个正在运行的 exe 是允许的**（删除不行）——不过我们一般已经等到
    旧进程退出了，这一步只是兜底。
    """
    exe = os.path.join(d, "score-tool.exe")
    srcp = src if os.path.isabs(src) else os.path.join(d, src)
    if not os.path.isfile(srcp):
        return False, "没有新文件：%s" % srcp
    ts = ts or time.strftime("%Y%m%d-%H%M%S")
    old = os.path.join(d, "score-tool_old_%s.exe" % ts)
    if os.path.isfile(exe):
        try:
            os.replace(exe, old)                     # 同盘原子改名
        except OSError as e:
            return False, "旧版改名失败（%s）" % e
        log(d, "renamed exe -> %s" % os.path.basename(old))
    try:
        os.replace(srcp, exe)
    except OSError as e:
        # 顶不上就把旧版还回去，别留个没有 exe 的目录
        try:
            if os.path.isfile(old) and not os.path.isfile(exe):
                os.replace(old, exe)
        except OSError:
            pass
        return False, "新版就位失败（%s）" % e
    log(d, "swapped ok: %s -> score-tool.exe" % os.path.basename(srcp))
    tidy(d, keep)
    return True, ""


def start(d, exe=None):
    """把新工作台启起来（脱离本进程，用户看到窗口自己回来）。返回启动的 pid（失败 0）。"""
    exe = exe or os.path.join(d, "score-tool.exe")
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200     # DETACHED | NEW_PROCESS_GROUP
    try:
        p = subprocess.Popen([exe], cwd=d, **kw)
        log(d, "started %s (pid=%d)" % (os.path.basename(exe), p.pid))
        return p.pid
    except OSError as e:
        log(d, "start failed: %s" % e)
        return 0


def alive(pid, timeout=3.0):
    """等 timeout 秒再看进程还活着吗——活着=True（第一次自启动能不能站住，就在这判断）。"""
    if pid <= 0:
        return False
    if os.name != "nt":
        return True
    SYNCHRONIZE = 0x00100000
    k = ctypes.windll.kernel32
    time.sleep(timeout)
    h = k.OpenProcess(SYNCHRONIZE, False, int(pid))
    if not h:
        return False
    try:
        k.CloseHandle(h)
        return True
    except OSError:
        return False


def clean_stale_mei(max_age_hours=24.0):
    """把 %TEMP% 里断头解包留下的 _MEI* 清掉（2026-10-06 加）。

    来源：工作台被 taskkill（「中止」按钮）/ 更新换位等异常退出时，PyInstaller 的
    onefile 解包目录删不掉，一次就漏一个（新机上实测一晚上攒了 8 个）。只动
    **24 小时前**的目录——正在跑的实例最多也就开几个钟头，不会误删活人的解包。
    """
    tmp = os.environ.get("TEMP") or os.environ.get("TMP")
    if not tmp or not os.path.isdir(tmp):
        return 0
    n = 0
    cutoff = time.time() - max_age_hours * 3600
    for p in glob.glob(os.path.join(tmp, "_MEI*")):
        try:
            if os.path.isdir(p) and os.path.getmtime(p) < cutoff:
                shutil.rmtree(p, ignore_errors=True)
                n += 1
        except OSError:
            pass
    return n


def main(argv=None):
    ap = argparse.ArgumentParser(description="工作台更新助手（由工作台自己拉起）")
    # 2026-10-21：**中文路径必须走 --args-file**。exe 经 PyInstaller bootloader 解析的
    # 命令行参数在中文路径下会乱码（实测 `D:\AI开发\…` 进助手变成 `D:\AI寮€鍙慭…`）——
    # 助手找不到更新包，换位直接失败（更早的版本还会在失败时把 exe 挪走不挪回）。
    ap.add_argument("--args-file", help="参数文件（JSON/UTF-8）：{dir,src,pid,mode,keep,timeout}")
    ap.add_argument("--dir", help="工作台所在目录")
    ap.add_argument("--src", help="新下载好的文件（相对 --dir 或绝对路径）；--mode restart 时不需要")
    ap.add_argument("--pid", type=int, default=0, help="旧工作台的进程号（等它退出）")
    ap.add_argument("--mode", default="apply", choices=["apply", "rollback", "restart"])
    ap.add_argument("--keep", type=int, default=KEEP_DEFAULT)
    ap.add_argument("--timeout", type=float, default=60.0)
    a = ap.parse_args(argv)

    if a.args_file:
        try:
            with open(a.args_file, encoding="utf-8") as f:
                cfg = json.load(f)
            for k in ("dir", "src", "pid", "mode", "keep", "timeout"):
                if k in cfg and cfg.get(k) not in (None, ""):
                    setattr(a, k, cfg[k])
            try:
                os.remove(a.args_file)
            except OSError:
                pass
        except (OSError, ValueError) as e:
            print("读不了参数文件 %s：%s" % (a.args_file, e), file=sys.stderr)
            return 2
    if not a.dir:
        print("缺 --dir（或 --args-file 里的 dir）", file=sys.stderr)
        return 2

    d = os.path.abspath(a.dir)
    log(d, "helper start mode=%s pid=%s src=%s" % (a.mode, a.pid, a.src))
    if not wait_gone(a.pid, timeout=a.timeout):
        srcp = a.src if os.path.isabs(a.src) else os.path.join(d, a.src)
        try:
            if os.path.isfile(srcp):
                os.remove(srcp)                      # 超时就别留半个状态，下次重来
                log(d, "giveup: app still running after %.0fs, removed new file" % a.timeout)
        except OSError:
            pass
        return 1
    # 主进程刚退出，Python DLL / pyd 的内存映射可能还没被 OS 回收；马上改名 _internal
    # 容易撞上瞬时占用。先静置一小段，给句柄释放和杀软扫描留出窗口。
    time.sleep(0.8)
    # 三种 src 形态都吃（2026-10-13 onedir 化）：
    #   · .zip  → 新版 onedir 整包：解压 → 摊入（exe + _internal）
    #   · 目录   → 回滚（_old\<ts>\）或已解压的包：直接摊入
    #   · 文件   → 老的单文件 exe（兼容期）
    if (a.mode or "") == "restart":
        # 纯重启（2026-10-16）：**不换任何文件**，等旧实例退干净 → 起新的。
        # 用途：界面里「切换 agent 通道」后让工作台以新通道重新搭桥（重启最干净）；
        # 走的是同一个助手（等退出 / 静置防杀软 / 起不来自动重试一次，这段逻辑全复用）。
        clean_stale_mei()
        time.sleep(2.0)
        for i in range(3):
            pid = start(d)
            if alive(pid, timeout=5.0):
                break
            log(d, "restart: attempt %d died (pid=%s)" % (i + 1, pid))
            if i < 2:
                time.sleep(2.0 + i * 2.0)
        return 0
    srcp = a.src if os.path.isabs(a.src) else os.path.join(d, a.src)
    if os.path.isfile(srcp) and srcp.lower().endswith(".zip"):
        tmp = tempfile.mkdtemp(prefix="heronbo_pkg_")
        try:
            with zipfile.ZipFile(srcp) as z:
                for n in z.namelist():                 # 防目录穿越：只解常规相对路径
                    if os.path.isabs(n) or ".." in n.replace("\\", "/").split("/"):
                        return 3, "包里路径不合法：%s" % n
                z.extractall(tmp)
            root = tmp
            if not os.path.isfile(os.path.join(root, "score-tool.exe")):
                for x in os.listdir(root):             # 兼容"zip 里套了一层同名目录"
                    if os.path.isdir(os.path.join(root, x)) \
                            and os.path.isfile(os.path.join(root, x, "score-tool.exe")):
                        root = os.path.join(root, x)
                        break
            ok, why = False, ""
            # 2026-11-16：杀软的实时扫描是**瞬时**的，换位时被占一下很常见。
            # 失败已经完整回滚（旧件在原位），所以隔一下重来一次是安全的。
            for _attempt in range(2):
                ok, why = apply_pkg(d, root, keep=a.keep)
                if ok:
                    break
                log(d, "apply attempt %d failed: %s" % (_attempt + 1, why))
                if _attempt == 0:
                    time.sleep(1.5)
        except zipfile.BadZipFile:
            ok, why = False, "下载的更新包解不开（可能没下完）"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        try:
            os.remove(srcp)                            # 包已摊入，原件不留
        except OSError:
            pass
    elif os.path.isdir(srcp):
        ok, why = apply_pkg(d, srcp, keep=a.keep, move_src=(a.mode == "apply"))
    else:
        ok, why = swap(d, a.src, keep=a.keep)
    if not ok:
        log(d, "swap failed: %s" % why)
        _alert("更新没有装上：%s\n\n旧版还在，可以直接打开工作台继续用。\n"
               "详细记录在同目录的 %s。" % (why, LOG_NAME))
        return 2
    # 换位后**稍等再启动**：新 exe 刚落盘，杀软的实时扫描经常正踩在它上面——
    # 2026-10-06 实测（本机更新 v0.5.1）：秒启动的那次解包被打断（_MEI 里只有 20 个
    # 基础 DLL、score_core.py 没解出来），弹"找不到 score_core.py"的白屏报错；
    # 手动再开就正常。所以：先静置 1.5s，启动后 3s 确认还活着，死了自动再拉一次。
    clean_stale_mei()
    time.sleep(2.0)
    # 2026-11-16：**别只试两次就撒手**。换位后新 exe 刚落盘，杀软实时扫描常常正压在上面，
    # 头一两次解包被打断很常见（旧的注释里 2026-10-06 就踩过）。给三次机会、每次多等一会儿，
    # 仍然失败才算真失败——那时旧版还完整躺在 _old\<ts>\ 里，用户可以手动起。
    ok_started = False
    for i in range(3):
        pid = start(d)
        if alive(pid, timeout=5.0):
            ok_started = True
            break
        log(d, "start attempt %d died (pid=%s)" % (i + 1, pid))
        if i < 2:
            time.sleep(2.0 + i * 2.0)
    if not ok_started:
        # 这里原来写了 % bak —— bak 只是 apply_pkg 的局部变量，跑到这一行会抛
        # NameError，助手直接崩掉，连日志都断了（2026-11-16 修）。
        _alert("新版已经装好了，但没能自动打开。\n\n"
               "请手动双击桌面的工作台快捷方式；旧版备份在 _old 文件夹里。")
        log(d, "!! 三次都没起来（新版已就位；旧版备份在 _old\\<时间戳>\\ 可手动回滚）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
