# -*- coding: utf-8 -*-
r"""把 重装引导.py 打成发布用的 score-tool.exe（老客户端「检查更新」下载的迁移引导）。

为什么（2026-10-16）：0.6.2 及更早是单文件形态，更新到目录式需要"重装一次"；
把这个引导程序以 **score-tool.exe** 之名挂到新版 Release（version.json 的 exe 字段指向它），
老客户端更新后双击看到的就是说明窗口（见 重装引导.py 的 docstring）。新客户端走 pkg
（zip 更新包），不碰这条路——互不影响。

用法：
    python tools\打迁移引导.py            # 产物：tools\build\迁移引导\score-tool.exe
说明：需要 PyInstaller（跟 打分打包换位.py 同一套环境；没有它会给出提示）。
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "重装引导.py")
OUT = os.path.join(HERE, "build", "迁移引导")
NAME = "score-tool"          # 产物必须叫 score-tool.exe（老客户端按这个名字去 Release 找）


def main():
    try:
        import PyInstaller                                            # noqa: F401
    except ImportError:
        print("这台机器的解释器没有 PyInstaller：%s" % sys.executable)
        print("（用带 PyInstaller 的解释器跑本脚本，或先 pip install pyinstaller）")
        return 2
    args = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
            "--onefile", "--noconsole", "--name", NAME,
            "--distpath", OUT,
            "--workpath", os.path.join(OUT, "_work"),
            "--specpath", os.path.join(OUT, "_spec"), SRC]
    r = subprocess.run(args, cwd=HERE)
    exe = os.path.join(OUT, NAME + ".exe")
    if r.returncode != 0 or not os.path.isfile(exe):
        print("打包失败（exit=%s）" % r.returncode)
        return 2
    print("产物：%s（%.1f MB）" % (exe, os.path.getsize(exe) / 1048576.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
