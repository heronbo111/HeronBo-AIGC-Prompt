# -*- coding: utf-8 -*-
"""首次配置 / 落位：本机路径与平台取值（写入 references/paths.local.md）。

设计（2026-09-10 v1.3）：
- 本机取值只写 references/paths.local.md（已 gitignore），paths.md 永远是模板 → git pull 永不冲突。
- 不检测任何平台账号；平台 CLI 由 references/platforms.md 决定（有才装、没有不装）。
- 未配置时只提示、不创建任何目录，并给出要问用户的原话。

用法：
    python 首次配置.py                                   # 校验：已配置 exit 0；未配置 exit 1 并打印问句
    python 首次配置.py --project "<项目目录>"             # 建项目骨架，其上一级登记为 SAMPLES_ROOT
    python 首次配置.py --set "<样本库根>"                 # 只登记样本库根
    python 首次配置.py --platform 即梦 --cli dreamina     # 登记平台与 CLI 命令

供启动器与 agent 调用；也可直接运行。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.normpath(os.path.join(HERE, ".."))
LOCAL_MD = os.path.join(SKILL_ROOT, "references", "paths.local.md")
PATHS_MD = os.path.join(SKILL_ROOT, "references", "paths.md")
PROJECT_DIRS = ["文案", "素材", "成片", "废片", "评价", "备注"]
PLACEHOLDER_HINTS = ("<", "待用户指定", "你的样本库根目录", "你的素材根目录")
ASK_PLACE = "请问您要把项目建在哪里？您提供好素材后，我会自动将其进行归类"
ASK_PLATFORM = "你主要用哪个平台做 AI 视频？即梦 / 小云雀 / updream"
HEADER = "# 本机取值（不提交仓库；由首次使用时的用户回答写入）\n"
ORDER = ["SAMPLES_ROOT", "AI_CREATE_ROOT", "PLATFORM", "CLI"]


def read_local():
    vals = {}
    try:
        with open(LOCAL_MD, encoding="utf-8-sig") as f:
            text = f.read()
    except FileNotFoundError:
        return vals
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        vals[k.strip()] = v.strip().strip("`").strip(chr(34))
    return vals


def write_local(**kw):
    vals = read_local()
    for k, v in kw.items():
        if v:
            vals[k] = v
    lines = [HEADER]
    for k in ORDER:
        if vals.get(k):
            lines.append("%s=%s\n" % (k, vals[k]))
    for k in sorted(set(vals) - set(ORDER)):
        lines.append("%s=%s\n" % (k, vals[k]))
    os.makedirs(os.path.dirname(LOCAL_MD), exist_ok=True)
    with open(LOCAL_MD, "w", encoding="utf-8", newline="") as f:
        f.write("".join(lines))
    return vals


def legacy_value(key):
    """兼容旧版：从 paths.md 表格里读值（迁移期用）。"""
    try:
        with open(PATHS_MD, encoding="utf-8-sig") as f:
            text = f.read()
    except FileNotFoundError:
        return None
    for line in text.splitlines():
        if key in line and line.strip().startswith("|"):
            cells = [c.strip().strip("`").strip() for c in line.split("|") if c.strip()]
            if cells:
                return cells[-1]
    return None


def looks_unset(value):
    return (not value) or any(h in value for h in PLACEHOLDER_HINTS)


def configured_root():
    root = read_local().get("SAMPLES_ROOT")
    if not looks_unset(root) and os.path.isdir(root):
        return root
    legacy = legacy_value("${SAMPLES_ROOT}")
    if not looks_unset(legacy) and os.path.isdir(legacy):
        return legacy
    return None


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
            f.write("    ├── 评价/*.json          # 六维评价（打分工具产出，tools/评价回收.py 回收）\n")
            f.write("    └── 备注/备注.txt        # 主观备注 + 生成参数\n")
    return root


def ensure_project(project_dir):
    project_dir = os.path.normpath(project_dir)
    for d in PROJECT_DIRS:
        os.makedirs(os.path.join(project_dir, d), exist_ok=True)
    return project_dir


def main(argv):
    if "--set" in argv:
        i = argv.index("--set")
        if i + 1 >= len(argv):
            print("[首次配置] --set 缺少路径参数。")
            return 2
        root = ensure_root(os.path.normpath(argv[i + 1]))
        write_local(SAMPLES_ROOT=root)
        print("[首次配置] 已写入 paths.local.md：SAMPLES_ROOT=%s" % root)
        return 0

    if "--project" in argv:
        i = argv.index("--project")
        if i + 1 >= len(argv):
            print("[首次配置] --project 缺少路径参数。")
            return 2
        proj = ensure_project(argv[i + 1])
        root = os.path.dirname(proj)
        ensure_root(root)
        write_local(SAMPLES_ROOT=root)
        print("[首次配置] 项目骨架已建：%s" % proj)
        print("[首次配置] 已写入 paths.local.md：SAMPLES_ROOT=%s" % root)
        return 0

    if "--platform" in argv:
        i = argv.index("--platform")
        if i + 1 >= len(argv):
            print("[首次配置] --platform 缺少平台名。")
            return 2
        cli = ""
        if "--cli" in argv:
            j = argv.index("--cli")
            if j + 1 < len(argv):
                cli = argv[j + 1]
        write_local(PLATFORM=argv[i + 1], CLI=cli)
        print("[首次配置] 已写入 paths.local.md：PLATFORM=%s CLI=%s" % (argv[i + 1], cli or "(无)"))
        return 0

    root = configured_root()
    if root:
        print("[首次配置] 样本库根目录已配置：%s" % root)
        if not read_local().get("PLATFORM"):
            print("[首次配置] 还没登记平台，问用户：%s" % ASK_PLATFORM)
            print("[首次配置] 拿到回答后：python 首次配置.py --platform 即梦 --cli dreamina")
        return 0

    print("[首次配置] 尚未指定样本库根目录（references/paths.local.md 不存在或无效）。")
    print("[首次配置] 先问用户：%s" % ASK_PLACE)
    print("[首次配置] 再问用户：%s" % ASK_PLATFORM)
    print("[首次配置] 拿到位置后：python 首次配置.py --project \"<项目目录>\"")
    print("[首次配置] 或只登记根目录：python 首次配置.py --set \"<样本库根目录>\"")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
