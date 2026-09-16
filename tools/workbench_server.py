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
import datetime
import glob
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


def recycle_bin_delete(path):
    """把整个目录扔进 **Windows 系统回收站**（ctypes 调 Shell API，不引第三方库）。

    为什么用它：用户要的「回收站」就是资源管理器里那个——能在回收站界面还原、也能被
    「清空回收站」真删掉，而且样本库目录里不留残渣。失败（非 Windows / 该盘没开回收站 /
    API 返回错）时返回 False，调用方退回项目内的 `_已删除/`（同样可逆）。
    系统确认框不带（我们自己的弹窗已经问过一遍了）；`FOF_ALLOWUNDO` 才是"进回收站"，
    少了它就是永久删——这个标志是这一段代码的全部关键。
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [("hwnd", wintypes.HWND),
                        ("wFunc", wintypes.UINT),
                        ("pFrom", wintypes.LPCWSTR),
                        ("pTo", wintypes.LPCWSTR),
                        ("fFlags", ctypes.c_uint16),
                        ("fAnyOperationsAborted", wintypes.BOOL),
                        ("hNameMappings", ctypes.c_void_p),
                        ("lpszProgressTitle", wintypes.LPCWSTR)]

        FO_DELETE = 3
        FOF_ALLOWUNDO = 0x40            # ← 进回收站而不是永久删
        FOF_NOCONFIRMATION = 0x10
        FOF_SILENT = 0x4
        FOF_NOERRORUI = 0x400
        op = SHFILEOPSTRUCTW()
        op.wFunc = FO_DELETE
        op.pFrom = os.path.abspath(path) + "\0\0"      # 双 \0 收尾：这个字段可以塞多条路径
        op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
        rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        return rc == 0 and not os.path.exists(path)
    except Exception:                                        # noqa: BLE001
        return False


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
pcheck = _load_core("提示词体检.py", "prompt_check")     # ③栏「体检」按钮用（统一骨架的自检）


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


def _proj_created(p):
    """项目创建时间 → (显示用 "YYYY-MM-DD HH:MM", 可排序的 epoch 秒)。

    两种来源格式不一样（框架.json 里是 ISO `2026-09-15T14:19:57`，目录时间戳是
    `2026-09-15 14:19`）——**直接比字符串会排错**（'T' 和空格的码位不同），
    所以这里统一算一个 epoch 给前端排序用。2026-09-16 用户指出"按创建时间排了却没变"：
    根因是 `_samples()` 压根没返回时间字段，前端拿空串比空串。
    """
    raw, ts = "", 0.0
    try:
        with open(os.path.join(p, "框架.json"), encoding="utf-8-sig") as f:
            raw = str(((json.load(f) or {}).get("project") or {}).get("createdAt") or "")
    except (OSError, ValueError):
        raw = ""
    for fmt in (None, "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        if not raw:
            break
        try:
            dt = (datetime.datetime.fromisoformat(raw) if fmt is None
                  else datetime.datetime.strptime(raw, fmt))
            ts = dt.timestamp()
            return dt.strftime("%Y-%m-%d %H:%M"), ts
        except ValueError:
            continue
    try:
        ts = os.path.getctime(p)
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)), ts
    except OSError:
        return raw[:16], 0.0


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
        try:
            mtime = os.path.getmtime(p)
        except OSError:
            mtime = 0.0
        created_disp, created_ts = _proj_created(p)
        rows.append({"name": fn, "dir": p, "materials": mats, "videos": gen,
                     "rejects": bad, "reviews": rev,
                     "createdAt": created_disp, "createdTs": created_ts, "mtime": mtime})
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
    out = {"prompts": [], "uploads": [], "uploadGroups": [], "wenan": [], "need": "",
           "state": _proj_state(pdir)}
    sk = None
    if pcore:
        try:
            sk = pcore.load_skeleton(pdir)
        except Exception:                                        # noqa: BLE001
            sk = None
    proj = ((sk or {}).get("project") or {}) if isinstance(sk, dict) else {}
    out["need"] = proj.get("need") or ""
    # **③栏只显示"能直接复制的那段正文"**（用户 2026-09-16 第二次收窄口径："只出现最核心需要复制的东西，
    # 像 DSH 那样只放正文"）。优先级：项目根 `提示词正文.txt` → `文案\提示词正文.txt`（**纯正文**）
    # → 退回 `提示词.txt`（那份带元信息 3 行与版本说明，是完整交付、用于存档与体检）。
    for cand in ("提示词正文.txt", os.path.join("文案", "提示词正文.txt"),
                 "提示词.txt", os.path.join("文案", "提示词.txt")):
        fp = os.path.join(pdir, cand) if pdir else ""
        if fp and os.path.isfile(fp):
            try:
                body = open(fp, encoding="utf-8-sig").read().strip()
            except OSError:
                continue
            if body:
                out["current"] = {"name": (cand if "正文" in cand else cand),
                                  "text": body, "size": os.path.getsize(fp),
                                  "pure": "正文" in cand}
                break
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
    # 即梦上传：**按版本分子目录**列（红白模替换那种一版几十个文件，平铺就看不清了）；
    # 根目录下的散件算"（未分版本）"。每组给个"打开这个文件夹"的入口。
    up = os.path.join(pdir, "即梦上传")
    if os.path.isdir(up):
        for fn in sorted(os.listdir(up)):
            p = os.path.join(up, fn)
            if os.path.isfile(p):
                try:
                    out["uploads"].append({"name": fn, "size": os.path.getsize(p)})
                except OSError:
                    out["uploads"].append({"name": fn, "size": 0})
            elif os.path.isdir(p):
                group = {"ver": fn, "dir": p, "files": [], "size": 0}
                for f2 in sorted(os.listdir(p)):
                    fp = os.path.join(p, f2)
                    if os.path.isfile(fp):
                        try:
                            sz = os.path.getsize(fp)
                        except OSError:
                            sz = 0
                        group["files"].append({"name": f2, "size": sz})
                        group["size"] += sz
                out["uploadGroups"].append(group)
        out["uploadGroups"].sort(key=lambda g: g["ver"])
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


# ── 本机界面偏好（项目列表排序、主题、栏宽）：gitignored 的 workbench.local.json ────
# ⚠️ **不能直接用 HERE（2026-09-16 实测踩到）**：onefile 打包后 `__file__` 落在 PyInstaller
# 的临时解包目录 `%TEMP%\_MEIxxxx`，程序一关目录连文件一起删 → 用户改过的排序/主题每次重开
# 都回到默认。证据与 agent 配置那次同源（见 agent_bridge._pick_cfg_dir 的注释）。
# 修法：路径交给 agent_bridge.cfg_dir() —— 源码运行＝tools/，打包后＝exe 旁边。
_UI_LOCK = threading.Lock()
_UI_DIR = None


def prefs_dir():
    global _UI_DIR
    if _UI_DIR is None:
        d = ""
        if abridge and hasattr(abridge, "cfg_dir"):
            try:
                d = abridge.cfg_dir() or ""
            except Exception:                                    # noqa: BLE001
                d = ""
        _UI_DIR = d or HERE
    return _UI_DIR


def ui_cfg_path():
    return os.path.join(prefs_dir(), "workbench.local.json")


def read_ui():
    try:
        with open(ui_cfg_path(), encoding="utf-8-sig") as f:
            d = json.load(f) or {}
    except (OSError, ValueError):
        d = {}
    d.setdefault("projSort", "default")     # default | name | mats | videos | time | mtime | custom
    d.setdefault("projOrder", [])           # custom 时用：项目目录列表
    d.setdefault("theme", "")               # 主题也存这儿（空＝还没存过，前端退回 localStorage）：
                                            # localStorage 的域带端口，端口一变整套偏好就"没了"
    return d


def write_ui(patch):
    with _UI_LOCK:
        cur = read_ui()
        cur.update(patch or {})
        p = ui_cfg_path()
        try:
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cur, f, ensure_ascii=False, indent=2)
            os.replace(tmp, p)              # 原子换：中途断电也不会留下半个文件
        except OSError as e:
            return {"ok": False, "error": "写不进 %s：%s" % (p, e)}
    return {"ok": True, "ui": cur, "cfgPath": p}


# ── agent 记录：工作台自己给每一轮落一份（跟哪个 agent 无关，翻起来最省事）─────
def agent_records_dir(pdir):
    return os.path.join(pdir, "_会话", "agent记录")


def list_records(pdir):
    """本项目跑过的 agent 轮次（新的在前）。"""
    d = agent_records_dir(pdir)
    out = []
    if os.path.isdir(d):
        for fn in sorted(os.listdir(d), reverse=True):
            if not fn.endswith(".jsonl"):
                continue
            p = os.path.join(d, fn)
            try:
                st = os.stat(p)
            except OSError:
                continue
            out.append({"name": fn, "size": st.st_size, "mtime": st.st_mtime,
                        "path": p})
    return out


def read_record(pdir, name, limit=4000):
    """读一份记录：返回 {meta, lines:[{t,kind,text}]}（名字要防目录穿越）。"""
    name = os.path.basename(name or "")
    p = os.path.join(agent_records_dir(pdir), name)
    if not (name and os.path.isfile(p)):
        return {"ok": False, "error": "没有这份记录：%s" % name}
    rows, meta = [], {}
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= limit:
                    rows.append({"kind": "line", "text": "…（记录太长，只显示前 %d 行）" % limit})
                    break
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if o.get("kind") == "start" or o.get("kind") == "end":
                    meta.update(o)
                rows.append(o)
    except OSError as e:
        return {"ok": False, "error": "读不了：%s" % e}
    return {"ok": True, "name": name, "path": p, "meta": meta, "lines": rows}


def _watch_project_files(pdir, rec, stop_evt):
    """盯项目里的文件动静当进度：**跟 agent 通道无关**。

    2026-09-16 用户实测：ZCode 那条通道 8 分钟零输出（它把过程都写进自己的会话文件，不进 stdout），
    界面上条不动、也不知道有没有在干活。可"它写了什么文件"是最诚实的进度信号：
    文案/ → 框架.json → 即梦上传/ → _会话/回执.jsonl 就是出提示词的四步。
    变了就报一行（顺带让阶段前进），没变就每 45 秒报一句"还在跑"（**进程真活着才报**）。
    """
    def snap():
        out = {}
        for pat in ("文案/*", "素材/*", "框架.json", "框架.md", "即梦上传/*",
                    "_会话/回执.jsonl", "备注/*"):
            for f in glob.glob(os.path.join(pdir, pat)):
                try:
                    out[f] = (os.path.getmtime(f), os.path.getsize(f))
                except OSError:
                    pass
        return out

    prev = snap()
    beat = time.time()
    while not stop_evt.is_set():
        time.sleep(2)
        cur = snap()
        for f, v in sorted(cur.items()):
            if prev.get(f) != v:
                rel = os.path.relpath(f, pdir).replace("\\", "/")
                _push_line("· 写了 %s（%.1f KB）" % (rel, v[1] / 1024.0), rec)
        if cur != prev:
            prev, beat = cur, time.time()
        elif time.time() - beat > 45 and abridge and abridge.current_alive():
            el = round(time.time() - _PROG["t0"]) if _PROG["t0"] else 0
            _push_line("…（还在跑，已用 %d:%02d，暂时没有新动静——这条通道不吐过程输出）"
                       % (el // 60, el % 60), rec)
            beat = time.time()


def _append_record(path, obj):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    except OSError:
        pass


# ── agent 进度：把 stdout 实时转成阶段事件 ─────────────────────────────────
STAGES = [("读技能 / 框架", 15), ("写提示词", 65), ("落即梦上传", 15), ("写回执", 5)]
# `pdir`＝这一轮是**在哪个项目上**跑的（2026-09-16 用户反馈：跑着的时候切项目，
# 提示词框里会显示成原项目的、还会卡住）——界面靠它判断"这一轮跟我现在看的项目是不是同一个"。
_PROG = {"job": 0, "stage": 0, "pct": 0, "lines": [], "done": False, "ok": None,
         "text": "", "t0": 0, "eta": 180, "running": False, "pdir": ""}
_PROG_LOCK = threading.Lock()
_STOPPED = {"flag": False}


# ── 执行记录：**每个项目自己一份**（2026-09-16 用户要求"每个项目单独做记录并留存"）──────
# 从前执行记录只活在页面内存里：切项目就串台、关掉就没了。现在落
# `<项目>/_会话/工作台日志.jsonl`，切项目时界面换成那个项目的尾段。
def log_file(pdir):
    return os.path.join(pdir, "_会话", "工作台日志.jsonl") if pdir else ""


def log_append(pdir, text, kind=""):
    text = (text or "").rstrip()
    if not (pdir and text):
        return
    try:
        os.makedirs(os.path.join(pdir, "_会话"), exist_ok=True)
        with open(log_file(pdir), "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({"t": time.strftime("%Y-%m-%d %H:%M:%S"),
                                "kind": kind, "text": text}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def log_tail(pdir, n=300):
    """读这个项目最近 n 条执行记录（界面切项目时直接换成它）。"""
    p = log_file(pdir)
    if not (p and os.path.isfile(p)):
        return []
    try:
        lines = open(p, encoding="utf-8", errors="replace").read().splitlines()[-n:]
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            d = json.loads(ln)
            out.append({"t": d.get("t", ""), "kind": d.get("kind", ""), "text": d.get("text", "")})
        except ValueError:
            continue
    return out


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
                      "running": True, "pdir": pdir})     # 记下这一轮是给哪个项目跑的
        _STOPPED["flag"] = False          # 每轮开始先清掉"手动停止"标记，别串到下一轮
        eta, n = _hist_eta(pdir)
        _PROG["eta"] = eta
        _PROG["eta_n"] = n
        job = _PROG["job"]
    st = _proj_state(pdir)
    # 技能根：**打包后 HERE 在 _MEIxxxx 临时目录**，dirname(HERE) 会给出一个不存在的路径
    # （2026-09-16 实测：ZCode 收到 "本机临时路径的 SKILL.md 不存在"，自己绕路去已装技能包里找——
    #  白花时间还有读错版本的风险）。统一走 pcore.find_skill_root()。
    try:
        _root = pcore.find_skill_root() if pcore else os.path.dirname(HERE)
    except Exception:                                            # noqa: BLE001
        _root = os.path.dirname(HERE)
    skill_md = os.path.join(_root, "SKILL.md")
    sid = st.get("agentSession") or None
    need = proj_need(pdir)
    # 这一轮的记录文件：**工作台自己落一份**（跟哪个 agent 无关，用户想了解软件在干什么时翻它）
    try:
        _k, _why = abridge.pick_agent()
        agent_label = (_why or "")[:80]
    except Exception:                                            # noqa: BLE001
        agent_label = ""
    rec = os.path.join(agent_records_dir(pdir),
                       "%s-%s.jsonl" % (time.strftime("%Y-%m-%d-%H%M%S"), what or "prompt"))
    _append_record(rec, {"kind": "start", "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                         "what": what or "prompt", "agent": agent_label,
                         "session": sid or "", "project": pdir})
    if sid:
        # 续同一个会话时**别再整篇重读技能**（"读技能"是每次跑最花时间的一段）。
        # 2026-09-16 用户问"是不是每次新开对话重读 skill"——ZCode 之前确实每次新开（会话 id 没存下来，
        # 已在 agent_bridge 修好），这里再把"续跑该怎么做"写进指令，配合上下文缓存提速。
        head = ("这个项目**接着上一次的会话继续**（同一个对话，你的上下文里已经有技能与这个项目的情况）：\n"
                "先看 %s\\_会话\\回执.jsonl（上一轮你自己报过什么）与 框架.json 的现状；"
                "技能文档**只在需要确认细节时按需查对应章节，别整篇重读**。\n\n" % pdir)
    else:
        head = ("先读技能说明 %s 再动手（本机已装，直接读文件即可；"
                "读你需要的章节即可，不必逐字通读全部文档）。\n\n" % skill_md)
    if need:
        # 用户填的「简单需求」是这次任务的硬约束，放在最前面
        head += "用户需求（**必须按它来**，与技能默认口径冲突时先满足它并在回执里说清）：\n%s\n\n" % need
    if what == "feedback":
        todo = (head +
                "用户在界面上给了第 %s 轮反馈：\n%s\n\n"
                "请按反馈重出一版提示词：更新 %s\\文案\\ 下的稿子与 框架.json 的 prompts，"
                "并把要上传的文件副本放进 即梦上传\\（含上传说明.txt；一版文件多就按 即梦上传\\<版本名>\\ 分子目录）；"
                "同时把当前版本的正文覆盖写到项目根 提示词.txt（只放能直接复制的提示词，历史版本留在 框架.json 的 prompts）。"
                "**改动仍要守住统一骨架**（references\\prompt-templates.md 第 0 节的 11 个小节与顺序，"
                "正文里不带 markdown 壳；表格与变更记录进 备注\\备注.txt），"
                "并在收工前跑 `python tools\\提示词体检.py --project \"%s\"` 把 FAIL 改掉；"
                "回执用 tools\\project_core.py --project \"%s\" --receipt \"改了什么\" --todo-done 写。"
                % (st.get("round", 1), feedback, pdir, pdir, pdir))
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
                "用户点了「叫 agent 出提示词」。**按 skill 的默认轻流程做（rules 第49条）**："
                "①先抽 4–8 帧看图/读文件，给每件素材定 role，**并给上传的视频定类别**"
                "（口播展示 / 剧情演绎 / 动作影视素材 / 纯空镜 / 多主体同框），**把类别写进该件的 role**"
                "（例：`参考视频·剧情演绎`），②栏的素材清单就会显示出来；"
                "②读 框架.json 的 need 与 文案/ 弄懂要什么、说什么；③按统一骨架直接出提示词，别绕路。"
                "**静音、裁时长、转画幅裁比例、转深度片、转写、OCR、全片运动量、抽帧拼图——"
                "都不是默认步骤**，命中触发条件才做，做了就在回执里写明为什么做、花了多久；"
                "默认预算：看图+需求 1–3 分钟、出提示词 1–5 分钟。"
                "请扫描 %s\\ 下的 框架.json 与 素材/、文案/，"
                "按 skill 规则出一版提示词，写回 框架.json 的 prompts 与 文案/，"
                "并把要上传的文件副本按引用编号放进 即梦上传\\（**一版文件多就按版本分子目录**："
                "即梦上传\\v1 主推\\、即梦上传\\v2 换服装\\…，每版带自己的 上传说明.txt，旧版保留）；"
                "**项目根再写两份**：`提示词.txt`＝当前版本、可直接复制的正文（元信息 3 行 + 版本 + 正文，"
                "体检认这份）；`提示词正文.txt`＝**只有主版正文**（从【总纲】到【负面】，"
                "不带元信息/上传行/版本说明）——工作台③栏与「复制提示词」显示的就是它；"
                "历史版本留在 框架.json 的 prompts）。"
                "**两条硬要求（2026-09-16 用户裁定，别省）**："
                "①**逐字用统一骨架**——references\\prompt-templates.md 第 0 节那 11 个小节与顺序"
                "（总纲 → 素材分工 → 主体 → 场景 → 道具 → 动作与时间轴 → 口播·音色·口型 → 镜头与景别 → "
                "画面纪律 → 负面）＋元信息 3 行，不许改名换序，正文里不带 markdown 标题/表格/引用块"
                "（表格与变更记录进 备注\\备注.txt）；"
                "②**替换类先看素材再选题材**（rules 第47条）：小动作素材（口播/拎盒展示，峰值运动量长期 "
                "低于约 5%% 画面）走原片直投、**不要转深度片**；只有大动作/影视素材或原片直投同因失败才上深度。"
                "出完**必须跑** `python tools\\提示词体检.py --project \"%s\"`，有 FAIL 就改到没有，"
                "并把通过/注意/不合格的数目写进回执；"
                "回执用 tools\\project_core.py --project \"%s\" --receipt \"改了什么\" "
                "--todo-done 写。" % (pdir, pdir, pdir))

    stop_evt = threading.Event()
    threading.Thread(target=_watch_project_files, args=(pdir, rec, stop_evt),
                     daemon=True).start()

    def run():
        t0 = time.time()
        try:
            res = abridge.ask(todo, session_id=sid, cwd=pdir, timeout=1800,
                              permission_mode="bypassPermissions",
                              on_line=lambda ln: _push_line(ln, rec))
        except Exception as e:                                   # noqa: BLE001
            res = {"ok": False, "error": "起不来：%s" % e}
        stop_evt.set()                     # 文件守护线程收工
        usec = time.time() - t0
        _record_eta(pdir, usec, res.get("ok"))
        try:
            if res.get("session_id") and res["session_id"] != sid:
                pcore.write_state(pdir, agentSession=res["session_id"])
            if res.get("ok"):
                pcore.push_receipt(pdir, res.get("text", ""), kind="出提示词")
                pcore.mark_todos_done(pdir)
                _push_line("✅ agent 回来了：" + (res.get("text") or "")[:400], rec)
            else:
                err = (res.get("error") or "")[:400]
                if "返回码" in err and _STOPPED["flag"]:
                    err = "已手动停止（这一轮的结果不算数，可重跑）"
                    _STOPPED["flag"] = False
                _push_line("!! agent 没跑成：" + err, rec)
        except Exception as e:                                   # noqa: BLE001
            _push_line("!! 回写失败：%s" % e, rec)
        # 收尾：把结果、耗时、以及 agent 自己那份会话正文的路径写进记录
        tpath = ""
        try:                       # 定位失败也要落"结束"这条（别把整条收尾记录一起吞掉）
            if abridge and res.get("session_id"):
                tpath = abridge.transcript_path(_k or "", pdir, res.get("session_id"))
        except Exception:                                        # noqa: BLE001
            tpath = ""
        _append_record(rec, {"kind": "end", "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                             "ok": bool(res.get("ok")), "seconds": round(usec, 1),
                             "session": res.get("session_id") or sid or "",
                             "transcript": tpath,
                             "text": (res.get("text") or res.get("error") or "")[:4000]})
        with _PROG_LOCK:
            _PROG.update({"done": True, "ok": bool(res.get("ok")), "running": False,
                          "pct": 100 if res.get("ok") else _PROG["pct"],
                          "text": res.get("text") or res.get("error") or "",
                          "seconds": round(usec, 1),
                          "record": rec})
    threading.Thread(target=run, daemon=True).start()
    return {"job": job, "eta": _PROG["eta"], "eta_n": _PROG.get("eta_n", 0), "record": rec}


def _recompute_pct_locked():
    """算进度百分比：max(已完成阶段的权重和, 按历史耗时推算的时间进度)，封顶 96%。

    ⚠️ **必须在"每次轮询"里也算一遍**（2026-09-16 用户指出"进度条仍然没有做"）：
    原先只在收到 agent 日志行时更新，而 ZCode 这条通道（text 模式）几乎不吐过程输出
    → 条一直 0%，只有阶段名在动。现在 SSE 每次拉快照都会重算，条会随时间平滑前进。
    """
    acc = sum(w for i, (_n, w) in enumerate(STAGES) if i < _PROG["stage"])
    by_time = 0
    if _PROG["t0"]:
        el = time.time() - _PROG["t0"]
        by_time = int(el / max(1.0, _PROG["eta"]) * 100)
    _PROG["pct"] = min(96, max(acc, by_time))


def _push_line(line, rec=None):
    line = (line or "").rstrip()
    if not line:
        return
    if rec:
        _append_record(rec, {"kind": "line", "at": time.strftime("%H:%M:%S"), "text": line})
    log_append(_PROG.get("pdir") or "", line, "agent")     # 落进本项目自己的执行记录
    with _PROG_LOCK:
        _PROG["lines"].append(line)
        if len(_PROG["lines"]) > 400:
            del _PROG["lines"][:-400]
        _PROG["stage"] = _stage_from_line(line, _PROG["stage"])
        _recompute_pct_locked()


def progress_snapshot(since=0):
    with _PROG_LOCK:
        el = time.time() - _PROG["t0"] if _PROG["t0"] else 0
        if _PROG["running"]:
            _recompute_pct_locked()          # 没日志行也要让条动（按时间估）
        d = {"job": _PROG["job"], "stage": _PROG["stage"],
             "stageName": STAGES[min(_PROG["stage"], 3)][0],
             "pct": _PROG["pct"], "elapsed": round(el), "eta": round(_PROG["eta"]),
             "running": _PROG["running"], "done": _PROG["done"], "ok": _PROG["ok"],
             "lines": _PROG["lines"][since:], "n": len(_PROG["lines"]),
             # 这条通道不给过程输出时如实说明（否则用户看着不动的条只能瞎猜）
             "quiet": bool(_PROG["running"] and not _PROG["lines"] and el > 25),
             "text": _PROG["text"],
             "pdir": _PROG.get("pdir") or "",     # 这一轮在哪个项目上跑（切项目时界面靠它分辨）
             "pname": os.path.basename((_PROG.get("pdir") or "").rstrip("\\/"))}
        return d


# ── HTTP ───────────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    server_version = "HeronBoWorkbench/1.0"
    last_ping = time.time()
    loaded_at = 0.0          # 页面（index.html/app.js）被拉取的时刻 → 判断"不是白窗"
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
        if path == "/api/promptcheck":
            return self._json(self.prompt_check())
        if path == "/api/review":
            return self._json(self.read_review())
        if path == "/api/progress":
            return self.sse()
        if path == "/api/agent/status":
            return self._json(progress_snapshot())
        if path == "/api/records":
            return self._json(self.records())
        if path == "/api/records/read":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return self._json(read_record(self._proj(), (q.get("name") or [""])[0]))
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
        if rel in ("index.html", "app.js"):          # 页面真被拉取了 → 看门狗据此判"不是白窗"
            Handler.loaded_at = time.time()
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
                return self._json({"ok": True, "project": _scan_project(d),
                                   "healed": self.heal_skeleton(d)})
            return self._json({"ok": False, "error": "目录不存在：%s" % d}, 400)
        if path == "/api/project/new":
            return self._json(self.new_project(self._body()))
        if path == "/api/project/delete":
            return self._json(self.delete_project(self._body()))
        if path == "/api/need":
            return self._json(self.save_need(self._body()))
        if path == "/api/ui":
            return self._json(write_ui(self._body()))
        if path == "/api/log":
            # 界面上发生的动作（收片、保存评分、切 agent…）也落进**本项目**的执行记录，
            # 这样"每个项目单独做记录并留存"才成立（2026-09-16 用户要求）。
            b = self._body()
            pdir = self._proj(b)
            txt = (b.get("text") or "").strip()
            if pdir and txt:
                log_append(pdir, txt, b.get("kind") or "ui")
            return self._json({"ok": bool(pdir and txt)})
        if path == "/api/open":
            return self._json(self.open_path(self._body()))
        if path == "/api/records/open":
            return self._json(self.open_record(self._body()))
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
        if path == "/api/agent/session":
            b = self._body()
            pdir = self._proj(b)
            if not (pcore and pdir and os.path.isdir(pdir)):
                return self._json({"ok": False, "error": "没有项目"})
            if (b.get("action") or "") == "new":
                try:
                    pcore.write_state(pdir, agentSession="")
                except Exception as e:                           # noqa: BLE001
                    return self._json({"ok": False, "error": "清会话失败：%s" % e})
                return self._json({"ok": True, "note": "下一轮开始新对话（技能要重读一次）"})
            return self._json({"ok": False, "error": "不认识的动作：%s" % b.get("action")})
        if path == "/api/agent/stop":
            _STOPPED["flag"] = True
            ok = bool(abridge and abridge.stop_current())
            if ok:
                _push_line("!! 用户手动停止了这一轮")
            return self._json({"ok": ok, "error": "" if ok else "现在没有在跑的任务"})
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
                  "ui": read_ui(),
                  "uiCfg": ui_cfg_path(),       # 显示给用户看："排序/主题存在这儿"
                  # 本项目的执行记录（切项目时界面直接换成它，别看别的项目的）
                  "logTail": log_tail(pdir) if (pdir and os.path.isdir(pdir)) else [],
                  # 正在跑的 agent 是在哪个项目上跑（不一样就说明"这轮不属于你现在看的项目"）
                  "jobPdir": (_PROG.get("pdir") or "") if _PROG.get("running") else "",
                  "jobPname": (os.path.basename((_PROG.get("pdir") or "").rstrip("\\/"))
                               if _PROG.get("running") else ""),
                  "need": proj_need(pdir) if (pdir and os.path.isdir(pdir))
                          else (self.server.need or "")})
        d.update(self.root_issue(root))
        return d

    # ---- ①栏「＋ 新建项目」/ ②栏「需求 · 素材增删排序」----------------------
    def delete_project(self, b):
        """①栏项目的「删除」，两种方式由用户当场选（2026-09-16 用户要求）：

        - `mode="trash"`（默认）：扔进 **Windows 系统回收站**（能在回收站里还原）；回收站用不了
          时退回项目内的 `<样本库根>/_已删除/<名字>-<时间戳>/`，同样可逆——而且在资源管理器里
          搬回来就恢复，`_` 开头不会被列出来（`_samples()` 跳过下划线目录）。
        - `mode="purge"`：**真删**（`shutil.rmtree`），删错了没得救，所以前端必须二次确认
          （把件数和目录路径都摆出来）。
        """
        d = (b.get("dir") or "").rstrip("\\/")
        mode = (b.get("mode") or "trash").strip().lower()
        if mode not in ("trash", "purge"):
            return {"ok": False, "error": "不认识的删除方式：%s" % mode}
        if not (d and os.path.isdir(d)):
            return {"ok": False, "error": "目录不存在：%s" % d}
        root = os.path.dirname(d)
        name = os.path.basename(d)
        if name.startswith("_"):
            return {"ok": False, "error": "这个目录不是项目（下划线开头）：%s" % name}
        if os.path.normcase(d) == os.path.normcase(read_proj() or ""):
            self._set_proj("")                       # 删的正是当前项目 → 清掉指针
        if mode == "purge":
            try:
                shutil.rmtree(d, ignore_errors=False)
            except OSError as e:
                return {"ok": False, "error": "永久删除失败：%s" % e}
            return {"ok": True, "purged": d}
        if mode == "trash" and recycle_bin_delete(d):
            return {"ok": True, "recycled": d, "how": "recycle"}
        dest_root = os.path.join(root, "_已删除")
        dest = os.path.join(dest_root, "%s-%s" % (name, time.strftime("%Y%m%d-%H%M%S")))
        try:
            os.makedirs(dest_root, exist_ok=True)
            shutil.move(d, dest)
        except OSError as e:
            return {"ok": False, "error": "移走失败：%s" % e}
        return {"ok": True, "movedTo": dest, "how": "trashdir"}

    def heal_skeleton(self, pdir):
        """打开一个**缺 框架.json** 的项目时把骨架补齐（返回 True 表示补了）。

        2026-09-16 实测遇到一个只有空目录、没有 框架.json/_会话 的项目（建到一半被打断，
        或目录是人手建的）——打开它界面什么都不显示，用户也不知道该怎么办。这里自动补一下：
        **只在文件不存在时补**（文件在但坏了不动，免得把内容覆盖掉）。
        """
        if not pcore:
            return False
        if os.path.isfile(os.path.join(pdir, "框架.json")):
            return False
        try:
            base = pdir.rstrip("\\/")
            pcore.build_skeleton(os.path.dirname(base), os.path.basename(base),
                                 project_dir=pdir, register=False)
            return True
        except Exception:                                        # noqa: BLE001
            return False

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

    def open_path(self, b):
        """在资源管理器里打开一个项目相关的目录/文件（用户点的动作）。

        kind: uploads（即梦上传）/ project（项目根）/ materials（素材）/ code（文案）/
              finals（成片）/ rejects（废片）/ reviews（评价）/ records（对话记录）
        sub: 打开某个子目录（比如即梦上传的某个版本）
        """
        pdir = self._proj(b)
        if not (pdir and os.path.isdir(pdir)):
            return {"ok": False, "error": "还没有项目"}
        kind = (b.get("kind") or "project").strip()
        sub = (b.get("sub") or "").strip()
        rel = {"uploads": "即梦上传", "project": "", "materials": "素材", "code": "文案",
               "finals": "成片", "rejects": "废片", "reviews": "评价",
               "records": os.path.join("_会话", "agent记录")}.get(kind)
        if rel is None:
            return {"ok": False, "error": "不认识的 kind：%s" % kind}
        target = os.path.normpath(os.path.join(pdir, rel, sub)) if sub else             os.path.normpath(os.path.join(pdir, rel))
        # 防目录穿越：必须还在项目里
        if not os.path.normcase(target).startswith(os.path.normcase(os.path.normpath(pdir))):
            return {"ok": False, "error": "路径越界"}
        if not os.path.exists(target):
            try:
                os.makedirs(target, exist_ok=True)
            except OSError as e:
                return {"ok": False, "error": "建不了目录：%s" % e}
        if os.name == "nt":
            try:
                os.startfile(target)                             # noqa: S606（本机动作，用户点的）
            except OSError as e:
                return {"ok": False, "error": "打不开：%s" % e}
        return {"ok": True, "opened": target}

    def prompt_check(self):
        """③栏「提示词体检」：按《统一骨架》检查当前项目的提示词（references/prompt-templates.md 第 0 节）。

        为什么要摆到界面上（2026-09-16）：用户发现同一天两个项目的提示词**风格各写各的**——
        光靠 agent 自觉记不住骨架，所以给一个当场能跑的检查，不合格就把行号与改法摆出来。
        """
        if not pcheck:
            return {"ok": False, "error": "找不到 tools\\提示词体检.py"}
        pdir = self._proj()
        if not (pdir and os.path.isdir(pdir)):
            return {"ok": False, "error": "还没选项目"}
        # 优先体检"正在交付的那一份"：项目根 提示词.txt → 文案/提示词.txt → 文案/*提示词*.txt
        cand = [os.path.join(pdir, "提示词.txt"), os.path.join(pdir, "文案", "提示词.txt")]
        d = os.path.join(pdir, "文案")
        if os.path.isdir(d):
            cand += [os.path.join(d, fn) for fn in sorted(os.listdir(d))
                     if "提示词" in fn and fn.lower().endswith(".txt")]
        p = next((x for x in cand if os.path.isfile(x)), "")
        if not p:
            return {"ok": False, "error": "这个项目还没有提示词文件"}
        ok, ps, ws, fs = pcheck.check_path(p)
        return {"ok": True, "pass": ok, "file": p,
                "passed": [{"name": n, "detail": d} for n, d in ps],
                "warn": [{"name": n, "detail": d} for n, d in ws],
                "fail": [{"name": n, "detail": d} for n, d in fs]}

    def records(self):
        """本项目跑过的 agent 轮次 + agent 自己那份会话正文的路径。

        为什么要工作台自己记一份：各家 agent 的会话存储格式/位置都不一样（WorkBuddy 在
        `~/.workbuddy/projects/<路径打平>/<id>.jsonl`，ZCode 在 `~/.zcode/cli/rollout/model-io-<id>.jsonl`），
        而且**桌面端里看不到**。工作台每跑一轮就写一份人能读的记录（时间线），翻起来最省事。
        """
        pdir = self._proj()
        if not (pdir and os.path.isdir(pdir)):
            return {"ok": False, "error": "还没有项目", "records": []}
        st = _proj_state(pdir)
        sid = st.get("agentSession") or ""
        tpath = ""
        if abridge and sid:
            try:
                key, _why = abridge.pick_agent()
                tpath = abridge.transcript_path(key or "", pdir, sid)
            except Exception:                                    # noqa: BLE001
                tpath = ""
        return {"ok": True, "dir": agent_records_dir(pdir),
                "session": sid, "transcript": tpath,
                "records": list_records(pdir)}

    def open_record(self, b):
        """在资源管理器里打开这条记录（或它所在的文件夹）——看"agent 到底干了什么"最直接。

        2026-09-16 用户反馈"点文件夹点不开"：项目还没跑过 agent 时，`_会话/agent记录/` 这个目录
        还不存在，原样直接报"没有这个"→ 用户看到的就是"点不开"。现在改成**不存在就建出来再打开**
        （空目录也能打开，用户就知道以后记录会落这儿）。
        """
        pdir = self._proj()
        if not (pdir and os.path.isdir(pdir)):
            return {"ok": False, "error": "还没有项目"}
        d = agent_records_dir(pdir)
        name = (b.get("name") or "").strip()
        target = os.path.join(d, os.path.basename(name)) if name else d
        if not os.path.exists(target):
            try:
                os.makedirs(d, exist_ok=True)
            except OSError as e:
                return {"ok": False, "error": "建不了记录目录：%s" % e}
            target = d
        if os.name == "nt":
            try:
                os.startfile(target)                             # noqa: S606（本机动作，用户点的）
            except OSError as e:
                return {"ok": False, "error": "打不开：%s" % e}
        return {"ok": True, "opened": os.path.normpath(target)}

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
            # 当前会话：id + 它那份会话正文的大小（用大小粗略感知"上下文多满了"，
            # 好让用户决定什么时候"开新会话"——2026-09-16 用户就是这么问的）
            pdir = self._proj()
            sid = (_proj_state(pdir) or {}).get("agentSession") or "" if pdir else ""
            info["session"] = sid
            info["sessionKB"] = 0
            info["sessionFile"] = ""
            if sid and pdir:
                try:
                    key, _why = abridge.pick_agent()
                    fp = abridge.transcript_path(key or "", pdir, sid)
                    if fp and os.path.isfile(fp):
                        info["sessionFile"] = fp
                        info["sessionKB"] = round(os.path.getsize(fp) / 1024.0, 1)
                except Exception:                                # noqa: BLE001
                    pass
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
        # 没素材也让它干一件有用的事：**把框架建出来**（2026-09-16 用户："新建项目了点击框架"）
        # 新建的空项目还没有 框架.json，这时点「建框架归类」原本只报"还没有待投放的素材"，
        # 用户看着像"点了没反应"。这里改成：把骨架（含 框架.json/框架.md/状态.json）建出来并说明。
        if not pend:
            pdir0 = self._proj(b)
            if pdir0 and os.path.isdir(pdir0) and self.heal_skeleton(pdir0):
                return {"ok": True, "built": True, "project": _scan_project(pdir0),
                        "note": "框架已建好（还没有素材）：把素材拖进②栏，再点一次就能按角色归类"}
            return {"ok": False, "error": "还没有待投放的素材——先把素材拖进②栏"}
        if not pcore:
            return {"ok": False, "error": "缺 project_core.py"}
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
    """用 pywebview 开一个**独立窗口**（内嵌 WebView2），返回 True 表示"界面真的出来了"。

    这是首选路径：窗口属于本程序自己——自己的标题、图标、任务栏条目，没有地址栏；
    也不用借用户正在用的 Edge 浏览器（2026-09-15 用户明确要求"变成独立程序而不是
    依赖 Edge"）。WebView2 **运行时**是系统组件（Win10/11 自带，Teams/微信都在用），
    不是用户的浏览器进程，关掉浏览器不影响它。

    **白窗看门狗（2026-09-16 加）**：窗口开出来 ≠ 界面出来了。WebView2 坏掉时是
    "标题有、里面一片空白"，而 pywebview 的报错发生在 GUI 线程里、程序自己吞掉，
    调用方完全看不出来（别的机器就这么白屏了）。所以这里盯**页面有没有真连上来**
    （静态页被拉取 / 心跳到达都算）：25 秒还没有 → 认定白窗，关掉它并返回 False，
    让调用方退到 Edge 窗口 / 默认浏览器，并留下体检结论与修复步骤。
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
    Handler.loaded_at = 0.0            # 页面真被拉取的时刻（看门狗与调用方都看它）
    t0 = time.time()
    state = {"ok": False, "blank": False}
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

        def _watch():
            """等页面连上来；等不到就是白窗——关掉它，让调用方走退路。"""
            # 默认 25 秒；验证/排查时可以调小（HERONBO_WV2_TIMEOUT 秒）
            try:
                budget = float(os.environ.get("HERONBO_WV2_TIMEOUT") or 25)
            except ValueError:
                budget = 25.0
            for _ in range(max(2, int(budget * 2))):
                time.sleep(0.5)
                if Handler.loaded_at > t0 or Handler.last_ping > t0:
                    state["ok"] = True
                    return
                if not _win_alive():
                    return                                       # 窗口已被用户关掉
            state["blank"] = True
            try:
                with open(os.path.join(os.environ.get("TEMP", "."),
                                       "heronbo_webview2.txt"), "w", encoding="utf-8") as f:
                    f.write("独立窗口开出来了，但 25 秒内界面一直是白的（页面没连上来）。\n"
                            "多半是 WebView2 运行库坏了/缺文件。已改用 Edge 窗口打开。\n\n"
                            + WEBVIEW2_HELP + "\n")
            except OSError:
                pass
            print("[工作台] 独立窗口是白窗（WebView2 没跑起来）→ 改用 Edge 窗口。\n"
                  + WEBVIEW2_HELP, flush=True)
            for fn in ("destroy", "hide"):
                try:
                    getattr(win, fn)()
                    break
                except Exception:                                # noqa: BLE001
                    pass

        def _boot():
            threading.Thread(target=_show, daemon=True).start()
            threading.Thread(target=_watch, daemon=True).start()

        kw = {}
        if store:
            kw["storage_path"] = store
        try:
            webview.start(_boot, **kw)       # _boot 在 GUI 起来后（另起线程）被调用
        except TypeError:             # 老版本没有 storage_path
            webview.start(_boot)
        if state["blank"]:
            return False                 # 白窗 → 让调用方退 Edge / 浏览器
        return True
    except Exception as e:                                       # noqa: BLE001
        try:
            open(os.path.join(os.environ.get("TEMP", "."), "heronbo_webview_error.log"),
                 "w", encoding="utf-8").write("pywebview 起不来：%r" % (e,))
        except OSError:
            pass
        return False


