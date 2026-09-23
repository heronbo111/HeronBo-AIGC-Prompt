# -*- coding: utf-8 -*-
"""Skill 安装向导：装之前**先问清楚两件事**——skill 装哪、项目（样本库）放哪。

为什么要有它（2026-09-22 用户反馈两件事）：
1. 「让别的电脑安装 skill 时速度太慢（通过 gitee 仓库安装）」——慢有三个来源：
   ① 完整 clone 拉了全部历史（仓库历史里有历代 exe，几十 MB 起）→ 一律 `--depth 1`；
   ② 新机没装 git → 直接从 gitee 下 zip 源码包（浏览器/PowerShell 就能下，不用 git）；
   ③ 装完拉 300MB 大件走 GitHub Release，国内裸连很慢 → 部署脚本已带镜像换源
     （见 部署.py 的 VENDOR_MIRRORS），这里不再重复。
2. 「并没有询问用户要把该 skill 与项目装在哪个位置」——本脚本第一件事就是问，
   默认值按你本机装的 agent harness 给（ZCode / WorkBuddy / Codex / DSH）。

用法（在解压好的仓库目录里）：
    python 安装向导.py                 # 交互式：问位置 → 就位 → 可选接去部署
    python 安装向导.py --dir "D:\\技能库\\HeronBo-AIGC-Prompt" --root "D:\\样本库" --yes
    python 安装向导.py --list          # 只列出各 harness 的默认目录，不装

铁律（照仓库 AGENTS.md）：装任何东西前先问用户；--yes 才真动手，否则只打印计划。
"""
import argparse
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = "https://gitee.com/HeronBo/HeronBo-AIGC-Prompt.git"
ZIP_URL = "https://gitee.com/HeronBo/HeronBo-AIGC-Prompt/repository/archive/main.zip"
NAME = "HeronBo-AIGC-Prompt"

# 各 harness 的用户级技能目录（存在哪个就推荐哪个；都没有就推荐第一个存在的，再不行 ~ 下新建）
HARNESS_DIRS = [
    ("ZCode 客户端", os.path.join(os.path.expanduser("~"), ".zcode", "skills")),
    ("WorkBuddy", os.path.join(os.path.expanduser("~"), ".workbuddy", "skills")),
    ("Codex CLI", os.path.join(os.path.expanduser("~"), ".codex", "skills")),
    ("DeepSeek Harness（DSH）", os.path.join(os.path.expanduser("~"), ".dsh", "skills")),
    # 2026-09-23 加：豆包（个人版）的用户技能目录；豆包工作（企业版）的用户技能在它的
    # Electron profile 里（`.user_skills`）——同一个目录联接套路，改一处四处同步。
    ("豆包", os.path.join(os.path.expanduser("~"), "Doubao", "skills")),
    ("豆包工作", os.path.join(os.environ.get("LOCALAPPDATA", ""), "DoubaoWork", "User Data",
                              "Default", ".doubaowork", "agent_mode", "workspace", ".user_skills")),
]


def log(m):
    print(m, flush=True)


def ask_dir(title, default):
    """问一个目录；回车 = 用默认。空字符串返回 \"\"。"""
    while True:
        log("")
        log(title)
        log("  回车 = %s" % (default or "（无默认，必须填）"))
        v = input("  位置> ").strip().strip('"')
        if not v:
            v = default
        if v:
            return os.path.abspath(os.path.expanduser(v))
        log("  （要填一个位置，或直接回车用默认）")


def ask_yesno(title, default_yes=False):
    d = "Y/n" if default_yes else "y/N"
    v = input("  %s [%s]> " % (title, d)).strip().lower()
    if not v:
        return default_yes
    return v in ("y", "yes", "1", "是")


