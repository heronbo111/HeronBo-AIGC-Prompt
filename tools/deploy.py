# -*- coding: utf-8 -*-
r"""ASCII-named entry for the deploy script (真身是 `部署.py`)。

为什么多这么一层：`.cmd` 批处理里写中文文件名不稳——cmd.exe 按当前代码页逐字节解析 .cmd，
UTF-8 的中文路径很容易被切错（2026-09-16 实测：一键安装.cmd 里引用 `tools\部署.py` 直接报
"'x' 不是内部或外部命令"）。所以批处理统一调这个 ASCII 名字的壳，中文只在 Python 里出现。

用法与 部署.py 完全一样：
    python tools\deploy.py check|install|agents|shortcut|start|wx|all
"""
import os
import runpy
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REAL = os.path.join(HERE, "部署.py")

if __name__ == "__main__":
    if not os.path.isfile(REAL):
        print("找不到 %s（真身被挪走了？）" % REAL)
        sys.exit(2)
    sys.argv[0] = REAL
    runpy.run_path(REAL, run_name="__main__")
