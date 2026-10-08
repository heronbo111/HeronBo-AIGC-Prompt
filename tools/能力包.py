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
    stt     转写（faster-whisper）—— 拆解/诊断转写音轨、做字幕（模型 461MB）
    depth   转深度片（Depth-Anything-V2-Small）—— 模型 95MB

（2026-10-08 更新：这两个能力包的模型**从国内模型镜像按需直取**，不再挂 Release 附件——
`vendor-2026-09-22` 那串 cap-*.zip 实测全 404。首选 ModelScope，理由见下面常量注释。）

⚠️ **cv2 / numpy 不在这里**（2026-09-22 定）：成片拆解、人物遮罩、成片对比、竖版画布
四个工具都要它俩，放"可选"会悄悄弄坏这些功能 → 归**核心 wheels**，默认就装。
默认安装因此是：wheels-core（含 cv2/numpy）＋ ffmpeg ＋ 工作台 exe ≈ 140MB，
**机器上已有能力够的系统 ffmpeg 时只剩约 68MB**；只把转写与转深度片留给按需。
"""
import os
import re
import sys

# 大件挂在 Release 附件上（仓库只放代码）。环境变量 HERONBO_VENDOR_REL 可覆盖（内网/自建镜像用）。
OWNER_GH, OWNER_GEE, REPO = "heronbo111", "HeronBo", "HeronBo-AIGC-Prompt"

# 能力包（依赖资源）固定通道：大文件不随产品版本重复挂，统一放这个 tag 上。
ASSETS_TAG = "vendor-2026-09-22"
DEFAULT_REL = ASSETS_GH = ("https://github.com/%s/%s/releases/download/%s/" % (OWNER_GH, REPO, ASSETS_TAG))
# Gitee 同一套附件（**国内首选**）：实测本机 GitHub 直连 0 字节（读超时）、Gitee 归档通道 2.4–3.3MB/s。
DEFAULT_GITEE_REL = ASSETS_GEE = ("https://gitee.com/%s/%s/releases/download/%s/" % (OWNER_GEE, REPO, ASSETS_TAG))
# 裸连 GitHub 慢/不通时依次试这些前缀（拼**完整 URL**）
DEFAULT_MIRRORS = ["https://gh-proxy.com/", "https://ghproxy.net/"]

# 模型（不是 GitHub 附件，是 HF 仓库）走**国内模型镜像**（2026-10-08）。
#
# 为什么必须换路：实测 GitHub 固定通道 `vendor-2026-09-22` 的五个 cap-* 附件**全部 404**
# （连那个 tag 本身在 GitHub 上都没建 Release）→ "按需下载"原来是一条走不通的死路径。
#
# ⚠️ **首选 ModelScope，不是 hf-mirror**（2026-10-08 实测，反直觉但决定性）：
#   · hf-mirror 的小文件能过，但**大件会被 302 到海外的 Xet/CAS 主机**
#     （`cas-bridge.xethub.hf.co`，AWS us-east-1）→ 字节仍出海外，实测 0.0–0.17MB/s，
#     甚至拿回 `401 Unauthorized`（`cas-server.xethub.hf.co/v2/reconstructions/…`）。
#   · ModelScope 最终由国内 `cdn-lfs-cn-1.modelscope.cn` 供字节：实测 9.2–12.3MB/s，
#     匿名、支持 Range（`bytes=0-1023`→206、尾段→206），461MB 约 47 秒。
# 所以 `_hf_urls` 里 ModelScope 排第一，hf-mirror 只当兜底。
#
# 另一个 ModelScope 的坑：它 **HEAD 不返回 Content-Length，GET 才返回**。
# `下载器.remote_info` 走 HEAD 会拿到 0 → 进度条算不出来；靠 `_once` 里 GET 的
# Content-Length 兜住（那边确实能拿到），并且下面按 **sha256 固定校验**，不依赖大小。
HF_MIRROR = "https://hf-mirror.com"
MS_API = "https://modelscope.cn/api/v1/models"
MS_WEB = "https://modelscope.cn/models"

# 产品本体走版本化 latest（2026-09-29 改造，替代"固定 tag 覆盖、看不到迭代"）：
# 每次发版一个不可变 vX.Y.Z release，客户端经平台 releases/latest 自动找最新。
LATEST_API = [
    ("gitee", "https://gitee.com/api/v5/repos/%s/%s/releases/latest" % (OWNER_GEE, REPO)),
    ("github", "https://api.github.com/repos/%s/%s/releases/latest" % (OWNER_GH, REPO)),
]
PRODUCT_FILES = {"version.json", "score-tool.exe"}   # 另：任何 .exe（完整 setup）都算产品本体
LATEST_TTL = 300.0
_LATEST_CACHE = {"ts": 0.0, "data": None}


def _http_json(url, timeout=12):
    import json as _json
    import urllib.request
    req = urllib.request.Request(
        url, headers={"User-Agent": "heronbo-deploy/2", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return _json.loads(r.read().decode("utf-8", "replace"))


def _norm_latest(d):
    """平台 release 对象归一：tag/version + {asset 名: 下载 URL}。"""
    tag = str(d.get("tag_name") or "").strip()
    assets = {a.get("name"): a.get("browser_download_url")
              for a in (d.get("assets") or []) if a.get("name") and a.get("browser_download_url")}
    return {"tag": tag, "version": tag.lstrip("vV"), "name": d.get("name") or "",
            "notes": d.get("body") or "", "assets": assets}


def latest_release(timeout=12, force=False, log=None):
    """最新产品版本：依次问 Gitee → GitHub 的 releases/latest，返回归一 dict；都失败返回 {}。

    结果缓存 LATEST_TTL 秒（一次检查会取 version.json + score-tool，避免重复打 API）。
    离线 / 匿名限流不算错误，返回 {}，调用方按"离线"处理。
    """
    import time as _time
    if (not force and _LATEST_CACHE["data"] is not None
            and (_time.time() - _LATEST_CACHE["ts"]) < LATEST_TTL):
        return _LATEST_CACHE["data"]
    err = ""
    for _who, url in LATEST_API:
        try:
            r = _norm_latest(_http_json(url, timeout=timeout))
            if r["tag"]:
                _LATEST_CACHE.update(ts=_time.time(), data=r)
                return r
        except Exception as e:                          # noqa: BLE001
            err = str(e)[:80]
    if log is not None:
        log("latest 取不到：%s" % err)
    return {}


def _is_product(rel):
    base = os.path.basename(str(rel))
    return base in PRODUCT_FILES or base.endswith(".exe")


def _gitee_asset(tag, rel):
    return "https://gitee.com/%s/%s/releases/download/%s/%s" % (
        OWNER_GEE, REPO, tag, os.path.basename(rel))


def _gh_asset(tag, rel):
    return "https://github.com/%s/%s/releases/download/%s/%s" % (
        OWNER_GH, REPO, tag, os.path.basename(rel))


def _latest_urls_for(rel, timeout=12):
    """产品本体：解析最新 vX.Y.Z tag，按 Gitee → GitHub 直连 → gh 代理 给出地址。"""
    r = latest_release(timeout=timeout)
    tag = r.get("tag")
    if not tag:
        return []                                      # 离线，交给 urls_for 兜底
    base = os.path.basename(str(rel))
    gh = _gh_asset(tag, base)
    out = [_gitee_asset(tag, base), gh]
    out += [(m + gh) for m in DEFAULT_MIRRORS]
    return out


def _fixed_urls_for(rel, rel_base=None, mirrors=None):
    """能力包依赖：固定 assets 通道（Gitee → GitHub 直连 → gh-proxy → ghproxy.net）。

    Gitee 排第一的理由：实测 GitHub 直连一个字节都下不来（读超时），镜像速度波动大，
    Gitee 是国内 CDN、速度稳（2.4–3.3MB/s）也不受墙影响；下载器续传重试，换源断点续接。
    """
    gh = rel_base or os.environ.get("HERONBO_VENDOR_REL") or ASSETS_GH
    env_mir = [m for m in (os.environ.get("HERONBO_VENDOR_MIRRORS") or "").split(",") if m]
    mir = env_mir or (mirrors if mirrors is not None else DEFAULT_MIRRORS)
    out = [ASSETS_GEE + rel, gh + rel]
    out += [(m + gh + rel) for m in mir]
    return out


def urls_for(rel, rel_base=None, mirrors=None, timeout=12):
    """这个附件按什么顺序试（Gitee → GitHub 直连 → gh 代理）。

    产品本体（score-tool.exe / version.json / *.exe setup）走**版本化 latest**：
      先解析最新 vX.Y.Z tag，每个版本在仓库都看得到；
    能力包依赖（wheels / ffmpeg / cap-*）走**固定 assets 通道**，大文件不随版本重复挂。
    latest 取不到（离线）时产品文件退回固定通道兜底。
    """
    rel = os.path.basename(str(rel))
    if _is_product(rel):
        u = _latest_urls_for(rel, timeout=timeout)
        if u:
            return u
    return _fixed_urls_for(rel, rel_base=rel_base, mirrors=mirrors)

# 默认安装就要有的（不在这里 = 默认必装）
CORE_ASSETS = [
    # (附件名, 落到 _vendor 下的子目录, 约多少 MB)
    ("wheels-core.zip", "wheels", 50),          # pywebview 依赖 + cv2 + numpy
    ("ffmpeg-win64-gpl-shared.zip", "ffmpeg", 72),
]

# 每件模型的 (远端路径, sha256, revision, 落盘名)：
#   · sha256 是**权威内容指纹**（HF 官方 LFS 值，2026-10-08 全量下完逐个核过；
#     depth 三件的值与本机装机树里那三份**逐字节一致**，等于互相印证）。
#     钉死它有两个好处：镜像给错/给成别的版本能当场发现；换源续传时不会把
#     两个不同 revision 的字节拼在一起还当成功。
#   · revision 钉住版本（同一个 repo 的不同文件可以在不同 revision，whisper 那份就是这样：
#     model.bin 与三个小文件的 revision **不一样**，统一喂一个 revision 会 404）。
#     revision 只是"去哪儿拿"，sha256 才是"对不对"。
#   · 落盘名：远端是 `onnx/model.onnx`，但**本地要叫 `model.onnx`（平铺）** ——
#     `深度视频.BUNDLED_MODEL`、`环境检查.DEPTH_DIR`、安装包载荷三处都按平铺找它，
#     照远端原样铺成 `onnx/model.onnx` 会变成"下好了但没人认"。
CAPS = {
    "stt": {
        "title": "转写（faster-whisper）",
        "why": "要把音轨转成文字（拆解、废片诊断、字幕）时才要",
        "mb": 461,
        "assets": [],                                     # 不再走 Release 附件（通道 404，见上）
        "wheels_dirs": ["wheels-stt"],
        "mods": ["faster_whisper"],
        # 装机自带 runtime 里已烘好 faster_whisper/ctranslate2/tokenizers，
        # 新机唯一缺的就是这个 461MB 的 model.bin。
        "hf_repo": "Systran/faster-whisper-small",
        "hf_files": [
            ("config.json", "b55496ac7940a7ae47d2c01eab40edfd8701feec1229d9cce3b40014383fb828",
             "afb04c7b910ac0b9851459d068069ab337931534", "config.json"),
            ("tokenizer.json", "fb7b63191e9bb045082c79fd742a3106a12c99513ab30df4a0d47fa6cb6fd0ab",
             "afb04c7b910ac0b9851459d068069ab337931534", "tokenizer.json"),
            ("vocabulary.txt", "34ce3fe1c5041027b3f8d42912270993f986dbc4bb34cf27f951e34a1e453913",
             "afb04c7b910ac0b9851459d068069ab337931534", "vocabulary.txt"),
            ("model.bin", "3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671",
             "ace8b2ad9dee031c53b6371f6c3c918b5e4f1db9", "model.bin"),
        ],
        "model_dir": "models/faster-whisper-small",
        "model": "models/faster-whisper-small/model.bin",
    },
    "depth": {
        "title": "转深度片（Depth-Anything-V2-Small）",
        "why": "要把参考片转成深度片（L4 复刻动作的替代路线）时才要",
        "mb": 95,
        "assets": [],                                     # 同上：模型走镜像，runtime 里已有 onnxruntime
        "wheels_dirs": ["wheels-depth"],
        "mods": ["onnxruntime"],
        "hf_repo": "onnx-community/depth-anything-v2-small",
        "hf_files": [
            ("config.json", "3aee5b9bc4f711ee885c2526d871f0c8c6c8c4b26b8e04253d0167f6a83264f5",
             "412694b964262937af8683baa8a5433485787a9a", "config.json"),
            ("preprocessor_config.json", "03576db3c13dd0471fdf5f5e1428befcb95de063fe699879150b293dc9e0a2c6",
             "412694b964262937af8683baa8a5433485787a9a", "preprocessor_config.json"),
            # ← 远端在 onnx/ 子目录里，本地平铺成 model.onnx（见上面字段说明）
            ("onnx/model.onnx", "afb6a5c28f3b6bf1618c6e43f02073ef9dfdc70e937502d51603e57b0a1df10c",
             "412694b964262937af8683baa8a5433485787a9a", "model.onnx"),
        ],
        "model_dir": "models/depth-anything-v2-small",
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
    """这个能力包**在盘上齐了吗**。

    2026-10-08 修假阴性：原来只认 `_vendor/wheels-stt`「目录在不在」，可是装机 runtime 里
    早就把 faster_whisper / ctranslate2 / tokenizers / onnxruntime 烘进 site-packages 了——
    目录不在盘上并不等于能力用不了（实测本机 depth 因此被判 False，而 onnxruntime 明明能
    导入、model.onnx 明明在盘、InferenceSession 明明加载成功）。改成：模型文件在 + 模块导得
    进（或 wheels 目录在）就算齐。
    """
    v = vendor_dir(tools_dir)
    if cap not in CAPS:
        return False
    c = CAPS[cap]
    m = c.get("model")
    if m and not os.path.isfile(os.path.join(v, m.replace("/", os.sep))):
        return False                                      # 模型没在盘上：这一条是硬门槛
    have_dir = any(os.path.isdir(os.path.join(v, d)) for d in c["wheels_dirs"]) \
        or os.path.isdir(os.path.join(v, "wheels-heavy"))  # 老布局也算在
    if have_dir:
        return True
    return all(mod_present(x) for x in c["mods"])          # runtime 里烘好的那份也算在


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
        # GUI 宿主没控制台：python/pip 子进程不加 CREATE_NO_WINDOW 会闪/挂黑窗（2026-10-07 黑窗修复）
        p = subprocess.run([py, "-c", code], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0)
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
            "  ↳ 装上它：python tools\\能力包.py get %s\n"
            "  ↳ 或者联网在线装：python tools\\环境检查.py --install --yes --extras"
            % (txt, " ".join(caps)))


def ensure(cap, tools_dir=None, py=None, auto=None, log=print):
    """用到某能力时调它：齐了返回 True；缺了就**抛 NeedCapability**（带上怎么装）。

    auto=True/False 或环境变量 HERONBO_AUTOFETCH=1 时改成**自动拉**（下载 + 解包 + pip 装），
    默认不自动——新机第一次用就闷头下 100MB 不是好体验，先告诉用户、让他一句话决定。
    注意：这里**不替用户跑生成任务**，只是把依赖补齐（铁律只管生成平台代提交）。
    """
    if present(cap, tools_dir):
        return True
    py = py or sys.executable
    if not missing_mods(py, cap) and not _model_dir_ok(cap, tools_dir):
        log("[能力包] %s 的 python 依赖在，只差模型文件（约 %dMB）…" % (cap, CAPS[cap]["mb"]))
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


def _model_dir_ok(cap, tools_dir=None):
    """模型文件在不在盘上（present 的硬门槛，单独拎出来给日志用）。"""
    m = CAPS.get(cap, {}).get("model")
    if not m:
        return True
    return os.path.isfile(os.path.join(vendor_dir(tools_dir), m.replace("/", os.sep)))


def _fetch(cap, tools_dir, log):
    """把这个能力包的模型拉下来（**ModelScope 主、hf-mirror 备**，见文件头注释）。

    2026-10-08 改造：原来下的是 Release 附件 `cap-stt.zip` / `cap-depth-*.zip`，实测那五个
    附件（含 `vendor-2026-09-22` 这个 tag 本身）在平台上**全部 404** → 自动补能力这条路
    一直是死的。模型本来就在公开仓库里，改成从国内镜像直取，不需要任何凭据。

    每件都带 **sha256 校验**（`下载器._verify` 会当场把错的删掉重下）——镜像给错版本、
    换源时把断点拼错，都能当场抓到，不会把坏模型留到 user 真用时才炸。
    """
    cap_cfg = CAPS[cap]
    v = vendor_dir(tools_dir)
    dest_dir = os.path.join(v, cap_cfg["model_dir"].replace("/", os.sep))
    os.makedirs(dest_dir, exist_ok=True)
    dl = _downloader(tools_dir)
    repo = cap_cfg["hf_repo"]
    for rel, sha, rev, local in cap_cfg["hf_files"]:
        dst = os.path.join(dest_dir, local.replace("/", os.sep))
        if os.path.isfile(dst) and os.path.getsize(dst) > 0:
            # 已在盘：有指纹就核一下（装机树里那份 depth 模型就是这么验的），
            # 核不过当没下过；没有指纹（老树）就信它，别为了重下 461MB 卡住用户。
            if not sha or _sha256_of(dst) == sha:
                continue
            log("  本地这份 %s 指纹不对 → 重下" % local)
            try:
                os.remove(dst)
            except OSError:
                pass
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        log("  下 %s/%s（%s）…" % (repo, rel, rev[:8]))
        urls = _hf_urls(repo, rel, rev)
        try:
            got = dl.fetch_any(urls, dst, sha256=sha, tries=2, log=log, timeout=180)
        except Exception as e:                               # noqa: BLE001
            raise NeedCapability("模型下不下来（%s/%s）：%s\n"
                                 "  ↳ 也可以手动下：%s\n"
                                 "  ↳ 或者临时挂代理：$env:HTTPS_PROXY='http://127.0.0.1:7897'"
                                 % (repo, rel, str(e)[:120], urls[0]))
        if not got:
            raise NeedCapability("模型没下成（%s/%s）。\n  ↳ 手动下：%s" % (repo, rel, urls[0]))
    return True


def _sha256_of(path, chunk=1 << 20):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _hf_urls(repo, rel, rev="main"):
    """一个文件的候选直链，**ModelScope 优先**（顺序有实测依据，见文件头注释）。

    ModelScope 有两种写法，都保留：`models/<repo>/resolve/<rev>/<path>`（网页同款直链，
    带 rev 才稳）与 `/api/v1/models/<repo>/repo?Revision=&FilePath=`（api 风格，实测 206）。
    """
    env = os.environ.get("HERONBO_HF_MIRROR", "").strip().rstrip("/")
    urls = []
    if env:                                   # 用户点名要用的源，放最前（探针/排障用）
        urls.append("%s/%s/resolve/main/%s" % (env, repo, rel))
    urls.append("%s/%s/resolve/%s/%s" % (MS_WEB, repo, rev, rel))
    urls.append("%s/%s/repo?Revision=%s&FilePath=%s" % (MS_API, repo, rev, rel))
    urls.append("%s/%s/resolve/main/%s" % (HF_MIRROR, repo, rel))
    urls.append("https://huggingface.co/%s/resolve/main/%s" % (repo, rel))
    return urls


def _downloader(tools_dir):
    """借 下载器.py 的原语（续传/重试/多源/进度）；拿不到就退回最小自实现。"""
    here = _root(tools_dir)
    if here not in sys.path:
        sys.path.insert(0, here)
    import importlib
    try:
        return importlib.import_module("下载器")
    except Exception:                                     # noqa: BLE001
        return _MiniDL()


class _MiniDL:
    """下载器.py 不在时的最小兜底（够下模型：Range 续传 + 重试 + sha256 校验）。

    只在 `下载器.py` 缺失时才会走到（正常安装不会）——所以这里**必须自己校验 sha256**：
    没有校验的兜底会把"下了一半"当成成功。
    """

    def fetch_any(self, urls, dst, sha256="", tries=2, log=print, timeout=180, **kw):
        last = None
        for u in urls:
            for _ in range(max(1, tries)):
                try:
                    self._once(u, dst, timeout, log)
                    if sha256 and _sha256_of(dst) != sha256.lower():
                        # 内容不对（换源拼错断点/镜像给错版本）→ 丢掉重下，别留坏文件
                        log("    指纹不对 → 丢掉重下")
                        os.remove(dst)
                        raise OSError("sha256 不对")
                    return os.path.getsize(dst)
                except Exception as e:                    # noqa: BLE001
                    last = e
                    log("  源不通（%s）：%s" % (u.split("/")[2], e))
        log("  都下不下来：%s" % last)
        return 0

    def _once(self, url, dst, timeout, log):
        import urllib.request
        have = os.path.getsize(dst) if os.path.isfile(dst) else 0
        req = urllib.request.Request(url, headers={"User-Agent": "heronbo/2"})
        if have:
            req.add_header("Range", "bytes=%d-" % have)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            code = getattr(r, "status", 200) or 200
            if have and code != 206:                      # 源不认 Range → 从头下
                have = 0
            with open(dst, "ab" if have else "wb") as f:
                while True:
                    buf = r.read(1 << 20)
                    if not buf:
                        break
                    f.write(buf)


def mirror_env():
    """让 faster_whisper / huggingface_hub 走国内镜像（设 `HF_ENDPOINT`，**不改调用方代码**）。

    ⚠️ 2026-10-08 实测的**边界**：`HF_ENDPOINT=https://hf-mirror.com` 对小文件有效
    （`hf_hub_download(...'config.json')` 成功），但 **LFS 大件不行**——镜像会把 `model.bin`
    这类 302 到 HF 的 Xet CAS 服务器，然后被 `401 Unauthorized` 挡下来
    （`cas-server.xethub.hf.co/v2/reconstructions/...`）。所以**下模型请走 `ensure()`**
    （自己的下载器直取 resolve 直链，实测 461MB / 21.5MB/s / 支持 Range 续传）；
    本函数只适合"模型已在本地缓存、只想让它别去连官方站"这类场景。

    返回设好的 endpoint，供调用方打日志。
    """
    ep = os.environ.get("HF_ENDPOINT", "").strip()
    if not ep:
        ep = os.environ.get("HERONBO_HF_MIRROR", "").strip() or HF_MIRROR
        os.environ["HF_ENDPOINT"] = ep
    if os.environ.get("HERONBO_NO_HF") == "1":            # 显式要求离线：别偷偷联网
        os.environ["HF_HUB_OFFLINE"] = "1"
    else:
        os.environ["HF_HUB_OFFLINE"] = "0"
    return ep


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
    # 同 missing()：pip 要跑几十秒，不隐藏就是挂一个几十秒的黑窗（2026-10-07 黑窗修复）
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=1800,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0)
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
        # GUI 宿主没控制台：ffmpeg/ffprobe 等控制台程序要隐藏（2026-10-07 黑窗修复）
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0)
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
