# -*- coding: utf-8 -*-
"""agent 动作流 → 工作台可显示的事件（思考 / 终端 / 读写 / 待办 / 提问）。

数据源＝**各 harness 自己的会话正文文件**——agent 在那个工具界面上看到的过程，
就是从这份文件渲染的。但每家工具把正文放在哪、长什么样都不一样，所以这里做成
「通道（channel）」表：

    一个通道 = 去哪里找（dir + glob，可递归） + 按什么格式解析（fmt）

工作台每轮把**本机探测到的所有通道**合起来读；探不到的通道自动跳过——所以
「只装了 WorkBuddy 的机子」「只装了豆包工作的机子」都能各自跑（2026-09-21 用户要求）。

内置通道（探不到就跳过，不会影响别的机子）：
    zcode      ~/.zcode/cli/rollout/model-io-*.jsonl    fmt=zcode
    workbuddy  ~/.workbuddy/projects/**/*.jsonl         fmt=workbuddy
    claude     ~/.claude/projects/**/*.jsonl            fmt=claude
    codex      ~/.codex/sessions/**/*.jsonl             fmt=auto（抽检通过才启用）
    gemini     ~/.gemini/tmp/**/*.jsonl                 fmt=auto
    doubao     ~/.doubao/**/*.jsonl                     fmt=auto

**换机器 / 换工具不用改代码**：在本机写一份 `agent_channels.local.json` 指路
（放在 exe 旁边、或 `~/.heronbo/agent_channels.json`，或用环境变量
HERONBO_AGENT_CHANNELS 指向它）：

    {"channels": [
       {"key": "doubao", "label": "豆包工作", "dir": "~/.doubao/sessions",
        "glob": "*.jsonl", "recursive": true, "fmt": "auto"}
     ],
     "exclude": ["claude"]}

fmt 取值：auto（按结构猜，推荐）/ zcode / workbuddy / claude。
dir 支持 ~ 与 {USERPROFILE} 占位；同 key 覆盖内置；exclude 停用内置。

⚠️ 猜测型通道（fmt=auto）要「抽最新正文尾部若干行，真能解析出事件」才启用，
免得把机器上无关的 .jsonl 当过程流显示出来。

用法（自检）：
    python tools\\agent_trace.py               # 最近 30 条动作（自动挑最新通道）
    python tools\\agent_trace.py --channels    # 只看本机探测到哪些通道
    python tools\\agent_trace.py --file X      # 指定正文文件
"""
import fnmatch
import glob
import json
import os
import sys
import time

HOME = os.environ.get("USERPROFILE") or os.path.expanduser("~")
CONFIG_ENV = "HERONBO_AGENT_CHANNELS"
CONFIG_NAME = "agent_channels.local.json"

# 一排动作里，用户最关心"它动了哪个文件/跑什么命令"——先给它们一个短标签
TOOL_KIND = {
    "bash": "term", "shell": "term", "run_command": "term", "terminal": "term",
    "read": "read", "read_file": "read", "view": "read",
    "write": "write", "create_file": "write",
    "edit": "edit", "multiedit": "edit", "str_replace": "edit", "apply_patch": "edit",
    "todowrite": "todo", "todoread": "todo",
    "askuserquestion": "ask", "ask": "ask",
    "glob": "find", "grep": "find", "search": "find", "list": "find",
}
KIND_ICON = {"think": "💡", "term": "⌨", "read": "📖", "write": "✍", "edit": "✏",
             "todo": "✅", "ask": "❓", "find": "🔍", "say": "💬", "tool": "🔧"}
KIND_LABEL = {"think": "思考", "term": "终端", "read": "读取", "write": "写入",
              "edit": "修改", "todo": "待办", "ask": "要你确认", "find": "查找",
              "say": "说明", "tool": "工具"}


# ── 通道表 ────────────────────────────────────────────────────────────────
def _h(*p):
    return os.path.join(HOME, *p)


