#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打 npm 包：把 skill 需要的东西复制到 npm/skill/，同步版本号，然后 npm pack 验证。

用法：
    python tools/打npm包.py            # 组装 + npm pack（生成 tgz 到 npm/）
    python tools/打npm包.py --no-pack  # 只组装，不跑 npm

发布：
    cd npm && npm publish --access public
（发版前记得先 npm login；包名 heronbo-aigc-prompt 若被占用，改 npm/package.json 的 name）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NPM = ROOT / "npm"
SKILL = NPM / "skill"

# 只装「公开技能本体」。清单直接取 git（tracked + 未忽略的未跟踪），
# 这样私有仓库才有的东西（tools/workbench 界面、workbench_server.py 等，
# 在 .gitignore 里）永远不会被 npm 包顺带发出去——那是 2026-09-19 的裁定。
SKIP_PREFIX = (
    "npm/",            # 打包产物自己
    ".git/", ".github/", ".claude/",
    "tools/dist/", "tools/_stage/", "tools/build/", "tools/_release/",
    "tools/_vendor/",  # 大件（模型/node/ffmpeg），npm 包不带
)
SKIP_SUFFIX = (".tgz", ".zip", ".exe", ".pyc", ".log")
SKIP_FILES = {"references/paths.local.md", "即梦技能-导入版.txt", "tools/打npm包.py"}


def read_version() -> str:
    txt = (ROOT / "SKILL.md").read_text(encoding="utf-8", errors="replace")
    m = re.search(r"^version:\s*([0-9.]+)\s*$", txt, re.M)
    if not m:
        raise SystemExit("SKILL.md 里没找到 version:")
    return m.group(1)


def git_files() -> list[Path]:
    """公开技能本体的文件清单（遵守 .gitignore）。"""
    r = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=str(ROOT), capture_output=True,
    )
    if r.returncode != 0:
        raise SystemExit("git ls-files 失败：%s" % (r.stderr.decode("utf-8", "replace")[:400]))
    out = []
    for raw in r.stdout.split(b"\0"):
        if not raw:
            continue
        rel = raw.decode("utf-8", "replace").replace("\\", "/")
        if rel.startswith(SKIP_PREFIX) or rel.endswith(SKIP_SUFFIX) or rel in SKIP_FILES:
            continue
        p = ROOT / rel
        if p.is_file():
            out.append(Path(rel))
    return out


def build() -> str:
    ver = read_version()
    if SKILL.exists():
        shutil.rmtree(SKILL)
    SKILL.mkdir(parents=True)
    files = git_files()
    for rel in files:
        d = SKILL / rel
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, d)
    n = len(files)
    # 版本号写进 package.json
    pkg_path = NPM / "package.json"
    pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
    pkg["version"] = ver
    pkg_path.write_text(json.dumps(pkg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    size = sum(f.stat().st_size for f in SKILL.rglob("*") if f.is_file())
    print("skill/ 组装完成：%d 个文件，%.1f MB（版本 %s）" % (n, size / 1048576, ver))
    return ver


def pack() -> None:
    npm = "npm.cmd" if os.name == "nt" else "npm"
    try:
        r = subprocess.run([npm, "pack", "--json"], cwd=str(NPM), capture_output=True, text=True, timeout=300)
    except FileNotFoundError:
        print("没装 npm，跳过 pack 验证（组装结果在 npm/skill/）")
        return
    if r.returncode != 0:
        print("npm pack 失败：")
        print((r.stderr or r.stdout)[-2000:])
        return
    try:
        info = json.loads(r.stdout)
        for it in info:
            print("生成 %s：%.2f MB（解包后 %.2f MB，%d 个文件）" % (
                it.get("filename"), it.get("size", 0) / 1048576,
                it.get("unpackedSize", 0) / 1048576, it.get("entryCount", 0)))
    except Exception:
        print(r.stdout[-1000:])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-pack", action="store_true", help="只组装，不跑 npm pack")
    a = ap.parse_args()
    build()
    if not a.no_pack:
        pack()
    print("\n发布：cd npm && npm publish --access public")


if __name__ == "__main__":
    main()
