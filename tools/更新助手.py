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


def log(d, msg):
    """给排障留一份轨迹（纯 ASCII：装机端可能在任何代码页下看它）。"""
    try:
        p = os.path.join(d, LOG_NAME)
        with open(p, "a", encoding="ascii", errors="replace", newline="\n") as f:
            f.write("[%s] %s\n" % (time.strftime("%Y%m%d-%H%M%S"), msg))
    except OSError:
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
    """
    exe_new = os.path.join(src_dir, "score-tool.exe")
    if not os.path.isfile(exe_new):
        return False, "新包里没有 score-tool.exe（%s）" % src_dir
    ts = ts or time.strftime("%Y%m%d-%H%M%S")
    bak = os.path.join(d, "_old", ts)
    cur_exe = os.path.join(d, "score-tool.exe")
    cur_int = os.path.join(d, INTERNAL)
    take = shutil.move if move_src else shutil.copy2
    try:
        if os.path.isfile(cur_exe) or os.path.isdir(cur_int):
            os.makedirs(bak, exist_ok=True)
            if os.path.isfile(cur_exe):
                shutil.move(cur_exe, os.path.join(bak, "score-tool.exe"))
            if os.path.isdir(cur_int):
                shutil.move(cur_int, os.path.join(bak, INTERNAL))
        take(exe_new, cur_exe)
        new_int = os.path.join(src_dir, INTERNAL)
        if os.path.isdir(new_int):
            if move_src:
                shutil.move(new_int, cur_int)
            else:
                shutil.copytree(new_int, cur_int)
    except OSError as e:                                          # noqa: BLE001
        return False, "摊入失败（%s）" % e
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

    来源：工作台被 taskkill（「暂停」按钮）/ 更新换位等异常退出时，PyInstaller 的
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
    ap.add_argument("--dir", required=True, help="工作台所在目录")
    ap.add_argument("--src", help="新下载好的文件（相对 --dir 或绝对路径）；--mode restart 时不需要")
    ap.add_argument("--pid", type=int, default=0, help="旧工作台的进程号（等它退出）")
    ap.add_argument("--mode", default="apply", choices=["apply", "rollback", "restart"])
    ap.add_argument("--keep", type=int, default=KEEP_DEFAULT)
    ap.add_argument("--timeout", type=float, default=60.0)
    a = ap.parse_args(argv)

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
    # 三种 src 形态都吃（2026-10-13 onedir 化）：
    #   · .zip  → 新版 onedir 整包：解压 → 摊入（exe + _internal）
    #   · 目录   → 回滚（_old\<ts>\）或已解压的包：直接摊入
    #   · 文件   → 老的单文件 exe（兼容期）
    if (a.mode or "") == "restart":
        # 纯重启（2026-10-16）：**不换任何文件**，等旧实例退干净 → 起新的。
        # 用途：界面里「切换 agent 通道」后让工作台以新通道重新搭桥（重启最干净）；
        # 走的是同一个助手（等退出 / 静置防杀软 / 起不来自动重试一次，这段逻辑全复用）。
        clean_stale_mei()
        time.sleep(1.5)
        pid = start(d)
        if not alive(pid, timeout=3.0):
            log(d, "restart: first start died (pid=%s), retrying once" % pid)
            time.sleep(1.0)
            pid2 = start(d)
            if not alive(pid2, timeout=3.0):
                log(d, "restart: second start died too (pid=%s) - user will start it manually" % pid2)
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
            ok, why = apply_pkg(d, root, keep=a.keep)
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
        return 2
    # 换位后**稍等再启动**：新 exe 刚落盘，杀软的实时扫描经常正踩在它上面——
    # 2026-10-06 实测（本机更新 v0.5.1）：秒启动的那次解包被打断（_MEI 里只有 20 个
    # 基础 DLL、score_core.py 没解出来），弹"找不到 score_core.py"的白屏报错；
    # 手动再开就正常。所以：先静置 1.5s，启动后 3s 确认还活着，死了自动再拉一次。
    clean_stale_mei()
    time.sleep(1.5)
    pid = start(d)
    if not alive(pid, timeout=3.0):
        log(d, "first start died (pid=%s), retrying once" % pid)
        time.sleep(1.0)
        pid2 = start(d)
        if not alive(pid2, timeout=3.0):
            log(d, "second start died too (pid=%s) - user will start it manually" % pid2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
