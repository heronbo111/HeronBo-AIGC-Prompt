# -*- coding: utf-8 -*-
r"""一键发布：扫 → 提交 → 推 gitee + GitHub → 校验。

为什么要有它：
- 本仓库是**多 agent 共享工作树**（见 AGENTS.md）：不许 `git add -A`、只提交自己改的文件、
  改完立刻提交并推双远程。手工敲容易漏步骤，尤其容易**漏推 GitHub**（Gitee 通了就当推完了）。
- 2026-09-17 起仓库只放代码（exe/大件走 Release 附件），所以推送很便宜，可以频繁推。

用法（在仓库根或 tools 下都行）：

    python tools\推送.py --dry --msg "试跑" README.md          # 只体检：扫词表、看会提交什么，不提交不推
    python tools\推送.py --msg "feat: 工作台动作流按环境自动适配" tools/agent_trace.py
    python tools\推送.py --msg "fix: 动作流读不到过程" --changed   # 自动收集**已跟踪文件**的改动
    python tools\推送.py --msg "..." --no-github 文件…         # 只推 gitee（GitHub 挂了时用）

提交信息写法（2026-09-21 起，对齐公开仓库如 clash-verge-rev 的版本说明）：
    标题 `类型: 一句话`（feat / fix / docs / ui / chore / refactor），讲**对使用者有什么用**；
    需要展开时用 `✨ 新增功能` / `🐞 修复问题` / `🚀 优化改进` 三段，每条以「新增…／修复…／优化…」开头。
    **公共历史里不写过程信息**（改了哪个源码文件、补交某文件、协作登记、测试怎么跑）；
    完整版本叙事写 CHANGELOG.md，提交信息是它的浓缩版。详见 AGENTS.md 铁律第 4 条。

它做的事：
1. 前置检查：分支是不是 main、工作树里有没有**别人未提交的改动**（有就警告，不碰）
2. 收文件清单（必须显式给，或 `--changed` 自动收已跟踪的改动）
3. 敏感词扫描：**两个远程都公开，所以都扫**（词表直接读 pre-push 钩子，保持唯一真相源）
4. 体量检查：>5MB 的二进制/视频拦下（AGENTS.md 规则 8）
5. 提交（提交信息按公开仓库写法：标题「类型: 一句话」+ 可选的 ✨/🐞/🚀 三段；不加 agent 前缀）
6. push gitee → push github → 用 `ls-remote` 逐个校验远程 sha == 本地 HEAD
"""
import argparse
import io
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


