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
import threading

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


def available(prefer=None):
    """返回 (可用?, 说明)。界面用它决定"交给 agent"按钮是否可点、以及显示用的哪个 agent。"""
    key, why = pick_agent(prefer)
    return (bool(key), why)


def agent_info():
    """给界面看的完整信息：候选列表 + 选中谁 + 为什么 + 用户是否已明确指定过。"""
    key, why = pick_agent()
    chosen = _local_cfg().get("agent") or ""
    return {"picked": key,
            "label": (ADAPTERS.get(key) or {}).get("label", ""),
            "why": why,
            "chosen": chosen,          # 空 = 还没让用户选过（界面据此决定要不要先问）
            "list": list_agents()}


def set_agent(key):
    """把"用哪个 agent"写进 agent_bridge.local.json（保留 node/cli_js/cmd 等其它键）。

    界面在"点出提示词前"让用户选一次，选完写这里；以后各次直接读，不再重复问。
    key 传空字符串表示恢复自动挑选。
    """
    cfg = _local_cfg()
    if key:
        rows = {r["key"]: r for r in list_agents()}
        if key not in rows:
            return False, "未知的 agent：%s" % key
        if not rows[key]["ok"]:
            return False, "%s 现在不可用：%s" % (rows[key]["label"], rows[key]["why"])
        cfg["agent"] = key
    else:
        cfg.pop("agent", None)
    try:
        with open(LOCAL_CFG, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError as e:
        return False, "写不进 %s：%s" % (LOCAL_CFG, e)
    return True, (ADAPTERS[key]["label"] if key else "自动挑选")


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


# ── 多 agent 适配层 ─────────────────────────────────────────────────────────
# 为什么要这层：这个 exe 是**跟着 skill 走的**——skill 装到哪个 agent 名下，就该用哪个
# agent 干活。原先只认 WorkBuddy 的 headless CLI，换台机器/换个 agent 就"叫不动"。
#
# 判定顺序（都可被 agent_bridge.local.json 覆盖）：
#   1) 显式配置 {"agent": "codex"} 或 {"cmd": [...]}（自己塞任意命令行）
#   2) **装了本 skill 的 agent**（在 ~/.zcode、~/.codex、~/.dsh、~/.workbuddy 的
#      skills/<技能名> 里能找到一个），且它的 CLI 可用
#   3) 本机任一个可用的 agent（workbuddy → codex 顺序）
#   4) 都没有 → UI 明确报「没找到 agent」，而不是点了没反应
HOMES = {
    "zcode": (".zcode", "skills"),
    "codex": (".codex", "skills"),
    "dsh": (".dsh", "skills"),
    "workbuddy": (".workbuddy", "skills"),
}


def skill_name():
    """本 skill 的名字（用仓库目录名，junction 也是这个名字）。"""
    cfg = _local_cfg().get("skill_name")
    if cfg:
        return cfg
    return os.path.basename(os.path.normpath(_skill_root()))


def _skill_root():
    """技能仓库根：**靠找 SKILL.md，不靠 HERE/..**。

    frozen 之后 HERE 落在临时解包目录 _MEIxxxx 里，HERE/.. 就成了 %TEMP%，于是
    "哪个 agent 装了本技能"永远查不到（2026-09-15 实测：exe 里 host_agents()
    返回空，因为它在找临时目录下的 skills 子目录）。与 project_core.find_skill_root 同一套判据。
    """
    env = os.environ.get("HERONBO_SKILL_ROOT")
    if env and os.path.isfile(os.path.join(env, "SKILL.md")):
        return os.path.normpath(env)
    starts = [HERE]
    if getattr(sys, "frozen", False):
        try:
            starts.append(os.path.dirname(os.path.abspath(sys.executable)))
        except (OSError, ValueError):
            pass
    for st in starts:
        d = st
        for _ in range(6):
            if os.path.isfile(os.path.join(d, "SKILL.md")):
                return d
            nd = os.path.dirname(d)
            if nd == d:
                break
            d = nd
    return os.path.normpath(os.path.join(HERE, ".."))


def host_agents():
    """哪些 agent 把本 skill 装在自己名下（返回按 HOMES 顺序的 key 列表）。"""
    name = skill_name()
    out = []
    home = os.path.expanduser("~")
    for key, (d, sub) in HOMES.items():
        p = os.path.join(home, d, sub, name)
        if os.path.exists(p):
            out.append(key)
    return out


def codex_exe():
    """找 Codex CLI（PATH → 常见 npm 全局目录 → 配置）。"""
    cfg = _local_cfg().get("codex")
    if cfg and os.path.isfile(cfg):
        return cfg
    hit = shutil.which("codex.cmd") or shutil.which("codex")
    if hit:
        return hit
    pats = [os.path.join(os.environ.get("APPDATA", ""), "npm", "codex.cmd"),
            os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "npm", "codex.cmd")]
    return _glob_first(pats)


