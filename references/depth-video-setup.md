# 深度视频：安装与使用（L4 复刻动作的前置件）

> 用途：把参考视频转成**黑白深度视频**（只保留人物动作姿态与前后空间关系），给即梦「智能编辑／全能参考」当 @视频1，
> 让模型不再照抄原片的脸/服装/背景/灯光。做法与依据见 `rules.md` 第30条、`replacement-playbook.md` 第二节首选路线。
> 工具：`tools\深度视频.py`（2026-09-11 建，本机实测通过）。

## 一、装了哪几样（一次性）

| 组件 | 本机现状 | 说明 |
|---|---|---|
| ffmpeg / ffprobe | ✅ 已在 PATH | 解码/编码，脚本用它写视频 |
| onnxruntime | ✅ 1.29.0 | 跑 ONNX 深度模型（CPU） |
| opencv-python | ✅ 4.10.0 | 读视频、缩放、灰度 |
| numpy | ✅ 2.5.3 | 张量处理 |
| **ONNX 深度模型** | ✅ 已下到本机缓存 | **99MB，不进仓库**（见下） |

## 二、模型下载（换机时重建，两条路）

模型：**Depth-Anything-V2-Small（ONNX, fp32）**，来源 `onnx-community/depth-anything-v2-small`。
落地目录：`C:\Users\<用户名>\.cache\depth-models\depth-anything-v2-small\`（需要 3 个文件：`model.onnx`、`preprocessor_config.json`、`config.json`）。

**路 A：走代理从 HuggingFace 拉（本机实测 27 秒）**

```bash
D="C:/Users/<用户名>/.cache/depth-models/depth-anything-v2-small"; mkdir -p "$D"
P="http://127.0.0.1:7897"     # 本机 Clash；直连 HF 不通
B="https://huggingface.co/onnx-community/depth-anything-v2-small/resolve/main"
for f in "onnx/model.onnx" "preprocessor_config.json" "config.json"; do
  curl -sL --max-time 600 -x $P "$B/$f" -o "$D/$(basename $f)"
done
ls -la "$D"    # model.onnx 应约 99,060,839 字节
```

**路 B：ModelScope 直连**（HF 打不开时用；搜同名模型 `depth-anything-v2-small` 的 ONNX 版，`modelscope.cn` 本机直连 302 正常）。
> 想更省 CPU 的话可选 `onnx/model_quantized.onnx`（约 25MB），但输入/量化口径要另调，脚本默认按 fp32 版写的，**换权重前先跑 `--check` 验证输入输出**。

## 三、模型路径怎么被找到

优先级（脚本内置）：`--model` 参数 → 环境变量 `DEPTH_MODEL` → `references/paths.local.md` 里的 `DEPTH_MODEL=` → 默认 `~/.cache/depth-models/depth-anything-v2-small/model.onnx`。
本机已在 `paths.local.md` 记了 `DEPTH_MODEL=`（该文件 gitignore，不进仓库）。

## 四、用法

```bash
S="C:/Users/<用户名>/.zcode/skills/seedance-prompt"
PY="C:/Users/<用户名>/AppData/Local/Programs/Python/Python312/python.exe"

