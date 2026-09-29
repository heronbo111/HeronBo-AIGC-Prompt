# -*- coding: utf-8 -*-
"""
wb_video.py —— 工作台「让豆包生成视频」后端（2026-09-29）。

全程在豆包工作里展开：经 CDP 跑 doubao_cdp.mjs video，画面用豆包内置 Seedance、
配音用豆包内置声音工具，扣的是豆包账号（企业套餐）点数；本地 ffmpeg 合成不耗额度。

对外（server 薄路由调用）：
  resolve_inputs(pdir)                 解析「平台上传」夹首帧/音色/提示词正文
  api_estimate(pdir, b)                规则17 默认估时
  api_prepare(pdir, b)                 后台 prepare（只读、不生成）
  api_confirm(pdir, b)                 后台 confirm（真生成、扣点）
  api_cancel(pdir, b)                  关会话
  snapshot()                           任务状态（前端轮询）
"""
import os
import sys
import re
import json
import math
import time
import subprocess
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    import agent_bridge as ab
except Exception:  # noqa: BLE001
    ab = None
try:
    import project_core as pcore_mod
except Exception:  # noqa: BLE001
    pcore_mod = None

MODEL_LIMIT = {"Seedance 2.5": 30, "Seedance 2.0": 15, "Seedance 2.0 Fast": 15}
DEFAULT_MODEL = "Seedance 2.0 Fast（720p）"
DEFAULT_RATIO = "9:16"

_LOCK = threading.Lock()
_STATE = {
    "running": False, "stage": "", "sid": "", "confirmText": "", "events": [],
    "params": {}, "result": None, "video": "", "error": "", "estimate": None,
    "started": 0, "finished": 0,
}


# ---------------- 状态 ----------------
def _log(msg):
    s = str(msg).strip()
    if not s:
        return
    with _LOCK:
        _STATE["events"].append({"t": round(time.time(), 1), "msg": s})
        if len(_STATE["events"]) > 400:
            del _STATE["events"][:-400]


def _set(**kw):
    with _LOCK:
        _STATE.update(kw)


def snapshot():
    with _LOCK:
        return json.loads(json.dumps(_STATE))


def is_busy():
    with _LOCK:
        return _STATE["running"]


def _reset(stage, params):
    with _LOCK:
        _STATE.update({
            "running": True, "stage": stage, "sid": "", "confirmText": "",
            "events": [], "params": params or {}, "result": None, "video": "",
            "error": "", "estimate": params.get("estimate") if params else None,
            "started": round(time.time(), 1), "finished": 0})


# ---------------- 规则17：估时 ----------------
def num_syllables(s):
    """数字串按中文读法折音节（促销/分数逐位读；整百/整千按数值读）。"""
    if not s.isdigit():
        return len(s)
    v = int(s)
    if v <= 9:
        return 1
    if v < 100:
        tens, ones = divmod(v, 10)
        ln = 1 if tens == 1 else 2
        if ones:
            ln += 1
        return ln
    if v % 1000 == 0 and v <= 99999:
        return 2
    if v % 100 == 0 and v <= 9999:
        return 2
    return len(s)


def count_units(text):
    han = len(re.findall(r"[\u4e00-\u9fff]", text or ""))
    dig = sum(num_syllables(s) for s in re.findall(r"\d+", text or ""))
    return han + dig, han, dig


def estimate(script, mode="sell"):
    units, han, dig = count_units(script)
    if mode == "sell":
        lo, hi, rate = units / 6.0, units / 5.5, "5.5-6"
    else:
        lo, hi, rate = units / 4.8, units / 4.5, "4.5-4.8"
    return {"units": units, "han": han, "digit": dig, "lo": round(lo, 1),
            "hi": round(hi, 1), "suggest": math.ceil(hi), "rate": rate, "mode": mode}


def model_limit(model):
    for k, v in MODEL_LIMIT.items():
        if k in (model or ""):
            return v
    return None


# ---------------- 素材解析 ----------------
def _current_ver_no(pdir):
    try:
        sk = json.load(open(os.path.join(pdir, "框架.json"), encoding="utf-8-sig"))
        cur = ((sk.get("project") or {}).get("current") or "")
        m = re.search(r"(?:v|ver|version|版本)\s*(\d+)", cur, re.I)
        return m.group(1) if m else ""
    except Exception:  # noqa: BLE001
        return ""


def _prompt_file(pdir):
    for cand in ("提示词正文.txt", os.path.join("文案", "提示词正文.txt"),
                 "提示词.txt", os.path.join("文案", "提示词.txt")):
        fp = os.path.join(pdir, cand)
        if os.path.isfile(fp):
            return fp
    return ""


