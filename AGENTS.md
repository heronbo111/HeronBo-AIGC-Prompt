# AGENTS.md —— 多 agent 协作约定（HeronBo-AIGC-Prompt 仓库）

> 本仓库可能被多个 agent 同时操作（ZCode / Codex CLI / DeepSeek Harness）。三者读的是**同一份工作树**，所以「内容同步」不是问题，冲突来自**并发写入**和**未提交改动堆积**。动手前先读本节。

## 铁律

1. **动手前先看状态**：`git fetch gitee && git status --short`
   - 本地落后 → `git pull --rebase`
   - 工作树里有**别人未提交的改动** → 不要碰那些文件；确需改动先问用户
2. **认领再改**：在下方「当前认领」表加一行（agent / 文件范围 / 开始时间），改完提交后删除该行。
3. **只提交自己改的文件**：`git add <明确文件名>`；禁止 `git add -A` / `git add .`。
4. **提交信息面向用户**：用用户看得懂的话写清「改了什么、对使用有什么影响」；不要 `[ZCode]`/`[DSH]` 这类 agent 前缀（要标来源就写在提交正文末尾）。
5. **改完立刻提交**，不要把未提交改动留在共享工作树里；跨机同步再 `git push gitee`（GitHub 私有库同推）。
6. **禁止**在共享克隆里执行 `git checkout .`、`git stash`、`git reset --hard`、`git clean -fd`——会清掉另一方的未提交改动；确需丢弃改动先问用户。
7. **仓库历史在 2026-09-19 整理过一次**（工作台源码改为不随公开仓库分发，`main` 随之重写）。
   - **重写之前克隆过的副本**：历史和远程已不是同一棵树，`git pull` 会失败 →
     先确认自己没有未提交改动，再 `git fetch <远程> && git reset --hard FETCH_HEAD` 对齐
     （这是第 6 条的**必要例外**）；或者干脆删掉重新 clone。
   - **重写之后克隆的副本**：不受影响，照旧 `git pull --rebase`。
   - 工作台源码（界面 / 本地服务 / 启动器 / 打包配置）**不在本仓库**了；用户拿工作台走
     Release 附件（`python tools\部署.py vendor --fetch --yes`），不需要源码。
7. **冲突裁决**：以「用户实测反馈 > 模板惯例 > 推断」为准；合并后必须自检：
   - `python tools/首次配置.py` 无参运行：已配置 exit 0；未配置 exit 1 并打印「请问您要把项目建在哪里？您提供好素材后，我会自动将其进行归类」
   - `tools\score_gui.cmd` 能弹出评分窗口（或直接跑 `tools\dist\score-tool.exe`）
   - `SKILL.md` frontmatter 完整（`name` / `description` / `version`）、`references/paths.md` 保持模板（本机取值只在 `paths.local.md`）
8. **不要提交**：大视频（.gitignore 已挡）、超过 5MB 的二进制/模型权重；`tools/识别工具/` 的脚本与小模型（`face_detection_yunet_2023mar.onnx` 0.22MB）随仓库走，保证可复现。
9. **本地优化只写 `references/*.local.md`（已 gitignore）**：`paths.local.md`（路径/平台）、`rules.local.md`（本地规则覆盖层，优先级高于上游 rules.md）、`eval-absorbed.local.json`（评价回收账本）。这样 `git pull` 永不冲突；上游改动保持向后兼容（`paths.md`/`platforms.md` 结构稳定，`SKILL.md` 的 `version` 递增）。
10. **改 `references/rules.md` 前先读顶部规则索引**；只追加或修订自己的条目，不重排/改写别人的规则。跨机通用的写 `rules.md`，本机个人经验写 `rules.local.md`。
11. **模型与限额只改一处**：模型或限额有变动时，只改 `references/platforms.md` 的「模型与限额」表；`rules.md` 与模板只引用，不写死数字。

## 维护纪律（2026-09-19 用户裁定：**所有改动都是为了让 skill 更好地帮 agent**）

1. **口径/行为类改动要落在 skill 层，而不是只写死在工具源码里**——写在工作台源码（私有仓库）
   里的规则，换 harness、换机器、别人部署都读不到。凡是"agent 该怎么做"的改动，
   **落 `references/rules.md`（上游）**，工具侧只留一句指回条号。
2. **改了"按类型查表"的文案，必须同时补表**：界面/指令里的四段名、任务名都是按任务类型查表
   （`STAGE_NAMES` 等），**表里缺一项会静默退回默认文案**（用户 2026-09-19 看到旧阶段名就是这么来的）。
3. **改了规则文件，要 grep 一遍调用方**：规则改了，工具链里的指令副本不会自己跟着变。
   例：`prompt-templates.md` 的「交付形态」一改，就搜 `workbench_server.py` 的指令段有没有旧口径。

## 当前认领

| agent | 文件范围 | 开始时间 |
|---|---|---|
| （空） | | |

## 分工建议（减少撞车）

- **ZCode**：规则/模板/样本库内容（`references/rules.md`、`references/prompt-templates.md`、`references/samples-db.md`）
- **Codex / DSH**：工具链与文档（`tools/`、`SKILL.md` 流程节、`README.md`、`TODO.md`、本文件）
- 跨范围改动先在「当前认领」登记再动手；同一文件两人都要改时，一人改完提交、另一人 `git pull --rebase` 后再改。

## 安装/换机入口

新会话先读：`README.md` → `references/paths.md` → `TODO.md` → `SKILL.md`。
