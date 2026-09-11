# -*- coding: utf-8 -*-
"""横版素材 → 9:16 竖版输入（口播/替换类默认竖版出片的预处理）。

设计（2026-09-11 v1.1，对应 rules.md 第26条「画幅默认竖版 9:16」）：
- 本 skill 默认按竖版出片，参考视频是横版也一样；横版源片**不要**直接丢给模型要求竖版输出
  （2026-09-10 中国人能飞首轮实测：模型会竖屏重构图，人物放大 2–3 倍、构图彻底改变）。
- 正确做法：本地先转成 9:16 再上传当 @视频1，输入输出画幅一致 → 模型没有重构图压力。
- 两种模式：
  * crop（默认，满屏竖版）：**只裁左右、整幅高度全保留**（16:9→9:16 高度无损），
    默认按人脸位置自动定位裁切窗口（--focus auto），主体不会跑出画面；适合交付竖屏成片。
  * pad（保构图竖版画布）：画面完整不裁切，上下用同一画面的放大模糊填充；适合要求
    "与源片逐帧一致/背景不能变"的替换类，或主体不在画面中央的镜头。

用法：
    python 竖版画布.py --check "<视频>"                        # 只看画幅/时长/有无音轨
    python 竖版画布.py -i "<横版视频>"                          # crop 满屏竖版 + 人脸自动定位
    python 竖版画布.py -i "<横版视频>" --mode pad                # 保构图竖版画布（不裁切）
    python 竖版画布.py -i "<横版视频>" --focus 0.35              # 手工指定裁切窗口位置
    python 竖版画布.py -i "<横版视频>" -o "<输出.mp4>" --mute     # 指定输出名并去掉音轨

参数要点：
    --size   默认 1080x1920（9:16）；720p 竖版用 720x1280
    --focus  crop 专用：auto（默认，抽帧人脸定位）／0=贴左 ～ 0.5=居中 ～ 1=贴右
    --blur   pad 模式填充背景模糊强度（sigma，默认 24）
    --mute   丢掉音轨（参考视频按 rules 第14条静音输入时用）

依赖：ffmpeg / ffprobe 在 PATH；--focus auto 另需 opencv-python-headless + 同目录
`识别工具/face_detection_yunet_2023mar.onnx`（缺失时自动退化为居中，不报错）。
脚本只做本地画幅处理，不提交任何生成平台、不消耗积分。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

DEFAULT_SIZE = "1080x1920"
HERE = os.path.dirname(os.path.abspath(__file__))
YUNET = os.path.join(HERE, "识别工具", "face_detection_yunet_2023mar.onnx")

# 包里其它工具走 chcp 65001；这里显式把输出固定成 UTF-8，agent 抓输出不乱码
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
    dur = data.get("format", {}).get("duration")
    return {
        "width": int(v.get("width") or 0),
        "height": int(v.get("height") or 0),
        "duration": float(dur) if dur else None,
        "fps": v.get("r_frame_rate"),
        "audio": bool(a),
        "codec": v.get("codec_name"),
    }


def fmt(info):
    w, h = info["width"], info["height"]
    ratio = "?"
    if w and h:
        from math import gcd
        g = gcd(w, h)
        ratio = "%d:%d" % (w // g, h // g)
    d = info["duration"]
    return ("%dx%d（%s）  %.2fs  %s  音轨=%s" %
            (w, h, ratio, d if d else -1, info["fps"], "有" if info["audio"] else "无"))


def load_bgr(path):
    """cv2 在 Windows 下读不了中文路径 → np.fromfile + imdecode（rules 第11条）。"""
    import numpy as np
    import cv2
    return cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)


def face_center_x(src, duration, samples=5):
    """抽帧做人脸定位，返回人脸中心的横向占比（0–1）；失败返回 None。"""
    try:
        import cv2
    except Exception:
        print("      [提示] 没装 opencv，跳过人脸定位，按居中裁切")
        return None
    if not os.path.isfile(YUNET):
        print("      [提示] 缺 识别工具/face_detection_yunet_2023mar.onnx，按居中裁切")
        return None

    # cv2 的模型加载同样吃不了中文路径 → 复制到临时 ASCII 路径
    tmp_model = os.path.join(tempfile.gettempdir(), "yunet_tmp_model.onnx")
    try:
        shutil.copyfile(YUNET, tmp_model)
    except Exception as e:
        print("      [提示] 模型复制失败（%s），按居中裁切" % e)
        return None

    det = cv2.FaceDetectorYN.create(tmp_model, "", (320, 320), 0.6, 0.3, 5000)
    ffmpeg = need("ffmpeg")
    xs = []
    with tempfile.TemporaryDirectory() as td:
        for i in range(samples):
            t = (duration or 1.0) * (i + 1) / (samples + 1)
            png = os.path.join(td, "f%d.png" % i)
            subprocess.run([ffmpeg, "-y", "-v", "error", "-ss", "%.2f" % t,
                            "-i", src, "-frames:v", "1", png],
                           capture_output=True)
            if not os.path.isfile(png):
                continue
            img = load_bgr(png)
            if img is None:
                continue
            h, w = img.shape[:2]
            det.setInputSize((w, h))
            _, faces = det.detect(img)
            if faces is not None and len(faces):
                x, fw = float(faces[0][0]), float(faces[0][2])
                xs.append((x + fw / 2) / w)
    if not xs:
        print("      [提示] 抽帧未检出人脸，按居中裁切")
        return None
    xs.sort()
    return xs[len(xs) // 2]   # 取中位数，抗单帧误检


def auto_focus(src, src_w, src_h, out_w, out_h, duration):
    """由人脸位置反算裁切窗口位置：0=贴左 0.5=居中 1=贴右。"""
    cx = face_center_x(src, duration)
    if cx is None:
        return 0.5, None
    scale = max(out_w / src_w, out_h / src_h)
    scaled_w = src_w * scale
    span = scaled_w - out_w
    if span <= 1:
        return 0.5, cx
    focus = (cx * scaled_w - out_w / 2) / span
    return min(1.0, max(0.0, focus)), cx


def build_filter(mode, w, h, blur, focus):
    if mode == "pad":
        return (
            "[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
            "crop={w}:{h},gblur=sigma={blur},eq=brightness=-0.06[bg];"
            "[0:v]scale={w}:-2[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2,format=yuv420p[v]"
        ).format(w=w, h=h, blur=blur)
    return (
        "[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
        "crop={w}:{h}:(iw-{w})*{focus}:(ih-{h})*{focus},format=yuv420p[v]"
    ).format(w=w, h=h, focus=focus)


def main():
    ap = argparse.ArgumentParser(description="横版素材 → 9:16 竖版输入（rules 第26条）")
    ap.add_argument("-i", "--input", help="输入视频（横版/任意画幅）")
    ap.add_argument("-o", "--output", help="输出视频；不写则自动命名 <原名>_竖版裁切.mp4 / _竖版画布.mp4")
    ap.add_argument("--mode", choices=["crop", "pad"], default="crop",
                    help="crop=满屏竖版，只裁左右（默认）；pad=不裁切的竖版画布（上下模糊填充）")
    ap.add_argument("--size", default=DEFAULT_SIZE, help="画布尺寸，默认 1080x1920")
    ap.add_argument("--focus", default="auto",
                    help="crop 裁切窗口位置：auto（默认，人脸定位）或 0～1 数值（0=左 0.5=中 1=右）")
    ap.add_argument("--blur", type=float, default=24, help="pad 模式填充背景模糊 sigma，默认 24")
    ap.add_argument("--mute", action="store_true", help="丢掉音轨（静音输入场景）")
    ap.add_argument("--crf", type=int, default=16, help="x264 质量，默认 16（越小越清晰）")
    ap.add_argument("--check", help="只看信息不转换")
    args = ap.parse_args()

    if args.check:
        if not os.path.isfile(args.check):
            sys.exit("[错误] 文件不存在：%s" % args.check)
        print("%s\n  %s" % (os.path.basename(args.check), fmt(probe(args.check))))
        return

    if not args.input:
        ap.error("要么给 --input，要么给 --check")
    src = os.path.abspath(args.input)
    if not os.path.isfile(src):
        sys.exit("[错误] 文件不存在：%s" % src)

    try:
        w, h = (int(x) for x in args.size.lower().split("x"))
    except ValueError:
        sys.exit("[错误] --size 要写成 1080x1920 这种格式")

    before = probe(src)
    suffix = "_竖版裁切" if args.mode == "crop" else "_竖版画布"
    if args.output:
        dst = os.path.abspath(args.output)
    else:
        base, ext = os.path.splitext(src)
        dst = base + suffix + (ext or ".mp4")

    print("[1/2] 准备：%s" % os.path.basename(src))
    print("      原画幅 %s → %s（%s）" % (fmt(before), args.size, args.mode))

    focus = 0.5
    if args.mode == "crop":
        if args.focus == "auto":
            focus, cx = auto_focus(src, before["width"], before["height"], w, h, before["duration"])
            if cx is None:
                print("      裁切窗口：居中（focus=0.50）")
            else:
                print("      人脸定位：横向 %.1f%% → 裁切窗口 focus=%.2f（只裁左右，整幅高度全保留）"
                      % (cx * 100, focus))
        else:
            try:
                focus = min(1.0, max(0.0, float(args.focus)))
            except ValueError:
                sys.exit("[错误] --focus 只能是 auto 或 0～1 的数值")
            print("      裁切窗口：focus=%.2f（手工指定）" % focus)

    ffmpeg = need("ffmpeg")
    cmd = [ffmpeg, "-y", "-i", src,
           "-filter_complex", build_filter(args.mode, w, h, args.blur, focus),
           "-map", "[v]"]
    if args.mute:
        cmd += ["-an"]
    else:
        cmd += ["-map", "0:a?", "-c:a", "copy"]
    cmd += ["-c:v", "libx264", "-crf", str(args.crf), "-preset", "medium",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", dst]

    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0 or not os.path.isfile(dst):
        sys.exit("[错误] ffmpeg 失败：\n%s" % (r.stderr or "")[-2000:])

    after = probe(dst)
    ok = (after["width"], after["height"]) == (w, h)
    print("[2/2] 输出：%s" % dst)
    print("      %s" % fmt(after))
    print("      画幅核验：%s" % ("✓ 已是 %s（9:16 竖版）" % args.size if ok else "✗ 不是 %s，请检查" % args.size))
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
