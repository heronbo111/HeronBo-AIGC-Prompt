# -*- coding: utf-8 -*-
"""环境检查 / 部署自检：换机、换 agent、第一次用这套 skill 时先跑它。

设计（2026-09-12 v1.0）：
- 这个 skill 的工具链要靠本机软件：ffmpeg/ffprobe、python 的 numpy/opencv、可选的
  faster-whisper（转写）与 onnxruntime（深度视频），外加两个模型文件。
  缺任何一个，工具都会在跑到一半时报错——所以**部署第一步先自检**。
- **默认只检查、不做任何改动**（exit 0=齐了 / exit 1=缺东西并打印该装什么）。
- 加 `--install` 才安装：会先把「要装什么」列出来**问一次**（`--yes` 跳过询问，供 agent 在
  用户已同意后无人值守执行）。**装软件属于要用户点头的动作，agent 必须先问用户再带 --install。**
- 不检测平台账号、不代登录、不提交生成、不消耗积分。

检查项与分级：
    [必需]     python≥3.9 / ffmpeg / ffprobe / numpy / opencv(cv2)
    [拆解用]   faster-whisper（口播/唱词转写）、Yunet 人脸模型（随仓库走）、Windows OCR（读画面文字）
    [深度视频] onnxruntime + Depth-Anything-V2-Small（L4 复刻动作的前置）

用法：
    python tools\\环境检查.py                     # 只检查（缺项 exit 1）
    python tools\\环境检查.py --json               # 机读输出（agent 用）
    python tools\\环境检查.py --install            # 装缺的 pip 包 / ffmpeg（会先确认一次）
    python tools\\环境检查.py --install --yes      # 无人值守（用户已同意）
    python tools\\环境检查.py --warm-asr           # 预下 faster-whisper 模型（默认 small）
    python tools\\环境检查.py --models             # 额外下 Depth 模型（约 99MB，HF 需代理）
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
YUNET = os.path.join(HERE, "识别工具", "face_detection_yunet_2023mar.onnx")
DEPTH_DIR = os.path.join(os.path.expanduser("~"), ".cache", "depth-models", "depth-anything-v2-small")
DEPTH_FILES = ["onnx/model.onnx", "preprocessor_config.json", "config.json"]
HF_BASE = "https://huggingface.co/onnx-community/depth-anything-v2-small/resolve/main"
PIP_PKGS = {
    "numpy": "numpy",
    "cv2": "opencv-python-headless==4.10.0.84",
    "faster_whisper": "faster-whisper",
    "onnxruntime": "onnxruntime",
}

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


def run(cmd, **kw):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", **kw)
    except Exception as e:
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


def has_bin(name):
    return shutil.which(name)


def mod_state(mod):
    """返回 (是否可导入, 版本或错误摘要)。"""
    r = run([sys.executable, "-c",
             "import %s,sys;print(getattr(%s,'__version__','?'))" % (mod, mod)])
    if r.returncode == 0:
        return True, (r.stdout or "").strip().splitlines()[-1] if r.stdout.strip() else "?"
    return False, (r.stderr or "").strip().splitlines()[-1][:120] if r.stderr.strip() else "导入失败"


def check_ocr():
    """Windows OCR（读画面上的文字）：靠系统 WinRT，无第三方依赖。"""
    if os.name != "nt":
        return False, "非 Windows，跳过（改用其它 OCR）"
    ps = shutil.which("powershell.exe") or shutil.which("powershell")
    if not ps:
        return False, "找不到 powershell"
    r = run([ps, "-NoProfile", "-Command",
             "[Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]|Out-Null;'ok'"])
    return ("ok" in (r.stdout or "")), ("系统 OCR 可用" if "ok" in (r.stdout or "") else "系统 OCR 不可用（缺语言包？）")


def probe():
    items = []

    def add(name, level, ok, detail, action=""):
        items.append({"项": name, "级别": level, "状态": "OK" if ok else "缺",
                      "详情": detail, "动作": action})

    ok = sys.version_info >= (3, 9)
    add("python≥3.9", "必需", ok, "%d.%d.%d" % sys.version_info[:3],
        "" if ok else "装 Python 3.9+ 并加入 PATH")

    for b in ("ffmpeg", "ffprobe"):
        p = has_bin(b)
        add(b, "必需", bool(p), (p or "不在 PATH"),
            "" if p else "winget install --id Gyan.FFmpeg -e（或官网下载后加入 PATH）")

    for mod, level in (("numpy", "必需"), ("cv2", "必需"),
                       ("faster_whisper", "拆解用"), ("onnxruntime", "深度视频")):
        ok, detail = mod_state(mod)
        add(mod, level, ok, detail, "" if ok else "pip install %s" % PIP_PKGS[mod])

    ok = os.path.isfile(YUNET)
    add("Yunet 人脸模型", "拆解用", ok, "随仓库走",
        "" if ok else "从仓库补回 tools\\识别工具\\face_detection_yunet_2023mar.onnx")

    ok, detail = check_ocr()
    add("系统 OCR（WinRT）", "拆解用", ok, detail, "" if ok else "Windows 设置里装中文语言/OCR 组件")

    model = os.path.join(DEPTH_DIR, "model.onnx")
    ok = os.path.isfile(model) and os.path.getsize(model) > 90 * 1024 * 1024
    add("Depth-Anything-V2-Small", "深度视频", ok,
        ("%.0f MB" % (os.path.getsize(model) / 1048576)) if os.path.isfile(model) else "未下载",
        "" if ok else "python tools\\环境检查.py --models（HF 需代理，见 depth-video-setup.md）")
    return items


def do_pip(pkgs, assume_yes):
    print("\n[安装] pip：%s" % " ".join(pkgs))
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade"] + pkgs
    if assume_yes:
        cmd.insert(3, "-q")
    r = subprocess.run(cmd)
    return r.returncode == 0


def do_winget_ffmpeg(assume_yes):
    if not has_bin("winget"):
        print("[安装] 没有 winget：请手动装 ffmpeg（https://www.gyan.dev/ffmpeg/builds/ ）并把 bin 加进 PATH")
        return False
    if not assume_yes:
        if input("用 winget 安装 ffmpeg（Gyan.FFmpeg）？[y/N] ").strip().lower() not in ("y", "yes"):
            print("[安装] 跳过 ffmpeg")
            return False
    print("[安装] winget install Gyan.FFmpeg（装完请重开终端让 PATH 生效）")
    r = run(["winget", "install", "--id", "Gyan.FFmpeg", "-e",
             "--accept-source-agreements", "--accept-package-agreements"])
    print((r.stdout or "")[-500:] or (r.stderr or "")[-500:])
    return r.returncode == 0


def do_models():
    print("\n[下载] Depth 模型 → %s" % DEPTH_DIR)
    os.makedirs(DEPTH_DIR, exist_ok=True)
    for rel in DEPTH_FILES:
        dst = os.path.join(DEPTH_DIR, os.path.basename(rel))
        if os.path.isfile(dst) and os.path.getsize(dst) > 1000:
            print("  已有 %s" % os.path.basename(dst))
            continue
        url = "%s/%s" % (HF_BASE, rel)
        print("  下载 %s" % url)
        # 用 python 的 urllib（尊重 HTTPS_PROXY，国内直连 HF 通常需要代理）
        code = ("import urllib.request,sys;"
                "urllib.request.urlretrieve(%r, %r);print('ok')" % (url, dst))
        r = run([sys.executable, "-c", code])
        if "ok" not in (r.stdout or ""):
            print("  ✗ 失败（HF 需代理：$env:HTTPS_PROXY='http://127.0.0.1:7897' 后重试）")
            return False
    print("  完成")
    return True


def do_warm_asr(model):
    print("\n[预热] faster-whisper 模型：%s（首次会下载，约几百 MB）" % model)
    code = ("from faster_whisper import WhisperModel;"
            "WhisperModel(%r, device='cpu', compute_type='int8');print('ok')" % model)
    r = run([sys.executable, "-c", code])
    print("  完成" if "ok" in (r.stdout or "") else "  ✗ 失败：%s" % (r.stderr or "")[-200:])
    return "ok" in (r.stdout or "")


def main():
    ap = argparse.ArgumentParser(description="Seedance skill 环境检查 / 部署自检")
    ap.add_argument("--install", action="store_true", help="安装缺的 pip 包（ffmpeg 走 winget，会先问一次）")
    ap.add_argument("--yes", action="store_true", help="不询问（供 agent 在用户已同意后使用）")
    ap.add_argument("--models", action="store_true", help="下载 Depth 模型（约 99MB，HF 需代理）")
    ap.add_argument("--warm-asr", action="store_true", help="预下 faster-whisper 模型")
    ap.add_argument("--asr-model", default="small", help="faster-whisper 模型档位，默认 small")
    ap.add_argument("--json", action="store_true", help="机读输出")
    args = ap.parse_args()

    items = probe()
    if args.json:
        print(json.dumps({"items": items,
                          "ready": all(i["状态"] == "OK" for i in items if i["级别"] == "必需")},
                         ensure_ascii=False, indent=2))
    else:
        print("=== Seedance skill 环境检查 ===")
        for i in items:
            print("[%s] %-4s %-26s %s%s" % (
                "OK" if i["状态"] == "OK" else "缺", i["级别"], i["项"], i["详情"],
                ("  → %s" % i["动作"]) if i["状态"] == "缺" and i["动作"] else ""))
        miss = [i for i in items if i["状态"] == "缺"]
        print("\n缺 %d 项；必需项 %s" % (
            len(miss), "齐了" if not any(i["级别"] == "必需" for i in miss) else "有缺失（先补必需项）"))

    if args.install:
        todo = [PIP_PKGS[i["项"]] for i in items
                if i["状态"] == "缺" and i["项"] in PIP_PKGS]
        need_ff = any(i["状态"] == "缺" and i["项"] == "ffmpeg" for i in items)
        if not todo and not need_ff:
            print("\n没有需要安装的项。")
        else:
            print("\n将安装：%s%s" % ("、".join(todo) or "（无 pip 包）",
                                    "＋ffmpeg" if need_ff else ""))
            if not args.yes and input("继续？[y/N] ").strip().lower() not in ("y", "yes"):
                print("已取消。")
                return 1
            if todo:
                do_pip(todo, args.yes)
            if need_ff:
                do_winget_ffmpeg(args.yes)
            print("\n复查：")
            items = probe()
            for i in items:
                if i["状态"] == "缺":
                    print("  仍缺 %s → %s" % (i["项"], i["动作"]))

    if args.models:
        do_models()
    if args.warm_asr:
        do_warm_asr(args.asr_model)

    core_ok = all(i["状态"] == "OK" for i in probe() if i["级别"] == "必需")
    print("\n结论：%s" % ("必需项齐备，可以开工。" if core_ok else "必需项有缺失，按上面的 → 处理后再来。"))
    return 0 if core_ok else 1


if __name__ == "__main__":
    sys.exit(main())
