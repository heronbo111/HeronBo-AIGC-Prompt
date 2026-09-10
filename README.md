# seedance-prompt

#### 介绍

把「口播文案/台词 + 素材」自动整理成即梦（Dreamina）Seedance 可直接使用的提示词的技能包，附成片六维评价工具。

- **默认只生成提示词**：交付物中永不出现 CLI 命令、积分价格、队列信息；只有你明确要求代提交时才动手（先报单价、确认后执行）。
- **越用越准**：内置「反馈优化循环」——每次成片反馈（六维评分 / 口头规律 / 废片原因）都会沉淀为已验证规则，写进 `references/rules.md`。

#### 软件架构

```
seedance-prompt/
├── SKILL.md                       # 技能工作流（分类器→分段决策→模板→交付）
├── references/
│   ├── rules.md                   # 硬规则与已验证规律（规范化经验表述，跨机读取）
│   ├── prompt-templates.md        # 三类模板（生成类单段/多段、参考视频类）
│   ├── samples-db.md              # 样本库对照（提示词→成片→评价）
│   ├── platforms.md               # 平台与 CLI（即梦/小云雀/updream；有才装）
│   └── paths.md                   # 路径表模板（本机取值在 paths.local.md，gitignored）
├── tools/
│   ├── 评价工具.html              # 六维评分网页（口型/动作/形象/语速/节奏/违禁项）
│   ├── 启动评分工具.bat           # 一键起服务（localhost:8787，免改路径）
│   ├── 首次配置.py                # 落位/平台登记（写 paths.local.md）
│   └── 评价回收.py                # 待吸收评价清单 + 记账
├── samples/                       # 规律数据（口播稿/提示词/评价json/备注，大视频不进仓）
├── README-安装说明.md             # 详细安装/换机/同步说明
└── TODO.md                        # 仓库项目交接入口
```

- 提交方式（供 agent 决策）：生成类 = 人物图+产品图 + 配音音频；参考视频类 = 原片 + 替换主体。
- 模型选择（2026-09-08 用户裁定，自动判断）：台词时长 ≤15s → seedance2.0fast 单段；>15s → seedance2.5 单段；两段拼接降为备选。

#### 安装教程

1. 克隆仓库（国内直连建议 Gitee：`git clone https://gitee.com/HeronBo/seedance-prompt.git`；作者主库为 GitHub 私有）。
2. 把本目录放到 `~/.zcode/skills/seedance-prompt/`（ZCode）、`~/.codex/skills/`（Codex CLI）或 `~/.dsh/skills/`（DeepSeek Harness），新对话自动识别。
3. 首次使用 agent 问两句：①「请问您要把项目建在哪里？您提供好素材后，我会自动将其进行归类」②「你主要用哪个平台做 AI 视频？即梦 / 小云雀 / updream」→ 建骨架、自动归类素材、按 `references/platforms.md` 装对应 CLI（**有才装、没有不装**），取值写入 `references/paths.local.md`（已 gitignore，不进仓库）。
4. 装依赖：Python 3、Edge/Chrome、ffmpeg（素材识别抽帧）。**不检测账号**，账号由你自己登录。
5. 更新与兼容：本地优化只写 `references/*.local.md`（gitignored），`git pull` 永不冲突；详见 `README-安装说明.md`。

#### 使用说明

1. 触发：直接说「帮我写口播提示词 / 把这段台词变成 Seedance 提示词 / 做分镜提示词」，或 `/seedance-prompt <台词>`。
2. 交付物 = 提示词 + 素材清单（+ 拼接说明）；生成在即梦网页操作。
3. 成片出来后：agent 按 SKILL.md「交付」节自动起评分服务并打开 `localhost:8787/评价工具.html`（手动兜底：双击 `tools\启动评分工具.bat`；首次点「连接样本目录」选样本库根，之后自动记忆）。
4. **评分会被自动回收**：下次会话 agent 跑 `python tools\评价回收.py` 拿「待吸收评价」清单，按反馈循环写进规则/模板并记账（账本 `references/eval-absorbed.local.json`）。
5. 给 agent 反馈（"口型对不上""这条成了"）→ 按反馈循环自动更新规则，越用越准。

#### 参与贡献

1. 使用并反馈：成片反馈/六维评分是规则更新的唯一来源。
2. 同步节奏：作者每周一 09:00 自动推送双远程；各端按需 `git pull`，不必跟随每次改动更新。**本地优化写 `references/*.local.md`（gitignored），所以 pull 不会冲突**；上游 `SKILL.md` 的 `version` 变大时按安装说明复核本地规则。
3. 大视频（成片/废片）不进仓库；真实口播稿/备注只存各机本机样本库，仓库 `samples/` 为脱敏示例，更新流程见 `samples/README.md`。

#### 特技

- 规则优先级：用户实测 > 模板 > 推断；出现 ≥2 次且因果明确的才升为「已验证规律」。
- 引用一致性硬规则：提示词里写的每个 @视频1/@图片1 必须真实存在于上传素材。
- 负面词每段写死：无字幕 / 无运镜 / 无AI畸变。
- 平台无关：提示词文本对即梦/小云雀/updream 通用；CLI 只是可选加速，装不装不影响交付。

#### 开源许可证

MIT License，见 [LICENSE](LICENSE)。