BUILTIN_CHANNELS = [
    {"key": "zcode", "label": "ZCode", "dir": _h(".zcode", "cli", "rollout"),
     "glob": "model-io-*.jsonl", "recursive": False, "fmt": "zcode"},
    {"key": "workbuddy", "label": "WorkBuddy", "dir": _h(".workbuddy", "projects"),
     "glob": "*.jsonl", "recursive": True, "fmt": "workbuddy"},
    {"key": "claude", "label": "Claude Code", "dir": _h(".claude", "projects"),
     "glob": "*.jsonl", "recursive": True, "fmt": "claude"},
    {"key": "codex", "label": "Codex CLI", "dir": _h(".codex", "sessions"),
     "glob": "*.jsonl", "recursive": True, "fmt": "auto"},
    {"key": "gemini", "label": "Gemini CLI", "dir": _h(".gemini", "tmp"),
     "glob": "*.jsonl", "recursive": True, "fmt": "auto"},
    {"key": "doubao", "label": "豆包工作", "dir": _h(".doubao"),
     "glob": "*.jsonl", "recursive": True, "fmt": "auto"},
]
TRUSTED_FMT = ("zcode", "workbuddy", "claude")   # 已知格式，直接信任；其余要抽检

_CFG_DIR = ""


def set_cfg_dir(d):
    """workbench_server 启动时把"本机配置目录"传进来（源码＝tools/，打包＝exe 旁边）。

    为什么要传：onefile 打包后 `__file__` 落在临时解包目录，程序一关就删；
    配置必须落在**用户能改、且关掉还在**的地方（沿用 workbench.local.json 的同一目录）。
    """
    global _CFG_DIR
    _CFG_DIR = d or ""
    _CHAN_CACHE["at"] = 0.0


def _expand(p):
    p = str(p or "")
    if not p:
        return ""
    p = p.replace("{USERPROFILE}", HOME).replace("{HOME}", HOME)
    if p.startswith("~"):
        p = os.path.join(HOME, p[1:].lstrip("\\/"))
    return os.path.normpath(p)


def _cfg_dirs():
    out = []
    if _CFG_DIR:
        out.append(_CFG_DIR)
    try:
        if getattr(sys, "frozen", False):
            out.append(os.path.dirname(os.path.abspath(sys.executable)))
        else:
            out.append(os.path.dirname(os.path.abspath(__file__)))
    except Exception:                                            # noqa: BLE001
        pass
    out.append(os.path.join(HOME, ".heronbo"))
    return out


def _config_paths():
    out = []
    ev = os.environ.get(CONFIG_ENV)
    if ev:
        out.append(ev)
    for d in _cfg_dirs():
        out.append(os.path.join(d, CONFIG_NAME))
    return out


def load_channels():
    """内置通道 + 本机配置里的通道（同 key 覆盖；exclude 可停用内置）。"""
    chans = [dict(c) for c in BUILTIN_CHANNELS]
    excl = set()
    for p in _config_paths():
        if not (p and os.path.isfile(p)):
            continue
        try:
            with open(p, encoding="utf-8-sig") as f:
                cfg = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(cfg, list):            # 允许直接写一个数组
            cfg = {"channels": cfg}
        if not isinstance(cfg, dict):
            continue
        for c in (cfg.get("channels") or []):
            if not (isinstance(c, dict) and c.get("key")):
                continue
            c = dict(c)
            c["_src"] = os.path.basename(p)
            chans = [x for x in chans if x.get("key") != c["key"]] + [c]
        for k in (cfg.get("exclude") or []):
            excl.add(str(k))
    if excl:
        chans = [c for c in chans if c.get("key") not in excl]
    return chans


def _files_in(ch):
    """这个通道下所有候选正文文件。递归时限制深度，免得在大目录里乱走。"""
    d = _expand(ch.get("dir"))
    if not d or not os.path.isdir(d):
        return []
    pat = ch.get("glob") or "*.jsonl"
    if not ch.get("recursive"):
        try:
            return [f for f in glob.glob(os.path.join(d, pat)) if os.path.isfile(f)]
        except OSError:
            return []
    out = []
    try:
        base = d.rstrip("\\/").count(os.sep)
        for root, dirs, files in os.walk(d):
            depth = root.rstrip("\\/").count(os.sep) - base
            if depth >= 4:
                dirs[:] = []
            for fn in files:
                if fnmatch.fnmatch(fn, pat):
                    out.append(os.path.join(root, fn))
                    if len(out) >= 4000:
                        return out
    except OSError:
        return out
    return out


