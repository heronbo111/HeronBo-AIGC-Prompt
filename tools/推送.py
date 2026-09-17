# -*- coding: utf-8 -*-
r"""一键发布：扫 → 提交 → 推 gitee + GitHub → 校验。

为什么要有它：
- 本仓库是**多 agent 共享工作树**（见 AGENTS.md）：不许 `git add -A`、只提交自己改的文件、
  改完立刻提交并推双远程。手工敲容易漏步骤，尤其容易**漏推 GitHub**（Gitee 通了就当推完了）。
- 2026-09-17 起仓库只放代码（exe/大件走 Release 附件），所以推送很便宜，可以频繁推。

用法（在仓库根或 tools 下都行）：

    python tools\推送.py --dry --msg "试跑" README.md          # 只体检：扫词表、看会提交什么，不提交不推
    python tools\推送.py --msg "工作台：②栏加了拖入体积提示" tools/workbench/app.js README.md
    python tools\推送.py --msg "修 xxx" --changed              # 自动收集**已跟踪文件**的改动
    python tools\推送.py --msg "..." --no-github 文件…         # 只推 gitee（GitHub 挂了时用）

它做的事：
1. 前置检查：分支是不是 main、工作树里有没有**别人未提交的改动**（有就警告，不碰）
2. 收文件清单（必须显式给，或 `--changed` 自动收已跟踪的改动）
3. 敏感词扫描：**两个远程都公开，所以都扫**（词表直接读 pre-push 钩子，保持唯一真相源）
4. 体量检查：>5MB 的二进制/视频拦下（AGENTS.md 规则 8）
5. 提交（提交信息面向用户，不加 agent 前缀；来源写在正文末尾）
6. push gitee → push github → 用 `ls-remote` 逐个校验远程 sha == 本地 HEAD
"""
import argparse
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BIG = 5 * 1024 * 1024


def sh(cmd, cwd=ROOT, check=False):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if check and p.returncode != 0:
        raise SystemExit("命令失败：%s\n%s" % (" ".join(cmd), (p.stderr or "").strip()))
    return (p.stdout or "").strip()


def git(*a, **kw):
    return sh(["git"] + list(a), **kw)


def blocked_words():
    """敏感词表：**只从 pre-push 钩子读**，脚本里不留词表副本。

    为什么不内置兜底词表：词表里全是敏感词本身，写进脚本 = 脚本自己就会命中扫描，
    推送时也会被钩子拦下（本脚本第一版就这么翻车过）。钩子是唯一真相源，
    它不在（换了机器/被删）就明确告警并跳过，让推送时的钩子去兜底。
    """
    hook = os.path.join(ROOT, ".git", "hooks", "pre-push")
    try:
        txt = open(hook, encoding="utf-8", errors="replace").read()
    except OSError:
        return [], "!! 找不到 .git/hooks/pre-push，取不到词表（本次不扫词）"
    words = re.findall(r"-e\s+'([^']+)'", txt)
    seen, out = set(), []
    for w in words:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out, "来自 .git/hooks/pre-push（唯一真相源）"


def changed_tracked():
    """已跟踪文件的改动（不含未跟踪的新文件——按 AGENTS.md 不碰别人的新文件）。"""
    out = set()
    for line in (git("diff", "--name-only") or "").splitlines():
        out.add(line.strip())
    for line in (git("diff", "--cached", "--name-only") or "").splitlines():
        out.add(line.strip())
    return sorted(x for x in out if x)


