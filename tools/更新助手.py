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
import subprocess
import sys
import time

LOG_NAME = "score-tool.update.log"
KEEP_DEFAULT = 2


def log(d, msg):
    """给排障留一份轨迹（纯 ASCII：装机端可能在任何代码页下看它）。"""
    try:
        p = os.path.join(d, LOG_NAME)
        with open(p, "a", encoding="ascii", errors="replace", newline="\n") as f:
            f.write("[%s] %s\n" % (time.strftime("%Y%m%d-%H%M%S"), msg))
    except OSError:
        pass


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
    """把新工作台启起来（脱离本进程，用户看到窗口自己回来）。"""
    exe = exe or os.path.join(d, "score-tool.exe")
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200     # DETACHED | NEW_PROCESS_GROUP
    try:
        subprocess.Popen([exe], cwd=d, **kw)
        log(d, "started %s" % os.path.basename(exe))
        return True
    except OSError as e:
        log(d, "start failed: %s" % e)
        return False


def main(argv=None):
    ap = argparse.ArgumentParser(description="工作台更新助手（由工作台自己拉起）")
    ap.add_argument("--dir", required=True, help="工作台所在目录")
    ap.add_argument("--src", required=True, help="新下载好的文件（相对 --dir 或绝对路径）")
    ap.add_argument("--pid", type=int, default=0, help="旧工作台的进程号（等它退出）")
    ap.add_argument("--mode", default="apply", choices=["apply", "rollback"])
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
    ok, why = swap(d, a.src, keep=a.keep)
    if not ok:
        log(d, "swap failed: %s" % why)
        return 2
    start(d)
    return 0


if __name__ == "__main__":
    sys.exit(main())