_VERIFY_CACHE = {}
VERIFY_CAP = 60          # 猜测型通道：只抽检最新的这么多个正文（够用，且别把大目录读爆）


def _safe_mtime(p):
    try:
        return os.path.getmtime(p)
    except OSError:
        return 0


def _verify_file(path, fmt, trust=False):
    """这个文件到底是不是「能解析出事件的会话正文」（结果按 路径+大小 缓存）。

    可信格式（zcode/workbuddy/claude）直接放行——它们的目录本来就是会话库；
    猜测型通道必须**逐个文件**验，免得把机器上无关的 .jsonl 当过程流显示出来
    （2026-09-21 测试实踩：同一目录里混进 data.jsonl，只按通道抽检挡不住）。
    """
    if trust:
        return True
    try:
        size = os.path.getsize(path)
    except OSError:
        return False
    key = (path, size)
    v = _VERIFY_CACHE.get(key)
    if v is None:
        evs, _ = events_since(path, max(0, size - 120000), limit=8, fmt=fmt)
        v = bool(evs)
        if len(_VERIFY_CACHE) > 4000:
            _VERIFY_CACHE.clear()
        _VERIFY_CACHE[key] = v
    return v


def _usable_files(ch):
    """这个通道下真能当过程流用的正文。"""
    files = _files_in(ch)
    if not files:
        return []
    fmt = ch.get("fmt") or "auto"
    if fmt in TRUSTED_FMT:
        return files
    files.sort(key=_safe_mtime, reverse=True)
    return [f for f in files[:VERIFY_CAP] if _verify_file(f, fmt)]


_CHAN_CACHE = {"at": 0.0, "list": []}
CHAN_TTL = 30.0


def available_channels(force=False):
    """本机探测到的通道（带 30 秒缓存）：目录得在、得有正文文件、猜测型还得抽检通过。"""
    now = time.time()
    if not force and _CHAN_CACHE["list"] and (now - _CHAN_CACHE["at"]) < CHAN_TTL:
        return _CHAN_CACHE["list"]
    out = []
    for ch in load_channels():
        files = _usable_files(ch)
        if not files:
            continue
        c = dict(ch)
        c["_files"] = files
        c["_n"] = len(files)
        out.append(c)
    _CHAN_CACHE["at"] = now
    _CHAN_CACHE["list"] = out
    return out


def channels_summary():
    """给界面/自检看：本机探测到哪些通道、各有多少正文。"""
    return [{"key": c.get("key"), "label": c.get("label"), "fmt": c.get("fmt"),
             "dir": _expand(c.get("dir")), "n": c.get("_n") or 0}
            for c in available_channels()]


def candidate_files():
    """本机所有可用通道里的正文文件（按 mtime 降序），每项 {path, fmt, channel, label}。"""
    res = []
    for ch in available_channels():          # 通道清单走缓存（探测较贵）
        for f in _usable_files(ch):          # 文件清单每次现扫：新会话要立刻能被发现
            res.append({"path": f, "fmt": ch.get("fmt") or "auto",
                        "channel": ch.get("key") or "",
                        "label": ch.get("label") or ch.get("key") or ""})
    res.sort(key=lambda x: _safe_mtime(x["path"]), reverse=True)
    return res


def _candidate_files():
    """兼容老调用：只要路径（新代码请用 candidate_files()，它带 fmt/通道名）。"""
    return [c["path"] for c in candidate_files()]


def fmt_of(path):
    for c in candidate_files():
        try:
            if os.path.normcase(os.path.abspath(c["path"])) == \
                    os.path.normcase(os.path.abspath(path)):
                return c["fmt"]
        except OSError:
            continue
    return "auto"


def rollout_dir():
    return _h(".zcode", "cli", "rollout")


