# -*- coding: utf-8 -*-
"""wb_cli.py —— 豆包 CLI 桥（2026-11-12，替代 doubao_cdp.mjs 的主通道）。

为什么要有：
  工作台原来靠 CDP 驱动豆包工作客户端，窗口/会话一卡就超时，且必须让豆包
  客户端保持打开。豆包 CLI 已开放，图片/视频技能可直接在 CLI 会话里跑，
  但 CLI 是「自然语言 Agent」而不是一个稳定的 --image/--video 子命令，
  所以这层桥把工作台的结构化参数翻译成一段受控任务，并负责：
    1. 找到 CLI（node + @bytedance/doubao-cli/scripts/run.cjs）；
    2. 复用用户 `doubao login` 的登录态；
    3. 让 CLI 把生成文件落到指定目录，读回真实路径；
    4. 把进度/取消暴露给 wb_video / wb_storyboard。

设计边界：
  - 不在 prepare 阶段生成任何东西、不扣额度；只有 confirm/generate 才发 CLI。
  - 生成失败不伪造文件；找不到输出就返回明确错误。
  - 仍保留 CDP 兜底（设置 HERONBO_VIDEO_BRIDGE=cdp 可强制回退）。
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_LOCK = threading.Lock()
_CACHE = {"path": "", "node": "", "auth": None, "auth_at": 0.0, "why": ""}
_PROCS = {}
_LOGIN_LOCK = threading.Lock()
_LOGIN = {}

# ── SYSTEM 通道（2026-11-14）────────────────────────────────────────────────
# 本机普通用户进程访问 CLI 安全存储被拦（连全新本地账号都复现），SYSTEM 身份
# 正常。tools\cli_channel.json 写 {"run_as": "system"}，或设环境变量
# HB_CLI_RUNAS=system，即可把所有 CLI 调用交给 SYSTEM 计划任务执行。
_CHANNEL_CFG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "cli_channel.json")


def _system_mode():
    env = (os.environ.get("HB_CLI_RUNAS") or "").strip().lower()
    if env in ("system", "1", "true", "on"):
        return True
    try:
        with open(_CHANNEL_CFG, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return str(cfg.get("run_as") or "").strip().lower() == "system"
    except Exception:                                            # noqa: BLE001
        return False


def _broker():
    try:
        import cli_system_broker as _b
        return _b
    except Exception:                                            # noqa: BLE001
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import cli_system_broker as _b
            return _b
        except Exception:                                        # noqa: BLE001
            return None


def _sess_of(text):
    for line in (text or "").splitlines()[:5]:
        try:
            o = json.loads(line.strip())
            if isinstance(o, dict) and o.get("type") == "session":
                return str(o.get("session_id") or "")
        except Exception:                                        # noqa: BLE001
            pass
    return ""

_MODEL_MAP = {
    "Seedance 2.5": "seedance_2.5",
    "Seedance 2.0 Fast（720p）": "seedance_2.0_fast",
    "Seedance 2.0 Fast": "seedance_2.0_fast",
    "Seedance 2.0": "seedance_2.0",
    "Seedance 2.0 mini": "seedance_2.0_mini",
}


def _clean(s):
    return _ANSI.sub("", str(s or "")).replace("\r\n", "\n").replace("\r", "\n")


def _version_key(path):
    try:
        return (os.path.getmtime(path), path)
    except OSError:
        return (0, path)


def _app_bases():
    """wb_cli.py 所在应用目录的若干层（仓库/安装版/绿色版布局都能覆盖）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    bases = []
    for _ in range(6):
        if here and here not in bases:
            bases.append(here)
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    return bases


def _bundled_paths():
    """安装包/绿色版自带的 CLI 布局。

    runtime 里放两样东西：
      runtime\\node\\node_modules\\@bytedance\\doubao-cli\\scripts\\run.cjs
      runtime\\doubao_cli\\app\\current\\doubao.exe
    run.cjs 的 appRoot 固定读 %LOCALAPPDATA%\\doubao_cli\\app。包内程序不能原地跑：
    360 等安全软件会拦截安装在非标准目录里的 doubao.exe，表现为 spawn EPERM /
    拒绝访问。所以第一次用自带 CLI 时，把 app\\current 复制到官方用户目录
    %LOCALAPPDATA%\\doubao_cli\\app\\current，再让 run.cjs 从那里启动（见 _cli_env）。
    """
    found = []
    for base in _app_bases():
        p = os.path.join(base, "runtime", "node", "node_modules", "@bytedance",
                         "doubao-cli", "scripts", "run.cjs")
        if os.path.isfile(p):
            found.append(os.path.abspath(p))
    return found


