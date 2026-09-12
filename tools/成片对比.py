# -*- coding: utf-8 -*-
"""成片对比：一条二创 vs 一条原片，判断「声音怎么处理的」和「画面是否同源同时间轴」。

设计（2026-09-12 v1.0，配合 `references/parody-teardown.md`）：
- **为什么**：拆解二创时最难判的两件事——①声音是原声照搬、还是保伴奏换人声、还是整曲重制；②画面是不是同一时间轴
  （同编舞/同剪辑，只换人物）。肉眼和听感都不够可靠，用信号比对给证据。
- **怎么判**：
  · **画面**：抽帧签名序列（内容区裁黑边）做**滞后搜索**——最佳相关出现在滞后≈0 且相关>0.25 → 同时间轴；相关接近 0 → 无关。
  · **声音**：分**人声段**与**伴奏段**分别做波形互相关（±0.6s 搜索），再用**色度（和声）序列**做滞后搜索：
    - 人声与伴奏都高相关（>0.85）→ **原声照搬**；
    - 伴奏高相关、人声不相关（<0.4）→ **保伴奏换人声**；
    - 两者都不相关、色度也低（<0.6）→ **整曲重制**（可能换了翻唱版本）。
- **输出**：控制台结论 + 可选 `-o` 写 Markdown 片段，可直接贴进拆解报告。

用法：
    python 成片对比.py -a "<二创.mp4>" -b "<原片.mp4>"
    python 成片对比.py -a a.mp4 -b b.mp4 -o "对比结论.md"
    python 成片对比.py --check          # 自检：合成 8s 片 + 从 1s 截出的副本（滞后应≈1s、判定=原声照搬）

依赖：ffmpeg/ffprobe 在 PATH；opencv-python + numpy。只做本地分析：不上传、不提交、不消耗积分。
"""
import argparse
import os
import shutil
import subprocess
import sys
import wave

SR = 8000

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
    out = subprocess.run([ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
                         capture_output=True, text=True, encoding="utf-8")
    import json
    d = json.loads(out.stdout or "{}")
    v = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), {})
    try:
        fr = (v.get("avg_frame_rate") or "30/1").split("/")
        fps = float(fr[0]) / float(fr[1]) if float(fr[1]) else 30.0
    except Exception:
        fps = 30.0
    try:
        dur = float(d.get("format", {}).get("duration") or 0)
    except Exception:
        dur = 0.0
    return {"w": int(v.get("width") or 0), "h": int(v.get("height") or 0), "fps": fps, "duration": dur,
            "has_audio": any(s.get("codec_type") == "audio" for s in d.get("streams", []))}


def content_bbox(path, n_probe=8):
    """自动去掉左右黑边（部分成片把内容嵌在更宽的画布里）。"""
    import cv2
    import numpy as np
    cap = cv2.VideoCapture(path)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cols = []
    for k in range(n_probe):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * (k + 0.5) / n_probe))
        ok, f = cap.read()
        if ok:
            cols.append(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).mean(axis=0))
    cap.release()
    if not cols:
        return 0, None
    cm = np.mean(cols, axis=0)
    l = 0
    while l < len(cm) and cm[l] < 6:
        l += 1
    r = len(cm) - 1
    while r > 0 and cm[r] < 6:
        r -= 1
    return l, r


def sigs(path, fps=4.0, n0=None, n1=None, size=(48, 27)):
    import cv2
    import numpy as np
    cap = cv2.VideoCapture(path)
    f = cap.get(cv2.CAP_PROP_FPS) or 30
    step = max(1, int(round(f / fps)))
    out, i = [], 0
    while True:
        ok, fr = cap.read()
        if not ok:
            break
        if i % step == 0:
            if n0 is not None:
                fr = fr[:, n0:n1]
            g = cv2.cvtColor(cv2.resize(fr, size), cv2.COLOR_BGR2GRAY).astype(np.float32)
            out.append(((g - g.mean()) / (g.std() + 1e-6)).ravel())
        i += 1
    cap.release()
    return np.array(out)