def newest_transcript():
    fs = [f for f in glob.glob(os.path.join(rollout_dir(), "model-io-*.jsonl"))
          if os.path.isfile(f)]
    return max(fs, key=os.path.getmtime) if fs else ""


def _clip(s, n=110):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


def _first_line(s, n=110):
    for ln in str(s or "").splitlines():
        if ln.strip():
            return _clip(ln, n)
    return ""


def _para(s, n=800):
    """思考 / 说明用：**保留换行**（`_clip` 会把换行压成空格，段落结构就没了），超长才截断。

    2026-09-21 用户要求"把 agent 返回的这些信息返回回来"——它界面上是带段落、
    带分点的整段思考，压成一行就只剩个开头，看不出判断过程。前端 `white-space:pre-wrap`
    照原样折行显示，超两行折起来、点一下展开。
    """
    t = "\n".join(ln.rstrip() for ln in str(s or "").splitlines() if ln.strip())
    return t if len(t) <= n else t[:n - 1] + "…"


def describe_tool(name, args):
    """一个工具调用 → (kind, 文本)。认不出来的就报个中性名，别糊界面。"""
    kind = TOOL_KIND.get(str(name or "").strip().lower(), "tool")
    a = args if isinstance(args, dict) else {}
    if kind == "term":
        # 命令留全一些（220 字）：ZCode 界面里就是整条命令，只给一行开头看不出它在干嘛
        return kind, _clip(a.get("command") or a.get("cmd") or "", 220)
    if kind == "read":
        off = a.get("offset")
        return kind, (_clip(a.get("file_path") or a.get("path") or "", 160)
                      + ("（第 %s 行起）" % off if off else ""))
    if kind in ("write", "edit"):
        return kind, _clip(a.get("file_path") or a.get("path") or "", 160)
    if kind == "todo":
        todos = a.get("todos") or []
        done = sum(1 for t in todos if isinstance(t, dict) and t.get("status") == "completed")
        return kind, "%d 项（已完成 %d）" % (len(todos), done)
    if kind == "ask":
        qs = a.get("questions") or []
        q = ""
        if qs and isinstance(qs[0], dict):
            q = qs[0].get("question") or ""
        return kind, _clip(q) or "有问题要问你"
    if kind == "find":
        return kind, _clip(a.get("pattern") or a.get("query") or a.get("path") or "")
    return kind, _clip(name)


def _ev(kind, text, ms=0, tool="", at=""):
    """统一事件形状。图标/标签一起带上——**只在这里定义一份**，
    前端照着画就行，免得两边各维护一份映射走样。"""
    return {"t": at, "kind": kind, "ms": int(ms or 0), "tool": tool,
            "text": text, "icon": KIND_ICON.get(kind, "·"),
            "label": KIND_LABEL.get(kind, kind)}


def _as_args(a):
    """工具参数可能是 JSON 字符串或已解析的 dict（各 harness 不一）。"""
    if isinstance(a, dict):
        return a
    if isinstance(a, str):
        try:
            return json.loads(a)
        except (ValueError, TypeError):
            return {}
    return {}


def _sniff_fmt(o):
    """按结构猜这是哪家的正文（认不出就 generic）。"""
    if isinstance(o.get("response"), dict):
        return "zcode"                                   # ZCode：嵌套 response
    t = o.get("type")
    if t in ("reasoning", "function_call", "function_call_result", "message"):
        return "workbuddy"                               # WorkBuddy：顶层 type 条目
    if t in ("assistant", "user") and isinstance(o.get("message"), dict):
        return "claude"                                  # Claude Code：消息包在 message 里
    return "generic"


