# -*- coding: utf-8 -*-
"""工作台 · Web 前端服务端（Python 标准库 http.server，零第三方依赖）。

为什么这么搭：界面用 HTML/CSS 写（好看、好改、agent 也能改），业务逻辑一行不动——
本文件只做三件事：① 把 `project_core` / `score_core` / `agent_bridge` 的现成函数
暴露成 JSON 接口；② 把 `workbench/` 里的静态页面发给浏览器；③ 用 SSE 把 agent 的
实时进度推给页面。

浏览器用系统自带 Edge 的 `--app=` 窗口打开（无地址栏、无工具栏，看起来就是个桌面窗口），
所以不需要打包任何 UI 框架；页面与本地服务只走 127.0.0.1。

接口一览（前端只认这些）：
    GET  /                  静态页面
    GET  /api/state         样本库 / 项目列表 / 当前项目 / 框架 / 状态 / 评分维度
    POST /api/project       选项目
    GET  /api/prompts       读提示词与即梦上传清单
    POST /api/pick          弹系统文件/目录选择框（Tk，跑在专用线程里）
    POST /api/intake        投放素材（路径列表）→ 自动判角色
    POST /api/intake/go     建框架 + 归类
    POST /api/upload        浏览器拖进来的文件（原样字节流）
    POST /api/receive       收成片 / 废片
    POST /api/feedback      写本轮反馈（可顺带叫 agent）
    POST /api/agent         叫 agent 出提示词 / 按反馈重出
    GET  /api/progress      SSE：阶段 + 已用 + 预计 + 日志行
    GET  /api/review        读该项目的评分
    POST /api/score         保存评分
    POST /api/ping          页面心跳（页面关了服务自己退）
"""
import datetime
import importlib.util
import json
import os
import queue
import re
import shutil
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = getattr(sys, "_MEIPASS", HERE)
WEB_DIR = os.path.join(BASE, "workbench")
if not os.path.isdir(WEB_DIR):
    WEB_DIR = os.path.join(HERE, "workbench")


def _load_core(fname, modname):
    for d in (BASE, HERE):
        p = os.path.join(d, fname)
        if os.path.isfile(p):
            spec = importlib.util.spec_from_file_location(modname, p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    return None


pcore = _load_core("project_core.py", "project_core")
score = _load_core("score_core.py", "score_core")
abridge = _load_core("agent_bridge.py", "agent_bridge")


# ── 系统文件选择框（Tk 只能在同一个线程里用，所以单开一个线程常驻）──────────
class Picker:
    """专用线程里跑一个隐藏 Tk 根窗口，主线程通过队列请它弹对话框。"""

    def __init__(self):
        self.q = queue.Queue()
        self.ready = threading.Event()
        self.t = threading.Thread(target=self._loop, daemon=True)
        self.t.start()
        self.ready.wait(6)

    def _loop(self):
        import tkinter as tk
        from tkinter import filedialog
        try:
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
        except Exception:                                        # noqa: BLE001
            return
        self.root = root
        self.ready.set()
        while True:
            job = self.q.get()
            if job is None:
                break
            kind, reply = job
            try:
                if kind == "dir":
                    out = filedialog.askdirectory(title="选文件夹")
                    reply["paths"] = [out] if out else []
                else:
                    out = filedialog.askopenfilenames(title="选素材（可多选）")
                    reply["paths"] = list(out)
            except Exception as e:                               # noqa: BLE001
                reply["error"] = str(e)
            reply["event"].set()

    def ask(self, kind="files", timeout=300):
        if not self.ready.is_set():
            return {"error": "本机弹不出文件选择框（Tk 不可用）"}
        reply = {"event": threading.Event(), "paths": []}
        self.q.put((kind, reply))
        reply["event"].wait(timeout)
        reply.pop("event")
        return reply


_picker = None
_WIN = None          # pywebview 窗口句柄（切经典界面时要主动关掉它）


def picker():
    global _picker
    if _picker is None:
        _picker = Picker()
    return _picker


# ── 业务胶水：把 core 的现成函数拼成前端要的形状 ────────────────────────────
def _proj_state(pdir):
    st = {}
    if pcore:
        try:
            st = pcore.read_state(pdir) or {}
        except Exception:                                        # noqa: BLE001
            st = {}
    return st


def _scan_project(pdir):
    """一个项目目录 → {素材, 文案, 成片, 废片, 评价} 清单 + 框架.json 摘要。"""
    out = {"dir": pdir, "name": os.path.basename(pdir.rstrip("\\/")), "folders": {},
           "framework": {}, "state": _proj_state(pdir)}
    fw = os.path.join(pdir, "框架.json")
    if os.path.isfile(fw):
        try:
            out["framework"] = json.load(open(fw, encoding="utf-8"))
        except (ValueError, OSError):
            out["framework"] = {}
    try:                      # 项目创建时间（Windows 上 st_ctime 就是创建时间）
        out["createdAt"] = datetime.datetime.fromtimestamp(
            os.stat(pdir).st_ctime).strftime("%m-%d %H:%M")
    except OSError:
        out["createdAt"] = ""
    for name in ("素材", "文案", "成片", "废片", "评价", "备注", "即梦上传", "废片原因"):
        d = os.path.join(pdir, name)
        items = []
        if os.path.isdir(d):
            for fn in sorted(os.listdir(d)):
                p = os.path.join(d, fn)
                try:
                    sz = os.path.getsize(p) if os.path.isfile(p) else 0
                except OSError:
                    sz = 0
                items.append({"name": fn, "size": sz, "dir": os.path.isdir(p)})
        out["folders"][name] = items
    return out


def _samples(root):
    if not root or not os.path.isdir(root):
        return []
    rows = []
    for fn in sorted(os.listdir(root)):
        p = os.path.join(root, fn)
        if not os.path.isdir(p) or fn.startswith("_"):
            continue
        try:
            mats = len(os.listdir(os.path.join(p, "素材")))
        except OSError:
            mats = 0
        gen = bad = 0
        for d, tgt in (("成片", "gen"), ("废片", "bad")):
            try:
                n = len([x for x in os.listdir(os.path.join(p, d)) if not x.startswith(".")])
            except OSError:
                n = 0
            if tgt == "gen":
                gen = n
            else:
                bad = n
        try:
            rev = len([x for x in os.listdir(os.path.join(p, "评价")) if x.endswith(".json")])
        except OSError:
            rev = 0
        rows.append({"name": fn, "dir": p, "materials": mats, "videos": gen,
                     "rejects": bad, "reviews": rev})
    return rows


def detect_root():
    if pcore:
        for fn in ("detect_root", "detect_samples_root"):
            f = getattr(pcore, fn, None)
            if f:
                try:
                    r = f(None)
                    if r:
                        return r
                except Exception:                                # noqa: BLE001
                    pass
                try:
                    r = f()
                    if r:
                        return r
                except Exception:                                # noqa: BLE001
                    pass
    return ""


def dims_payload():
    d = {"dims": [], "forbid": [], "concl": ["可用", "可改", "作废"],
         "forbid_default": "无", "forbid_on": "有"}
    if score:
        try:
            d["dims"] = [{"key": k, "options": list(o), "hint": s}
                         for k, o, s in score.DIMS]
        except Exception:                                        # noqa: BLE001
            pass
        try:
            d["forbid"] = [k for k, _s in score.FORBID]
        except Exception:                                        # noqa: BLE001
            pass
    return d


def build_info():
    src = sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__)
    try:
        stamp = datetime.datetime.fromtimestamp(os.path.getmtime(src)).strftime("%m-%d %H:%M")
    except OSError:
        stamp = "?"
    return {"path": src, "stamp": stamp, "frozen": bool(getattr(sys, "frozen", False))}


