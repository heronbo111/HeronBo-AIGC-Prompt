# HeronBo-AIGC-Prompt

AI 视频提示词生成 + 成片六维评价工具。把「文案/台词 + 素材（图片/视频/音频）」整理成可直接粘贴到生成平台的提示词，并附成片评分工具。

## 一、这是什么 / 能做什么

**核心铁律：只生成提示词文字，不提交生成、不消耗任何积分**；视频生成由用户自己在平台网页操作。交付物中不会出现 CLI 命令 / 积分价格 / 队列信息。只有用户明确要求代提交时才动手（先报单价 → 确认 → 执行 → 如实报告消耗）。

**看图版**：工作流程 / 新手教程 / 文件对照的图文说明在仓库根目录 `图解-工作流与新手教程.html`——单文件、离线可看，浏览器直接打开。

### 覆盖四大类

| 大类 | 典型需求 | 备注 |
|---|---|---|
| **图生视频** | 给图片让它动起来；含**道具/主体替换**（例：只换掉手里三本书） | 图片是唯一锚 |
| **文生视频** | 什么素材都不给，只有一段文字 | 需把人物/场景/动作/镜头/风格全写进文字；模板标「未验证」 |
| **参考视频生视频** | 照某段视频的运镜/动作/节奏，生成新画面 | @视频1 只管动作与节奏 |
| **参考视频替换生视频** | 保留原片动作构图，把里面的人/物换掉 | 难度分 L1–L4，见 `references/playbooks.md` |
| （音乐二创/翻唱） | 给音乐，做卡点/配口型 | 参考视频类的特例 |

口播带货、数字人、老师/角色设定都只是这套流程里的一种形态与参数——人设由项目给，skill 不预设。

### 三条附加能力

- **拆解 AI 二创/爆款成片**：读片三问 + 机器证据（`tools\成片拆解.py`）+ 拆解报告 + 复现路线。
- **越用越准**：内置「反馈优化循环」——每次成片反馈（六维评分 / 口头规律 / 废片原因）都会沉淀为已验证规则，写进 `references/rules.md`。
- **模型无关**：提示词文本对即梦/小云雀/updream 通用；限额按模型维护在 `references/platforms.md` 的「模型与限额」表，CLI 只是可选加速，装不装不影响交付。

### 触发与交付

1. 触发：直接说「帮我写提示词 / 把这段台词变成 Seedance 提示词 / 做分镜提示词 / 把视频里的人换成另一个 / 拆解这条二创」，或 `/HeronBo-AIGC-Prompt <台词>`。
2. 交付物 = 提示词 + 素材清单（+ 拼接说明）；生成在平台网页操作。
3. 成片出来后：agent 按 SKILL.md「交付」节**必须弹出评分窗口**（`tools\score_gui.cmd`，可带 `--sample` 直接定位项目）；手动兜底就双击同一个 `tools\score_gui.cmd`。窗口里选样本 → 选成片 → 点分 → 保存。
4. **评分会被自动回收**：下次会话 agent 跑 `python tools\评价回收.py` 拿「待吸收评价」清单，按反馈循环写进规则/模板并记账（账本 `references/eval-absorbed.local.json`）。
5. 给 agent 反馈（"口型对不上""这条成了"）→ 按反馈循环自动更新规则，越用越准。

## 二、安装 / 换机 / 自检

### 放置位置（新对话自动识别）

| 运行环境 | 目录 |
|---|---|
| ZCode 客户端 | `C:\Users\你的用户名\.zcode\skills\HeronBo-AIGC-Prompt\`（或项目级 `.agents\skills\`） |
| Codex CLI | `C:\Users\你的用户名\.codex\skills\HeronBo-AIGC-Prompt\` |
| DeepSeek Harness（DSH） | `C:\Users\你的用户名\.dsh\skills\HeronBo-AIGC-Prompt\`（用户级；也可项目级 `<项目>\.dsh\skills\`） |
| 其他 agent harness | 对 agent 说：`读取 <解压路径>\HeronBo-AIGC-Prompt\SKILL.md 并严格按其工作流执行` |

克隆：`git clone https://gitee.com/HeronBo/seedance-prompt.git`（国内直连建议 Gitee；作者主库为 GitHub 私有）。GitHub 私有库地址 `https://github.com/heronbo111/seedance-prompt.git`。

### 换机四件套（必做，约 5 分钟）

