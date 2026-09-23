# HeronBo-AIGC-Prompt

AI 视频提示词生成 + 成片六维评价工具。把「文案/台词 + 素材（图片/视频/音频）」整理成可直接粘贴到生成平台的提示词，并附成片评分工具。

## 一、这是什么 / 能做什么

**核心铁律：只生成提示词文字，不提交生成、不消耗任何积分**；视频生成由用户自己在平台网页操作。交付物中不会出现 CLI 命令 / 积分价格 / 队列信息。只有用户明确要求代提交时才动手（先报单价 → 确认 → 执行 → 如实报告消耗）。

### 覆盖四大类

| 大类 | 典型需求 | 备注 |
|---|---|---|
| **图生视频** | 给图片让它动起来；含**道具/主体替换** | 图片是唯一锚 |
| **文生视频** | 什么素材都不给，只有一段文字 | 需把人物/场景/动作/镜头/风格全写进文字 |
| **参考视频生视频** | 照某段视频的运镜/动作/节奏，生成新画面 | 只管动作与节奏 |
| **参考视频替换生视频** | 保留原片动作构图，把里面的人/物换掉 | 难度分 L1–L4 |
| （音乐二创/翻唱） | 给音乐，做卡点/配口型 | 参考视频类的特例 |

### 三条附加能力

- **拆解 AI 二创/爆款成片**：读片三问 + 机器证据 + 拆解报告 + 复现路线
- **越用越准**：内置「反馈优化循环」——每次成片反馈都会沉淀为已验证规则
- **模型无关**：提示词文本对即梦/小云雀/updream 通用

### 触发与交付

1. 触发：直接说「帮我写提示词 / 把这段台词变成 Seedance 提示词 / 做分镜提示词 / 把视频里的人换成另一个 / 拆解这条二创」
2. 交付物 = 提示词 + 素材清单（+ 拼接说明）；生成在平台网页操作
3. 成片出来后必须弹出评分窗口进行六维评分
4. 评分会被自动回收，下次会话按反馈循环写进规则并记账
5. 给 agent 反馈后自动更新规则，越用越准

## 二、安装 / 换机 / 自检

### 放置位置（新对话自动识别）

| 运行环境 | 目录 |
|---|---|
| ZCode 客户端 | `C:\Users\你的用户名\.zcode\skills\HeronBo-AIGC-Prompt\` |
| Codex CLI | `C:\Users\你的用户名\.codex\skills\HeronBo-AIGC-Prompt\` |
| DeepSeek Harness（DSH） | `C:\Users\你的用户名\.dsh\skills\HeronBo-AIGC-Prompt\` |

### 安装向导：先问你装哪（2026-09-22 起）

装 skill / 放项目**都会先问位置**，不再默认塞进固定目录：

```bash
python 安装向导.py            # 问两件事：①skill 装到哪（按本机 agent 给默认值）②项目样本库放哪
python 安装向导.py --list     # 只看各 harness 的默认目录
```

它会：选位置（ZCode / WorkBuddy / Codex / DSH 默认目录或自定义）→ 没下载就浅克隆（`--depth 1`，
没 git 就直接下 gitee 的 zip 源码包，**不用装 git**）→ 写样本库根 → 可选给其它 agent 建 junction
（一份实体多处可用）→ 可选接着跑 `tools\部署.py all --yes`。

### 下载（任选其一，都只要最新一层）

```bash
git clone --depth 1 https://gitee.com/HeronBo/HeronBo-AIGC-Prompt.git      # 国内，浅克隆约 13MB
git clone --depth 1 https://github.com/heronbo111/HeronBo-AIGC-Prompt.git   # GitHub（已公开）
# 没装 git：直接下 zip（解压后跑 python 安装向导.py）
# https://gitee.com/HeronBo/HeronBo-AIGC-Prompt/repository/archive/main.zip
```

### 一键安装（新机最短路径）

**双击仓库根的 `一键安装.cmd`**，或者：

```bash
python tools\部署.py all --yes
```

它会：找 Python → 用仓库自带的离线 wheel 装必需依赖 → 接 agent 通道并真跑一句验证连通 → 建桌面快捷方式 → 把工作台弹出来。

### 大件与工作台 exe 怎么拿（仓库只放代码）

**仓库只放代码（约 13MB），大件走 Release 附件**（2026-09-17 起）——因为把 300MB 安装资产和
历代 exe 放进 git，会把 Gitee 的 1GB 配额撑爆（实测 1047MB 被拒收）。

| 你要什么 | 命令 |
|---|---|
| 代码（clone 12 秒） | `git clone --depth 1 https://gitee.com/HeronBo/HeronBo-AIGC-Prompt.git` |
| **安装全套**（Python 轮子 + ffmpeg + 深度模型 + 工作台 exe，约 305MB，一次就好） | `python tools/deploy.py vendor --fetch --yes` |
| **更新工作台**（已有安装，想把 exe 换成最新版） | **工作台里点标题条右侧的「检查更新」** → 「下载并重启」，它自己下、校验、换位、重启（开着工作台也能更新；弹窗里还有「回滚到上一版」）。命令行也能：关掉工作台 → `python tools/deploy.py vendor --fetch --yes`（`--exe` 可强制重下）|
| 只要核心（工作台 + 出提示词 + 评分） | 什么都不用做 |
| 想要最新 exe | **工作台里点「检查更新」**（不用关工作台）；命令行：关掉工作台 → `python tools/deploy.py vendor --fetch --yes` |
| 我这版是哪一版 | 看一眼标题条「检查更新」旁的版本号；命令行 `python tools\版本.py`（current / remote / check）|