def prompts_payload(pdir):
    """③栏读的东西：**只给提示词正文** + 即梦上传清单 + 状态里的阶段。

    口径（2026-09-16 用户裁定后改）：`框架.json → project.prompts` 是提示词正文；`文案/` 下
    **只有文件名里带「提示词」的**才算（agent 有时写在 `文案/提示词.txt`）；其余 `文案/*.txt|md`
    是**文案稿（输入）**，另放 `wenan`——从前把它也算提示词，结果没出提示词时③栏显示的是文案稿，
    用户看到的是"提示词正文里塞了文案"（2026-09-16 截图指出）。
    """
    out = {"prompts": [], "uploads": [], "wenan": [], "need": "",
           "state": _proj_state(pdir)}
    sk = None
    if pcore:
        try:
            sk = pcore.load_skeleton(pdir)
        except Exception:                                        # noqa: BLE001
            sk = None
    proj = ((sk or {}).get("project") or {}) if isinstance(sk, dict) else {}
    out["need"] = proj.get("need") or ""
    for pr in (proj.get("prompts") or []):
        if isinstance(pr, dict):
            out["prompts"].append({"ver": pr.get("name") or "提示词",
                                   "text": pr.get("text") or "",
                                   "ratio": pr.get("ratio") or "",
                                   "type": pr.get("type") or "",
                                   "refs": pr.get("refs") or {}})
        elif isinstance(pr, str):
            out["prompts"].append({"ver": "提示词", "text": pr})
    wenan = os.path.join(pdir, "文案")
    if os.path.isdir(wenan):
        for fn in sorted(os.listdir(wenan)):
            if not fn.lower().endswith((".txt", ".md")):
                continue
            p = os.path.join(wenan, fn)
            try:
                body = open(p, encoding="utf-8-sig").read().strip()
            except OSError:
                continue
            if "提示词" in fn:
                out["prompts"].append({"ver": "文案/" + fn, "text": body})
            else:
                try:
                    sz = os.path.getsize(p)
                except OSError:
                    sz = 0
                out["wenan"].append({"name": fn, "size": sz,
                                     "text": body[:2000]})
    up = os.path.join(pdir, "即梦上传")
    if os.path.isdir(up):
        for fn in sorted(os.listdir(up)):
            p = os.path.join(up, fn)
            if os.path.isfile(p):
                try:
                    out["uploads"].append({"name": fn, "size": os.path.getsize(p)})
                except OSError:
                    out["uploads"].append({"name": fn, "size": 0})
    return out


def materials_payload(pdir):
    """②栏「文案/素材识别」的清单。

    来源是 `框架.json → project.materials`——**这份数组的顺序就是引用编号的顺序**
    （agent 按它排 @图片1 / @音频1），所以②栏里拖动排序改的就是它。
    另外兜底扫描 `文案/` `素材/` 里没登记进框架的文件（agent 或人手拷进去的）。
    """
    rows = []
    sk = None
    if pcore:
        try:
            sk = pcore.load_skeleton(pdir)
        except Exception:                                        # noqa: BLE001
            sk = None
    proj = ((sk or {}).get("project") or {}) if isinstance(sk, dict) else {}
    seen = set()
    for m in (proj.get("materials") or []):
        if not isinstance(m, dict):
            continue
        rel = (m.get("file") or "").replace("\\", "/")
        name = m.get("name") or os.path.basename(rel)
        fp = os.path.join(pdir, rel.replace("/", os.sep)) if rel else ""
        size = m.get("size") or 0
        exists = bool(fp) and os.path.isfile(fp)
        if exists:
            try:
                size = os.path.getsize(fp)
            except OSError:
                pass
        seen.add(name)
        rows.append({"id": m.get("id") or "", "file": rel, "name": name,
                     "role": m.get("role") or "", "size": size,
                     "type": m.get("type") or "", "note": m.get("note") or "",
                     "missing": bool(rel) and not exists})
    for sub in ("文案", "素材"):
        d = os.path.join(pdir, sub)
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            fp = os.path.join(d, fn)
            if not os.path.isfile(fp) or fn in seen:
                continue
            try:
                sz = os.path.getsize(fp)
            except OSError:
                sz = 0
            rows.append({"id": "", "file": "%s/%s" % (sub, fn), "name": fn, "role": "",
                         "size": sz, "type": "", "note": "还没登记进框架", "missing": False})
    return rows


def proj_need(pdir):
    """用户填的「简单需求」（框架.json → project.need）：agent 出提示词时必须按它来。"""
    if not pcore or not pdir:
        return ""
    try:
        sk = pcore.load_skeleton(pdir)
    except Exception:                                            # noqa: BLE001
        return ""
    return (((sk or {}).get("project") or {}).get("need") or "").strip()


# ── agent 进度：把 stdout 实时转成阶段事件 ─────────────────────────────────
STAGES = [("读技能 / 框架", 15), ("写提示词", 65), ("落即梦上传", 15), ("写回执", 5)]
_PROG = {"job": 0, "stage": 0, "pct": 0, "lines": [], "done": False, "ok": None,
         "text": "", "t0": 0, "eta": 180, "running": False}
_PROG_LOCK = threading.Lock()


def _hist_eta(pdir):
    """按本项目历史耗时估剩余（中位数），没有历史就按 180 秒。"""
    p = os.path.join(pdir, "_会话", "耗时.jsonl")
    vals = []
    if os.path.isfile(p):
        for line in open(p, encoding="utf-8", errors="replace"):
            try:
                v = float(json.loads(line).get("seconds") or 0)
            except (ValueError, TypeError):
                v = 0
            if 5 < v < 3600:
                vals.append(v)
    if not vals:
        return 180.0, 0
    vals.sort()
    return vals[len(vals) // 2], len(vals)


def _record_eta(pdir, seconds, ok):
    d = os.path.join(pdir, "_会话")
    try:
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "耗时.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": datetime.datetime.now().isoformat(timespec="seconds"),
                                "seconds": round(seconds, 1), "ok": bool(ok)},
                               ensure_ascii=False) + "\n")
    except OSError:
        pass


