# -*- coding: utf-8 -*-
"""首次配置：确保样本库根目录存在，并同步 references/paths.md。

规则：
- 从 paths.md 的 ${SAMPLES_ROOT} 行解析"本机默认"路径（第三列）。
- 若该路径已存在 -> 直接使用，不改文件。
- 若不存在（换机/新电脑，路径还是作者机器的）-> 自动改用本机默认
  %USERPROFILE%\\AI创作\\提示词skill生成尝试，创建目录骨架，并写回 paths.md。

由 启动评分工具.bat 开头自动调用；agent 也可直接运行本脚本。
"""
import os
import re
import sys

# 本机默认（无路径可用时）
HOME = os.path.expanduser("~")
DEFAULT_ROOT = os.path.join(HOME, "AI创作", "提示词skill生成尝试")

# paths.md 所在位置（相对于本脚本的 上级目录/references/）
PATHS_MD = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "references", "paths.md"))


def read_paths():
    try:
        with open(PATHS_MD, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None


def extract_current_root(text):
    """取 ${SAMPLES_ROOT} 行第三个反引号列（本机默认路径）。"""
    for line in text.splitlines():
        if "${SAMPLES_ROOT}" in line and "|" in line:
            cells = line.split("|")
            if len(cells) >= 4:
                return cells[3].strip().strip("`").strip()
    return None


def write_paths(text, new_root):
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if "${SAMPLES_ROOT}" in line and "|" in line:
            cells = line.split("|")
            cells[3] = " `%s` " % new_root
            if len(cells) >= 5:
                cells[4] = " `%s` " % new_root  # 换机示例同步
            lines[i] = "|".join(cells)
            break
    with open(PATHS_MD, "w", encoding="utf-8") as f:
        f.writelines(lines)


def ensure_skeleton(root):
    """生成根目录 + 说明文件；实验子目录由后续项目按需创建（素材自动归位规则）。"""
    os.makedirs(os.path.join(root, "参考"), exist_ok=True)
    note = os.path.join(root, "README.txt")
    if not os.path.exists(note):
        with open(note, "w", encoding="utf-8") as f:
            f.write(
                "这是打分工具的样本库根目录（评分工具连接本目录）。\n"
                "每个新项目/实验建立一个子文件夹，统一结构：\n"
                "  <实验名>/\n"
                "    ├── 文案/口播稿.txt      # 台词原文\n"
                "    ├── 文案/提示词.txt      # 生成指令\n"
                "    ├── 素材/                # 上传的参考图/音视频（agent 自动归类）\n"
                "    ├── 成片/                # 验收成片\n"
                "    ├── 废片/                # 作废抽卡（文件名=日期-废因）\n"
                "    ├── 评价/*.json          # 六维评价（打分工具产出）\n"
                "    └── 备注/备注.txt        # 主观备注 + 生成参数\n"
            )
    return root


def main():
    text = read_paths()
    if text is None:
        print("[首次配置] 未找到 references/paths.md，直接使用本机默认目录。")
        root = DEFAULT_ROOT
        ensure_skeleton(root)
        print("[首次配置] 样本库根目录(默认): %s" % root)
        return 0

    current = extract_current_root(text)
    if current and os.path.isdir(current):
        print("[首次配置] 样本库根目录已存在，直接使用: %s" % current)
        return 0

    root = DEFAULT_ROOT
    ensure_skeleton(root)
    if current and current != root:
        write_paths(text, root)
        print("[首次配置] 原路径 %s 不存在，已改用本机默认并写回 paths.md。" % current)
    else:
        print("[首次配置] 使用本机默认并创建目录: %s" % root)
    print("[首次配置] 完成。样本库根目录: %s" % root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
