# AGENTS.md —— 多 agent 协作约定（HeronBo-AIGC-Prompt 仓库）

> **谁读这份**：ZCode / Codex CLI / DeepSeek Harness / WorkBuddy（各自的入口目录里都指向**同一份工作树**，
> 所以「内容同步」不是问题——冲突来自**并发写入**与**未提交改动堆积**）。动手前先读本节。
> 最后更新：2026-09-21。

## 新会话从这里开始（顺序照读，别整篇通读任何一份）

1. **本文件**（协作纪律与红线）
2. `README.md`（这个 skill 是什么、怎么装）
3. `references/paths.md` → 本机取值在 `references/paths.local.md`（gitignore，**改它不进仓库**）
4. `TODO.md`（手上还有什么没做）
5. `SKILL.md`（工作流入口）——**按需查章节，不要整篇读**（见下面「上下文纪律」）

## 目录地图与入口（找东西先看这里）

| 位置 | 是什么 | 入口 / 主要文件 |
|---|---|---|
| `SKILL.md` | 技能入口：工作流、四大类、限额引用 | 新会话按需读章节 |
| `references/` | 知识库：规则、模板、平台口径、样本库、拆解手册 | `rules.md`（上游规则，**先读顶部索引**）、`prompt-templates.md`（交付形态与句式）、`platforms.md`（模型与限额，**唯一数字源**）、`playbooks.md`、`samples-db.md`、`eval-cases.md` |
| `references/*.local.md` | 本机个人层（gitignore，不进仓库） | `rules.local.md`（本地经验，**优先级高于上游 rules.md**）、`paths.local.md` |
| `tools/` | 工具链（Python / .cmd，脚本名多为中文） | `首次配置.py`、`部署.py`、`换exe.py`、`提示词体检.py`、`联调自检.py`、`project_core.py`（项目骨架/回执/清单）、`workbench_server.py` + `workbench/`（**工作台源码，gitignore、不随仓库分发**） |
| `tools/dist/` | 工作台 exe（Release 附件来的） | `score-tool.exe` |
| `docs/` | 给人读的文档 | `新机部署.md`、`给用户-最快用上.md`、`上架市场.md` |
| `samples/` | 示例项目（结构参考） | 每个子目录 = 一个项目 |
| `_stage/` | 待换的 exe（换位用） | `score-tool.exe` |

**样本库（不在本仓库、但你会被叫去干活）**：每个项目一个子文件夹，内含 `文案/ 素材/ 平台上传/ 成片/ 废片/ 评价/ 备注/ _会话/`。

## 命令（都能直接跑；改完东西至少跑前两条）

```bash
python tools/首次配置.py                     # 自检：已配置 exit 0；未配置 exit 1 并打印要问用户的话
python tools/联调自检.py                     # 全流程自检（35 项，改工具链后必跑）
python tools/提示词体检.py --project "<项目目录>"   # 提示词外形与内容体检（0 不合格才算交付）
python tools/部署.py vendor --fetch --yes    # 拉大件与工作台 exe（Release 附件，约 305MB）
python tools/换exe.py                        # 换工作台 exe（要求当前没有实例在跑）
tools/score_gui.cmd                          # 弹评分窗口（或直接跑 tools/dist/score-tool.exe）
python tools/发布exe附件.py                   # 把大件/exe 发到 Release 附件
```

## 铁律

1. **动手前先看状态**：`git fetch gitee && git status --short`
   - 本地落后 → `git pull --rebase`
   - 工作树里有**别人未提交的改动** → 不要碰那些文件；确需改动先问用户
2. **认领再改**：在下方「当前认领」表加一行（agent / 文件范围 / 开始时间），改完提交后删除该行。
3. **只提交自己改的文件**：`git add <明确文件名>`；禁止 `git add -A` / `git add .`。
4. **提交信息按公开仓库的写法**（对齐 `clash-verge-rev` 这类项目的版本说明，2026-09-21 用户要求）：
   - **标题**：`类型: 一句话`，类型取 `feat` / `fix` / `docs` / `ui` / `chore` / `refactor`；
     一句话讲**对使用者有什么用**。
   - **正文**（要展开时）按固定三段，每条以「新增…／修复…／优化…」开头：
     `✨ 新增功能` · `🐞 修复问题` · `🚀 优化改进`（需要分平台/分模块再起小标题）。
   - **同一批多处改动合成一条提交**，不要拆成多轮。
   - **公共历史里不出现这些东西**：改了哪个源码文件、补交某文件、降频、协作登记、测试怎么跑的、
     内部参数取舍——这些是过程信息，留在本机日志或私有仓库；提交信息只说**使用者能感知的变化**。
   - 不加 `[ZCode]`/`[DSH]` 这类 agent 前缀（要标来源就写在提交正文末尾一行）。
   - 面向使用者的完整版本叙事写 `CHANGELOG.md`，提交信息是它的浓缩版，两边口径一致。
