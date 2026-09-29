# -*- coding: utf-8 -*-
r"""指令桥：三通道任务队列（2026-09-28 融合自妙搭「豆包指令桥」的 agent_tasks 能力）。

三条执行通道（对齐妙搭版 agent_tasks.channel）：

  cdp    豆包桌面 CDP——走工作台现有 agent 桥（agent_bridge.ask()），带技能上下文，慢但准
  plugin 豆包 API 直连——火山方舟 chat/completions，轻任务（改一句话、润色）不用起整个
         CLI 会话；key/model 配在 `~/.heronbo/桥配置.json`（不进 git，换机不冲突）
  mcp    外部 Agent 领取——任务保持 pending，等外部 Agent（Claude Code 等）经
         GET /api/bridge?pull=1&channel=mcp 原子领取、POST /api/bridge {action:"submit"}
         回写（端点只绑 127.0.0.1，跟工作台其他接口同一信任模型）

存储：`~/.heronbo/桥任务.json`（环境变量 `HERONBO_BRIDGE` 可指向别的文件）。
领取语义：pull 在锁内「挑最老 pending → 改 running → 落盘」一把完成，并发领取不重复；
任务终结（completed/failed）后不可再回写。

服务端在 create 时注入 runner（cdp 通道的执行器），本模块不 import agent_bridge——
缺它/换实现都不连坐（对齐 workbench_server 的 _load_core 降级哲学）。
"""
import json
import os
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid

_LOCK = threading.RLock()

CHANNELS = ("cdp", "plugin", "mcp")


def path():
    p = os.environ.get("HERONBO_BRIDGE") or ""
    if p.strip():
        return os.path.normpath(p.strip())
    return os.path.join(os.path.expanduser("~"), ".heronbo", "桥任务.json")


def cfg():
    """plugin 通道配置：`~/.heronbo/桥配置.json`（HERONBO_BRIDGE_CFG 可指向别的文件）。

    字段：ark_api_key（火山方舟 API Key，必填）、ark_model（豆包模型名，必填）、
    ark_base_url（默认方舟北京站）。缺哪个，plugin 任务就直接 failed 并写清缺什么。
    """
    fp = os.environ.get("HERONBO_BRIDGE_CFG") or ""
    fp = fp.strip() or os.path.join(os.path.expanduser("~"), ".heronbo", "桥配置.json")
    try:
        with open(fp, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:                                            # noqa: BLE001
        return {}


def _read_all():
    try:
        with open(path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:                                            # noqa: BLE001
        return []
    return data if isinstance(data, list) else []


def _write_all(items):
    fp = path()
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="_btask_", suffix=".json",
                               dir=os.path.dirname(fp))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=1)
        os.replace(tmp, fp)
    except Exception:                                            # noqa: BLE001
        try:
            os.unlink(tmp)
        except Exception:                                        # noqa: BLE001
            pass
        raise


def list_tasks():
    with _LOCK:
        items = _read_all()
    items.sort(key=lambda t: -(t.get("createdAt") or 0))
    return {"items": items}


def create(title, content, channel, runner=None):
    """落一条指令任务并按通道分流。

    返回 {"ok": True, "task": …}；执行失败不算 create 失败——任务以 failed 落库、
    errorMessage 写清原因（界面上能看到为什么）。
    """
    content = (content or "").rstrip()
    if not content:
        return {"ok": False, "error": "指令正文不能为空"}
    if channel not in CHANNELS:
        return {"ok": False, "error": "不认识的通道：%s（只有 cdp / plugin / mcp）" % channel}
    title = (title or "").strip()[:60] or content.strip()[:24]
    now = time.time()
    task = {"id": str(uuid.uuid4()), "title": title, "content": content[:20000],
            "channel": channel, "status": "pending", "result": "", "errorMessage": "",
            "createdAt": now, "updatedAt": now}
    with _LOCK:
        items = _read_all()
        if channel == "mcp":
            items.append(task)                                   # 等外部 Agent 领取
        elif channel == "cdp":
            if runner is None:
                task["status"] = "failed"
                task["errorMessage"] = "cdp 通道不可用：缺 agent_bridge.py（桥执行器没注入）"
            else:
                task["status"] = "running"
            items.append(task)
            if task["status"] == "running":
                t = threading.Thread(target=_run_cdp, args=(task["id"], content, runner),
                                     daemon=True)
                t.start()
        else:                                                    # plugin
            c = cfg()
            missing = [k for k in ("ark_api_key", "ark_model") if not (c.get(k) or "").strip()]
            if missing:
                task["status"] = "failed"
                task["errorMessage"] = (
                    "plugin 通道未配置：缺 %s。到 ~/.heronbo/桥配置.json 里补上（不进 git）" % "、".join(missing))
            else:
                task["status"] = "running"
            items.append(task)
            if task["status"] == "running":
                t = threading.Thread(target=_run_plugin, args=(task["id"], content), daemon=True)
                t.start()
        _write_all(items)
    return {"ok": True, "task": task}


