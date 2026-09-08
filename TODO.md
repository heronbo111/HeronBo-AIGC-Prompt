# 仓库项目交接（2026-09-08 新建独立对话/项目使用）

> 本文件是 seedance-prompt skill 仓库的独立项目交接入口。新对话开场直接说：
> 「继续 seedance-prompt 仓库项目，先读本目录 README-安装说明.md、references/paths.md、TODO.md 和 ZCode 记忆 project-seedance-prompt-skill.md」

## 项目是什么
seedance-prompt skill（只生成 Seedance 口播提示词）+ 六维评分工具，做成 git 仓库跨机/团队使用。
仓库根：`C:\Users\<用户名>\.zcode\skills\seedance-prompt\`（git 已 init，main，首个 commit 5682f92，9 文件）。

## 当前状态（2026-09-08 晚更新）
- v1.1 跨机化已完成；**v1.2 规律数据入库已完成**：`samples/` 收录 4 个实验的口播稿/提示词/评价 json/备注（大视频不进仓），.gitignore 已放行 samples 评价 json，README「同步与版本」已更新。提交 9a7ecf8。
- **远程仓库尚未创建/推送**：用户 2026-09-08 晚已选定「用浏览器帮我建仓」（Gitee 私有 + GitHub 私有，均命名 seedance-prompt 空仓库）；但浏览器探查发现 **Edge 里 Gitee/GitHub 均未登录**——登录页标签页已打开（Gitee /projects/new、GitHub /new），等用户登录后继续。
- 推送凭据已备好：本机 Git Credential Manager 2.9 可用，已配 repo 级 `credential.helper=manager`（`git config --local credential.helper manager`）。

## 下一条行动线（用户登录后执行）
1. 在已打开的登录页确认用户登录 Gitee + GitHub（或让用户直接给 Gitee 私人令牌 + GitHub PAT 改走 API）。
2. 创建两个私有空仓库（不选初始化文件），创建前向用户复述一次仓库名/公开性。
3. ```bash
   cd C:\Users\<用户名>\.zcode\skills\seedance-prompt
   git remote add gitee https://gitee.com/<用户名>/seedance-prompt.git
   git remote add github https://github.com/<用户名>/seedance-prompt.git
   git push -u gitee main && git push -u github main
   ```
4. 首次 push 会弹 GCM 浏览器授权：GitHub 登录态可自动通过；Gitee 走通用凭据页，按提示输入 Gitee 密码或私人令牌。
- Gitee=给同事用（国内直连），GitHub=作者编辑；之后规则更新走 push 双远程，同事 git pull。
- 已解决：规律数据入库（samples/ 见 samples/README.md，含更新流程）。

## 关键约束（务必保持）
- 交付物只出提示词：永不出现 CLI 命令/积分报价/队列信息（rules.md 第13条；READ ME 铁律5）。
- 交付后主动打开 tools\启动评分工具.bat 请用户打分，按反馈循环更新，并询问是否优化 skill。
- 换机 = clone + 改 references/paths.md 两个变量 + 装 Python/Chrome/ffmpeg（详见 README 换机三件套）。