def git_status_entries():
    """`git status` 的条目列表，**不 strip、不转义**。

    ⚠️ 两个坑（2026-09-21 一次 dry 里同时踩到）：
      · `sh()` 会对整段输出 `.strip()` —— 状态位是两列（如 `" M 路径"`），整段一 strip
        **第一行的前导空格就没了**，`line[3:]` 取到的路径名少一个字符 → 列表第一条永远
        匹配不上真实文件名（表现：只有第一个文件显示"不动"）。
      · 默认 git 会把**中文文件名**转义成 `"tools\\346\\216\\250..."`，和真实路径比永远不等
        → 中文名文件被静默跳过、提交不上去。
    修法一次解决两条：用 `--porcelain -z`（NUL 分隔 = 不 strip 也不会有换行粘连）＋
    `core.quotepath=false`（中文名原样输出）。
    """
    p = subprocess.run(["git", "-c", "core.quotepath=false", "status", "--porcelain", "-z"],
                       cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return [x for x in (p.stdout or "").split("\0") if x]


def status_path(entry):
    """一条 porcelain 条目 → (状态位, 路径)。`XY 路径` 的两列状态位后跟一个空格。"""
    st = entry[:2]
    path = entry[3:] if len(entry) > 3 else ""
    return st, path.replace("\\", "/")


def blocked_words():
    """敏感词表：**只从本机文件读**，任何进仓库的文件里都不留词表副本。

    为什么：词表里全是敏感词本身，写进版本化文件 = 那个文件自己就会被扫描命中、
    永远推不上去（本脚本第一版、以及把钩子纳入仓库那次，都这么翻车过）。
    所以词表放 `.git/hooks/敏感词.local.txt`（.git 内的文件天生不进仓库）。
    """
    path = os.path.join(ROOT, ".git", "hooks", "敏感词.local.txt")
    if os.path.isfile(path):
        out = []
        for line in io.open(path, encoding="utf-8", errors="replace"):
            w = line.strip()
            if w and not w.startswith("#") and w not in out:
                out.append(w)
        return out, "来自 .git/hooks/敏感词.local.txt（本机私有）"
    return [], "!! 没有本机词表（--install-hook 可生成），本次不扫词"


def git_push_via_proxy(remote):
    """直推失败后，改走本机 Clash 的 7897 再试一次（只对 github 用）。

    为什么要它：这台机器的环境变量里挂着别的代理（实测 127.0.0.1:9378），
    github 走它必失败（schannel: server closed abruptly / CONNECT tunnel failed 502），
    而显式指定 Clash 的 7897 就通。代理只在这一条命令里生效，不改全局配置。
    """
    if remote != "github":
        return False
    import socket
    s = socket.socket()
    s.settimeout(1.5)
    try:
        s.connect(("127.0.0.1", 7897))
    except OSError:
        return False
    finally:
        s.close()
    env = dict(os.environ)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
              "ALL_PROXY", "all_proxy"):
        env.pop(k, None)                      # 先摘掉坏代理，再用 -c 指定好的
    p = subprocess.run(["git", "-c", "http.proxy=http://127.0.0.1:7897",
                        "-c", "https.proxy=http://127.0.0.1:7897",
                        "push", remote, "main"],
                       cwd=ROOT, env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return p.returncode == 0


def changed_tracked():
    """已跟踪文件的改动（不含未跟踪的新文件——按 AGENTS.md 不碰别人的新文件）。

    ⚠️ 必须带 `-c core.quotepath=false`（2026-09-21 实测踩到）：git 默认会给**中文文件名**
    输出成 `"tools/\346\216\250\351\200\201.py"` 这种八进制转义，脚本拿去和真实路径比对
    永远对不上 → 中文名文件被**静默跳过、提交不上去**（`推送.py`、`docs/动作流通道.md`
    这类名字首当其冲）。
    """
    out = set()
    for args in (("diff", "--name-only"), ("diff", "--cached", "--name-only")):
        for line in (git("-c", "core.quotepath=false", *args) or "").splitlines():
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
    ap.add_argument("--msg", help="提交信息：标题 `类型: 一句话` + 可选的 ✨/🐞/🚀 三段（见文件头说明）")
    ap.add_argument("--changed", action="store_true", help="自动收集已跟踪文件的改动")
    ap.add_argument("--dry", action="store_true", help="只体检，不提交不推送")
    ap.add_argument("--no-github", action="store_true", help="只推 gitee")
    ap.add_argument("--note", default="", help="提交正文末尾的来源/说明")
    ap.add_argument("--install-hook", action="store_true",
                    help="把仓库里的 tools/git-hooks/pre-push 装到 .git/hooks/")
    a = ap.parse_args()

    os.chdir(ROOT)
    print("仓库：%s" % ROOT)

    # 防护钩子：仓库里有一份版本化的，本机 .git/hooks 里的可能没有/是旧的
    src_hook = os.path.join(ROOT, "tools", "git-hooks", "pre-push")
    dst_hook = os.path.join(ROOT, ".git", "hooks", "pre-push")
    same = False
    if os.path.isfile(src_hook) and os.path.isfile(dst_hook):
        try:
            same = open(src_hook, "rb").read() == open(dst_hook, "rb").read()
        except OSError:
            same = False
    if a.install_hook:
        if os.path.isfile(src_hook):
            import shutil as _sh
            _sh.copy2(src_hook, dst_hook)
            try:
                os.chmod(dst_hook, 0o755)
            except OSError:
                pass
            words_path = os.path.join(ROOT, ".git", "hooks", "敏感词.local.txt")
            if not os.path.isfile(words_path):
                io.open(words_path, "w", encoding="utf-8", newline="\n").write(
                    "# 公开发布敏感词表（本机私有：.git 内的文件不进仓库）\n"
                    "# 一行一个词，# 开头为注释。把你要防的品牌/书名/人名/本机路径逐行写上。\n\n")
                print("已建空的词表：.git/hooks/敏感词.local.txt（请把敏感词逐行填进去）")
            print("已装/更新防护钩子：.git/hooks/pre-push")
            same = True
    elif not os.path.isfile(dst_hook):
        print("提醒：本机没装防护钩子（本仓库两个远程都公开）。装：--install-hook")
    elif not same:
        print("提醒：防护钩子与本机版本不一致（仓库里有新版）。更新：--install-hook")
    br = git("rev-parse", "--abbrev-ref", "HEAD")
    if br != "main":
        print("!! 当前分支是 %s（本仓库约定在 main 上发布）" % br)

    files = list(a.files)
    if a.changed:
        files += [f for f in changed_tracked() if f not in files]
    files = [f.replace("\\", "/") for f in files]

    entries = git_status_entries()
    print("\n[1] 工作树现状（%d 条改动）" % len(entries))
    for entry in entries[:40]:
        st, path = status_path(entry)
        mark = "→ 本次提交" if path in files else "  （不动）"
        print("    %-3s %s %s" % (st.strip() or st, path, mark))

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
        cmd = ["git", "push", r, "main"]
        p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        tail = ((p.stdout or "") + (p.stderr or "")).strip().splitlines()
        if p.returncode != 0:
            # ★ 本机到 GitHub 会被「环境变量里挂着的其它代理」搞坏
            # （实测 HTTP_PROXY=http://127.0.0.1:9378 下 github 必失败：
            #  schannel: server closed abruptly / CONNECT tunnel failed 502；
            #  显式改走 Clash 的 7897 就通）。所以失败后自动重试一次带代理的推送。
            proxied = git_push_via_proxy(r)
            if proxied:
                print("    %s 已推（经本机 Clash 代理重试成功）" % r)
            else:
                ok_all = False
                why = tail[-1] if tail else "未知"
                print("    %s 推送失败：%s" % (r, why))
                if "防护" in (p.stderr or ""):
                    print("       （防护钩子拦下了 → 先按上面清单扫敏感词）")
        else:
            print("    %s 已推" % r)

    print("\n[6] 校验（只信 ls-remote）")
    local = git("rev-parse", "HEAD")
    unverified = []
    for r in remotes:
        sha = ""
        for _try in range(3):                      # 本机到 GitHub 会抽风，重试三次
            parts = git("ls-remote", r, "refs/heads/main").split()
            if parts:
                sha = parts[0]
                break
        if not sha:
            # 网络没通 ≠ 推送失败：这里必须说清楚，否则会被读成"没推上去"
            print("    %-7s 未校验（网络/代理问题，不代表推送失败）" % r)
            unverified.append(r)
            continue
        if sha == local:
            print("    %-7s %s  OK" % (r, sha[:12]))
        else:
            print("    %-7s %s  !! 与本地 %s 不一致（需要补推）" % (r, sha[:12], local[:12]))
            ok_all = False
    for r in unverified:
        print("    稍后自查：git ls-remote %s refs/heads/main   # 应为 %s" % (r, local[:12]))
    if unverified and ok_all:
        print("\n推送已完成；%s 只是没校验上（网络），稍后照上面命令自查即可。" % "/".join(unverified))
    else:
        print("\n%s" % ("发布完成。" if ok_all else "有环节没成功，按上面提示处理。"))
    return 0 if ok_all else 6


if __name__ == "__main__":
    sys.exit(main())
