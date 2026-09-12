# 仓库项目交接（2026-09-08 建立）

> 本文件是 HeronBo-AIGC-Prompt skill 仓库的交接入口。新会话开始：先读 `README-安装说明.md`、`references/paths.md`、`TODO.md`，再按 `SKILL.md` 工作流执行。

## 项目
HeronBo-AIGC-Prompt skill（生成 Seedance 口播提示词）+ 六维评分工具，git 仓库跨机/团队使用。
仓库根：`C:\Users\<用户名>\.zcode\skills\HeronBo-AIGC-Prompt\`（git init，main；规则截至第21条，2026-09-08）。

## 远程与同步
- **双远程**：GitHub 私有 `heronbo111/HeronBo-AIGC-Prompt`（作者主库）；Gitee 公开 `HeronBo/HeronBo-AIGC-Prompt`（国内直连；公开版已脱敏，真实数据只存各机本机样本库）。
- **同步节奏（2026-09-08 用户定）**：每周一 09:00 自动推送双远程（cron 已建，automation-45fe6fb0）；平时改动只提交不推送；急更新可手动 push。
- Gitee 推送凭据=私人令牌，已存本机 GCM（`credential.helper=manager`，repo 级已配）。令牌值由用户自行保管；如需撤销重建：平台令牌页删除→新建后重新授权。

## 关键约束
- 交付物只出提示词：永不出现 CLI 命令/积分报价/队列信息（rules.md 第13条）。
- 交付后由 agent 自动启动评分工具请用户打分（SKILL.md 交付节）；按「反馈优化循环」更新规则，并询问是否优化 skill。
- 换机 = clone + 首次使用按固定话术问用户项目位置（写入 `references/paths.md`）+ 装 Python/Edge/Chrome/ffmpeg（README-安装说明.md）。
- 术语约定：规则文件只写「规则+依据（日期/来源）」，不写 agent 推理过程（SKILL.md 第6条）。
