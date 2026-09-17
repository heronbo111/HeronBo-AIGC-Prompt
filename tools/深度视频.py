# -*- coding: utf-8 -*-
"""参考视频 → 黑白深度视频（L4「复刻动作/换人换环境」的预处理，对应 rules.md 第30条 + playbooks.md 第一部分）。

设计（2026-09-11 v1.0）：
- **为什么要做**：原参考视频自带模特的脸、服装、背景、灯光与整体风格；直接当 @视频1 时模型会把这些
  一起照抄，替换指令被"视频锚定"压制（规则20b，三本书 B 版实证：书和衣服都没换）。
  转成深度视频＝做一次**信息过滤**，只保留人物动作姿态与前后空间关系。
- **模型**：Depth-Anything-V2-Small（ONNX，fp32），输入 518×518，输出**相对深度**（数值越大=离镜头越近）。
  灰度规则：近处亮（可用 --invert 反过来）。模型文件不进仓库，见 references/playbooks.md 第三部分。
- **归一化**：默认先抽 N 帧估出全片 2%/98% 分位，再做**全片统一拉伸**——逐帧 min-max 会闪、深度也会跳。
- **输出**：8bit 灰度、libx264/yuv420p；`--segment N` 按 ≤N 秒自动分段（配合规则17/30 的 15 秒切分习惯）。
- **边界**：只做本地预处理，不提交任何生成平台、不消耗积分、不碰 dreamina CLI。

用法：
    python 深度视频.py --check "<视频>"                    # 看参数 + 模型是否就位
    python 深度视频.py -i "<参考视频>"                      # 输出 "<同名>_depth.mp4"
    python 深度视频.py -i "<参考视频>" --segment 15          # 按 ≤15 秒自动分段
    python 深度视频.py -i "<参考视频>" --max-frames 30       # 只跑前 30 帧，做短测
    python 深度视频.py -i "<参考视频>" --invert              # 反相（远处亮）

参数要点：
    --model      模型路径；默认依次取：命令行 > 环境变量 DEPTH_MODEL > paths.local.md 里的 DEPTH_MODEL
                 > ~/.cache/depth-models/depth-anything-v2-small/model.onnx
    --size       模型输入边长，默认 518（Depth-Anything-V2 标准）
    --per-frame  逐帧各自归一化（默认全片统一，防闪）
    --crf        x264 质量，默认 18；--preset 默认 veryfast
依赖：ffmpeg/ffprobe 在 PATH；onnxruntime + opencv-python + numpy。
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)
PATHS_LOCAL = os.path.join(SKILL_ROOT, "references", "paths.local.md")
DEFAULT_MODEL = os.path.join(
    os.path.expanduser("~"), ".cache", "depth-models", "depth-anything-v2-small", "model.onnx"
)
# 仓库自带的那份模型（装机时随 skill 一起走，省掉"HF 下模型还要代理"这一步）
BUNDLED_MODEL = os.path.join(HERE, "_vendor", "models", "depth-anything-v2-small", "model.onnx")


def pick_model(explicit=""):
    """挑模型文件：显式给的 > ~/.cache（用户自己下的）> 仓库自带的。"""
    if explicit:
        return explicit
    if os.path.isfile(DEFAULT_MODEL):
        return DEFAULT_MODEL
    if os.path.isfile(BUNDLED_MODEL):
        return BUNDLED_MODEL
    return DEFAULT_MODEL
MEAN = (0.485, 0.456, 0.406)
STD = (0.229, 0.224, 0.225)

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


def need(tool):
    p = shutil.which(tool)
    if not p:      # 仓库自带的那份（装机时 python tools/deploy.py install 会解到 tools/_vendor/ffmpeg/bin）
        _c = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "_vendor", "ffmpeg", "bin", tool + ".exe")
        p = _c if os.path.isfile(_c) else None
    if not p:
        sys.exit("[错误] 找不到 %s：系统 PATH 里没有，仓库自带的 tools/_vendor/ffmpeg/bin 里也没有。" % tool
                 + "\n        跑一次 python tools/deploy.py install 会自动解开仓库自带的那份，"
                 + "或者自己装 ffmpeg 并加入 PATH。")
    return p

def probe(path):
    """读视频信息：宽/高/时长/帧率/有无音轨。"""
    ffprobe = need("ffprobe")
    cmd = [ffprobe, "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", path]
    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if out.returncode != 0:
        sys.exit("[错误] ffprobe 读不了这个文件：%s\n%s" % (path, out.stderr.strip()))
    data = json.loads(out.stdout or "{}")
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    a = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    fr = v.get("avg_frame_rate") or v.get("r_frame_rate") or "30/1"
    try:
        num, den = fr.split("/")
        fps = float(num) / float(den) if float(den) else 30.0
    except Exception:
        fps = 30.0
    dur = data.get("format", {}).get("duration")
    return {
        "width": int(v.get("width") or 0),
        "height": int(v.get("height") or 0),
        "fps": round(fps, 3),
        "duration": round(float(dur), 3) if dur else None,
        "has_audio": a is not None,
    }


def resolve_model(explicit=None):
    if explicit:
        return explicit
    env = os.environ.get("DEPTH_MODEL")
    if env:
        return env
    try:
        if os.path.isfile(PATHS_LOCAL):
            text = open(PATHS_LOCAL, encoding="utf-8").read()
            m = re.search(r"^\s*DEPTH_MODEL\s*[=:]\s*(\S+)\s*$", text, re.M)
            if m and os.path.isfile(m.group(1)):
                return m.group(1)
    except Exception:
        pass
    return pick_model()          # ~/.cache 优先，其次仓库自带的 tools/_vendor/models/…


def load_session(model_path, size):
    if not os.path.isfile(model_path):
        sys.exit("[错误] 找不到深度模型：%s\n       下载方式见 references/playbooks.md 第三部分（一次性，约 100MB）。"
                 % model_path)
    try:
        import onnxruntime as ort
    except ImportError:
        sys.exit("[错误] 缺 onnxruntime，请先 pip install onnxruntime。")
    so = ort.SessionOptions()
    so.log_severity_level = 3
    so.intra_op_num_threads = max(1, (os.cpu_count() or 4) - 1)
    return ort.InferenceSession(model_path, so, providers=["CPUExecutionProvider"])


def infer(sess, frame_bgr, size):
    """一帧 → 深度图（float32，越大越近）。"""
    import cv2
    import numpy as np
    img = cv2.resize(frame_bgr, (size, size), interpolation=cv2.INTER_AREA)
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB).astype("float32") / 255.0
    img = (img - np.array(MEAN, dtype="float32")) / np.array(STD, dtype="float32")
    x = np.transpose(img, (2, 0, 1))[None, ...]
    out = sess.run(None, {sess.get_inputs()[0].name: x})[0]
    return out[0]


def depth_to_gray(depth, lo, hi, invert):
    import cv2
    import numpy as np
    d = depth.astype("float32")
    if hi - lo < 1e-6:
        lo, hi = float(d.min()), float(d.max()) + 1e-6
    g = np.clip((d - lo) / (hi - lo), 0.0, 1.0)
    if invert:
        g = 1.0 - g
    return (g * 255.0).astype("uint8")


def open_writer(ffmpeg, path, w, h, fps, crf, preset):
    return subprocess.Popen(
        [ffmpeg, "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "gray",
         "-s", "%dx%d" % (w, h), "-r", "%.6f" % fps, "-i", "-",
         "-an", "-c:v", "libx264", "-crf", str(crf), "-preset", preset,
         "-pix_fmt", "yuv420p", path],
        stdin=subprocess.PIPE,
    )


def main():
    ap = argparse.ArgumentParser(description="参考视频 → 黑白深度视频（信息过滤，供 Seedance 复刻动作）")
    ap.add_argument("-i", "--input", help="输入视频")
    ap.add_argument("-o", "--output", help="输出路径；--segment 时作为模板（默认 <同名>_depth.mp4）")
    ap.add_argument("--check", help="只看视频参数与模型是否就位，不做转换")
    ap.add_argument("--model", help="ONNX 模型路径（默认 ~/.cache/depth-models/...）")
    ap.add_argument("--size", type=int, default=518, help="模型输入边长，默认 518")
    ap.add_argument("--segment", type=float, default=0, help="按 ≤N 秒自动分段（如 15）")
    ap.add_argument("--max-frames", type=int, default=0, help="只处理前 N 帧（短测用）")
    ap.add_argument("--invert", action="store_true", help="反相：远处亮（默认近处亮）")
    ap.add_argument("--per-frame", action="store_true", help="逐帧归一化（默认全片统一，防闪）")
    ap.add_argument("--range-samples", type=int, default=24, help="全片分位抽样的帧数，默认 24")
    ap.add_argument("--lo", type=float, default=2.0, help="下分位，默认 2（%）")
    ap.add_argument("--hi", type=float, default=98.0, help="上分位，默认 98（%）")
    ap.add_argument("--crf", type=int, default=18, help="x264 CRF，默认 18")
    ap.add_argument("--preset", default="veryfast", help="x264 preset，默认 veryfast")
    ap.add_argument("--fps", type=float, default=0, help="输出帧率（默认跟源）")
    args = ap.parse_args()

    ffmpeg = need("ffmpeg")

    if args.check:
        info = probe(args.check)
        model = resolve_model(args.model)
        print("视频：%s" % args.check)
        print("  %dx%d  %.3f fps  时长 %s s  音轨 %s"
              % (info["width"], info["height"], info["fps"], info["duration"],
                 "有" if info["has_audio"] else "无"))
        print("模型：%s  %s" % (model, "就位 ✓" if os.path.isfile(model) else "缺失 ✗"))
        if os.path.isfile(model):
            s = load_session(model, args.size)
            print("  输入 %s / 输出 %s" % (s.get_inputs()[0].shape, s.get_outputs()[0].shape))
        return

    if not args.input:
        ap.error("要么给 --check，要么给 -i")
    if not os.path.isfile(args.input):
        sys.exit("[错误] 找不到输入视频：%s" % args.input)

    import cv2
    import numpy as np

    info = probe(args.input)
    w, h, fps = info["width"], info["height"], (args.fps or info["fps"])
    src_name = os.path.splitext(os.path.basename(args.input))[0]
    out_base = args.output or os.path.join(os.path.dirname(os.path.abspath(args.input)),
                                           src_name + "_depth.mp4")

    sess = load_session(resolve_model(args.model), args.size)
    cap = cv2.VideoCapture(args.input)
    if not cap.isOpened():
        sys.exit("[错误] opencv 打不开这个视频：%s" % args.input)

    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if args.max_frames:
        total = min(total, args.max_frames) if total else args.max_frames
    seg_frames = int(round(args.segment * fps)) if args.segment else 0

    # ① 全片分位（抽样）——避免逐帧归一化导致的闪烁
    lo = hi = None
    if not args.per_frame:
        n = max(3, args.range_samples)
        step = max(1, (total or n) // n)
        vals = []
        idx = 0
        while True:
            ok = cap.grab()
            if not ok:
                break
            if idx % step == 0:
                ok, fr = cap.retrieve()
                if not ok:
                    break
                vals.append(infer(sess, fr, args.size).ravel())
            idx += 1
            if args.max_frames and idx >= args.max_frames:
                break
        if vals:
            allv = np.concatenate(vals)
            lo = float(np.percentile(allv, args.lo))
            hi = float(np.percentile(allv, args.hi))
        cap.release()
        cap = cv2.VideoCapture(args.input)
        print("全片深度范围（分位 %.0f%%/%.0f%%）：%.3f ~ %.3f" % (args.lo, args.hi, lo, hi))

    # ② 逐帧推理 + 写视频（可分段）
    outputs = []
    writer = None
    seg_idx = 1
    seg_path = out_base

    def start_writer(path):
        return open_writer(ffmpeg, path, w, h, fps, args.crf, args.preset)

    writer = start_writer(seg_path)
    outputs.append(seg_path)
    done = 0
    t0 = time.time()
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        depth = infer(sess, frame, args.size)
        d_lo, d_hi = (float(depth.min()), float(depth.max())) if args.per_frame else (lo, hi)
        gray = depth_to_gray(depth, d_lo, d_hi, args.invert)
        gray = cv2.resize(gray, (w, h), interpolation=cv2.INTER_LINEAR)
        try:
            writer.stdin.write(gray.tobytes())
        except (BrokenPipeError, OSError):
            sys.exit("[错误] ffmpeg 写入中断，请检查输出路径是否可写：%s" % seg_path)
        done += 1
        if seg_frames and done % seg_frames == 0:
            writer.stdin.close()
            writer.wait()
            seg_idx += 1
            seg_path = os.path.splitext(out_base)[0] + "_%02d.mp4" % seg_idx
            writer = start_writer(seg_path)
            outputs.append(seg_path)
        if done % 30 == 0 or done == total:
            el = time.time() - t0
            eta = (el / done) * (total - done) if total and done else 0
            print("  已处理 %d/%s 帧  用时 %.0fs  预计剩余 %.0fs"
                  % (done, total or "?", el, eta), end="\r")
        if args.max_frames and done >= args.max_frames:
            break

    if writer and writer.stdin:
        writer.stdin.close()
        writer.wait()
    cap.release()
    el = time.time() - t0
    print("\n完成：%d 帧，用时 %.1fs（%.2f 帧/秒）" % (done, el, done / el if el else 0))
    for p in outputs:
        size = os.path.getsize(p) if os.path.isfile(p) else 0
        print("  输出：%s  %.2f MB" % (p, size / 1048576.0))
    print("提示：竖版交付前先跑 tools\\竖版画布.py 把源片转竖版，再转深度（顺序：竖版 → 深度）。")


if __name__ == "__main__":
    main()
