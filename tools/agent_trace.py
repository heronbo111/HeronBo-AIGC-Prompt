# -*- coding: utf-8 -*-
"""agent 动作流 → 工作台可显示的事件（思考 / 终端 / 读写 / 待办 / 提问）。

为什么用会话正文而不是 stdout：ZCode CLI 的**流式正文**落在
`~/.zcode/cli/rollout/model-io-<sess_xxx>.jsonl`——你在这家 CLI 界面上看到的
「💡 思考 · 持续了 12 秒 / 终端 / 读取」就是从它渲染的。工作台照同一份数据渲染，
就能把"agent 到底在干什么"放回工作台里（用户 2026-09-18 要求：任务可视化）。
**不动调用方式**（改 `--output-format` 会碰到已经跑通的解析路径，风险不值得）。

每条正文 = 一次模型调用，关键字段：
    durationMs      这次调用花了多久  → 「思考 · 持续了 N 秒」
    reasoningText   思考正文（不是每次都有）
    text            给用户看的说明
    toolCalls[]     {name, input} —— Bash / Read / Write / Edit / TodoWrite / AskUserQuestion

用法（自检）：
    python tools\\agent_trace.py            # 打最近一次会话的动作流（最近 30 条）
    python tools\\agent_trace.py --file X   # 指定正文文件
"""
import glob
import json
import os
import time

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


def rollout_dir():
    return os.path.join(os.environ.get("USERPROFILE") or os.path.expanduser("~"),
                        ".zcode", "cli", "rollout")


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


def describe_tool(name, args):
    """一个工具调用 → (kind, 文本)。认不出来的就报个中性名，别糊界面。"""
    kind = TOOL_KIND.get(str(name or "").strip().lower(), "tool")
    a = args if isinstance(args, dict) else {}
    if kind == "term":
        return kind, _first_line(a.get("command") or a.get("cmd") or "")
    if kind == "read":
        off = a.get("offset")
        return kind, (_clip(a.get("file_path") or a.get("path") or "")
                      + ("（第 %s 行起）" % off if off else ""))
    if kind in ("write", "edit"):
        return kind, _clip(a.get("file_path") or a.get("path") or "")
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


def events_from_line(line, at=None):
    """一条 model_io → 若干事件（思考 / 说明 / 若干工具）。"""
    try:
        o = json.loads(line)
    except (ValueError, TypeError):
        return []
    if not isinstance(o, dict):
        return []
    resp = o.get("response") or {}
    ts = o.get("startedAt") or o.get("completedAt") or ""
    hhmm = ""
    if isinstance(ts, str) and "T" in ts:
        hhmm = ts.split("T")[1][:8]                  # UTC，只当"序号/间隔"看，不当本地时间
    out = []
    ms = int(o.get("durationMs") or 0)
    think = resp.get("reasoningText") or resp.get("reasoning") or ""
    if think:
        out.append(_ev("think", _first_line(think, 120), ms=ms, at=hhmm))
    for tc in (resp.get("toolCalls") or []):
        if not isinstance(tc, dict):
            continue
        name = tc.get("name") or (tc.get("function") or {}).get("name")
        args = tc.get("input")
        if args is None:
            args = (tc.get("function") or {}).get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        k, txt = describe_tool(name, args)
        out.append(_ev(k, txt, tool=name or "", at=hhmm))
    say = resp.get("text") or ""
    if say and not (resp.get("toolCalls")):
        out.append(_ev("say", _first_line(say, 140), at=hhmm))
    return out


def events_since(path, offset, limit=400):
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
            evs.extend(events_from_line(raw))
    return evs[-limit:], new_off


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="")
    ap.add_argument("--n", type=int, default=30)
    a = ap.parse_args()
    p = a.file or newest_transcript()
    if not p:
        print("找不到会话正文（%s）" % rollout_dir())
        return 1
    print("正文：%s（%.0f KB）\n" % (p, os.path.getsize(p) / 1024))
    evs, _ = events_since(p, 0, limit=100000)
    for e in evs[-a.n:]:
        tag = KIND_LABEL.get(e["kind"], e["kind"])
        ms = (" · %.1f 秒" % (e["ms"] / 1000.0)) if e.get("ms") else ""
        print("  %s %-6s%s  %s" % (KIND_ICON.get(e["kind"], "·"), tag, ms, e["text"]))
    print("\n共 %d 条事件（显示最后 %d 条）" % (len(evs), min(a.n, len(evs))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
