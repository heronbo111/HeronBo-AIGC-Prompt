# -*- coding: utf-8 -*-
r"""编译 Windows 安装版（Inno Setup）并把产物拷到工作区的 `_安装包\` 目录。

为什么要有它（2026-10-13 用户反馈"路径在哪，我怎么没找到"）：
  安装包的源码与编译都在 `D:\HeronBo-Build`（不在仓库），编译命令一长串；
  编完还得记得拷到 `_安装包\` —— 漏拷过。这里把"验前提 → 编译 → 拷贝 → 报体积"一条命令做掉。

**前提**（脚本会自检并在缺的时候说清楚）：
  ① 装了 Inno Setup 6（`ISCC.exe`）；
  ② 安装载荷是新的 —— 先 `python tools\打包换位.py`，再
     `python tools\载荷同步.py D:\HeronBo-Build\pkg-real`，否则打出来的是旧程序。

用法：
    python tools\打安装版.py            # 编译 → 拷到 _安装包\ → 报体积
    python tools\打安装版.py --dry      # 只看前提在不在、会产出什么，不编译
    python tools\打安装版.py --build-dir D:\HeronBo-Build --out D:\...
"""
import argparse
import glob
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.dirname(os.path.dirname(HERE))        # tools → 仓库 → 工作区根
DEFAULT_BUILD_DIR = r"D:\HeronBo-Build"
DEFAULT_OUT = os.path.join(WORKSPACE, "_安装包")
ISS = "setup-real.iss"


def say(m=""):
    print(m, flush=True)


def find_iscc():
    cands = []
    for root in (os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)",
                 os.environ.get("ProgramFiles") or r"C:\Program Files"):
        cands += glob.glob(os.path.join(root, "Inno Setup *", "ISCC.exe"))
    for c in sorted(cands, reverse=True):                  # 版本号大的优先
        if os.path.isfile(c):
            return c
    hit = shutil.which("ISCC")
    return hit or ""


def main():
    ap = argparse.ArgumentParser(description="编译 Windows 安装版并拷到 _安装包\\")
    ap.add_argument("--build-dir", default=DEFAULT_BUILD_DIR, help="安装器工程目录（默认 %s）" % DEFAULT_BUILD_DIR)
    ap.add_argument("--out", default=DEFAULT_OUT, help="产物拷贝到哪（默认 %s）" % DEFAULT_OUT)
    ap.add_argument("--dry", action="store_true", help="只体检，不编译")
    a = ap.parse_args()

    build = os.path.abspath(a.build_dir)
    iss = os.path.join(build, ISS)
    payload = os.path.join(build, "pkg-real")
    iscc = find_iscc()

    say("=" * 64)
    say("打 Windows 安装版")
    say("=" * 64)
    say("  工程目录：%s" % build)
    say("  编译脚本：%s%s" % (iss, "" if os.path.isfile(iss) else "   ← 不在！"))
    say("  安装载荷：%s%s" % (payload, "" if os.path.isdir(payload) else "   ← 不在！"))
    say("  ISCC    ：%s%s" % (iscc or "（没找到）", "" if iscc else "   ← 装 Inno Setup 6 后重试"))
    if not os.path.isfile(iss) or not os.path.isdir(payload):
        return 2
    if not iscc:
        say("  装一个 Inno Setup 6（inno-setup.com）就有了；或把 ISCC.exe 加进 PATH。")
        return 2

    # 载荷新鲜度提醒：dist 里的主程序比仓库那份旧就可能是"没同步载荷"
    try:
        here_dist = os.path.join(HERE, "dist", "score-tool.exe")
        pay_dist = os.path.join(payload, "skill", "tools", "dist", "score-tool.exe")
        if os.path.isfile(here_dist) and os.path.isfile(pay_dist):
            if os.path.getmtime(pay_dist) < os.path.getmtime(here_dist) - 1:
                say("")
                say("  !! 载荷里的主程序比仓库 dist 旧 —— 先跑 打包换位.py 再跑 载荷同步.py，")
                say("     否则这个安装包打的是旧程序（程序本体 2026-10-13 起是 onedir：exe + _internal\\）。")
    except OSError:
        pass

    if a.dry:
        say("")
        say("[dry] 到这里为止（去掉 --dry 就真编，约 6–10 分钟）。")
        return 0

    say("")
    say("[1/3] 编译（LZMA 压缩 1GB 级载荷，约 6–10 分钟，请耐心）…")
    t0 = time.time()
    with open(os.path.join(build, "build-last.log"), "w", encoding="utf-8", errors="replace") as lf:
        p = subprocess.run([iscc, iss], cwd=build, stdout=lf, stderr=subprocess.STDOUT)
    if p.returncode != 0:
        say("  编译失败（%.0f 秒）。日志结尾：" % (time.time() - t0))
        try:
            with open(os.path.join(build, "build-last.log"), encoding="utf-8", errors="replace") as f:
                for ln in f.read().splitlines()[-20:]:
                    say("    " + ln)
        except OSError:
            pass
        return 1
    out_dir = os.path.join(build, "output")
    hits = [f for f in glob.glob(os.path.join(out_dir, "*.exe")) if "Setup" in os.path.basename(f)]
    if not hits:
        say("  编译完了，但 output\\ 里没找到 Setup 产物 —— 看 build-last.log")
        return 1
    newest = max(hits, key=os.path.getmtime)
    say("  编译完成（%.0f 秒）：%s（%.0f MB）"
        % (time.time() - t0, os.path.basename(newest), os.path.getsize(newest) / 1048576.0))

    say("")
    say("[2/3] 拷到 %s" % a.out)
    try:
        os.makedirs(a.out, exist_ok=True)
        dst = os.path.join(a.out, os.path.basename(newest))
        shutil.copy2(newest, dst)
        say("  → %s（%.0f MB）" % (dst, os.path.getsize(dst) / 1048576.0))
    except OSError as e:
        say("  !! 拷贝失败：%r（产物仍在 %s，手动拷一下）" % (e, newest))
        return 1

    say("")
    say("[3/3] 目录现状：")
    for f in sorted(glob.glob(os.path.join(a.out, "*")), key=os.path.getmtime, reverse=True):
        say("  %7.0f MB  %s" % (os.path.getsize(f) / 1048576.0, os.path.basename(f)))
    say("")
    say("完成。发版前记得：版本号若进位，改 D:\\HeronBo-Build\\setup-common.iss 的两处 define 再编。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