def dsh_pkg():
    """找 DSH（DeepSeek Harness）的启动脚本。

    DSH 是 npm 包 @deepseek-ai/dsh（用户平时用 `npx @deepseek-ai/dsh web --no-open` 起网页端）。
    它自带无头档：`dsh --profile headless "<任务>"` —— stderr 流推理、stdout 出最终答复、跑完退出。
    这里直接找到包里的 lib/bin.js，用 node 调（**不走 npx/cmd**，免得提示词里的引号被 shell 吃掉）。
    """
    cfg = _local_cfg().get("dsh")
    if cfg and os.path.isfile(cfg):
        return cfg
    import glob as _glob
    pats = [os.path.join(os.environ.get("LOCALAPPDATA", ""), "npm-cache", "_npx", "*",
                         "node_modules", "@deepseek-ai", "dsh", "lib", "bin.js"),
            os.path.join(os.environ.get("APPDATA", ""), "npm", "node_modules",
                         "@deepseek-ai", "dsh", "lib", "bin.js")]
    return _glob_first(pats)


def zcode_cli():
    """找 ZCode 自带的 CLI。

    ZCode 桌面端里**真的带了一个命令行**（我原先只查了桌面端主进程的参数白名单，漏了这个）：
        <ZCode安装目录>
esources\glm\zcode.cjs   （12.6MB Node 单文件，zcode 0.16.5）
    它的无头用法（`zcode --help` 实证）：
        zcode --prompt "<任务>" --cwd <目录> --mode yolo [--resume <sess_...>] [--json]
    注意：`--settings` / `--config` 虽然写在帮助里，但实际报 Unknown option（文档与实现不一致），
    模型配置只能落在它固定的 `~/.zcode/cli/config.json`（要显式 provider）。
    """
    cfg = _local_cfg().get("zcode")
    if cfg and os.path.isfile(cfg):
        return cfg
    import glob as _glob
    pats = []
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"),
                 os.environ.get("ProgramW6432"), "F:\\", "C:\\"):
        if base and os.path.isdir(base):
            pats.append(os.path.join(base, "ZCode", "resources", "glm", "zcode.cjs"))
            pats.append(os.path.join(base, "*", "resources", "glm", "zcode.cjs"))
    return _glob_first(pats)


def _probe_zcode():
    cli = zcode_cli()
    if not cli:
        return False, "没找到 ZCode 自带的 CLI（resources/glm/zcode.cjs）"
    if not node_exe():
        return False, "没找到 node 可执行文件"
    # 就绪检查：CLI 明确要求 ~/.zcode/cli/config.json 里有 provider（2026-09-15 实测报
    # "Model config is missing"）。顺手查一下，别让用户"选了才发现"。
    cfgp = os.path.join(os.path.expanduser("~"), ".zcode", "cli", "config.json")
    try:
        cfg = json.load(open(cfgp, encoding="utf-8-sig"))
    except (OSError, ValueError):
        return False, "找不到 %s" % cfgp
    # 两种就绪形态都认：① 有 provider（自己填的 API Key 通道）
    # ② 有 model（`zcode login` 后它自己写的 "provider/model" 引用，如 zai/glm-5.1）
    if cfg.get("provider") or cfg.get("model"):
        return True, cli
    return False, ("CLI 要 %s 里有 provider 或 model（现在只有 %s）；"
                   "先跑一次 `zcode login`，或把桌面端 ~/.zcode/v2/config.json 的 provider 搬过去"
                   % (cfgp, "/".join(cfg.keys())))


