# -*- coding: utf-8 -*-
r"""把老项目里的 `即梦上传/` 改成新名 `平台上传/`（2026-09-17 用户要求"改成平台上传"）。

程序本来就**两个名字都认**（`project_core.upload_dir()`），所以这个迁移是"想统一就跑一次"，不跑也不影响用。
**只做重命名**（`os.rename`），不改文件内容、不删东西——想退回把名字改回来即可。

用法：
    python tools\迁移上传夹.py --root "F:\AI创作\提示词skill生成尝试"      # 只看要改哪些（默认不改）
    python tools\迁移上传夹.py --root "<样本库根>" --yes                  # 真改
    python tools\迁移上传夹.py --root "<样本库根>" --yes --dry-run        # 同 --dry-run 只预览
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

NEW = "平台上传"
OLD = "即梦上传"


def scan(root):
    """返回 [(项目目录, 旧夹路径, 新夹路径, 会不会撞名)]。"""
    out = []
    for name in sorted(os.listdir(root)):
        pdir = os.path.join(root, name)
        if not os.path.isdir(pdir) or name.startswith("_"):
            continue
        old = os.path.join(pdir, OLD)
        new = os.path.join(pdir, NEW)
        if os.path.isdir(old):
            out.append((pdir, old, new, os.path.exists(new)))
    return out


def main():
    ap = argparse.ArgumentParser(description="把 即梦上传/ 重命名为 平台上传/")
    ap.add_argument("--root", required=True, help="样本库根目录（里面每个子目录＝一个项目）")
    ap.add_argument("--yes", action="store_true", help="真的改；不加只预览")
    ap.add_argument("--dry-run", action="store_true", help="只预览（和默认一样）")
    a = ap.parse_args()
    root = os.path.abspath(a.root)
    if not os.path.isdir(root):
        print("目录不存在：%s" % root)
        return 2
    rows = scan(root)
    if not rows:
        print("没有需要改的（%s 下已经没有 %s/ 了）" % (root, OLD))
        return 0
    print("要改 %d 个项目：" % len(rows))
    for pdir, old, new, clash in rows:
        flag = "  ⚠ 目标已存在（跳过）" if clash else ""
        print("  %-28s %s → %s%s" % (os.path.basename(pdir), OLD, NEW, flag))
    do = [r for r in rows if not r[3]]
    if not (a.yes and not a.dry_run):
        print("\n（预览模式：没有改动。确认后加 --yes 执行）")
        return 0
    ok = fail = 0
    for pdir, old, new, _clash in do:
        try:
            os.rename(old, new)
            ok += 1
        except OSError as e:
            fail += 1
            print("  [失败] %s：%s" % (os.path.basename(pdir), e))
    print("\n改完：成功 %d · 失败 %d（共 %d 个待改）" % (ok, fail, len(do)))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
