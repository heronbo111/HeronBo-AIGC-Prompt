# -*- coding: utf-8 -*-
r"""即梦（Dreamina）CLI 通道：调官方 `dreamina` CLI 生成视频（2026-10-23 用户指定功能）。

背景（用户原话）：「可以做即梦通道然后让 agent 去接入（按钮就放在让豆包生视频旁边，
叫做即梦一键生成，备注积分存在消耗，慎用）」。

为什么这样设计：
  · **提交即扣点**（官方 CLI 文档明示"All generation operations consume credits"），
    所以按钮处明确标注「积分存在消耗，慎用」，点开先弹确认（参数 + 预计花费 + 当前余额），
    用户按「确认生成（扣点）」才真正提交——**绝不代用户提交**（与工作台铁律一致）；
  · CLI 没有"试算/报价"接口（2026-10-23 实测：全部子命令 -h 均无 --estimate/--quote；
    `list_task` 只记录 CLI 自己提交过的任务）。预计花费只能按面板单价估：
    Seedance 2.5 · 720p = ¥0.18/秒（CLI 额外 9 折，即 ×0.9）；其它模型标注"以即梦页面为准"；
  · **真实消耗**用「提交前后 `user_credit` 余额差」算出来，完成后写进结果——用它逐步校准单价。

CLI 来源：官方安装脚本（`curl -s https://jimeng.jianying.com/cli | bash`）装到 `~/bin/dreamina.exe`，
自带 SKILL 说明在 `~/.dreamina_cli/dreamina/SKILL.md`；OAuth 登录态也在 `~/.dreamina_cli/`。
"""
import datetime
import json
import os
import re
import shutil
import subprocess
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# 面板单价（¥/秒）：只有 Seedance 2.5 · 720p 有公开价（横幅"低至 ¥0.18/秒"）
PRICE_PER_SEC = {"seedance2.5": {"720p": 0.18}}
CLI_DISCOUNT = 0.9                       # 会员/团队使用 CLI 额外 9 折

_LOCK = threading.Lock()
_DRE = {
    "running": False, "stage": "", "submit_id": "", "video": "", "error": "",
    "events": [], "params": {}, "balance_before": None, "balance_after": None,
    "spent": None, "started": 0, "finished": 0,
}


def _say(msg):
    with _LOCK:
        _DRE["events"].append({"at": time.time(), "text": str(msg)[:300]})
        _DRE["events"] = _DRE["events"][-200:]


def snapshot():
    with _LOCK:
        return dict(_DRE)


# ── CLI 探测 / 调用 ──────────────────────────────────────────────────────────
def cli_path():
    """找 dreamina：① PATH ② 官方脚本默认落点 ~/bin ③ 常见安装目录。"""
    for name in ("dreamina.exe", "dreamina"):
        p = shutil.which(name)
        if p:
            return p
    home = os.path.expanduser("~")
    cands = [os.path.join(home, "bin", "dreamina.exe"),
             os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "dreamina", "dreamina.exe"),
             os.path.join(home, ".dreamina_cli", "dreamina.exe")]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return ""


def _run(args, timeout=120):
    """跑 CLI → (ok, parsed)。CLI 多数命令输出 JSON；非 JSON 时返回文本。"""
    exe = cli_path()
    if not exe:
        return False, "没找到 dreamina CLI（先按官方脚本安装：curl -s https://jimeng.jianying.com/cli | bash）"
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = 0x08000000          # CREATE_NO_WINDOW：别弹黑窗
    try:
        p = subprocess.run([exe] + args, capture_output=True, timeout=timeout, env=env, **kw)
    except (OSError, subprocess.SubprocessError) as e:      # noqa: BLE001
        return False, str(e)[:300]
    out = (p.stdout or b"").decode("utf-8", "replace").strip()
    err = (p.stderr or b"").decode("utf-8", "replace").strip()
    text = out or err
    try:
        return True, json.loads(text) if text[:1] in ("{", "[") else text
    except ValueError:
        return True, text