def _dsh_note():
    """DSH 已知的坑：settings 里的 provider 可能只有 web 档才有（2026-09-15 实测）。"""
    p = os.path.join(os.path.expanduser("~"), ".dsh", "settings.yaml")
    try:
        txt = open(p, encoding="utf-8", errors="replace").read()
    except OSError:
        return ""
    if "provider: deepseek-vision" in txt:
        return ("；注意你的 settings 把 provider 指向 deepseek-vision，那是 web 档插件 "
                "dsh-vision-router 提供的，headless 档没有它（会报 NO_ADAPTER）")
    return ""


def _probe_dsh():
    if not dsh_pkg():
        return False, "没找到 DSH（需要 npx @deepseek-ai/dsh 跑过一次，或全局安装）"
    if not node_exe():
        return False, "没找到 node 可执行文件"
    return True, dsh_pkg() + _dsh_note()


def _probe_codex():
    exe = codex_exe()
    if not exe:
        return False, "没找到 codex（需要先装 Codex CLI）"
    return True, exe


def _probe_workbuddy():
    if not cli_js():
        return False, "没找到 headless CLI（codebuddy-headless.js）"
    if not node_exe():
        return False, "没找到 node 可执行文件"
    return True, "OK"


ADAPTERS = {
    "workbuddy": {"label": "WorkBuddy", "probe": _probe_workbuddy},
    "codex": {"label": "Codex CLI", "probe": _probe_codex},
    # 这两个先留探测位：ZCode / DSH 目前没在安装目录暴露无头 CLI。
    # 找得到就把命令写进 agent_bridge.local.json 的 cmd 字段（见 pick_agent 的说明）。
    # ZCode：桌面端主进程参数里没有无头入口，但**安装目录里自带 CLI**（resources/glm/zcode.cjs），
    # 支持 `--prompt` 无头跑一条任务 —— 2026-09-15 实证找到（先前只查了桌面端参数白名单）。
    # 唯一前置：`~/.zcode/cli/config.json` 里要有显式 provider，否则报 "Model config is missing"。
    "zcode": {"label": "ZCode CLI", "probe": _probe_zcode},
    "dsh": {"label": "DSH（DeepSeek Harness）", "probe": _probe_dsh},
}


def list_agents():
    """本机探测结果：[{key,label,ok,why,host}]（host = 本 skill 装在这个 agent 名下）。"""
    hosts = host_agents()
    rows = []
    for key, a in ADAPTERS.items():
        ok, why = a["probe"]()
        rows.append({"key": key, "label": a["label"], "ok": bool(ok), "why": why,
                     "host": key in hosts})
    return rows


def pick_agent(prefer=None):
    """选一个 agent 干活，返回 (key, why)。理由写清楚，界面直接显示给用户看。"""
    cfg = _local_cfg()
    want = prefer or cfg.get("agent")
    rows = {r["key"]: r for r in list_agents()}
    if want and want in rows:
        r = rows[want]
        if r["ok"]:
            return want, ("按配置用 %s" % r["label"]) if prefer or cfg.get("agent")                 else r["label"]
        return None, "配置指定的 %s 不可用：%s" % (r["label"], r["why"])
    for key in ("workbuddy", "codex", "zcode", "dsh"):
        r = rows.get(key)
        if r and r["ok"] and r["host"]:
            return key, "%s（本 skill 就装在它名下）" % r["label"]
    for key in ("workbuddy", "codex", "zcode", "dsh"):
        r = rows.get(key)
        if r and r["ok"]:
            return key, "%s（本机可用；本 skill 未装在任何 agent 名下）" % r["label"]
    miss = "；".join("%s：%s" % (r["label"], r["why"]) for r in rows.values())
    return None, "本机没找到可用的 agent（%s）" % miss


