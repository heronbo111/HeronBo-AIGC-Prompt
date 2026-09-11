# -*- coding: utf-8 -*-
"""参考视频 → 人物遮罩片（把"人物长什么样"这层信息抹掉，保留动作/场景/光线/机位）。

配套：`tools\深度视频.py`（先出深度片，本工具用它当掩膜）。

设计（2026-09-11 v1.0，对应 rules 第30/32 条与 replacement-playbook 第二节）：
- **为什么要做**：L4"换人+复刻动作"失败的两个极端——
  ① 用原片：原片自带的脸/服装/背景把替换指令压死（规则20b）；
  ② 用深度片：连场景/光线/机位一起丢了（信息过滤过头）。
  本工具取中间态：**只把人物区域糊掉**，场景保持清晰 → "要保留的"与"要替换的"从素材层就分开。
- **掩膜怎么来**：`--depth` 深度片做 Otsu 近/远分割 → 取最大连通域 → 膨胀；**再并上人脸框加固**
  （YuNet 检出的人脸向外扩 N 倍，保证脸一定被糊）；可选把底部字幕带一起糊（`--band`）。
- **输出**：默认高斯模糊（`--mode blur --sigma 35`）；也可 `--mode solid` 涂纯色块（更像"遮挡"但不推荐：
  纯色块容易被模型当成画面元素画出来）。
- **验收**：`--check` 只跑验证——抽帧检人脸 + 比对脸区清晰度（清晰度大幅下降=身份已去除；若仍清晰=掩膜没盖住）。

用法：
    python 人物遮罩.py -i "<源片>" --depth "<深度片>"                 # 输出 "<源片>_masked.mp4"
    python 人物遮罩.py -i "<源片>" --depth "<深度片>" --mode solid     # 涂纯色块
    python 人物遮罩.py --check "<遮罩片>" --ref "<源片>"               # 只验证（人脸 + 清晰度）
    python 人物遮罩.py -i "<源片>" --depth "<深度片>" --max-frames 60  # 短测

依赖：ffmpeg / ffprobe 在 PATH；opencv-python + numpy；`识别工具/face_detection_yunet_2023mar.onnx`（缺则跳过人脸加固，仅用深度掩膜）。
只做本地预处理：不提交任何生成平台、不消耗积分。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
YUNET = os.path.join(HERE, "识别工具", "face_detection_yunet_2023mar.onnx")

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


def need(tool):
    p = shutil.which(tool)
    if not p:
        sys.exit("[错误] 找不到 %s，请先装 ffmpeg 并加入 PATH。" % tool)
    return p


def probe(path):
    ffprobe = need("ffprobe")
    out = subprocess.run([ffprobe, "-v", "error", "-print_format", "json",
                          "-show_format", "-show_streams", path],
                         capture_output=True, text=True, encoding="utf-8")
    if out.returncode != 0:
        sys.exit("[错误] ffprobe 读不了这个文件：%s" % path)
    d = json.loads(out.stdout or "{}")
    v = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), {})
    fr = v.get("avg_frame_rate") or "30/1"
    try:
        n, dn = fr.split("/")
        fps = float(n) / float(dn) if float(dn) else 30.0
    except Exception:
        fps = 30.0
    return {"w": int(v.get("width") or 0), "h": int(v.get("height") or 0), "fps": fps}


def grab(video, t, tmp):
    p = os.path.join(tmp, "grab.png")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(t), "-i", video,
                    "-frames:v", "1", p], check=True)
    import cv2
    return cv2.imread(p)


def local_model(tmp):
    """opencv 的 ONNX 读取不支持非 ASCII 路径（本仓库路径含中文），先拷到临时目录再加载。"""
    if not os.path.isfile(YUNET):
        return None
    dst = os.path.join(tmp, "yunet_tool.onnx")
    try:
        if not os.path.isfile(dst) or os.path.getsize(dst) != os.path.getsize(YUNET):
            shutil.copyfile(YUNET, dst)
        return dst
    except Exception as e:
        print("[提示] 模型拷到临时目录失败（%s），跳过人脸加固" % e)
        return None


def do_check(media, ref, tmp):
    """验证：抽帧检人脸 + 脸区清晰度（遮罩片 vs 参考片）。"""
    import cv2
    import numpy as np
    probe(media)
    times = [0.5, 2, 4, 6, 8, 10, 12]
    faced = 0
    ok = 0
    model = local_model(tmp)
    for t in times:
        a = grab(media, t, tmp)
        if a is None:
            continue
        h, w = a.shape[:2]
        line = "  t=%4.1fs  " % t
        if not model:
            line += "（缺 YuNet，跳过人脸检查）"
            print(line)
            continue
        det = cv2.FaceDetectorYN.create(model, "", (w, h), 0.5, 0.3, 5000)
        det.setInputSize((w, h))
        _, f = det.detect(a)
        if f is None or not len(f):
            print(line + "0 人脸 ✓ 身份已去除")
            ok += 1
            continue
        b = grab(ref, t, tmp) if ref else None
        x, y, fw, fh = [int(v) for v in f[0][:4]]
        x2, y2 = min(w, x + fw), min(h, y + fh)
        va = float(cv2.Laplacian(cv2.cvtColor(a[max(0, y):y2, max(0, x):x2], cv2.COLOR_BGR2GRAY), cv2.CV_32F).var())
        if b is None:
            print(line + "检出人脸，脸区清晰度=%.1f（无参考片可对比）" % va)
            faced += 1
            continue
        vb = float(cv2.Laplacian(cv2.cvtColor(b[max(0, y):y2, max(0, x):x2], cv2.COLOR_BGR2GRAY), cv2.CV_32F).var())
        if va < vb * 0.35:
            print(line + "误检（模糊块）✓ 身份已模糊：清晰度 %.1f vs 原片 %.1f" % (va, vb))
            ok += 1
        else:
            print(line + "⚠️ 脸仍清晰：%.1f vs 原片 %.1f —— 掩膜没盖住" % (va, vb))
            faced += 1
    print("结论：%d/%d 个时间点身份已去除（其余 %d 个仍清晰）" % (ok, len(times), faced))


def main():
    ap = argparse.ArgumentParser(description="参考视频 → 人物遮罩片（抹掉人物外观，保留动作与场景）")
    ap.add_argument("-i", "--input", help="源视频（要处理的那条）")
    ap.add_argument("--depth", help="深度片（tools\\深度视频.py 产出），用于生成人物掩膜")
    ap.add_argument("-o", "--output", help="输出路径（默认 <源片>_masked.mp4）")
    ap.add_argument("--mode", choices=["blur", "solid"], default="blur", help="模糊（默认）或纯色块")
    ap.add_argument("--sigma", type=float, default=35.0, help="高斯模糊强度，默认 35")
    ap.add_argument("--face-expand", type=float, default=1.6, help="人脸框外扩倍数，默认 1.6（覆盖头+肩）")
    ap.add_argument("--band", default="0.815,0.915", help="底部字幕带（比例起,止；留空=不处理）")
    ap.add_argument("--no-dilate", action="store_true", help="不做掩膜膨胀（默认膨胀两轮 41px）")
    ap.add_argument("--max-frames", type=int, default=0, help="只处理前 N 帧（短测）")
    ap.add_argument("--check", help="只验证某个遮罩片（配合 --ref）")
    ap.add_argument("--ref", help="--check 用的参考原片（对比脸区清晰度）")
    args = ap.parse_args()

    import cv2
    import numpy as np
    tmp = os.environ.get("TEMP") or "/tmp"

    if args.check:
        do_check(args.check, args.ref, tmp)
        return
    if not args.input or not args.depth:
        ap.error("要么 --check，要么同时给 -i 与 --depth")

    ffmpeg = need("ffmpeg")
    info = probe(args.input)
    w, h, fps = info["w"], info["h"], info["fps"]
    out = args.output or os.path.splitext(args.input)[0] + "_masked.mp4"
    band = None
    if args.band:
        a, b = (float(x) for x in args.band.split(","))
        band = (int(h * a), int(h * b))

    det = None
    model = local_model(tmp)
    if model:
        det = cv2.FaceDetectorYN.create(model, "", (w, h), 0.45, 0.3, 5000)
        det.setInputSize((w, h))
    else:
        print("[提示] 缺 YuNet 模型，只用深度掩膜（脸部加固跳过）")

    cap_s = cv2.VideoCapture(args.input)
    cap_d = cv2.VideoCapture(args.depth)
    ff = subprocess.Popen([ffmpeg, "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
                           "-s", "%dx%d" % (w, h), "-r", "%.6f" % fps, "-i", "-", "-an",
                           "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
                           "-pix_fmt", "yuv420p", out], stdin=subprocess.PIPE)
    n = 0
    t0 = time.time()
    facehit = 0
    while True:
        ok1, f1 = cap_s.read()
        ok2, f2 = cap_d.read()
        if not ok1 or not ok2:
            break
        g = cv2.cvtColor(f2, cv2.COLOR_BGR2GRAY)
        _, near = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        near = cv2.morphologyEx(near, cv2.MORPH_OPEN, np.ones((9, 9), np.uint8))
        nl, lab, st, _ = cv2.connectedComponentsWithStats(near, 8)
        person = ((lab == 1 + int(np.argmax(st[1:, 4]))).astype(np.uint8) * 255) if nl > 1 else near
        if not args.no_dilate:
            person = cv2.dilate(person, np.ones((41, 41), np.uint8), iterations=2)
        if det is not None:
            _, faces = det.detect(f1)
            if faces is not None and len(faces):
                facehit += 1
                for _f in faces:
                    x, y, fw, fh = [float(v) for v in _f[:4]]
                    cx, cy = x + fw / 2, y + fh / 2
                    cv2.circle(person, (int(cx), int(cy)),
                               int(max(fw, fh) * args.face_expand), 255, -1)
        if band:
            person[band[0]:band[1], :] = 255
        if args.mode == "solid":
            fill = np.zeros_like(f1)
        else:
            fill = cv2.GaussianBlur(f1, (0, 0), args.sigma)
        out_frame = np.where(person[..., None] > 0, fill, f1)
        try:
            ff.stdin.write(out_frame.astype(np.uint8).tobytes())
        except (BrokenPipeError, OSError):
            sys.exit("[错误] ffmpeg 写入中断，检查输出路径：%s" % out)
        n += 1
        if n % 100 == 0:
            print("  %d 帧 %.0fs" % (n, time.time() - t0), flush=True)
        if args.max_frames and n >= args.max_frames:
            break
    ff.stdin.close()
    ff.wait()
    cap_s.release()
    cap_d.release()
    print("完成 %d 帧 %.0fs（人脸加固触发 %d 帧）→ %s" % (n, time.time() - t0, facehit, out))
    if n > 40:
        print("建议接着验收：python 人物遮罩.py --check \"%s\" --ref \"%s\"" % (out, args.input))


if __name__ == "__main__":
    main()
