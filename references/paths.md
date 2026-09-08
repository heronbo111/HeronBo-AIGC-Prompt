# 路径约定（每台机器只维护本文件）

skill 正文一律使用变量名（`${SAMPLES_ROOT}` / `${AI_CREATE_ROOT}`），不写死具体路径。
**换电脑时：只改本文件取值 + 安装工具依赖（Python/ffmpeg），其余零修改。**

| 变量 | 说明 | 本机默认（<用户名>，2026-09-08） | 换机示例 |
|---|---|---|---|
| `${SAMPLES_ROOT}` | 样本库根目录（各实验目录 + 评价工具选中的目录） | `<你的样本库根目录>` | `D:\AI创作\提示词skill生成尝试` |
| `${AI_CREATE_ROOT}` | 参考素材/来源素材根目录 | `<你的素材根目录>` | `D:\AI创作` |

## 换机清单

1. 把本 skill 仓库 clone 到 `~/.zcode/skills/`（ZCode 用户级；Codex 则是 `~/.codex/skills/`）：
   `git clone <gitee仓库地址> C:\Users\你的用户名\.zcode\skills\seedance-prompt`（或从仓库管理页下载 zip 解压）
2. **改本文件**上述两个变量为你机器的实际路径。
3. 建样本库根目录（建议结构：`{实验名}\{文案, 素材, 成片, 废片, 评价, 备注}\`，参考 samples-db.md）。
4. 评分工具：双击 `tools\启动评分工具.bat`（首次需点「连接样本目录」选中你的样本库根；之后自动记忆）。依赖 Python 3（在 PATH）；bat 已通用化，无需改路径。
5. 环境：Edge/Chrome + ffmpeg（素材识别抽帧用）+ 即梦账号（生成在网页完成；CLI 已断开，本 skill 不代提交/不报价）。
6. 全流程自检：`python -m http.server 8787 --directory <本机 tools 路径>` 手动起服务 → 打开 `http://localhost:8787/评价工具.html` 能连目录、能打分、能保存 json。
