# -*- coding: utf-8 -*-
r"""打在线更新包：把 dist\ 的工作台本体（onedir）压成 zip——已装用户「检查更新」下载的就是它。

为什么要有它（2026-10-16）：工作台对外发布（Release）要挂两样产品附件——
  ① HeronBo-Workbench-Setup-vX.Y.Z.exe   完整安装包（新装/重装；Inno，打安装版.py）
  ② HeronBo-Workbench-Update-vX.Y.Z.zip  在线更新包（只换程序本体；本脚本）
「检查更新」链路（2026-10-13 起 onedir）：远端 version.json 的 `pkg` 字段指向 ②，
客户端下载 → 校验 → 交更新助手解压摊入工作台目录。

内容：dist\score-tool.exe + dist\_internal\（**只有这两样**——*.local.json 本机配置与凭据、
_old\ 旧版备份、score-tool_旧_*.exe 都不进包，与 打包发行.py 的 _zip_workbench 同一口径）。
zip 顶层**不带目录**（更新助手解压后直接摊入；它也兼容"套了一层同名目录"的老结构）。
版本号读 tools\_version.json（打包换位.py 打包时会按 SKILL.md 重写它）。

用法：
    python tools\打更新包.py            # 打到工作区的 _安装包\（先跑 打包换位.py，dist 才是新的）
    python tools\打更新包.py --dry      # 只体检：dist 在不在、多少文件多大、产物路径
"""
import argparse
import json
import os
import sys
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.dirname(os.path.dirname(HERE))          # tools → 仓库 → 工作区根
DIST = os.path.join(HERE, "dist")
DEFAULT_OUT = os.path.join(WORKSPACE, "_安装包")
# 绝不进包（2026-10-16 实测抓到 _internal\feedback.local.json——本机反馈凭据——混了进来）：
# 本机配置与凭据（*.local.json / *.local.md）、字节码、日志。与 打绿色版.py 同一口径。
EXCLUDE_SUFFIXES = (".local.json", ".local.md", ".pyc", ".log")
EXCLUDE_DIRS = {"__pycache__", "_old"}


def say(m=""):
    print(m, flush=True)


def version_of():
    r"""版本号：tools\_version.json（打包换位.py 按 SKILL.md 重写过）；读不到退回 SKILL.md。"""
    try:
        with open(os.path.join(HERE, "_version.json"), encoding="utf-8-sig") as f:
            v = str((json.load(f) or {}).get("version") or "").strip()
        if v:
            return v
    except (OSError, ValueError):
        pass
    try:
        with open(os.path.join(os.path.dirname(HERE), "SKILL.md"), encoding="utf-8") as f:
            for line in f:
                if line.startswith("version:"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return "unknown"


def collect(dist_dir):
    r"""要进包的文件清单（相对 dist 的路径，正斜杠）——exe + _internal\，剔除本机件。"""
    if not os.path.isfile(os.path.join(dist_dir, "score-tool.exe")):
        return []
    out = ["score-tool.exe"]
    internal = os.path.join(dist_dir, "_internal")
    if os.path.isdir(internal):
        for dp, dns, fns in os.walk(internal):
            dns[:] = [d for d in dns if d not in EXCLUDE_DIRS]
            for fn in sorted(fns):
                if fn.lower().endswith(EXCLUDE_SUFFIXES):
                    continue
                fp = os.path.join(dp, fn)
                out.append(os.path.relpath(fp, dist_dir).replace(os.sep, "/"))
    return out


def main():
    ap = argparse.ArgumentParser(description="打在线更新包（HeronBo-Workbench-Update-v*.zip）")
    ap.add_argument("--out", default=DEFAULT_OUT, help="产物目录（默认工作区 _安装包）")
    ap.add_argument("--dry", action="store_true", help="只体检，不打包")
    a = ap.parse_args()

    ver = version_of()
    say("版本（tools\\_version.json）：%s" % ver)
    files = collect(DIST)
    if not files:
        say("✗ dist 里没有 score-tool.exe —— 先跑：python tools\\打包换位.py")
        return 2
    onedir = any(f.startswith("_internal/") for f in files)
    if not onedir:
        # 更新助手把 zip 解压后摊入工作台目录：缺 _internal\ 的包对 onedir 是残废（跑不起来）
        say("✗ 不是 onedir（dist 里缺 _internal\\）——先跑 python tools\\打包换位.py 再打")
        return 2
    total = sum(os.path.getsize(os.path.join(DIST, f.replace("/", os.sep))) for f in files)
    zp = os.path.join(a.out, "HeronBo-Workbench-Update-v%s.zip" % ver)
    say("内容：%d 个文件（score-tool.exe + _internal\\）、合计 %.1f MB" % (len(files), total / 1048576.0))
    say("产物：%s" % zp)
    if a.dry:
        say("\n这是体检（--dry），没有打包。")
        return 0
    if os.path.isfile(zp):
        say("（同名包已存在，覆盖）")
    os.makedirs(a.out, exist_ok=True)
    t0 = time.time()
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for f in files:
            z.write(os.path.join(DIST, f.replace("/", os.sep)), f)
    say("\n✓ 打好：%s（%.1f MB，用时 %.0f 秒）" % (zp, os.path.getsize(zp) / 1048576.0,
                                              time.time() - t0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
