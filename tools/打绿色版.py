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
DEFAULT_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "..", "HeronBo-Workbench", "installer", "pkg-real")
# 默认落到工作区的 `_安装包\`（2026-10-13 用户反馈"产物在哪、我找不到"）——
# 那是他收安装包的地方；放在构建目录里他翻不到。--out 可覆盖。
DEFAULT_OUT = os.path.join(os.path.dirname(os.path.dirname(HERE)), "_安装包")
# 系统运行库安装器（2026-10-13）：安装版会在装的时候替你装这两样（见 setup-common.iss 的
# [Run] 段），绿色版不能少——否则目标机缺运行库时表现就不一致了（界面渲染/组件起不来）。
# 源在 D:\HeronBo-Build\vendor\（203MB 的 WebView2 是**离线完整版**，断网机器也能装）。
VENDOR_DIR = r"D:\HeronBo-Build\vendor"
RUNTIME_PKGS = [("vc_redist.x64.exe", 24),
                ("MicrosoftEdgeWebView2RuntimeInstallerX64.exe", 203)]
PKG_SUBDIR = "runtime_pkgs"          # 目录名刻意用 ASCII：cmd 处理中文路径/编码容易翻车

# ⚠️ 别把 node_modules 排除掉：runtime\node 自带的 corepack（npm/npx/pnpm 的 shim）就在里面，
# 排除后绿色版会比安装版少 5000 多个文件（2026-10-13 实测踩过）。
EXCLUDE_DIRS = {"__pycache__", "_old", "_已删除", "_release", "_stage"}
EXCLUDE_SUFFIXES = (".pyc", ".log", ".local.json", ".local.md", "heronbo_version_remote.json")

GUIDE = """HeronBo 视频工作台 · 绿色版（解压即用，不需要安装）

【第一次使用】
  双击文件夹里的  first-run.cmd  ——它会：
    1. 检查并补装两个系统运行库（VC++ 运行库、界面渲染用的 WebView2；装过的机器几秒跳过）
    2. 自动启动工作台
  以后每次使用：直接双击 HeronBo.exe 即可。

【不想跑 first-run.cmd 也行】
  机器上如果已经有这两个运行库，直接双击 HeronBo.exe；界面渲染库缺失时，工作台会自动
  改用 Edge 窗口打开（功能不缺，只是窗口形态不同）。

【其它】
  · 本版自带运行环境（Python / Node / ffmpeg / 模型），不需要另装任何东西
  · 不写注册表、不产生卸载项；不想要了直接删掉整个文件夹
  · 想要桌面图标：右键 HeronBo.exe → 发送到 → 桌面快捷方式
  · 若被杀毒软件或系统拦截：把**整个文件夹**加进信任区（加目录，不要只加单个文件）。
    Windows「智能应用控制」拦的是未签名程序——签名需要购买代码签名证书（当前发行版未签名），
    遇到该项拦截需要在系统设置里关闭它，或把程序交给能加白名单的机器使用。
  · 更新：在工作台里点「检查更新」会自动换新版程序。**别把这个文件夹放进 Program Files**，
    那里普通用户没有写权限，更新会失败（放 D 盘等可写位置最稳）。
"""