def _parse_zcode(o, at=None):
    """ZCode：`response{reasoningText, text, toolCalls[]}`（嵌套）。"""
    resp = o.get("response") or {}
    ts = o.get("startedAt") or o.get("completedAt") or ""
    hhmm = ""
    if isinstance(ts, str) and "T" in ts:
        hhmm = ts.split("T")[1][:8]                  # UTC，只当"序号/间隔"看，不当本地时间
    out = []
    ms = int(o.get("durationMs") or 0)
    think = resp.get("reasoningText") or resp.get("reasoning") or ""
    if think:
        # 思考**不再只取首行**（2026-09-21 用户截图：ZCode 界面里是整段思考，工作台只给一行
        # 摘要 → 等于看不到它到底怎么判断的）。这里留 800 字，前端 CSS 折两行 + 点一下展开。
        out.append(_ev("think", _para(think, 800), ms=ms, at=hhmm))
    for tc in (resp.get("toolCalls") or []):
        if not isinstance(tc, dict):
            continue
        name = tc.get("name") or (tc.get("function") or {}).get("name")
        args = tc.get("input")
        if args is None:
            args = (tc.get("function") or {}).get("arguments")
        k, txt = describe_tool(name, _as_args(args))
        out.append(_ev(k, txt, tool=name or "", at=hhmm))
    say = resp.get("text") or ""
    if say and not (resp.get("toolCalls")):
        out.append(_ev("say", _para(say, 800), at=hhmm))
    return out


def _parse_wb(o, at=None):
    """WorkBuddy：顶层 `type` 条目（reasoning / function_call / message）。"""
    t = o.get("type")
    out = []
    if t == "reasoning":
        txt = o.get("content") or o.get("rawContent") or ""
        if isinstance(txt, list):                 # content 可能是 [{type,text}]
            txt = " ".join(str(it.get("text", "")) for it in txt
                           if isinstance(it, dict)).strip()
        if isinstance(txt, str):
            txt = _para(txt, 800)
        if txt:
            out.append(_ev("think", txt, at=at))
    elif t == "function_call":
        name = o.get("name") or ""
        k, txt = describe_tool(name, _as_args(o.get("arguments")))
        out.append(_ev(k, txt, tool=name or "", at=at))
    elif t == "message" and o.get("role") == "assistant":
        txt = ""
        c = o.get("content")
        if isinstance(c, list):
            for it in c:
                if isinstance(it, dict) and it.get("type") in ("output_text", "text"):
                    txt = _para(it.get("text"), 800)
                    break
        elif isinstance(c, str):
            txt = _para(c, 800)
        if txt:
            out.append(_ev("say", txt, at=at))
    return out


def _parse_claude(o, at=None):
    """Claude Code：`{type: assistant, message:{content:[{type:text|tool_use|thinking}]}}`。"""
    if o.get("type") != "assistant":
        return []
    msg = o.get("message") or {}
    ct = msg.get("content")
    out = []
    if isinstance(ct, list):
        for it in ct:
            if not isinstance(it, dict):
                continue
            k = it.get("type")
            if k in ("text", "output_text"):
                txt = _para(it.get("text"), 800)
                if txt:
                    out.append(_ev("say", txt, at=at))
            elif k == "tool_use":
                nm = it.get("name") or ""
                kk, txt = describe_tool(nm, it.get("input"))
                out.append(_ev(kk, txt, tool=nm, at=at))
            elif k == "thinking":
                txt = _para(it.get("thinking"), 800)
                if txt:
                    out.append(_ev("think", txt, at=at))
    elif isinstance(ct, str):
        txt = _para(ct, 800)
        if txt:
            out.append(_ev("say", txt, at=at))
    return out


def _parse_generic(o, at=None):
    """兜底：尽量从常见键里挖出思考/工具/说明（新工具没配 fmt 时能有个交代）。"""
    out = []
    think = o.get("reasoningText") or o.get("reasoning") or o.get("thinking") or ""
    if isinstance(think, list):
        think = " ".join(str(it.get("text", "")) for it in think
                         if isinstance(it, dict)).strip()
    if isinstance(think, str) and think:
        out.append(_ev("think", _para(think, 800), at=at))
    calls = o.get("toolCalls") or o.get("tool_calls") or []
    if not isinstance(calls, list):
        calls = []
    if isinstance(o.get("function_call"), dict):
        calls = calls + [o["function_call"]]
    for tc in calls:
        if not isinstance(tc, dict):
            continue
        nm = tc.get("name") or (tc.get("function") or {}).get("name") or ""
        args = tc.get("input")
        if args is None:
            args = tc.get("arguments")
        if args is None:
            args = (tc.get("function") or {}).get("arguments")
        k, txt = describe_tool(nm, _as_args(args))
        out.append(_ev(k, txt, tool=nm, at=at))
    if not out:
        # `result` 是 stream-json 那种"最终答复"字段（WorkBuddy/Claude 流式输出的收尾行）
        c = o.get("content") or o.get("text") or o.get("result") or ""
        if isinstance(c, list):
            txt2 = ""
            for it in c:
                if isinstance(it, dict) and it.get("type") in ("text", "output_text"):
                    txt2 = it.get("text")
                    break
            c = txt2
        if isinstance(c, str) and c:
            out.append(_ev("say", _para(c, 800), at=at))
    return out


