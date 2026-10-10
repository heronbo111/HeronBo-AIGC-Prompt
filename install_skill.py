# -*- coding: utf-8 -*-
r"""install_skill.py —— agent / 命令行专用的 ASCII 安装入口（转发到 安装向导.py）。

为什么有这个文件：仓库里中文文件名（安装向导.py）在 cmd / PowerShell 某些代码页下
传参会碎（仓库根 一键安装.cmd 的注释记过同款坑）；远端 agent 拿到包后要一条命令装完，
不适合再敲中文路径。本文件只做转发，逻辑都在 安装向导.py。

用法（在解压出来的仓库根，或任意目录）：
    python install_skill.py            # 无人值守：自动探测本机 agent 技能目录并装入
    python install_skill.py --dir X    # 指定落位目录
    python install_skill.py --list     # 只列出各 harness 的默认目录
    python install_skill.py --wizard   # 走交互式向导（人自己装才用）
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WIZARD = os.path.join(HERE, "安装向导.py")


def main(argv):
    if not os.path.isfile(WIZARD):
        print("[STOP] 找不到 %s —— 请在解压后的仓库根目录运行本脚本。" % WIZARD)
        return 1
    if argv and argv[0] == "--wizard":
        argv = argv[1:]
    else:
        argv = ["--auto"] + [x for x in argv if x != "--auto"]
    py = sys.executable or "python"
    r = subprocess.run([py, WIZARD] + argv)
    return r.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
