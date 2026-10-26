# heronbo-aigc-prompt

HeronBo 视频提示词 skill 的 npm 分发（把技能装进 agent 的 skills 目录）。

```bash
npm i -g heronbo-aigc-prompt     # 全局安装，自动拷到检测到的 agent skills 目录
npx heronbo-aigc-prompt          # 或者只跑一次安装器
```

安装器会往这些**已存在**的目录各放一份 `heronbo-aigc-prompt/`：

- `~/.zcode/skills/`
- `~/.agents/skills/`
- `~/.codebuddy/skills/`
- `~/.claude/skills/`
- `~/.codex/skills/`

指定别的目录：`HERONBO_SKILL_DIRS="D:/某处/skills" npm i -g heronbo-aigc-prompt`

装好后在 agent 里说「用 HeronBo 做视频提示词」即可触发。工具脚本需要 Python 3.9+；
第一次可运行 `python tools/环境检查.py` 看依赖是否齐。

完整工作台（图形界面、豆包/即梦通道、在线更新）请用安装包，不在 npm 包里。
