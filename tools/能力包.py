# -*- coding: utf-8 -*-
r"""能力包：把"用到才装"的大依赖从默认安装里摘出来，按名字按需拉。

为什么（2026-09-22，用户："我最想解决的就是安装慢的核心问题"）：

  默认安装原来是 304MB，但**默认流程（rules 49 轻流程：抽帧看图 → 读需求 → 出提示词 → 体检）
  一个字节都不需要**这些。实测 `_vendor` 构成：

    ffmpeg 包 82MB（要）｜ core wheels 4MB（要，pywebview 用）｜ 工作台 exe 18MB（要）
    wheels-heavy 117MB  ← 只有"转写"和"图像/深度"用
    depth 模型 88MB     ← 只有"转深度片"用
    其中 `av`(PyAV) 26MB **全仓库没有任何 import**（环境检查.py 也不检查它）→ 直接丢

  拆完之后：**默认安装 304MB → 约 104MB**；想要转写/深度的人再按需拉（各 26 / 101MB）。

三个能力（名字就是 CLI 上用的 key）：
    stt     转写（faster-whisper，约 28MB）—— 拆解/诊断转写音轨、做字幕
    depth   转深度片（Depth-Anything-V2-Small 模型 88MB + onnxruntime 13MB）

⚠️ **cv2 / numpy 不在这里**（2026-09-22 定）：成片拆解、人物遮罩、成片对比、竖版画布
四个工具都要它俩，放"可选"会悄悄弄坏这些功能 → 归**核心 wheels**，默认就装。
默认安装因此是：wheels-core（含 cv2/numpy）＋ ffmpeg ＋ 工作台 exe ≈ 140MB，
**机器上已有能力够的系统 ffmpeg 时只剩约 68MB**；只把转写与转深度片留给按需。
"""
import os
import re
import sys

# 大件挂在 Release 附件上（仓库只放代码）。换 tag 时改这一处即可；
# 环境变量 HERONBO_VENDOR_REL 可以覆盖（内网/自建镜像用）。
DEFAULT_REL = "https://github.com/heronbo111/HeronBo-AIGC-Prompt/releases/download/vendor-2026-09-22/"
# Gitee 同一套附件（**国内首选**）：2026-09-22 实测本机 GitHub 直连 0 字节（读超时）、
# Gitee 归档通道 2.4–3.3MB/s；挂上去之后新机按下面 SOURCES 的顺序试，谁先成用谁。
DEFAULT_GITEE_REL = "https://gitee.com/HeronBo/HeronBo-AIGC-Prompt/releases/download/vendor-2026-09-22/"
# 裸连 GitHub 慢/不通时依次试这些前缀（拼**完整 URL**）
DEFAULT_MIRRORS = ["https://gh-proxy.com/", "https://ghproxy.net/"]


def urls_for(rel, rel_base=None, mirrors=None):
    """这个附件按什么顺序试：**Gitee → GitHub 直连 → gh-proxy → ghproxy.net**。

    为什么 Gitee 排第一（2026-09-22，用户问"把 300MB 放到 gitee 会不会更快"）：实测本机
    GitHub 直连一个字节都下不来（读超时），gh-proxy 当天 10.9MB/s 但 ghproxy.net 只有 0.36MB/s
    ——镜像的运气波动太大；Gitee 是国内 CDN、速度稳（归档通道实测 2.4–3.3MB/s），也不受墙影响。
    脚本每台机器各自试，谁先成用谁；下载器会续传与重试，换源时断点也是接着下的。
    """
    gh = rel_base or os.environ.get("HERONBO_VENDOR_REL") or DEFAULT_REL
    env_mir = [m for m in (os.environ.get("HERONBO_VENDOR_MIRRORS") or "").split(",") if m]
    mir = env_mir or (mirrors if mirrors is not None else DEFAULT_MIRRORS)
    out = [DEFAULT_GITEE_REL + rel, gh + rel]
    out += [(m + gh + rel) for m in mir]
    return out

