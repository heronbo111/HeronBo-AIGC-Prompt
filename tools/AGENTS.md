# tools/ —— 工具链约定（本目录专属，主 `AGENTS.md` 仍然有效）

> 本目录 20+ 个脚本**名字多为中文**（`提示词体检.py`、`换exe.py`…），别改名、别翻译——
> 用户与其它 agent 都按这个名字在找。最后更新：2026-09-21。

## 一句话版

改完脚本，**至少跑 `python tools/联调自检.py`（35 项）**；动了 `references/` 的规则，
**grep 一遍这里的指令副本**（见主文件「维护纪律」第 3 条）。

## 哪些是"公开仓库跟踪"、哪些是"私有留档"

| 文件 | 归属 |
|---|---|
| `workbench_server.py`、`workbench/`、`工作台.py` | **不进公开仓库**（`.gitignore` 已挡）→ 改完必须 `python <私有仓库>\同步.py` |
| `score-tool.spec`、`build_exe.cmd` | 同上 |
| 其它 `*.py` / `*.cmd` / `*.bat` | 公开仓库跟踪，正常提交 |

## 常用命令（本目录里跑）

```bash
python 首次配置.py                       # 自检：已配置 exit 0；未配置 exit 1 并打印要问用户的话
python 联调自检.py                       # 全流程自检（35 项）
python 提示词体检.py --project "<项目>"    # 提示词体检（0 不合格才算交付）
python 部署.py vendor --fetch --yes      # 拉 Release 附件里的大件与工作台 exe
python 换exe.py                          # 换工作台 exe（实例在跑会拒绝——别用 --force 硬换）
python 发布exe附件.py                     # 把大件/exe 发到 Release 附件
python 打市场包.py                        # 打上架用的包
```

## 改脚本时的硬约定

1. **口径不写死在脚本里**：凡是"agent 该怎么做"的规则落 `references/rules.md`，脚本里的指令文案
   **引用条号**（例：「按 rules 第 66 条」）。写死在源码里的口径，换机器就没人读得到。
2. **改"按类型查表"的文案必须同时补表**：`workbench_server.py` 的 `STAGE_NAMES` 之类，
   表里缺一项会**静默**退回默认文案（用户看得见，报错看不见）。
3. **交互期文案只面向用户**：界面/日志里不出现进程名、PID、命令行原文；原始细节收在「执行记录」可展开区。
4. **`.cmd` 批处理必须纯 ASCII + CRLF**（中文一律交给 Python 打印）——cmd.exe 按当前代码页逐字节解析，UTF-8 中文会被切碎。
5. **临时脚本别留在 `tools/`**：调试用完就删；`_vendor/`、`dist/`、`_stage/` 都是生成物，只搜不读。
6. **`project_core.py` 是项目骨架的唯一入口**：回执/待办/清单/@编号都走它；
   出提示词那一轮用 `--assemble` 拼 `提示词.txt`（正文只写一遍），**多版本项目它会被拒**（要自己写）。
