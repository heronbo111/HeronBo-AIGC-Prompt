# Seedance 口播提示词 Skill —— 安装与使用说明

版本：v1.3（2026-09-10，跨机化 + 平台选择 + 评价回收 + 本地覆盖层）　适用：ZCode / Codex CLI / DeepSeek Harness / 其他能读文件的 agent

## 这个包是什么

把「口播文案/台词 + 素材」自动整理成即梦（Dreamina）Seedance 可直接使用的提示词。
**核心铁律：只生成提示词文字，不提交生成、不消耗任何积分；视频生成由你自己在即梦网页操作。本 skill 交付物中不会出现 CLI 命令/积分价格/队列信息。**

里面包含：
- `seedance-prompt/` —— 技能本体（SKILL.md + references/ 下的规则、模板、样本库、路径表）
- `tools/评价工具.html` —— 成片六维评价网页（打分→存本地→统计），配套 `tools/启动评分工具.bat`（已通用化：用 PATH 里的 python 起服务，换机免改路径）

## 安装

### ZCode 客户端
把 `seedance-prompt` 文件夹整个放到：
```
C:\Users\你的用户名\.zcode\skills\seedance-prompt\
```
（或项目级 `.agents\skills\`）新对话即可自动识别。

### Codex CLI
```
C:\Users\你的用户名\.codex\skills\seedance-prompt\
```

### DeepSeek Harness（DSH）
```
C:\Users\你的用户名\.dsh\skills\seedance-prompt\
```
（用户级；也可项目级 `<项目>\.dsh\skills\`）新对话自动识别。

### 其他 agent harness（通用）
对 agent 说：
```
读取 <解压路径>\seedance-prompt\SKILL.md 并严格按其工作流执行
```

### 换机四件套（必做，5 分钟）
0. **环境检查（第一件事）**：`python tools\环境检查.py` —— 逐项自检 python≥3.9 / ffmpeg / ffprobe / numpy / opencv / faster-whisper（拆解转写）/ onnxruntime（深度视频）/ Yunet 人脸模型 / 系统 OCR / Depth 模型，缺什么就打印该装什么。
   装缺项：**先问用户**，同意后 `python tools\环境检查.py --install --yes`（pip 包直装；ffmpeg 走 `winget install Gyan.FFmpeg`）；模型另跑 `--models`（Depth，约 99MB，HF 需代理）与 `--warm-asr`（预下转写模型）。**装软件必须用户同意，不许静默安装。**
1. **问项目位置**：agent 问「请问您要把项目建在哪里？您提供好素材后，我会自动将其进行归类」→ `python tools\首次配置.py --project "<项目目录>"` 建骨架（`文案/素材/成片/废片/评价/备注`）+ 自动归类素材 + 写入 `references/paths.local.md`（已 gitignore，不进仓库）；
2. **问平台（可选装 CLI）**：agent 问「你主要用哪个平台做 AI 视频？即梦 / 小云雀 / updream」→ 按 `references/platforms.md` 检测：
   - 即梦 = `dreamina`（官方脚本 `curl -fsSL https://jimeng.jianying.com/cli \| bash`；Windows 用 Git Bash 或按官方指引）；
   - 小云雀 = `pippit-tool-cli`（`npm i -g @pippit-dev/cli`，使用时需 `XYQ_ACCESS_KEY`，用户自行申请、不要写进仓库）；
   - updream = 暂无公开 CLI → **不装**，网页操作。
   **只有用户指定、且该平台确实有 CLI 时才装**；不检测账号、不代登录。
3. **装依赖**：跑第 0 件（`python tools\环境检查.py`）即可一次看全：python、ffmpeg、numpy/opencv、可选 faster-whisper（转写）与 onnxruntime（深度视频）；Edge/Chrome（必须 localhost 方式打开，file:// 无法写入本地目录）。**不检测即梦账号**，账号由用户自己登录。

## 使用

- **自然触发**：直接说"帮我写口播提示词 / 把这段台词变成 Seedance 提示词 / 做分镜提示词"
- **显式调用**：`/seedance-prompt 把这段台词做成提示词：……`
- 提示词生成后，自己在即梦网页生成视频；生成完**由 agent 自动启动评分工具**打分（六维+违禁项+结论；agent 按 SKILL.md 交付节执行：Bash 后台起 `python -m http.server 8787 --directory <tools 目录>`，再用浏览器打开 `http://localhost:8787/评价工具.html`）。手动兜底：**双击 `tools\启动评分工具.bat`**（首次可传参：`启动评分工具.bat "<样本库根目录>"`）——① 校验样本库根（未指定时提示先问用户项目位置，不再自动乱建目录）→ ② 起服务 → ③ 自动打开页面 → ④ 浏览器首次点「连接样本目录」选样本库根后自动记忆，之后打开即用。
- **给其他 agent 的自动化入口**：日常评分服务启动按 SKILL.md 交付节（Bash 后台+浏览器打开，无需人工）；建骨架可单独跑 `python seedance-prompt\tools\首次配置.py --project "<项目目录>"`（或 `--set "<样本库根>"` 只登记根目录）；bat 仅作手动兜底。
- **评分自动回收**：用户打完分后，agent 跑 `python seedance-prompt\tools\评价回收.py` 拿「待吸收评价」清单（六维均分/结论/备注），按《反馈优化循环》写进规则/模板/样本库，再 `--mark-all` 记账（账本 `references/eval-absorbed.local.json`，本机、不进仓库）。
- **给 agent 反馈**（"口型对不上""这条成了"）→ agent 会按 skill 的《反馈优化循环》自动把规律写进 rules.md，越用越准

