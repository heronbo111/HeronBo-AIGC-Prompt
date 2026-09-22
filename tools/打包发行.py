# -*- coding: utf-8 -*-
r"""打发行包：把 `_vendor` 里的东西打成 Release 附件（默认安装 + 三个能力包），并出 SHA256SUMS。

为什么（2026-09-22，用户："我最想解决的就是安装慢的核心问题"）：

  默认安装原来是 304MB，而**默认轻流程一个字节都不需要**转写与深度那两坨。实测构成并重排：

    默认（core）：wheels-core 4MB ＋ ffmpeg 76MB ＋ 工作台 exe 18MB
                  —— 新机上若已有**能力够的系统 ffmpeg**（编码器/滤镜探测，见 能力包.py），
                     连这 76MB 都能跳过，默认安装只剩约 22MB
    能力包 stt   ：26MB（faster-whisper）—— 转写/诊断时才装
    能力包 stt   ：28MB（faster-whisper）—— 转写/诊断时才装
    能力包 depth ：101MB（模型 88MB + onnxruntime 13MB，分两件传以避开 Gitee 单附件 100MB 上限）
  注意 cv2/numpy **在核心**（抽帧/遮罩/对比/画布四个工具都要它俩，放可选会弄坏功能）。

  顺带丢掉 `av`（PyAV, 26MB）：全仓库没有任何 `import av`，环境检查.py 也不检查它。

用法：
    python tools\打包发行.py                     # 打到 tools\_release\<tag>\ 并出 SHA256SUMS.txt
    python tools\打包发行.py --tag vendor-2026-09-22
    python tools\打包发行.py --upload-plan       # 只打印上传计划（GitHub / Gitee 两边的命令）
不做的：不自动上传（外发要用户点头），也不 git 提交（附件不进仓库）。
"""
import argparse
import hashlib
import json
import os
import sys
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.join(HERE, "_vendor")
sys.path.insert(0, HERE)
import 能力包 as kp                                             # noqa: E402

TAG_DEFAULT = "vendor-2026-09-22"

# 能力包 → wheel 前缀（按包名前缀归组，剩下来的会**报出来**，不会悄悄丢）
# 注意：`wheels/` 是核心目录（pywebview 依赖 + **cv2 + numpy**，2026-09-22 从"图像能力包"
# 挪回核心——4 个工具都要它俩，放可选会悄悄弄坏抽帧/遮罩/对比/画布）。
WHEEL_GROUPS = {
    "wheels": [],                        # core：_vendor/wheels 整个目录
    "wheels-stt": ["ctranslate2", "tokenizers", "faster_whisper", "faster-whisper",
                   "huggingface_hub", "hf_xet", "fsspec", "pyyaml", "tqdm", "packaging",
                   "protobuf", "httpx", "httpcore", "h11", "anyio", "idna", "certifi",
                   "click", "colorama", "filelock", "typing_extensions", "flatbuffers"],
    "wheels-depth": ["onnxruntime"],
}
# 明确不要的（写出来免得下次有人又问"av 呢"）
DROP = {"av": "全仓库没有 import av（2026-09-22 实测），26MB 白装"}

# ffmpeg 包里能丢的
FFMPEG_DROP = {"ffplay.exe": "播放器，工作台/工具链从不调用（17.9MB）"}


def _mb(p):
    return os.path.getsize(p) / 1048576.0


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(1024 * 1024)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _zip_from_dir(zpath, root, prefix="", skip=(), log=print):
    """把 root 目录压进 zpath，包内路径 = prefix + 相对 root 的路径。

    ⚠️ 别用相对 root 的**父目录**算（2026-09-21 踩过）：那样会多套一层（bin/bin/、wheels/wheels/），
    新机解开后 部署.py 得靠 `_flatten_dup` 兜，而 ffmpeg 那条路不摊平 → 直接找不到 ffmpeg.exe。
    """
    n = 0
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for dp, _dn, fns in os.walk(root):
            for fn in sorted(fns):
                if fn in skip:
                    continue
                fp = os.path.join(dp, fn)
                rel = os.path.relpath(fp, root)
                z.write(fp, (prefix + rel).replace(os.sep, "/"))
                n += 1
    log("    %-30s %6.1f MB  %d 个文件" % (os.path.basename(zpath), _mb(zpath), n))
    return n


def _zip_from_files(zpath, files, base, prefix="", log=print):
    n = 0
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for fp in sorted(files):
            z.write(fp, (prefix + os.path.basename(fp)).replace(os.sep, "/"))
            n += 1
    log("    %-30s %6.1f MB  %d 个文件" % (os.path.basename(zpath), _mb(zpath), n))
    return n