# 默认安装就要有的（不在这里 = 默认必装）
CORE_ASSETS = [
    # (附件名, 落到 _vendor 下的子目录, 约多少 MB)
    ("wheels-core.zip", "wheels", 50),          # pywebview 依赖 + cv2 + numpy
    ("ffmpeg-win64-gpl-shared.zip", "ffmpeg", 72),
]

CAPS = {
    "stt": {
        "title": "转写（faster-whisper）",
        "why": "要把音轨转成文字（拆解、废片诊断、字幕）时才要",
        "mb": 28,
        "assets": [("cap-stt.zip", "wheels-stt", 28)],
        "wheels_dirs": ["wheels-stt"],
        "mods": ["faster_whisper"],
    },
    "depth": {
        "title": "转深度片（Depth-Anything-V2-Small）",
        "why": "要把参考片转成深度片（L4 复刻动作的替代路线）时才要",
        "mb": 101,
        "assets": [("cap-depth-model.zip", "models", 88),        # 模型：单独一件，Gitee 单附件限 100MB
                   ("cap-depth-runtime.zip", "wheels-depth", 13)],
        "wheels_dirs": ["wheels-depth"],
        "mods": ["onnxruntime"],
        "model": "models/depth-anything-v2-small/model.onnx",
    },
}
ORDER = ["stt", "depth"]


class NeedCapability(Exception):
    """缺能力包：带上"跑哪条命令"的说明，调用方直接把它打给用户看即可。"""


def _root(tools_dir=None):
    return tools_dir or os.path.dirname(os.path.abspath(__file__))


def vendor_dir(tools_dir=None):
    return os.path.join(_root(tools_dir), "_vendor")


def assets_of(caps):
    """(附件名, 子目录, MB) 列表：core + 指定能力包（含依赖，如 depth→vision）。"""
    want = []
    for c in caps:
        if c not in CAPS:
            raise KeyError("没这个能力包：%s（可选：%s）" % (c, "、".join(ORDER)))
        want.append(c)
        for dep in CAPS[c].get("needs") or []:
            want.append(dep)
    out = list(CORE_ASSETS)
    seen = set()
    for c in want:
        if c in seen:
            continue
        seen.add(c)
        out += CAPS[c]["assets"]
    return out


def present(cap, tools_dir=None):
    """这个能力包**在盘上齐了吗**（目录在位 + 模块装得上）。"""
    v = vendor_dir(tools_dir)
    if cap not in CAPS:
        return False
    c = CAPS[cap]
    for d in c["wheels_dirs"]:
        if not os.path.isdir(os.path.join(v, d)):
            # 老布局（仓库里只有 wheels-heavy 的年代）也算在：装机不必先重下
            if not os.path.isdir(os.path.join(v, "wheels-heavy")):
                return False
    m = c.get("model")
    if m and not os.path.isfile(os.path.join(v, m.replace("/", os.sep))):
        # 模型也可能还躺在压缩包旁边没解
        return False
    return True


def mod_present(mod):
    try:
        __import__(mod)
        return True
    except Exception:                                     # noqa: BLE001
        return False


def missing_mods(py=None, cap=None):
    """真正 import 得上去吗——用**要装的那个解释器**问（工作台自带的 python 与系统 python 不同）。"""
    import subprocess
    mods = []
    caps = [cap] if cap else ORDER
    for c in caps:
        mods += CAPS[c]["mods"]
    if not mods:
        return []
    code = ";".join("import %s" % m for m in mods)
    py = py or sys.executable
    try:
        p = subprocess.run([py, "-c", code], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120)
    except (OSError, subprocess.SubprocessError):
        return mods
    return [] if p.returncode == 0 else mods


def status(tools_dir=None, py=None):
    """给界面/CLI 用的一行行状态。"""
    rows = []
    v = vendor_dir(tools_dir)
    for fn, sub, mb in CORE_ASSETS:
        z = os.path.join(v, fn)
        d = os.path.join(v, sub)
        rows.append(("core", fn, os.path.isdir(d) or os.path.isfile(z), mb))
    for c in ORDER:
        rows.append((c, CAPS[c]["title"], present(c, tools_dir), CAPS[c]["mb"]))
    return rows