def visual_align(a, b, max_lag_frames=12, step_s=0.25):
    """画面序列滞后搜索：返回 (最佳相关, 最佳滞后秒, 同刻相关)"""
    import numpy as np
    A = sigs(a)
    b0, b1 = content_bbox(b)
    B = sigs(b, n0=b0, n1=b1)
    a0, a1 = content_bbox(a)
    if a0 or (a1 is not None and a1 < probe(a)["w"] - 1):
        A = sigs(a, n0=a0, n1=a1)
    if len(A) < 8 or len(B) < 8:
        return None
    C = (A @ B.T) / A.shape[1]
    best = (-2.0, 0)
    for L in range(-max_lag_frames, max_lag_frames + 1):
        ii = np.arange(max(0, -L), min(len(A), len(B) - L))
        if len(ii) < 8:
            continue
        c = float(np.mean(C[ii, ii + L]))
        if c > best[0]:
            best = (c, L)
    n = min(len(A), len(B))
    same = float(np.mean(np.diag(C)[:n]))
    return best[0], best[1] * step_s, same


_WAV_N = [0]


def wav(path, tmp, sec=None):
    ffmpeg = need("ffmpeg")
    _WAV_N[0] += 1
    out = os.path.join(tmp, "cmp_%d.wav" % _WAV_N[0])
    cmd = [ffmpeg, "-y", "-v", "error"]
    if sec:
        cmd += ["-t", str(sec)]
    cmd += ["-i", path, "-vn", "-ac", "1", "-ar", str(SR), "-f", "wav", out]
    subprocess.run(cmd, check=True, capture_output=True)
    with wave.open(out, "rb") as w:
        import numpy as np
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0


def wave_corr(a, b):
    """±0.6s 内最佳归一化互相关"""
    import numpy as np
    best = (-2.0, 0.0)
    for lag in np.arange(-0.6, 0.61, 0.02):
        L = int(lag * SR)
        if L >= 0:
            x, y = a[L:], b[:len(a) - L]
        else:
            x, y = a[:len(a) + L], b[-L:]
        n = min(len(x), len(y))
        if n < SR:
            continue
        x = x[:n] - x[:n].mean()
        y = y[:n] - y[:n].mean()
        c = float((x * y).sum() / (np.sqrt((x * x).sum() * (y * y).sum()) + 1e-9))
        if c > best[0]:
            best = (c, lag)
    return best


def chroma(x, hop=0.1):
    import numpy as np
    H, S = int(0.25 * SR), int(hop * SR)
    freqs = np.fft.rfftfreq(H * 2, 1 / SR)
    mask = (freqs >= 80) & (freqs <= 2500)
    pc = (np.round(12 * np.log2(np.maximum(freqs[mask], 1e-6) / 440.0)).astype(int)) % 12
    out = []
    for i in range(0, len(x) - H, S):
        seg = x[i:i + H] * np.hanning(H)
        mag = np.abs(np.fft.rfft(seg, n=H * 2))[mask]
        v = np.zeros(12)
        for k in range(12):
            v[k] = mag[pc == k].sum()
        out.append(v / (v.sum() + 1e-9))
    return np.array(out)


def chroma_lag(a, b, max_frames=80):
    import numpy as np
    A, B = chroma(a), chroma(b)
    best = (-2.0, 0)
    for L in range(-max_frames, max_frames + 1):
        if L >= 0:
            x, y = A[L:], B[:len(A) - L]
        else:
            x, y = A[:len(A) + L], B[-L:]
        n = min(len(x), len(y))
        if n < 30:
            continue
        x = x[:n].ravel() - x[:n].ravel().mean()
        y = y[:n].ravel() - y[:n].ravel().mean()
        c = float((x * y).sum() / (np.sqrt((x * x).sum() * (y * y).sum()) + 1e-9))
        if c > best[0]:
            best = (c, L * 0.1)
    return best


