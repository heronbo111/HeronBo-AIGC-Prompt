# -*- coding: utf-8 -*-
"""分镜预览：按提示词先出一组分镜图（豆包 Seedream 优先生成，其次即梦 CLI）。

为什么要有（2026-11-11 用户要求）：
  视频一条要几十积分、等几分钟；先用生图把「这条片子长什么样」画出来，
  用户看一眼就知道分镜对不对，再决定要不要花积分生成视频。
  出的图同时放进 `平台上传/`，可以直接当参考素材喂给视频模型
  （wb_video.resolve_inputs 会把 平台上传 里的图全部当参考图带上）。

两条通道：
  · 豆包工作（优先）：`doubao_cdp.mjs media --kind image`——让它用内置的 Seedream 技能画，
    走企业套餐、不扣个人积分。
  · 即梦 CLI（兜底）：`dreamina text2image --model_version 5.0 --resolution_type 2k`，
    按张扣积分（先报价、确认后才提交）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time

try:
    import wb_cli
except Exception:  # noqa: BLE001
    wb_cli = None
try:
    import project_core as pcore
except Exception:  # noqa: BLE001
    pcore = None

HERE = os.path.dirname(os.path.abspath(__file__))
_LOCK = {"running": False, "stage": "", "log": [], "result": None, "started": 0.0,
         "channel": "", "n": 0, "storyPromptPath": ""}


def _bridge_mode():
    """生成通道：默认 cli；只有用户明确切换后才走 cdp（本地豆包，较慢）。"""
    env = (os.environ.get("HERONBO_VIDEO_BRIDGE") or "").strip().lower()
    if env in ("cli", "cdp"):
        return env
    try:
        val = ((pcore.read_local() if pcore else {}) or {}).get("VIDEO_BRIDGE", "")
        val = str(val or "").strip().lower()
        if val in ("cli", "cdp"):
            return val
    except Exception:  # noqa: BLE001
        pass
    return "cli"


# ---------------- 小工具 ----------------
def _log(s):
    _LOCK["log"].append("%s %s" % (time.strftime("%H:%M:%S"), s))
    _LOCK["log"] = _LOCK["log"][-80:]


def _set(**kw):
    _LOCK.update(kw)


def status():
    return {"running": _LOCK["running"], "stage": _LOCK["stage"],
            "log": list(_LOCK["log"]), "result": _LOCK["result"],
            "channel": _LOCK["channel"], "n": _LOCK["n"],
            "storyPromptPath": _LOCK.get("storyPromptPath") or "",
            "elapsed": round(time.time() - _LOCK["started"], 1) if _LOCK["started"] else 0}


def _read(path, limit=20000):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()[:limit]
    except OSError:
        return ""


def _body_text(pdir):
    """项目正文：优先 文案/提示词正文.txt，退回 提示词.txt。"""
    for rel in (("文案", "提示词正文.txt"), ("提示词.txt",)):
        t = _read(os.path.join(pdir, *rel))
        if t.strip():
            return t.strip()
    return ""


def _fields(pdir):
    """框架.json → project.fields（专业参数）。"""
    try:
        with open(os.path.join(pdir, "框架.json"), encoding="utf-8-sig") as f:
            sk = json.load(f) or {}
        return (sk.get("project") or {}).get("fields") or {}
    except (OSError, ValueError):
        return {}


def _ratio(pdir):
    """画幅：框架.json 里定过就用它，否则 16:9（分镜表横着看最舒服）。"""
    f = _fields(pdir)
    for k in ("画幅", "比例", "ratio"):
        v = str(f.get(k) or "").strip()
        if v:
            m = re.search(r"(\d{1,2})\s*[:：]\s*(\d{1,2})", v)
            if m:
                return "%s:%s" % (m.group(1), m.group(2))
    txt = _body_text(pdir)
    m = re.search(r"(\d{1,2})\s*[:：]\s*(\d{1,2})", txt[:600])
    if m:
        r = "%s:%s" % (m.group(1), m.group(2))
        if r in ("21:9", "16:9", "3:2", "4:3", "1:1", "3:4", "2:3", "9:16", "4:5"):
            return r
    return "16:9"


def _split_shots(pdir, n):
    """每格画面的依据。

    优先用正文里已经写好的「镜头N：…」行——上游/用户通常已经分好镜了，
    再按句子硬切只会把「镜头1：」套进下一格，格序全错位。
    没有这种行时才退回按句子切。
    """
    txt = _body_text(pdir)
    shots = []
    for ln in txt.splitlines():
        ln = re.sub(r"^[\s\-*·•\d.、]+", "", ln).strip()
        m = re.match(r"^(?:镜头|分镜|画面|Shot)\s*(\d+)\s*[：:.、\-）)]\s*(.+)$",
                     ln, re.I)
        if m and m.group(2).strip():
            shots.append(m.group(2).strip())
    if not shots:
        body = re.sub(r"（[^）]{0,40}）", "", txt)
        sents = [s.strip() for s in re.split(r"[。！？!?\n]+", body) if s.strip()]
        shots = [s for s in sents if len(s) > 4]
    if not shots:
        shots = ["人物站在画面中央，面向镜头", "环境全景，交代场景", "手部特写，展示道具"]
    return [shots[min(i, len(shots) - 1)][:80] for i in range(n)]


def _session_slug(pdir):
    """每个项目用自己那段豆包对话（2026-11-15 用户：对话应该跟着项目走）。

    此前 `--session storyboard` 是写死的常量，`doubao_cdp.mjs` 会把它持久化成
    `storyboard:sessionSid` —— 所有项目共用同一段对话，换项目、删项目还接着用旧的，
    上下文和素材全串在一起。现在按项目名派 slug：名字里能认人，尾部哈希保证两个
    同名项目（不同目录）不会撞。
    """
    import hashlib
    base = os.path.basename(str(pdir).rstrip("\\/")) or "project"
    keep = re.sub(r"[^A-Za-z0-9._-]", "", base)[:20]
    tail = hashlib.md5(base.encode("utf-8")).hexdigest()[:6]
    return "sb-" + (keep + "-" if keep else "") + tail


def _ref_images(pdir, limit=3):
    """分镜生图用的「形象基准」图：平台上传里的图片，优先首帧 / 图片1。

    2026-11-15 用户报「分镜图生成没有做素材匹配」：此前只把人物特征写成文字
    （"短发、细金框长眼镜…"）交给生图模型，模型每次都画成另一个人。生图模型本身
    支持参考图，把素材图一起带上才是真正的形象一致。
    """
    up = _upload_dir(pdir)
    scored = []
    try:
        for fn in os.listdir(up):
            if not fn.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                continue
            p = os.path.join(up, fn)
            if not os.path.isfile(p):
                continue
            if "首帧" in fn or fn.startswith("图片1_"):
                scored.append((2, fn, p))
            elif re.match(r"图片\d+_", fn):
                scored.append((1, fn, p))
    except OSError:
        pass
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [p for _s, _f, p in scored[:limit]]


def build_prompt(pdir, n):
    """给生图模型的提示词：要求按镜号一格格画分镜图。"""
    shots = _split_shots(pdir, n)
    f = _fields(pdir)
    style = "、".join("%s=%s" % (k, v) for k, v in f.items() if str(v or "").strip())[:200]
    cols = 3 if n <= 9 else 4
    lines = [
        "画一张「%d 格分镜图」（storyboard contact sheet）：%d 个格子按 %d 列排布，" % (n, n, cols),
        "每格是一帧写实电影画面，格子之间留 6px 白边，整体像专业广告分镜表。",
        "统一风格：写实电影感、自然光、浅景深、干净构图；不要文字、不要水印、不要字幕。",
        "每格的画面内容（按顺序）：",
    ]
    for i, s in enumerate(shots, 1):
        lines.append("%d. 镜头%d：%s" % (i, i, s))
    if style:
        lines.append("全片统一参数：%s" % style)
    if _ref_images(pdir):
        lines.append(
            "形象基准：随附的参考图就是本片的人物与商品实物——%d 个格子里必须是**同一个"
            "人、同一件商品**：脸型、发型、妆容、服装、体型、商品外观与包装都以参考图为准，"
            "不得换脸、不得换发型、不得换服装、不得替换商品。" % n)
    return chr(10).join(lines)


def _upload_dir(pdir):
    d = os.path.join(pdir, "平台上传")
    os.makedirs(d, exist_ok=True)
    return d


def _next_index(updir):
    """平台上传里现有的「图片N」最大编号 + 1（避开已占用的引用编号）。"""
    mx = 0
    for fn in os.listdir(updir):
        m = re.match(r"图片(\d+)[_\-.]", fn)
        if m:
            mx = max(mx, int(m.group(1)))
    return mx + 1


def _land(pdir, files, n):
    """把生成的图放进 预览/，并复制成 平台上传/图片N_分镜.png（可供视频引用）。

    一次只画一张「N 格分镜表」，所以正常只有 1 个文件；
    若通道真回了多张，就按顺序落成 _分镜1、_分镜2…

    另写一版「分镜提示词-<时间>.txt」：正文 + 每格画面说明，
    让视频那一步能"按这组分镜顺序拍"（图本身会被视频流程自动当参考图带上）。
    """
    prev = os.path.join(pdir, "预览")
    os.makedirs(prev, exist_ok=True)
    stamp = time.strftime("%m%d-%H%M%S")
    up = _upload_dir(pdir)
    idx = _next_index(up)
    landed, uploaded = [], []
    files = [f for f in files[:4] if os.path.isfile(f)]
    many = len(files) > 1
    for i, src in enumerate(files, 1):
        ext = os.path.splitext(src)[1] or ".png"
        p1 = os.path.join(prev, "分镜-%s-%d%s" % (stamp, i, ext))
        try:
            shutil.copy2(src, p1)
            landed.append(p1)
        except OSError:
            continue
        tail = ("%d" % i) if many else ""
        p2 = os.path.join(up, "图片%d_分镜%s%s" % (idx + i - 1, tail, ext))
        try:
            shutil.copy2(src, p2)
            uploaded.append(os.path.basename(p2))
        except OSError:
            pass
    try:
        base = _body_text(pdir)
        # 2026-11-12：连续生成/应用时旧的分镜参考块不能一层层叠加。
        # 只保留正文第一段，旧的「【分镜参考（本轮预览图）】」整块丢掉。
        marker = "【分镜参考（本轮预览图）】"
        if marker in base:
            base = base.split(marker, 1)[0].rstrip()
        shots = _split_shots(pdir, n)
        lines = [base, "", "【分镜参考（本轮预览图）】"]
        if len(uploaded) == 1:
            lines.append("%s：一张 %d 格分镜表，按从左到右、从上到下的格序"
                         "对应下面 %d 个镜头——" % (uploaded[0], n, n))
            for i, s in enumerate(shots, 1):
                lines.append("第%d格 镜头%d：%s" % (i, i, s))
        else:
            for i, nm in enumerate(uploaded, 1):
                lines.append("%s：镜头%d：%s" % (
                    nm, i, shots[i - 1] if i - 1 < len(shots) else ""))
        lines.append("生成时以上面分镜表的画面、景别、构图与先后顺序为准；"
                     "镜头切分点与格序一一对应，人物形象在各格之间保持一致。")
        wdir = os.path.join(pdir, "文案")
        os.makedirs(wdir, exist_ok=True)
        fpath = os.path.join(wdir, "分镜提示词-%s.txt" % stamp)
        text = chr(10).join([x for x in lines if x is not None]).strip() + chr(10)
        with open(fpath, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        _set(storyPromptPath=fpath)
        _log("已生成「分镜版提示词」：文案/分镜提示词-%s.txt" % stamp)
    except OSError as e:
        _log("分镜版提示词写入失败：%s" % e)
    return landed, uploaded


# ---------------- 两条通道 ----------------
def _run(cmd, timeout=900, cwd=HERE):
    kw = {"capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace",
          "timeout": timeout, "cwd": cwd}
    if os.name == "nt":
        kw["creationflags"] = 0x08000000
    return subprocess.run(cmd, **kw)


def _node():
    for c in (os.path.join(HERE, "_vendor", "node", "node.exe"),
              os.path.join(HERE, "..", "runtime", "node", "node.exe"),
              os.path.join(HERE, "..", "..", "runtime", "node", "node.exe")):
        if os.path.isfile(c):
            return os.path.abspath(c)
    return "node"


def _dreamina():
    p = shutil.which("dreamina")
    if p:
        return p
    c = os.path.join(os.path.expanduser("~"), "bin", "dreamina.exe")
    return c if os.path.isfile(c) else ""


def _cdp_port_now():
    """CDP 只读探测：有没有可用的调试端口。**绝不启动/重启豆包**。

    为什么单独拎出来（2026-11-14 用户实测）：分镜预览的 cdp 分支直接调
    `doubao_cdp.mjs media`，它内部的 ensureAttached 发现"豆包在跑但没端口"会
    直接 kill + 带端口重开——用户正在用豆包就被打断。生成前先只读探一次，
    探不到就把决定权交回用户（前端弹「重启豆包并接管」）。
    """
    try:
        import wb_video as _wv
        return int(_wv.cdp_port() or 0)
    except Exception:                                            # noqa: BLE001
        return 0


def _doubao_work_running():
    try:
        import agent_bridge as _ab
        return bool(_ab._doubaowork_running())
    except Exception:                                            # noqa: BLE001
        return False


def doubao_alive(timeout=120):
    """豆包能干活吗（不只是端口通不通）。

    2026-11-13 起默认只走豆包 CLI；用户明确切到 CDP 才碰本地豆包桥。
    CLI 不在/未登录都直接报错，不再自动回退，避免生成时拉起调试端口。
    """
    mode = _bridge_mode()
    if mode == "cli":
        if wb_cli is None:
            return False, "组件缺失，无法走豆包 CLI"
        try:
            ok, why = wb_cli.available(force=True)
        except Exception as e:  # noqa: BLE001
            return False, str(e)[:160]
        return (True, "") if ok else (False, str(why or "豆包 CLI 未登录"))
    js = os.path.join(HERE, "doubao_cdp.mjs")
    if not os.path.isfile(js):
        return False, "缺 doubao_cdp.mjs"
    # 2026-11-14：原来这里跑 `doubao_cdp.mjs probe` 探活，但 probe 内部也会走
    # ensureAttached —— "豆包在跑但没端口"时它直接 kill + 重开，等于把用户正在
    # 用的豆包打断。探活必须只读：只问端口，不动进程。
    if _cdp_port_now():
        return True, ""
    if _doubao_work_running():
        return False, "豆包工作正在运行但没开调试端口（重启豆包后才能接管）"
    return True, ""                       # 没在跑：生成阶段会按需拉起


def channel_available():
    out = {"doubao": False, "dreamina": False, "why": "", "cli": False,
           "mode": _bridge_mode()}
    mode = out["mode"]
    # 默认只查 CLI；CDP 只有用户手动切换后才参与，不在启动/分镜准备阶段碰端口。
    if mode == "cli":
        if wb_cli is not None:
            try:
                ok, why = wb_cli.available(force=True)
                out["cli"] = bool(ok)
                out["doubao"] = bool(ok)
                out["why"] = "" if ok else str(why or "")
            except Exception as e:                               # noqa: BLE001
                out["why"] = str(e)[:120]
        else:
            out["why"] = "组件缺失"
    else:
        js = os.path.join(HERE, "doubao_cdp.mjs")
        try:
            import agent_bridge as ab
            ok, why = ab._probe_doubaowork()
            out["doubao"] = bool(ok) and os.path.isfile(js)
            out["why"] = "" if out["doubao"] else str(why or "本地豆包未就绪")
        except Exception as e:                                   # noqa: BLE001
            out["why"] = str(e)[:120]
    out["dreamina"] = bool(_dreamina())
    return out


def _write_prompt(pdir, prompt):
    tmp = os.path.join(pdir, "_会话", "分镜提示词.txt")
    os.makedirs(os.path.dirname(tmp), exist_ok=True)
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(prompt)
    return tmp


def _gen_doubao(pdir, prompt, ratio, outdir):
    """一次只画一张分镜表（格数写在提示词里，不是画 N 张）。

    ratio/quality 要显式给：不给的话豆包按 1:1 + 默认清晰度出，
    多格拼在一张里会糊成一片。2026-11-13 起默认走豆包 CLI 的 seedream-55；
    CDP 只有用户手动切换后才使用，不再自动兜底。
    """
    tmp = _write_prompt(pdir, prompt)
    mode = _bridge_mode()
    if mode == "cli":
        if wb_cli is None:
            return [], (1, "组件缺失，无法走豆包 CLI")
        try:
            ok, why = wb_cli.available()
        except Exception as e:  # noqa: BLE001
            return [], (1, str(e)[:400])
        if not ok:
            return [], (1, "豆包 CLI 不可用：%s；如需本地豆包，请到首次配置页手动切换 CDP"
                        % (why or "未登录"))
        r = wb_cli.generate_image(prompt, outdir, ratio=ratio,
                                  model="seedream_5.5", timeout=900,
                                  session_key="storyboard-%s" % int(time.time()),
                                  refs=_ref_images(pdir))
        files = list((r or {}).get("files") or [])
        tail = ((r or {}).get("raw", {}).get("out") or "")[-400:]
        if files:
            return files, (0, tail)
        return [], (1, str((r or {}).get("error") or "豆包 CLI 没返回图片")[-400:])
    js = os.path.join(HERE, "doubao_cdp.mjs")
    cmd = [_node(), js, "media",
           "--kind", "image", "--count", "1",
           "--model", "Seedream 5.5",
           "--session", _session_slug(pdir),
           "--ratio", ratio, "--quality", "2K",
           "--prompt-file", tmp, "--out", outdir, "--timeout", "420"]
    # 2026-11-15：把素材图当参考图一起发（`--image` 可重复）。不带的话豆包只会
    # 按文字描述另画一个人 —— 这就是用户看到的「没做素材匹配」。
    for _p in _ref_images(pdir):
        cmd += ["--image", _p]
    r = _run(cmd, timeout=480)
    tail = ((r.stdout or "") + (r.stderr or ""))[-400:]
    files = sorted((os.path.join(outdir, f) for f in os.listdir(outdir)
                    if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))),
                   key=lambda p: os.path.getmtime(p))
    if not files:
        # 2026-11-11：豆包那边图已经出了、但抓取被页面重渲染打断时，
        # 用 collect 按这轮记下的会话 id 再补取一次（不重新发送、不重复扣点）。
        _log("没抓到文件，按会话补取一次…")
        _run([_node(), js, "collect", "--kind", "image",
              "--session", _session_slug(pdir), "--out", outdir], timeout=120)
        files = sorted((os.path.join(outdir, f) for f in os.listdir(outdir)
                        if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))),
                       key=lambda p: os.path.getmtime(p))
    return files, (r.returncode, tail)


def _gen_dreamina(pdir, prompt, ratio, outdir):
    exe = _dreamina()
    if not exe:
        return [], (1, "找不到 dreamina CLI")
    _write_prompt(pdir, prompt)
    t0 = time.time()
    # 2026-11-12：2K 图通常要十几到几十秒，--poll 8 太短，CLI 会在任务还没完成时返回；
    # 这里给足 120 秒，仍失败时下面再按下载目录回收。
    cmd = [exe, "text2image", "--prompt", prompt[:1800],
           "--model_version", "5.0", "--ratio", ratio,
           "--resolution_type", "2k", "--generate_num", "1", "--poll", "120"]
    r = _run(cmd, timeout=1200)
    out = (r.stdout or "") + chr(10) + (r.stderr or "")
    files = []
    for m in re.finditer(r"([A-Za-z]:\\[^\r\n\"']+?\.(?:png|jpg|jpeg|webp))", out):
        p = m.group(1).strip()
        if os.path.isfile(p):
            files.append(p)
    if not files:
        # 只认本轮开始后落盘的新图，避免把下载目录里的旧图当成本轮结果。
        for root in (os.path.join(os.path.expanduser("~"), ".dreamina", "downloads"),
                     os.path.join(os.path.dirname(exe), "downloads")):
            if os.path.isdir(root):
                cand = [os.path.join(root, f) for f in os.listdir(root)
                        if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))]
                cand = [p for p in cand if os.path.getmtime(p) >= t0 - 5]
                if cand:
                    files.append(max(cand, key=os.path.getmtime))
    return files, (r.returncode, out[-400:])


# ---------------- 对外 API ----------------
def _n_of(b, default=6):
    try:
        return max(2, min(16, int((b or {}).get("n") or default)))
    except (TypeError, ValueError):
        return default


def api_prepare(pdir, b):
    """只算方案，不生成、不扣积分。"""
    if not pdir or not os.path.isdir(pdir):
        return {"ok": False, "error": "没有项目"}
    n = _n_of(b)
    ch = channel_available()
    prefer = (b or {}).get("channel") or ("doubao" if ch["doubao"] else
                                          ("dreamina" if ch["dreamina"] else ""))
    if not prefer or not ch.get(prefer):
        return {"ok": False, "error": "两条通道都不可用：豆包工作没连上，即梦 CLI 也没装"}
    return {"ok": True, "channel": prefer, "n": n, "ratio": _ratio(pdir),
            "model": "Seedream 5.5（豆包 CLI）" if prefer == "doubao" else "即梦 text2image 5.0",
            "shots": _split_shots(pdir, n), "prompt": build_prompt(pdir, n),
            "costHint": ("豆包 CLI / 企业套餐内，不扣个人积分" if prefer == "doubao"
                         else "即梦按张扣积分（生成后显示余额变化）"),
            "channels": ch}


def api_generate(pdir, b):
    """确认后生成。必须先 prepare，且 confirm=true。"""
    b = b or {}
    if not b.get("confirm"):
        return {"ok": False, "error": "还没有确认生成"}
    if _LOCK["running"]:
        return {"ok": False, "error": "上一组分镜还在生成，等它结束"}
    if not pdir or not os.path.isdir(pdir):
        return {"ok": False, "error": "没有项目"}
    n = _n_of(b)
    ch = channel_available()
    ch_name = (b.get("channel") or "").strip() or ("doubao" if ch["doubao"] else
                                                   ("dreamina" if ch["dreamina"] else ""))
    if not ch_name or not ch.get(ch_name):
        return {"ok": False, "error": "通道不可用"}
    # 2026-11-12 修复：每轮用独立目录。以前固定用 _会话/分镜原图，
    # 目录里上一轮的图会被 _gen_doubao 一起列出来，导致把旧分镜当成本轮结果。
    outdir = os.path.join(pdir, "_会话", "分镜原图",
                          time.strftime("%Y%m%d-%H%M%S-") + str(int(time.time() * 1000) % 1000))
    os.makedirs(outdir, exist_ok=True)
    prompt = build_prompt(pdir, n)
    ratio = str(b.get("ratio") or "").strip()
    if not re.match(r"^\d{1,2}:\d{1,2}$", ratio):
        ratio = _ratio(pdir)

    def _job():
        _set(running=True, stage="生图", log=[], started=time.time(),
             channel=ch_name, n=n, result=None, storyPromptPath="")
        try:
            _log("用「%s」画 %d 格分镜表（%s）…" % (
                "豆包 Seedream（CLI）" if ch_name == "doubao" else "即梦 text2image", n, ratio))
            if ch_name == "doubao":
                # 2026-11-14 关键修复：以前不管 mode 是 cli 还是 cdp，这里一律做
                # `doubao_alive()`（CLI 登录态探活）。用户明明切了 CDP，照样报
                # "豆包 CLI 不可用"——分镜预览图这条链路就是这么被 CLI 卡死的。
                if _bridge_mode() == "cdp":
                    port = _cdp_port_now()
                    if not port and _doubao_work_running():
                        _log("本地豆包在跑但没开调试端口：不自动重启，交给用户决定")
                        _set(stage="失败", running=False,
                             result={"ok": False,
                                     "error": "豆包工作正在运行，但不是以调试端口启动的，现在接管不上。",
                                     "detail": "点下面的「重启豆包并接管」，工作台会带调试端口把它重开一次"
                                               "（对话历史保留，但手上那条会被打断）。",
                                     "errorCode": "cdp_not_ready",
                                     "action": "重启豆包并接管",
                                     "fallback": "dreamina" if ch["dreamina"] else ""})
                        return
                    _log("本地豆包调试端口 %d 已就绪" % port if port else "本地豆包没在跑，生成时会拉起")
                else:
                    _log("检查豆包 CLI 登录态…")
                    alive, why = doubao_alive(20)
                    if not alive:
                        _set(stage="失败", running=False,
                             result={"ok": False, "stage": "busy",
                                     "error": "豆包 CLI 不可用：%s" % why,
                                     "detail": "点下面的「改用本地豆包并重试」可以直接切到 CDP；"
                                               "也可以先在终端运行 doubao login 完成登录。",
                                     "errorCode": "cli_unavailable",
                                     "action": "改用本地豆包并重试",
                                     "fallback": "dreamina" if ch["dreamina"] else ""})
                        _log("豆包 CLI 不可用：" + why)
                        return
                files, (rc, tail) = _gen_doubao(pdir, prompt, ratio, outdir)
            else:
                files, (rc, tail) = _gen_dreamina(pdir, prompt, ratio, outdir)
            if not files:
                extra = ("\n豆包这条没出图（它可能正忙、窗口被关，或提示词被它当成了聊天）。"
                         "可以换成「即梦 CLI」再试。" if ch_name == "doubao" else "")
                _set(stage="失败", running=False,
                     result={"ok": False, "error": "没拿到图（rc=%s）" % rc,
                             "detail": (tail[-300:] + extra).strip(),
                             "fallback": "dreamina" if (ch_name == "doubao" and ch["dreamina"]) else ""})
                _log("失败：没拿到图 " + tail[-160:])
                return
            _log("拿到 %d 张图，归档到 预览/ 与 平台上传/ …" % len(files))
            landed, uploaded = _land(pdir, files, n)
            _set(stage="完成", running=False,
                 result={"ok": True, "files": landed, "uploaded": uploaded,
                         "channel": ch_name, "n": len(landed), "panels": n,
                         "prompt": prompt,
                         "storyPromptPath": _LOCK.get("storyPromptPath") or ""})
            _log("完成：%d 张进 预览/，%d 张进 平台上传/" % (len(landed), len(uploaded)))
        except Exception as e:                               # noqa: BLE001
            _set(stage="失败", running=False,
                 result={"ok": False, "error": str(e)[:300]})
            _log("异常：" + str(e)[:200])

    # 先在调用线程里占住 running，避免用户连点两下、第二个请求在 job 启动前又进来。
    _set(running=True, stage="准备", log=[], started=time.time(),
         channel=ch_name, n=n, result=None, storyPromptPath="")
    threading.Thread(target=_job, daemon=True, name="storyboard-gen").start()
    return {"ok": True, "stage": "started", "channel": ch_name, "n": n}


def api_apply(pdir, b):
    """把「分镜版提示词」设为项目正文（旧稿备份到 文案/_备份/）。

    之后点③栏「让豆包生成视频」：参考图会自动带上 平台上传 里那组分镜图，
    正文也按分镜顺序描述——这就是"按预览图去做视频"。
    """
    if not pdir or not os.path.isdir(pdir):
        return {"ok": False, "error": "没有项目"}
    src = str((b or {}).get("path") or "").strip()
    if not src or not os.path.isfile(src):
        wdir = os.path.join(pdir, "文案")
        cands = []
        if os.path.isdir(wdir):
            cands = sorted([os.path.join(wdir, f) for f in os.listdir(wdir)
                            if f.startswith("分镜提示词-") and f.endswith(".txt")],
                           key=os.path.getmtime)
        if not cands:
            return {"ok": False, "error": "还没有分镜版提示词——先生成一组分镜"}
        src = cands[-1]
    dst = os.path.join(pdir, "文案", "提示词正文.txt")
    try:
        os.makedirs(os.path.join(pdir, "文案", "_备份"), exist_ok=True)
        if os.path.isfile(dst):
            bak = os.path.join(pdir, "文案", "_备份",
                               "提示词正文-%s.txt" % time.strftime("%m%d-%H%M%S"))
            shutil.copy2(dst, bak)
        shutil.copy2(src, dst)
        return {"ok": True, "path": dst, "source": os.path.basename(src)}
    except OSError as e:
        return {"ok": False, "error": str(e)[:200]}