def build_cmd_for(key, prompt, session_id=None, cwd=None, permission_mode=None,
                  tools=None, extra=None):
    """按 agent 拼命令行。返回 (cmd, mode)：mode 决定输出怎么解析。"""
    cfg = _local_cfg()
    if cfg.get("cmd"):                       # 自定义命令行：{prompt}/{cwd} 占位
        cmd = [str(x).replace("{prompt}", prompt).replace("{cwd}", cwd or "")
               for x in cfg["cmd"]]
        return cmd, cfg.get("cmd_mode") or "text"
    if key == "zcode":
        cli = zcode_cli()
        if not cli:
            raise RuntimeError("ZCode CLI 不可用（没找到 resources/glm/zcode.cjs）")
        cmd = [node_exe(), cli, "--prompt", prompt, "--mode", "yolo"]
        if cwd:
            cmd += ["--cwd", cwd]
        if session_id:
            cmd += ["--resume", session_id]      # 钉住本项目自己的会话，不用 -c（不抢用户正在聊的）
        return cmd, "text"
    if key == "dsh":
        pkg = dsh_pkg()
        if not pkg:
            raise RuntimeError("DSH 不可用（没找到 @deepseek-ai/dsh）")
        cmd = [node_exe(), pkg, "--profile", "headless", prompt]
        return cmd, "text"          # stdout = 最终答复；stderr = 推理过程（当进度用）
    if key == "codex":
        exe = codex_exe()
        if not exe:
            raise RuntimeError("codex 不可用")
        cmd = [exe, "exec", "--json", "--skip-git-repo-check",
               "--dangerously-bypass-approvals-and-sandbox"]
        if cwd:
            cmd += ["-C", cwd]
        if session_id:
            cmd += ["resume", session_id]
        cmd.append(prompt)
        return cmd, "codex-json"
    return build_cmd(prompt, session_id=session_id, permission_mode=permission_mode or "acceptEdits",
                     tools=tools, extra=extra), "workbuddy-json"


