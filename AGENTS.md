# AGENTS.md —— 多 agent 协作约定（seedance-prompt 仓库）

> 本仓库可能被多个 agent 同时操作（ZCode / Codex CLI / DeepSeek Harness）。三者读的是**同一份工作树**，
> 所以「内容同步」不是问题，冲突来自**并发写入**和**未提交改动堆积**。动手前先读本节。

## 铁律

1. **动手前先看状态**：`git fetch gitee && git status --short`
   - 本地落后 → `git pull --rebase`
   - 工作树里有**别人未提交的改动** → 不要碰那些文件；确需改动先问用户
2. **认领再改**：在下方「当前认领」表加一行（agent / 文件范围 / 开始时间），改完提交后删除该行。
3. **只提交自己改的文件**：`git add <明确文件名>`；禁止 `git add -A` / `git add .`。
4. **提交信息面向用户**：用用户看得懂的话写清「改了什么、对使用有什么影响」；不要 `[ZCode]`/`[DSH]` 这类 agent 前缀（要标来源就写在提交正文末尾）。
5. **改完立刻提交**，不要把未提交改动留在共享工作树里；跨机同步再 `git push gitee`（GitHub 私有库同推）。
6. **禁止**在共享克隆里执行 `git checkout .`、`git stash`、`git reset --hard`、`git clean -fd`——会清掉另一方的未提交改动；确需丢弃改动先问用户。
7. **冲突裁决**：以「用户实测反馈 > 模板惯例 > 推断」为准；合并后必须自检：
   - `python tools/首次配置.py` 无参运行：已配置 exit 0；未配置 exit 1 并打印「请问您要把项目建在哪里？您提供好素材后，我会自动将其进行归类」
   - `python -m http.server 8787 --directory tools` → `http://localhost:8787/评价工具.html` 能打开、能连目录
   - `SKILL.md` frontmatter 完整（`name` / `description` / `version`）、`references/paths.md` 保持模板（本机取值只在 `paths.local.md`）
8. **不要提交**：大视频（.gitignore 已挡）、超过 5MB 的二进制/模型权重；`tools/识别工具/` 的脚本与小模型（`face_detection_yunet_2023mar.onnx` 0.22MB）随仓库走，保证可复现。
9. **本地优化只写 `references/*.local.md`（已 gitignore）**：`paths.local.md`（路径/平台）、`rules.local.md`（本地规则覆盖层，优先级高于上游 rules.md）、`eval-absorbed.local.json`（评价回收账本）。这样 `git pull` 永不冲突；上游改动保持向后兼容（`paths.md`/`platforms.md` 结构稳定，`SKILL.md` 的 `version` 递增）。

## 当前认领

| agent | 文件范围 | 开始时间 |
|---|---|---|
| ZCode（吸收 B站 BV1kuKE66Eds 二创拆解经验） | `references/rules.md`、`references/prompt-templates.md`、`references/eval-cases.md`、`references/parody-teardown.md`（新增）、`tools/成片拆解.py`（新增）、`SKILL.md` | 2026-09-12 |

## 分工建议（减少撞车）

- **ZCode**：规则/模板/样本库内容（`references/rules.md`、`references/prompt-templates.md`、`references/samples-db.md`）
- **Codex / DSH**：工具链与文档（`tools/`、`SKILL.md` 流程节、`README*.md`、`TODO.md`、本文件）
- 跨范围改动先在「当前认领」登记再动手；同一文件两人都要改时，一人改完提交、另一人 `git pull --rebase` 后再改。
- 改 `references/rules.md` 前先读顶部「规则索引」；只追加或修订自己的条目，不重排/改写别人的规则。跨机通用的写 `rules.md`，本机个人经验写 `rules.local.md`。

## 安装/换机入口

新会话先读：`README-安装说明.md` → `references/paths.md` → `TODO.md` → `SKILL.md`。