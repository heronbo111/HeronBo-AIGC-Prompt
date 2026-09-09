# -*- coding: utf-8 -*-
"""首次配置 / 落位：读取或写入本机样本库根目录，并按需建项目骨架。

设计（2026-09-10 修订）：
- 本机路径取值写在 references/paths.md 的取值表里，**不预设任何默认路径**。
- 未配置时只提示、不创建任何目录，并给出要问用户的原话：
  「请问您要把项目建在哪里？您提供好素材后，我会自动将其进行归类」
- 用法：
    python 首次配置.py                       # 校验配置：已配置返回 0，未配置打印话术返回 1
    python 首次配置.py --set "<样本库根>"      # 登记样本库根目录并写回 paths.md
    python 首次配置.py --project "<项目目录>"  # 建项目骨架，并把其上一级登记为 SAMPLES_ROOT

由 启动评分工具.bat 开头调用；agent 也可直接运行。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.normpath(os.path.join(HERE, ".."))
PATHS_MD = os.path.join(SKILL_ROOT, "references", "paths.md")
PROJECT_DIRS = ["文案", "素材", "成片", "废片", "评价", "备注"]
PLACEHOLDER = "<待用户指定>"
ASK = "请问您要把项目建在哪里？您提供好素材后，我会自动将其进行归类"
ROW_SAMPLES = "${SAMPLES_ROOT}"
ROW_AI = "${AI_CREATE_ROOT}"


def read_paths():
    try:
        with open(PATHS_MD, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None


def read_value(text, key):
    """从取值表行取最后一个非空单元格（去掉反引号）。"""
    for line in text.splitlines():
        if key in line and line.strip().startswith("|"):
            cells = [c.strip().strip("`").strip() for c in line.split("|")]
            cells = [c for c in cells if c]
            if cells:
                return cells[-1]
    return None


def write_value(text, key, value):
    out = []
    hit = False
    for line in text.splitlines(keepends=True):
        if (not hit) and key in line and line.strip().startswith("|"):
            cells = line.rstrip("\r\n").split("|")
            for i in range(len(cells) - 1, -1, -1):
                if cells[i].strip():
                    cells[i] = " `%s` " % value
                    break
            line = "|".join(cells) + ("\r\n" if line.endswith("\r\n") else "\n")
            hit = True
        out.append(line)
    return "".join(out), hit


def ensure_root(root):
    os.makedirs(root, exist_ok=True)
    note = os.path.join(root, "README.txt")
    if not os.path.exists(note):
        with open(note, "w", encoding="utf-8") as f:
            f.write("这是打分工具的样本库根目录（评分工具连接本目录）。\n")
            f.write("每个项目/实验一个子文件夹，统一结构：\n")
            f.write("  <实验名>/\n")
            f.write("    ├── 文案/口播稿.txt      # 台词原文\n")
            f.write("    ├── 文案/提示词.txt      # 生成指令\n")
            f.write("    ├── 素材/                # 用户提供的参考图/音视频（agent 自动归类）\n")
            f.write("    ├── 即梦上传/            # 提示词要上传的副本 + 上传说明.txt\n")
            f.write("    ├── 成片/                # 验收成片\n")
            f.write("    ├── 废片/                # 作废抽卡（文件名=日期-废因）\n")
            f.write("    ├── 评价/*.json          # 六维评价（打分工具产出）\n")
            f.write("    └── 备注/备注.txt        # 主观备注 + 生成参数\n")
    return root


def ensure_project(project_dir):
    project_dir = os.path.normpath(project_dir)
    for d in PROJECT_DIRS:
        os.makedirs(os.path.join(project_dir, d), exist_ok=True)
    return project_dir


def record(root, ai_root=None):
    text = read_paths()
    if text is None:
        print("[首次配置] 未找到 references/paths.md，跳过写回；根目录=%s" % root)
        return
    text, ok1 = write_value(text, ROW_SAMPLES, root)
    ai = ai_root or os.path.dirname(os.path.normpath(root))
    text, ok2 = write_value(text, ROW_AI, ai)
    with open(PATHS_MD, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    print("[首次配置] 已写回 references/paths.md：SAMPLES_ROOT=%s%s" % (root, "" if ok1 else "（未匹配到表格行）"))
    print("[首次配置] AI_CREATE_ROOT=%s%s" % (ai, "" if ok2 else "（未匹配到表格行）"))


def configured_root():
    text = read_paths() or ""
    val = read_value(text, ROW_SAMPLES)
    if val and PLACEHOLDER not in val and os.path.isdir(val):
        return val
    return None


def main(argv):
    if "--set" in argv:
        i = argv.index("--set")
        if i + 1 >= len(argv):
            print("[首次配置] --set 缺少路径参数。")
            return 2
        root = ensure_root(os.path.normpath(argv[i + 1]))
        record(root)
        return 0
    if "--project" in argv:
        i = argv.index("--project")
        if i + 1 >= len(argv):
            print("[首次配置] --project 缺少路径参数。")
            return 2
        proj = ensure_project(argv[i + 1])
        root = os.path.dirname(proj)
        ensure_root(root)
        record(root)
        print("[首次配置] 项目骨架已建：%s" % proj)
        return 0
    root = configured_root()
    if root:
        print("[首次配置] 样本库根目录已配置，直接使用：%s" % root)
        return 0
    print("[首次配置] 尚未指定样本库根目录（references/paths.md 还是占位符）。")
    print("[首次配置] 先问用户：%s" % ASK)
    print("[首次配置] 拿到位置后：python 首次配置.py --project \"<项目目录>\"")
    print("[首次配置] 或只登记根目录：python 首次配置.py --set \"<样本库根目录>\"")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
