# 仓库项目交接（2026-09-08 新建独立对话/项目使用）

> 本文件是 seedance-prompt skill 仓库的独立项目交接入口。新对话开场直接说：
> 「继续 seedance-prompt 仓库项目，先读本目录 README-安装说明.md、references/paths.md、TODO.md 和 ZCode 记忆 project-seedance-prompt-skill.md」

## 项目是什么
seedance-prompt skill（只生成 Seedance 口播提示词）+ 六维评分工具，做成 git 仓库跨机/团队使用。
仓库根：`C:\Users\<用户名>\.zcode\skills\seedance-prompt\`（git 已 init，main，首个 commit 5682f92，9 文件）。

## 当前状态（2026-09-08）
- v1.1 跨机化改造已完成：路径抽离到 references/paths.md（正文变量），评价工具+通用 bat 收进 tools/，README v1.1。
- **远程仓库尚未创建/推送**：等待用户提供 Gitee + GitHub 私有空仓库地址（本机无 gh CLI；git 身份=仓库级占位 <用户名>/wukong@zcode.local）。

## 下一条行动线（拿到仓库地址后执行）
```bash
cd C:\Users\<用户名>\.zcode\skills\seedance-prompt
git remote add gitee <gitee地址> && git remote add github <github地址>
git push -u gitee main && git push -u github main
```
- Gitee=给同事用（国内直连），GitHub=作者编辑；之后规则更新走 push 双远程，同事 git pull。
- 待用户决策项：是否把样本库的「规律数据」（各实验 `评价/*.json` + `文案/`）也收进仓库（大视频继续 .gitignore）；建议收，待确认后设计目录与同步方式。

## 关键约束（务必保持）
- 交付物只出提示词：永不出现 CLI 命令/积分报价/队列信息（rules.md 第13条；READ ME 铁律5）。
- 交付后主动打开 tools\启动评分工具.bat 请用户打分，按反馈循环更新，并询问是否优化 skill。
- 换机 = clone + 改 references/paths.md 两个变量 + 装 Python/Chrome/ffmpeg（详见 README 换机三件套）。
