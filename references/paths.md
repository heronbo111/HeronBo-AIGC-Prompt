# 路径与平台约定（模板；本机取值在 paths.local.md）

skill 正文一律使用变量名（`${SAMPLES_ROOT}` / `${AI_CREATE_ROOT}`），不写死任何具体路径。
**本机取值不写在本文件**——写进同目录的 `references/paths.local.md`（已 gitignore，不进仓库、不参与 git 冲突）。
该文件不存在 = 首次使用 → 先问用户（见下）。

## 首次使用：先问用户，不要猜

agent 按顺序问两个问题（同一轮问完）：

1. > 请问您要把项目建在哪里？您提供好素材后，我会自动将其进行归类
2. > 你主要用哪个平台做 AI 视频？即梦 / 小云雀 / updream

拿到答案后：① 建项目框架（`文案/素材/成片/废片/评价/备注`，交付提示词时再加 `平台上传/`）；② 把用户提供的素材/文案自动归类落位；③ 按 `references/platforms.md` 处理平台 CLI（**有才装、没有不装、不检测账号**）；④ 写入 `paths.local.md`。
用户没给明确位置时**再问一次**——不得自己猜，也不得创建默认目录（旧版自动建默认目录的行为已废弃）。

## paths.local.md 格式

```
SAMPLES_ROOT=<样本库根目录：各项目目录的上一级，评分工具连接这一级>
AI_CREATE_ROOT=<参考素材/来源素材根目录>
PLATFORM=<即梦|小云雀|updream>
CLI=<CLI 命令名，没有就留空>
DEPTH_MODEL=<深度模型 onnx 路径，可选；也可用环境变量 DEPTH_MODEL>
```

由 `tools\首次配置.py` 创建并写回；也可以手写。

## 目录模型（样本库内含素材库）

```
${SAMPLES_ROOT}/
└── <实验名>/
    ├── 文案/      口播稿.txt、提示词.txt
    ├── 素材/      用户提供的参考图/产品图/音频/原片（agent 自动归类，保留原文件名）
    ├── 平台上传/  交付提示词时同步生成的副本（文件名=引用编号+角色+时长；多版本再分子目录）
    ├── 成片/      验收成片
    ├── 废片/      作废抽卡（文件名=项目-版本-废因）
    ├── 评价/      评分工具产出的 *.json（用 tools\评价回收.py 回收）
    └── 备注/      备注.txt（主观备注 + 生成参数）
```

## 首次使用 / 换机清单

1. 安装 skill：ZCode `~/.zcode/skills/`；Codex CLI `~/.codex/skills/`；DeepSeek Harness `~/.dsh/skills/`（也可项目级 `.dsh/skills/`）。新对话自动识别。
2. agent 问位置 + 问平台 → 建框架 + 归类素材 + 按 `platforms.md` 装 CLI（可选）+ 写 `paths.local.md`。
3. 评分工具：双击 `tools\score_gui.cmd`（优先 `dist\score-tool.exe`）；找不到样本库根时会弹目录选择框，选 `${SAMPLES_ROOT}` 后自动写回 `paths.local.md`。exe 自带运行时、不需要装 Python；要自行打包才需要 Python 3 + PyInstaller。
4. 环境：Python 3、Edge/Chrome、ffmpeg（素材识别抽帧）；**不检测平台账号**，账号由用户自行登录。
5. 自检：`python tools\首次配置.py`（无参：已配置 exit 0 / 未配置 exit 1 并打印问句）、`python tools\评价回收.py`、`tools\score_gui.cmd` 能弹出评分窗口。