def _extract_codex_line(line):
    """从 codex --json 的一行里挖出「可读文本」和最终消息。"""
    try:
        obj = json.loads(line)
    except ValueError:
        return line.strip(), None
    if not isinstance(obj, dict):
        return "", None
    t = obj.get("type") or ""
    msg = obj.get("message") or obj.get("msg") or ""
    if isinstance(msg, dict):
        msg = msg.get("content") or msg.get("text") or ""
    if isinstance(msg, list):
        msg = " ".join(str(x.get("text", "")) if isinstance(x, dict) else str(x) for x in msg)
    text = " ".join(str(msg).split())
    final = None
    if t in ("agent_message", "message", "response.completed", "turn.completed"):
        final = text or None
    return text, final


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
        permission_mode="acceptEdits", tools=None, on_line=None, extra=None,
        agent=None):
    """叫 agent 干一件事（**边跑边回调每一行**，界面靠它显示实时进度）。

    返回 dict: {ok, agent, session_id, text, res, returncode, cmd, error, seconds}
    - 输出**真流式**：Popen 逐行读，`on_line(text)` 立刻拿到（原先用 subprocess.run，
      行要等进程结束才一起回调，进度条会"卡在 0% 然后跳到 96%"——2026-09-15 修）。
    - on_line 里改 UI 要小心：它跑在后台线程，GUI 侧要用 after/SSE 转一手。
    - 超时就杀掉子进程，别留孤儿。
    """
    key, why = pick_agent(agent)
    if not key:
        return {"ok": False, "error": why, "session_id": session_id, "text": "", "agent": None}
    out_file = None
    try:
        cmd, mode = build_cmd_for(key, prompt, session_id=session_id, cwd=cwd,
                                 permission_mode=permission_mode, tools=tools, extra=extra)
    except RuntimeError as e:
        return {"ok": False, "error": str(e), "session_id": session_id, "text": "",
                "agent": key}
    if mode == "codex-json":                    # codex 的最终消息落文件最稳（-o 在 cmd 末尾之前）
        import tempfile
        fd, out_file = tempfile.mkstemp(prefix="heronbo_last_", suffix=".txt")
        os.close(fd)
        cmd = cmd[:-1] + ["-o", out_file, cmd[-1]]
    env = child_env()
    text_parts, tail = [], []
    try:
        p = subprocess.Popen(cmd, cwd=cwd or HERE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                             encoding="utf-8", errors="replace", env=env,
                             bufsize=1, **no_window_kwargs())
    except OSError as e:
        return {"ok": False, "error": "起不来：%s" % e, "session_id": session_id,
                "text": "", "cmd": cmd, "agent": key}
    killed = {"timeout": False}

    def _kill():
        killed["timeout"] = True
        try:
            p.kill()
        except OSError:
            pass

    err_lines = []

    def _drain_err():
        """stderr 必须单独抽干：DSH 无头档把**推理过程**全写 stderr，量大到能灌满管道
        （管道满了子进程会阻塞 → 看起来"卡住不动"）。同时把它当进度喂给界面。"""
        try:
            for line in p.stderr:
                line = line.rstrip().rstrip(chr(13) + chr(10))
                if not line.strip():
                    continue
                err_lines.append(line)
                if len(err_lines) > 400:
                    del err_lines[:-400]
                if on_line and mode == "text":
                    on_line(chr(183) + " " + line.strip()[:200])
        except Exception:                                        # noqa: BLE001
            pass

    threading.Thread(target=_drain_err, daemon=True).start()
    timer = threading.Timer(timeout, _kill)
    timer.start()
    try:
        for line in p.stdout:                   # 逐行读 = 真流式
            line = line.rstrip().rstrip("\r\n")
            if not line.strip():
                continue
            tail.append(line)
            if len(tail) > 400:
                del tail[:-400]
            if mode == "codex-json":
                txt, final = _extract_codex_line(line)
                if final:
                    text_parts.append(final)
                if on_line and txt:
                    on_line(txt)
            else:
                try:
                    obj = json.loads(line)
                    txt = _pick_text(obj) if isinstance(obj, dict) else ""
                except ValueError:
                    txt = line
                if txt and on_line:
                    on_line(txt)
        p.wait(timeout=20)
    except Exception:                                            # noqa: BLE001
        pass
    finally:
        timer.cancel()
        err = chr(10).join(err_lines)
    rc = p.returncode
    raw = chr(10).join(tail)
    if mode == "text":                     # DSH：stdout 就是最终答复
        text = raw.strip()
        ok = (rc == 0) and bool(text)
        return {"ok": ok, "agent": key, "agent_label": ADAPTERS[key]["label"],
                "session_id": session_id, "text": text, "cmd": cmd, "returncode": rc,
                "stderr": err[-2000:],
                "error": "" if ok else ("超时 %ds 已终止" % timeout if killed["timeout"]
                                        else (text or ("CLI 返回码 %s" % rc)))}
    if mode == "codex-json":
        text = (text_parts[-1] if text_parts else "").strip()
        if out_file and os.path.isfile(out_file):
            try:
                body = open(out_file, encoding="utf-8", errors="replace").read().strip()
                if body:
                    text = body
            except OSError:
                pass
            try:
                os.remove(out_file)
            except OSError:
                pass
        ok = (rc == 0) and bool(text)
        return {"ok": ok, "agent": key, "agent_label": ADAPTERS[key]["label"],
                "session_id": session_id, "text": text, "cmd": cmd, "returncode": rc,
                "stderr": err[-2000:],
                "error": "" if ok else ("超时 %ds 已终止" % timeout if killed["timeout"]
                                        else "CLI 返回码 %s" % rc)}
    res = _parse_output(raw)
    sid = session_id
    text = ""
    if res:
        sid = res.get("session_id") or res.get("sessionId") or session_id
        text = res.get("result") or _pick_text(res) or ""
        if isinstance(text, list):
            text = "".join(seg.get("text", "") if isinstance(seg, dict) else str(seg)
                           for seg in text)
    if not text:
        text = raw.strip()
    is_err = bool(res and res.get("is_error"))
    ok = (rc == 0) and (not is_err) and bool(text)
    return {"ok": ok, "agent": key, "agent_label": ADAPTERS[key]["label"],
            "session_id": sid, "text": text, "res": res, "returncode": rc, "cmd": cmd,
            "stderr": err[-2000:],
            "error": "" if ok else ("超时 %ds 已终止" % timeout if killed["timeout"]
                                    else ("agent 报错" if is_err else "CLI 返回码 %s" % rc))}


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