def _win_alive():
    try:
        return bool(_WIN) and not _WIN.events.closed.is_set()
    except Exception:                                            # noqa: BLE001
        return True


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


# ── WebView2 运行库体检（2026-09-16 新机踩坑后加的）────────────────────────────
# 别的机器上遇到的现象：双击工作台，**窗口开出来一片空白**（标题有、里面什么都没有），
# 因为 WebView2 运行库"注册表说有、实际上缺文件"：
# 注册表指向 ...\EdgeWebView\Application\134.0.3124.93\，而该目录里 msedgewebview2.exe
# 不见了（284MB 的 msedge.dll 还在）→ pywebview 报
# "Couldn't find a compatible Webview2 Runtime installation to host WebViews"（0x80070002）。
# **只查注册表会误判成"装了"**，所以这里既看注册表、也看那个版本目录里宿主 exe 在不在；
# 注册表指的那个坏了、但别的版本是完整的，就报"可疑"（让它试，白窗兜底在后面）。
WEBVIEW2_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
WEBVIEW2_HELP = ("修法：装微软官方运行库（一次就好，之后所有用 WebView2 的程序都不再白屏）\n"
                 "       下载 https://go.microsoft.com/fwlink/p/?LinkId=2124703 （Evergreen "
                 "Bootstrapper，MicrosoftEdgeWebview2Setup.exe）\n"
                 "       或跑：python tools\\部署.py fix-webview2 --yes")


