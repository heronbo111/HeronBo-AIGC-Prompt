# 仓库项目交接（2026-09-08 新建独立对话/项目使用）

> 本文件是 seedance-prompt skill 仓库的独立项目交接入口。新对话开场直接说：
> 「继续 seedance-prompt 仓库项目，先读本目录 README-安装说明.md、references/paths.md、TODO.md 和 ZCode 记忆 project-seedance-prompt-skill.md」

## 项目是什么
seedance-prompt skill（只生成 Seedance 口播提示词）+ 六维评分工具，做成 git 仓库跨机/团队使用。
仓库根：`C:\Users\<用户名>\.zcode\skills\seedance-prompt\`（git 已 init，main，首个 commit 5682f92，9 文件）。

## 当前状态（2026-09-08 晚 完成双远程）
- v1.1 跨机化、v1.2 规律数据入库均已提交；并行会话持续更新规则（现至第15条：模型自读台词易读错，样本4 转 v3 配音驱动——以仓库为准）。
- **双远程已建并推送完成**：GitHub **私有** `heronbo111/seedance-prompt`（作者主库）；Gitee **公开** `HeronBo/seedance-prompt`（团队同事用；该账号"私有"选项禁用=仅支持公开仓库，若日后解锁可在设置转私有）。Gitee 推送凭据=私人令牌，已存入本机 GCM（`credential.helper=manager`，repo 级已配）。
- 备注：Gitee 私人令牌生成需账号密码验证（用户体验待优化点）；token 值请用户自行保管（聊天记录中含一份，安全性自担/建议必要时撤销重建）。

## 下一条行动线（无阻塞，可选优化）
1. 若 Gitee 账号解锁私有权限：仓库设置改私有 + 更新本 README 的可见性描述。
2. 令牌轮换：如需撤销/重建（Gitee 私人令牌页 -> 删除 -> 新建），重建后需重存 GCM 凭据（`printf "protocol=https\nhost=gitee.com\nusername=HeronBo\npassword=新令牌\n\n" | git credential approve`）。
3. **同步节奏（2026-09-08 用户定）**：每周一 09:00 自动推送 cron 已建（automation-45fe6fb0）；平时改动只提交不推送，周推送前想提前让同事拿到可在任意会话手动 push。

## 关键约束（务必保持）
- 交付物只出提示词：永不出现 CLI 命令/积分报价/队列信息（rules.md 第13条；READ ME 铁律5）。
- 交付后主动打开 tools\启动评分工具.bat 请用户打分，按反馈循环更新，并询问是否优化 skill。
- 换机 = clone + 改 references/paths.md 两个变量 + 装 Python/Chrome/ffmpeg（详见 README 换机三件套）。
