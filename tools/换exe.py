# -*- coding: utf-8 -*-
r"""把新打好的程序换到发布位置（默认 `tools\dist\`），带"别在开着的时候换"的闸门。

**为什么需要这么个闸门（2026-09-16 实踩）**：exe 正在跑的时候，文件在 Windows 上本该是换成不掉的，
但那天用 `mv -f` 换成功了——结果正在跑的实例，它的映像文件已经变成另一份程序，之后再去从文件里
取还没载入的代码页，就可能拿到对不上的字节（表现为莫名崩溃）。所以换位前必须确认：
**没有 score-tool.exe 在跑**；万一手工操作已经这么干过，关掉重开一次就干净了。

**两种形态都吃（2026-10-13）**：
- 目录（onedir，现在的主形态）：`--new` 传 `_stage\score-tool\`（内含 score-tool.exe 与 _internal\），
  摊到 dist\ 下——`dist\score-tool.exe` + `dist\_internal\`。**dist\ 里的数据文件
  （*.local.json 等）原地保留**，换位只换程序本体。
- 单文件（旧版 onefile，兼容期还认）：`--new` 传 exe 文件。

用法：
    python tools\换exe.py                      # 新产物取 tools\_stage\score-tool\
    python tools\换exe.py --new 别的目录或exe
    python tools\换exe.py --force               # 明知有实例在跑也要换（危险，一般别用）
"""
import argparse
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
INTERNAL = "_internal"          # PyInstaller onedir 的依赖目录名
KEEP_OLD = 2                    # 只留最近几份旧备份


def running_pids():
    """正在跑的 score-tool.exe 的 PID（任务管理器里那个名字，认的是映像名）。"""
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq score-tool.exe",
                              "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, errors="replace",
                             timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    pids = []
    for line in out.splitlines():
        f = [x.strip('"') for x in line.split('","')]
        if f and f[0].lower() == "score-tool.exe" and len(f) > 1 and f[1].isdigit():
            pids.append(f[1])
    return pids


def _gate(force):
    """返回 (能不能动手, 提示/错误)。"""
    pids = running_pids()
    if pids and not force:
        return False, ("工作台正开着（score-tool.exe PID %s）——**先关掉它再换**。\n"
                       "开着的时候换，正在跑的实例会读到「已经被改过的文件」，可能莫名崩掉。\n"
                       "（真要在这种情况下硬换，加 --force，然后**手动关掉重开**那个实例。）"
                       % "、".join(pids))
    return True, "、".join(pids)


def _tidy_old(root, keep=KEEP_OLD):
    """回收旧备份：老形态的 score-tool_旧_*.exe 只留最近 keep 个；_old\\<ts>\\ 目录同理。"""
    try:
        olds = sorted(f for f in os.listdir(root)
                      if f.startswith("score-tool_旧_") and f.endswith(".exe"))
        for fn in olds[:-keep]:
            try:
                os.remove(os.path.join(root, fn))
            except OSError:
                pass
    except OSError:
        pass
    try:
        d = os.path.join(root, "_old")
        sub = sorted(n for n in os.listdir(d) if os.path.isdir(os.path.join(d, n)))
        for n in sub[:-keep]:
            shutil.rmtree(os.path.join(d, n), ignore_errors=True)
    except OSError:
        pass