def hint(caps):
    """缺能力时给用户看的**一句话 + 一条命令**（别让 agent 自己编命令）。"""
    caps = [c for c in (caps if isinstance(caps, (list, tuple)) else [caps]) if c in CAPS]
    if not caps:
        return ""
    txt = "、".join("%s（%s，约 %dMB）" % (c, CAPS[c]["title"], CAPS[c]["mb"]) for c in caps)
    return ("缺能力包：%s\n"
            "  ↳ 装上它：python tools\\部署.py vendor --fetch --%s\n"
            "  ↳ 或者联网在线装：python tools\\环境检查.py --install --yes --extras"
            % (txt, " --".join(caps)))


def ensure(cap, tools_dir=None, py=None, auto=None, log=print):
    """用到某能力时调它：齐了返回 True；缺了就**抛 NeedCapability**（带上怎么装）。

    auto=True/False 或环境变量 HERONBO_AUTOFETCH=1 时改成**自动拉**（下载 + 解包 + pip 装），
    默认不自动——新机第一次用就闷头下 100MB 不是好体验，先告诉用户、让他一句话决定。
    注意：这里**不替用户跑生成任务**，只是把依赖补齐（铁律只管生成平台代提交）。
    """
    if present(cap, tools_dir) and not missing_mods(py, cap):
        return True
    if auto is None:
        auto = os.environ.get("HERONBO_AUTOFETCH") == "1"
    if not auto:
        raise NeedCapability(hint(cap))
    log("[能力包] 缺 %s，开始自动补（%s）…" % (cap, CAPS[cap]["title"]))
    _fetch(cap, tools_dir, log)
    _pip(cap, tools_dir, py, log)
    if not present(cap, tools_dir):
        raise NeedCapability(hint(cap))
    return True


def _fetch(cap, tools_dir, log):
    """下这个能力包的附件并解包（走 下载器：续传 + 重试 + sha256）。"""
    import zipfile
    here = _root(tools_dir)
    sys.path.insert(0, here)
    import importlib
    dl = importlib.import_module("下载器")
    v = vendor_dir(tools_dir)
    sums = _sums(dl)
    for fn, sub, mb in CAPS[cap]["assets"]:
        z = os.path.join(v, fn)
        log("  下 %s（约 %dMB）…" % (fn, mb))
        _fetch_one(dl, sums.get(fn, ""), fn, z, log)
        os.makedirs(os.path.join(v, sub), exist_ok=True)
        with zipfile.ZipFile(z) as zf:
            zf.extractall(v)
        _flatten(v, sub, log)


def _fetch_one(dl, sha, fn, z, log):
    """按 urls_for 的顺序试源（Gitee → GitHub → 镜像）；**断点跨源保留**。"""
    return dl.fetch_any(urls_for(fn), z, sha256=sha, tries=2, log=log)


def _flatten(v, sub, log):
    inner = os.path.join(v, sub, sub)
    if not os.path.isdir(inner):
        return
    import shutil
    for name in os.listdir(inner):
        src, dst = os.path.join(inner, name), os.path.join(v, sub, name)
        if not os.path.exists(dst):
            shutil.move(src, dst)
    try:
        os.rmdir(inner)
    except OSError:
        pass


def _sums(dl):
    """Release 上的 SHA256SUMS.txt（没有就算了：大小校验仍然生效）。"""
    try:
        return dl.hashes(urls_for("SHA256SUMS.txt"))
    except Exception:                                     # noqa: BLE001
        return {}


def _pip(cap, tools_dir, py, log):
    import subprocess
    v = vendor_dir(tools_dir)
    dirs = [os.path.join(v, d) for d in CAPS[cap]["wheels_dirs"] if os.path.isdir(os.path.join(v, d))]
    if not dirs and os.path.isdir(os.path.join(v, "wheels-heavy")):
        dirs = [os.path.join(v, "wheels-heavy")]           # 老布局
    if not dirs:
        return True
    py = py or sys.executable
    cmd = [py, "-m", "pip", "install", "--no-index"]
    for d in dirs:
        cmd += ["--find-links", d]
    cmd += CAPS[cap]["mods"]
    log("  装 %s…" % "、".join(CAPS[cap]["mods"]))
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=1800)
    if p.returncode != 0:
        log("  pip 没装成：%s" % ((p.stderr or p.stdout or "")[-300:]))
        return False
    return True


