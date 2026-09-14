# -*- coding: utf-8 -*-
"""中文名入口：等价于 python tools/score_core.py（保持旧文档/习惯可用）。"""
import os, runpy, sys
here = os.path.dirname(os.path.abspath(__file__))
target = os.path.join(here, "score_core.py")
sys.argv[0] = target
runpy.run_path(target, run_name="__main__")