def _wv2_dirs(version):
    out = []
    for base in (r"C:\Program Files (x86)\Microsoft\EdgeWebView\Application",
                 r"C:\Program Files\Microsoft\EdgeWebView\Application",
                 os.path.join(os.environ.get("LOCALAPPDATA", ""),
                              "Microsoft", "EdgeWebView", "Application")):
        if version:
            out.append(os.path.join(base, version, "msedgewebview2.exe"))
        out.append(base)
    return out


def webview2_versions():
    """磁盘上装了哪些 WebView2 版本（目录名），以及哪个是完整的（宿主 exe 在）。"""
    import glob as _glob
    good, allv = [], []
    base = r"C:\Program Files (x86)\Microsoft\EdgeWebView\Application"
    bases = [base, r"C:\Program Files\Microsoft\EdgeWebView\Application",
             os.path.join(os.environ.get("LOCALAPPDATA", ""),
                          "Microsoft", "EdgeWebView", "Application")]
    for b in bases:
        for d in _glob.glob(os.path.join(b, "*")):
            if not os.path.isdir(d):
                continue
            v = os.path.basename(d)
            if not re.match(r"^\d+\.", v):
                continue
            allv.append(v)
            if os.path.isfile(os.path.join(d, "msedgewebview2.exe")):
                good.append(v)
    return sorted(set(allv)), sorted(set(good))