def events_from_line(line, at=None, fmt=None):
    """一条会话正文 → 若干事件（思考 / 说明 / 若干工具）。

    fmt 留空或 auto 就按结构猜；指定 zcode / workbuddy / claude 则强制走对应解析。
    """
    try:
        o = json.loads(line)
    except (ValueError, TypeError):
        return []
    if not isinstance(o, dict):
        return []
    f = fmt
    if not f or f == "auto":
        f = _sniff_fmt(o)
    if f == "zcode":
        return _parse_zcode(o, at)
    if f == "workbuddy":
        return _parse_wb(o, at)
    if f == "claude":
        return _parse_claude(o, at)
    return _parse_generic(o, at)


def events_since(path, offset, limit=400, fmt=None):
    """从字节偏移往后读（边跑边追加），返回 (新事件, 新偏移)。"""
    if not (path and os.path.isfile(path)):
        return [], offset
    try:
        size = os.path.getsize(path)
        if size < offset:                    # 文件被换过（新会话）→ 从头读
            offset = 0
        if size == offset:
            return [], offset
        with open(path, "rb") as f:
            f.seek(offset)
            blob = f.read()
            new_off = f.tell()
    except OSError:
        return [], offset
    evs = []
    for raw in blob.decode("utf-8", "replace").splitlines():
        raw = raw.strip()
        if raw:
            evs.extend(events_from_line(raw, fmt=fmt))
    return evs[-limit:], new_off


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="")
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--channels", action="store_true", help="只看本机探测到哪些通道")
    a = ap.parse_args()
    chans = channels_summary()
    if a.channels:
        print("本机探测到的动作流通道（%d 个）：" % len(chans))
        for c in chans:
            print("  · %-10s %-12s fmt=%-9s %5d 个正文  %s"
                  % (c["key"], c["label"], c["fmt"], c["n"], c["dir"]))
        if not chans:
            print("  （一个都没有——确认工具装没装；或写一份 %s 指路，格式见本文件顶部）"
                  % CONFIG_NAME)
        print("\n配置文件查找位置：")
        for p in _config_paths():
            print("  %s %s" % ("[有]" if os.path.isfile(p) else "[无]", p))
        return 0
    p = a.file
    if not p:
        cs = candidate_files()
        p = cs[0]["path"] if cs else newest_transcript()
    if not p:
        print("找不到会话正文。本机通道：")
        for c in chans:
            print("  · %s -> %s（%d 个）" % (c["key"], c["dir"], c["n"]))
        print("\n没通道可用时：写一份 %s（放 exe 旁边或 ~/.heronbo/），格式见本文件顶部。"
              % CONFIG_NAME)
        return 1
    print("正文：%s（%.0f KB）" % (p, os.path.getsize(p) / 1024))
    evs, _ = events_since(p, 0, limit=100000)
    for e in evs[-a.n:]:
        tag = KIND_LABEL.get(e["kind"], e["kind"])
        ms = (" · %.1f 秒" % (e["ms"] / 1000.0)) if e.get("ms") else ""
        print("  %s %-6s%s  %s" % (KIND_ICON.get(e["kind"], "·"), tag, ms, e["text"]))
    print("\n共 %d 条事件（显示最后 %d 条）" % (len(evs), min(a.n, len(evs))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