def classify(wheel_dir, log=print):
    """把 wheels-heavy 里的 wheel 按前缀归组；返回 {组名: [文件…]} + 未归组的。"""
    out = {k: [] for k in WHEEL_GROUPS if k != "wheels"}
    left, dropped = [], []
    for fn in sorted(os.listdir(wheel_dir)):
        if not fn.endswith(".whl"):
            continue
        low = fn.lower()
        hit = None
        for grp, prefixes in WHEEL_GROUPS.items():
            if grp == "wheels":
                continue
            for p in prefixes:
                if low.startswith(p.lower().replace("-", "_")) or low.startswith(p.lower()):
                    hit = grp
                    break
            if hit:
                break
        if hit:
            out[hit].append(os.path.join(wheel_dir, fn))
        elif any(low.startswith(d.lower()) for d in DROP):
            dropped.append(fn)
        else:
            left.append(fn)
    for fn in dropped:
        log("    [丢] %s —— %s" % (fn, next(v for k, v in DROP.items() if fn.lower().startswith(k.lower()))))
    for fn in left:
        log("    [!! 未归组] %s —— 请把它加进 WHEEL_GROUPS，否则新机上装不到它" % fn)
    return out, left


def build(tag, notes="", version="", log=print):
    # ⚠️ 运行顺序必须是：打包换位 --build-only → --swap-only → **本脚本** → 上传。
    # 2026-09-22 实测踩过：在换位前跑本脚本，给 version.json/SHA256SUMS 哈希的是**旧 dist**
    # → 元数据和真正发出去的 exe 对不上 → 面板永远说"远端的包换过内容"，装完还循环报新版本。
    _stage = os.path.join(HERE, "_stage", "score-tool.exe")
    _dist = os.path.join(HERE, "dist", "score-tool.exe")
    if os.path.isfile(_stage) and os.path.isfile(_dist) and             os.path.getmtime(_stage) > os.path.getmtime(_dist) + 1:
        log("  [!!] _stage 里有一份比 dist 更新的 exe——你多半跑早了：先 "
            "`打包换位.py --swap-only` 换位，再跑本脚本（否则元数据会指错包）")
    out = os.path.join(HERE, "_release", tag)
    os.makedirs(out, exist_ok=True)
    log("== 打 %s ==" % tag)
    assets = []

    heavy = os.path.join(VENDOR, "wheels-heavy")
    groups, left = ({}, [])
    if os.path.isdir(heavy):
        log("  · 归组 wheels-heavy：")
        groups, left = classify(heavy, log)
    else:
        log("  · 没有 wheels-heavy/，跳过能力包（只打 core）")

    # 1) core wheels
    core_w = os.path.join(VENDOR, "wheels")
    if os.path.isdir(core_w):
        p = os.path.join(out, "wheels-core.zip")
        _zip_from_dir(p, core_w, prefix="wheels/", log=log)
        assets.append(p)
    # 2) 能力包 wheels（**名字从 能力包.py 的清单取**，别再自己拼字符串——2026-09-22 拼错过一次：
    #    wheels-depth 拼成 cap-depth.zip，而清单里叫 cap-depth-runtime.zip → 新机会去下一件不存在的附件）
    for cap in kp.ORDER:
        for fn, sub, mb in kp.CAPS[cap]["assets"]:
            if not sub.startswith("wheels-"):
                continue
            files = groups.get(sub) or []
            if not files:
                log("    [!!] %s 的轮子没归到组里（组名 %s），检查 WHEEL_GROUPS" % (cap, sub))
                continue
            p = os.path.join(out, fn)
            _zip_from_files(p, files, heavy, prefix="%s/" % sub, log=log)
            assets.append(p)
    # 3) 深度模型
    md = os.path.join(VENDOR, "models")
    if os.path.isdir(md):
        p = os.path.join(out, "cap-depth-model.zip")
        _zip_from_dir(p, md, prefix="models/", log=log)
        assets.append(p)
    # 4) ffmpeg（瘦身：丢 ffplay 与未用 dll）
    ffb = os.path.join(VENDOR, "ffmpeg", "bin")
    if os.path.isdir(ffb):
        p = os.path.join(out, "ffmpeg-win64-gpl-shared.zip")
        log("  · ffmpeg 瘦身：丢 %s" % "、".join("%s（%s）" % (k, v) for k, v in FFMPEG_DROP.items()))
        _zip_from_dir(p, ffb, prefix="ffmpeg-win64-gpl-shared/bin/", skip=tuple(FFMPEG_DROP), log=log)
        assets.append(p)

    # 5) SHA256SUMS.txt —— **工作台 exe 也要在里面**（2026-09-22 补）：
    #    部署.py 判断"exe 要不要更新"就是拿这份清单比 sha256（比原来靠 GitHub 发 HEAD 比大小可靠得多，
    #    国内那条 HEAD 常常直接超时 → 表现成"永远不更新"）。exe 由上传脚本一起传，这里只登记哈希。
    exe = os.path.join(HERE, "dist", "score-tool.exe")
    extra = []
    if os.path.isfile(exe):
        extra.append(exe)
    sums = os.path.join(out, "SHA256SUMS.txt")
    with open(sums, "w", encoding="utf-8", newline="\n") as f:
        for a in list(assets) + extra:
            f.write("%s  %s\n" % (sha256(a), os.path.basename(a)))
    log("  · SHA256SUMS.txt（%d 条，含工作台 exe：%s）" % (len(assets) + len(extra), bool(extra)))
    # 6) version.json（2026-09-22 加）：工作台「检查更新」读的就是它——**版本号 + exe 的 sha256**。
    #    同一份也写回 tools/_version.json，打包时被 spec 打进 exe → 「我这版是谁」与
    #    「远端这版是谁」是同一份结构，永远不会对不上。放在 SHA256SUMS 之后，好把清单哈希也带上。
    vp = os.path.join(out, "version.json")
    try:
        import 版本 as ver
        sums_map = {}
        with open(sums, encoding="utf-8") as f:
            for line in f:
                p = line.split()
                if len(p) == 2:
                    sums_map[p[1].lstrip("*")] = p[0]
        local = ver.write_local(version=version, notes=notes)
        d = dict(local)
        if os.path.isfile(exe):                     # 两份元数据相互校验用：清单里也有一份 exe 哈希
            d["sumsExeSha256"] = sums_map.get(os.path.basename(exe), "")
        with open(vp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        assets.append(vp)
        log("  · version.json（版本 %s · commit %s · exe %s…）"
            % (d.get("version"), d.get("commit") or "—", (d.get("exe") or {}).get("sha256", "")[:12]))
    except Exception as e:                                       # noqa: BLE001
        log("  [!!] version.json 没生成：%s（工作台就查不到新版了）" % e)
    # 7) 清单（人看的）
    man = os.path.join(out, "MANIFEST.md")
    with open(man, "w", encoding="utf-8", newline="\n") as f:
        f.write("# %s 发行附件\n\n生成时间：%s\n\n" % (tag, time.strftime("%Y-%m-%d %H:%M:%S")))
        f.write("| 附件 | 大小 | 属于 |\n|---|---|---|\n")
        core_names = {a for a, _s, _m in kp.CORE_ASSETS}
        for a in list(assets) + extra:
            base = os.path.basename(a)
            who = "默认安装" if base in core_names else ("工作台(单下)" if base.endswith(".exe") else "能力包")
            f.write("| %s | %.1f MB | %s |\n" % (base, _mb(a), who))
        f.write("\n默认安装 = wheels-core.zip + ffmpeg-win64-gpl-shared.zip（+ score-tool.exe）；"
                "系统 ffmpeg 经 %s 探测够用时可跳过 ffmpeg 那包。\n" % "能力包.ffmpeg_ok()")
    log("  产出：%s" % out)
    total = sum(_mb(a) for a in assets)
    log("  合计 %.1f MB（默认那份约 %.1f MB）"
        % (total, sum(_mb(os.path.join(out, n)) for n in ("wheels-core.zip",
                                                          "ffmpeg-win64-gpl-shared.zip")
                      if os.path.isfile(os.path.join(out, n)))))
    return out


def upload_plan(tag):
    d = os.path.join(HERE, "_release", tag)
    print("\n== 上传计划（外发动作，要用户同意再跑）==")
    print("GitHub（走 API，token 用 `git credential fill` 取，永不外显）：")
    print("  · 新建/更新 tag %s 的发行版，附件从 %s 逐个上传" % (tag, d))
    print("  · 用到的接口：POST /repos/{owner}/{repo}/releases  →  uploads.github.com/…/assets")
    print("Gitee（单附件 ≤100MB、单仓附件总量 ≤1GB——本套每件都 <100MB，无需分卷）：")
    print("  · POST https://gitee.com/api/v5/repos/{owner}/{repo}/releases（tag_name=%s）" % tag)
    print("  · POST https://gitee.com/api/v5/repos/{owner}/{repo}/releases/{id}/attach_files")
    print("  · 上传后必须验一次**匿名直链**能不能下（这是新机实际走的路）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default=TAG_DEFAULT)
    ap.add_argument("--upload-plan", action="store_true")
    ap.add_argument("--notes", default="", help="写进 version.json 的更新说明（默认取 CHANGELOG 顶部）")
    ap.add_argument("--version", default="", help="工作台版本号（如 0.2；不给就按 skill_version 的老口径）")
    a = ap.parse_args()
    if not a.upload_plan:
        build(a.tag, notes=a.notes, version=a.version)
    upload_plan(a.tag)
    return 0


if __name__ == "__main__":
    sys.exit(main())