def _stage_from_line(line, cur):
    """把 agent 的一行输出映射到阶段。工具名/文件路径是最可靠的信号。"""
    low = line.lower()
    if "receipt" in low or "回执" in line:
        return 3
    if "即梦上传" in line:
        return max(cur, 2)
    if "框架.json" in line or "提示词" in line or "prompt" in low:
        return max(cur, 1)
    if "skill.md" in low or "读" in line and cur == 0:
        return 0
    return cur


def start_agent(pdir, what, feedback=""):
    """起一个 agent 任务（后台线程），进度写进 _PROG，页面用 SSE 读。

    **先查通道再起任务**：没有 agent（或本机没一个可用）时直接返回原因，
    别起一个注定失败的"假进度"——界面上按钮本来就是灰的，这里是第二道闸。
    """
    if not abridge:
        return {"error": "agent 通道不可用：缺 agent_bridge.py"}
    try:
        ok, why = abridge.available()
    except Exception as e:                                       # noqa: BLE001
        return {"error": "agent 通道探测失败：%s" % e}
    if not ok:
        return {"error": "agent 通道不可用：%s" % why}
    with _PROG_LOCK:
        if _PROG["running"]:
            return {"error": "已经有一个任务在跑"}
        _PROG.update({"job": _PROG["job"] + 1, "stage": 0, "pct": 0, "lines": [],
                      "done": False, "ok": None, "text": "", "t0": time.time(),
                      "running": True})
        eta, n = _hist_eta(pdir)
        _PROG["eta"] = eta
        _PROG["eta_n"] = n
        job = _PROG["job"]
    st = _proj_state(pdir)
    skill_md = os.path.join(os.path.dirname(HERE), "SKILL.md")
    sid = st.get("agentSession") or None
    need = proj_need(pdir)
    head = ("先读技能说明 %s 再动手（本机已装，直接读文件即可）。\n\n" % skill_md)
    if need:
        # 用户填的「简单需求」是这次任务的硬约束，放在最前面
        head += "用户需求（**必须按它来**，与技能默认口径冲突时先满足它并在回执里说清）：\n%s\n\n" % need
    if what == "feedback":
        todo = (head +
                "用户在界面上给了第 %s 轮反馈：\n%s\n\n"
                "请按反馈重出一版提示词：更新 %s\\文案\\ 下的稿子与 框架.json 的 prompts，"
                "并把要上传的文件副本放进 即梦上传\\（含上传说明.txt）。"
                "完成后跑 tools\\project_core.py --project \"%s\" --receipt \"改了什么\" "
                "--todo-done 写回执。" % (st.get("round", 1), feedback, pdir, pdir))
    elif what == "intake":
        # 归类复核（2026-09-16 用户指出：光靠文件名/扩展名猜角色不够准，这一步要 agent 来）
        todo = (head +
                "用户点了「核对归类」。软件只按文件名/扩展名猜了角色，**不一定准**。请打开 "
                "%s\\框架.json 的 materials、以及 素材/ 与 文案/ 里的实际文件，逐件核对每件的 role"
                "（形象参考 / 音色参考 / 参考视频 / 文案 / 道具 / 场景 …）："
                "能读到内容的按内容判（图片看画面、音频看用途、文本看体裁），拿不准的**标成「待确认」"
                "并在回执里说清为什么**，别硬猜。把修正后的 role 写回 框架.json，"
                "并把要上传的文件副本按引用编号放进 即梦上传\\（含上传说明.txt）。"
                "完成后跑 tools\\project_core.py --project \"%s\" "
                "--receipt \"归类复核：改了哪几件、依据是什么\" --todo-done 写回执。"
                % (pdir, pdir))
    elif what == "learn":
        # 评价反哺 skill（2026-09-16 用户指出：④⑤两栏的结论要回到 skill 里，闭环才成立）
        todo = (head +
                "用户点了「评价反哺 skill」。请读：\n"
                "  · %s\\评价\\*.json（六维 / 违禁项 / 整体评分 / 结论 / 备注）\n"
                "  · %s\\废片\\ 的文件名（废因写在文件名里）\n"
                "  · %s\\_会话\\回执.jsonl（这一轮 agent 自己报的做了什么）\n\n"
                "把**可复用的经验**沉淀回技能，别写流水账：本机个人经验写 "
                "references\\rules.local.md，跨机通用规则写 references\\rules.md"
                "（只追加或修订自己的条目，别重排/改写别人的规则；规则只写「规则 + 依据（日期/来源）」）。"
                "已经吸收过的评价别重复写（可对照 tools\\评价回收.py 的账本）。"
                "完成后跑 tools\\project_core.py --project \"%s\" "
                "--receipt \"反哺：改了哪几条规则、依据哪条评价\" --todo-done 写回执。"
                % (pdir, pdir, pdir, pdir))
    else:
        todo = (head +
                "用户点了「叫 agent 出提示词」。请扫描 %s\\ 下的 框架.json 与 素材/、文案/，"
                "按 skill 规则出一版提示词，写回 框架.json 的 prompts 与 文案/，"
                "并把要上传的文件副本按引用编号放进 即梦上传\\；"
                "完成后跑 tools\\project_core.py --project \"%s\" --receipt \"改了什么\" "
                "--todo-done 写回执。" % (pdir, pdir))

    def run():
        t0 = time.time()
        try:
            res = abridge.ask(todo, session_id=sid, cwd=pdir, timeout=1800,
                              permission_mode="bypassPermissions",
                              on_line=lambda ln: _push_line(ln))
        except Exception as e:                                   # noqa: BLE001
            res = {"ok": False, "error": "起不来：%s" % e}
        usec = time.time() - t0
        _record_eta(pdir, usec, res.get("ok"))
        try:
            if res.get("session_id") and res["session_id"] != sid:
                pcore.write_state(pdir, agentSession=res["session_id"])
            if res.get("ok"):
                pcore.push_receipt(pdir, res.get("text", ""), kind="出提示词")
                pcore.mark_todos_done(pdir)
                _push_line("✅ agent 回来了：" + (res.get("text") or "")[:400])
            else:
                _push_line("!! agent 没跑成：" + (res.get("error") or "")[:400])
        except Exception as e:                                   # noqa: BLE001
            _push_line("!! 回写失败：%s" % e)
        with _PROG_LOCK:
            _PROG.update({"done": True, "ok": bool(res.get("ok")), "running": False,
                          "pct": 100 if res.get("ok") else _PROG["pct"],
                          "text": res.get("text") or res.get("error") or "",
                          "seconds": round(usec, 1)})
    threading.Thread(target=run, daemon=True).start()
    return {"job": job, "eta": _PROG["eta"], "eta_n": _PROG.get("eta_n", 0)}


