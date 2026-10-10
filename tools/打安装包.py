# -*- coding: utf-8 -*-
r"""打一个「一键安装 skill 包」：其它机器上的 agent / 人拿到 zip 就能装。

与 打市场包.py 的分工：
    打市场包.py  → WorkBuddy 技能市场用：≤3MB、纯文本、两级目录、市场 frontmatter；
    本脚本       → 直发用（发消息 / 网盘 / Release 附件）：完整仓库跟踪文件
                   （SKILL.md + references + tools 全部脚本 + samples 示例 + 安装器），
                   体积 ≈1MB，解压后跑 一键安装skill.cmd / python install_skill.py 即装。

内容来源是 **git archive**（只取 git 跟踪的文件，_vendor / dist / workbench 源码 /
*.local.* 这些本机大件与私有件天然进不来），所以：
  · 装 skill 不需要 workbench（它是完整安装包的事）；
  · tools 里的界面/打包脚本虽在包里但用不到，留着无妨（<200KB，换来"包=仓库快照"）。

用法：
    python tools\\打安装包.py            # 产出 _stage/HeronBo-AIGC-Prompt-skill-v<版本>.zip
    python tools\\打安装包.py --out X    # 指定输出路径
"""
import argparse
import io
import os
import re
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 必须在包里的锚点文件（少一个都不许出货）
MUST_HAVE = [
    "SKILL.md",
    "install_skill.py",
    "安装向导.py",
    "一键安装skill.cmd",
    "references/rules.md",
    "references/prompt-templates.md",
    "references/platforms.md",
    "references/playbooks.md",
    "references/paths.md",
    "tools/首次配置.py",
    "tools/提示词体检.py",
    "tools/环境检查.py",
]
# 包里绝对不许出现的（本机取值 / 私有源码 / 大件 / 可执行）
FORBID_NAME = re.compile(r"(\.local\.|\.local$|_vendor|/dist/|workbench_server\.py|"
                         r"workbench/|\.exe$|\.dll$|\.onnx$|\.mp4$|\.whl$)", re.I)
# 这些 git 跟踪文件也刻意不进 skill 包（不是 forbid，是"跳过+计数"，见 SKIP_PASS）
SKIP_DIRS = ("tools/_vendor/", "tools/识别工具/")   # 离线依赖与识别模型：完整安装包/按需下载的事
SKIP_PASS = True
# 文本自检只拦"真实本机取值"；文档/注释里引用的历史路径（如 载荷同步.py 注释里的
# F:\AI创作 那条翻车记录）与 README 的 C:\Users\你的用户名\ 占位符都合法，故带行上下文再判
FORBID_TEXT = [
    ("疑似密钥", re.compile(r"(?i)(api[_-]?key|access[_-]?key|secret|password|cookie)\s*[:=]\s*[\"']?\S{8,}")),
]
# 这些行（按"行内容含此词"豁免）：注释/文档在讲历史教训，不是本机取值
PATH_ALLOW_WORDS = ("翻车", "教训", "历史", "曾把", "带进了安装包", "占位", "你的用户名",
                    "is-XXXX", "<安装目录>", "<profile>", "<用户名>")


def path_hit(text):
    """找文本里的本机绝对路径命中行，带豁免词判断；返回 [(行, 内容)]。"""
    hits = []
    for line in text.splitlines():
        m = re.search(r"[A-Za-z]:[\\/](AI创作|AI开发|工作台AI|Users[\\/](?!你的用户名)[^\\/\"\s<>]+)", line)
        if not m:
            continue
        if any(w in line for w in PATH_ALLOW_WORDS):
            continue
        hits.append((m.group(0), line.strip()[:90]))
    return hits


def read_version():
    src = io.open(os.path.join(ROOT, "SKILL.md"), encoding="utf-8").read()
    m = re.search(r"^version:\s*(\S+)", src, re.M)
    return m.group(1) if m else "0.0.0"