def account():
    """登录态 + 余额（失败时给出可操作的提示）。"""
    exe = cli_path()
    if not exe:
        return {"ok": False, "error": "没找到 dreamina CLI：先按官方脚本装一次"
                                      "（curl -s https://jimeng.jianying.com/cli | bash），再 dreamina login"}
    ok, d = _run(["user_credit"], timeout=40)
    if isinstance(d, dict) and ("total_credit" in d or "user_id" in d):
        return {"ok": True, "cli": exe, **d}
    msg = str(d)[:300] if not isinstance(d, dict) else json.dumps(d, ensure_ascii=False)[:300]
    if "login" in msg.lower() or "登录" in msg:
        return {"ok": False, "error": "即梦 CLI 还没登录：命令行执行 `dreamina login` 完成一次 OAuth 授权即可",
                "cli": exe, "raw": msg}
    return {"ok": False, "error": "查不到账户信息：%s" % msg, "cli": exe, "raw": msg}


# ── 参数映射（前端③栏 → CLI）───────────────────────────────────────────────
MODEL_MAP = {
    "Seedance 2.5": "seedance2.5",
    "Seedance 2.0": "seedance2.0",
    "Seedance 2.0 Fast（720p）": "seedance2.0fast",
}
RES_FOR = {  # 每个模型允许的分辨率；第一个是默认
    "seedance2.5": ["720p", "1080p", "480p"],
    "seedance2.0": ["720p"],
    "seedance2.0fast": ["720p"],
}
EXT_IMG = (".png", ".jpg", ".jpeg", ".webp")
EXT_VID = (".mp4", ".mov", ".webm")
EXT_AUD = (".mp3", ".wav", ".m4a", ".aac", ".flac")


def _upload_inputs(pdir):
    """平台上传清单（当前版）→ 按扩展名分成 image/video/audio。"""
    out = {"images": [], "videos": [], "audios": []}
    if not pdir or not os.path.isdir(pdir):
        return out
    bases = []
    up = os.path.join(pdir, "平台上传")
    if os.path.isdir(up):
        for name in sorted(os.listdir(up)):
            d = os.path.join(up, name)
            if os.path.isdir(d):
                bases.append(d)
        bases.append(up)                                   # 散件
    for base in bases:
        if not os.path.isdir(base):
            continue
        for fn in sorted(os.listdir(base)):
            fp = os.path.join(base, fn)
            if not os.path.isfile(fp):
                continue
            low = fn.lower()
            if low.endswith(EXT_IMG):
                out["images"].append(fp)
            elif low.endswith(EXT_VID):
                out["videos"].append(fp)
            elif low.endswith(EXT_AUD):
                out["audios"].append(fp)
        if out["images"] or out["videos"] or out["audios"]:
            break                                              # 只用第一个有料的目录（当前版优先）
    return out


def _estimate_text(model, res, dur):
    p = PRICE_PER_SEC.get(model, {}).get(res)
    if not p:
        return "预计花费：以即梦页面为准（CLI 没有试算接口）"
    return "预计约 ¥%.2f（%.2f 元/秒 × %d 秒 × CLI 9 折）" % (p * dur * CLI_DISCOUNT, p, dur)