def _push_line(line):
    line = (line or "").rstrip()
    if not line:
        return
    with _PROG_LOCK:
        _PROG["lines"].append(line)
        if len(_PROG["lines"]) > 400:
            del _PROG["lines"][:-400]
        _PROG["stage"] = _stage_from_line(line, _PROG["stage"])
        # 百分比 = max(已完成阶段的权重和, 按历史耗时推算的时间进度)，封顶 96%
        # （不封 100：真正到 100 只发生在任务结束时，进度条才不会"卡在 100 还在等"）
        acc = sum(w for i, (_n, w) in enumerate(STAGES) if i < _PROG["stage"])
        by_time = 0
        if _PROG["t0"]:
            el = time.time() - _PROG["t0"]
            by_time = int(el / max(1.0, _PROG["eta"]) * 100)
        _PROG["pct"] = min(96, max(acc, by_time))


def progress_snapshot(since=0):
    with _PROG_LOCK:
        el = time.time() - _PROG["t0"] if _PROG["t0"] else 0
        d = {"job": _PROG["job"], "stage": _PROG["stage"],
             "stageName": STAGES[min(_PROG["stage"], 3)][0],
             "pct": _PROG["pct"], "elapsed": round(el), "eta": round(_PROG["eta"]),
             "running": _PROG["running"], "done": _PROG["done"], "ok": _PROG["ok"],
             "lines": _PROG["lines"][since:], "n": len(_PROG["lines"]),
             "text": _PROG["text"]}
        return d