def webview2_registered():
    """注册表里"当前生效"的 WebView2 版本（per-user 优先，再 per-machine）。"""
    try:
        import winreg
    except ImportError:
        return ""
    for hive, key in ((winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\EdgeUpdate\Clients"),
                      (winreg.HKEY_LOCAL_MACHINE,
                       r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients"),
                      (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\EdgeUpdate\Clients")):
        try:
            with winreg.OpenKey(hive, key + "\\" + WEBVIEW2_GUID) as k:
                v = winreg.QueryValueEx(k, "pv")[0]
                if v and v != "0.0.0.0":
                    return str(v)
        except OSError:
            continue
    return ""


def webview2_state():
    """WebView2 到底能不能用 → `(ok, 版本, 说明)`。

    - `ok=True`：注册表版本目录里有宿主 exe（pywebview 有戏）
    - `ok=False`：一个完整版本都没有 / 注册表指的那个缺文件且没有别的可用版本
      （此时**不要**开 pywebview 窗口，直接退 Edge，免得用户对着白窗发呆）
    """
    if os.name != "nt":
        return True, "", "非 Windows，交给 pywebview 自己判断"
    reg = webview2_registered()
    allv, good = webview2_versions()
    if reg:
        ok_reg = os.path.isfile(_wv2_dirs(reg)[0])
        if ok_reg:
            return True, reg, "注册表版本 %s 完整" % reg
        if good:
            return True, good[-1], ("注册表指的 %s 缺 msedgewebview2.exe，但磁盘上还有完整的 %s"
                                    "（可能仍会白屏）" % (reg, good[-1]))
        return False, reg, ("注册表写着装了 %s，但那个目录里**没有 msedgewebview2.exe**"
                            "（运行库坏了）" % reg)
    if good:
        return True, good[-1], "磁盘上有 %s（注册表没记）" % good[-1]
    return False, "", "本机没装 WebView2 运行库（Win10/11 一般自带；缺了就装一下）"


def _wv2_report(ok, ver, why):
    log_p = os.path.join(os.environ.get("TEMP", "."), "heronbo_webview2.txt")
    try:
        with open(log_p, "w", encoding="utf-8") as f:
            f.write("WebView2 体检：%s\n版本：%s\n说明：%s\n\n%s\n"
                    % ("可用" if ok else "不可用", ver or "（未检出）", why, WEBVIEW2_HELP))
    except OSError:
        pass
    print("[工作台] WebView2 体检：%s（%s）" % ("可用" if ok else "**不可用**", why), flush=True)
    if not ok:
        print("[工作台] 独立窗口用不了，改用 Edge 窗口打开。\n" + WEBVIEW2_HELP, flush=True)


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
    srv = serve(root=root or "", port=port or 0)   # port 传下去：--port 固定端口给自动化用
    url = "http://127.0.0.1:%d/" % srv.server_address[1]
    write_proj(read_proj(), port=srv.server_address[1])
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    Handler.last_ping = time.time()
    print("[工作台] %s" % url, flush=True)
    # 只要服务、不要窗口（给"别的程序用 API 驱动工作台"与自动化验证用）
    if os.environ.get("HERONBO_NO_WINDOW"):
        print("[工作台] HERONBO_NO_WINDOW=1：只起服务、不开窗口", flush=True)
        if wait:
            wait_and_exit(srv, idle=24 * 3600)      # 没人点页面也不退，等调用方喊停
        return srv, url, "headless"
    ok_wv2, wv2ver, wv2why = webview2_state() if backend != "edge" else (True, "", "指定走 Edge")
    if backend != "edge":
        _wv2_report(ok_wv2, wv2ver, wv2why)
    # 预检不过就**根本不试**独立窗口：坏的运行库只会给你一个白窗，不如直接退 Edge（2026-09-16）
    if backend != "edge" and ok_wv2 and native_window(WIN_TITLE, url):
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
