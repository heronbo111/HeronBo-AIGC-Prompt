## 改了什么

<!-- 一两句说清意图；细节留给 diff -->

## 对使用者的影响

- [ ] 改了规则/模板口径（`references/`）→ 需要发新版 / 用户在别处 pull
- [ ] 改了工具链（`tools/`）→ 需要用户重跑某条命令
- [ ] 只是想改提示词交付形态，影响每一份产物
- [ ] 只是措辞/文档，无功能影响

## 自检

- [ ] `python tools/首次配置.py` 已配置时 exit 0
- [ ] `python tools/联调自检.py` 全过（35 项）
- [ ] 改了 `prompt-templates.md` 的「交付形态」→ 已 grep `workbench_server.py` 的指令段有无旧口径
- [ ] `SKILL.md` frontmatter 完整；`version` 已递增（发布类改动）
- [ ] 没把用户账号/本机绝对路径/客户名写进公开文件