# ── HTTP ───────────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    server_version = "HeronBoWorkbench/1.0"
    last_ping = time.time()
    root = ""

    def log_message(self, *a):                                   # 静默：别刷控制台
        pass

    # ---- 小工具 ----
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if not n:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return {}

    def _proj(self, payload=None):
        """当前项目目录：请求里带的优先，否则用服务端记着的。"""
        if payload and payload.get("project"):
            return payload["project"]
        return read_proj()

    def _set_proj(self, pdir):
        write_proj(pdir)

    # ---- 静态 ----
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/state":
            return self._json(self.state())
        if path == "/api/prompts":
            return self._json(prompts_payload(self._proj()))
        if path == "/api/review":
            return self._json(self.read_review())
        if path == "/api/progress":
            return self.sse()
        if path == "/api/agent/status":
            return self._json(progress_snapshot())
        if path.startswith("/api/upload/") and path.endswith("/open"):
            return self._json({"skip": True})
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        # 静态文件
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        fp = os.path.normpath(os.path.join(WEB_DIR, rel))
        if not fp.startswith(os.path.abspath(WEB_DIR)) or not os.path.isfile(fp):
            self.send_error(404, "not found")
            return
        ctype = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".js": "application/javascript; charset=utf-8",
                 ".svg": "image/svg+xml", ".png": "image/png",
                 ".woff2": "font/woff2"}.get(os.path.splitext(fp)[1], "application/octet-stream")
        data = open(fp, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # ---- POST ----
    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/ping":
            Handler.last_ping = time.time()
            return self._json({"ok": True})
        if path == "/api/project":
            b = self._body()
            d = b.get("dir") or ""
            if d and os.path.isdir(d):
                self._set_proj(d)
                return self._json({"ok": True, "project": _scan_project(d)})
            return self._json({"ok": False, "error": "目录不存在：%s" % d}, 400)
        if path == "/api/project/new":
            return self._json(self.new_project(self._body()))
        if path == "/api/need":
            return self._json(self.save_need(self._body()))
        if path == "/api/material/remove":
            return self._json(self.remove_material(self._body()))
        if path == "/api/material/order":
            return self._json(self.order_materials(self._body()))
        if path == "/api/pick":
            b = self._body()
            return self._json(picker().ask(b.get("kind") or "files"))
        if path == "/api/root":
            return self._json(self.set_root(self._body()))
        if path == "/api/intake":
            return self._json(self.intake(self._body()))
        if path == "/api/intake/go":
            return self._json(self.intake_go(self._body()))
        if path == "/api/upload":
            return self._json(self.upload())
        if path == "/api/receive":
            return self._json(self.receive(self._body()))
        if path == "/api/feedback":
            return self._json(self.feedback(self._body()))
        if path == "/api/classic":
            return self._json(self.classic())
        if path == "/api/agent/set":
            b = self._body()
            if not abridge:
                return self._json({"ok": False, "error": "缺 agent_bridge.py"})
            ok, why = abridge.set_agent((b.get("key") or "").strip())
            return self._json({"ok": ok, "why": why, "agent": self.agent_ok()})
        if path == "/api/agent":
            b = self._body()
            pdir = b.get("project") or self._proj()
            if not pdir:
                return self._json({"error": "先选项目"}, 400)
            return self._json(start_agent(pdir, b.get("what") or "prompt",
                                          b.get("feedback") or ""))
        if path == "/api/score":
            return self._json(self.save_review(self._body()))
        self.send_error(404, "not found")

    # ---- 各接口实现 ----
    def state(self):
        root = Handler.root or detect_root()
        pdir = self._proj()
        proj = _scan_project(pdir) if pdir and os.path.isdir(pdir) else None
        d = dims_payload()
        d.update({"root": root, "samples": _samples(root), "project": proj,
                  "build": build_info(), "agent": self.agent_ok(),
                  "theme": self.theme_name(), "stages": [n for n, _w in STAGES],
                  "materials": materials_payload(pdir) if (pdir and os.path.isdir(pdir)) else [],
                  # 待投放的素材以**服务端为准**：页面一刷新，前端的 S.pending 就空了，
                  # 但服务端还留着（不然"建框架归类"会误判成"还没丢素材"，2026-09-16 实测踩到）
                  "pending": list(self.server.pending or []),
                  "need": proj_need(pdir) if (pdir and os.path.isdir(pdir))
                          else (self.server.need or "")})
        d.update(self.root_issue(root))
        return d

    # ---- ①栏「＋ 新建项目」/ ②栏「需求 · 素材增删排序」----------------------
    def new_project(self, b):
        """给一张白纸：空骨架 + 空提示词 + 空素材，并切过去。

        为什么要有（2026-09-16 用户要求）：工作台记着"上次打开的项目"，用旧项目干活时它身上
        已经堆满了东西，想从干净状态开始得自己去样本库翻。名字留空就自动起一个。
        register=False：建项目**不动**全局样本库根（换根只走①栏「选样本库根」）。
        """
        name = (b.get("name") or "").strip() or ("新项目-" + time.strftime("%m%d-%H%M"))
        root = b.get("root") or Handler.root or detect_root()
        if not (pcore and root):
            return {"ok": False, "error": "还没有样本库根，先在①栏选一个目录"}
        try:
            pdir, _sk, _log = pcore.build_skeleton(root, name, register=False)
        except Exception as e:                                   # noqa: BLE001
            return {"ok": False, "error": "建项目失败：%s" % e}
        try:                       # 规则第 270 条：交付提示词时必须同步建 即梦上传/
            os.makedirs(os.path.join(pdir, "即梦上传"), exist_ok=True)
        except OSError:
            pass
        self._set_proj(pdir)
        return {"ok": True, "dir": pdir, "project": _scan_project(pdir)}

    def save_need(self, b):
        """用户的「简单需求」：写进 框架.json→project.need 与 备注/需求.txt。

        为什么要落盘：agent 出提示词/复核归类时都读 框架.json 与 备注/，需求必须跟项目在一起，
        不能只活在页面里（关掉就没了）。
        """
        text = (b.get("text") or "").strip()
        pdir = self._proj(b)
        if not (pdir and os.path.isdir(pdir)):
            self.server.need = text          # 还没建项目：先记着，建框架时带上
            return {"ok": True, "pending": True, "need": text}
        if not pcore:
            return {"ok": False, "error": "缺 project_core.py"}
        sk = pcore.load_skeleton(pdir)
        if not isinstance(sk, dict):
            return {"ok": False, "error": "读不到 框架.json"}
        sk.setdefault("project", {})["need"] = text
        pcore.save_skeleton(pdir, sk)
        try:
            d = os.path.join(pdir, "备注")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "需求.txt"), "w", encoding="utf-8", newline="\n") as f:
                f.write(text + "\n")
        except OSError:
            pass
        return {"ok": True, "need": text}

    def remove_material(self, b):
        """②栏素材的「移除」。

        - 还没归类的（待投放）→ 直接从待投放里拿掉；
        - 已归类的 → 从 框架.json 的 materials 里删掉，文件**移到项目里的 `_已移除/`**
          （不真删：素材是用户辛苦攒的，误删要能捞回来），`即梦上传/` 下的同名副本一并挪走。
        """
        f = (b.get("file") or "").strip().replace("\\", "/")
        name = (b.get("name") or "").strip()
        if b.get("pending"):
            pend = self._pending()
            for p_ in list(pend):
                if p_ == b.get("pending") or os.path.basename(p_) == name:
                    pend.remove(p_)
            return {"ok": True, "pending": pend, "moved": []}
        pdir = self._proj(b)
        if not (pcore and pdir and os.path.isdir(pdir)):
            return {"ok": False, "error": "没有项目"}
        sk = pcore.load_skeleton(pdir)
        if not isinstance(sk, dict):
            return {"ok": False, "error": "读不到 框架.json"}
        mats = (sk.get("project") or {}).get("materials") or []
        keep, moved = [], []
        dest = os.path.join(pdir, "_已移除")
        for m in mats:
            if not isinstance(m, dict):
                continue
            rel = (m.get("file") or "").replace("\\", "/")
            if (f and rel == f) or (not f and name and (m.get("name") or "") == name):
                try:
                    os.makedirs(dest, exist_ok=True)
                    src = os.path.join(pdir, rel.replace("/", os.sep))
                    if os.path.isfile(src):
                        shutil.move(src, os.path.join(dest, os.path.basename(src)))
                        moved.append(rel)
                    up = os.path.join(pdir, "即梦上传", os.path.basename(rel))
                    if os.path.isfile(up):
                        shutil.move(up, os.path.join(dest, os.path.basename(up)))
                        moved.append("即梦上传/" + os.path.basename(rel))
                except OSError as e:
                    return {"ok": False, "error": "移动失败：%s" % e}
                continue
            keep.append(m)
        if len(keep) == len(mats) and not moved:
            return {"ok": False, "error": "框架里没有这件素材：%s" % (f or name)}
        sk["project"]["materials"] = keep
        pcore.save_skeleton(pdir, sk)
        return {"ok": True, "moved": moved, "materials": materials_payload(pdir),
                "project": _scan_project(pdir)}

    def order_materials(self, b):
        """②栏拖动排序：按传来的顺序重写 框架.json 的 materials。

        **顺序有意义**：agent 按这份数组的先后排 @图片1 / @音频1，用户想要的引用编号顺序
        就靠它（2026-09-16 用户要求"可以拖动素材顺序"）。
        """
        order = [str(x).replace("\\", "/") for x in (b.get("files") or [])]
        pdir = self._proj(b)
        if not (pcore and pdir and os.path.isdir(pdir)):
            return {"ok": False, "error": "没有项目"}
        sk = pcore.load_skeleton(pdir)
        if not isinstance(sk, dict):
            return {"ok": False, "error": "读不到 框架.json"}
        mats = [m for m in ((sk.get("project") or {}).get("materials") or [])
                if isinstance(m, dict)]
        idx = {o: i for i, o in enumerate(order)}
        mats.sort(key=lambda m: idx.get((m.get("file") or "").replace("\\", "/"), len(idx)))
        sk["project"]["materials"] = mats
        pcore.save_skeleton(pdir, sk)
        return {"ok": True, "materials": materials_payload(pdir)}

    def root_issue(self, root):
        """样本库根的问题要说清楚：**配置指向的目录不存在**时最容易被误判成"没配"。

        2026-09-15 实踩：paths.local.md 被写成某个临时目录（测试脚本用 auto_build 时
        顺手改的），临时目录一清，界面就只剩"未配置样本库根 / 0 个样本"——用户
        根本不知道发生了什么。这里把"配了什么、为什么不可用"原样报出来。
        """
        out = {"rootConfigured": "", "rootIssue": ""}
        if pcore:
            try:
                out["rootConfigured"] = (pcore.read_local() or {}).get("SAMPLES_ROOT") or ""
            except Exception:                                    # noqa: BLE001
                pass
        cfg = out["rootConfigured"]
        if root and os.path.isdir(root):
            return out
        if cfg and not os.path.isdir(cfg):
            out["rootIssue"] = "配置里的样本库根不存在（可能被删/被改）：%s" % cfg
        elif not cfg:
            out["rootIssue"] = "还没配置样本库根——选一个目录，我会记进 references/paths.local.md"
        return out

    def agent_ok(self):
        """界面用：选中的 agent + 候选清单（哪个装了本技能、哪个可用）。

        "exe 跟着 skill 走"就体现在这里：exe 先看本机哪些 agent 把本技能装在自己
        名下（~/.zcode / ~/.codex / ~/.dsh / ~/.workbuddy 的 skills/<技能名>），
        再挑一个可用的来干活，并把理由显示给用户。
        """
        if not abridge:
            return {"ok": False, "why": "缺 agent_bridge.py", "picked": None, "list": []}
        try:
            info = abridge.agent_info()
            info["ok"] = bool(info.get("picked"))
            return info
        except Exception as e:                                   # noqa: BLE001
            return {"ok": False, "why": str(e), "picked": None, "list": []}

    def theme_name(self):
        p = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                         "HeronBoScoreTool", "theme.txt")
        try:
            return open(p, encoding="utf-8").read().strip() or "浅色"
        except OSError:
            return "浅色"

    def _pending(self):
        return self.server.pending

    def classic(self):
        """切到经典 Tk 界面：另起一个 --classic 进程，然后本窗口关掉、服务退出。

        为什么这么做而不是"页面上换个皮"：经典界面是独立的 Tk 程序，只能另起进程；
        而两个界面同时开着会争同一个"当前项目/未保存草稿"，所以起完就把这边收掉。
        """
        import subprocess
        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "--classic"]
        else:
            w = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
            cmd = [(w if os.path.isfile(w) else sys.executable),
                   os.path.join(HERE, "score_gui.pyw"), "--classic"]
        kw = {"cwd": HERE}
        if os.name == "nt":
            kw["creationflags"] = 0x08000000          # 别弹控制台窗口
            kw["close_fds"] = True
        try:
            subprocess.Popen(cmd, **kw)
        except OSError as e:
            return {"ok": False, "error": "经典界面起不来：%s" % e}

        def _bye():
            """能关就把本窗口关掉；**关不掉就留着**（pywebview 的 destroy 从服务线程调用
            对 WinForms 不生效——2026-09-15 实测窗口没关掉）。留着时页面会提示"可关掉了"，
            服务继续跑，用户自己关窗后心跳超时自动退出。"""
            import time as _t
            _t.sleep(1.0)
            closed = False
            try:
                if _WIN is not None:
                    _WIN.destroy()
                    closed = True
            except Exception:                            # noqa: BLE001
                closed = False
            if closed:
                try:
                    self.server.shutdown()
                except Exception:                        # noqa: BLE001
                    pass
        threading.Thread(target=_bye, daemon=True).start()
        return {"ok": True, "note": "已切到经典界面，本窗口稍后自动关闭"}


    def set_root(self, b):
        """改样本库根：写进技能仓库的 references/paths.local.md（本机取值，不进 git）。"""
        d = (b.get("dir") or "").strip()
        if not d or not os.path.isdir(d):
            return {"ok": False, "error": "目录不存在：%s" % d}
        d = os.path.abspath(d)
        if not pcore:
            return {"ok": False, "error": "缺 project_core.py"}
        try:
            pcore.write_local(SAMPLES_ROOT=d)
        except Exception as e:                                   # noqa: BLE001
            return {"ok": False, "error": "写配置失败：%s" % e}
        Handler.root = d
        return {"ok": True, "root": d,
                "samples": _samples(d), "wrote": pcore.LOCAL_MD}

    def intake(self, b):
        paths = b.get("paths") or []
        pend = self._pending()
        for p in paths:
            p = os.path.normpath(p)
            if p and (os.path.isfile(p) or os.path.isdir(p)) and p not in pend:
                pend.append(p)
        plan = None
        if pcore and pend:
            try:
                plan = pcore.auto_plan(pend)
            except Exception as e:                               # noqa: BLE001
                return {"ok": False, "error": "规划失败：%s" % e, "pending": pend}
        return {"ok": True, "pending": pend, "plan": plan}

    def intake_go(self, b):
        pend = self._pending()
        if not (pcore and pend):
            return {"ok": False, "error": "还没有待投放的素材"}
        root = b.get("root") or Handler.root or detect_root()
        if not root:
            return {"ok": False, "error": "没有样本库根，请先选"}
        try:
            pdir, plan, _log = pcore.auto_build(
                list(pend), root=root, name=b.get("name") or None,
                platform=b.get("platform") or "",
                # register=False：**建项目时别顺手改全局样本库根**。2026-09-15/16 两次踩到——
                # 用临时目录跑这一步，SAMPLES_ROOT 就被写成临时目录，临时目录一清，界面
                # 只剩「配置里的样本库根不存在」。要换根只有一处：界面「选样本库根」→ /api/root。
                register=False,
                on_log=lambda s: _push_line("[归类] " + str(s)))
        except Exception as e:                                   # noqa: BLE001
            return {"ok": False, "error": "建框架失败：%s" % e}
        if pdir and os.path.isdir(pdir):
            self._set_proj(pdir)
            del pend[:]
            # 建项目前填的「简单需求」带进新项目（用户可能先写需求再丢素材）
            if (self.server.need or "").strip():
                try:
                    self.save_need({"text": self.server.need, "project": pdir})
                except Exception:                                # noqa: BLE001
                    pass
        return {"ok": bool(pdir), "project": _scan_project(pdir) if pdir else None,
                "plan": {"final_name": (plan or {}).get("final_name"),
                         "count": (plan or {}).get("count")}}

    def upload(self):
        """浏览器拖进来的文件：请求体就是文件字节，名字放 X-File-Name（URL 编码）。

        两种投递方式都要认：
        - `fetch(url, {body: file})` → 带 Content-Length（常规路径）；
        - `fetch(url, {body: file.stream(), duplex:"half"})` → **分块传输、没有
          Content-Length**，这时读到 EOF 为止（2026-09-15：前端先前用流式上传
          但没给 duplex，请求直接抛错 → 界面弹"投递失败"，两处都修了）。
        """
        name = urllib.parse.unquote(self.headers.get("X-File-Name") or "")
        pdir = self._proj()
        if not (name and pdir):
            return {"ok": False, "error": "缺少文件名或项目"}
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        chunked = (not n) and ("chunked" in (self.headers.get("Transfer-Encoding") or ""))
        dest_dir = os.path.join(pdir, "素材")
        os.makedirs(dest_dir, exist_ok=True)
        target = os.path.join(dest_dir, os.path.basename(name))
        base, ext = os.path.splitext(target)
        i = 1
        while os.path.exists(target):
            target = "%s(%d)%s" % (base, i, ext)
            i += 1
        left = n
        total = 0
        with open(target + ".part", "wb") as f:
            if chunked:                          # 无 Content-Length：读到 EOF
                while True:
                    chunk = self.rfile.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    total += len(chunk)
                    if total > (8 << 30):        # 8GB 上限，防呆
                        break
            else:
                while left > 0:
                    chunk = self.rfile.read(min(1 << 20, left))
                    if not chunk:
                        break
                    f.write(chunk)
                    left -= len(chunk)
                    total += len(chunk)
        if total == 0:
            try:
                os.remove(target + ".part")
            except OSError:
                pass
            return {"ok": False, "error": "没收到文件内容（前端要带 Content-Length 或用 duplex 流式）"}
        os.replace(target + ".part", target)
        pend = self._pending()
        if target not in pend:
            pend.append(target)
        return {"ok": True, "saved": target, "pending": pend}

    def receive(self, b):
        pdir = b.get("project") or self._proj()
        paths = b.get("paths") or []
        good = bool(b.get("good"))
        if not (pcore and pdir and paths):
            return {"ok": False, "error": "需要项目 + 至少一个文件"}
        try:
            placed, _ = pcore.accept_deliverables(
                pdir, list(paths), verdict="good" if good else "bad",
                note=b.get("note") or "", on_log=lambda s: _push_line("[接收] " + s))
        except Exception as e:                                   # noqa: BLE001
            return {"ok": False, "error": str(e)}
        return {"ok": True, "placed": placed,
                "project": _scan_project(pdir) if pdir else None}

    def feedback(self, b):
        pdir = b.get("project") or self._proj()
        text = (b.get("text") or "").strip()
        if not (pcore and pdir and text):
            return {"ok": False, "error": "先选项目、写点反馈"}
        try:
            n = pcore.new_round(pdir, text)
        except Exception as e:                                   # noqa: BLE001
            return {"ok": False, "error": "写待办失败：%s" % e}
        out = {"ok": True, "round": n}
        if b.get("callAgent"):
            out["agent"] = start_agent(pdir, "feedback", text)
        return out

    def read_review(self):
        pdir = self._proj()
        if not (pdir and os.path.isdir(os.path.join(pdir, "评价"))):
            return {"review": None}
        d = os.path.join(pdir, "评价")
        js = sorted(x for x in os.listdir(d) if x.endswith(".json"))
        if not js:
            return {"review": None}
        try:
            return {"review": json.load(open(os.path.join(d, js[-1]), encoding="utf-8")),
                    "file": js[-1]}
        except (ValueError, OSError):
            return {"review": None}

    def save_review(self, b):
        """保存评分：口径与 Tk 版**完全一致**（同一个 score_core.save，同一个 json 结构）。

        score_core.save 要的是「样本库根 + 样本名」而不是项目目录；payload 是
        {六维:{}, 违禁项:{}, 整体评分, 结论, 备注}。
        """
        pdir = b.get("project") or self._proj()
        rev = b.get("review") or {}
        video = b.get("video") or ""
        root = Handler.root or detect_root()
        if not (score and pdir and root and rev):
            return {"ok": False, "error": "缺 score_core / 项目 / 样本库根 / 评分内容"}
        sample = os.path.basename(pdir.rstrip("\\/"))
        if video.startswith("（"):
            video = ""
        try:
            p = score.save(root, sample, video, rev.get("六维") or {},
                           rev.get("违禁项") or {}, rev.get("整体评分") or 4,
                           rev.get("结论") or "可改", rev.get("备注") or "")
        except Exception as e:                                   # noqa: BLE001
            return {"ok": False, "error": str(e)}
        return {"ok": True, "file": p}

    def sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        since = 0
        try:
            while True:
                snap = progress_snapshot(since)
                since = snap["n"]
                payload = json.dumps(snap, ensure_ascii=False)
                self.wfile.write(("data: %s\n\n" % payload).encode("utf-8"))
                self.wfile.flush()
                Handler.last_ping = time.time()
                if snap["done"] and not snap["running"]:
                    time.sleep(0.6)
                    self.wfile.write(b"data: {\"bye\": true}\n\n")
                    self.wfile.flush()
                    break
                time.sleep(0.7)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