def pull(channel):
    """原子领取一条待执行任务（pending → running），没有就返回 {"task": None}。"""
    with _LOCK:
        items = _read_all()
        cand = None
        for t in items:
            if t.get("status") == "pending" and t.get("channel") == channel:
                if cand is None or (t.get("createdAt") or 0) < (cand.get("createdAt") or 0):
                    cand = t
        if cand is None:
            return {"task": None}
        cand["status"] = "running"
        cand["updatedAt"] = time.time()
        _write_all(items)
        return {"task": cand}


def submit(tid, ok, result="", error_message=""):
    """外部执行方回写结果。任务不存在 / 没被领取过（pending）/ 已终结 → 拒绝。"""
    with _LOCK:
        items = _read_all()
        for t in items:
            if t.get("id") == tid:
                if t.get("status") not in ("running",):
                    return {"ok": False, "error": "任务不在执行中（当前 %s），不能回写"
                            % (t.get("status") or "?")}
                t["status"] = "completed" if ok else "failed"
                t["result"] = (result or "")[:100000]
                t["errorMessage"] = (error_message or "")[:2000]
                t["updatedAt"] = time.time()
                _write_all(items)
                return {"ok": True, "task": t}
        return {"ok": False, "error": "任务不存在（可能已被删）"}


def _finish(tid, ok, result="", error=""):
    with _LOCK:
        items = _read_all()
        for t in items:
            if t.get("id") == tid and t.get("status") == "running":
                t["status"] = "completed" if ok else "failed"
                t["result"] = (result or "")[:100000]
                t["errorMessage"] = (error or "")[:2000]
                t["updatedAt"] = time.time()
                _write_all(items)
                return


def _run_cdp(tid, content, runner):
    try:
        r = runner(content) or {}
        _finish(tid, bool(r.get("ok")), r.get("text") or "",
                r.get("error") or ("" if r.get("ok") else "agent 桥返回失败"))
    except Exception as e:                                       # noqa: BLE001
        _finish(tid, False, "", "cdp 执行出错：%s" % e)


def _run_plugin(tid, content):
    try:
        r = _ark_chat(content)
        _finish(tid, bool(r.get("ok")), r.get("text") or "", r.get("error") or "")
    except Exception as e:                                       # noqa: BLE001
        _finish(tid, False, "", "plugin 执行出错：%s" % e)


def _ark_chat(content):
    """火山方舟 chat/completions 一次性返回（轻任务不做流式）。"""
    c = cfg()
    base = (c.get("ark_base_url") or "https://ark.cn-beijing.apigw.com/api/v3").rstrip("/")
    payload = {"model": c["ark_model"], "messages": [{"role": "user", "content": content}]}
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer %s" % c["ark_api_key"]},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8")[:300]
        except Exception:                                        # noqa: BLE001
            body = ""
        return {"ok": False, "error": "方舟 API HTTP %s：%s" % (e.code, body)}
    except Exception as e:                                       # noqa: BLE001
        return {"ok": False, "error": "方舟 API 调不通：%s" % e}
    try:
        text = data["choices"][0]["message"]["content"]
    except Exception:                                            # noqa: BLE001
        return {"ok": False, "error": "方舟返回结构不认识：%s" % str(data)[:300]}
    return {"ok": True, "text": text}