附件地址：`https://github.com/heronbo111/HeronBo-AIGC-Prompt/releases`（最新 tag `vendor-2026-09-22`）
（下载慢的话：脚本会**自动换镜像源**（gh-proxy 等前缀依次试）；也可 `HERONBO_VENDOR_REL=<镜像前缀>` 手动换源）。
`python tools/deploy.py all --yes` 会**自动**把缺的拉齐。

### exe 被杀软 / Defender 删了（2026-09-22 实测）

打包是 PyInstaller onefile 且未签名，Defender 启发式容易误报。装机端跑：

```bash
python tools/deploy.py defender          # 只检查：exe 还在不在、保护历史有没有动过它
python tools/deploy.py defender --yes    # 提权把工作台目录加进白名单（UAC 点「是」）
```

然后二选一：到「Windows 安全中心 → 保护历史」把隔离的 `score-tool.exe` **还原**；
或 `python tools/deploy.py vendor --fetch --yes` **重新下一份**。加过白名单后不会再删。
要彻底除名可向微软提交误报申诉（https://www.microsoft.com/en-us/wdsi/filesubmission）。

**工作台窗口一片空白？** 那是 WebView2 运行库坏了（注册表写着装了、目录里 `msedgewebview2.exe` 却没了）——
`python tools\部署.py check` 会报出来，`python tools\部署.py wx --yes` 一条命令修好；工作台现在还会
**自动退到 Edge 窗口**，不会再晾你一个白窗。整条新机流程见 `docs/新机部署.md`。

### 换机四件套（必做，约 5 分钟）

0. **环境检查（第一件事）**：`python tools\环境检查.py` —— 逐项自检 python≥3.9 / ffmpeg / ffprobe / WebView2 / pywebview，以及**按需**的 numpy / opencv / faster-whisper（拆解转写）/ onnxruntime（深度视频）/ Yunet 人脸模型 / 系统 OCR / Depth 模型，缺什么就打印该装什么。
   装缺项：**先问用户**，同意后 `python tools\环境检查.py --install --yes`（**仓库自带离线包时默认一次装全**；ffmpeg 与深度模型都在 `tools/_vendor/` 里，装完即用）；模型另跑 `--models`（Depth，约 99MB，HF 需代理）与 `--warm-asr`（预下转写模型）。**装软件必须用户同意，不许静默安装。**
