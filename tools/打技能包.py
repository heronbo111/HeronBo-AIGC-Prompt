#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""打「只给 agent 用」的技能包，不夹带 GUI 运行件、样例、开发文档和构建缓存。

解压后得到一个标准 skill 目录，agent 读 SKILL.md 就能用；
需要安装时跑 install_skill.py --auto / --dir <目标目录>。

用法：
    python tools\打技能包.py
    python tools\打技能包.py --no-zip
    python tools\打技能包.py --out D:\某处\HeronBo-AIGC-Prompt-skill.zip
"""
from __future__ import annotations

import argparse
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

from skill_manifest import NAME, ROOT, selected_files


def read_version() -> str:
    text = (ROOT / "SKILL.md").read_text(encoding="utf-8", errors="replace")
    m = re.search(r"^version:\s*([0-9.]+)\s*$", text, re.M)
    return m.group(1) if m else "dev"


def build(out_zip: Path | None, keep_stage: bool = False) -> Path:
    ver = read_version()
    stage = Path(tempfile.mkdtemp(prefix="heronbo-skill-"))
    dst_root = stage / NAME
    dst_root.mkdir(parents=True)
    n = 0
    size = 0
    for rel in selected_files():
        src = ROOT / rel
        dst = dst_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        n += 1
        size += src.stat().st_size

    (dst_root / "安装说明.txt").write_text(
        "这是给 agent 安装的技能包。\n"
        "解压后运行：python install_skill.py --auto\n"
        "指定目录：python install_skill.py --dir <目标目录>\n"
        "安装后让 agent 读取 <目标目录>/SKILL.md 并按其工作流执行。\n",
        encoding="utf-8",
    )
    n += 1
    size += (dst_root / "安装说明.txt").stat().st_size

    print("技能包组装：%d 个文件，%.2f MB（版本 %s）" % (n, size / 1048576, ver))
    if out_zip is None:
        out_zip = ROOT.parent / "_安装包" / ("%s-skill-v%s.zip" % (NAME, ver))
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    if out_zip.exists():
        out_zip.unlink()
    with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=6) as z:
        for f in sorted(dst_root.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(stage).as_posix())
    print("已生成：%s（%.2f MB）" % (out_zip, out_zip.stat().st_size / 1048576))
    if keep_stage:
        print("staging：%s" % stage)
    else:
        shutil.rmtree(stage, ignore_errors=True)
    return out_zip


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--no-zip", action="store_true", help="只组装不压缩（仍会生成 staging）")
    ap.add_argument("--keep-stage", action="store_true")
    a = ap.parse_args()
    out = None if a.no_zip else a.out
    build(out, keep_stage=a.keep_stage or a.no_zip)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