def pick_target():
    """让用户选 skill 装哪：列出各 harness 默认目录 + 自定义。"""
    log("== 第 1 问：skill 装到哪？==")
    opts = []
    for label, d in HARNESS_DIRS:
        mark = "（本机装了这个）" if os.path.isdir(d) else ""
        opts.append((label, os.path.join(d, NAME), bool(mark)))
    i = 0
    for label, full, live in opts:
        i += 1
        log("  %d. %s → %s%s" % (i, label, full, "  ← 推荐（本机装了它）" if live else ""))
    i += 1
    log("  %d. 自定义位置（装在别处，之后再决定要不要链接给某个 agent）" % i)
    v = input("  选几号（回车 = 第一个本机装了的，没有就 1）> ").strip()
    n = int(v) if v.isdigit() and 1 <= int(v) <= len(opts) + 1 else 0
    if n == 0:
        n = next((k + 1 for k, o in enumerate(opts) if o[2]), 1)
    if n <= len(opts):
        return opts[n - 1][1]
    return ask_dir("  自定义位置：", os.path.join(os.path.expanduser("~"), "skills", NAME))


def is_repo(d):
    return os.path.isfile(os.path.join(d, "SKILL.md"))


def install_mode():
    """当前目录是不是已经下载好的仓库：是 → 只搬位置；不是 → 先下载。"""
    if is_repo(HERE):
        return "here"
    # 也许用户在仓库的 tools/ 里跑的
    up = os.path.dirname(HERE)
    if is_repo(up):
        return "here:" + up
    return "download"


