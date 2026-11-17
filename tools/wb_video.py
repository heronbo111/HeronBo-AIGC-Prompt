# -*- coding: utf-8 -*-
"""
wb_video.py —— 工作台「让豆包生成视频」后端（2026-09-29）。

全程在豆包工作里展开：经 CDP 跑 doubao_cdp.mjs video，生成用豆包内置 Seedance，
时长在模型单次上限内整条一次生成（口型天然同步）、超上限才分段，扣的是豆包账号
（企业套餐）点数。

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
import shutil
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
try:
    import doubao_tray as _tray     # 豆包窗口收进托盘（2026-10-07，缺了就退化为离屏不藏）
except Exception:  # noqa: BLE001
    _tray = None
try:
    import wb_cli as cli_bridge     # 豆包 CLI 主通道（2026-11-12，CDP 仅兜底）
except Exception:  # noqa: BLE001
    cli_bridge = None

# 2026-11-13 用户要求：默认只走 CLI，启动/准备阶段不再自动碰 CDP；
# CDP 只能由用户在首次配置页明确切换（本地豆包，较慢）。
# 可选值：cli（默认）/ cdp。旧的 auto 一律按 cli 处理。
_BRIDGE_MODES = ("cli", "cdp")


class _ChannelError(RuntimeError):
    """通道不可用——带 machine-readable code，前端据此给"一键分流"按钮。

    2026-11-14 ZERO 反馈：CLI 登录不了只能走 CDP，但生视频仍按 CLI 判定，
    报一句"豆包 CLI 不可用"就结束，用户不知道该点哪儿。这里把原因、可选的
    下一条路一起交给前端，而不是让用户自己回配置页找开关。
    """

    def __init__(self, msg, code="channel_unavailable", action=""):
        super().__init__(msg)
        self.code = code
        self.action = action


def _load_bridge():
    env = (os.environ.get("HERONBO_VIDEO_BRIDGE") or "").strip().lower()
    if env in _BRIDGE_MODES:
        return env
    try:
        val = ((pcore_mod.read_local() if pcore_mod else {}) or {}).get("VIDEO_BRIDGE", "")
        val = str(val or "").strip().lower()
        if val in _BRIDGE_MODES:
            return val
    except Exception:  # noqa: BLE001
        pass
    return "cli"


_VIDEO_BRIDGE = _load_bridge()

# (最短秒, 最长秒)：Seedance 2.5 = 5–30；2.0 / 2.0 Fast = 4–15
MODEL_LIMIT = {"Seedance 2.5": (5, 30), "Seedance 2.0 Fast": (4, 15), "Seedance 2.0": (4, 15)}
DEFAULT_MODEL = "Seedance 2.0 Fast（720p）"
DEFAULT_RATIO = "9:16"

_LOCK = threading.Lock()
_STATE = {
    "running": False, "stage": "", "sid": "", "confirmText": "", "events": [],
    "params": {}, "result": None, "video": "", "error": "", "estimate": None,
    "started": 0, "finished": 0,
    # ── 进度（2026-11-11 用户要求："生视频超过二十分钟不返回，加个进度条"）──────
    # 按桥的里程碑 + 历史成片耗时折算 pct/eta，前端任务窗画条（慢片也看得出在动）
    "pct": 0, "eta": 0, "stageName": "", "elapsed": 0, "etaLeft": 0, "over": 0,
    "pctFloor": 0,
}

# 里程碑：桥的过程行 → (下限百分比, 阶段名)。只在对应 stage 生效。
_MARKS = {
    "prepare": [
        (r"开始准备视频方案", 3, "读取素材与参数"),
        (r"正在接入固定会话|固定会话已就绪|接入会话", 9, "接入豆包会话"),
        (r"已填入\s*\d+\s*字|灌入提示词", 22, "灌入提示词"),
        (r"点了发送按钮|已发送（|改用回车发送", 32, "已发送给豆包"),
        (r"它开始答了|等待豆包答复|已等待\s*\d+\s*s", 40, "豆包在读素材"),
        (r"正在写…（", 48, "豆包在写方案"),
        (r"答完了（用时", 64, "方案已写好"),
        (r"确认清单|等待你确认", 78, "确认清单已就绪"),
    ],
    "confirm": [
        (r"已回确认|确认已提交", 26, "确认已提交"),
        (r"流水线执行中|豆包：|生成中", 34, "豆包侧生成中"),
        (r"成片已出现在豆包工作区|取回|成片已生成", 90, "取回成片"),
        (r"豆包 CLI 已接收|CLI 已接收", 30, "豆包 CLI 已接收"),
        (r"豆包 CLI 正在生成|CLI 正在生成", 48, "豆包 CLI 生成中"),
        (r"豆包 CLI 已返回|CLI 已返回", 92, "CLI 已返回成片"),
    ],
}

_ETA_FILE = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                         "HeronBo", "video_eta.json")


def _eta_default(stage, p):
    """无历史时的兜底估时（超时后条会继续蠕）。"""
    dur = int((p or {}).get("duration") or 15)
    if stage == "prepare":
        imgs = len((p or {}).get("images") or []) + (1 if (p or {}).get("image") else 0)
        return 100 + 10 * max(0, imgs)
    if "2.5" in str((p or {}).get("model") or ""):
        return max(240, 30 * dur)
    return max(170, 19 * dur)


def _eta_hist(stage, p):
    """历史成片中位耗时，同模型同时长档优先。"""
    try:
        data = json.load(open(_ETA_FILE, encoding="utf-8"))
    except Exception:                                            # noqa: BLE001
        return 0.0, 0
    if not isinstance(data, list):
        return 0.0, 0
    model = str((p or {}).get("model") or "")
    dur = int((p or {}).get("duration") or 0)
    same = [float(x.get("seconds") or 0) for x in data
            if x.get("stage") == stage and x.get("model") == model and int(x.get("duration") or 0) == dur]
    same = [v for v in same if 20 < v < 7200]
    if len(same) >= 1:
        same.sort()
        return same[len(same) // 2], len(same)
    allv = [float(x.get("seconds") or 0) for x in data if x.get("stage") == stage]
    allv = [v for v in allv if 20 < v < 7200]
    if allv:
        allv.sort()
        return allv[len(allv) // 2], len(allv)
    return 0.0, 0


def _record_video_eta(stage, p, seconds, ok):
    try:
        os.makedirs(os.path.dirname(_ETA_FILE), exist_ok=True)
        data = []
        if os.path.isfile(_ETA_FILE):
            try:
                data = json.load(open(_ETA_FILE, encoding="utf-8"))
            except Exception:                                    # noqa: BLE001
                data = []
        if not isinstance(data, list):
            data = []
        data.append({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "stage": stage,
                     "model": str((p or {}).get("model") or ""),
                     "duration": int((p or {}).get("duration") or 0),
                     "seconds": round(float(seconds), 1), "ok": bool(ok)})
        del data[:-60]                       # 只留最近 60 条
        json.dump(data, open(_ETA_FILE, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:                                            # noqa: BLE001
        pass


def _recompute_prog_locked():
    """里程碑下限 + 段内指数逼近，算生视频 pct/剩余。"""
    st = _STATE
    stage = st.get("stage") or ""
    if stage not in _MARKS:
        return
    started = st.get("started") or time.time()
    el = max(0.0, time.time() - started)
    floor, name = 0, st.get("stageName") or ("准备中" if stage == "prepare" else "生成中")
    for e in (st.get("events") or [])[-80:]:
        msg = e.get("msg") or ""
        for rx, pct, nm in _MARKS[stage]:
            if re.search(rx, msg) and pct > floor:
                floor, name = pct, nm
    eta = float(st.get("eta") or 0) or _eta_default(stage, st.get("params") or {})
    if stage == "prepare":
        curve = 12 + 66 * (1 - math.exp(-1.9 * el / max(1.0, eta)))
        if el > eta:                                 # 迟迟不回也继续蠕
            curve = max(curve, 88.0 - 6.0 * math.exp(-(el - eta) / max(45.0, eta)))
        curve = min(90.0, curve)                     # 没拿到清单前别显得快完了
    else:
        base = max(float(floor), 26.0)
        curve = base + (88.0 - base) * (1 - math.exp(-1.45 * el / max(1.0, eta)))
        if el > eta:                                 # 超时往 94 蠕
            curve = max(curve, 94.0 - 7.0 * math.exp(-(el - eta) / max(60.0, eta)))
    pct = min(95.0, max(float(floor), curve, float(st.get("pct") or 0)))
    # 剩余 = 预计总时长 - 已用（超时后写"已超出"由前端读 over）
    st["pct"] = round(pct, 1)
    st["pctFloor"] = floor
    st["stageName"] = name
    st["elapsed"] = round(el)
    st["eta"] = round(eta)
    st["etaLeft"] = round(max(0.0, eta - el))
    st["over"] = round(max(0.0, el - eta)) if st.get("running") else 0


def _prog_loop(gen):
    """每秒重算一次，豆包不吐心跳时条也在动。"""
    while True:
        with _LOCK:
            if _PROG_GEN["n"] != gen:
                return                       # 换了一轮，线程收工
            if not _STATE.get("running") or _STATE.get("stage") not in _MARKS:
                return
            _recompute_prog_locked()
        time.sleep(1.0)


_PROG_GEN = {"n": 0}


def _done_set(stage, ok=True, **kw):
    """收尾：停线程、记这一轮真实耗时。"""
    with _LOCK:
        el = max(0.0, time.time() - (_STATE.get("started") or time.time()))
        prev = _STATE.get("stage") or ""
        eta = float(_STATE.get("eta") or 0)
        upd = {"running": False, "stage": stage, "finished": round(time.time(), 1),
               "elapsed": round(el), "etaLeft": 0,
               "pct": 100.0 if ok else min(99.0, float(_STATE.get("pct") or 0)),
               "over": 0 if ok else round(max(0.0, el - eta))}
        upd.update(kw)
        _STATE.update(upd)
        _PROG_GEN["n"] += 1
        params = dict(_STATE.get("params") or {})
    _record_video_eta(prev, params, el, ok)


# ---------------- 状态 ----------------
def _flow(msg, kind=None):
    """把视频过程也推一份到工作台全局动作流；独立运行/无 server 时静默跳过。"""
    try:
        import workbench_server as _ws
        if kind is None:
            kind = _ws._kind_of_seg(msg) if hasattr(_ws, "_kind_of_seg") else "think"
        _ws._push_event(kind, msg)
    except Exception:  # noqa: BLE001
        pass


def _log(msg):
    s = str(msg).strip()
    if not s:
        return
    with _LOCK:
        _STATE["events"].append({"t": round(time.time(), 1), "msg": s})
        if len(_STATE["events"]) > 400:
            del _STATE["events"][:-400]
        try:
            _recompute_prog_locked()      # 里程碑一到立刻跳
        except Exception:                 # noqa: BLE001
            pass
    _flow(s)


def _set(**kw):
    with _LOCK:
        _STATE.update(kw)


def snapshot():
    with _LOCK:
        out = json.loads(json.dumps(_STATE))
        out["bridge"] = _VIDEO_BRIDGE
        return out


def exported_paths():
    """本进程自己取回过的成片路径（只用于 `/api/video/play` 的窄白名单）。

    豆包会把成片以本地文件卡片交付在 `~/DoubaoWork/chats/...`，也可能落在用户
    自己选的交接目录；不能只按固定根目录放行，否则前端拿到 done 却播不出来。
    """
    with _LOCK:
        out = []
        v = str(_STATE.get("video") or "").strip()
        if v:
            out.append(v)
        r = _STATE.get("result") or {}
        if isinstance(r, dict):
            v2 = str(r.get("video") or "").strip()
            if v2:
                out.append(v2)
        return out


def _stash_final(pdir, src):
    """把成片落到项目 `成片/`，保证前端回显时文件一定还在、一定在读白名单内。

    2026-11-05 用户报「豆包生成成功但工作台不显示成片」：桥返回的是豆包工作区
    原路径（或用户自选交接目录），文件被豆包清理/移动后 `/api/video/play` 就 404。
    这里优先硬链接（同盘几乎零成本），跨盘再复制；已在项目内则原样返回。
    """
    src = str(src or "").strip()
    if not src or not os.path.isfile(src):
        return src
    pdir = str(pdir or "").strip()
    if not pdir:
        return src
    try:
        src_real = os.path.realpath(src)
        pdir_real = os.path.realpath(pdir)
        if src_real.startswith(pdir_real + os.sep):
            return src
    except OSError:
        pass
    dst_dir = os.path.join(pdir, "成片")
    try:
        os.makedirs(dst_dir, exist_ok=True)
        name = os.path.basename(src) or "final.mp4"
        dst = os.path.join(dst_dir, name)
        base, ext = os.path.splitext(name)
        i = 1
        while os.path.exists(dst) and os.path.realpath(dst) != os.path.realpath(src):
            dst = os.path.join(dst_dir, "%s_%d%s" % (base, i, ext))
            i += 1
        if os.path.realpath(dst) == os.path.realpath(src):
            return dst
        try:
            os.link(src, dst)          # 同盘硬链接：不占额外空间，删除原文件也不影响
        except OSError:
            shutil.copy2(src, dst)     # 跨盘/文件系统不支持硬链接：复制一份
        return dst
    except OSError as e:
        _log("成片落盘失败（继续用原路径）：%s" % e)
        return src



def is_busy():
    """有没有一轮视频任务正占着这个坑。

    2026-11-16 用户反馈「让豆包生视频偶尔点一下没反应，要重启工作台才能再点」。
    原因是 `running` 只在 `_done_set` 里被放开；一旦后台线程被卡住（豆包 CLI 网关
    挂起、子进程不返回、异常被吞掉），它永远是 True，前端每一下都被这句挡回去，
    只能重启整个进程才能解开——表现就是"点了没反应"。

    超龄兜底：超过该阶段的合理上限，判定这一轮已经死了，直接把坑放开，让用户能再点。
    prepare 正常几十秒，confirm 的 CLI 内层超时是 30 分钟，所以分别给 10 / 45 分钟。
    """
    stalled = 0
    with _LOCK:
        if not _STATE["running"]:
            return False
        stage = _STATE.get("stage") or ""
        started = float(_STATE.get("started") or 0)
        limit = 600 if stage == "prepare" else 2700
        el = (time.time() - started) if started else 0
        if el <= limit:
            return True
        stalled = int(el // 60)
        _STATE.update({
            "running": False, "stage": "stalled", "finished": round(time.time(), 1),
            "stageName": "上一轮卡住已放开", "etaLeft": 0,
            "error": "上一轮跑了 %d 分钟没有结果，已自动放开；可以重新点一次" % stalled})
        _PROG_GEN["n"] += 1                  # 让还在转的进度线程收工
    _log("stalled: %s 上一轮 %d 分钟没结果，自动放开 busy" % (stage, stalled))
    return False


def _reset(stage, params):
    with _LOCK:
        _STATE.update({
            "running": True, "stage": stage, "sid": "", "confirmText": "",
            "events": [], "params": params or {}, "result": None, "video": "",
            "error": "", "estimate": params.get("estimate") if params else None,
            "started": round(time.time(), 1), "finished": 0,
            "pct": 0, "pctFloor": 0, "elapsed": 0,
            "stageName": "读取素材与参数" if stage == "prepare" else "确认已提交"})
        _hist, _n = _eta_hist(stage, params or {})
        _STATE["eta"] = round(_hist or _eta_default(stage, params or {}))
        _STATE["etaLeft"] = _STATE["eta"]
        _STATE["over"] = 0
        _PROG_GEN["n"] += 1
        _gen = _PROG_GEN["n"]
    threading.Thread(target=_prog_loop, args=(_gen,), daemon=True).start()


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


def model_range(model):
    for k, v in MODEL_LIMIT.items():
        if k in (model or ""):
            return v                       # (min, max)
    return None


def model_limit(model):
    r = model_range(model)
    return r[1] if r else None             # 上限（兼容旧调用）


def clamp_duration(model, sec):
    r = model_range(model)
    if not r:
        return int(sec)
    lo, hi = r
    return max(lo, min(int(sec), hi))


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
    """提取口播台词（估时只用台词，不能用整段提示词）。

    来源优先级（2026-10-08 修"出完提示词后默认时长消失"）：
    ① 备注/需求.txt 的「台词[改成]：」行（工作台口径）；
    ② 框架.json versions/prompts 里登记的 lines 字段（agent 出提示词时回填的台词）；
    ③ 文案/提示词正文.txt 里的「台词改成：」行（老口径）。
    都没有 → ''（estimate 返回 0，界面时长落回模型下限）。
    """
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
    try:   # ② 框架.json 里 agent 回填的台词（出完提示词后就有）
        sk = json.load(open(os.path.join(pdir, "框架.json"), encoding="utf-8-sig"))
        proj = sk.get("project") or {}
        for v in list(proj.get("versions") or []) + list(proj.get("prompts") or []):
            t = str(v.get("lines") or v.get("台词") or "").strip()
            if t:
                return t
    except Exception:  # noqa: BLE001
        pass
    for cand in (os.path.join("文案", "提示词正文.txt"), "提示词正文.txt",
                 os.path.join("文案", "提示词.txt"), "提示词.txt"):
        fp = os.path.join(pdir, cand)
        if os.path.isfile(fp):
            try:
                m = re.search(r"台词(?:改成)?[：:]\s*([^\n]+)",
                              open(fp, encoding="utf-8-sig").read())
                if m and m.group(1).strip():
                    return m.group(1).strip()
            except OSError:
                pass
    return ""


def _pick_in_dir(d):
    """返回 (主参考图, 音色, 参考视频, 全部参考图[])。

    全部参考图（2026-10-07）：平台上传夹里 staged 的每张图（图片1_首帧形象_…、图片2_广告牌牌面_…）
    都要带给豆包——之前只挑一张，牌面/分镜图根本进不了生成请求。主图仍按 形象/首帧/图片1 优先。
    """
    image = audio = video = ""
    images = []
    for root, _ds, fs in os.walk(d):
        for f in sorted(fs):
            lf = f.lower()
            if not video and lf.endswith((".mp4", ".mov", ".mkv", ".webm")) and (
                    "参考视频" in f or "口播展示" in f or re.match(r"视频1", f)):
                video = os.path.join(root, f)
            if lf.endswith((".png", ".jpg", ".jpeg", ".webp")):
                p = os.path.join(root, f)
                if p not in images:
                    images.append(p)
                if not image and ("形象" in f or "首帧" in f or re.match(r"图片1", f)):
                    image = p
            if not audio and lf.endswith((".mp3", ".wav", ".m4a")) and (
                    "音色" in f or re.match(r"音频1", f)):
                audio = os.path.join(root, f)
    if not image or not audio or not video:  # 兜底任意 png/mp3/mp4
        for root, _ds, fs in os.walk(d):
            for f in sorted(fs):
                lf = f.lower()
                if not video and lf.endswith((".mp4", ".mov", ".mkv", ".webm")):
                    video = os.path.join(root, f)
                if not image and lf.endswith((".png", ".jpg", ".jpeg", ".webp")):
                    image = os.path.join(root, f)
                    if image not in images:
                        images.append(image)
                if not audio and lf.endswith((".mp3", ".wav", ".m4a")):
                    audio = os.path.join(root, f)

    def _img_no(p):
        m = re.match(r"图片(\d+)", os.path.basename(p))
        return int(m.group(1)) if m else 999

    images.sort(key=lambda q: (_img_no(q), q))
    if image and image in images:            # 主图排最前（首帧槽位）
        images.remove(image)
        images.insert(0, image)
    return image, audio, video, images


def resolve_inputs(pdir):
    """解析当前版参考图（全部）/音色/提示词；form 图生/文生按有无形象图判定。"""
    out = {"image": "", "images": [], "audio": "", "video": "", "promptPath": "", "speech": "", "form": "文生"}
    if not (pdir and os.path.isdir(pdir)):
        return out
    pf = _prompt_file(pdir)
    if pf:
        out["promptPath"] = pf
    out["speech"] = _speech_text(pdir)
    up = pcore_mod.upload_dir(pdir) if pcore_mod else os.path.join(pdir, "平台上传")
    # 2026-10-10：素材/平台上传/（原始上传素材，m11803 两级布局）也参与候选——
    # agent 加工前的原件收在那里，加工副本还没落顶层时从这里取。
    src_dirs = []
    if pcore_mod and getattr(pcore_mod, "upload_src_dir", None):
        sd = pcore_mod.upload_src_dir(pdir)
        if sd:
            src_dirs.append(sd)
    dirs = []
    for d0 in [up] + src_dirs:
        if os.path.isdir(d0):
            dirs.append(d0)
            for fn in os.listdir(d0):
                d = os.path.join(d0, fn)
                if os.path.isdir(d):
                    dirs.append(d)
    cur_no = _current_ver_no(pdir)
    best = None  # (score, mtime, image, audio, video, images)
    for d in dirs:
        image, audio, video, images = _pick_in_dir(d)
        score = (1 if image else 0) + (1 if audio else 0) + (1 if video else 0)
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
            best = (score, mt, image, audio, video, images)
    if best:
        out["image"], out["audio"], out["video"], out["images"] = best[2], best[3], best[4], best[5]
    out["form"] = ("视频参考" if out["video"] else
                   ("图生" if out["image"] else "文生"))
    return out


# ---------------- CDP 子进程 ----------------
_CDP_PORTS = range(9333, 9345)          # 与 doubao_cdp.mjs 的 PORT_RANGE 一致
_CDP_STATE = os.path.join(os.environ.get("TEMP") or os.environ.get("TMP") or ".",
                          "heronbo_canvas_cdp.json")


def cdp_port():
    """只读探测豆包的调试端口（先看桥记下的，再扫 9333–9344）。返回端口或 0。

    绝不启动/重启豆包——这个函数只回答"现在能不能直接接管"。
    """
    cands = []
    try:
        with open(_CDP_STATE, encoding="utf-8") as f:
            saved = int((json.load(f) or {}).get("port") or 0)
            if saved:
                cands.append(saved)
    except Exception:  # noqa: BLE001
        pass
    for p in _CDP_PORTS:
        if p not in cands:
            cands.append(p)
    import urllib.request
    for p in cands:
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/json/version" % p, timeout=1.2) as r:
                if r.status == 200:
                    return p
        except Exception:  # noqa: BLE001
            continue
    return 0


def _cdp_guard():
    """接管前的前置检查：豆包在跑但没调试端口时，**不替用户擅自重启**。

    2026-11-14 ZERO 反馈"CDP 把豆包生视频整坏了"：桥发现"在跑但没端口"会直接
    kill + 带端口重开（见 doubao_cdp.mjs ensureAttached）。本机原本为了修"桌面上
    两个豆包"才这么写，但对正在用豆包的人就是一次打断。改成先报出来、由用户点
    「重启豆包并接管」再动手；豆包没在跑时照旧自动拉起（不碰任何东西，安全）。
    """
    if cdp_port():
        return
    try:
        if ab and getattr(ab, "_doubaowork_running", None) and ab._doubaowork_running():
            raise _ChannelError(
                "豆包工作正在运行，但不是以调试端口启动的，现在接管不上。"
                "继续会把它关掉再带调试端口重开（对话历史会保留，但你手上这条会被打断）。",
                "cdp_not_ready", "重启豆包并接管")
    except _ChannelError:
        raise
    except Exception:  # noqa: BLE001
        pass


def restart_doubao(timeout=90):
    """用户点「重启豆包并接管」后才调：走桥的 restart --yes（WMI 守场员执行）。"""
    if not ab:
        return {"ok": False, "error": "组件缺失"}
    js = ab.doubao_js()
    runner, renv = ab.lib_runner(js) if js else (None, None)
    if not (js and runner):
        return {"ok": False, "error": "视频桥没准备好，暂时用不了"}
    cmd = [runner, js, "restart", "--yes"]
    env = ab.child_env()
    env.update(renv or {})
    env.update(ab.runtime_env_for(cmd))
    try:
        p = subprocess.Popen(cmd, cwd=_HERE, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             encoding="utf-8", errors="replace", bufsize=1,
                             **ab.no_window_kwargs())
        try:
            out, _ = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            out = "（等待超时，豆包可能还在起，稍等十几秒再点一次生成）"
        rc = p.returncode
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:300]}
    ok = bool(cdp_port()) or rc == 0
    return {"ok": ok, "port": cdp_port(), "rc": rc,
            "error": "" if ok else ((out or "").strip()[-300:] or "重启没成功")}


def show_doubao(timeout=40):
    """把豆包工作的窗口拉回屏幕可见区并置前（用户点「打开豆包窗口」时调）。

    2026-11-15 用户反馈「托盘里点豆包没反应，以为被工作台屏蔽了」。根因有两层：
      1) 窗口位置是客户端自己记住的，前几次实验把它记成了屏外坐标（HOME 实测
         rect=(-970,0)-(30,720)，屏幕上只剩 30px 挂边，点任务栏图标当然"没反应"）；
      2) 工作台侧原本根本没有"把豆包叫出来"的入口。
    桥里的 show 动作会把窗口设成 normal、挪到 (80,80,1120,780) 并 bringToFront，
    这里直接借它；顺带把在跑但没调试端口的情况一起解决不现实，那种仍走 restart。
    """
    if not ab:
        return {"ok": False, "error": "组件缺失"}
    js = ab.doubao_js()
    runner, renv = ab.lib_runner(js) if js else (None, None)
    if not (js and runner):
        return {"ok": False, "error": "视频桥没准备好，暂时用不了"}
    cmd = [runner, js, "show"]
    env = ab.child_env()
    env.update(renv or {})
    env.update(ab.runtime_env_for(cmd))
    try:
        p = subprocess.Popen(cmd, cwd=_HERE, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             encoding="utf-8", errors="replace", bufsize=1,
                             **ab.no_window_kwargs())
        try:
            out, _ = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            return {"ok": False, "error": "叫醒豆包超时了，稍等十几秒再点一次"}
        rc = p.returncode
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:300]}
    # 桥成功时输出一行 {"ok": true}；失败时带 error。
    ok = False
    err = ""
    try:
        import json as _json
        for line in (out or "").splitlines():
            line = line.strip()
            if line.startswith("{"):
                o = _json.loads(line)
                ok = bool(o.get("ok"))
                err = str(o.get("error") or "")
    except Exception:  # noqa: BLE001
        pass
    return {"ok": ok, "rc": rc, "error": "" if ok else (err or (out or "").strip()[-300:] or "没叫醒豆包")}


def _run_video(args):
    if not ab:
        raise RuntimeError("组件缺失")
    stage = ""
    for i, a in enumerate(args):
        if str(a) == "--stage" and i + 1 < len(args):
            stage = str(args[i + 1])
    if stage in ("prepare", "confirm"):
        _cdp_guard()          # 只在"要接管并操作页面"的阶段拦；cancel/collect 放行
    js = ab.doubao_js()
    runner, renv = ab.lib_runner(js) if js else (None, None)
    if not (js and runner):
        raise RuntimeError("视频桥没准备好，暂时用不了")
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
        raise RuntimeError("视频桥返回码 %s%s" % (rc, (("：" + raw[-200:]) if raw else "（看动作流）")))
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


def bridge_mode():
    return _VIDEO_BRIDGE


def set_bridge(mode):
    """手动切换生成通道；默认 cli，cdp 只能由用户明确点选。

    2026-11-13 用户要求：启动时不走 CDP，避免拉起调试端口、影响正在用的豆包工作。
    """
    global _VIDEO_BRIDGE
    m = str(mode or "").strip().lower()
    if m not in _BRIDGE_MODES:
        return {"ok": False, "error": "通道只能是 cli / cdp"}
    _VIDEO_BRIDGE = m
    persisted = False
    try:
        if pcore_mod:
            pcore_mod.write_local(VIDEO_BRIDGE=m)
            # 回读校验：write_local 出错时静默返回，光看"没抛异常"会误报切换成功，
            # 重启后打回 cli，下次生成又是"CLI 不可用"——ZERO 上就是这么踩的。
            got = str(((pcore_mod.read_local() or {}) or {}).get("VIDEO_BRIDGE") or "")
            persisted = got.strip().lower() == m
    except Exception:  # noqa: BLE001
        persisted = False
    return {"ok": True, "mode": m, "persisted": persisted}


def bridge_info():
    """给首次配置页显示当前模式和 CDP 是否具备条件；不连 CDP、不拉起客户端。"""
    cdp_ready, cdp_why = False, ""
    try:
        if ab:
            cdp_ready, cdp_why = ab._probe_doubaowork()
    except Exception as e:  # noqa: BLE001
        cdp_why = str(e)[:160]
    return {"mode": _VIDEO_BRIDGE, "cdp_ready": bool(cdp_ready),
            "cdp_why": str(cdp_why or "")[:200],
            "modes": list(_BRIDGE_MODES)}


def _use_cli():
    """是否走豆包 CLI。默认 cli；cdp 只在用户手动切换后使用，绝不自动回退。"""
    if _VIDEO_BRIDGE == "cdp":
        return False
    if cli_bridge is None:
        raise _ChannelError("组件缺失，无法走豆包 CLI", "cli_missing",
                            "改用本地豆包（CDP）")
    ok, why = cli_bridge.available()
    if ok:
        return True
    # 2026-11-14：不再只甩一句"CLI 不可用"。带上"改用本地豆包"这个可执行出口，
    # 前端拿 code=cli_unavailable 直接给按钮，省掉"回配置页找开关"这一段。
    raise _ChannelError("豆包 CLI 不可用：%s" % (why or "未登录"), "cli_unavailable",
                        "改用本地豆包（CDP）")


def _fail(e, stageName="失败"):
    """把异常收成前端能分流的状态：通道类错误带 code/action，其余只留文案。"""
    code = getattr(e, "code", "") if isinstance(e, _ChannelError) else ""
    action = getattr(e, "action", "") if isinstance(e, _ChannelError) else ""
    _done_set("error", ok=False, error=str(e), errorCode=code, errorAction=action,
              stageName=stageName)
    _flow("%s：%s" % (stageName, e), "think")


def _read_prompt(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


# ---------------- 后台线程 ----------------
def start_prepare(p):
    if is_busy():
        return {"ok": False, "error": "已有视频任务在跑，等它结束"}
    p = dict(p or {})
    _reset("prepare", p)
    _flow("开始准备视频方案（只读、不生成、不扣点）", "tool")

    def _job():
        try:
            if _use_cli():
                sid = "cli-%s-%s" % (time.strftime("%Y%m%d%H%M%S"),
                                     int(time.time() * 1000) % 1000)
                model = p.get("model") or DEFAULT_MODEL
                duration = int(p.get("duration") or 15)
                ratio = p.get("ratio") or DEFAULT_RATIO
                nimg = len([x for x in (p.get("images") or []) if x])
                confirm = (
                    "准备用豆包 CLI 生成成片：\n"
                    "模型：%s\n时长：%s 秒\n画幅：%s\n参考图：%s 张\n"
                    "提示词：%s\n"
                    "确认后才会提交生成并消耗豆包企业套餐额度；现在只做本地准备，不扣点。"
                    % (model, duration, ratio, nimg,
                       os.path.basename(p.get("promptPath") or "") or "（缺提示词正文）")
                )
                _done_set("await_confirm", sid=sid, confirmText=confirm,
                          result={"ok": True, "state": "await_confirm", "bridge": "cli",
                                  "sid": sid, "model": model, "duration": duration,
                                  "ratio": ratio, "images": nimg},
                          stageName="CLI 方案已准备")
                _flow("豆包 CLI 方案已准备：%s / %s秒 / %s，等待你确认（未确认不扣点）"
                      % (model, duration, ratio), "done")
                return
            args = ["--stage", "prepare", "--model", p.get("model") or DEFAULT_MODEL,
                    "--ratio", p.get("ratio") or DEFAULT_RATIO,
                    "--duration", int(p.get("duration") or 15),
                    "--prompt-file", p.get("promptPath"),
                    "--timeout", int(p.get("timeout") or 500)]
            imgs = list(p.get("images") or [])
            if not imgs and p.get("image"):
                imgs = [p["image"]]
            for _im in imgs:                       # 每张参考图都带给豆包（--image 可重复，2026-10-07）
                args += ["--image", _im]
            if p.get("audio"):
                args += ["--audio", p["audio"]]
            if p.get("video"):
                args += ["--video", p["video"]]
            if p.get("visible"):
                args += ["--visible"]              # 非豆包通道：跳转豆包界面（可见窗口），用户手动确认
                if _tray:                          # 那扇窗就是要给用户看的：把上一轮收进托盘的
                    try:                           # 放出来（否则复用到同一段会话时用户什么也看不见）
                        _tray.show_all()
                    except Exception:  # noqa: BLE001
                        pass
            elif _tray:
                try:                               # 后台驱动：豆包窗口收进托盘，任务栏不再挂窗
                    _tray.hide_async(ab.doubaowork_exe() if ab else "", icon=True)
                except Exception:  # noqa: BLE001
                    pass
            r = _run_video(args)
            if r.get("state") == "manual":
                _done_set("manual", sid=r.get("sid", ""), result=r, stageName="等待你在界面确认")
                _flow("已打开生成平台界面——请在窗口里核对确认清单并手动确认执行", "done")
            elif r.get("state") == "await_confirm" and r.get("confirmText"):
                _done_set("await_confirm", sid=r.get("sid", ""), stageName="看起来没问题 → 等你确认",
                          confirmText=r.get("confirmText", ""), result=r)
                _flow("方案已准备好，等待你确认（未确认不会生成、不扣点）", "done")
            else:
                # 2026-10-21：失败文案带上诊断目录（桥会把截图/选择器/输入框文本落盘到那儿），
                # 用户截个图我们也能顺着原始数据定位（豆包界面改版时唯一有效的排查手段）。
                _err = r.get("error") or "prepare 没拿到确认清单"
                _diag = str(r.get("diag") or "").strip()
                if _diag:
                    _err = "%s（诊断已保存：%s）" % (_err, _diag)
                _done_set("error", ok=False, error=_err, result=r, stageName="准备失败")
                _flow("准备失败：" + _err, "think")
        except Exception as e:  # noqa: BLE001
            _fail(e, "准备失败")
            _flow("准备失败：" + str(e), "think")

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
    _flow("已确认，开始生成成片", "tool")

    def _job():
        try:
            if _use_cli():
                prompt = _read_prompt(p.get("promptPath") or "") or _read_prompt(
                    p.get("prompt") or "")
                if not prompt.strip():
                    _done_set("error", ok=False, error="缺提示词正文，无法提交 CLI",
                              stageName="生成失败")
                    _flow("生成失败：缺提示词正文", "think")
                    return
                model = p.get("model") or DEFAULT_MODEL
                ratio = p.get("ratio") or DEFAULT_RATIO
                duration = int(p.get("duration") or 15)
                outdir = os.path.dirname(p.get("outPath") or "") or os.path.join(
                    p.get("project") or "", "成片")
                os.makedirs(outdir, exist_ok=True)
                _log("豆包 CLI 已接收：%s / %s秒 / %s，开始提交生成…"
                     % (model, duration, ratio))
                imgs = list(p.get("images") or [])
                if not imgs and p.get("image"):
                    imgs = [p["image"]]
                # 2026-11-16 用户反馈「豆包 CLI 跑进度不显示面板或者没动作」：
                # generate_video 是阻塞调用（最长 30 分钟），期间一条 flow 都不发，
                # 界面看起来就是点了没反应。这里起心跳，每 15 秒报一次已等时长。
                _t0 = time.time()
                _hb = threading.Event()

                def _heartbeat():
                    while not _hb.wait(15):
                        try:
                            el = time.time() - _t0
                            _flow("豆包 CLI 生成中：已等 %d 分 %02d 秒（%s / %s秒 / %s）"
                                  % (int(el // 60), int(el % 60), model, duration, ratio),
                                  "think")
                        except Exception:  # noqa: BLE001
                            pass

                threading.Thread(target=_heartbeat, daemon=True,
                                 name="cli-video-heartbeat").start()
                try:
                    r = cli_bridge.generate_video(
                        prompt, outdir, ratio=ratio, duration=duration, model=model,
                        images=imgs, audio=p.get("audio") or "", video=p.get("video") or "",
                        timeout=int(p.get("timeout") or 1800), session_key=p.get("sid") or "")
                finally:
                    _hb.set()
                _log("豆包 CLI 已返回：%s" % (r.get("path") or r.get("error") or "无结果"))
                v = str(r.get("path") or "").strip()
                if r.get("ok") and v:
                    local = _stash_final(p.get("project"), v)
                    if local and local != v:
                        r["sourceVideo"] = v
                        r["video"] = local
                    else:
                        r["video"] = local or v
                    _done_set("done", video=local or v, result=r, stageName="CLI 成片已生成")
                    _flow("豆包 CLI 成片已生成：" + (local or v), "done")
                else:
                    _done_set("error", ok=False, error=r.get("error") or "CLI 没拿到成片",
                              result=r, stageName="没拿到成片")
                    _flow("生成失败：" + (r.get("error") or "CLI 没拿到成片"), "think")
                return
            args = ["--stage", "confirm", "--sid", p["sid"],
                    "--ratio", p.get("ratio") or DEFAULT_RATIO,
                    "--duration", int(p.get("duration") or 15),
                    "--timeout", int(p.get("timeout") or 1500)]
            if p.get("outPath"):
                args += ["--out", p["outPath"]]
            if _tray:
                try:                               # 生成阶段同样把豆包收进托盘
                    _tray.hide_async(ab.doubaowork_exe() if ab else "", icon=True)
                except Exception:  # noqa: BLE001
                    pass
            r = _run_video(args)
            v = str(r.get("video") or r.get("videoPath") or r.get("path") or r.get("file") or "").strip()
            if r.get("state") == "done" and v:
                local = _stash_final(p.get("project"), v)
                if local and local != v:
                    r["sourceVideo"] = v
                    r["video"] = local
                _done_set("done", video=local or v, result=r, stageName="成片已生成")
                _flow("成片已生成：" + (local or v), "done")
            else:
                _done_set("error", ok=False,
                          error=r.get("error") or "confirm 没拿到成片（可能超时）", result=r,
                          stageName="没拿到成片")
                _flow("生成失败：" + (r.get("error") or "没拿到成片（可能超时）"), "think")
        except Exception as e:  # noqa: BLE001
            _fail(e, "生成失败")
            _flow("生成失败：" + str(e), "think")

    threading.Thread(target=_job, daemon=True).start()
    return {"ok": True, "stage": "confirm"}


def cancel_now(sid):
    try:
        if _use_cli():
            r = cli_bridge.cancel(sid)
        else:
            r = _run_video(["--stage", "cancel", "--sid", sid, "--timeout", 30])
    except Exception as e:  # noqa: BLE001
        r = {"ok": False, "error": str(e)}
    _done_set("cancelled", ok=False, error="", stageName="已取消")
    _flow("已取消视频任务", "think")
    return {"ok": True, "state": "cancelled", "sid": sid, "result": r}


# ---------------- 高层 API（server 薄路由调用） ----------------
def _settled_duration(pdir):
    """项目里**已经定过**的成片时长（2026-10-08 修"出完提示词后默认时长消失"）：
    agent 出提示词时把估的时长回填进了 框架.json 的 prompts[].duration（workbench_server
    里的 settled_brief 也读它）。返回 None 表示没定过。"""
    try:
        sk = json.load(open(os.path.join(pdir, "框架.json"), encoding="utf-8-sig"))
        proj = sk.get("project") or {}
        for v in list(proj.get("prompts") or []) + list(proj.get("versions") or []):
            d = v.get("duration")
            if d not in (None, "", 0):
                return int(d)
    except Exception:  # noqa: BLE001
        pass
    return None


def api_estimate(pdir, b):
    b = b or {}
    est = estimate(b.get("script") or resolve_inputs(pdir).get("speech") or "",
                   "sell" if b.get("mode", "sell") == "sell" else "normal")
    settled = _settled_duration(pdir)
    if settled:
        # 出过提示词的项目：时长以 agent 定下的为准（语音估时只在没有定值时兜底）
        est["suggest"] = settled
        est["settled"] = True
    return {"ok": True, "estimate": est}


def api_prepare(pdir, b, visible=False):
    b = b or {}
    inp = resolve_inputs(pdir)
    model = b.get("model") or DEFAULT_MODEL
    ratio = b.get("ratio") or DEFAULT_RATIO
    est = estimate(inp.get("speech") or "", "sell")
    settled = _settled_duration(pdir)
    if settled:
        est["suggest"] = settled      # 同 api_estimate：出过提示词按时长定值（2026-10-08）
        est["settled"] = True
    duration = clamp_duration(
        model, b["duration"] if b.get("duration") else est["suggest"])
    return start_prepare({
        "project": pdir, "image": inp.get("image") or "",
        "images": inp.get("images") or [],
        "audio": inp.get("audio") or "",
        "video": inp.get("video") or "",
        "promptPath": inp.get("promptPath"), "model": model, "ratio": ratio,
        "duration": duration, "form": inp.get("form"), "estimate": est,
        "timeout": int(b.get("timeout") or 500), "visible": bool(visible)})


def api_confirm(pdir, b):
    b = b or {}
    st = snapshot()
    sid = b.get("sid") or st.get("sid")
    if not sid:
        return {"ok": False, "error": "缺 sid（先 prepare）"}
    params = st.get("params") or {}
    model = params.get("model") or b.get("model") or DEFAULT_MODEL
    ratio = b.get("ratio") or params.get("ratio") or DEFAULT_RATIO
    duration = clamp_duration(
        model, b.get("duration") or params.get("duration") or 15)
    out = os.path.join(pdir, "成片", "heronbo_%s.mp4" % sid)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    return start_confirm({"project": pdir, "sid": sid, "ratio": ratio,
                          "duration": duration, "outPath": out,
                          "model": model,
                          "promptPath": params.get("promptPath") or "",
                          "images": params.get("images") or [],
                          "image": params.get("image") or "",
                          "audio": params.get("audio") or "",
                          "video": params.get("video") or "",
                          "timeout": int(b.get("timeout") or 1800)})


def api_cancel(pdir, b):
    b = b or {}
    sid = b.get("sid") or snapshot().get("sid")
    if not sid:
        return {"ok": False, "error": "缺 sid"}
    return cancel_now(sid)


def api_dismiss(pdir, b):
    """清掉已收尾（done/error）但没看过的任务残留——恢复弹窗给用户看完后调，下次点
    「生成视频」不再弹旧结果。2026-10-10（m11686 2.3①）。"""
    with _LOCK:
        if _STATE.get("running"):
            return {"ok": False, "error": "任务还在跑，不能清"}
        _STATE.update({"stage": "", "sid": "", "confirmText": "", "video": "",
                       "error": "", "result": None, "params": {}, "events": [],
                       "pct": 0, "pctFloor": 0, "stageName": "", "elapsed": 0,
                       "etaLeft": 0, "over": 0})
        _PROG_GEN["n"] += 1
    return {"ok": True}