5. **改完立刻提交**，不要把未提交改动留在共享工作树里；跨机同步再 `git push gitee`（GitHub 同推）。
6. **禁止**在共享克隆里执行 `git checkout .`、`git stash`、`git reset --hard`、`git clean -fd`——会清掉另一方的未提交改动；确需丢弃改动先问用户。
7. **仓库历史在 2026-09-19 整理过一次**（工作台源码改为不随公开仓库分发，`main` 随之重写）。
   - **重写之前克隆过的副本**：历史和远程已不是同一棵树，`git pull` 会失败 →
     先确认自己没有未提交改动，再 `git fetch <远程> && git reset --hard FETCH_HEAD` 对齐
     （这是第 6 条的**必要例外**）；或者干脆删掉重新 clone。
   - **重写之后克隆的副本**：不受影响，照旧 `git pull --rebase`。
   - 工作台源码（界面 / 本地服务 / 启动器 / 打包配置）**不在本仓库**了；用户拿工作台走
     Release 附件（`python tools\部署.py vendor --fetch --yes`），不需要源码。
7.1 **冲突裁决**：以「用户实测反馈 > 模板惯例 > 推断」为准；合并后必须自检：
   - `python tools/首次配置.py` 无参运行：已配置 exit 0；未配置 exit 1 并打印「请问您要把项目建在哪里？您提供好素材后，我会自动将其进行归类」
   - `tools\score_gui.cmd` 能弹出评分窗口（或直接跑 `tools\dist\score-tool.exe`）
   - `SKILL.md` frontmatter 完整（`name` / `description` / `version`）、`references/paths.md` 保持模板（本机取值只在 `paths.local.md`）
8. **不要提交**：大视频（.gitignore 已挡）、超过 5MB 的二进制/模型权重；`tools/识别工具/` 的脚本与小模型（`face_detection_yunet_2023mar.onnx` 0.22MB）随仓库走，保证可复现。
9. **本地优化只写 `references/*.local.md`（已 gitignore）**：`paths.local.md`（路径/平台）、`rules.local.md`（本地规则覆盖层，优先级高于上游 rules.md）、`eval-absorbed.local.json`（评价回收账本）。这样 `git pull` 永不冲突；上游改动保持向后兼容（`paths.md`/`platforms.md` 结构稳定，`SKILL.md` 的 `version` 递增）。
10. **改 `references/rules.md` 前先读顶部规则索引**；只追加或修订自己的条目，不重排/改写别人的规则。跨机通用的写 `rules.md`，本机个人经验写 `rules.local.md`。
11. **模型与限额只改一处**：模型或限额有变动时，只改 `references/platforms.md` 的「模型与限额」表；`rules.md` 与模板只引用，不写死数字。

## ✅ 可以直接做 / ⚠️ 先问用户 / 🚫 绝对不做

对照用；与上面铁律冲突时以铁律为准。

- ✅ **可以直接做**：读/搜任何文件；写 `references/*.local.md`；改自己认领范围内的文件；
  按 `prompt-templates.md` 的口径出提示词；在样本库项目里建 `文案/ 平台上传/ 备注/ _会话/` 产物；跑上面「命令」里那些自检。
- ⚠️ **先问用户**：改 `references/rules.md` 等上游规则 / 模板口径；改 `SKILL.md` 流程与 `version`；
  动别人项目或别人的未提交改动；删文件；改本机取值（`paths.local.md` 指向的路径）；
  装任何软件/依赖；对素材做加工（静音/裁剪/转比例/深度片/转写/OCR/抽水印…）。
- 🚫 **绝对不做**：**代用户在即梦（及一切生成平台）提交任何生成任务**（铁律见工作区 `AGENTS.md` 第 5 节；
  用户明确同意后要带 `DREAMINA_CONFIRMED=1` 才放行，闸门在 `~/.zcode/cli/config.json` 的 PreToolUse 钩子里）；
  读取/解密任何已有的剪映加密草稿（只能程序新建明文草稿）；把大视频或成片提交进仓库；
  在共享工作树里跑 `git checkout .` / `stash` / `reset --hard` / `clean -fd`；
  把用户的账号、路径、客户名写进公开仓库（往 rules/playbooks 写「依据」时项目名一律泛化）。

## 上下文纪律（本仓库文档较多，按它省时间）

- `SKILL.md`(35KB) + `references/`(~341KB) **不要整篇读**：先看本文件的目录地图，再按需 grep 定位、只读目标章节。
- 大文件先搜后读：`grep -n` 定位行号 → 带 `offset/limit` 读区间；**同一段落别重复读**。
- 工具输出只抓一次（`… 2>&1 | tee /tmp/x.log`），之后分析文件、别重跑。
- 批量改动攒够再跑一次自检，不要改一处跑一次。
- 生成物目录（`tools/_vendor/`、`tools/dist/`、`_stage/`）**只搜不读**。

