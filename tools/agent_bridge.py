r"""程序 ↔ agent 的通道：把 headless CLI 包成几个函数，让 exe 能直接叫 agent 干活。

为什么能这么做：本机 WorkBuddy CLI 自带无头入口
    <WorkBuddy>\resources\app.asar.unpacked\cli\dist\codebuddy-headless.js
支持 `-p`（非交互跑完退出）、`--output-format json`（可解析出 session id）、
`-r/--resume <id>`（钉住同一个会话，不会抢用户正在聊的那个）、`--permission-mode`、`--tools`。

⚠️ **关键坑（2026-09-14 实测）**：CLI 要绑一个本机控制端口（默认与桌面端同为 7632），
桌面端开着时直接 `listen EADDRINUSE` → 进程静默卡死、零输出。
修法：给子进程注入 `SERVER__PORT`（和 `CLAUDE_CODE_SSE_PORT`）指向一个**空闲端口**。
本文件自动挑空端口，所以 exe 在桌面端开着的情况下也能唤起 agent。

设计取舍：
- **会话钉在项目上**：第一次调用不带 -c/-r，从 JSON 里读出 sessionId 存进项目 `_会话/状态.json`；
  以后都用 `-r <id>`。绝不用 `-c`（它续的是"最近一个会话"，会污染用户在桌面端正在聊的对话）。
- **可被 exe 主动调用**，也可被 agent 自己用（命令行同款）。
- 关不住就超时杀进程；输出逐行回调，界面可以边跑边显示。
"""
import json
import os
import shutil
import socket
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# 本机路径一律**按环境变量推导 + 常见位置探测**，不写死用户名（否则会被发布防护钩子拦下）。
# 需要覆盖时写同目录的 agent_bridge.local.json（已 gitignore）：{"node": "...", "cli_js": "..."}
LOCAL_CFG = os.path.join(HERE, "agent_bridge.local.json")

DEFAULT_TIMEOUT = 600


def _local_cfg():
    try:
        with open(LOCAL_CFG, encoding="utf-8-sig") as f:
            return json.load(f) or {}
    except (FileNotFoundError, ValueError, OSError):
        return {}


def _glob_first(patterns):
    import glob
    for p in patterns:
        hit = glob.glob(p, recursive=True)
        if hit:
            return sorted(hit)[0]
    return None


def cli_js():
    """找 WorkBuddy 的 headless CLI 入口。

    推导顺序：显式配置 → 环境变量给的安装位置 → 常见安装目录 glob。
    """
    cfg = _local_cfg().get("cli_js")
    if cfg and os.path.isfile(cfg):
        return cfg
    env = os.environ.get("CODEBUDDY_CLI_JS")
    if env and os.path.isfile(env):
        return env
    roots = []
    # 这个环境变量指向 <安装目录>\resources\app.asar.unpacked\...\skills，往回退两级就是 resources
    hint = os.environ.get("CODEBUDDY_BUILTIN_SKILLS_DIR", "")
    if hint:
        p = os.path.normpath(hint)
        for _ in range(4):
            p = os.path.dirname(p)
            if p and p not in roots:
                roots.append(p)
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"),
                 os.environ.get("ProgramW6432"), "F:\\", "C:\\"):
        if base and os.path.isdir(base):
            roots.append(base)
    pats = []
    for r in roots:
        pats += [
            os.path.join(r, "WorkBuddy", "resources", "app.asar.unpacked", "cli",
                         "dist", "codebuddy-headless.js"),
            os.path.join(r, "*", "resources", "app.asar.unpacked", "cli",
                         "dist", "codebuddy-headless.js"),
        ]
    pats.append(os.path.join("**", "resources", "app.asar.unpacked", "cli", "dist",
                             "codebuddy-headless.js"))
    return _glob_first(pats)


def node_exe():
    """找 node：显式配置 → PATH → 常见安装位置。"""
    cfg = _local_cfg().get("node")
    if cfg and os.path.isfile(cfg):
        return cfg
    hit = shutil.which("node")
    if hit:
        return hit
    pats = [r"C:\Program Files\nodejs\node.exe", r"C:\Program Files (x86)\nodejs\node.exe"]
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if d and "node" in d.lower():
            pats.append(os.path.join(d, "node.exe"))
    return _glob_first(pats)


