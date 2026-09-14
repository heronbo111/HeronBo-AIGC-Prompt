# -*- coding: utf-8 -*-
"""ASCII-named launcher for 评分.py (keeps .cmd files free of non-ASCII text)."""
import os, runpy, sys
here = os.path.dirname(os.path.abspath(__file__))
target = os.path.join(here, "评分.py")
sys.argv[0] = target
runpy.run_path(target, run_name="__main__")