# ── 启动 ───────────────────────────────────────────────────────────────────
WIN_TITLE = "HeronBo · AI 视频工作台"     # 网页工作台的窗口标题（经典界面会带「（经典界面）」后缀）


def _http_json(port, path, body=None, timeout=0.8):
    """跟本机已开的实例说一句话（复用/切项目用）。"""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=data,
                                headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def find_window(title=WIN_TITLE):
    """按标题找我们自己那个窗口（返回句柄，没有就 0）。

    **只认完全同名**：经典界面是「…（经典界面）」，不能被当成网页工作台复用。
    """
    if os.name != "nt":
        return 0
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return 0
    u = ctypes.windll.user32
    hit = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _l):
        if not u.IsWindowVisible(hwnd):
            return True
        n = u.GetWindowTextLengthW(hwnd)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(hwnd, buf, n + 1)
            if buf.value.strip() == title:
                hit.append(hwnd)
                return False
        return True

    u.EnumWindows(_cb, 0)
    return hit[0] if hit else 0


def focus_window(hwnd):
    """把已开着的窗口叫到前面（最小化了就先还原）。

    Windows 有前台锁：SetForegroundWindow 可能被拒（那就任务栏闪一下，用户点一下即可），
    但最小化还原（ShowWindow SW_RESTORE）是稳的。
    """
    if not hwnd:
        return False
    try:
        import ctypes
        u = ctypes.windll.user32
        if u.IsIconic(hwnd):
            u.ShowWindow(hwnd, 9)          # SW_RESTORE
        u.SetForegroundWindow(hwnd)
        return True
    except (OSError, AttributeError, ImportError):
        return False