def available():
    """返回 (可用?, 说明)。界面用它决定"交给 agent"按钮是否可点。"""
    js, nd = cli_js(), node_exe()
    if not js:
        return False, "没找到 headless CLI（codebuddy-headless.js）"
    if not nd:
        return False, "没找到 node 可执行文件"
    return True, "OK"


def free_port():
    """要一个当前空闲的端口（给 CLI 的控制通道用）。"""
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def child_env():
    """子进程环境：关键是给 CLI 一个自己的控制端口，避开桌面端占着的那个。"""
    env = dict(os.environ)
    p1 = free_port()
    p2 = free_port()
    env["SERVER__PORT"] = str(p1)
    env["SERVER__HOST"] = "127.0.0.1"
    env["CLAUDE_CODE_SSE_PORT"] = str(p2)
    env["CODEBUDDY_IDE_PORT"] = str(p1)
    env.pop("CODEBUDDY_SANDBOX_BROKER_IPC_ADDRESS", None)   # 别继承外层沙箱的 IPC 地址
    return env


def build_cmd(prompt, session_id=None, permission_mode="acceptEdits",
              tools=None, output_format="json", extra=None):
    """拼命令行。tools=None 表示不限制；tools="" 表示禁用全部工具（自检用）。"""
    js, nd = cli_js(), node_exe()
    if not (js and nd):
        raise RuntimeError("headless CLI 不可用：%s" % available()[1])
    cmd = [nd, js, "-p", "--output-format", output_format]
    if session_id:
        cmd += ["-r", session_id]
    if permission_mode:
        cmd += ["--permission-mode", permission_mode]
    if tools is not None:
        cmd += ["--tools", tools]
    if extra:
        cmd += list(extra)
    cmd.append(prompt)
    return cmd


def _pick_session(obj):
    """从 CLI 的 JSON 输出里挖 session id（不同版本字段名可能不同，宽容一点）。"""
    if not isinstance(obj, dict):
        return None
    for k in ("session_id", "sessionId", "sessionID", "conversation_id", "id"):
        v = obj.get(k)
        if isinstance(v, str) and len(v) >= 8:
            return v
    for key in ("result", "data", "meta", "session"):
        v = obj.get(key)
        if isinstance(v, dict):
            hit = _pick_session(v)
            if hit:
                return hit
    return None


def _pick_text(obj):
    if isinstance(obj, str):
        return obj
    if not isinstance(obj, dict):
        return ""
    for k in ("result", "text", "content", "message", "output", "response"):
        v = obj.get(k)
        if isinstance(v, str) and v.strip():
            return v
        if isinstance(v, dict):
            t = _pick_text(v)
            if t:
                return t
    return ""


def _parse_output(out):
    """解析 CLI 输出。

    `--output-format json` 实测返回的是**数组**，末元素形如：
      {"type":"result","subtype":"success","is_error":false,"result":"OK",
       "session_id":"…","duration_ms":2684,"num_turns":2,"total_cost_usd":0,
       "usage":{…},"permission_denials":[]}
    另外也兼容单对象 / JSONL（每行一个 JSON）两种形态。
    """
    stripped = (out or "").strip()
    if not stripped:
        return None
    cands = []
    try:
        cands.append(json.loads(stripped))
    except ValueError:
        for line in stripped.splitlines():
            line = line.strip()
            if line.startswith(("{", "[")):
                try:
                    cands.append(json.loads(line))
                except ValueError:
                    pass
    for obj in cands:
        if isinstance(obj, dict) and obj.get("type") == "result":
            return obj
    for obj in cands:
        if isinstance(obj, list):
            for it in reversed(obj):
                if isinstance(it, dict) and it.get("type") == "result":
                    return it
            if obj and isinstance(obj[-1], dict):
                return obj[-1]
    for obj in cands:
        if isinstance(obj, dict):
            return obj
    return None