def _local_cli_root():
    """CLI 官方用户目录；360 对这里的 doubao.exe 放行，对安装目录里的同文件会拦。"""
    local = os.environ.get("LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(local, "doubao_cli")


def _install_bundle_to_local(bundle_app, local_app):
    """把包内 app\\current 原子复制到官方目录，保留同级的登录态/缓存。"""
    if not os.path.isdir(bundle_app):
        return False
    bundle_exe = os.path.join(bundle_app, "doubao.exe")
    local_exe = os.path.join(local_app, "doubao.exe")
    if not os.path.isfile(bundle_exe):
        return False
    # 已有同目录 CLI 且比包内文件大（通常是更高版本）时保留用户版本，不覆盖。
    if os.path.isfile(local_exe):
        try:
            if os.path.getsize(local_exe) >= os.path.getsize(bundle_exe):
                return True
        except OSError:
            return True
    tmp = local_app + ".installing-%s" % os.getpid()
    try:
        parent = os.path.dirname(local_app)
        os.makedirs(parent, exist_ok=True)
        if os.path.isdir(tmp):
            shutil.rmtree(tmp, ignore_errors=True)
        shutil.copytree(bundle_app, tmp)
        if os.path.isdir(local_app):
            shutil.rmtree(local_app, ignore_errors=True)
        os.replace(tmp, local_app)
        return True
    except Exception:                                           # noqa: BLE001
        if os.path.isdir(tmp):
            shutil.rmtree(tmp, ignore_errors=True)
        return os.path.isfile(local_exe)


def _bundled_runtime(path):
    """若 path 来自本应用 runtime，返回给子进程用的官方 LOCALAPPDATA 根。

    先把包内 app\\current 复制到 %LOCALAPPDATA%\\doubao_cli\\app\\current；
    360 会拦截安装目录里的同名程序，复制到官方目录后才能正常 spawn。
    """
    p = os.path.abspath(path)
    marker = os.sep + "runtime" + os.sep + "node" + os.sep + "node_modules"
    if marker not in p:
        return ""
    root = p.split(marker, 1)[0] + os.sep + "runtime"
    bundle_app = os.path.join(root, "doubao_cli", "app", "current")
    if not os.path.isdir(bundle_app):
        return ""
    local_root = _local_cli_root()
    local_app = os.path.join(local_root, "app", "current")
    if not _install_bundle_to_local(bundle_app, local_app):
        return ""
    # LOCALAPPDATA 是 doubao_cli 的父目录；返回 doubao_cli 会让 run.cjs
    # 再拼一次 doubao_cli\app，形成 ...\doubao_cli\doubao_cli\app 的坏路径。
    return os.path.dirname(local_root)


def _candidates():
    """返回可用的 run.cjs 候选；不执行，不猜不存在的目录。"""
    out = []
    env = os.environ.get("HERONBO_DOUBAO_CLI_HOME") or ""
    if env:
        out.append(os.path.join(env, "scripts", "run.cjs") if os.path.isdir(env)
                   else env)
    local = os.environ.get("LOCALAPPDATA") or ""
    roots = []
    if local:
        roots.append(os.path.join(local, "DoubaoWork", "User Data",
                                  "sandbox_runtime", "bases"))
        roots.append(os.path.join(local, "DoubaoWork", "User Data",
                                  "sandbox_runtime", "node", "node_modules"))
    appdata = os.environ.get("APPDATA") or ""
    if appdata:
        roots.append(os.path.join(appdata, "npm", "node_modules"))
    # 当前解释器所在盘/用户目录，避免只依赖一个环境变量
    roots.append(os.path.join(os.path.expanduser("~"), "AppData", "Local",
                              "DoubaoWork", "User Data", "sandbox_runtime",
                              "bases"))
    for root in roots:
        if not os.path.isdir(root):
            continue
        # sandbox_runtime/bases/<hash>/node/node_modules/@bytedance/doubao-cli/scripts/run.cjs
        try:
            for d in os.listdir(root):
                p = os.path.join(root, d, "node", "node_modules", "@bytedance",
                                 "doubao-cli", "scripts", "run.cjs")
                if os.path.isfile(p):
                    out.append(p)
        except OSError:
            pass
        p = os.path.join(root, "@bytedance", "doubao-cli", "scripts", "run.cjs")
        if os.path.isfile(p):
            out.append(p)
    # 安装包/绿色版自带的 CLI 放最后，优先复用用户已登录的系统 CLI。
    out.extend(_bundled_paths())
    # PATH 里的 doubao.cmd 反推包目录（npm shim 的常见布局）
    for name in ("doubao.cmd", "doubao.exe", "doubao"):
        p = shutil.which(name)
        if not p:
            continue
        d = os.path.dirname(os.path.abspath(p))
        for cand in (os.path.join(d, "node_modules", "@bytedance", "doubao-cli",
                                  "scripts", "run.cjs"),
                     os.path.join(d, "..", "node_modules", "@bytedance", "doubao-cli",
                                  "scripts", "run.cjs")):
            if os.path.isfile(cand):
                out.append(cand)
    # 去重；系统 CLI 优先（通常已在官方目录、登录态可用，也不会被安全软件
    # 当作“安装目录里的陌生 exe”拦截）；没有系统 CLI 时再用包内自带版本。
    seen = {}

    def _rank(p):
        p = os.path.abspath(p)
        bundled = any(p == os.path.abspath(b) for b in _bundled_paths())
        # 新用户没有系统 CLI，自带版本兜底；老用户已装的系统 CLI 优先复用。
        return (1 if bundled else 0, -_version_key(p)[0])

    for p in sorted({os.path.abspath(p) for p in out}, key=_rank):
        seen[p] = 1
    return [p for p in seen if os.path.isfile(p)]


def resolve(force=False):
    """找 node + CLI run.cjs；找不到返回 (None,None,原因)。"""
    with _LOCK:
        if not force and _CACHE["path"] and _CACHE["node"]:
            return _CACHE["path"], _CACHE["node"], ""
        path = ""
        node = ""          # 候选一个都没有时也要有值，不然下面赋值会直接抛异常
        for cand in _candidates():
            node = os.path.abspath(os.path.join(os.path.dirname(cand), "..", "..",
                                                "..", "..", "node.exe"))
            if not os.path.isfile(node):
                node = shutil.which("node") or ""
            if node and os.path.isfile(node):
                path, node = cand, os.path.abspath(node)
                break
        _CACHE["path"], _CACHE["node"] = path, node
        if not path:
            # 2026-11-17 用户要求：不在提示里写包名和依赖，只说结论。
            _CACHE["why"] = "没找到豆包 CLI，可以先用别的通道"
            return None, None, _CACHE["why"]
        _CACHE["why"] = ""
        return path, node, ""


def _cli_env(path):
    """CLI 子进程环境；自带 CLI 时只把 LOCALAPPDATA 指到包内 runtime。"""
    env = os.environ.copy()
    env.setdefault("NO_COLOR", "1")
    bundle_local = _bundled_runtime(path)
    if bundle_local:
        env["LOCALAPPDATA"] = bundle_local
    return env


def _start_hint(why, denied=False):
    """把启动失败的原话翻成用户看得懂的一句，别把路径和错误码甩给人家。"""
    if denied:
        return "豆包程序被安全软件拦住了：去安全软件里允许它运行，或者等几秒再点一次"
    return "豆包程序没能启动：先关掉再点一次，还不行就换通道"


def _run(args, timeout=60, cwd=None, env_extra=None, session_key=""):
    path, node, why = resolve()
    if not path:
        return {"rc": 127, "out": "", "err": why, "session": ""}
    if _system_mode():
        b = _broker()
        if not b:
            return {"rc": 127, "out": "", "err": "SYSTEM 通道模块缺失", "session": ""}
        ok, bwhy = b.available()
        if not ok:
            return {"rc": 127, "out": "", "err": bwhy, "session": ""}
        r = b.run(node, path, [str(a) for a in args], timeout=timeout, cwd=cwd)
        text = _clean(r.get("out") or "")
        err = _clean(r.get("err") or "")
        return {"rc": r.get("rc"), "out": text, "err": err, "session": _sess_of(text)}
    env = _cli_env(path)
    if env_extra:
        env.update(env_extra)
    cmd = [node, path] + [str(a) for a in args]
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = 0x08000000       # CREATE_NO_WINDOW
    p = None
    denied = False
    for attempt in range(3):
        try:
            p = subprocess.Popen(cmd, cwd=cwd or os.path.dirname(path),
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 stdin=subprocess.DEVNULL, env=env,
                                 text=True, encoding="utf-8", errors="replace",
                                 **kw)
            break
        except Exception as e:  # noqa: BLE001
            why = str(e)
            # “拒绝访问”多半是安全软件正在查这个程序，等两秒再试一次就好，
            # 不用让用户自己去点。真被禁止运行的话，三次都过不去。
            denied = any(k in why for k in ("拒绝访问", "Access is denied",
                                            "WinError 5", "EPERM"))
            if denied and attempt < 2:
                time.sleep(2)
                continue
            return {"rc": 1, "out": "", "err": _start_hint(why, denied),
                    "session": ""}
    if p is None:
        return {"rc": 1, "out": "", "err": _start_hint("启动失败", denied),
                "session": ""}
    if session_key:
        with _LOCK:
            _PROCS[session_key] = p

    # 不能一边 communicate() 一边另开线程读 stderr：两者会抢同一个管道，
    # Windows 上容易把输出吞掉甚至卡住。communicate 本身会同时收 stdout/stderr。
    timed_out = False
    try:
        out, errout = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            p.kill()
        except Exception:  # noqa: BLE001
            pass
        out, errout = "", ""
    rc = p.returncode
    with _LOCK:
        if session_key and _PROCS.get(session_key) is p:
            _PROCS.pop(session_key, None)
    text = _clean(out or "")
    err = _clean(errout or "")
    sess = _sess_of(text)
    return {"rc": rc, "out": text, "err": err, "session": sess, "timed_out": timed_out}


def auth_status(force=False, quick=False):
    """登录态；缓存 30 秒，避免每次 prepare 都跑 auth。

    quick=True 用于首次配置页/轮询：不带 --verify，超时 8 秒，CLI 服务冷启动
    慢时返回「未登录或启动较慢」，不把整页检查拖到 40 秒以上。
    """
    with _LOCK:
        if not force and _CACHE["auth_at"] and time.time() - _CACHE["auth_at"] < 30:
            return _CACHE["auth"]
    path, node, why = resolve()
    if not path:
        return {"ok": False, "why": why, "rc": 127}
    args = ["auth", "status"] + ([] if quick else ["--verify"])
    r = _run(args, timeout=8 if quick else 25)
    ok = r["rc"] == 0 and "登录成功" in (r["out"] + r["err"])
    why = ""
    if not ok:
        why = (r["out"] + r["err"]).strip().splitlines()
        why = why[-1] if why else ("豆包 CLI 未登录或启动较慢" if quick else "豆包 CLI 未登录")
        if "login_required" in why or "未登录" in why:
            why = "豆包 CLI 未登录：先在终端运行 doubao login"
    res = {"ok": ok, "why": why, "rc": r["rc"], "detail": (r["out"] + r["err"])[-400:]}
    with _LOCK:
        _CACHE["auth"] = res
        _CACHE["auth_at"] = time.time()
    return res


def available(force=False):
    path, node, why = resolve(force=force)
    if not path:
        return False, why
    a = auth_status(force=force, quick=True)
    return bool(a.get("ok")), ("" if a.get("ok") else str(a.get("why") or ""))


def version():
    path, node, why = resolve()
    if not path:
        return ""
    r = _run(["--version"], timeout=20)
    return (r["out"] + r["err"]).strip().splitlines()[0] if (r["out"] + r["err"]).strip() else ""


# ── 首次配置页扫码登录（2026-11-12）────────────────────────────────────────────
# doubao login --qr=png 会把临时二维码路径打到终端；登录结束后 CLI 会删原图，
# 所以在读到路径时立刻复制到自己的临时文件，再以 data URL 返回给本机页面。
# 二维码只在本机进程内存/临时文件里，不写日志、不打包。
_QR_RE = re.compile(r"([A-Za-z]:[\\/][^\r\n\"<>|]+?\.png|/[^\r\n\"<>|]+?\.png)", re.I)


def _qr_data(path):
    try:
        with open(path, "rb") as f:
            return "data:image/png;base64," + base64.b64encode(f.read()).decode("ascii")
    except OSError:
        return ""


def _login_payload(entry, state=""):
    qr_data = entry.get("qrData") or _qr_data(entry.get("qr") or "")
    return {"ok": True, "state": state or entry.get("state") or "pending",
            "id": entry.get("id") or "", "qr": qr_data,
            "message": entry.get("message") or "",
            "pid": entry.get("proc").pid if entry.get("proc") else 0}


def _login_reader(proc, entry):
    lines = []
    try:
        for raw in iter(proc.stdout.readline, ""):
            line = _clean(raw).strip()
            if not line:
                continue
            lines.append(line)
            m = _QR_RE.search(line)
            if m and not entry.get("qr"):
                src = m.group(1).strip().strip('"').strip("'")
                src = os.path.abspath(src)
                qr_copy = entry.get("qrFile") or ""
                if src and qr_copy:
                    try:
                        shutil.copy2(src, qr_copy)
                        with _LOGIN_LOCK:
                            entry["qr"] = qr_copy
                            entry["qrData"] = _qr_data(qr_copy)
                            entry["message"] = "二维码已生成，请用豆包 App 扫码"
                    except OSError:
                        with _LOGIN_LOCK:
                            entry["message"] = "二维码读取失败，请点重新生成"
    except Exception as e:                                      # noqa: BLE001
        lines.append("读取二维码输出失败：%s" % str(e)[:120])
    finally:
        try:
            rc = proc.wait()
        except Exception:                                       # noqa: BLE001
            rc = -1
        with _LOGIN_LOCK:
            entry["rc"] = rc
            entry["ended"] = True
            entry["out"] = "\n".join(lines)[-1600:]
            if rc != 0:
                entry["message"] = "登录没成功：%s" % (
                    entry["out"].splitlines()[-1] if entry["out"] else "未登录")
            elif not entry.get("qr"):
                entry["message"] = "登录进程已结束，但没有生成二维码；请重新生成"


def _login_clean(entry, remove_qr=True):
    """结束进程并清理二维码；不在这里做 auth_status，避免持锁执行子进程。"""
    if not entry:
        return
    proc = entry.get("proc")
    try:
        if proc and proc.poll() is None:
            proc.kill()
    except Exception:                                           # noqa: BLE001
        pass
    job = entry.get("job")
    if job:
        b = _broker()
        if b:
            try:
                b.cancel(job)
                b.cleanup(job, drop_files=False)
            except Exception:                                   # noqa: BLE001
                pass
    if remove_qr:
        qr = entry.get("qr") or ""
        if qr and os.path.isfile(qr):
            try:
                os.remove(qr)
            except OSError:
                pass


def _login_watch_system(b, job, entry):
    """SYSTEM 通道的登录看护：轮询任务输出，拿到二维码就复制并转 data URL。"""
    deadline = time.time() + 300
    while time.time() < deadline:
        st = b.poll(job)
        out = st.get("out") or ""
        qr_path = st.get("qr") or ""
        if qr_path and not entry.get("qr"):
            data = _qr_data(qr_path)
            if data:
                with _LOGIN_LOCK:
                    entry["qr"] = qr_path
                    entry["qrData"] = data
                    entry["message"] = "二维码已生成，请用豆包 App 扫码"
        if st.get("done"):
            lines = [l for l in out.splitlines() if l.strip()]
            with _LOGIN_LOCK:
                entry["ended"] = True
                entry["rc"] = st.get("rc")
                entry["out"] = out[-1600:]
                if st.get("rc") not in (0, None):
                    entry["message"] = "登录没成功：%s" % (
                        lines[-1] if lines else "未登录")
                elif not entry.get("qr"):
                    entry["message"] = "登录进程结束，但没有生成二维码；请重新生成"
            b.cleanup(job, drop_files=False)
            return
        time.sleep(0.8)


def _login_start_system(path, node):
    b = _broker()
    if not b:
        return {"ok": False, "error": "SYSTEM 通道模块缺失"}
    ok, why = b.available()
    if not ok:
        return {"ok": False, "error": why}
    with _LOGIN_LOCK:
        cur = _LOGIN.get("job")
        if cur:
            st = b.poll(cur)
            if _LOGIN.get("qr") or not st.get("done"):
                return _login_payload(_LOGIN)
            _login_clean(_LOGIN)
            _LOGIN.clear()
        lid = "login-%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), os.getpid())
        qr_copy = os.path.join(tempfile.gettempdir(), "heronbo_cli_%s.png" % lid)
        try:
            job = b.start(node, path, ["login", "--qr=png"], qr_dst=qr_copy)
        except Exception as e:                                  # noqa: BLE001
            return {"ok": False, "error": "SYSTEM 通道启动登录失败：%s" % str(e)[:160]}
        entry = {"id": lid, "job": job, "proc": None, "qrFile": qr_copy,
                 "qr": "", "qrData": "", "state": "pending",
                 "message": "正在生成二维码…", "ended": False, "rc": None,
                 "out": "", "reader": None}
        _LOGIN.clear()
        _LOGIN.update(entry)
        t = threading.Thread(target=_login_watch_system, args=(b, job, _LOGIN),
                             daemon=True)
        _LOGIN["reader"] = t
        t.start()
    for _ in range(24):
        with _LOGIN_LOCK:
            if _LOGIN.get("qr"):
                return _login_payload(_LOGIN)
            if _LOGIN.get("ended"):
                break
        time.sleep(0.25)
    with _LOGIN_LOCK:
        if _LOGIN.get("qr"):
            return _login_payload(_LOGIN)
        if _LOGIN.get("ended"):
            out = (_LOGIN.get("out") or "").strip()
            last = out.splitlines()[-1].strip() if out else ""
            err = last or "登录进程结束，但没有生成二维码"
            detail = out[-1200:]
            _login_clean(_LOGIN)
            _LOGIN.clear()
            return {"ok": False, "error": err, "detail": detail}
        return {"ok": True, "state": "pending", "id": _LOGIN.get("id") or "",
                "qr": "", "message": "二维码还在生成，请稍后刷新"}


def login_start():
    """生成登录二维码；已登录则直接返回 done。"""
    path, node, why = resolve()
    if not path:
        return {"ok": False, "error": why}
    if _system_mode():
        return _login_start_system(path, node)

    with _LOGIN_LOCK:
        cur = _LOGIN.get("proc")
        reuse = cur is not None and cur.poll() is None
        if reuse:
            # 已有登录进程：二维码已拿到就直接复用，没拿到就等它输出
            if _LOGIN.get("qr"):
                return _login_payload(_LOGIN)
            p = cur
        else:
            if _LOGIN:
                _login_clean(_LOGIN)
                _LOGIN.clear()
            lid = "login-%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), os.getpid())
            qr_copy = os.path.join(tempfile.gettempdir(), "heronbo_cli_%s.png" % lid)
            try:
                p = subprocess.Popen(
                    [node, path, "login", "--qr=png"], cwd=os.path.dirname(path),
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL, env=_cli_env(path),
                    text=True, encoding="utf-8", errors="replace", bufsize=1,
                    creationflags=0x08000000 if os.name == "nt" else 0)
            except Exception as e:                              # noqa: BLE001
                return {"ok": False, "error": "启动登录失败：%s" % str(e)[:160]}
            entry = {"id": lid, "proc": p, "qrFile": qr_copy, "qr": "",
                     "qrData": "", "state": "pending", "message": "正在生成二维码…",
                     "ended": False, "rc": None, "out": "", "reader": None}
            _LOGIN.clear()
            _LOGIN.update(entry)
            reader = threading.Thread(target=_login_reader, args=(p, _LOGIN), daemon=True)
            _LOGIN["reader"] = reader
            reader.start()

    # 只等 3 秒拿二维码；没出来就先返回 pending，让前端继续轮询，
    # 不要把 HTTP 请求卡 20 秒再报错。
    exit_wait = False
    for _ in range(12):
        with _LOGIN_LOCK:
            if _LOGIN.get("qr"):
                return _login_payload(_LOGIN)
            if _LOGIN.get("ended"):
                break
            exit_wait = p.poll() is not None
        if exit_wait:
            # 进程已退出但 reader 还没把最后一行 stderr 收完；先等它收尾，
            # 避免把「正在生成二维码…」当成最终错误返回给前端。
            reader = _LOGIN.get("reader")
            if reader:
                reader.join(2.0)
            break
        time.sleep(0.25)
    with _LOGIN_LOCK:
        if _LOGIN.get("qr"):
            return _login_payload(_LOGIN)
        if p.poll() is not None or _LOGIN.get("ended"):
            out = (_LOGIN.get("out") or "").strip()
            last = ""
            if out:
                for line in reversed(out.splitlines()):
                    if line.strip():
                        last = line.strip()
                        break
            err = (last or _LOGIN.get("message") or "登录进程结束，但没有生成二维码")
            detail = out[-1200:]
            _login_clean(_LOGIN)
            _LOGIN.clear()
            return {"ok": False, "error": err, "detail": detail}
        return {"ok": True, "state": "pending", "id": _LOGIN.get("id") or "",
                "qr": "", "message": "二维码还在生成，请稍后刷新"}


def login_status(login_id=""):
    """轮询登录状态；不每次轮询都跑 auth（那会把 2 秒轮询拖成 40 秒）。"""
    with _LOGIN_LOCK:
        entry = _LOGIN.get("proc")
        if not entry or (login_id and _LOGIN.get("id") != login_id):
            return {"ok": False, "state": "error", "error": "没有正在进行的登录"}
        if _LOGIN.get("qr") and not _LOGIN.get("ended"):
            return _login_payload(_LOGIN, "pending")
        ended = bool(_LOGIN.get("ended"))
    if ended:
        # 扫码后 CLI 进程会退出；只在退出时验证一次登录态，避免每次轮询都跑 auth。
        a = auth_status(force=True, quick=False)
        if a.get("ok"):
            with _LOGIN_LOCK:
                _login_clean(_LOGIN)
                _LOGIN.clear()
            return {"ok": True, "state": "done", "message": "登录成功，账号已通过验证"}
        with _LOGIN_LOCK:
            msg = _LOGIN.get("message") or (a.get("why") or "登录进程已结束")
            _login_clean(_LOGIN)
            _LOGIN.clear()
        return {"ok": False, "state": "error", "error": msg}
    with _LOGIN_LOCK:
        if not _LOGIN:
            return {"ok": False, "state": "error", "error": "登录已结束"}
        return _login_payload(_LOGIN, "pending")


def login_cancel(login_id=""):
    with _LOGIN_LOCK:
        if not _LOGIN or (login_id and _LOGIN.get("id") != login_id):
            return {"ok": True, "cancelled": False}
        _login_clean(_LOGIN)
        _LOGIN.clear()
    return {"ok": True, "cancelled": True}


# ── 首次配置：把工作台技能交给 CLI / agent（2026-11-12）────────────────────
# CLI 实际扫描的用户技能根目录是 ~\Doubao\skills（本机实测有效）；~\.agents\skills
# 是部分 agent 通道也会读取的目录。只安装安装包内置技能，不联网下载、不覆盖用户版本。
_SKILLS = {
    "heronbo-aigc-prompt": {
        "label": "HeronBo 提示词技能",
        "required": True,
        "folder": "HeronBo-AIGC-Prompt",
        "source": "base",
    },
    "seedance-25": {
        "label": "Seedance 2.5 技能",
        "required": False,
        "folder": "seedance-25",
        "source": "skills",
    },
}


def _norm_skill_name(name):
    return re.sub(r"[^a-z0-9]+", "", str(name or "").lower())


def _skill_roots():
    home = os.path.expanduser("~")
    return [os.path.join(home, "Doubao", "skills"),
            os.path.join(home, ".agents", "skills")]


def _skill_hit(key, roots=None):
    norm = _norm_skill_name(key)
    for root in (roots if roots is not None else _skill_roots()):
        if not os.path.isdir(root):
            continue
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for name in names:
            p = os.path.join(root, name)
            if (os.path.isdir(p) and _norm_skill_name(name) == norm
                    and os.path.isfile(os.path.join(p, "SKILL.md"))):
                return p
    return ""


def _skill_source(key):
    spec = _SKILLS.get(key) or {}
    if not spec:
        return ""
    base = _app_skill_root()
    if not base:
        return ""
    if spec.get("source") == "base":
        return base if os.path.isfile(os.path.join(base, "SKILL.md")) else ""
    return os.path.join(base, "skills", str(spec.get("folder") or key))


def _app_skill_root():
    """从 wb_cli.py 往上找技能根（源目录 / 安装版 / exe 内 _internal 都适用）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    for _ in range(8):
        if (os.path.isfile(os.path.join(here, "SKILL.md"))
                or os.path.isdir(os.path.join(here, "skills", "seedance-25"))):
            return here
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    return ""


def skill_status():
    """只读检查：以 CLI 主技能根（~\\Doubao\\skills）为准，避免只装在 .agents 时误报可用。"""
    roots = _skill_roots()
    items = []
    for key, spec in _SKILLS.items():
        hit = _skill_hit(key, roots[:1])
        items.append({
            "key": key,
            "label": spec["label"],
            "required": bool(spec.get("required")),
            "ok": bool(hit),
            "path": hit,
            "bundled": bool(_skill_source(key)),
        })
    req = [x for x in items if x["required"]]
    return {
        "ok": all(x["ok"] for x in req),
        "complete": all(x["ok"] for x in items),
        "items": items,
        "roots": roots,
    }


def _rm_force(p):
    """强删（先把只读位去掉再删）。删不掉不抛异常，交给调用方判断结果。"""
    import stat as _st
    if not os.path.exists(p):
        return
    if not os.path.isdir(p) or os.path.islink(p):
        try:
            os.chmod(p, _st.S_IWRITE)
        except OSError:
            pass
        try:
            os.remove(p)
        except OSError:
            pass
        return

    def _onerr(func, path, _exc):
        try:
            os.chmod(path, _st.S_IWRITE)
            func(path)
        except OSError:
            pass

    try:
        shutil.rmtree(p, onerror=_onerr)
    except TypeError:
        shutil.rmtree(p, ignore_errors=True)
    except OSError:
        pass


def skill_install(key):
    """把内置技能放到 CLI 主根；.agents 根存在时也同步一份；已存在则保留用户版本。"""
    key = str(key or "").strip()
    if key not in _SKILLS:
        return {"ok": False, "error": "不认识的技能：%s" % key}
    spec = _SKILLS[key]
    src = _skill_source(key)
    if not src or not os.path.isfile(os.path.join(src, "SKILL.md")):
        return {"ok": False, "error": "安装包没有内置「%s」技能" % spec["label"]}
    primary = os.path.join(os.path.expanduser("~"), "Doubao", "skills")
    agents_root = os.path.join(os.path.expanduser("~"), ".agents", "skills")
    roots = [primary]
    # .agents 是部分 agent 通道的技能根；存在时才写入，避免在外部机器凭空造目录。
    if os.path.isdir(agents_root):
        roots.append(agents_root)
    installed = []
    primary_hit = _skill_hit(key, roots[:1])
    for root in roots:
        found = _skill_hit(key, [root])
        if found:
            installed.append(found)
            continue
        target = os.path.join(root, spec["folder"])
        if os.path.exists(target):
            return {"ok": False, "key": key,
                    "error": "目标目录已存在但不是完整技能，未覆盖：%s" % target}
        try:
            os.makedirs(root, exist_ok=True)
            # 2026-11-14 用户实测：[WinError 183] 当文件已存在时，无法创建该文件。
            # 原因是临时目录名只用 PID —— 同一个进程里点两次"安装"、或上次复制中途
            # 失败留下只读残留（rmtree ignore_errors 静默没删掉），都会撞上已存在。
            # 三层一起修：名字带时间戳+序号避免复用；先按只读属性强删；复制时允许已存在目录。
            tmp = target + ".installing-%s-%s" % (os.getpid(), int(time.time() * 1000))
            if os.path.exists(tmp):
                _rm_force(tmp)
            shutil.copytree(src, tmp, dirs_exist_ok=True)
            if os.path.exists(target):
                _rm_force(target)
            os.replace(tmp, target)
            installed.append(target)
        except Exception as e:                                  # noqa: BLE001
            return {"ok": False, "key": key,
                    "error": "复制技能失败：%s（目标：%s）" % (str(e)[:180], target)}
    return {"ok": True, "already": bool(primary_hit), "key": key,
            "path": primary_hit or installed[0] if installed else "",
            "paths": installed}


def _read(path, limit=26000):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()[:limit]
    except OSError:
        return ""


def _clip_save(text, outdir, name):
    os.makedirs(outdir, exist_ok=True)
    p = os.path.join(outdir, name)
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    return p


def _new_target(outdir, prefix, ext):
    os.makedirs(outdir, exist_ok=True)
    return os.path.join(outdir, "%s_%s.%s" % (
        prefix, time.strftime("%Y%m%d-%H%M%S"), ext.lstrip(".")))


def _scan(outdir, exts, since):
    out = []
    try:
        for f in os.listdir(outdir):
            p = os.path.join(outdir, f)
            if not os.path.isfile(p):
                continue
            if not f.lower().endswith(exts):
                continue
            try:
                if os.path.getmtime(p) >= since - 8:
                    out.append(p)
            except OSError:
                pass
    except OSError:
        pass
    return sorted(out, key=lambda p: os.path.getmtime(p), reverse=True)


def _harvest_videos(primary, outdir, since, out_text=""):
    """在 outdir 之外再找一遍成片：CLI 自报的绝对路径 + 常见下载目录。

    2026-11-16 用户报「豆包会话出片了，但工作台只说放在文件夹里，前端拿不到视频」：
    agent 未必照我们给的 target 落盘，常见是收进它自己的下载目录、再在回答里报一句
    「已放在 X」。_scan(outdir) 只扫一层本目录，于是成片看得见名字、拿不到文件。
    """
    exts = (".mp4", ".mov", ".mkv", ".webm")
    seen = set()
    out = []

    def add(p):
        try:
            p = os.path.abspath(str(p).strip().strip("`\"'"))
        except (OSError, ValueError):
            return
        if p in seen or not os.path.isfile(p):
            return
        if not p.lower().endswith(exts):
            return
        try:
            if os.path.getmtime(p) < since - 600:   # 只认这一轮前后的片子
                return
        except OSError:
            return
        seen.add(p)
        out.append(p)

    for p in primary or []:                          # 我们指定过的 target 排最前
        add(p)
    for mt in re.finditer(r"([A-Za-z]:\\[^\"\r\n<>|*?]*?\.(?:mp4|mov|mkv|webm))",
                          str(out_text or ""), re.I):
        add(mt.group(1))
    for root in (outdir,
                 os.path.join(os.path.expanduser("~"), "Downloads"),
                 os.path.join(os.path.expanduser("~"), "下载")):
        try:
            for name in sorted(os.listdir(root), reverse=True):   # 新文件通常在后
                p = os.path.join(root, name)
                add(p)
                if os.path.isdir(p):
                    try:
                        for sub in os.listdir(p):
                            add(os.path.join(p, sub))
                    except OSError:
                        pass
        except OSError:
            pass
    return out


def _model_id(label):
    s = str(label or "")
    for k, v in _MODEL_MAP.items():
        if k in s:
            return v
    return "seedance_2.0_fast"


def generate_image(prompt, outdir, ratio="16:9", model="seedream_5.5",
                   timeout=900, session_key="", refs=None):
    """CLI 生图；一次只一张，返回实际文件路径。

    refs（2026-11-15 加）：形象基准图的绝对路径。分镜图必须跟素材里是同一个人/同一件
    商品，只靠文字描述模型每次都会画成另一个人——生图能力本身支持参考图，把路径给它。
    """
    outdir = os.path.abspath(outdir)
    target = _new_target(outdir, "cli_img", "png")
    ref_block = ""
    refs = [os.path.abspath(p) for p in (refs or []) if p and os.path.isfile(str(p))]
    if refs:
        ref_block = (
            "参考图（形象基准：必须保持同一个人 / 同一件商品，不得换脸、换发型、换服装、"
            "换商品包装）：\n"
            + "\n".join("- " + p for p in refs)
            + "\n先用 FileBatchUpload 把每张参考图上传成 URL，再按上面顺序传给 image_gen 的 "
              "image_reference_url_list；万一上传不成，就先读这些图片的内容，再照它们的"
              "长相与外观生成。\n"
        )
    task = (
        "你在执行 HeronBo 工作台的一次受控图片生成任务。只做这一件事。\n"
        "直接调用图片生成工具 image_gen，调用时 model_version 必须是 "
        "\"%s\"，不得降级或换模型。\n"
        "不要因为找不到某个 Skill 就停止：Skill 只是参数说明，工具才是执行手段；"
        "只有工具本身真的不可用（报不存在、无权限）才如实报错。\n"
        "只生成 1 张图片，不要生成第二张，不要做图片编辑以外的任何事。\n"
        "画幅：%s；分辨率：2K；格式：PNG。\n"
        "%s"
        "请把生成文件保存到：%s\n"
        "提示词正文如下（原样使用，不要改写、不要扩写）：\n"
        "<<<PROMPT\n%s\nPROMPT>>>\n"
        "完成后只报告实际文件绝对路径、文件大小、尺寸和实际使用的 model_version；"
        "不要修改其他文件。" % (model, ratio, ref_block, target, _read_prompt(prompt))
    )
    task = _safe_task_text(task)
    pfile = _clip_save(task, outdir, "_cli_image_task.txt")
    started = time.time()
    r = _run(["-p", task, "-C", outdir, "--permission-mode", "always_allow"],
              timeout=timeout, cwd=outdir, session_key=session_key)
    if r["rc"] != 0:
        return {"ok": False,
                "error": "这一轮等得太久，已经停掉，重试一次" if r.get("timed_out")
                         else "生成没成功，重试一次",
                "raw": r, "taskFile": pfile, "session": r.get("session", "")}
    files = _scan(outdir, (".png", ".jpg", ".jpeg", ".webp"), started)
    # 目标路径优先；否则用本轮最新文件（CLI 有时会自行改名）
    if os.path.isfile(target):
        files = [target] + [p for p in files if os.path.abspath(p) != os.path.abspath(target)]
    if not files:
        return {"ok": False, "error": "CLI 返回成功但没有找到图片文件",
                "raw": r, "taskFile": pfile, "session": r.get("session", "")}
    return {"ok": True, "path": files[0], "files": files[:4],
            "model": model, "session": r.get("session", ""),
            "raw": r, "taskFile": pfile}


def _read_prompt(prompt):
    """容错：调用方可能传路径，也可能传正文。"""
    s = str(prompt or "")
    if len(s) < 900 and os.path.isfile(s):
        return _read(s) or s
    return s


def _safe_task_text(s):
    # CLI -p 走 Windows 命令行；去掉 NUL，避免 CreateProcess 失败
    return str(s).replace("\x00", "")


def _probe_video(path):
    try:
        ff = shutil.which("ffprobe")
        if not ff:
            return {}
        r = subprocess.run([ff, "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=width,height,codec_name,avg_frame_rate,duration",
                            "-show_entries", "format=duration,size", "-of", "json", path],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=20,
                           creationflags=0x08000000 if os.name == "nt" else 0)
        return json.loads(r.stdout or "{}")
    except Exception:  # noqa: BLE001
        return {}


def generate_video(prompt, outdir, ratio="9:16", duration=5,
                   model="Seedance 2.5", images=None, audio="", video="",
                   timeout=1800, session_key=""):
    """CLI 生视频；一次一段，返回实际 mp4 路径。"""
    outdir = os.path.abspath(outdir)
    target = _new_target(outdir, "cli_video", "mp4")
    model_id = _model_id(model)
    imgs = [os.path.abspath(p) for p in (images or []) if p and os.path.isfile(p)]
    refs = ""
    if imgs:
        refs += "参考图（按顺序作为图生视频/首帧参考，必须全部传给生成工具）：\n" + \
                "\n".join("- " + p for p in imgs) + "\n"
    if audio and os.path.isfile(audio):
        refs += "参考音频：%s\n" % os.path.abspath(audio)
    if video and os.path.isfile(video):
        refs += "参考视频：%s\n" % os.path.abspath(video)
    task = (
        "你在执行 HeronBo 工作台的一次受控视频生成任务。只做这一件事。\n"
        "直接调用你当前可用的视频生成工具（text_to_video 或 image_to_video，有参考图就用能收参考图的那个），"
        "调用时 model_version 必须是 "
        "\"%s\"（用户可见名：%s），不得默认切到 seedance_2.0_fast，也不得换模型。\n"
        "不要因为找不到某个 Skill 就停止：Skill 只是参数说明，工具才是执行手段；"
        "只有工具本身真的不可用（报不存在、无权限）才如实报错。\n"
        "只生成 1 段视频，不要生成第二段，不要剪辑、拼接、改字幕或做其他事。\n"
        "时长：%s 秒；画幅：%s；视频无额外水印/字幕；按工具允许的最高清晰度输出。\n"
        "%s"
        "请把生成文件保存到：%s\n"
        "提示词正文如下（原样使用，不要改写、不要扩写）：\n"
        "<<<PROMPT\n%s\nPROMPT>>>\n"
        "完成后只报告实际文件绝对路径、时长、分辨率、文件大小和实际使用的 model_version；"
        "不要修改其他文件。" % (model_id, model, int(duration), ratio, refs, target,
                                  _read_prompt(prompt))
    )
    task = _safe_task_text(task)
    pfile = _clip_save(task, outdir, "_cli_video_task.txt")
    started = time.time()
    r = _run(["-p", task, "-C", outdir, "--permission-mode", "always_allow"],
              timeout=timeout, cwd=outdir, session_key=session_key)
    if r["rc"] != 0:
        return {"ok": False,
                "error": "这一轮等得太久，已经停掉，重试一次" if r.get("timed_out")
                         else "生成没成功，重试一次",
                "raw": r, "taskFile": pfile, "session": r.get("session", "")}
    files = _scan(outdir, (".mp4", ".mov", ".mkv", ".webm"), started)
    if os.path.isfile(target):
        files = [target] + [p for p in files if os.path.abspath(p) != os.path.abspath(target)]
    # 2026-11-16：agent 可能没照 target 落盘，改看它自报的路径和下载目录
    files = _harvest_videos(files, outdir, started,
                            (r.get("out") or "") + "\n" + (r.get("err") or ""))
    if not files:
        # 2026-11-17 用户要求：别把内部路径糊到脸上。原来这里会把成片目录和下载目录
        # 的绝对路径（还是 \\172.16.0.12 这种网络盘）整段拼进提示，用户只看到一堵墙。
        return {"ok": False,
                "error": "没找到这轮生成的视频。如果片子已经下好了，"
                         "把视频文件的完整路径填进「成片路径」再点一次。",
                "raw": r, "taskFile": pfile, "session": r.get("session", "")}
    path = files[0]
    size = os.path.getsize(path) if os.path.isfile(path) else 0
    if size < 8000:
        return {"ok": False, "error": "视频文件过小，疑似空壳：%s" % path,
                "raw": r, "taskFile": pfile, "session": r.get("session", "")}
    return {"ok": True, "path": path, "files": files[:2], "video": path,
            "model": model_id, "duration": int(duration),
            "probe": _probe_video(path), "session": r.get("session", ""),
            "raw": r, "taskFile": pfile}


def cancel(session_key):
    """杀掉这条 CLI 任务的进程树（只用于用户主动取消）。"""
    with _LOCK:
        p = _PROCS.pop(session_key, None)
    if not p:
        return {"ok": True, "killed": False}
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"],
                           capture_output=True, timeout=15,
                           creationflags=0x08000000)
        else:
            p.kill()
        return {"ok": True, "killed": True, "pid": p.pid}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def cancel_all():
    """把登记在案的 CLI 子进程整棵树都杀掉（用户点「中止」时兜底，怕只杀 agent 父进程漏了 CLI 这条）。"""
    with _LOCK:
        procs = list(_PROCS.items())
        _PROCS.clear()
    killed = 0
    for _key, p in procs:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"],
                               capture_output=True, timeout=15,
                               creationflags=0x08000000)
            else:
                p.kill()
            killed += 1
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "killed": killed}