def existing_instance():
    """已经开着的那个实例：(窗口句柄, 端口)；都没有就 (0, 0)。

    两个判据各自独立探：窗口在（标题同名）就算活着；`%TEMP%` 里记的端口还能应答 /api/state
    也算活着。有任何一个就算"已经开着"，不再起第二个（第二个会抢 WebView2 数据目录而起不来）。
    """
    hwnd = find_window()
    port = 0
    try:
        with open(_proj_file(), encoding="utf-8") as f:
            port = int((json.load(f) or {}).get("port") or 0)
    except (ValueError, OSError, TypeError):
        port = 0
    if port:
        try:
            st = _http_json(port, "/api/state")
            if not (isinstance(st, dict) and "build" in st and "agent" in st):
                port = 0
        except (OSError, ValueError):
            port = 0
    return hwnd, port


def native_window(title, url, width=1520, height=940):
    """用 pywebview 开一个**独立窗口**（内嵌 WebView2），返回 True 表示成功接管。

    这是首选路径：窗口属于本程序自己——自己的标题、图标、任务栏条目，没有地址栏；
    也不用借用户正在用的 Edge 浏览器（2026-09-15 用户明确要求"变成独立程序而不是
    依赖 Edge"）。WebView2 **运行时**是系统组件（Win10/11 自带，Teams/微信都在用），
    不是用户的浏览器进程，关掉浏览器不影响它。
    跑不通就返回 False，让调用方退回 Edge app 窗口 / 默认浏览器。
    """
    try:
        import webview
    except ImportError:
        return False
    store = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                         "HeronBoScoreTool", "webview")
    try:
        os.makedirs(store, exist_ok=True)
    except OSError:
        store = None
    try:
        win = webview.create_window(title, url, width=width, height=height,
                                    min_size=(1180, 700), text_select=True)
        global _WIN
        _WIN = win

        def _show():
            """显式 show/restore：实测（2026-09-15）打包后窗口会以**最小化**状态起来
            （任务栏上只有 160x28 一条），不还原的话用户以为程序没打开。"""
            time.sleep(1.2)
            for fn in ("show", "restore", "on_top"):
                try:
                    getattr(win, fn)()
                except Exception:                                # noqa: BLE001
                    pass
        threading.Thread(target=_show, daemon=True).start()
        kw = {}
        if store:
            kw["storage_path"] = store
        try:
            webview.start(**kw)       # 阻塞到窗口关闭
        except TypeError:             # 老版本没有 storage_path
            webview.start()
        return True
    except Exception as e:                                       # noqa: BLE001
        try:
            open(os.path.join(os.environ.get("TEMP", "."), "heronbo_webview_error.log"),
                 "w", encoding="utf-8").write("pywebview 起不来：%r" % (e,))
        except OSError:
            pass
        return False


