# -*- coding: utf-8 -*-
"""环境检查 / 部署自检：换机、换 agent、第一次用这套 skill 时先跑它。

设计（2026-09-12 v1.0）：
- 这个 skill 的工具链要靠本机软件：ffmpeg/ffprobe、python 的 numpy/opencv、可选的
  sherpa-onnx（SenseVoice 转写）与 onnxruntime（深度视频），外加两个模型文件。
  缺任何一个，工具都会在跑到一半时报错——所以**部署第一步先自检**。
- **默认只检查、不做任何改动**（exit 0=齐了 / exit 1=缺东西并打印该装什么）。
- 加 `--install` 才安装：会先把「要装什么」列出来**问一次**（`--yes` 跳过询问，供 agent 在
  用户已同意后无人值守执行）。**装软件属于要用户点头的动作，agent 必须先问用户再带 --install。**
- 不检测平台账号、不代登录、不提交生成、不消耗积分。

检查项与分级：
    [必需]     python≥3.9 / ffmpeg / ffprobe
    [源码运行用] pywebview + pythonnet（用 dist 里的 exe 时不需要，exe 自带）
    [可选]     numpy / opencv（成片对比、人物遮罩、竖版画布、拆解抽帧）——**用到才装**
               sherpa-onnx（SenseVoice 转写）、Yunet 人脸模型（随仓库走）、Windows OCR（读画面文字）
    [可选]     onnxruntime + Depth-Anything-V2-Small（L4 复刻动作的前置）
    **大包默认不装**：`--install` 只补必需项，`--install --extras` 才连可选的一起装
    （2026-09-16：新机一上来就下几百 MB、装很久，用户反馈过）。

用法：
    python tools\\环境检查.py                     # 只检查（缺项 exit 1）
    python tools\\环境检查.py --json               # 机读输出（agent 用）
    python tools\\环境检查.py --install            # 装缺的必需项（会先确认一次）
    python tools\\环境检查.py --install --extras   # 连可选大包一起装
    python tools\\环境检查.py --install --yes      # 无人值守（用户已同意）
    python tools\\环境检查.py --warm-asr           # 预下转写模型 SenseVoice（约 239MB）
    python tools\\环境检查.py --models             # 额外下 Depth 模型（约 99MB，HF 需代理）
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _skill_root():
    """往上找带 SKILL.md 的那一层（打包后 HERE 在 _internal 里，模型在 skill 根下）。"""
    d = HERE
    for _ in range(6):
        if os.path.isfile(os.path.join(d, "SKILL.md")):
            return d
        nd = os.path.dirname(d)
        if nd == d:
            break
        d = nd
    return os.path.dirname(HERE)


SKILL_ROOT = _skill_root()
YUNET = os.path.join(HERE, "识别工具", "face_detection_yunet_2023mar.onnx")
YUNET_CANDS = [YUNET,
               os.path.join(SKILL_ROOT, "tools", "识别工具",
                            "face_detection_yunet_2023mar.onnx")]

DEPTH_DIR = os.path.join(os.path.expanduser("~"), ".cache", "depth-models", "depth-anything-v2-small")
# 仓库自带的那份（随 skill 分发，装机即用）
DEPTH_BUNDLED = os.path.join(HERE, "_vendor", "models", "depth-anything-v2-small")
DEPTH_CANDS = [DEPTH_DIR, DEPTH_BUNDLED,
               os.path.join(SKILL_ROOT, "tools", "_vendor", "models",
                            "depth-anything-v2-small")]
DEPTH_FILES = ["onnx/model.onnx", "preprocessor_config.json", "config.json"]
# 走**国内模型镜像**（2026-10-08）：裸连 huggingface.co 实测连不上。
# ⚠️ hf-mirror 只适合小文件：大件会被 302 到海外 Xet/CAS（`cas-bridge.xethub.hf.co`），
# 实测 0.0–0.17MB/s 甚至 401 → 所以 ModelScope 排第一（国内 cdn-lfs-cn-1，实测 11–12MB/s）。
# 也可以用 HERONBO_HF_MIRROR 换自建镜像。
HF_MIRROR = "https://hf-mirror.com"
HF_BASE = HF_MIRROR + "/onnx-community/depth-anything-v2-small/resolve/main"
MS_WEB = "https://modelscope.cn/models/onnx-community/depth-anything-v2-small/resolve"
HF_BASE_MS = ("https://modelscope.cn/api/v1/models/onnx-community/depth-anything-v2-small"
              "/repo?Revision=master&FilePath=")
PIP_PKGS = {
    "numpy": "numpy",
    "cv2": "opencv-python-headless==4.10.0.84",
    "sherpa_onnx": "sherpa-onnx",
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


def py_exe():
    """真·Python 解释器。打包版里 `sys.executable` 是工作台自己——
    拿它跑 `-c` 会再起一个工作台窗口（2026-10-26 实测：自检时把主程序自己顶掉了）。"""
    if not getattr(sys, "frozen", False):
        return sys.executable
    base = os.path.dirname(sys.executable)
    for c in (os.path.join(base, "runtime", "python", "python.exe"),
              os.path.join(base, "..", "runtime", "python", "python.exe"),
              os.path.join(base, "python.exe")):
        if os.path.isfile(c):
            return os.path.abspath(c)
    return ""


def mod_state(mod):
    """返回 (是否可导入, 版本或错误摘要)。"""
    if getattr(sys, "frozen", False):
        # 打包版：这些库是给**随包 Python**（runtime\python）跑脚本用的，不是给 exe 自己用的。
        # 所以要用随包 python 去 import 才算数（2026-10-26 实踩：拿 exe 的 find_spec 判断，
        # 明明装好的 numpy/cv2 全被报成"未内置"）。
        exe = py_exe()
        if exe:
            r = run([exe, "-c",
                     "import %s,sys;print(getattr(%s,'__version__','?'))" % (mod, mod)])
            if r.returncode == 0:
                v = (r.stdout or "").strip().splitlines()
                return True, (v[-1] if v else "已安装")
            return False, "随包 Python 里没有"
        try:
            import importlib.util as _iu
            sp = _iu.find_spec(mod)
        except Exception as e:                                   # noqa: BLE001
            return False, "探测失败：%s" % str(e)[:80]
        return (sp is not None), ("内置" if sp is not None else "未内置")
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

    def add(name, level, ok, detail, action="", target=""):
        items.append({"项": name, "级别": level, "状态": "OK" if ok else "缺",
                      "详情": detail, "动作": action, "可安装": target})

    ok = sys.version_info >= (3, 9)
    add("python≥3.9", "必需", ok, "%d.%d.%d" % sys.version_info[:3],
        "" if ok else "装 Python 3.9+ 并加入 PATH")

    for b in ("ffmpeg", "ffprobe"):
        p = has_bin(b)
        add(b, "必需", bool(p), (p or "不在 PATH"),
            "" if p else "winget install --id Gyan.FFmpeg -e（或官网下载后加入 PATH）")

    # 分级很重要（2026-09-16 用户反馈"新机安装花了很长时间"）：numpy/opencv 只在
    # 「成片对比 / 人物遮罩 / 竖版画布 / 拆解抽帧」这些工具里用得上，新机不该为了跑
    # 提示词流程先下几百 MB。核心链路（写提示词、评分、工作台）不依赖它们。
    for mod, level in (("numpy", "可选（对比/画布工具用）"), ("cv2", "可选（遮罩/抽帧用）"),
                       ("sherpa_onnx", "可选（拆解转写）"), ("onnxruntime", "可选（深度视频）")):
        ok, detail = mod_state(mod)
        add(mod, level, ok, detail, "" if ok else "点右边的「安装」自动补", "pip:" + mod)

    yunet = next((p for p in YUNET_CANDS if os.path.isfile(p)), "")
    add("Yunet 人脸模型", "拆解用", bool(yunet), ("随包" if yunet else "缺文件"),
        "" if yunet else "安装包自带；缺了说明安装不完整，重装一次即可")

    ok, detail = check_ocr()
    add("系统 OCR（WinRT）", "拆解用", ok, detail, "" if ok else "Windows 设置里装中文语言/OCR 组件")

    # 工作台界面：exe 自带一份 pywebview（独立窗口），从源码跑才需要本机装
    ok, detail = mod_state("webview")
    add("pywebview（工作台独立窗口，界面默认入口）", "源码运行用", ok, detail,
        "" if ok else "pip install pywebview（或直接用 dist 目录里的 score-tool.exe，里面已带）")

    model = next((os.path.join(d, "model.onnx") for d in DEPTH_CANDS
                  if os.path.isfile(os.path.join(d, "model.onnx"))), "")
    ok = bool(model) and os.path.getsize(model) > 90 * 1024 * 1024
    add("Depth-Anything-V2-Small", "深度视频", ok,
        ("%.0f MB" % (os.path.getsize(model) / 1048576)) if model else "未下载",
        "" if ok else "点右边的「下载」自动拉（国内镜像优先）", "model:depth")
    return items


def do_pip(pkgs, assume_yes):
    print("\n[安装] pip：%s" % " ".join(pkgs))
    cmd = [py_exe() or sys.executable, "-m", "pip", "install", "--upgrade"] + pkgs
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
    mirror = os.environ.get("HERONBO_HF_MIRROR", "").strip().rstrip("/") or HF_MIRROR
    for rel in DEPTH_FILES:
        dst = os.path.join(DEPTH_DIR, os.path.basename(rel))
        if os.path.isfile(dst) and os.path.getsize(dst) > 1000:
            print("  已有 %s" % os.path.basename(dst))
            continue
        # ModelScope 排第一（大件只有它走国内 CDN），之后才是 hf-mirror 与官方 HF 兜底。
        # 注意落盘一律用 basename：远端是 `onnx/model.onnx`，本地要平铺成 `model.onnx`
        # （`深度视频.pick_model` 与安装包载荷都按平铺找）。
        urls = ["%s/master/%s" % (MS_WEB, rel),
                HF_BASE_MS + rel,
                "%s/%s" % (HF_BASE, rel)]
        if os.environ.get("HERONBO_HF_MIRROR", "").strip():
            urls.insert(0, "%s/%s/resolve/main/%s"
                        % (mirror, "onnx-community/depth-anything-v2-small", rel))
        ok = False
        for url in urls:
            print("  下载 %s" % url)
            code = ("import urllib.request;"
                    "urllib.request.urlretrieve(%r, %r);print('ok')" % (url, dst))
            r = run([py_exe() or sys.executable, "-c", code])
            if "ok" in (r.stdout or ""):
                ok = True
                break
            print("    不通，换下一个源")
        if not ok:
            print("  ✗ 三个源都没成（也可挂代理：$env:HTTPS_PROXY='http://127.0.0.1:7897' 后重试）")
            return False
    print("  完成")
    return True


def do_warm_asr(model=""):
    """预下转写模型：**SenseVoice**（2026-10-13 起是唯一引擎）。

    原来是"默认 SenseVoice、--asr-model 走 faster-whisper"两条路；faster-whisper 连同它
    内置的 461MB 模型已移除（省安装包体积），所以 --asr-model 接收但不再切引擎——
    只是一句提示，避免老命令直接报参数错。
    """
    if model and model != "small":
        print("\n[提示] 转写引擎现在只有 SenseVoice，--asr-model %s 不再生效（参数只是兼容老命令）。"
              % model)
    try:
        sys.path.insert(0, HERE)
        import 能力包
        print("\n[预热] 转写模型 SenseVoice（走国内镜像，约 239MB）")
        print("  [下载] → %s"
              % os.path.join(HERE, "_vendor", "models", "sense-voice"))
        能力包.ensure("sense-voice", HERE, py=sys.executable, auto=True, log=lambda m: print("  " + str(m)))
        print("  完成")
        return True
    except Exception as e:                                    # noqa: BLE001
        print("  ✗ 失败：%s" % str(e)[:300])
        return False


def main():
    ap = argparse.ArgumentParser(description="Seedance skill 环境检查 / 部署自检")
    ap.add_argument("--install", action="store_true", help="安装缺的必需项（ffmpeg 走 winget，会先问一次）")
    ap.add_argument("--extras", action="store_true",
                    help="连「用到才装」的大包一起装（numpy/opencv/转写/深度，几百 MB）")
    ap.add_argument("--yes", action="store_true", help="不询问（供 agent 在用户已同意后使用）")
    ap.add_argument("--models", action="store_true", help="下载 Depth 模型（约 99MB，走国内镜像）")
    ap.add_argument("--warm-asr", action="store_true", help="预下转写模型 SenseVoice（约 239MB，走国内镜像）")
    ap.add_argument("--asr-model", default="", help="（已废弃）转写只有 SenseVoice，传入只作兼容提示")
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
        # **默认只装必需项**（2026-09-16）：numpy/opencv/转写/深度这几个是"用到才装"的大包，
        # 一次全下就是几百 MB、新机要等很久（用户反馈过）。要一次装全加 --extras。
        todo = [PIP_PKGS[i["项"]] for i in items
                if i["状态"] == "缺" and i["项"] in PIP_PKGS
                and (args.extras or i["级别"] == "必需")]
        skipped = [i["项"] for i in items if i["状态"] == "缺" and i["项"] in PIP_PKGS
                   and not (args.extras or i["级别"] == "必需")]
        need_ff = any(i["状态"] == "缺" and i["项"] == "ffmpeg" for i in items)
        if skipped:
            print("\n[按需] 这几个先不装（用到再加 --extras）：%s" % "、".join(skipped))
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
        do_warm_asr(args.asr_model or "")

    core_ok = all(i["状态"] == "OK" for i in probe() if i["级别"] == "必需")
    print("\n结论：%s" % ("必需项齐备，可以开工。" if core_ok else "必需项有缺失，按上面的 → 处理后再来。"))
    return 0 if core_ok else 1


if __name__ == "__main__":
    sys.exit(main())


def install_item(name):
    """装一个可选项。name: pip:<模块> 或 model:depth。"""
    name = (name or "").strip()
    if name.startswith("pip:"):
        mod = name.split(":", 1)[1]
        pkg = PIP_PKGS.get(mod)
        if not pkg:
            return {"ok": False, "msg": "不认识的项：%s" % mod}
        exe = py_exe() or sys.executable
        r = run([exe, "-m", "pip", "install", "--upgrade", pkg], timeout=1800)
        tail = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()[-3:]
        return {"ok": r.returncode == 0,
                "msg": "%s：%s" % (pkg, " / ".join(tail) or "完成")}
    if name == "model:depth":
        ok = do_models()
        return {"ok": ok, "msg": "深度模型已就绪" if ok else "下载没成功（网络不通？稍后再试）"}
    return {"ok": False, "msg": "不认识的项：%s" % name}