1. **问项目位置**：agent 问「请问您要把项目建在哪里？您提供好素材后，我会自动将其进行归类」→ `python tools\首次配置.py --project "<项目目录>"` 建框架（`文案/素材/成片/废片/评价/备注`）+ 自动归类素材 + 写入 `references/paths.local.md`（已 gitignore，不进仓库）。
2. **问平台（可选装 CLI）**：agent 问「你主要用哪个平台做 AI 视频？即梦 / 小云雀 / updream」→ 按 `references/platforms.md` 检测：
   - 即梦 = `dreamina`（官方脚本 `curl -fsSL https://jimeng.jianying.com/cli | bash`；Windows 用 Git Bash 或按官方指引）；
   - 小云雀 = `pippit-tool-cli`（`npm i -g @pippit-dev/cli`，使用时需 `XYQ_ACCESS_KEY`，用户自行申请、不要写进仓库）；
   - updream = 暂无公开 CLI → **不装**，网页操作。
   **只有用户指定、且该平台确实有 CLI 时才装**；不检测账号、不代登录。
3. **装依赖**：跑第 0 件即可一次看全：python、ffmpeg、numpy/opencv、可选 faster-whisper（转写）与 onnxruntime（深度视频）；Edge/Chrome（必须 localhost 方式打开，file:// 无法写入本地目录）。**不检测即梦账号**，账号由用户自己登录。

### 自检清单

- `python tools\首次配置.py`（无参）：已配置 exit 0；未配置 exit 1
- `python tools\评价回收.py` 能列出待吸收评价
- `tools\score_gui.cmd` 能弹出评分窗口

## 三、目录结构

```
HeronBo-AIGC-Prompt/
├── SKILL.md                       # 技能工作流
├── README.md                      # 本文件
├── AGENTS.md                      # 多 agent 协作约定
├── TODO.md                        # 待办入口
├── references/                    # 规则、模板、平台信息
│   ├── rules.md                   # 硬规则与已验证规律
│   ├── prompt-templates.md        # 提示词模板
│   ├── playbooks.md               # 实战手册
│   ├── platforms.md               # 平台与 CLI + 模型限额
│   ├── samples-db.md              # 样本库对照
│   ├── paths.md                   # 路径表模板
│   ├── rules.local.md             # 本机规则覆盖层
│   └── paths.local.md             # 本机路径配置
├── tools/                         # 工具链
│   ├── score_gui.pyw              # 六维评分窗口
│   ├── score_core.py              # 评分核心
│   ├── 环境检查.py                # 环境自检
│   ├── 首次配置.py                # 落位/平台登记
│   ├── 评价回收.py                # 待吸收评价清单
│   ├── 成片拆解.py                # 二创/成片机器证据
│   ├── 深度视频.py                # 黑白深度视频
│   └── 人物遮罩.py                # 人物区糊掉、场景保留
└── samples/                       # 规律数据仓库版
```

### 各目录要点

- **references/**：写提示词前只读本次相关条目；rules.md 顶部有规则索引
- **tools/**：本地预处理与评价工具，都不提交生成、不消耗积分
- **samples/**：仓库内为脱敏示例，真实数据存各机本机样本库

## 四、参与贡献

1. 使用并反馈：成片反馈/六维评分是规则更新的唯一来源
2. 本地优化写 `references/*.local.md`（gitignored），git pull 不会冲突
3. 大视频（成片/废片）不进仓库；真实数据只存各机本机样本库

### 作者侧：改完怎么发出去

工具更新一律走 `tools\推送.py`：

```bash
# 1) 先体检
python tools\推送.py --dry --msg "说明" tools/score_core.py README.md

# 2) 体检通过就真发
python tools\推送.py --msg "评分：口型同步判据补一条" tools/score_core.py README.md
```

## 五、特技（可直接抄的硬口径）

- 规则优先级：用户实测 > 模板 > 推断
- 引用一致性：提示词里写的每个 @视频1/@图片1 必须真实存在于上传素材
- 负面词每段写死：无字幕 / 无运镜 / 无AI畸变
- 生成类禁止写"参考视频/视频1"；替换类提示词宜短
- 平台无关：提示词文本对即梦/小云雀/updream 通用
- 交付物只出提示词：不出现 CLI 命令/积分价格/队列信息

## 六、开源许可证

MIT License，见 [LICENSE](LICENSE)。

## 七、更新记录

对使用者有意义的变化按时间倒序记在 [CHANGELOG.md](CHANGELOG.md)。当前版本见 SKILL.md 的 version 字段。