def find_edge():
    """找系统 Edge 的可执行文件（只在"独立窗口起不来"时当**退路**用）。"""
    import glob as _glob
    cands = [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
             r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"]
    for c in cands:
        if os.path.isfile(c):
            return c
    hits = _glob.glob(os.path.join(os.environ.get("LOCALAPPDATA", ""),
                                   "Microsoft", "Edge", "Application", "msedge.exe"))
    return hits[0] if hits else None


def serve(root="", port=0, host="127.0.0.1"):
    Handler.root = root or ""
    srv = ThreadingHTTPServer((host, port or 0), Handler)
    srv.pending = []
    srv.need = ""            # 用户填的「简单需求」（还没建项目时先记在这，建框架时带进去）
    srv.daemon_threads = True
    return srv


def wait_and_exit(srv, idle=25, total=None):
    """页面关了（心跳停了）就自己退，别留个孤儿进程。"""
    t0 = time.time()
    while True:
        time.sleep(2)
        if time.time() - Handler.last_ping > idle and time.time() - t0 > 12:
            break
        if total and time.time() - t0 > total:
            break
    srv.shutdown()


# ── 「上次打开的项目」记在小文件里，重启后还在同一个项目上 ──────────────────
def _proj_file():
    return os.path.join(os.environ.get("TEMP", "."), "heronbo_workbench.json")


def read_proj():
    try:
        return json.load(open(_proj_file(), encoding="utf-8")).get("project") or ""
    except (ValueError, OSError):
        return ""


def write_proj(pdir, port=None):
    """记下当前项目（顺带记端口，便于排查：临时文件里一眼看到服务在哪个端口）。"""
    try:
        cur = {}
        try:
            cur = json.load(open(_proj_file(), encoding="utf-8")) or {}
        except (ValueError, OSError):
            cur = {}
        cur["project"] = pdir
        if port:
            cur["port"] = int(port)
        json.dump(cur, open(_proj_file(), "w", encoding="utf-8"))
    except OSError:
        pass


def launch_web(root="", project="", hint="", port=0, wait=True, backend=None):
    """起服务 + 开界面窗口。

    窗口优先级（2026-09-15 起）：
      1. **pywebview 独立窗口**（内嵌 WebView2）——自己的窗口，不依赖用户的 Edge 浏览器；
      2. Edge 的 `--app=` 窗口——退路（独立窗口起不来时）；
      3. 默认浏览器打开 URL——最后的退路（至少能用）。
    `backend="edge"` 可强制走退路（排查用）。

    **已经开着一个就不开第二个**（2026-09-15 晚补）：两个实例会抢同一个 WebView2 数据目录
    （`%LOCALAPPDATA%\\HeronBoScoreTool\\webview`），第二个的 pywebview 报
    `0x8007139F 组或资源的状态不是执行请求操作的正确状态` 起不来 —— 双击两次快捷方式就会撞上
    （表现为"没反应"或另开一个 Edge 窗口）。所以动手前先看有没有活着的实例：有就把它的窗口
    叫到前面来（带了 --project 就顺手让它切过去），本进程直接退出。
    `HERONBO_NO_REUSE=1` 可强制另起一个（验证/排查用）。
    """
    import webbrowser
    prev_hwnd, prev_port = (0, 0)
    if not os.environ.get("HERONBO_NO_REUSE"):
        prev_hwnd, prev_port = existing_instance()
    if prev_hwnd or prev_port:
        if project and os.path.isdir(project) and prev_port:
            try:
                _http_json(prev_port, "/api/project", {"dir": project})
            except (OSError, ValueError):
                pass
        if prev_hwnd and focus_window(prev_hwnd):
            print("[工作台] 已经开着一个了，把它叫到前面来（端口 %s）" % (prev_port or "?"),
                  flush=True)
        elif prev_port:
            webbrowser.open("http://127.0.0.1:%d/" % prev_port)
            print("[工作台] 已经开着一个了，用浏览器打开它（端口 %d）" % prev_port, flush=True)
        else:
            focus_window(prev_hwnd)
            print("[工作台] 已经开着一个了（没找到窗口句柄）", flush=True)
        return None, ("http://127.0.0.1:%d/" % prev_port if prev_port else ""), "reuse"
    if project and os.path.isdir(project):
        write_proj(project)
    elif hint:
        r = root or detect_root()
        for s_ in _samples(r):
            if hint in s_["name"]:
                write_proj(s_["dir"])
                break
    srv = serve(root=root or "")
    url = "http://127.0.0.1:%d/" % srv.server_address[1]
    write_proj(read_proj(), port=srv.server_address[1])
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    Handler.last_ping = time.time()
    print("[工作台] %s" % url, flush=True)
    if backend != "edge" and native_window(WIN_TITLE, url):
        srv.shutdown()                 # 窗口关了 → 收摊（不用再靠心跳判断）
        return srv, url, "native"
    # 窗口起不来、而起之前就已经有一个在跑 → 就是它占着 WebView2 的数据目录：别再开 Edge 窗口
    if prev_hwnd or prev_port:
        srv.shutdown()
        if prev_hwnd:
            focus_window(prev_hwnd)
        print("[工作台] 第二个窗口起不来（已有一个在跑），已把它叫到前面来", flush=True)
        return srv, url, "reuse"
    # 退路：Edge app 窗口
    exe = find_edge()
    proc = None
    if exe:
        prof = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                            "HeronBoScoreTool", "webprofile")
        try:
            os.makedirs(prof, exist_ok=True)
        except OSError:
            pass
        try:
            import subprocess as _sp
            proc = _sp.Popen(
                [exe, "--app=" + url, "--user-data-dir=" + prof,
                 "--window-size=1520,940", "--no-first-run",
                 "--no-default-browser-check", "--disable-features=Translate,msEdgeSidebarV2",
                 "--disable-session-crashed-bubble"],
                **({"creationflags": 0x08000000} if os.name == "nt" else {}))
        except OSError:
            proc = None
    if proc is None:
        try:
            webbrowser.open(url)
        except Exception:                                        # noqa: BLE001
            pass
    if wait:
        wait_and_exit(srv)
        try:
            if proc is not None and proc.poll() is None:
                proc.terminate()
        except OSError:
            pass
    return srv, url, "edge" if proc is not None else "browser"


if __name__ == "__main__":
    s = serve()
    print("http://127.0.0.1:%d/" % s.server_address[1])
    try:
        s.serve_forever()
    except KeyboardInterrupt:
        pass