def _speech_text(pdir):
    """提取口播台词（估时只用台词，不能用整段提示词）：优先 备注/需求.txt 的"台词[改成]："后内容。"""
    for cand in (os.path.join("备注", "需求.txt"), "需求.txt"):
        fp = os.path.join(pdir, cand)
        if os.path.isfile(fp):
            try:
                for line in open(fp, encoding="utf-8-sig"):
                    m = re.search(r"台词(?:改成)?[：:]\s*(.+)", line)
                    if m:
                        t = m.group(1).strip()
                        if t:
                            return t
            except OSError:
                pass
    return ""


def _pick_in_dir(d):
    image = audio = ""
    for root, _ds, fs in os.walk(d):
        for f in fs:
            lf = f.lower()
            if not image and lf.endswith((".png", ".jpg", ".jpeg", ".webp")) and (
                    "形象" in f or "首帧" in f or re.match(r"图片1", f)):
                image = os.path.join(root, f)
            if not audio and lf.endswith((".mp3", ".wav", ".m4a")) and (
                    "音色" in f or re.match(r"音频1", f)):
                audio = os.path.join(root, f)
    if not image or not audio:  # 兜底任意 png/mp3
        for root, _ds, fs in os.walk(d):
            for f in sorted(fs):
                lf = f.lower()
                if not image and lf.endswith((".png", ".jpg", ".jpeg", ".webp")):
                    image = os.path.join(root, f)
                if not audio and lf.endswith((".mp3", ".wav", ".m4a")):
                    audio = os.path.join(root, f)
    return image, audio


def resolve_inputs(pdir):
    """解析当前版首帧/音色/提示词；form 图生/文生按有无形象图判定。"""
    out = {"image": "", "audio": "", "promptPath": "", "speech": "", "form": "文生"}
    if not (pdir and os.path.isdir(pdir)):
        return out
    pf = _prompt_file(pdir)
    if pf:
        out["promptPath"] = pf
    out["speech"] = _speech_text(pdir)
    up = pcore_mod.upload_dir(pdir) if pcore_mod else os.path.join(pdir, "平台上传")
    dirs = []
    if os.path.isdir(up):
        dirs.append(up)
        for fn in os.listdir(up):
            d = os.path.join(up, fn)
            if os.path.isdir(d):
                dirs.append(d)
    cur_no = _current_ver_no(pdir)
    best = None  # (score, mtime, image, audio)
    for d in dirs:
        image, audio = _pick_in_dir(d)
        score = (1 if image else 0) + (1 if audio else 0)
        dno = re.search(r"(?:v|版本)\s*(\d+)", os.path.basename(d), re.I)
        if cur_no and dno and dno.group(1) == cur_no:
            score += 3
        mts = []
        for r, _ds, fs in os.walk(d):
            for f in fs:
                try:
                    mts.append(os.path.getmtime(os.path.join(r, f)))
                except OSError:
                    pass
        mt = max(mts or [0])
        cand = (score, mt)
        if best is None or cand > (best[0], best[1]):
            best = (score, mt, image, audio)
    if best:
        out["image"], out["audio"] = best[2], best[3]
    out["form"] = "图生" if out["image"] else "文生"
    return out


# ---------------- CDP 子进程 ----------------
def _run_video(args):
    if not ab:
        raise RuntimeError("缺 agent_bridge.py")
    js = ab.doubao_js()
    runner, renv = ab.lib_runner(js) if js else (None, None)
    if not (js and runner):
        raise RuntimeError("豆包桥不可用：缺 doubao_cdp.mjs 或 node")
    cmd = [runner, js, "video"] + [str(a) for a in args]
    env = ab.child_env()
    env.update(renv or {})
    env.update(ab.runtime_env_for(cmd))
    p = subprocess.Popen(cmd, cwd=_HERE, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                        encoding="utf-8", errors="replace", bufsize=1,
                        **ab.no_window_kwargs())

    def _drain_err():
        try:
            for line in p.stderr:
                if line.strip():
                    _log(line.rstrip())
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=_drain_err, daemon=True).start()
    parts = []
    for line in p.stdout:
        if line.strip():
            parts.append(line)
    try:
        p.wait(timeout=30)
    except Exception:  # noqa: BLE001
        pass
    rc = p.returncode
    raw = "".join(parts).strip()
    obj = _extract_json(raw)
    if obj is not None:
        obj["returncode"] = rc
        return obj
    if rc != 0:
        raise RuntimeError("豆包桥返回码 %s%s" % (rc, (("：" + raw[-200:]) if raw else "（看动作流）")))
    return {"ok": False, "returncode": rc, "raw": raw}


def _extract_json(raw):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        pass
    i = raw.find("{")
    if i < 0:
        return None
    try:
        obj, _e = json.JSONDecoder().raw_decode(raw[i:])
        return obj
    except Exception:  # noqa: BLE001
        return None