def scan(paths, words):
    """按行扫敏感词。返回 [(文件, 行号, 词, 行内容)]。"""
    hits = []
    for rel in paths:
        fp = os.path.join(ROOT, rel)
        if not os.path.isfile(fp):
            continue
        try:
            if os.path.getsize(fp) > 4 * 1024 * 1024:
                continue
            with open(fp, encoding="utf-8", errors="ignore") as f:
                for i, line in enumerate(f, 1):
                    for w in words:
                        if w in line:
                            hits.append((rel, i, w, line.strip()[:110]))
                            break
        except OSError:
            continue
    return hits


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("files", nargs="*", help="要提交的文件（相对仓库根）")
    ap.add_argument("--msg", help="提交信息（面向用户，写清改了什么、有什么影响）")
    ap.add_argument("--changed", action="store_true", help="自动收集已跟踪文件的改动")
    ap.add_argument("--dry", action="store_true", help="只体检，不提交不推送")
    ap.add_argument("--no-github", action="store_true", help="只推 gitee")
    ap.add_argument("--note", default="", help="提交正文末尾的来源/说明")
    a = ap.parse_args()

    os.chdir(ROOT)
    print("仓库：%s" % ROOT)
    br = git("rev-parse", "--abbrev-ref", "HEAD")
    if br != "main":
        print("!! 当前分支是 %s（本仓库约定在 main 上发布）" % br)

    files = list(a.files)
    if a.changed:
        files += [f for f in changed_tracked() if f not in files]
    files = [f.replace("\\", "/") for f in files]

    dirty = git("status", "--short").splitlines()
    print("\n[1] 工作树现状（%d 条改动）" % len(dirty))
    for line in dirty[:40]:
        mark = "→ 本次提交" if line[3:].strip().replace("\\", "/") in files else "  （不动）"
        print("    %s %s" % (line, mark))

    if not files:
        print("\n没有指定要提交的文件。加文件清单，或用 --changed 自动收已跟踪改动。")
        return 0

    print("\n[2] 敏感词扫描（两个远程都公开 → 都按公开标准扫）")
    words, src = blocked_words()
    print("    词表 %d 个，%s" % (len(words), src))
    hits = scan(files, words)
    if hits:
        print("    !! 命中 %d 处，先改成中性说法再推：" % len(hits))
        for rel, ln, w, ctx in hits:
            print("       %s:%d  「%s」  %s" % (rel, ln, w, ctx))
        return 2
    print("    OK，无命中")

    print("\n[3] 体量检查（>5MB 的二进制不进仓库，走 Release 附件）")
    big = [(f, os.path.getsize(os.path.join(ROOT, f))) for f in files
           if os.path.isfile(os.path.join(ROOT, f))
           and os.path.getsize(os.path.join(ROOT, f)) > BIG]
    for f, n in big:
        print("    !! %s  %.1f MB" % (f, n / 1048576.0))
    if big:
        print("    这些不该进 git。大件上传到 GitHub Release，再让 部署.py vendor --fetch 拉。")
        return 3
    print("    OK")

    if a.dry:
        print("\n[dry] 到这里为止（没有提交、没有推送）。")
        return 0

    if not a.msg:
        print("\n!! 提交要写 --msg \"改了什么、有什么影响\"")
        return 4

    print("\n[4] 提交")
    git("add", "--", *files, check=True)
    body = a.msg if not a.note else "%s\n\n%s" % (a.msg, a.note)
    p = subprocess.run(["git", "commit", "-m", body], cwd=ROOT, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        print("    提交失败：%s" % ((p.stdout or "") + (p.stderr or "")).strip()[:400])
        return 5
    head = git("rev-parse", "--short", "HEAD")
    print("    已提交 %s" % head)

    print("\n[5] 推送")
    remotes = ["gitee"] if a.no_github else ["gitee", "github"]
    ok_all = True
    for r in remotes:
        p = subprocess.run(["git", "push", r, "main"], cwd=ROOT, capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
        tail = ((p.stdout or "") + (p.stderr or "")).strip().splitlines()
        if p.returncode != 0:
            ok_all = False
            print("    %s 推送失败：%s" % (r, tail[-1] if tail else "未知"))
            if r == "gitee" and "防护" in (p.stderr or ""):
                print("       （防护钩子拦下了 → 先按上面清单扫敏感词）")
        else:
            print("    %s 已推" % r)

    print("\n[6] 校验（只信 ls-remote）")
    local = git("rev-parse", "HEAD")
    for r in remotes:
        remote = git("ls-remote", r, "refs/heads/main").split()
        sha = remote[0] if remote else "(空)"
        flag = "OK" if sha == local else "!! 不一致"
        print("    %-7s %s  %s" % (r, sha[:12], flag))
        if sha != local:
            ok_all = False
    print("\n%s" % ("发布完成。" if ok_all else "有环节没成功，按上面提示处理。"))
    return 0 if ok_all else 6


if __name__ == "__main__":
    sys.exit(main())