0. **环境检查（第一件事）**：`python tools\环境检查.py` —— 逐项自检 python≥3.9 / ffmpeg / ffprobe / numpy / opencv / faster-whisper（拆解转写）/ onnxruntime（深度视频）/ Yunet 人脸模型 / 系统 OCR / Depth 模型，缺什么就打印该装什么。
   装缺项：**先问用户**，同意后 `python tools\环境检查.py --install --yes`（pip 包直装；ffmpeg 走 `winget install Gyan.FFmpeg`）；模型另跑 `--models`（Depth，约 99MB，HF 需代理）与 `--warm-asr`（预下转写模型）。**装软件必须用户同意，不许静默安装。**
1. **问项目位置**：agent 问「请问您要把项目建在哪里？您提供好素材后，我会自动将其进行归类」→ `python tools\首次配置.py --project "<项目目录>"` 建框架（`文案/素材/成片/废片/评价/备注`）+ 自动归类素材 + 写入 `references/paths.local.md`（已 gitignore，不进仓库）。
2. **问平台（可选装 CLI）**：agent 问「你主要用哪个平台做 AI 视频？即梦 / 小云雀 / updream」→ 按 `references/platforms.md` 检测：
   - 即梦 = `dreamina`（官方脚本 `curl -fsSL https://jimeng.jianying.com/cli | bash`；Windows 用 Git Bash 或按官方指引）；
   - 小云雀 = `pippit-tool-cli`（`npm i -g @pippit-dev/cli`，使用时需 `XYQ_ACCESS_KEY`，用户自行申请、不要写进仓库）；
   - updream = 暂无公开 CLI → **不装**，网页操作。
   **只有用户指定、且该平台确实有 CLI 时才装**；不检测账号、不代登录。
3. **装依赖**：跑第 0 件即可一次看全：python、ffmpeg、numpy/opencv、可选 faster-whisper（转写）与 onnxruntime（深度视频）；Edge/Chrome（必须 localhost 方式打开，file:// 无法写入本地目录）。**不检测即梦账号**，账号由用户自己登录。

### 自检清单

- `python tools\首次配置.py`（无参）：已配置 exit 0；未配置 exit 1 并打印「请问您要把项目建在哪里？您提供好素材后，我会自动将其进行归类」这句问话。
- `python tools\评价回收.py` 能列出待吸收评价。
- `tools\score_gui.cmd` 能弹出评分窗口（优先 `dist\score-tool.exe`，没有则 pythonw 起 `.pyw`；找不到样本库根时会弹目录选择框，选完自动写回 `references\paths.local.md`）。
- `python tools\成片拆解.py --check`、`python tools\深度视频.py --check "<视频>"`、`python tools\人物遮罩.py --check "<遮罩片>" --ref "<源片>"` 等工具自带自检可跑。

### 更新与本地覆盖（本地优化不会与上游冲突）

- **本地优化只写 gitignored 的本地覆盖层**，上游文件一律不改：
  - `references/paths.local.md` —— 本机路径/平台（`tools\首次配置.py` 维护）
  - `references/rules.local.md` —— 本地新增/推翻的规则，优先级**高于**上游 `rules.md`
  - `references/eval-absorbed.local.json` —— 评价回收账本
- 因此 `git pull` 不会冲突。**更新步骤**：`git pull --rebase` → `python tools\首次配置.py`（无参自检）→ `python tools\评价回收.py` → 起评分工具确认能连目录。
- **兼容检查**：`SKILL.md` frontmatter 的 `version` 变大 = 上游有结构性改动。这时 agent 要对照本文件与 `references/platforms.md`，检查 `rules.local.md` 的条目是否被上游新规则取代或冲突；**冲突以本地用户实测为准**，并在回复里明确提示。
- 想把本地规则贡献回上游：把 `rules.local.md` 的条目整理成「规则 + 依据（日期/来源）」搬进 `rules.md` 再提交。
- **同步节奏**：作者端每周一 09:00 自动推送双远程；各安装端不必跟随每次改动更新，需要新规则/新模板/评分工具时 `git pull` 一次即可。
- **首次 push/pull 如弹出登录**：安装并启用 Git Credential Manager（Git for Windows 通常自带；`git config credential.helper manager` 后，git 会引导浏览器授权）。

### 注意事项

- `references/samples-db.md` 里的样本路径是 `${SAMPLES_ROOT}` 变量；本机取值见 `references/paths.local.md`（模板与说明见 `references/paths.md`）。
- `tools/score_gui.cmd` 无需改路径（自动优先 `dist\score-tool.exe`，否则探测 pythonw / python）。
- 窗口版评分工具不占端口、不起本地服务；若 exe 起不来，去看 `%TEMP%\score_gui_crash.log`。
- 大视频素材（成片/废片）不进仓库（.gitignore），各机样本库各自维护；**规律数据（评价 json、文案、备注）以仓库 `samples/` 为准**（结构与约定见 `samples/README.md`）。