def swap_dir(new_dir, dist_dir, force=False):
    """onedir 换位：把 new_dir 里的程序本体摊到 dist_dir，**dist_dir 里的数据文件原样保留**。

    备份：换下来的 score-tool.exe 与 _internal\\ 一起挪进 dist\\_old\\<时间戳>\\（可回滚）。
    """
    new_exe = os.path.join(new_dir, "score-tool.exe")
    if not os.path.isfile(new_exe):
        return 1, "新目录里没有 score-tool.exe：%s" % new_dir
    ok, why = _gate(force)
    if not ok:
        return 2, why
    pids = why
    os.makedirs(dist_dir, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    bak = os.path.join(dist_dir, "_old", ts)
    cur_exe = os.path.join(dist_dir, "score-tool.exe")
    cur_int = os.path.join(dist_dir, INTERNAL)
    try:
        if os.path.isfile(cur_exe) or os.path.isdir(cur_int):
            os.makedirs(bak, exist_ok=True)
            if os.path.isfile(cur_exe):
                shutil.move(cur_exe, os.path.join(bak, "score-tool.exe"))
            if os.path.isdir(cur_int):
                shutil.move(cur_int, os.path.join(bak, INTERNAL))    # 同盘 move＝改名，秒级
    except OSError as e:                                              # noqa: BLE001
        return 3, "旧版备份不出来（%s）：%s" % (bak, e)
    try:
        shutil.move(new_exe, cur_exe)
        new_int = os.path.join(new_dir, INTERNAL)
        if os.path.isdir(new_int):
            shutil.move(new_int, cur_int)
        for n in os.listdir(new_dir):        # 产物目录里除 exe/_internal 之外的东西（如 _version.json）
            if n in ("score-tool.exe", INTERNAL):
                continue
            s, d = os.path.join(new_dir, n), os.path.join(dist_dir, n)
            if os.path.isdir(s):
                shutil.copytree(s, d, dirs_exist_ok=True)
            else:
                shutil.copy2(s, d)
    except OSError as e:                                              # noqa: BLE001
        return 4, ("换不进去（%s）：%s\n新产物还在原地：%s" % (dist_dir, e, new_dir))
    _tidy_old(dist_dir)
    return 0, ("已换位（onedir）：%s\\score-tool.exe + %s\\\n旧版备份：%s%s"
               % (dist_dir, INTERNAL, bak,
                  "\n⚠ 这次是 --force 强换的：正在跑的那个实例请**关掉重开**" if pids else ""))


def swap(new, to, force=False):
    """兼容入口：new 是目录走 onedir 换位；是文件走旧的文件级换位（兼容期）。"""
    if os.path.isdir(new):
        # to 可能是 dist\score-tool.exe（老调用方）或 dist\（新调用方）→ 统一取目录
        dist_dir = to if os.path.isdir(to) or not to.lower().endswith(".exe") \
            else os.path.dirname(os.path.abspath(to))
        return swap_dir(new, dist_dir, force)
    if not os.path.isfile(new):
        return 1, ("找不到新产物：%s\n（onedir 先打包：python tools\\打包换位.py）" % new)
    ok, why = _gate(force)
    if not ok:
        return 2, why
    pids = why
    os.makedirs(os.path.dirname(os.path.abspath(to)) or ".", exist_ok=True)
    old = os.path.join(os.path.dirname(os.path.abspath(to)),
                       "score-tool_旧_%s.exe" % time.strftime("%Y%m%d-%H%M"))
    try:
        if os.path.isfile(to):
            shutil.copy2(to, old)
    except OSError as e:                                          # noqa: BLE001
        return 3, "旧 exe 备份不出来（%s）：%s" % (old, e)
    try:
        os.replace(new, to)          # 同盘原子换；被占用时直接抛 PermissionError，不会换半个
    except OSError as e:
        return 4, ("换不进去（%s）：%s\n新 exe 还在原地：%s"
                   % (to, e, new))
    _tidy_old(os.path.dirname(os.path.abspath(to)))
    return 0, ("已换位：%s\n旧版备份：%s%s"
               % (to, old, "\n⚠ 这次是 --force 强换的：正在跑的那个实例请**关掉重开**"
                           if pids else ""))


def main():
    ap = argparse.ArgumentParser(description="把新产物换到发布位置（默认 tools\\dist\\）")
    ap.add_argument("--new", default=os.path.join(HERE, "_stage", "score-tool"),
                    help="新产物（onedir 目录，默认 tools\\_stage\\score-tool；也认单个 exe）")
    ap.add_argument("--to", default=os.path.join(HERE, "dist"),
                    help="发布位置（默认 tools\\dist\\）")
    ap.add_argument("--force", action="store_true", help="有实例在跑也换（危险）")
    a = ap.parse_args()
    code, msg = swap(a.new, a.to, a.force)
    print(("[OK] " if code == 0 else "[停] ") + msg)
    return code


if __name__ == "__main__":
    sys.exit(main())
