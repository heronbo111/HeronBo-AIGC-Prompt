# TODO

> 交接入口：新会话先读 `README.md` → `references/paths.md` → 本文件 → `SKILL.md`。

## 待办

- **L4「换人＋保原片」路线跑通**：三条老路（通用模型单步替换 / 深度片·遮罩片 / 外部付费工具）已实测否定，现走替代方向（只换台词、只换物件、目标图驱动、换素材）。免费路线入口见 `references/playbooks.md` 第一部分「外部工具」。**装任何软件前先问用户**，未跑通前一律标「待验证」。
- **文生视频模板补样本**：`references/prompt-templates.md` 模板 F 目前**未验证**（本机暂无文生视频样本），第一条真实需求进来后补样本并转正。
- **MiniMax H3 落地方式确认**：本地 ComfyUI 分片推理 / 云端 API 二选一，定了再补 `references/platforms.md` 的写法差异。**外部平台先核价、软件先问用户。**
- **样本参数补全**：生成参数（模型版本/入口/分辨率/抽卡次数）缺失 = 信息丢失，收集新样本时优先补；样本2「神临 vs 天降.mp4」名字待归一。
- **规则转正**：`rules.md` 中带「待验证」的条目，等成片验证后转正或推翻；真实失败案例同步补进 `references/eval-cases.md`。

## 维护约定

- 改完 `rules.md` / `prompt-templates.md` → 跑 `references/eval-cases.md` 回归；再跑 `python tools\评价回收.py` 收评价。
- 模型或限额有变动 → 只改 `references/platforms.md` 的「模型与限额」表。
- 协作写前规则见 `AGENTS.md`；本机取值只写 `references/*.local.md`（gitignored）。