def git_files():
    r = subprocess.run(["git", "-C", ROOT, "ls-files", "-z"],
                       capture_output=True)
    if r.returncode != 0:
        raise SystemExit("git ls-files 失败：%s" % r.stderr.decode("utf-8", "replace"))
    names = [n for n in r.stdout.decode("utf-8", "replace").split("\0") if n]
    # 未跟踪的新文件也算进去（打"当前包"：刚写完还没提交的安装器/脚本要能进包）
    u = subprocess.run(["git", "-C", ROOT, "ls-files", "--others",
                        "--exclude-standard", "-z"], capture_output=True)
    if u.returncode == 0:
        names += [n for n in u.stdout.decode("utf-8", "replace").split("\0")
                  if n and not n.endswith("/")]
    return sorted(set(names))


def git_blob(name):
    """git show HEAD:<name>；未提交的新文件回退读工作树（打"当前包"是常态）。"""
    r = subprocess.run(["git", "-C", ROOT, "show", "HEAD:%s" % name],
                       capture_output=True)
    if r.returncode == 0:
        return r.stdout
    p = os.path.join(ROOT, name)
    if os.path.isfile(p):
        with open(p, "rb") as f:
            return f.read()
    return None


def main():
    ap = argparse.ArgumentParser(description="打一键安装 skill 包（git 跟踪文件全量）")
    ap.add_argument("--out", help="输出 zip 路径（默认 _stage/…-skill-v<版本>.zip）")
    a = ap.parse_args()
    ver = read_version()
    out = a.out or os.path.join(ROOT, "_stage",
                                "HeronBo-AIGC-Prompt-skill-v%s.zip" % ver)

    names = git_files()
    skipped = [n for n in names if n.replace("\\", "/").startswith(SKIP_DIRS)]
    names = [n for n in names if not n.replace("\\", "/").startswith(SKIP_DIRS)]
    # 未提交改动提醒（工作树有改动时，已跟踪文件仍取 HEAD；未跟踪新文件取工作树）
    dirty = subprocess.run(["git", "-C", ROOT, "status", "--porcelain"],
                           capture_output=True, text=True, encoding="utf-8").stdout
    dirty = [l for l in dirty.splitlines() if l.strip()]
    if dirty:
        print("⚠️ 工作树有 %d 处未提交改动——已跟踪文件取 HEAD（不含改动），未跟踪新文件取工作树。" % len(dirty))
        for l in dirty[:10]:
            print("   " + l)
        print("   先提交再打包最稳。\n")
    print("刻意跳过 %d 个（%s）——离线依赖与识别模型走完整安装包/按需下载" % (len(skipped), "、".join(SKIP_DIRS)))

    top = "HeronBo-AIGC-Prompt"
    bad = []
    n = 0
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name in names:
            if FORBID_NAME.search(name.replace("\\", "/")):
                bad.append("夹带不该进包的文件：%s" % name)
                continue
            data = git_blob(name)
            if data is None:
                bad.append("读不到 git 内容（刚提交的？）：%s" % name)
                continue
            if name.endswith((".md", ".py", ".cmd", ".txt", ".json", ".html", ".cfg", ".toml")):
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    bad.append("非 UTF-8 文本：%s" % name)
                    continue
                for tag, rx in FORBID_TEXT:
                    m = rx.search(text)
                    if m:
                        bad.append("%s（%s）：%s → %s" % (tag, m.group(0)[:40], name, "…"))
                for where, line in path_hit(text):
                    bad.append("本机路径（%s）：%s → %s" % (where, name, line))
            z.writestr(top + "/" + name.replace("\\", "/"), data)
            n += 1

    # 锚点自检
    have = set(names)
    for f in MUST_HAVE:
        if f not in have:
            bad.append("缺锚点文件：%s" % f)

    kb = os.path.getsize(out) / 1024.0
    print("包内 %d 个文件，%.0f KB" % (n, kb))
    print("自检：%s" % ("通过 ✅" if not bad else "有问题 ❌"))
    for b in bad:
        print("   " + b)
    if bad:
        return 2
    print("产物：%s" % out)
    print("用法：解压到任意目录 → 双击 一键安装skill.cmd（或 python install_skill.py）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
