# -*- coding: utf-8 -*-
"""wb_prep.py —— 识别前的确定性加工，本地并行做掉。

2026-11-15 用户实测：一次「素材与需求识别」跑了 8 分 56 秒，而同样这几件活
（探测 / 抽帧拼图 / 裁前 15 秒 / 图片裁白边缩图）用 ffmpeg 并行跑只要 16.3 秒。
慢的不是算力，是「每件事都要在会话里走一轮思考 + 工具往返」。

分工原则：
  · 输入输出完全确定、不需要判断的 → 这里做（ffmpeg 本机跑，线程池并发）。
  · 要决定"这段留不留、这张裁哪边、这版叫什么"的 → 仍归 agent。
所以产物一律落在 `_会话/预加工/`（临时工作区，不是上传件），**命名与引用编号
仍由 agent 定**（`图片1_角色_名称.png` 这类语义不能在这里瞎猜，否则 refmap 会错）。
prompt 里会告诉 agent「已经备好，直接搬过去改名，别再跑一遍 ffmpeg」。
"""
import os
import re
import json
import time
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
VIDEO_EXT = (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v")
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".bmp")
PREP_DIR = os.path.join("_会话", "预加工")
CUT_SECONDS = 15          # 2026-11-15：5 秒放宽到 15 秒（seedance 2.0 fast 输入上限）
GRID_FRAMES = 4
SHRINK_MAX = 2000         # 图片长边上限（2K 参考图够用，再大只是拖慢上传）

# 主程序启动时已经把随包工具的绝对路径解析好了（_tool_paths），这里直接用，
# 免得自己再猜一遍目录，也免得用户机器上没装 ffmpeg 时退化成"加工不了"。
_FIXED = {}


def set_tools(d):
    global _FIXED
    _FIXED = dict(d or {})


def _bin(name):
    """找 ffmpeg / ffprobe：主程序给的优先，其次是随包 _vendor，最后才是 PATH。"""
    p = _FIXED.get(name) or ""
    if p and os.path.isfile(p):
        return p
    cands = [
        os.path.join(HERE, "_vendor", "ffmpeg", "bin", name + ".exe"),
        os.path.join(HERE, "..", "..", "skill", "tools", "_vendor", "ffmpeg", "bin", name + ".exe"),
        os.path.join(os.path.dirname(HERE), "_vendor", "ffmpeg", "bin", name + ".exe"),
    ]
    for c in cands:
        if os.path.isfile(os.path.abspath(c)):
            return os.path.abspath(c)
    return shutil.which(name) or ""


