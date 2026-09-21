# 贡献指南（给人类）

> **AI agent 请读 [`AGENTS.md`](AGENTS.md)**——那份是给 agent 的运行手册（命令、目录地图、红线）。
> 这份是�serving人类的贡献政策：你想改这个仓库，流程与边界在这里。

## 这个仓库是什么

一套 **AI 视频提示词生成 skill**（四类：图生视频 / 文生视频 / 参考视频生视频 / 参考视频替换生视频）
＋ 一套本地工作台（pywebview 独立窗口 + 多 agent 协作）。使用者是短视频团队：批量做口播带货、
替换类、二创拆解，产出可直接粘贴到即梦 / Seedance 的提示词。

- 知识内核：`references/`（规则、模板、平台限额、样本库、拆解手册）
- 工具链：`tools/`（Python 脚本 + 工作台）
- 工作台 exe 走 **Release 附件**分发，源码**不在本仓库**（见 `AGENTS.md` 铁律 7）

## 三种改动，走不同的路

| 你想改 | 改哪里 | 注意 |
|---|---|---|
| **通用规则 / 模板口径** | `references/rules.md`、`references/prompt-templates.md` | 先读 `rules.md` 顶部索引；**只追加或修订自己的条目**，不重排别人的；规则只写「规则 + 依据（日期/来源）」，不写推理过程 |
| **本机个人经验** | `references/rules.local.md`、`paths.local.md`（已 gitignore） | 这样 `git pull` 永不冲突；跨机通用的才写上游 |
| **工具链 / 工作台** | `tools/` | 改完跑 `python tools/联调自检.py`；工作台源码真身在私有仓库，用 `同步.py` 同步 |

## 提交流程

```bash
git fetch gitee && git status --short     # 先看有没有别人未提交的改动
# 在 AGENTS.md「当前认领」登记一行（agent）或知会对方（人类）
git add <明确文件名>                       # 禁止 git add -A / .
git commit                                # 写清「改了什么、对使用有什么影响」
git push gitee HEAD:main && git push github HEAD:main
```

- **禁止** `git checkout .` / `git stash` / `git reset --hard` / `git clean -fd`（会冲掉别人未提交的改动）。
- 提交信息不要 agent 前缀（要标来源写在正文末尾）。

## 禁区（硬红线）

1. **不代用户在生成平台提交任何任务**（即梦及一切生成平台）：任何生成（视频/图片/音频）执行前
   必须先向用户汇报入口、模型、时长/张数、分辨率、预计积分，**拿到明确同意才动手**；
   工作台/CLI 侧有硬闸门（`DREAMINA_CONFIRMED=1` 才放行）。
2. **剪映 6.0+ 草稿 AES 加密**：只能程序生成明文草稿，**绝不读取/解密任何已有加密草稿**；只新建，不改用户现有草稿。
3. **不提交**大视频、成片、废片、超过 5MB 的二进制/模型权重（`.gitignore` 已挡）。
4. **不往公开仓库写**用户账号、本机绝对路径、客户名——往 `rules`/`playbooks` 写「依据」时项目名一律泛化。

## 平台 CLI 与依赖

- 平台 CLI（如即梦 `dreamina`）**有才装、不检测账号**：装不装由用户决定，装了也不去探测他的登录态。
- 本机取值只写 `references/*.local.md`，不进仓库。

## 许可

MIT（见 `LICENSE`）。
