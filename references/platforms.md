# 平台与 CLI（安装后问用户用哪个平台）

**安装完成后问一次**（固定话术，与「项目位置」同一轮问）：

> 你主要用哪个平台做 AI 视频？即梦 / 小云雀 / updream

只处理用户指定的平台；**只在该平台确实有 CLI 时才安装，没有就不装**。**不检测账号、不代登录**（账号由用户自己完成登录）。

| 平台 | CLI | 检测命令 | 安装方式（用户同意后才执行） |
|---|---|---|---|
| 即梦 Dreamina | `dreamina` | `dreamina --version` | 官方脚本 `curl -fsSL https://jimeng.jianying.com/cli \| bash`（Windows 用 Git Bash 执行或按官方页面指引；本机已装在 `%USERPROFILE%\bin\dreamina.exe`，数据目录 `%USERPROFILE%\.dreamina_cli\`） |
| 小云雀（xyq） | `pippit-tool-cli`（npm 包 `@pippit-dev/cli`） | `pippit-tool-cli --version` 或 `npx -y @pippit-dev/cli --version` | `npm i -g @pippit-dev/cli`（支持 win32/macOS/Linux）；使用前需用户提供 `XYQ_ACCESS_KEY`（Bearer 令牌，用户自行申请，**不要写入仓库**） |
| updream | 暂无公开 CLI（npm 无同名包；官方有 Skill 社区） | `updream --version`（找不到即视为无 CLI） | **不装**，在网页/客户端操作 |

## 处理规则

1. 检测到 CLI → 在 `references/paths.local.md` 记 `PLATFORM=<平台>`、`CLI=<命令>`；交付物仍然**只有提示词文本**（不出现 CLI 命令、积分、队列）。
2. 没检测到但该平台确实有 CLI → 先说明安装命令与影响，**问用户是否安装**，同意后再执行；不擅自安装、不擅自升级。
3. 该平台没有 CLI（如 updream 目前）→ 只记 `PLATFORM=updream`，不装任何东西，生成在网页完成。
4. 用户换平台 → 重问一次上面的问句，更新 `paths.local.md`。
5. 即使装了 CLI，本 skill **默认不代提交生成**（要代提交须先报单价、用户确认；该内容不进常规交付）。

## 维护

平台/CLI 有变动时只改本表；`SKILL.md` 只引用本文件，不写死命令。