## 必守铁律（已写入 skill，务必遵守）

1. 提示词里写的每个"@视频1/@图片1"必须真实存在于本次上传的素材（引用一致性）
2. 负面词每段写死：无字幕 / 无运镜 / 无AI畸变
3. 生成类（图片+音频驱动）提示词**禁止写"参考视频"**
4. 硬质产品替换软体角色（如礼盒换果冻）必须写"Q弹软体化形变"；音乐必须写"沿用原片原声"
5. **交付物只出提示词**：不出现 CLI 命令/积分价格/队列信息；默认由你在平台网页提交生成。**即梦 CLI 已恢复可用**（2026-09-09 实测 `dreamina` 在 PATH）；只有你明确要求代提交时 agent 才执行——先报单价 → 你确认 → 执行 → 如实报告消耗，且这类内容不进入常规交付（rules.md 第13条）。

## 同步与版本（本包用 git 管理）

- 本包是 **git 仓库**（双远程已建）：GitHub **私有** `https://github.com/heronbo111/seedance-prompt.git`（作者编排主库）；Gitee **公开** `https://gitee.com/HeronBo/seedance-prompt.git`（国内直连使用；公开版已脱敏，真实品牌/台词/本机路径已替换为示例内容）。
- **同步节奏（2026-09-08 约定）**：作者端每周一 09:00 自动检查并推送（有无新提交均静默）；**各安装端不必跟着每次改动更新**——需要用到新规则、新模板或评分工具时，`git pull` 一次即可。
- **按反馈更新规则后**：作者 `git push 双远程`；其他机器 `git pull` 即拿到最新规则——不存在"zip 快照过期"问题。
- 大视频素材（成片/废片）不进仓库（.gitignore），各机样本库各自维护；**规律数据（评价 json、文案、备注）以仓库 `samples/` 为准**（结构与约定见 `samples/README.md`）。
- 首次拉取：`git clone https://github.com/heronbo111/seedance-prompt.git C:\Users\你的用户名\.zcode\skills\seedance-prompt`（国内直连可用 Gitee 同构替换地址）
- 反馈给作者：把成片放入自己样本库 `成片/`，用评分工具打分，把 `评价/*.json` 结论更新进仓库 `samples/`（或合并进仓库后 push）。

## 更新与兼容（本地优化了 skill 怎么办）

- **本地优化只写 gitignored 的本地覆盖层**，上游文件一律不改：
  - `references/paths.local.md` —— 本机路径/平台（`tools\首次配置.py` 维护）
  - `references/rules.local.md` —— 本地新增/推翻的规则，优先级**高于**上游 `rules.md`
  - `references/eval-absorbed.local.json` —— 评价回收账本
- 因此 `git pull` 不会冲突：受版本控制的文件保持上游原样，本地经验留在 `*.local.md` 里被 skill 优先读取。
- **更新步骤**：`git pull --rebase` → `python tools\首次配置.py`（无参自检）→ `python tools\评价回收.py` → 起评分页确认能连目录。
- **兼容检查**：`SKILL.md` frontmatter 的 `version` 变大 = 上游有结构性改动。这时 agent 要对照本文件与 `references/platforms.md`，检查 `rules.local.md` 的条目是否被上游新规则取代或冲突；**冲突以本地用户实测为准**，并在回复里明确提示。
- 想把本地规则贡献回上游：把 `rules.local.md` 的条目整理成「规则 + 依据（日期/来源）」搬进 `rules.md` 再提交。

## 注意事项

- `references/samples-db.md` 里的样本路径是 `${SAMPLES_ROOT}` 变量；本机取值见 `references/paths.local.md`（模板与说明见 `references/paths.md`）。
- `tools/启动评分工具.bat` 无需改路径（自动探测 python/py；页面从 bat 所在目录提供）。
- 若服务端口 8787 被占用：关掉旧「评价工坊服务」窗口后重开 bat，或改 bat 端口并同步改打开 URL。
- **首次 push/pull 如弹出登录**：安装并启用 Git Credential Manager（Git for Windows 通常自带；`git config credential.helper manager` 后，git 会引导浏览器授权，帐号密码不用输入 git 命令行）。
