# -*- coding: utf-8 -*-
r"""打绿色版 zip：把装机载荷打成"解压即用"的压缩包（不需要安装器）。

为什么要有它（2026-10-13 用户要"两个版本"）：
  安装版走 Inno Setup：要跑安装向导、写注册表，而且**"装"这一步实测会被杀软/系统拦**
  （CreateProcess 4551、「智能应用控制已阻止」）。绿色版解压到任意目录、双击 HeronBo.exe
  就用，把"安装"这一整步绕开。程序本来就是自包含的（目录式自带 Python 运行时），所以可行。

源：默认 `D:\HeronBo-Build\pkg-real`（安装包载荷目录，干净）；`--from` 换别的。
**必须排除**（绝不能进包）：本机配置与凭据 `*.local.json`、旧版备份 `_old\`、日志、`__pycache__`。
产物：`<out>\HeronBo-Workbench-Portable-v<版本>.zip`，zip 内顶层**带一层目录**（解压不散落）。

    python tools\打绿色版.py --dry        # 只体检：源在不在、多少文件多大、排除项命中几次
    python tools\打绿色版.py              # 真打（1GB 级，几分钟）
    python tools\打绿色版.py --from <目录> --out <目录>
"""
import argparse
import json
import os
import sys
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SRC = r"D:\HeronBo-Build\pkg-real"
DEFAULT_OUT = r"D:\HeronBo-Build\art"

EXCLUDE_DIRS = {"__pycache__", "_old", "_已删除", "node_modules", "_release", "_stage"}
EXCLUDE_SUFFIXES = (".pyc", ".log", ".local.json", ".local.md")

GUIDE = """HeronBo 视频工作台 · 绿色版（解压即用，不需要安装）

1. 把整个文件夹解压到任意位置（建议路径不含空格，例如 D:\\HeronBo）
2. 双击文件夹里的 HeronBo.exe 启动工作台（第一次启动稍慢，属正常现象）
3. 不写注册表、不产生卸载项；不想要了直接删掉整个文件夹即可
4. 想要桌面图标：右键 HeronBo.exe → 发送到 → 桌面快捷方式

说明
· 本版自带运行环境（Python / Node / ffmpeg / 模型），不需要另装任何东西；
· 若杀毒软件或系统拦截：把**整个文件夹**加进信任区（加目录，不要只加单个文件）；
  Windows「智能应用控制」拦的是未签名程序，与是否安装无关——那种情况需要在
  系统设置里关闭该项，或使用带代码签名发行的版本；
· 首次用到「拆解转写 / 深度视频」时若提示缺模型，按提示联网拉取一次即可（之后都在本地）。
"""


def version_of(src):
    """从载荷里的 _version.json 读版本（读不到就退回 SKILL.md，再不行 unknown）。"""
    p = os.path.join(src, "skill", "tools", "_version.json")
    try:
        with open(p, encoding="utf-8") as f:
            return str((json.load(f) or {}).get("version") or "unknown")
    except (OSError, ValueError):
        pass
    try:
        with open(os.path.join(src, "skill", "SKILL.md"), encoding="utf-8") as f:
            for line in f:
                if line.startswith("version:"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return "unknown"


def collect(src):
    """遍历载荷，返回 (要打包的文件列表[(绝对路径, 包内相对路径)], 命中排除项数)。"""
    files, skipped = [], 0
    for dp, dns, fns in os.walk(src):
        dns[:] = [d for d in dns if d not in EXCLUDE_DIRS]
        skipped += 1 if any(d in EXCLUDE_DIRS for d in os.listdir(dp) if os.path.isdir(os.path.join(dp, d))) else 0
        for fn in sorted(fns):
            if fn.endswith(EXCLUDE_SUFFIXES):
                skipped += 1
                continue
            fp = os.path.join(dp, fn)
            files.append((fp, os.path.relpath(fp, src).replace(os.sep, "/")))
    return files, skipped


def main():
    ap = argparse.ArgumentParser(description="打绿色版 zip（解压即用，不需要安装器）")
    ap.add_argument("--from", dest="src", default=DEFAULT_SRC, help="装机载荷目录（默认 %s）" % DEFAULT_SRC)
    ap.add_argument("--out", default=DEFAULT_OUT, help="产物放哪（默认 %s）" % DEFAULT_OUT)
    ap.add_argument("--dry", action="store_true", help="只体检，不写 zip")
    a = ap.parse_args()

    src = os.path.abspath(a.src)
    if not os.path.isdir(src):
        print("!! 源目录不存在：%s" % src)
        print("   （安装包载荷在 D:\\HeronBo-Build\\pkg-real；开发机也可用安装副本目录）")
        return 2
    if not os.path.isfile(os.path.join(src, "HeronBo.exe")):
        print("!! 源目录里没有 HeronBo.exe（启动器）——这不像装机载荷目录：%s" % src)
        return 2

    ver = version_of(src)
    top = "HeronBo-Workbench-Portable-v%s" % ver
    files, skipped = collect(src)
    total = sum(os.path.getsize(p) for p, _ in files)
    print("源：%s" % src)
    print("版本：%s    文件：%d 个    原始大小：%.0f MB    排除命中：%d" %
          (ver, len(files), total / 1048576.0, skipped))

    if a.dry:
        print("\n[dry] 到这里为止（真打去掉 --dry）。")
        return 0

    os.makedirs(a.out, exist_ok=True)
    out = os.path.join(a.out, "%s.zip" % top)
    t0 = time.time()
    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.writestr(top + "/先读我-绿色版.txt", GUIDE)
        n += 1
        for fp, rel in files:
            z.write(fp, top + "/" + rel)
            n += 1
            if n % 2000 == 0:
                print("   …已写入 %d 个文件（%.0f 秒）" % (n, time.time() - t0), flush=True)
    # 出货前自检：包内不许有本机配置/凭据（这是"发出去别人能用、别把本机信息带出去"的底线）
    bad = []
    with zipfile.ZipFile(out) as z:
        for info in z.infolist():
            base = os.path.basename(info.filename)
            if base.endswith((".local.json", ".local.md")):
                bad.append(info.filename)
    print("\n产物：%s" % out)
    print("  %d 个文件，%.0f MB（压缩后 %.0f MB，用时 %.0f 秒）"
          % (n - 1, total / 1048576.0, os.path.getsize(out) / 1048576.0, time.time() - t0))
    if bad:
        print("!! 自检没过：包里带了本机配置——立刻删掉这个包重打")
        for b in bad[:10]:
            print("   " + b)
        return 3
    print("  自检通过：不含本机配置/凭据。用户解压后双击 %s/HeronBo.exe 即可用。" % top)
    return 0


if __name__ == "__main__":
    sys.exit(main())
