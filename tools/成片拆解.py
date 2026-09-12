# -*- coding: utf-8 -*-
"""成片拆解：把一条成片/二创视频变成"可判断的机器证据"，供读片三问与拆解报告使用。

设计（2026-09-12 v1.0，对应 rules 第33–37 条与 `references/parody-teardown.md`）：
- **为什么**：用户发来一条 AI 二创/爆款成片要"拆解/仿做"时，肉眼判断（节奏跟不跟音乐、画面是不是多短段、
  口型是不是逐段配）需要证据。本工具**只出证据、不下结论**——结论由 agent 按 rules 第33条「读片三问」给出。
- **出什么**：
  ① 镜头切点表（切点/每段时长/平均亮度/运动量）；
  ② 主体证据（逐采样点人脸数、人脸中心 x%、人脸宽占画面比）→ 每段人脸位移 → "人物与镜头稳不稳"；
  ③ 音轨证据（有无音轨、能量起音数、起音间隔中位数、切点与节拍对齐率）→ "节奏是否跟着音乐走"；
  ④ 可选 `--asr`：faster-whisper 转写 → 判断口播 / 演唱 / 纯音乐。
- **报告**：默认在源片旁写 `<源片>_拆解报告.md`（人读）与 `<源片>_拆解报告.json`（机读）。

用法：
    python 成片拆解.py -i "<成片>"                    # 出报告（分析采样默认 4 帧/秒）
    python 成片拆解.py -i "<成片>" --asr              # 追加转写（需 faster-whisper）
    python 成片拆解.py -i "<成片>" --sample-fps 6     # 提高时间分辨率
    python 成片拆解.py --check                        # 自检：合成 3 段硬切测试片跑全流程

依赖：ffmpeg / ffprobe 在 PATH；opencv-python + numpy；可选 faster-whisper；
人脸用 `识别工具/face_detection_yunet_2023mar.onnx`（缺则跳过人脸列，其余照出）。
只做本地分析：不上传、不提交、不消耗积分。
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
SIG_W, SIG_H = 32, 18          # 帧签名尺寸（算画面差与直方图）
BRIGHT_W, BRIGHT_H = 160, 90   # 取亮度/直方图用的缩放尺寸
SHORT_SHOT = 2.5               # "多短段"判据：平均镜头长 < 2.5s
STABLE_RANGE = 3.0             # "人物稳定"判据：段内人脸中心 x 极差 < 3%

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
    out = subprocess.run(
        [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
        capture_output=True, text=True, encoding="utf-8")
    if out.returncode != 0:
        sys.exit("[错误] ffprobe 读不了这个文件：%s" % path)
    d = json.loads(out.stdout or "{}")
    v = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), {})
    a = next((s for s in d.get("streams", []) if s.get("codec_type") == "audio"), None)
    fr = v.get("avg_frame_rate") or "30/1"
    try:
        n, dn = fr.split("/")
        fps = float(n) / float(dn) if float(dn) else 30.0
    except Exception:
        fps = 30.0
    try:
        dur = float(d.get("format", {}).get("duration") or 0)
    except Exception:
        dur = 0.0
    return {"w": int(v.get("width") or 0), "h": int(v.get("height") or 0), "fps": fps,
            "duration": dur, "has_audio": a is not None,
            "size_mb": round(float(d.get("format", {}).get("size") or 0) / 1048576.0, 2)}


def open_capture(path, tmp):
    """openCV 在少数环境下读不了非 ASCII 路径：直接打开失败时拷到临时目录再试。"""
    import cv2
    cap = cv2.VideoCapture(path)
    if cap.isOpened():
        return cap, path
    dst = os.path.join(tmp, "teardown_src.mp4")
    shutil.copyfile(path, dst)
    cap = cv2.VideoCapture(dst)
    return cap, dst


def face_detector(tmp, w, h):
    """opencv 的 ONNX 读取不支持非 ASCII 路径（本仓库路径含中文），先拷到临时目录再加载。"""
    if not os.path.isfile(YUNET):
        return None
    dst = os.path.join(tmp, "yunet_tool.onnx")
    try:
        if not os.path.isfile(dst) or os.path.getsize(dst) != os.path.getsize(YUNET):
            shutil.copyfile(YUNET, dst)
    except Exception as e:
        print("[提示] 模型拷到临时目录失败（%s），跳过人脸列" % e)
        return None
    import cv2
    det = cv2.FaceDetectorYN.create(dst, "", (w, h), 0.5, 0.3, 5000)
    det.setInputSize((w, h))
    return det


def scan_video(path, tmp, sample_fps, det, fps):
    """按 sample_fps 抽采样帧，出亮度/签名/人脸记录。"""
    import cv2
    import numpy as np
    cap, src = open_capture(path, tmp)
    if not cap.isOpened():
        sys.exit("[错误] opencv 打不开：%s" % path)
    step = max(1, int(round(fps / max(0.5, sample_fps))))
    samples = []
    idx = 0
    t0 = time.time()
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if idx % step == 0:
            small = cv2.resize(frame, (BRIGHT_W, BRIGHT_H))
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            hist = cv2.calcHist([gray], [0], None, [64], [0, 256])
            cv2.normalize(hist, hist)
            rec = {"t": round(idx / fps, 3), "bright": float(gray.mean()),
                   "sig": cv2.resize(gray, (SIG_W, SIG_H)).astype(np.int16), "hist": hist}
            if det is not None:
                _, faces = det.detect(frame)
                fh, fw = frame.shape[:2]
                rec["faces"] = [
                    {"x": round((float(f[0]) + float(f[2]) / 2) / fw, 4),
                     "y": round((float(f[1]) + float(f[3]) / 2) / fh, 4),
                     "w": round(float(f[2]) / fw, 4)}
                    for f in (faces if faces is not None else [])]
            samples.append(rec)
            if len(samples) % 100 == 0:
                print("  采样 %d 帧 %.0fs" % (len(samples), time.time() - t0), flush=True)
        idx += 1
    cap.release()
    if src != path:
        try:
            os.remove(src)
        except Exception:
            pass
    return samples


def change_series(samples):
    """逐采样点的帧间差（diff）与直方图相关（corr）。"""
    import cv2
    import numpy as np
    diffs = [float(np.mean(np.abs(samples[i]["sig"].astype(np.int32) - samples[i + 1]["sig"].astype(np.int32))))
             for i in range(len(samples) - 1)]
    corrs = [float(cv2.compareHist(samples[i]["hist"], samples[i + 1]["hist"], cv2.HISTCMP_CORREL))
             for i in range(len(samples) - 1)]
    return diffs, corrs


def scene_blocks(path, win=0.5, k=4):
    """大场景块检测：0.5s 窗签名 + KMeans 聚类 + 合并相邻同类。
    用途：找"大场景变化的转场"（切段依据）；比 detect_cuts 的镜头级硬切更粗。"""
    import cv2
    import numpy as np
    cap = cv2.VideoCapture(path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    need = max(1, int(round(fps * win)))
    feats, times, acc, i = [], [], [], 0
    while True:
        ok, fr = cap.read()
        if not ok:
            break
        acc.append(cv2.cvtColor(cv2.resize(fr, (32, 18)), cv2.COLOR_BGR2GRAY).astype(np.float32))
        if len(acc) == need:
            feats.append(np.mean(acc, axis=0).ravel())
            times.append(len(times) * win)
            acc = []
        i += 1
    cap.release()
    if len(feats) < k + 1:
        return []
    F = np.array(feats)
    F = (F - F.mean(0)) / (F.std(0) + 1e-6)
    rng = np.random.default_rng(0)
    C = F[rng.choice(len(F), k, replace=False)]
    lab = np.zeros(len(F), dtype=int)
    for _ in range(60):
        lab = ((F[:, None, :] - C[None, :, :]) ** 2).sum(-1).argmin(1)
        for j in range(k):
            if (lab == j).any():
                C[j] = F[lab == j].mean(0)
    blocks, s0 = [], 0
    for t in range(1, len(lab) + 1):
        if t == len(lab) or lab[t] != lab[s0]:
            blocks.append({"start": round(times[s0], 2), "end": round(times[t - 1] + win, 2),
                           "dur": round(times[t - 1] + win - times[s0], 2), "label": int(lab[s0])})
            s0 = t
    return blocks


def detect_cuts(samples, min_shot=0.6):
    """双重判据找硬切点：①直方图相关性塌陷（低于局部中位 -0.18，且 <0.75）；②画面差相对局部中位数突增（>1.7× 且 >18，抗高运动误报）。
    min_shot：最短镜头长度（秒）——相邻采样点连触发时只保留第一个，避免切出 0.00s 伪镜头。"""
    import numpy as np
    if len(samples) < 3:
        return []
    diffs, corrs = change_series(samples)
    n = len(diffs)
    cuts = []
    for i in range(n):
        lo, hi = max(0, i - 10), min(n, i + 10)
        local = float(np.median(diffs[lo:hi])) or 1.0
        local_corr = float(np.median(corrs[lo:hi]))
        jump = diffs[i] > max(18.0, 1.7 * local)
        collapse = corrs[i] < min(0.75, local_corr - 0.18)
        if jump or collapse:
            t = samples[i + 1]["t"]
            prev_t = samples[cuts[-1]]["t"] if cuts else 0.0
            if t - prev_t >= min_shot:
                cuts.append(i + 1)
    return cuts


def top_changes(samples, n=5):
    """变化最大的候选点（可能含硬切/闪黑/大动作），供人工复核；算法没到阈值也列出来。"""
    if len(samples) < 3:
        return []
    diffs, corrs = change_series(samples)
    order = sorted(range(len(diffs)), key=lambda i: -diffs[i])[:n]
    return [{"t": samples[i + 1]["t"], "diff": round(diffs[i], 1), "corr": round(corrs[i], 2)}
            for i in sorted(order)]


def shots_from(samples, cuts):
    bounds = [0] + cuts + [len(samples)]
    shots = []
    for a, b in zip(bounds, bounds[1:]):
        if b <= a:
            continue
        seg = samples[a:b]
        if not seg:
            continue
        bright = sum(s["bright"] for s in seg) / len(seg)
        motion = 0.0
        if len(seg) > 1:
            import numpy as np
            motion = float(np.mean([
                float(np.mean(np.abs(seg[i]["sig"].astype(np.int32) - seg[i + 1]["sig"].astype(np.int32))))
                for i in range(len(seg) - 1)]))
        faces = [f for s in seg for f in s.get("faces", [])]
        fw = sorted(f["w"] for f in faces)
        fx = sorted(f["x"] for f in faces)
        shot = {
            "start": seg[0]["t"],
            "end": round(seg[-1]["t"], 3),
            "dur": round(seg[-1]["t"] - seg[0]["t"], 3),
            "bright": round(bright, 1),
            "motion": round(motion, 2),
            "face_n": len(faces),
            "face_w_median": round(fw[len(fw) // 2], 4) if fw else 0.0,
            "face_x_median": round(fx[len(fx) // 2], 4) if fx else 0.0,
            "face_x_range": round(fx[-1] - fx[0], 4) if fx else 0.0,
        }
        shot["scale"] = guess_scale(shot["face_w_median"]) if faces else "空镜/无正面人脸"
        shots.append(shot)
    return shots


def guess_scale(ratio):
    if ratio >= 0.35:
        return "大特写"
    if ratio >= 0.18:
        return "近景"
    if ratio >= 0.08:
        return "中景"
    return "全景/远景"


def extract_audio(path, tmp):
    ffmpeg = need("ffmpeg")
    wav = os.path.join(tmp, "teardown_audio.wav")
    r = subprocess.run([ffmpeg, "-y", "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", "16000",
                        "-f", "wav", wav], capture_output=True, text=True)
    if r.returncode != 0 or not os.path.isfile(wav) or os.path.getsize(wav) < 2000:
        return None
    return wav


def audio_profile(wav):
    """能量包络 → 起音点（用于估节拍）与响度分布。"""
    import wave
    import numpy as np
    with wave.open(wav, "rb") as w:
        rate, n = w.getframerate(), w.getnframes()
        data = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float32) / 32768.0
    if not len(data):
        return None
    hop, win = int(rate * 0.01), int(rate * 0.05)
    frames = 1 + max(0, (len(data) - win) // hop)
    env = np.array([float(np.sqrt(np.mean(data[i * hop:i * hop + win] ** 2) + 1e-12))
                    for i in range(frames)])
    db = 20 * np.log10(env + 1e-6)
    db_mean = float(db.mean())
    near_silent = db_mean < -55.0
    if near_silent:
        return {"db_mean": round(db_mean, 1), "silence_ratio": 1.0, "near_silent": True,
                "onsets": [], "ioi_median": 0.0, "ioi_std": 0.0}
    nov = np.diff(env)
    nov[nov < 0] = 0
    thr = float(nov.mean() + 1.5 * nov.std())
    onsets = []
    for i in range(1, len(nov) - 1):
        if nov[i] > thr and nov[i] >= nov[i - 1] and nov[i] >= nov[i + 1]:
            t = (i * hop) / rate
            if not onsets or t - onsets[-1] > 0.12:
                onsets.append(round(t, 3))
    ioi = np.diff(onsets) if len(onsets) > 1 else np.array([])
    floor_db = max(-60.0, db_mean - 25.0)
    silence = float(np.mean(db < floor_db))
    return {"db_mean": round(db_mean, 1), "silence_ratio": round(silence, 3), "near_silent": False,
            "onsets": onsets, "ioi_median": round(float(np.median(ioi)), 3) if len(ioi) else 0.0,
            "ioi_std": round(float(np.std(ioi)), 3) if len(ioi) else 0.0}


def transcribe(wav, lang="zh"):
    try:
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        from faster_whisper import WhisperModel
    except Exception:
        print("[提示] 没装 faster-whisper，跳过转写（--asr）")
        return None
    model = WhisperModel("small", device="cpu", compute_type="int8")
    segments, info = model.transcribe(wav, language=lang, vad_filter=True)
    out = [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()} for s in segments]
    return {"lang_prob": round(float(info.language_probability), 2), "segments": out}


def align_rate(cuts, onsets, tol=0.2):
    """切点落在起音点 ±tol 内的比例（高 = 节奏跟着音乐走）。"""
    if not cuts or not onsets:
        return None
    hit = 0
    for t in cuts:
        if any(abs(t - o) <= tol for o in onsets):
            hit += 1
    return round(hit / len(cuts), 3)


def build_report(path, info, samples, shots, aud, asr, cands):
    name = os.path.basename(path)
    cuts = [round(s["start"], 3) for s in shots[1:]] if shots else []
    avg_shot = round(sum(s["dur"] for s in shots) / len(shots), 2) if shots else 0.0
    xr = [s["face_x_range"] for s in shots if s["face_n"]]
    stability = round(sorted(xr)[len(xr) // 2] * 100, 2) if xr else None
    lines = []
    lines.append("# 成片拆解报告 · %s" % name)
    lines.append("")
    lines.append("- 源片：`%s`" % path)
    lines.append("- 规格：%d×%d / %.2f fps / %.2fs / %.2f MB / 音轨：%s" % (
        info["w"], info["h"], info["fps"], info["duration"], info["size_mb"],
        "有" if info["has_audio"] else "无"))
    lines.append("- 采样：分析帧 %d 个；工具：`tools\\成片拆解.py`（只出证据，结论按 rules 第33条「读片三问」由 agent 给）"
                 % len(samples))
    lines.append("")
    lines.append("## 一、镜头表（机器切点）")
    lines.append("")
    lines.append("| # | 起止(s) | 时长 | 平均亮度 | 运动量 | 人脸样本数 | 人脸宽% | 人脸中心x% | 段内x位移% | 景别猜测 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for i, s in enumerate(shots, 1):
        lines.append("| %d | %.2f–%.2f | %.2fs | %.1f | %.2f | %d | %.1f | %.1f | %.1f | %s |" % (
            i, s["start"], s["end"], s["dur"], s["bright"], s["motion"], s["face_n"],
            s["face_w_median"] * 100, s["face_x_median"] * 100, s["face_x_range"] * 100, s["scale"]))
    lines.append("")
    if cands:
        lines.append("### 变化候选点（算法眼中变化最大的 5 个采样点，可能含硬切/闪黑/大动作，需人工复核）")
        lines.append("")
        lines.append("| 时间(s) | 画面差 | 直方图相关 |")
        lines.append("|---|---|---|")
        for c in cands:
            lines.append("| %.2f | %.1f | %.2f |" % (c["t"], c["diff"], c["corr"]))
        lines.append("")
    lines.append("## 二、汇总证据")
    lines.append("")
    lines.append("- 镜头数 **%d**，平均镜头长 **%.2fs**（多短段特征：%s，判据 <%.1fs）" % (
        len(shots), avg_shot, "是" if shots and avg_shot < SHORT_SHOT else "否", SHORT_SHOT))
    if stability is not None:
        lines.append("- 人物稳定度：段内人脸中心 x 极差中位数 **%.2f%%**（%.1f%% 以内视为稳定镜头）" % (
            stability, STABLE_RANGE))
    else:
        lines.append("- 人物稳定度：本片未检出人脸（空镜/无人正面镜头），人脸证据缺")
    if aud:
        if aud.get("near_silent"):
            lines.append("- 音频：**静音轨**（平均 %.1f dB，近似无声）——口播/音乐类需先补声音；替换类可当静音驱动片" % aud["db_mean"])
        else:
            ar = align_rate(cuts, aud["onsets"])
            lines.append("- 音频：平均 %.1f dB，静音占比 %.1f%%；起音点 %d 个，起音间隔中位数 %.2fs（±%.2f）" % (
                aud["db_mean"], aud["silence_ratio"] * 100, len(aud["onsets"]), aud["ioi_median"], aud["ioi_std"]))
            density = len(aud["onsets"]) / max(1e-6, info["duration"])
            caveat = ("（起音点密度 %.1f/s，偏密——对齐率会被抬高，仅作参考）" % density) if density > 1.0 else ""
            lines.append("- 切点与节拍对齐率：%s（±0.2s；高 = 节奏跟着音乐走）%s" % (
                "%.0f%%" % (ar * 100) if ar is not None else "无法计算（缺起音点或切点）", caveat))
    else:
        lines.append("- 音频：**无音轨**（口播/音乐类需先补声音；替换类可作为静音驱动片）")
    if asr:
        lines.append("- 转写（faster-whisper small，语言置信 %.2f）：**%d 段**" % (
            asr["lang_prob"], len(asr["segments"])))
        for s in asr["segments"][:20]:
            lines.append("  - [%6.2f → %6.2f] %s" % (s["start"], s["end"], s["text"]))
        if len(asr["segments"]) > 20:
            lines.append("  - …（其余 %d 段见 json）" % (len(asr["segments"]) - 20))
    lines.append("")
    lines.append("## 三、读片三问的机器证据（结论由 agent 按 rules 第33条给出）")
    lines.append("")
    lines.append("1. **节奏是否跟着音乐走？** 看：平均镜头长 / 切点与节拍对齐率 / 转写是否成句成歌。")
    lines.append("2. **人物与镜头是否前后稳定？** 看：段内人脸中心位移 / 人脸宽是否跳变 / 是否有硬切。")
    lines.append("3. **口型是否逐段配？** 机器不能直接判定：看转写段边界是否与镜头切点同拍，再抽帧复核口型。")
    lines.append("")
    lines.append("## 四、下一步（流程见 `references/parody-teardown.md`）")
    lines.append("")
    lines.append("- 判为音乐驱动 → 声音先行（规则35 一字不改 + 旋律贴合声调），再按原片镜头切 1–2 句短段逐段配口型（规则34）。")
    lines.append("- 拆「保留/替换清单」（规则36：锁机位、构图、背景与台词原句，只换人物表现层）。")
    lines.append("- 光影不贴时走「全景空镜 + 局部重绘」（规则37，待验证）。")
    lines.append("- 复现前按 rules 第7/13条确认版权与交付边界；提交生成由用户完成（本 skill 不代提交）。")
    return "\n".join(lines) + "\n"


def make_test_clip(tmp):
    ffmpeg = need("ffmpeg")
    out = os.path.join(tmp, "teardown_check.mp4")
    cmd = [ffmpeg, "-y", "-v", "error",
           "-f", "lavfi", "-i", "testsrc=size=320x568:rate=30:duration=2",
           "-f", "lavfi", "-i", "smptebars=size=320x568:rate=30:duration=2",
           "-f", "lavfi", "-i", "testsrc2=size=320x568:rate=30:duration=2",
           "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
           "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
           "-map", "[v]", "-map", "3:a", "-c:v", "libx264", "-preset", "veryfast",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", out]
    subprocess.run(cmd, check=True)
    return out


def do_check(tmp):
    print("[自检] 合成 3 段硬切 + 正弦音轨的测试片…")
    clip = make_test_clip(tmp)
    info = probe(clip)
    det = face_detector(tmp, info["w"], info["h"])
    samples = scan_video(clip, tmp, 4.0, det, info["fps"])
    cuts = detect_cuts(samples)
    shots = shots_from(samples, cuts)
    wav = extract_audio(clip, tmp)
    aud = audio_profile(wav) if wav else None
    ok = []
    ok.append(("切点数 2", len(shots) == 3, "检出镜头 %d 个（期望 3）" % len(shots)))
    ok.append(("音轨检出", bool(aud), "音频包络：%s" % ("有" if aud else "无")))
    ok.append(("时长≈6s", abs(info["duration"] - 6.0) < 0.6, "ffprobe 时长 %.2fs" % info["duration"]))
    bad = [x for x in ok if not x[1]]
    for name, good, detail in ok:
        print("  %s %s：%s" % ("✓" if good else "✗", name, detail))
    if bad:
        sys.exit("[自检失败] 见上面 ✗ 项")
    print("[自检通过] 切点/音轨/时长三项全对；报告示例见 %s" % clip)


def main():
    ap = argparse.ArgumentParser(description="成片拆解：出镜头切点/主体稳定度/音轨节拍等机器证据（只出证据，不下结论）")
    ap.add_argument("-i", "--input", help="要拆解的成片/二创视频")
    ap.add_argument("--asr", action="store_true", help="追加 faster-whisper 转写（判断口播/演唱/纯音乐）")
    ap.add_argument("--lang", default="zh", help="转写语言，默认 zh")
    ap.add_argument("--sample-fps", type=float, default=4.0, help="分析采样帧率，默认 4")
    ap.add_argument("--min-shot", type=float, default=0.6, help="最短镜头长度（秒），默认 0.6——抑制相邻点连触发的伪镜头")
    ap.add_argument("-o", "--out-prefix", help="报告输出前缀（默认 <源片>_拆解报告）")
    ap.add_argument("--scene", action="store_true", help="只做「大场景块」检测（切段依据；0.5s 窗聚类）")
    ap.add_argument("--check", action="store_true", help="自检：合成测试片跑全流程")
    args = ap.parse_args()

    tmp = os.environ.get("TEMP") or "/tmp"
    if args.check:
        do_check(tmp)
        return
    if not args.input:
        ap.error("给 -i 指定成片，或 --check 自检")

    if args.scene:
        if not args.input:
            ap.error("--scene 需要 -i 指定视频")
        blocks = scene_blocks(args.input)
        print("大场景块（%d 块）——切段就切在这些转场处：" % len(blocks))
        lines = ["# 大场景块表 · %s" % os.path.basename(args.input), "",
                 "| # | 起止(s) | 时长 |", "|---|---|---|"]
        for bi, b in enumerate(blocks, 1):
            print("  %2d. %6.2f–%6.2f s  (%.2fs)" % (bi, b["start"], b["end"], b["dur"]))
            lines.append("| %d | %.2f–%.2f | %.2fs |" % (bi, b["start"], b["end"], b["dur"]))
        out = (args.out_prefix or os.path.splitext(args.input)[0]) + "_场景块.md"
        with open(out, "w", encoding="utf-8") as f:
            f.write(chr(10).join(lines) + chr(10))
        print("场景块表：%s" % out)
        return
    info = probe(args.input)
    print("源片：%d×%d / %.2ffps / %.2fs / 音轨 %s" % (
        info["w"], info["h"], info["fps"], info["duration"], "有" if info["has_audio"] else "无"))
    det = face_detector(tmp, info["w"], info["h"])
    if det is None:
        print("[提示] 缺 YuNet 模型，人脸列跳过（其余照出）")
    print("抽采样帧（%.1f 帧/秒）…" % args.sample_fps)
    samples = scan_video(args.input, tmp, args.sample_fps, det, info["fps"])
    cuts = detect_cuts(samples, args.min_shot)
    shots = shots_from(samples, cuts)
    cands = top_changes(samples)
    print("镜头 %d 个，切点 %d 处" % (len(shots), len(cuts)))
    aud = None
    asr = None
    wav = extract_audio(args.input, tmp) if info["has_audio"] else None
    if wav:
        aud = audio_profile(wav)
        if args.asr:
            print("转写中（faster-whisper small，可能要 1–2 分钟）…")
            asr = transcribe(wav, args.lang)
    report = build_report(args.input, info, samples, shots, aud, asr, cands)

    prefix = args.out_prefix or (os.path.splitext(args.input)[0] + "_拆解报告")
    md, js = prefix + ".md", prefix + ".json"
    with open(md, "w", encoding="utf-8") as f:
        f.write(report)
    payload = {"source": args.input, "probe": info,
               "shots": shots, "cuts": [round(samples[i]["t"], 3) for i in cuts],
               "candidates": cands, "audio": aud, "asr": asr}
    with open(js, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print("报告：%s\n      %s" % (md, js))


if __name__ == "__main__":
    main()