# ── 系统 ffmpeg 够不够用（2026-09-22）──────────────────────────────────────────
# 默认安装里最大的一件就是 ffmpeg 包（82MB）。但新机上**常常已经有**一份 ffmpeg（winget、
# conda、各种软件自带），而且仓库里的工具本来就是「PATH 优先、仓库自带兜底」（见 人物遮罩.py
# / 下载B站成片.py 里的 `need()`）。所以装之前先问一句：系统那份**编码器/滤镜齐不齐**？
# 齐 → 跳过这 82MB（日志里写明用的是谁）；不齐才拉仓库自带那包。
# 标准只列仓库真会用到的：h264 / aac / mp3 编码（音色参考导出 mp3）＋ 字幕与文字滤镜
# （字幕烧录走 libass 的 subtitles，drawtext 走 freetype）。
FFMPEG_NEED_ENC = ("libx264", "aac", "libmp3lame")
FFMPEG_NEED_FLT = ("subtitles", "drawtext")
FFMPEG_MIN = (4, 4)


def _run(cmd, timeout=25):
    import subprocess
    try:
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def ffmpeg_ok(exe="ffmpeg", timeout=25):
    """系统那份 ffmpeg 能不能顶上默认安装（返回 (能否, 说明)）。"""
    import shutil
    p = shutil.which(exe) if os.sep not in exe else (exe if os.path.isfile(exe) else None)
    if not p:
        return False, "PATH 里没有 %s" % exe
    r = _run([p, "-hide_banner", "-version"], timeout)
    if r is None or r.returncode != 0:
        return False, "%s 跑不起来" % p
    head = ((r.stdout or "").strip().splitlines() or [""])[0]
    ver = re.search(r"version\s+n?(\d+)\.(\d+)", head)
    if ver and (int(ver.group(1)), int(ver.group(2))) < FFMPEG_MIN:
        return False, "%s 版本太老（%s；至少要 %d.%d）" % (p, head[:40], FFMPEG_MIN[0], FFMPEG_MIN[1])
    enc = _run([p, "-hide_banner", "-encoders"], timeout)
    flt = _run([p, "-hide_banner", "-filters"], timeout)
    if enc is None or flt is None:
        return False, "%s 查不了编码器/滤镜列表" % p
    etxt, ftxt = enc.stdout or "", flt.stdout or ""
    miss = [x for x in FFMPEG_NEED_ENC if x not in etxt] + [x for x in FFMPEG_NEED_FLT if x not in ftxt]
    if miss:
        return False, "%s 少了：%s" % (p, "、".join(miss))
    if not shutil.which("ffprobe"):
        return False, "%s 够用，但 PATH 里没有 ffprobe" % p
    return True, "%s 够用（%s）" % (p, head[:60])


def main(argv):
    """命令行：python tools\能力包.py [status|get <cap>|ffmpeg]"""
    cmd = (argv[1] if len(argv) > 1 else "status").lower()
    if cmd == "ffmpeg":
        ok, why = ffmpeg_ok(argv[2] if len(argv) > 2 else "ffmpeg")
        print("  [%s] %s" % ("✓" if ok else " ", why))
        return 0 if ok else 1
    if cmd == "status":
        for key, title, ok, mb in status():
            print("  [%s] %-6s %-38s %s" % ("✓" if ok else " ", key, title,
                                            ("约 %dMB" % mb) if mb else ""))
        return 0
    if cmd == "get":
        caps = argv[2:] or ORDER
        for c in caps:
            try:
                ensure(c, auto=True)
                print("  [✓] %s 好了" % c)
            except NeedCapability as e:
                print("  [!] %s：%s" % (c, e))
                return 2
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