def compare(a_path, b_path, tmp):
    import numpy as np
    ia, ib = probe(a_path), probe(b_path)
    lines = []
    lines.append("# 成片对比 · %s（二创） vs %s（原片）" % (os.path.basename(a_path), os.path.basename(b_path)))
    lines.append("")
    lines.append("- 二创：%.2fs / %d×%d；原片：%.2fs / %d×%d" % (
        ia["duration"], ia["w"], ia["h"], ib["duration"], ib["w"], ib["h"]))

    vis = visual_align(a_path, b_path)
    if vis:
        c, lag, same = vis
        if c > 0.25:
            verdict = "同时间轴（同编舞/同剪辑）" if abs(lag) <= 0.75 else "同源、整体偏移 %+.2fs（片头片尾裁切过）" % lag
        elif c > 0.15:
            verdict = "弱相关（可能部分同源）"
        else:
            verdict = "不同源"
        lines.append("- **画面**：序列相关 %.2f（同刻 %.2f），最佳滞后 %+.2fs → **%s**" % (c, same, lag, verdict))
    else:
        lines.append("- **画面**：帧数不足，未比对")

    if not (ia["has_audio"] and ib["has_audio"]):
        lines.append("- **声音**：一侧无音轨，跳过音频比对")
        return "\n".join(lines) + "\n"

    A, B = wav(a_path, tmp), wav(b_path, tmp)
    n = min(len(A), len(B))
    dur = n / SR
    seg_v = (int(2 * SR), int(min(6.0, dur - 0.5) * SR))
    seg_i = (int(max(0, dur - 8) * SR), int(max(0, dur - 4) * SR))
    cv, lv = wave_corr(A[seg_v[0]:seg_v[1]], B[seg_v[0]:seg_v[1]]) if seg_v[1] > seg_v[0] + SR else (-2, 0)
    ci, li = wave_corr(A[seg_i[0]:seg_i[1]], B[seg_i[0]:seg_i[1]]) if seg_i[1] > seg_i[0] + SR else (-2, 0)
    cc, lc = chroma_lag(A[:n], B[:n])
    if cv > 0.85 and ci > 0.85:
        route = "原声照搬（人声+伴奏均同源）"
    elif ci > 0.7 and cv < 0.4:
        route = "保伴奏、换人声（伴奏同源、人声不同）"
    elif cc < 0.6:
        route = "整曲重制（和声序列也不同源）"
    else:
        route = "需人工判断（证据不典型）"
    lines.append("- **声音**：人声段(2–6s) 相关 %.2f；伴奏段(尾段) 相关 %.2f；色度相关 %.2f（滞后 %+.1fs）→ **%s**" % (
        max(cv, 0), max(ci, 0), max(cc, 0), lc, route))
    lines.append("")
    lines.append("> 下一步：声音路线→ rules 第35条（三选一：原声照搬/保伴奏换人声/整曲重制）；")
    lines.append("> 画面同时间轴→ 按原片剪辑点切段逐段重绘（rules 第34/36条），切点表见 `tools\\成片拆解.py`。")
    return "\n".join(lines) + "\n"


def do_check(tmp):
    ffmpeg = need("ffmpeg")
    a = os.path.join(tmp, "cmp_check_a.mp4")
    b = os.path.join(tmp, "cmp_check_b.mp4")
    subprocess.run([ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x568:rate=30:duration=8",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=8", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", a], check=True)
    subprocess.run([ffmpeg, "-y", "-v", "error", "-ss", "1", "-i", a, "-c", "copy", b], check=True)
    out = compare(a, b, tmp)
    print(out)
    ok = ("滞后 -1" in out or "滞后 -1.0" in out) and "原声照搬" in out
    print("[自检] %s：期望「最佳滞后 ≈ -1.0s」且「原声照搬」" % ("通过" if ok else "未通过"))
    sys.exit(0 if ok else 1)


def main():
    ap = argparse.ArgumentParser(description="成片对比：声音路线判定 + 画面时间轴对齐（拆解用）")
    ap.add_argument("-a", "--parody", help="二创成片")
    ap.add_argument("-b", "--original", help="原片")
    ap.add_argument("-o", "--out", help="把结论写到 markdown 文件（可选）")
    ap.add_argument("--check", action="store_true", help="自检：合成测试片并比对")
    args = ap.parse_args()
    tmp = os.environ.get("TEMP") or "/tmp"
    if args.check:
        do_check(tmp)
        return
    if not args.parody or not args.original:
        ap.error("给 -a 二创与 -b 原片，或 --check 自检")
    report = compare(args.parody, args.original, tmp)
    print(report)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(report)
        print("已写入：%s" % args.out)


if __name__ == "__main__":
    main()