def download(dst_parent):
    """浅克隆优先；没 git 就直接下 gitee 的 zip 源码包（不用 git）。"""
    dst = os.path.join(dst_parent, NAME)
    if shutil.which("git"):
        log("  浅克隆（--depth 1，只要最新一层，约 13MB）…")
        r = subprocess.run(["git", "clone", "--depth", "1", REPO, dst],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode == 0 and is_repo(dst):
            return dst
        log("  克隆没成（%s），改走 zip 直包…" % (r.stderr or r.stdout or "").strip()[:120])
    import urllib.request
    import zipfile
    zip_dst = dst + ".zip"
    log("  下 gitee 源码包（约 13MB，不用 git）…")
    urllib.request.urlretrieve(ZIP_URL, zip_dst)
    with zipfile.ZipFile(zip_dst) as z:
        names = z.namelist()
        root = names[0].split("/")[0] if names else "HeronBo-AIGC-Prompt"
        z.extractall(dst_parent)
    os.remove(zip_dst)
    got = os.path.join(dst_parent, root)
    if got != dst:
        os.rename(got, dst)
    return dst


def move_here2(src, dst):
    """把 src 整目录挪到 dst（同盘挪目录，跨盘 copy+删源——都是整目录，秒级）。"""
    if os.path.abspath(src) == os.path.abspath(dst):
        return src
    if os.path.exists(dst):
        bak = dst + ".旧"
        os.rename(dst, bak)
        log("  目标原本有东西，已挪成备份：%s" % bak)
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    try:
        shutil.move(src, dst)
    except OSError:
        shutil.copytree(src, dst)
        shutil.rmtree(src, ignore_errors=True)
    return dst


def link_to_others(installed):
    """装到一个 harness 目录后，问要不要给其它 harness 也链一份（junction，不占空间）。"""
    real = os.path.realpath(installed).lower()
    others = []
    for label, d in HARNESS_DIRS:
        cand = os.path.join(d, NAME)
        if os.path.isdir(d) and os.path.realpath(cand).lower() != real:
            others.append((label, cand))
    if not others:
        return
    log("")
    log("== 要不要给其它 agent 也链一份？（Windows 目录联接，不占第二份空间，改一处全同步）==")
    for k, (label, d) in enumerate(others, 1):
        log("  %d. %s → %s" % (k, label, d))
    v = input("  选几号（可只填一个；回车 = 跳过）> ").strip()
    if not v.isdigit() or not (1 <= int(v) <= len(others)):
        return
    dst = others[int(v) - 1][1]
    if os.path.exists(dst):
        log("  [跳过] %s 已存在" % dst)
        return
    try:
        import _winapi
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        _winapi.CreateJunction(installed, dst)
        log("  [OK] 已链接 %s → %s" % (dst, installed))
    except Exception as e:                                   # noqa: BLE001
        log("  [没成] %s（%s）；手动：cmd 里 mklink /J \"%s\" \"%s\"" % (dst, e, dst, installed))


def main():
    ap = argparse.ArgumentParser(description="Skill 安装向导（先问位置，再就位）")
    ap.add_argument("--dir", help="直接指定 skill 安装位置（跳过第 1 问）")
    ap.add_argument("--root", help="直接指定项目（样本库）根目录（跳过第 2 问）")
    ap.add_argument("--yes", action="store_true", help="真动手（不加只打印计划）")
    ap.add_argument("--list", action="store_true", help="只列出各 harness 默认目录")
    a = ap.parse_args()
    if a.list:
        for label, d in HARNESS_DIRS:
            log("%-24s %s" % (label, os.path.join(d, NAME)))
        return 0
    log("HeronBo-AIGC-Prompt · 安装向导")
    log("（装任何东西前先问你；不加 --yes 只打印计划不动手）")
    mode = install_mode()
    target = a.dir and os.path.abspath(os.path.expanduser(a.dir)) or pick_target()
    log("")
    log("== 第 2 问：项目（样本库）放哪？==")
    log("  这是你以后放各视频项目素材与产出的工作根目录（不是 skill 本体）。")
    default_root = os.path.join(os.path.splitdrive(target)[0] + os.sep, "AI创作", "视频项目")
    root = (a.root and os.path.abspath(os.path.expanduser(a.root))) or \
        ask_dir("  样本库根目录：", default_root)
    log("")
    log("== 计划 ==")
    if mode == "download":
        log("  1. 下载 skill（浅克隆优先，没 git 走 gitee zip 直包）→ %s" % target)
    elif mode.startswith("here"):
        log("  1. 当前这份仓库就位（挪/留在）→ %s" % target)
    log("  2. 样本库根设为 %s" % root)
    log("  3. （可选）链接给本机其它 agent harness")
    log("  4. （可选）接着跑 python tools\\部署.py all --yes（装依赖/接通道/快捷方式/弹工作台）")
    if not a.yes:
        log("")
        log("只是计划（没动手）。确认无误就加 --yes 再跑一次。")
        return 0
    if mode == "download":
        os.makedirs(os.path.dirname(target), exist_ok=True)
        installed = download(os.path.dirname(target))
    elif mode.startswith("here"):
        src = mode.split(":", 1)[1] if ":" in mode else HERE
        if os.path.abspath(src) == os.path.abspath(target):
            installed = src
            log("  1. 已在目标位置，不用挪：%s" % src)
        else:
            installed = move_here2(src, target)
    else:
        installed = target
    # 样本库根：写进 skill 的 paths.local（不进 git 的机器级配置）
    try:
        p = os.path.join(installed, "references", "paths.local.md")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        line = "SAMPLES_ROOT: %s\n" % root
        old = ""
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as f:
                old = f.read()
        if "SAMPLES_ROOT" in old:
            import re
            old = re.sub(r"SAMPLES_ROOT:.*", line.strip(), old)
            with open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write(old)
        else:
            with open(p, "a", encoding="utf-8", newline="\n") as f:
                f.write(line)
        log("  2. [OK] 样本库根已写入 %s" % p)
    except OSError as e:
        log("  2. [没成] 样本库根没写上（%s）；装完自己改 references/paths.local.md" % e)
    link_to_others(installed)
    dep = os.path.join(installed, "tools", "部署.py")
    if os.path.isfile(dep):
        log("")
        if ask_yesno("== 现在就接着跑部署（装依赖/接 agent 通道/快捷方式/弹工作台）？"):
            subprocess.run([sys.executable, dep, "all", "--yes", "--root", root])
            return 0
        log("  以后再跑：python \"%s\" all --yes --root \"%s\"" % (dep, root))
    log("")
    log("装好了：%s" % installed)
    log("下一步：对你的 agent 说「读取 %s\\SKILL.md 并严格按其工作流执行」" % installed)
    return 0


def move_here2(src, dst):
    """把 src 整目录挪到 dst（move_here 的两参数版；交互确认在调用方）。"""
    if os.path.exists(dst):
        bak = dst + ".旧"
        os.rename(dst, bak)
        log("  旧目录已备份为 %s" % bak)
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    try:
        shutil.move(src, dst)
    except OSError:
        shutil.copytree(src, dst)
        shutil.rmtree(src, ignore_errors=True)
    return dst


if __name__ == "__main__":
    sys.exit(main())