## 维护纪律（2026-09-19 用户裁定：**所有改动都是为了让 skill 更好地帮 agent**）

1. **口径/行为类改动要落在 skill 层，而不是只写死在工具源码里**——写在工作台源码（私有仓库）
   里的规则，换 harness、换机器、别人部署都读不到。凡是"agent 该怎么做"的改动，
   **落 `references/rules.md`（上游）**，工具侧只留一句指回条号。
2. **改了"按类型查表"的文案，必须同时补表**：界面/指令里的四段名、任务名都是按任务类型查表
   （`STAGE_NAMES` 等），**表里缺一项会静默退回默认文案**（用户 2026-09-19 看到旧阶段名就是这么来的）。
3. **改了规则文件，要 grep 一遍调用方**：规则改了，工具链里的指令副本不会自己跟着变。
   例：`prompt-templates.md` 的「交付形态」一改，就搜 `workbench_server.py` 的指令段有没有旧口径。
4. **动作流是「通道表」驱动的**：`agent_trace.py` 的 `BUILTIN_CHANNELS` 每个通道＝去哪找（dir+glob）
   ＋按什么格式解析（fmt）。增删通道或新增 fmt 取值时，**要同时补** `docs/动作流通道.md` 与
   `tools/agent_channels.example.json`——否则换机器的人不知道 `agent_channels.local.json` 怎么写
   （2026-09-21 用户：有的机子只装了 WorkBuddy、有的只装了别的工具，要各自适配）。

## 当前认领

| agent | 文件范围 | 开始时间 |
|---|---|---|
| ZCode | **界面二选一改造**（用户 2026-09-22 指派）：只动 workbench/index.html、workbench/app.js、workbench/shell.js —— ① 界面三选里移除「经典界面」按钮（用户点过它就「弹回网页端、切不回来」，走他给的兜底：只留流程台＋极简）②「工作台界面」改名「流程台界面」③ 清掉各窗口里没必要的小字。**不碰** workbench_server.py / score_gui.pyw / 版本.py / 更新助手.py（DSH 在改）。**已完成**：exe 已换位重开、浏览器 DOM 断言 11 项全过（17:5x） | 2026-09-22 14:35 |
| ZCode | **安装提速线**（用户 2026-09-22 指派）：**已完成并提交 `4ceceef`（已推 GitHub；Gitee 待 README 那 2 个提交合流后再推）**。新增 `tools/下载器.py`、`tools/能力包.py`、`tools/打包发行.py`、`tools/发布发行附件.py`；`tools/部署.py` 里加了三处钩子（`_download_any` 转发多源续传、`vendor --fetch` 能力包参数、`exe_refresh_needed` 改比 sha256）；发行 tag 换到 `vendor-2026-09-22`（Gitee 8 件已验、GitHub 补齐中）。⚠️ **DSH：你这轮也在改 部署.py / 安装向导.py**，保存 部署.py 前请 `git status` 看一眼，别把上面那三处钩子整文件覆盖掉（钩子没了 = 安装退回"无续传、默认拉 300MB"的老逻辑） | 2026-09-22 12:25 |
| DSH（WorkBuddy） | 「ZCode 交班清单」收尾（已完成：`references/paths.md`、`references/samples-db.md`、`tools/联调说明.md`、`README.md`、`.gitignore`）＋ **版本＝一套完整快照（rules 71）**。**已改完、未提交、未推**：`references/rules.md`（新增第 71 条 + 索引）、`CHANGELOG.md`(v2.9)、`SKILL.md`(2.9)、`tools/project_core.py`（骨架加 `current`/`versions`）、私有工作台的 `tools/workbench_server.py` / `workbench/app.js` / `workbench/index.html` / `workbench/app.css`。**去冗已收口**：`workbench_server.py` 全量字面量替换经评估风险大于收益（3909 行、ZCode 也在改），只做口径一致性修复（4 处），不再做常量化 | 2026-09-21 16:40 |

## 分工建议（减少撞车）

- **ZCode**：规则/模板/样本库内容（`references/rules.md`、`references/prompt-templates.md`、`references/samples-db.md`）
- **Codex / DSH**：工具链与文档（`tools/`、`SKILL.md` 流程节、`README.md`、`TODO.md`、本文件）
- 跨范围改动先在「当前认领」登记再动手；同一文件两人都要改时，一人改完提交、另一人 `git pull --rebase` 后再改。

## 安装/换机入口

新会话先读：`AGENTS.md`（本文件）→ `README.md` → `references/paths.md` → `TODO.md` → `SKILL.md`。
