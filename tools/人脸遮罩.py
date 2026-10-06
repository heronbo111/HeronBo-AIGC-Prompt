# -*- coding: utf-8 -*-
r"""人脸遮罩（图片版）：把图片里检测到的人脸用色块盖住（或模糊），另存一份。

为什么要有它（2026-10-06 用户裁定）：素材里的真人脸会触发平台审核、也会被模型
当成身份锚定（rules 20b「参考视频锚定会压制替换指令」）——上传前把脸盖掉再走
生成，是同类工具里被验证过的做法。对应关系与既有规则：

- 盖色块 + 提示词补全  → rules 29（水印/字幕的黑块遮挡法）同一族；
- 只遮"脸"、背景保持清晰 → `人物遮罩.py` 的**图片版**（那边管视频，这边管单图/多视图）；
- 整图色块遮盖属于"素材加工" → 默认不做（rules 49），agent 动手前按 rules 62 单独报一步，
  ②栏「素材加工」面板勾了「遮脸」才允许做（rules 55 的清单制）。

用法：

    python tools\人脸遮罩.py -i "<图片>"                          # 默认：黑色块，遮脸范围 80%
    python tools\人脸遮罩.py -i "<图片>" --mode blur             # 高斯模糊而不是色块
    python tools\人脸遮罩.py -i "<图片>" --color green           # 黑|白|绿|#RRGGBB
    python tools\人脸遮罩.py -i "<图片>" --ratio 0.9             # 色块相对脸框的比例（默认 0.8）
    python tools\人脸遮罩.py -i "<多视图.png>" --json            # 机读输出（agent / 工作台用）

输入可以是单图，也可以是目录（目录时逐张处理 jpg/jpeg/png/webp/bmp）。
输出默认写到**原图所在目录**、文件名加 `_遮脸` 后缀；`--out` 可另指定。
多视图（一张图里多张人脸）会逐脸全部盖掉。

检测：首选 YuNet（`识别工具/face_detection_yunet_2023mar.onnx`，与 `人物遮罩.py`
同一个模型）；模型或 opencv 版本不支持时退回 Haar 级联（opencv 自带，不用装东西）。
两个已知的坑都在这里处理了：cv2 不能读写中文路径（np.fromfile + cv2.imdecode 绕）；
ONNX 加载不支持非 ASCII 目录（先把模型拷到 %TEMP% 再加载，与 `人物遮罩.py` 同法）。

边界：只做本地图片处理——不提交任何平台、不消耗积分、不动视频（视频走 `人物遮罩.py`）。
"""
import argparse
import glob
import json
import os
import shutil
import sys
import tempfile

import numpy as np
import cv2

HERE = os.path.dirname(os.path.abspath(__file__))
YUNET = os.path.join(HERE, "识别工具", "face_detection_yunet_2023mar.onnx")
EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp")
SUFFIX = "_遮脸"

COLORS = {"black": (15, 15, 15), "white": (245, 245, 245), "green": (0, 255, 0)}  # BGR


def _color_bgr(name):
    """黑|白|绿|#RRGGBB → BGR 元组；写错颜色名宁可用黑，也别把图涂花。"""
    n = (name or "black").strip().lower()
    if n in COLORS:
        return COLORS[n]
    if n.startswith("#") and len(n) == 7:
        try:
            r, g, b = int(n[1:3], 16), int(n[3:5], 16), int(n[5:7], 16)
            return (b, g, r)
        except ValueError:
            pass
    return COLORS["black"]


def _imread(path):
    """cv2 不能读中文路径 → np.fromfile + imdecode（playbooks 3.8 的坑）。"""
    try:
        buf = np.fromfile(path, dtype=np.uint8)
        return cv2.imdecode(buf, cv2.IMREAD_COLOR)
    except OSError:
        return None


def _imwrite(path, img):
    """同上，写也要绕（imencode + tofile），保证 `_遮脸.png` 落在中文目录里不花。"""
    ext = os.path.splitext(path)[1] or ".png"
    ok, buf = cv2.imencode(ext, img)
    if not ok:
        return False
    buf.tofile(path)
    return True


def _detector(w, h):
    """YuNet 优先，退回 Haar。返回 (detector, 名字)；都不可用返回 (None, 原因)。"""
    model = YUNET
    # ONNX 加载不支持非 ASCII 路径：先把模型拷进 %TEMP% 再加载（与 人物遮罩.py 同法）
    if os.path.isfile(model):
        if any(ord(c) > 127 for c in model):
            try:
                model = os.path.join(tempfile.gettempdir(), "yunet_tool.onnx")
                if not os.path.isfile(model):
                    shutil.copy(YUNET, model)
            except OSError:
                model = YUNET
        try:
            det = cv2.FaceDetectorYN.create(model, "", (w, h), 0.5, 0.3, 5000)
            return ("yunet", det)
        except cv2.error:
            pass
    cascade = os.path.join(os.path.join(cv2.data.haarcascades),
                           "haarcascade_frontalface_default.xml")
    if os.path.isfile(cascade):
        try:
            return ("haar", cv2.CascadeClassifier(cascade))
        except cv2.error:
            pass
    return (None, "没有可用人脸检测器（缺 YuNet 模型且 Haar 不可用）")


