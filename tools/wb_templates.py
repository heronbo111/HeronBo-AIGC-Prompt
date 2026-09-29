# -*- coding: utf-8 -*-
r"""指令模板库（2026-09-28 融合自妙搭「豆包指令桥」的 command_templates 能力）。

为什么要有它：工作台的 agent 任务原先只有四种预设 todo（出提示词/按反馈重出/核对归类/评价反哺），
用户反复用的"自定义指令"每次都要重打一遍。模板库把常用指令存成条目（名称 + 正文 + 使用次数），
界面上一键填充到需求框，或直接作为「自定义指令」任务叫 agent 执行。

存储：`~/.heronbo/指令模板.json`（环境变量 `HERONBO_TEMPLATES` 可指向别的文件；
与 `agent_bridge.local.json` 的"本机个人配置"哲学一致——不进 git、换机不冲突）。
结构对齐妙搭版 agent_tasks/command_templates：

.. code-block:: json

    [ {"id": "…uuid…", "name": "出竖版口播", "content": "…指令正文…",
       "useCount": 3, "createdAt": 1695873600.0, "updatedAt": 1695873600.0} ]

线程安全：workbench_server 是多线程 HTTP 服务，这里一把模块级锁兜住读改写。
写入用"临时文件 + os.replace"原子替换，断电/崩溃不会留下半截 JSON。
"""
import json
import os
import tempfile
import threading
import time
import uuid

_LOCK = threading.Lock()


def path():
    p = os.environ.get("HERONBO_TEMPLATES") or ""
    if p.strip():
        return os.path.normpath(p.strip())
    return os.path.join(os.path.expanduser("~"), ".heronbo", "指令模板.json")


def _read_all():
    fp = path()
    try:
        with open(fp, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:                                            # noqa: BLE001
        return []
    return data if isinstance(data, list) else []


def _write_all(items):
    fp = path()
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="_tpl_", suffix=".json",
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


def list_templates():
    with _LOCK:
        items = _read_all()
    items.sort(key=lambda t: -(t.get("useCount") or 0))
    return items


def add(name, content):
    name = (name or "").strip()
    content = (content or "").rstrip()
    if not name:
        return {"ok": False, "error": "模板名称不能为空"}
    if not content:
        return {"ok": False, "error": "指令正文不能为空"}
    now = time.time()
    item = {"id": str(uuid.uuid4()), "name": name[:50], "content": content[:8000],
            "useCount": 0, "createdAt": now, "updatedAt": now}
    with _LOCK:
        items = _read_all()
        if any(t.get("name") == name for t in items):
            return {"ok": False, "error": "已有同名模板「%s」" % name}
        items.append(item)
        _write_all(items)
    return {"ok": True, "item": item}


def update(tid, name, content):
    name = (name or "").strip()
    content = (content or "").rstrip()
    if not name or not content:
        return {"ok": False, "error": "名称与正文都不能为空"}
    with _LOCK:
        items = _read_all()
        hit = None
        for t in items:
            if t.get("id") == tid:
                hit = t
            elif t.get("name") == name and (t.get("id") != tid):
                return {"ok": False, "error": "已有同名模板「%s」" % name}
        if hit is None:
            return {"ok": False, "error": "模板不存在（可能已被删除）"}
        hit["name"] = name[:50]
        hit["content"] = content[:8000]
        hit["updatedAt"] = time.time()
        _write_all(items)
        return {"ok": True, "item": hit}


def delete(tid):
    with _LOCK:
        items = _read_all()
        rest = [t for t in items if t.get("id") != tid]
        if len(rest) == len(items):
            return {"ok": False, "error": "模板不存在（可能已被删除）"}
        _write_all(rest)
    return {"ok": True}


def mark_use(tid):
    with _LOCK:
        items = _read_all()
        for t in items:
            if t.get("id") == tid:
                t["useCount"] = int(t.get("useCount") or 0) + 1
                t["updatedAt"] = time.time()
                _write_all(items)
                return {"ok": True, "useCount": t["useCount"]}
    return {"ok": False, "error": "模板不存在"}