def _norm_params(pdir, b):
    """把界面传来的参数规整成 CLI 可用的样子（非法值给明确报错，不静默改）。"""
    model = MODEL_MAP.get(str(b.get("model") or "").strip())
    if not model:
        return None, "不认识的模型：%s（可选：%s）" % (b.get("model"), "、".join(MODEL_MAP))
    res = str(b.get("resolution") or "").strip()
    allowed = RES_FOR.get(model) or ["720p"]
    if res not in allowed:
        res = allowed[0]
    try:
        dur = int(b.get("duration") or 0)
    except (TypeError, ValueError):
        dur = 0
    lo, hi = (4, 30) if model == "seedance2.5" else (4, 15)
    if dur < lo or dur > hi:
        dur = max(lo, min(hi, dur or 5))
    ratio = str(b.get("ratio") or "9:16").strip()
    if ratio not in ("1:1", "3:4", "16:9", "4:3", "9:16", "21:9"):
        ratio = "9:16"
    prompt = str(b.get("prompt") or "").strip()
    if not prompt and pdir:
        # 界面通常不传長正文：直接读项目里那份（提示词正文.txt 优先级最高）
        for rel in ("文案/提示词正文.txt", "文案/提示词.txt"):
            fp = os.path.join(pdir, rel.replace("/", os.sep))
            if not os.path.isfile(fp):
                continue
            try:
                txt = open(fp, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            if rel.endswith("提示词.txt"):
                # 去掉元信息行（时长/模型/比例/版本/画幅…），只留正文
                lines = [x for x in txt.splitlines()
                         if not re.match(r"^\s*(时长|模型|比例|版本|画幅|上传)\s*[:：]", x)]
                txt = "\n".join(lines)
            prompt = txt.strip()
            if prompt:
                break
    if not prompt:
        return None, "缺提示词正文（③栏先出一版提示词，或检查 文案/提示词正文.txt）"
    if len(prompt) > 4000:
        return None, "提示词太长（%d 字，CLI 建议 ≤4000）" % len(prompt)
    ins = _upload_inputs(pdir)
    kind = "multimodal2video" if (ins["images"] or ins["videos"] or ins["audios"]) else "text2video"
    if kind == "multimodal2video" and model != "seedance2.5":
        # 2.0 家族同样支持 multimodal2video（帮助里写明），保持同一命令即可
        pass
    return {"model": model, "resolution": res, "duration": dur, "ratio": ratio,
            "prompt": prompt, "kind": kind, "inputs": ins}, ""


# ── 对外 API ────────────────────────────────────────────────────────────────
def api_prepare(pdir, b):
    """确认弹窗用：余额 + 参数 + 预计花费（**只读，不提交**）。"""
    acc = account()
    if not acc.get("ok"):
        return {"ok": False, "error": acc.get("error") or "即梦 CLI 不可用", "account": acc}
    ps, err = _norm_params(pdir, b)
    if not ps:
        return {"ok": False, "error": err, "account": acc}
    n_in = len(ps["inputs"]["images"]) + len(ps["inputs"]["videos"]) + len(ps["inputs"]["audios"])
    return {"ok": True, "account": {"total_credit": acc.get("total_credit"),
                                    "user_id": acc.get("user_id"),
                                    "vip_level": acc.get("vip_level")},
            "params": {"model": ps["model"], "resolution": ps["resolution"],
                       "duration": ps["duration"], "ratio": ps["ratio"],
                       "kind": ps["kind"], "inputs": n_in,
                       "images": ps["inputs"]["images"], "videos": ps["inputs"]["videos"],
                       "audios": ps["inputs"]["audios"]},
            "estimate": _estimate_text(ps["model"], ps["resolution"], ps["duration"]),
            "warn": "积分存在消耗，慎用：提交后立即开始计费，取消也无法退回。"}


def api_generate(pdir, b):
    """确认后真正提交（后台线程）。b.confirm 必须为 true——防误调。"""
    if not b.get("confirm"):
        return {"ok": False, "error": "没有确认（confirm 必须为 true）——这一步会扣点，必须由用户点「确认生成」"}
    with _LOCK:
        if _DRE["running"]:
            return {"ok": False, "error": "上一次即梦生成还没结束，等它跑完（或等结果回来）"}
    ps, err = _norm_params(pdir, b)
    if not ps:
        return {"ok": False, "error": err}
    out_dir = os.path.join(pdir, "成片") if pdir else ""
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with _LOCK:
        _DRE.update({"running": True, "stage": "submit", "submit_id": "", "video": "",
                     "error": "", "events": [], "finished": 0, "spent": None,
                     "balance_after": None, "balance_before": None,
                     "params": {"model": ps["model"], "resolution": ps["resolution"],
                                "duration": ps["duration"], "ratio": ps["ratio"],
                                "kind": ps["kind"]},
                     "started": time.time()})
    threading.Thread(target=_job, args=(pdir, ps), daemon=True).start()
    return {"ok": True, "started": True}


def _job(pdir, ps):
    out_dir = os.path.join(pdir, "成片") if pdir else ""
    before = None
    try:
        acc = account()
        if acc.get("ok"):
            before = acc.get("total_credit")
            with _LOCK:
                _DRE["balance_before"] = before
            _say("余额：%s 点" % before)
        args = [ps["kind"], "--prompt", ps["prompt"],
                "--duration", str(ps["duration"]),
                "--model_version", ps["model"],
                "--video_resolution", ps["resolution"]]
        if ps["kind"] != "text2video":
            for p in ps["inputs"]["images"][:9]:
                args += ["--image", p]
            for p in ps["inputs"]["videos"][:3]:
                args += ["--video", p]
            for p in ps["inputs"]["audios"][:3]:
                args += ["--audio", p]
        if not (ps["model"] == "seedance2.5" and ps["kind"] == "multimodal2video"):
            args += ["--ratio", ps["ratio"]]
        args += ["--poll", "1"]
        _say("已提交即梦任务（%s · %s · %ds · %s）…"
             % (ps["model"], ps["resolution"], ps["duration"], ps["ratio"]))
        ok, d = _run(args, timeout=600)
        if not ok:
            raise RuntimeError(str(d)[:300])
        sid = _pick(d, "submit_id", "submitId", "task_id", "taskId")
        if isinstance(d, dict) and d.get("fail_reason"):
            raise RuntimeError("提交失败：%s" % d.get("fail_reason"))
        if not sid:
            raise RuntimeError("提交没拿到 submit_id：%s" % (json.dumps(d, ensure_ascii=False)[:300]
                                                        if isinstance(d, dict) else str(d)[:300]))
        with _LOCK:
            _DRE["submit_id"] = sid
            _DRE["stage"] = "running"
        _say("submit_id = %s，开始轮询结果…" % sid)
        deadline = time.time() + 45 * 60
        status = ""
        last = ""
        while time.time() < deadline:
            time.sleep(5)
            okq, q = _run(["query_result", "--submit_id", sid], timeout=120)
            if not okq:
                _say("查询失败：%s（继续重试）" % str(q)[:160])
                continue
            status = (_pick(q, "gen_status", "genStatus", "status") or "").lower()
            msg = _pick(q, "message", "msg", "progress")
            line = "状态：%s%s" % (status or "?", (" · %s" % msg) if msg else "")
            if line != last:
                last = line
                _say(line)
            if status in ("success", "succeeded", "done", "finished"):
                break
            if status in ("fail", "failed", "error"):
                raise RuntimeError("生成失败：%s" % (_pick(q, "fail_reason", "failReason", "error") or "未知原因"))
        else:
            raise RuntimeError("等结果超时（45 分钟）——submit_id=%s，稍后可用 query_result 再查" % sid)
        # 下载（CLI 的 --download_dir 会按网页同款下载场景落地）
        _say("生成完成，正在下载成片…")
        target = out_dir or os.path.expanduser("~")
        os.makedirs(target, exist_ok=True)
        okd, d2 = _run(["query_result", "--submit_id", sid, "--download_dir", target], timeout=600)
        files = []
        if okd and isinstance(d2, dict):
            for k in ("download_paths", "files", "paths"):
                v = d2.get(k)
                if isinstance(v, list):
                    files = [x for x in v if isinstance(x, str)]
                    break
            if not files:
                u = _pick(d2, "download_path", "local_path", "video_path", "media_path")
                if isinstance(u, str) and u:
                    files = [u]
        if not files and okd and isinstance(d2, str):
            files = [x for x in re.findall(r"[A-Za-z]:\\[^\r\n\"']+\.(?:mp4|mov|webm)", d2)]
        if not files:
            # 兜底：扫成片目录里最近 5 分钟新增的视频
            files = [os.path.join(target, f) for f in os.listdir(target)
                     if f.lower().endswith((".mp4", ".mov", ".webm"))
                     and time.time() - os.path.getmtime(os.path.join(target, f)) < 300]
        videos = [f for f in files if os.path.isfile(f) and f.lower().endswith((".mp4", ".mov", ".webm"))]
        if not videos:
            raise RuntimeError("任务成功但没找到下载的成片文件（输出目录：%s）" % target)
        video = max(videos, key=os.path.getmtime)
        with _LOCK:
            _DRE.update({"video": video, "stage": "done", "running": False,
                         "finished": round(time.time(), 1)})
        _say("成片：%s" % video)
        after = account()
        if after.get("ok") and before is not None:
            spent = before - (after.get("total_credit") or before)
            with _LOCK:
                _DRE["balance_after"] = after.get("total_credit")
                _DRE["spent"] = spent
            _say("本次消耗：%s 点（余额 %s → %s）" % (spent, before, after.get("total_credit")))
    except Exception as e:                                        # noqa: BLE001
        with _LOCK:
            _DRE.update({"running": False, "stage": "error", "error": str(e)[:400],
                         "finished": round(time.time(), 1)})
        _say("失败：%s" % str(e)[:300])


def _pick(d, *keys):
    """从 dict 里取第一个存在且非空的键（CLI 输出的字段名可能有变体）。"""
    if not isinstance(d, dict):
        return ""
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return ""


def api_dismiss():
    with _LOCK:
        if _DRE["running"]:
            return {"ok": False, "error": "还在跑，等它结束"}
        _DRE.update({"stage": "", "video": "", "error": "", "submit_id": "",
                     "events": [], "spent": None, "balance_before": None, "balance_after": None})
    return {"ok": True}
