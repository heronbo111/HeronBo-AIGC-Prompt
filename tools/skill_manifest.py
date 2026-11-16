# -*- coding: utf-8 -*-
"""技能包唯一文件清单：agent 包、npm 包、安装包都从这里取，避免三套口径走偏。"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "HeronBo-AIGC-Prompt"

CORE_FILES = (
    "SKILL.md",
    "LICENSE",
    "install_skill.py",
    "安装向导.py",
    "一键安装skill.cmd",
)
CORE_DIRS = (
    "references",
    "skills",
)
DOC_FILES = (
    "docs/新机部署.md",
)

# tools 下真正给 agent 跑提示词/拆片/部署用的文件。
# 明确白名单，避免把打安装包、发布、签名、备份、GUI 产物一起带进去。
TOOL_FILES = {
    "_version.json",
    "agent_bridge.py",
    "agent_channels.example.json",
    "agent_trace.py",
    "doubao_cdp.mjs",
    "project_core.py",
    "score_core.py",
    "wb_bridge.py",
    "wb_cli.py",
    "wb_dreamina.py",
    "wb_storyboard.py",
    "wb_templates.py",
    "wb_video.py",
    "下载B站成片.py",
    "下载器.py",
    "人物遮罩.py",
    "人脸遮罩.py",
    "找工作台.py",
    "成片拆解.py",
    "成片对比.py",
    "提示词体检.py",
    "深度视频.py",
    "环境检查.py",
    "部署.py",
    "评价回收.py",
    "评分.py",
    "竖版画布.py",
    "首次配置.py",
}
TOOL_DIRS = {
    "识别工具",
}
TOOL_EXCLUDE_PREFIX = (
    "打", "打包", "发布", "签名", "换exe", "图标", "版本", "更新助手",
    "补迁移", "迁移", "载荷", "联调", "drag_selftest", "workbench_server",
    "工作台.py", "score_gui", "deploy.py", "doubao_tray", "build_exe",
)
TOOL_EXCLUDE_SUFFIX = (
    ".local.json", ".local.md", ".pyc", ".log", ".tmp", ".spec", ".cmd",
    ".png", ".ico", ".jpg", ".jpeg", ".bak", ".part.json",
)


def selected_files() -> list[Path]:
    """返回相对仓库根的技能包文件；本地私有配置、样例、开发文档都不包含。"""
    files: list[Path] = []

    def add(rel: str) -> None:
        p = ROOT / rel
        if p.is_file():
            files.append(Path(rel))

    for rel in CORE_FILES + DOC_FILES:
        add(rel)

    for d in CORE_DIRS:
        p = ROOT / d
        if not p.is_dir():
            continue
        for f in p.rglob("*"):
            if not f.is_file():
                continue
            rel = f.relative_to(ROOT).as_posix()
            if ".local." in rel:
                continue
            files.append(Path(rel))

    tools = ROOT / "tools"
    if tools.is_dir():
        for f in tools.rglob("*"):
            if not f.is_file():
                continue
            rel = f.relative_to(tools).as_posix()
            parts = rel.split("/")
            if parts[0] in {"dist", "_vendor", "workbench", "canvas", "build",
                            "_stage", "_release", "__pycache__"}:
                continue
            if any(p.startswith("workbench_backup-") or p.startswith("workbench_bak-")
                   or p.startswith("workbench.bak-") for p in parts):
                continue
            if parts[0] in TOOL_DIRS:
                files.append(Path("tools") / rel)
                continue
            if len(parts) != 1:
                continue
            if rel in TOOL_FILES:
                if rel.startswith(TOOL_EXCLUDE_PREFIX):
                    continue
                if rel.endswith(TOOL_EXCLUDE_SUFFIX):
                    continue
                files.append(Path("tools") / rel)

    return sorted(set(files), key=lambda p: p.as_posix())