KW = {"capture_output": True, "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}


def probe(path):
    """时长 / 宽高 / 有无音轨。ffprobe 的输出用 json 读，别去正则文本。"""
    fp = _bin("ffprobe")
    if not fp or not os.path.isfile(path):
        return {}
    try:
        r = subprocess.run([fp, "-v", "error", "-show_entries",
                            "format=duration:stream=codec_type,width,height",
                            "-of", "json", path], timeout=30, **KW)
        d = json.loads(r.stdout.decode("utf-8", "ignore") or "{}")
    except Exception:                                            # noqa: BLE001
        return {}
    out = {"duration": 0.0, "width": 0, "height": 0, "audio": False}
    try:
        out["duration"] = float((d.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        pass
    for s in (d.get("streams") or []):
        if s.get("codec_type") == "audio":
            out["audio"] = True
        elif s.get("codec_type") == "video" and not out["width"]:
            out["width"] = int(s.get("width") or 0)
            out["height"] = int(s.get("height") or 0)
    return out


def _safe(name):
    s = re.sub(r"[\\/:*?\"<>|]", "_", str(name or ""))
    return s.strip() or "item"


def cut_video(src, out, seconds=CUT_SECONDS):
    """截前 N 秒（转码，保证平台侧能直接读）。原声保留。"""
    ff = _bin("ffmpeg")
    if not ff:
        return ""
    try:
        subprocess.run([ff, "-y", "-i", src, "-t", str(seconds),
                        "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
                        "-c:a", "aac", "-movflags", "+faststart", out], timeout=600, **KW)
    except Exception:                                            # noqa: BLE001
        return ""
    return out if os.path.isfile(out) and os.path.getsize(out) > 0 else ""


def grid_video(src, out, seconds=CUT_SECONDS, frames=GRID_FRAMES):
    """前 N 秒均匀抽 frames 帧横排成一张联系表：一次看完整段，模型直接读图。"""
    ff = _bin("ffmpeg")
    if not ff:
        return ""
    try:
        subprocess.run([ff, "-y", "-t", str(seconds), "-i", src,
                        "-vf", "fps=%d/%d,scale=520:-1,tile=%dx1" % (frames, seconds, frames),
                        "-frames:v", "1", out], timeout=300, **KW)
    except Exception:                                            # noqa: BLE001
        return ""
    return out if os.path.isfile(out) and os.path.getsize(out) > 0 else ""


def shrink_image(src, out, max_side=SHRINK_MAX):
    """长边超过 max_side 就等比缩下来；不需要就返回空（别做无意义的转码）。"""
    info = probe(src)
    long_side = max(int(info.get("width") or 0), int(info.get("height") or 0))
    if long_side and long_side <= max_side:
        return ""
    ff = _bin("ffmpeg")
    if not ff:
        return ""
    try:
        subprocess.run([ff, "-y", "-i", src, "-vf", "scale='if(gt(iw,ih),%d,-1)':'if(gt(iw,ih),-1,%d)'"
                        % (max_side, max_side), out], timeout=180, **KW)
    except Exception:                                            # noqa: BLE001
        return ""
    return out if os.path.isfile(out) and os.path.getsize(out) > 0 else ""


def run(pdir, on_log=None, workers=4):
    """对 `素材/` 下的原件并行做完确定性加工，返回清单（也会写 json 落盘）。

    返回：{"items":[{kind, src, out, note, seconds}], "seconds": 总耗时, "tmp": 目录}
    """
    log = on_log or (lambda _s: None)
    src_dir = os.path.join(pdir, "素材")
    tmp = os.path.join(pdir, PREP_DIR)
    items = []
    if not os.path.isdir(src_dir):
        return {"items": items, "seconds": 0.0, "tmp": tmp}
    try:
        os.makedirs(tmp, exist_ok=True)
    except OSError:
        return {"items": items, "seconds": 0.0, "tmp": tmp}

    srcs = [os.path.join(src_dir, f) for f in sorted(os.listdir(src_dir))
            if os.path.isfile(os.path.join(src_dir, f))]

    def _one(src):
        ext = os.path.splitext(src)[1].lower()
        stem = _safe(os.path.splitext(os.path.basename(src))[0])
        t0 = time.time()
        rec = []
        if ext in VIDEO_EXT:
            info = probe(src)
            dur = float(info.get("duration") or 0)
            sub = os.path.join(tmp, stem)
            try:
                os.makedirs(sub, exist_ok=True)
            except OSError:
                return rec
            g = grid_video(src, os.path.join(sub, "抽帧拼图_前15秒4帧.png"))
            if g:
                rec.append({"kind": "grid", "src": src, "out": g,
                            "note": "前 %d 秒均匀抽 %d 帧" % (CUT_SECONDS, GRID_FRAMES),
                            "seconds": round(time.time() - t0, 1)})
            if dur > CUT_SECONDS + 0.5:
                c = cut_video(src, os.path.join(sub, "前%d秒.mp4" % CUT_SECONDS))
                if c:
                    rec.append({"kind": "cut", "src": src, "out": c,
                                "note": "%.1fs → 前 %ds（原声保留）" % (dur, CUT_SECONDS),
                                "seconds": round(time.time() - t0, 1)})
            elif dur:
                rec.append({"kind": "probe", "src": src, "out": "",
                            "note": "时长 %.1fs，未超 %ds，不用裁" % (dur, CUT_SECONDS),
                            "seconds": round(time.time() - t0, 1)})
        elif ext in IMAGE_EXT:
            s = shrink_image(src, os.path.join(tmp, stem + "_长边%d.png" % SHRINK_MAX))
            if s:
                rec.append({"kind": "shrink", "src": src, "out": s,
                            "note": "长边缩到 %d" % SHRINK_MAX,
                            "seconds": round(time.time() - t0, 1)})
        return rec

    t0 = time.time()
    try:
        with ThreadPoolExecutor(max_workers=max(1, int(workers))) as ex:
            for rec in ex.map(_one, srcs):
                items.extend(rec or [])
    except Exception as e:                                       # noqa: BLE001
        log("并行加工中断：%s" % e)
    total = round(time.time() - t0, 1)
    try:
        with open(os.path.join(pdir, "_会话", "预加工清单.json"), "w",
                  encoding="utf-8", newline="") as f:
            f.write(json.dumps({"items": items, "seconds": total,
                                "at": time.strftime("%Y-%m-%d %H:%M:%S")},
                               ensure_ascii=False, indent=1))
    except OSError:
        pass
    if items:
        log("本地并行加工 %d 件，用时 %.1f 秒" % (len(items), total))
    return {"items": items, "seconds": total, "tmp": tmp}


def brief(pdir):
    """给 agent 的一句话清单（拼进 prompt）：已经做完的，别再跑一遍 ffmpeg。"""
    p = os.path.join(pdir, "_会话", "预加工清单.json")
    if not os.path.isfile(p):
        return ""
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return ""
    items = d.get("items") or []
    if not items:
        return ""
    lines = ["【预加工件（程序已在本机并行做完，用时 %s 秒）——**直接拿来用，不要再用 ffmpeg 跑一遍**】"
             % d.get("seconds", 0)]
    for it in items:
        kind = it.get("kind")
        if kind == "grid":
            lines.append("- 抽帧拼图：`%s`（%s）——看画面内容就直接读这张图"
                         % (it.get("out"), it.get("note")))
        elif kind == "cut":
            lines.append("- 已裁好的片子：`%s`（%s）——要进 `平台上传/` 只需按引用编号改名搬过去"
                         "（`视频N_参考视频_原片前15秒_15s.mp4`），**别再转码**"
                         % (it.get("out"), it.get("note")))
        elif kind == "shrink":
            lines.append("- 已缩好的图：`%s`（%s）——同上，改名搬进 `平台上传/` 即可"
                         % (it.get("out"), it.get("note")))
        elif kind == "probe":
            lines.append("- %s" % it.get("note"))
    lines.append("这些都在 `%s`（临时工作区）。原件仍在 `素材/` 原地不动。" % PREP_DIR)
    return "\n".join(lines) + "\n\n"