FIRST_RUN_CMD = """@echo off
rem HeronBo Workbench - first run helper.
rem ASCII-only on purpose: cmd.exe parses .cmd byte-by-byte in the active code page,
rem so UTF-8 Chinese text in here would get mangled (we hit that before).
rem What it does: install the two system runtimes if missing, then start the app.
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title HeronBo Workbench - first run
echo ============================================================
echo   HeronBo Workbench  -  first run
echo   step 1/2  VC++ runtime     (skipped if already installed)
echo   step 2/2  WebView2 runtime (skipped if already installed)
echo   then      start the workbench
echo   (UAC may ask for permission - that is expected)
echo ============================================================
echo.
if exist "runtime_pkgs\\vc_redist.x64.exe" (
  echo [1/2] VC++ runtime ...
  start /wait "" "runtime_pkgs\\vc_redist.x64.exe" /install /quiet /norestart
) else (
  echo [1/2] vc_redist not bundled - skipped.
)
if exist "runtime_pkgs\\MicrosoftEdgeWebView2RuntimeInstallerX64.exe" (
  echo [2/2] WebView2 runtime ...
  start /wait "" "runtime_pkgs\\MicrosoftEdgeWebView2RuntimeInstallerX64.exe" /silent /install
) else (
  echo [2/2] webview2 installer not bundled - skipped.
)
echo.
echo Starting HeronBo ...
start "" "HeronBo.exe"
echo Done.  Next time just double-click HeronBo.exe
exit /b 0
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
    pkgs = [(os.path.join(VENDOR_DIR, fn), fn) for fn, _ in RUNTIME_PKGS
            if os.path.isfile(os.path.join(VENDOR_DIR, fn))]
    missing_pkgs = [fn for fn, _ in RUNTIME_PKGS if not os.path.isfile(os.path.join(VENDOR_DIR, fn))]
    total += sum(os.path.getsize(p) for p, _ in pkgs)
    print("源：%s" % src)
    print("版本：%s    文件：%d 个    原始大小：%.0f MB    排除命中：%d" %
          (ver, len(files), total / 1048576.0, skipped))
    print("系统运行库：%s" % ("、".join(fn for _p, fn in pkgs) or "（一个都没找到）"))
    for fn in missing_pkgs:
        print("  !! 缺 %s（不在 %s）——目标机缺该运行库时绿色版装不上，功能会与安装版不一致"
              % (fn, VENDOR_DIR))

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
        z.writestr(top + "/first-run.cmd", FIRST_RUN_CMD)
        n += 1
        for fp, fn in pkgs:                       # 系统运行库（安装版会替你装的那两个）
            z.write(fp, "%s/%s/%s" % (top, PKG_SUBDIR, fn))
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
    # 一致性自检（2026-10-13 用户要求"绿色版和安装版功能一致"）：能找到安装副本就比文件清单。
    # 差异只允许出现在"壳"上：卸载器、首启脚本、运行库目录、说明文件。
    try:
        import importlib.util as _ilu
        _fp = os.path.join(HERE, "找工作台.py")
        if os.path.isfile(_fp):
            _spec = _ilu.spec_from_file_location("heronbo_findwb", _fp)
            _m = _ilu.module_from_spec(_spec)
            _spec.loader.exec_module(_m)
            _app = ((_m.detect(scan=True) or {}).get("appDir") or "")
            if _app and os.path.isdir(_app):
                shell = ("unins", PKG_SUBDIR + "/", "first-run", "先读我")
                have = set()
                for dp, dns, fns in os.walk(_app):
                    dns[:] = [d for d in dns if d not in EXCLUDE_DIRS]
                    for f in fns:
                        have.add(os.path.relpath(os.path.join(dp, f), _app).replace("\\", "/"))
                with zipfile.ZipFile(out) as z:
                    inzip = {i.filename[len(top) + 1:] for i in z.infolist()
                             if i.filename.startswith(top + "/") and not i.filename.endswith("/")}
                a = sorted(x for x in have - inzip if not x.startswith(shell))
                b = sorted(x for x in inzip - have if not x.startswith(shell))
                print("  与安装版比对（%s）：只在安装版 %d 个、只在绿色版 %d 个"
                      % (os.path.basename(_app.rstrip("\\/")), len(a), len(b)))
                for x in (a + b)[:8]:
                    print("     · %s" % x)
                if not a and not b:
                    print("     ✓ 两边内容一致（除安装器自带的壳文件）")
    except Exception:                                            # noqa: BLE001
        pass
    # 签名状态说清（2026-10-13）：绿色版绕开的是"安装"这一步；若主程序没签名，
    # 用户那边照样会被 SmartScreen 警告、被智能应用控制拦——那跟装不装没关系。
    try:
        import importlib.util as _ilu
        _sp = os.path.join(HERE, "签名.py")
        if os.path.isfile(_sp):
            _spec = _ilu.spec_from_file_location("heronbo_sign", _sp)
            _sg = _ilu.module_from_spec(_spec)
            _spec.loader.exec_module(_sg)
            _st = _sg.is_signed(os.path.join(src, "HeronBo.exe"))
            print("  主程序签名：%s" % {
                True: "已签名",
                False: "**未签名**（用户侧可能被 SmartScreen 警告 / 被「智能应用控制」拦——"
                       "签名请见 tools\\签名.py 顶部说明）",
            }.get(_st, "未知（没装 signtool，看不出）"))
    except Exception:                                            # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
