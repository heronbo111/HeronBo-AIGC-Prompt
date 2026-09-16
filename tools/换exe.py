# -*- coding: utf-8 -*-
r"""把新打好的 exe 换到发布位置（默认 `tools\dist\score-tool.exe`），带"别在开着的时候换"的闸门。

**为什么需要这么个闸门（2026-09-16 实踩）**：exe 正在跑的时候，文件在 Windows 上本该是换成不掉的，
但那天用 `mv -f` 换成功了——结果正在跑的实例，它的映像文件已经变成另一份程序，之后再去从文件里
取还没载入的代码页，就可能拿到对不上的字节（表现为莫名崩溃）。所以换位前必须确认：
**没有 score-tool.exe 在跑**；万一手工操作已经这么干过，关掉重开一次就干净了。

用法：
    python tools\换exe.py                      # 新 exe 取 tools\_stage\score-tool.exe
    python tools\换exe.py --new 别的.exe
    python tools\换exe.py --force               # 明知有实例在跑也要换（危险，一般别用）
"""
import argparse
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


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


def swap(new, to, force=False):
    if not os.path.isfile(new):
        return 1, ("找不到新 exe：%s\n（先打包：python -m PyInstaller --noconfirm --distpath _stage "
                   "--workpath build score-tool.spec）" % new)
    pids = running_pids()
    if pids and not force:
        return 2, ("工作台正开着（score-tool.exe PID %s）——**先关掉它再换**。\n"
                   "开着的时候换，正在跑的实例会读到「已经被改过的文件」，可能莫名崩掉。\n"
                   "（真要在这种情况下硬换，加 --force，然后**手动关掉重开**那个实例。）"
                   % "、".join(pids))
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
    return 0, ("已换位：%s\n旧版备份：%s%s"
               % (to, old, "\n⚠ 这次是 --force 强换的：正在跑的那个实例请**关掉重开**"
                           if pids else ""))


def main():
    ap = argparse.ArgumentParser(description="把新 exe 换到发布位置（默认 tools\\dist\\score-tool.exe）")
    ap.add_argument("--new", default=os.path.join(HERE, "_stage", "score-tool.exe"),
                    help="新打好的 exe（默认 tools\\_stage\\score-tool.exe）")
    ap.add_argument("--to", default=os.path.join(HERE, "dist", "score-tool.exe"),
                    help="发布位置（默认 tools\\dist\\score-tool.exe）")
    ap.add_argument("--force", action="store_true", help="有实例在跑也换（危险）")
    a = ap.parse_args()
    code, msg = swap(a.new, a.to, a.force)
    print(("[OK] " if code == 0 else "[停] ") + msg)
    return code


if __name__ == "__main__":
    sys.exit(main())