## 三、目录结构

```
HeronBo-AIGC-Prompt/
├── SKILL.md                       # 技能工作流（四大类判定 → 分类 → 模板 → 交付）
├── README.md                      # 本文件：介绍 / 安装换机 / 目录结构
├── AGENTS.md                      # 多 agent 协作约定（共享工作树的写前规则）
├── TODO.md                        # 待办入口
├── references/
│   ├── rules.md                   # 硬规则与已验证规律（只写「规则 + 依据」）
│   ├── prompt-templates.md        # 模板 A–F（四大类 ↔ 模板对应表在开头）
│   ├── playbooks.md               # 实战手册：替换类 / 二创拆解 / 深度视频与环境准备
│   ├── platforms.md               # 平台与 CLI + 模型与限额（唯一真相源）+ 平台玩法提炼
│   ├── samples-db.md              # 样本库对照（提示词→成片→评价）
│   ├── paths.md                   # 路径表模板（本机取值在 paths.local.md，gitignored）
│   ├── eval-cases.md              # 回归用例与断言
│   └── rules.local.md             # 本机规则覆盖层（gitignored，不进仓库）
├── tools/
│   ├── score_gui.pyw              # 六维评分窗口（可换主题；未保存关窗会拦）
│   ├── score_core.py              # 评分核心（窗口版与命令行版共用同一套 json）
│   ├── score_gui.cmd              # 一键启动（优先 tools/dist/score-tool.exe）
│   ├── build_exe.cmd              # 首次打包 exe（产物 tools/dist/score-tool.exe）
│   ├── 环境检查.py                # 环境自检 / 装缺项 / 下模型
│   ├── 首次配置.py                # 落位/平台登记（写 paths.local.md）
│   ├── 评价回收.py                # 待吸收评价清单 + 记账
│   ├── 成片拆解.py                # 二创/成片机器证据（切点、转写、报告）
│   ├── 成片对比.py                # 二创 vs 原片（画面同轴 / 声音路线）
│   ├── 深度视频.py                # 黑白深度视频（信息过滤）
│   ├── 人物遮罩.py                # 人物区糊掉、场景保留
│   ├── 竖版画布.py                # 横版源转竖版（crop / pad）
│   ├── 下载B站成片.py             # 拆解对象落本地（只取匿名清晰度）
│   └── 识别工具/                  # OCR 脚本 + YuNet 人脸模型（无视觉时的素材识别）
├── samples/                       # 规律数据（口播稿/提示词/评价json/备注，大视频不进仓）
└── LICENSE                        # MIT
```

### 各目录要点

- **`references/`**：写提示词前只读本次相关条目，不必通读；`rules.md` 顶部有规则索引。
- **`tools/`**：本地预处理与评价工具，**都不提交生成、不消耗积分**；`tools/识别工具/` 的脚本与小模型随仓库走，保证可复现。
- **`samples/`**：仓库内为脱敏示例，真实文案/备注只存各机本机样本库（`${SAMPLES_ROOT}`）。

## 四、参与贡献

1. 使用并反馈：成片反馈/六维评分是规则更新的唯一来源。
2. 本地优化写 `references/*.local.md`（gitignored），`git pull` 不会冲突；上游 `SKILL.md` 的 `version` 变大时按上文「更新与本地覆盖」复核本地规则。
3. 大视频（成片/废片）不进仓库；真实口播稿/备注只存各机本机样本库，仓库 `samples/` 为脱敏示例，更新流程见 `samples/README.md`。

## 五、特技（可直接抄的硬口径）

- 规则优先级：用户实测 > 模板 > 推断；出现 ≥2 次且因果明确的才升为「已验证规律」。
- 引用一致性硬规则：提示词里写的每个 @视频1/@图片1 必须真实存在于上传素材。
- 负面词每段写死：无字幕 / 无运镜 / 无AI畸变。
- 生成类禁止写"参考视频/视频1"；替换类提示词宜短（分工三句 + 锁定句）。
- 平台无关：提示词文本对即梦/小云雀/updream 通用。
- 交付物只出提示词：不出现 CLI 命令/积分价格/队列信息。

## 六、开源许可证

MIT License，见 [LICENSE](LICENSE)。