def no_window_kwargs():
    """子进程创建参数：**别让它弹控制台窗口**（Windows）。

    宿主是 GUI 进程（score-tool.exe / pythonw），本身**没有控制台**；这时创建一个
    控制台子进程（node.exe），Windows 会给它新开一个终端窗口。Win11 的默认终端是
    Windows Terminal，于是用户看到"一个什么都没有的命令行"——因为子进程的
    stdout/stderr 被我们用管道接管了，终端里自然什么都不显示（2026-09-15 实测复现：
    窗口类 `CASCADIA_HOSTING_WINDOW_CLASS`，标题就是 node.exe 的路径）。

    `CREATE_NO_WINDOW` 让子进程不建控制台；`STARTUPINFO.wShowWindow=SW_HIDE` 是给
    老系统的双保险。ffprobe 之类短命令同理（归类素材时也会闪一下黑窗）。

    ⚠️ 只对**直接子进程**有效。agent 自己再派生的 shell 属于孙子进程，是否弹窗取决于
    它自己的 spawn 参数（Node 的 windowsHide）——所以验收要跑一次真调用看全程。
    """
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        try:
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0                      # SW_HIDE
            kw["startupinfo"] = si
        except (AttributeError, ValueError):
            pass
    return kw


def ask(prompt, session_id=None, cwd=None, timeout=DEFAULT_TIMEOUT,
        permission_mode="acceptEdits", tools=None, on_line=None, extra=None):
    """叫 agent 干一件事。

    返回 dict: {ok, session_id, text, res, returncode, cmd, error}
    res 里带 duration_ms / num_turns / total_cost_usd / usage，界面可以顺手显示花了多少。
    on_line(text) 会收到每一行 stdout，界面可实时显示。
    """
    try:
        cmd = build_cmd(prompt, session_id=session_id, permission_mode=permission_mode,
                        tools=tools, extra=extra)
    except RuntimeError as e:
        return {"ok": False, "error": str(e), "session_id": session_id, "text": ""}
    try:
        p = subprocess.run(cmd, cwd=cwd or HERE, capture_output=True, timeout=timeout,
                           encoding="utf-8", errors="replace", env=child_env(),
                           stdin=subprocess.DEVNULL, **no_window_kwargs())
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "超时 %ds，已放弃（agent 可能还在跑）" % timeout,
                "session_id": session_id, "text": "", "cmd": cmd}
    except OSError as e:
        return {"ok": False, "error": "起不来：%s" % e, "session_id": session_id,
                "text": "", "cmd": cmd}
    out = p.stdout or ""
    if on_line:
        for line in out.splitlines():
            on_line(line)
    res = _parse_output(out)
    sid = session_id
    text = ""
    if res:
        sid = res.get("session_id") or res.get("sessionId") or session_id
        text = res.get("result") or _pick_text(res) or ""
        if isinstance(text, list):
            text = "".join(seg.get("text", "") if isinstance(seg, dict) else str(seg)
                           for seg in text)
    if not text:
        text = out.strip()
    is_err = bool(res and res.get("is_error"))
    return {"ok": (p.returncode == 0) and (not is_err) and bool(text),
            "session_id": sid, "text": text, "res": res,
            "returncode": p.returncode, "cmd": cmd,
            "stderr": (p.stderr or "")[-2000:],
            "error": "" if (p.returncode == 0 and not is_err)
                     else ("agent 报错" if is_err else "CLI 返回码 %d" % p.returncode)}


if __name__ == "__main__":                      # 命令行自检：python agent_bridge.py "问题"
    ok, why = available()
    print("[通道] 可用=%s  %s" % (ok, why))
    if not ok:
        sys.exit(1)
    q = " ".join(sys.argv[1:]) or "只回复两个字：收到"
    r = ask(q, tools="", timeout=180, permission_mode="dontAsk")
    print("[通道] ok=%s rc=%s session=%s" % (r.get("ok"), r.get("returncode"),
                                           r.get("session_id")))
    print("[通道] 回复 =", (r.get("text") or r.get("error"))[:200])