# ---------------- 后台线程 ----------------
def start_prepare(p):
    if is_busy():
        return {"ok": False, "error": "已有视频任务在跑，等它结束"}
    p = dict(p or {})
    _reset("prepare", p)

    def _job():
        try:
            args = ["--stage", "prepare", "--model", p.get("model") or DEFAULT_MODEL,
                    "--ratio", p.get("ratio") or DEFAULT_RATIO,
                    "--duration", int(p.get("duration") or 15),
                    "--prompt-file", p.get("promptPath"),
                    "--timeout", int(p.get("timeout") or 500)]
            if p.get("image"):
                args += ["--image", p["image"]]
            if p.get("audio"):
                args += ["--audio", p["audio"]]
            r = _run_video(args)
            if r.get("state") == "await_confirm" and r.get("confirmText"):
                _set(running=False, stage="await_confirm", sid=r.get("sid", ""),
                     confirmText=r.get("confirmText", ""), result=r,
                     finished=round(time.time(), 1))
            else:
                _set(running=False, stage="error",
                     error=r.get("error") or "prepare 没拿到确认清单", result=r,
                     finished=round(time.time(), 1))
        except Exception as e:  # noqa: BLE001
            _set(running=False, stage="error", error=str(e), finished=round(time.time(), 1))

    threading.Thread(target=_job, daemon=True).start()
    return {"ok": True, "stage": "prepare"}


def start_confirm(p):
    if is_busy():
        return {"ok": False, "error": "已有视频任务在跑，等它结束"}
    p = dict(p or {})
    if not p.get("sid"):
        return {"ok": False, "error": "confirm 缺 sid"}
    _reset("confirm", p)
    _set(sid=p["sid"])

    def _job():
        try:
            args = ["--stage", "confirm", "--sid", p["sid"],
                    "--ratio", p.get("ratio") or DEFAULT_RATIO,
                    "--duration", int(p.get("duration") or 15),
                    "--timeout", int(p.get("timeout") or 1500)]
            if p.get("outPath"):
                args += ["--out", p["outPath"]]
            r = _run_video(args)
            if r.get("state") == "done" and r.get("video"):
                _set(running=False, stage="done", video=r.get("video"), result=r,
                     finished=round(time.time(), 1))
            else:
                _set(running=False, stage="error",
                     error=r.get("error") or "confirm 没拿到成片（可能超时）", result=r,
                     finished=round(time.time(), 1))
        except Exception as e:  # noqa: BLE001
            _set(running=False, stage="error", error=str(e), finished=round(time.time(), 1))

    threading.Thread(target=_job, daemon=True).start()
    return {"ok": True, "stage": "confirm"}


def cancel_now(sid):
    try:
        r = _run_video(["--stage", "cancel", "--sid", sid, "--timeout", 30])
    except Exception as e:  # noqa: BLE001
        r = {"ok": False, "error": str(e)}
    _set(running=False, stage="cancelled", error="", finished=round(time.time(), 1))
    return {"ok": True, "state": "cancelled", "sid": sid, "result": r}


# ---------------- 高层 API（server 薄路由调用） ----------------
def api_estimate(pdir, b):
    b = b or {}
    speech = b.get("script") or resolve_inputs(pdir).get("speech") or ""
    mode = "sell" if b.get("mode", "sell") == "sell" else "normal"
    return {"ok": True, "estimate": estimate(speech, mode)}


def api_prepare(pdir, b):
    b = b or {}
    inp = resolve_inputs(pdir)
    model = b.get("model") or DEFAULT_MODEL
    ratio = b.get("ratio") or DEFAULT_RATIO
    est = estimate(inp.get("speech") or "", "sell")
    duration = int(b["duration"]) if b.get("duration") else min(
        est["suggest"], model_limit(model) or 15)
    return start_prepare({
        "project": pdir, "image": inp.get("image") or "", "audio": inp.get("audio") or "",
        "promptPath": inp.get("promptPath"), "model": model, "ratio": ratio,
        "duration": duration, "form": inp.get("form"), "estimate": est,
        "timeout": int(b.get("timeout") or 500)})


def api_confirm(pdir, b):
    b = b or {}
    st = snapshot()
    sid = b.get("sid") or st.get("sid")
    if not sid:
        return {"ok": False, "error": "缺 sid（先 prepare）"}
    params = st.get("params") or {}
    ratio = b.get("ratio") or params.get("ratio") or DEFAULT_RATIO
    duration = int(b.get("duration") or params.get("duration") or 15)
    out = os.path.join(pdir, "成片", "heronbo_%s.mp4" % sid)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    return start_confirm({"project": pdir, "sid": sid, "ratio": ratio,
                          "duration": duration, "outPath": out,
                          "timeout": int(b.get("timeout") or 1500)})


def api_cancel(pdir, b):
    b = b or {}
    sid = b.get("sid") or snapshot().get("sid")
    if not sid:
        return {"ok": False, "error": "缺 sid"}
    return cancel_now(sid)