def detect_faces(img):
    """返回脸框列表 [(x, y, w, h)]（像素，越界会裁回图内）。"""
    h, w = img.shape[:2]
    kind, det = _detector(w, h)
    if det is None:
        return [], kind
    boxes = []
    if kind == "yunet":
        det.setInputSize((w, h))
        _n, faces = det.detect(img)
        for f in (faces if faces is not None else []):
            x, y, fw, fh = [int(round(v)) for v in f[:4]]
            boxes.append((x, y, fw, fh))
    else:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        for (x, y, fw, fh) in det.detectMultiScale(gray, 1.1, 5, minSize=(30, 30)):
            boxes.append((int(x), int(y), int(fw), int(fh)))
    out = []
    for x, y, fw, fh in boxes:
        x, y = max(0, x), max(0, y)
        fw, fh = min(fw, w - x), min(fh, h - y)
        if fw > 4 and fh > 4:
            out.append((x, y, fw, fh))
    return out, kind


def mask_image(img, mode="solid", color="black", ratio=0.8):
    """按检测到的脸盖遮罩，返回 (新图, 脸框列表, 检测器名)。"""
    boxes, kind = detect_faces(img)
    out = img.copy()
    bgr = _color_bgr(color)
    h, w = out.shape[:2]
    for (x, y, fw, fh) in boxes:
        # 遮罩中心对准脸框中心，边长 = 脸框 × ratio（默认 0.8：遮住五官、少压头发）
        mw, mh = int(fw * ratio), int(fh * ratio)
        mx, my = x + (fw - mw) // 2, y + (fh - mh) // 2
        mx, my = max(0, mx), max(0, my)
        mw, mh = min(mw, w - mx), min(mh, h - my)
        if mw <= 0 or mh <= 0:
            continue
        if mode == "blur":
            roi = out[my:my + mh, mx:mx + mw]
            if roi.size:
                k = max(3, (min(mw, mh) // 4) * 2 + 1)   # 核随脸大小走，必须是奇数
                out[my:my + mh, mx:mx + mw] = cv2.GaussianBlur(roi, (k, k), 0)
        else:
            cv2.rectangle(out, (mx, my), (mx + mw - 1, my + mh - 1), bgr, -1)
    return out, boxes, kind


def out_path(src, out=None):
    if out:
        return out
    root, ext = os.path.splitext(src)
    return root + SUFFIX + (ext if ext else ".png")


def process_one(src, mode, color, ratio, out=None):
    """处理单张图。返回 dict（--json 的条目 / 普通模式打印用）。"""
    r = {"input": src, "output": "", "faces": 0, "ok": False, "error": ""}
    img = _imread(src)
    if img is None:
        r["error"] = "读不了图（路径中文/文件损坏都按这条报）"
        return r
    masked, boxes, kind = mask_image(img, mode=mode, color=color, ratio=ratio)
    dst = out_path(src, out)
    if not _imwrite(dst, masked):
        r["error"] = "写不出结果图（目录只读？）"
        return r
    r.update({"output": dst, "faces": len(boxes), "ok": True, "detector": kind})
    return r


def main(argv=None):
    ap = argparse.ArgumentParser(description="图片人脸遮罩：检测到的人脸盖色块/模糊，另存 _遮脸 一份")
    ap.add_argument("-i", "--input", required=True, help="图片路径或目录")
    ap.add_argument("--mode", choices=["solid", "blur"], default="solid",
                    help="solid＝色块（默认）；blur＝高斯模糊")
    ap.add_argument("--color", default="black", help="solid 模式的颜色：black|white|green|#RRGGBB")
    ap.add_argument("--ratio", type=float, default=0.8, help="遮罩边长相对脸框的比例（默认 0.8）")
    ap.add_argument("--out", default="", help="输出路径（只对单张输入有效；默认原图目录加 _遮脸 后缀）")
    ap.add_argument("--json", action="store_true", help="机读输出（agent / 工作台用）")
    a = ap.parse_args(argv)

    src = os.path.abspath(a.input)
    if os.path.isdir(src):
        files = [f for f in sorted(glob.glob(os.path.join(src, "*")))
                 if f.lower().endswith(EXTS)]
        if not files:
            print(json.dumps({"ok": False, "error": "目录里没有可处理的图片"}, ensure_ascii=False))
            return 1
        results = [process_one(f, a.mode, a.color, a.ratio) for f in files]
    elif os.path.isfile(src):
        results = [process_one(src, a.mode, a.color, a.ratio, out=(a.out or None))]
    else:
        results = [{"input": src, "ok": False, "error": "文件不存在", "output": "", "faces": 0}]

    ok_n = sum(1 for r in results if r["ok"])
    face_n = sum(r["faces"] for r in results)
    if a.json:
        print(json.dumps({"ok": ok_n == len(results) and ok_n > 0,
                          "processed": len(results), "okCount": ok_n,
                          "faces": face_n, "results": results}, ensure_ascii=False, indent=2))
    else:
        for r in results:
            if r["ok"]:
                print("✓ %s → %s（%d 张脸）" % (os.path.basename(r["input"]),
                                               os.path.basename(r["output"]), r["faces"]))
            else:
                print("✗ %s：%s" % (os.path.basename(r["input"]), r["error"]))
        print("共 %d 张，成功 %d，盖掉 %d 张脸" % (len(results), ok_n, face_n))
    return 0 if ok_n else 2


if __name__ == "__main__":
    sys.exit(main())