"$PY" "$S/tools/深度视频.py" --check "<参考视频>"          # 看参数 + 模型是否就位（不跑，秒出）
"$PY" "$S/tools/深度视频.py" -i "<参考视频>"                # 输出 <同名>_depth.mp4
"$PY" "$S/tools/深度视频.py" -i "<参考视频>" --segment 15    # 按 ≤15 秒自动分段（配合规则17/30）
"$PY" "$S/tools/深度视频.py" -i "<参考视频>" --max-frames 30 # 只跑前 30 帧做短测
"$PY" "$S/tools/深度视频.py" -i "<参考视频>" --invert        # 反相（默认近处亮）
```

**顺序建议**：先 `python tools\竖版画布.py -i "<横版源片>"` 转竖版，**再**转深度（画幅定了再过滤，避免白跑一遍）。

## 五、性能（本机 CPU 实测）

| 项 | 实测 |
|---|---|
| 推理速度 | **1.97 帧/秒**（180 帧用时 91.6s） |
| 一条 14s / 30fps 片子（420 帧） | 约 **3.5–4 分钟**（另有分位抽样开销，约 24 帧） |
| 模型输入 | 518×518（14 的倍数），输出同尺寸相对深度，推理后缩放回原分辨率 |

## 六、无视觉也能验收（照这四条查）

1. `ffprobe` 输出的宽高/帧率/时长与源片一致、**无音轨**；
2. 抽一帧看 R≈G≈B（**是灰度**）；
3. **人物区域（画面中心带）比背景（四周/顶部带）亮**——本机实测：中心带 152.1 vs 顶部带 5.2；
4. 用 `tools\识别工具\ocr.ps1` 抽帧 OCR 应为**无文字**（深度图没有文字信息）。

## 七、排错

| 现象 | 原因 → 处理 |
|---|---|
| `[错误] 找不到深度模型` | 模型没下或路径不对 → 按第二节重建；或 `--model` 指到实际文件 |
| `[错误] 缺 onnxruntime` | `pip install onnxruntime`（本机已装） |
| 慢得离谱 | CPU 单线程/被占满 → 脚本默认留 1 核给系统；先 `--max-frames 30` 短测确认识别没问题 |
| 画面闪（明暗跳动） | 默认已做全片统一归一化；若仍闪，加大 `--range-samples`（如 48）或提高 `--lo/--hi` 到 5/95 |
| 近处反而暗 | 加 `--invert` |

## 八、维护约定

- **模型不进仓库**（99MB，超过 5MB 上限；`.gitignore` 已挡）。仓库里只有脚本与本说明，换机按第二节两条命令重建。
- 改脚本后请用第六节的四条验收跑一遍，并在 `samples-db.md` 记一条样本（日期 + 输入片子 + 用时 + 是否可用）。
- 本工具**只做本地预处理**：不提交任何生成平台、不消耗积分、不碰 `dreamina` CLI。

---

# 配套工具：人物遮罩（`tools\人物遮罩.py`，2026-09-11 建并实测）

**用途**：把参考视频里**人物区域糊掉、场景保持清晰**，做出"只去掉人物外观、保留动作/光线/机位"的驱动片。
它是"深度片（丢场景）"与"原片（锁死身份）"之间的**中间态**——L4 换人时用它当 @视频1，身份锚定与场景丢失两个问题一起规避。

```bash
PY="C:/Users/<用户名>/AppData/Local/Programs/Python/Python312/python.exe"
S="C:/Users/<用户名>/.zcode/skills/seedance-prompt"

# 先出深度片（做掩膜用），再出遮罩片
"$PY" "$S/tools/深度视频.py" -i "<源片>" --max-frames 30          # 可选：先短测
"$PY" "$S/tools/人物遮罩.py" -i "<源片>" --depth "<源片>_depth.mp4"     # 输出 <源片>_masked.mp4
"$PY" "$S/tools/人物遮罩.py" -i "<源片>" --depth "<...>" --mode solid    # 涂纯色块（不推荐，易被模型画出来）
"$PY" "$S/tools/人物遮罩.py" --check "<遮罩片>" --ref "<源片>"           # 只验收
```

- **掩膜怎么来**：深度片 Otsu 近/远分割 → 最大连通域 → 膨胀；**再并上 YuNet 人脸框加固**（外扩 1.6 倍，保证脸一定被糊）；`--band 0.815,0.915` 默认把底部字幕带一起糊。
- **性能**：与深度片同量级（无推理，仅模糊+编码）——本机 1080×1920 / 412 帧约 **2.5 分钟**。
- **验收（`--check` 自动跑）**：抽 7 个时间点检人脸 + 比对脸区清晰度。**本机实测**：7/7 时间点身份已去除（其中 1 个是模糊块误检：清晰度 3.0 vs 原片 21.9），场景区清晰度 82.8（人物区 14.6）。
- **依赖约束（已处理）**：opencv 的 ONNX 读取**不支持非 ASCII 路径**，而本仓库路径含"识别工具"中文目录 → 工具会先把模型拷到 `%TEMP%\yunet_tool.onnx` 再加载（`tools\竖版画布.py` 也是同一处理）。**自己写脚本时记得同样处理**。
- **边界**：同样只做本地预处理，不提交任何平台、不消耗积分。
