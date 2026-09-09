# Seedance 口播提示词 Skill —— 安装与使用说明

版本：v1.1（2026-09-08，跨机化）　适用：ZCode / Codex CLI / 其他能读文件的 agent

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

### 换机三件套（必做，5 分钟）
1. **问项目位置**：agent 按固定话术问「请问您要把项目建在哪里？您提供好素材后，我会自动将其进行归类」，按答复建骨架并把 `${SAMPLES_ROOT}` 写进 `references/paths.md`（skill 正文不写死任何本机路径；不预设默认目录）；
2. **建样本库**：`python tools\首次配置.py --project "<项目目录>"` 一次建好骨架（项目子目录：`文案/素材/成片/废片/评价/备注`，参考 `references/samples-db.md`）；
3. **装依赖**：Python 3（评分工具，需在 PATH）、Edge/Chrome（必须 localhost 方式打开，file:// 无法写入本地目录）、ffmpeg（素材识别抽帧用）；生成在即梦网页完成，需即梦账号。

## 使用

- **自然触发**：直接说"帮我写口播提示词 / 把这段台词变成 Seedance 提示词 / 做分镜提示词"
- **显式调用**：`/seedance-prompt 把这段台词做成提示词：……`
- 提示词生成后，自己在即梦网页生成视频；生成完**由 agent 自动启动评分工具**打分（六维+违禁项+结论；agent 按 SKILL.md 交付节执行：Bash 后台起 `python -m http.server 8787 --directory <tools 目录>`，再用浏览器打开 `http://localhost:8787/评价工具.html`）。手动兜底：**双击 `tools\启动评分工具.bat`**（首次可传参：`启动评分工具.bat "<样本库根目录>"`）——① 校验样本库根（未指定时提示先问用户项目位置，不再自动乱建目录）→ ② 起服务 → ③ 自动打开页面 → ④ 浏览器首次点「连接样本目录」选样本库根后自动记忆，之后打开即用。
- **给其他 agent 的自动化入口**：日常评分服务启动按 SKILL.md 交付节（Bash 后台+浏览器打开，无需人工）；建骨架可单独跑 `python seedance-prompt\tools\首次配置.py --project "<项目目录>"`（或 `--set "<样本库根>"` 只登记根目录）；bat 仅作手动兜底。
- **给 agent 反馈**（"口型对不上""这条成了"）→ agent 会按 skill 的《反馈优化循环》自动把规律写进 rules.md，越用越准

## 必守铁律（已写入 skill，务必遵守）

1. 提示词里写的每个"@视频1/@图片1"必须真实存在于本次上传的素材（引用一致性）
2. 负面词每段写死：无字幕 / 无运镜 / 无AI畸变
3. 生成类（图片+音频驱动）提示词**禁止写"参考视频"**
4. 硬质产品替换软体角色（如礼盒换果冻）必须写"Q弹软体化形变"；音乐必须写"沿用原片原声"
5. **交付物只出提示词**：不出现 CLI 命令/积分价格/队列信息；提交生成由你在即梦网页完成（CLI 已断开；将来如恢复代提交，须先报单价、用户确认后才动，且该内容不进入常规交付）

## 同步与版本（本包用 git 管理）

- 本包是 **git 仓库**（双远程已建）：GitHub **私有** `https://github.com/heronbo111/seedance-prompt.git`（作者编排主库）；Gitee **公开** `https://gitee.com/HeronBo/seedance-prompt.git`（国内直连使用；公开版已脱敏，真实品牌/台词/本机路径已替换为示例内容）。
- **同步节奏（2026-09-08 约定）**：作者端每周一 09:00 自动检查并推送（有无新提交均静默）；**各安装端不必跟着每次改动更新**——需要用到新规则、新模板或评分工具时，`git pull` 一次即可。
- **按反馈更新规则后**：作者 `git push 双远程`；其他机器 `git pull` 即拿到最新规则——不存在"zip 快照过期"问题。
- 大视频素材（成片/废片）不进仓库（.gitignore），各机样本库各自维护；**规律数据（评价 json、文案、备注）以仓库 `samples/` 为准**（结构与约定见 `samples/README.md`）。
- 首次拉取：`git clone https://github.com/heronbo111/seedance-prompt.git C:\Users\你的用户名\.zcode\skills\seedance-prompt`（国内直连可用 Gitee 同构替换地址）
- 反馈给作者：把成片放入自己样本库 `成片/`，用评分工具打分，把 `评价/*.json` 结论更新进仓库 `samples/`（或合并进仓库后 push）。

## 注意事项

- `references/samples-db.md` 里的样本路径是 `${SAMPLES_ROOT}` 变量；取值见 `references/paths.md`（每台机器一行，首次由用户指定）。
- `tools/启动评分工具.bat` 无需改路径（自动探测 python/py；页面从 bat 所在目录提供）。
- 若服务端口 8787 被占用：关掉旧「评价工坊服务」窗口后重开 bat，或改 bat 端口并同步改打开 URL。
- **首次 push/pull 如弹出登录**：安装并启用 Git Credential Manager（Git for Windows 通常自带；`git config credential.helper manager` 后，git 会引导浏览器授权，帐号密码不用输入 git 命令行）。
