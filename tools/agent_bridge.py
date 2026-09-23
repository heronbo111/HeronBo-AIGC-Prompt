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
import re
import shutil
import socket
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# 本机路径一律**按环境变量推导 + 常见位置探测**，不写死用户名（否则会被发布防护钩子拦下）。
# 需要覆盖时写 agent_bridge.local.json（已 gitignore）：{"node": "...", "cli_js": "...", "agent": "..."}

DEFAULT_TIMEOUT = 600

_CURRENT = {"proc": None}          # 正在跑的子进程（给"停止"用）


def zcode_rollout_dir():
    """ZCode CLI 的会话正文目录（`~/.zcode/cli/rollout/model-io-<sess_xxx>.jsonl`）。"""
    return os.path.join(os.path.expanduser("~"), ".zcode", "cli", "rollout")


def zcode_sessions():
    """{会话文件名: mtime}——跑之前拍一张，跑完对比就知道新会话是哪个。"""
    d = zcode_rollout_dir()
    out = {}
    try:
        for fn in os.listdir(d):
            if fn.endswith(".jsonl"):
                try:
                    out[fn] = os.path.getmtime(os.path.join(d, fn))
                except OSError:
                    pass
    except OSError:
        pass
    return out


def zcode_session_from_snapshot(before, since):
    """跑完之后：认出**这次新建**的会话文件，取出 `sess_xxx` 当会话 id。

    为什么这么绕：ZCode CLI 的 `--prompt` 只把结果打在 stdout，**不回会话 id**
    （`--resume` 要 sess_ 开头那个 id，但它没有"指定 id"的参数）。而它每次开新会话都会新建
    `~/.zcode/cli/rollout/model-io-<sess_xxx>.jsonl`——"跑前快照里没有、跑完出现了"的那个就是本次会话。
    2026-09-16 用户问"是不是每次新开对话重读 skill"，查出来正是：text 模式只回显输入 id，
    第一次没有、返回来也没有 → 项目里永远存不到会话 → 每次都新开。

    ⚠️ **只认"新出现的文件"，没有兜底**（2026-09-16 实测踩坑）：起初我按"最近被改写的那个"来认，
    结果用户同时在用 ZCode（他别的会话文件也在被改写）→ **把他的会话误当成本次会话去 `--resume`**，
    等于污染了他的对话。拿不准就返回空——宁可白开一轮，也不能串到别人的会话上。
    """
    after = zcode_sessions()
    fresh = [fn for fn in after if fn not in before]        # 只在"跑之前不存在"的文件里挑
    if not fresh:
        return ""
    sid = ""
    for fn in sorted(fresh, key=lambda f: -after.get(f, 0)):
        m = re.search(r"model-io-(sess_[A-Za-z0-9\-]+)\.jsonl", fn)
        if m:
            sid = m.group(1)
            break
    if not sid or after.get("model-io-%s.jsonl" % sid, 0) < since - 30:
        return ""                                           # 再核一次写入时间（防时钟误差）
    return sid


def proc_children():
    """当前 agent 进程**自己拉起来的子进程**（含孙进程）：[(pid, 名字)]。

    为什么要它（2026-09-17 用户要求"进程可视化 + 能刹住"）：agent 干活时会自己起
    ffmpeg / python 深度视频.py 这类长任务；只看界面的阶段是看不见的，用户也没法判断
    "它现在是不是在做我不想要的那件事"。
    实现用 Windows 的 Toolhelp32 快照（纯 ctypes，毫秒级，不依赖 wmic/PowerShell）。
    """
    root = (_CURRENT.get("proc").pid if _CURRENT.get("proc") else 0)
    if not root or os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes

    TH32CS_SNAPPROCESS = 0x00000002
    MAX_PATH = 260

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_char * MAX_PATH)]

    try:
        k32 = ctypes.windll.kernel32
        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap == -1:
            return []
        rows, ents = {}, {}
        e = PROCESSENTRY32()
        e.dwSize = ctypes.sizeof(PROCESSENTRY32)
        more = k32.Process32First(snap, ctypes.byref(e))
        while more:
            pid, ppid = int(e.th32ProcessID), int(e.th32ParentProcessID)
            name = e.szExeFile.decode("mbcs", "replace")
            rows[pid] = (ppid, name)
            ents[pid] = e
            more = k32.Process32Next(snap, ctypes.byref(e))
        k32.CloseHandle(snap)
    except Exception:                                            # noqa: BLE001
        return []
    # 从 root 出发把整棵树收下来（含孙进程）
    out, frontier, seen = [], [root], set()
    while frontier:
        cur = frontier.pop()
        for pid, (ppid, name) in rows.items():
            if ppid == cur and pid not in seen:
                seen.add(pid)
                out.append((pid, name))
                frontier.append(pid)
    return out


def heavy_children():
    """子进程里"看得懂的那几个长任务"→ [{pid, name, label, what}]（给界面显示"现在在做 X"）。

    只挑我们认得的：ffmpeg/ffprobe（转码、合成、抽帧）、python（深度视频/转写/遮罩那几个脚本）、
    node（另一个 CLI）等；认不出来的一律不报，别拿噪声糊界面。
    """
    KNOWN = {
        "ffmpeg.exe": ("转码 / 合成 / 抽帧", True),
        "ffprobe.exe": ("读时长（很快）", False),
        "python.exe": ("跑脚本（深度视频 / 转写 / 遮罩…）", True),
        "pythonw.exe": ("跑脚本（深度视频 / 转写 / 遮罩…）", True),
        "node.exe": ("另一个 CLI 在干活", True),
        "msedgewebview2.exe": ("浏览器内核", False),
        "msedge.exe": ("浏览器", False),
    }
    out = []
    for pid, name in proc_children():
        low = name.lower()
        if low not in KNOWN:
            continue
        label, heavy = KNOWN[low]
        out.append({"pid": pid, "name": name, "label": label, "heavy": heavy,
                    "seconds": _proc_seconds(pid)})
    return out


def _proc_seconds(pid):
    """这个进程跑了多少秒（拿不到就 0）。"""
    if os.name != "nt":
        return 0
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, int(pid))       # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return 0
        try:
            creation, exit_, kern, user = (wintypes.FILETIME(), wintypes.FILETIME(),
                                           wintypes.FILETIME(), wintypes.FILETIME())
            if not k32.GetProcessTimes(h, ctypes.byref(creation), ctypes.byref(exit_),
                                       ctypes.byref(kern), ctypes.byref(user)):
                return 0
            ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
            secs = ticks / 10000000.0 - 11644473600         # FILETIME → Unix 秒
            return max(0, int(time.time() - secs))
        finally:
            k32.CloseHandle(h)
    except Exception:                                            # noqa: BLE001
        return 0


def stop_current():
    """把正在跑的 agent 任务杀掉（界面上的「暂停」按钮）。

    为什么需要：agent 一跑就是几分钟，ZCode 这条通道还不吐过程输出；万一它卡住/跑飞了，
    用户只能等超时（30 分钟）——这不合理。

    ⚠️ **必须连它拉起来的子进程一起杀**（2026-09-17 用户实测教训：让 agent 别做深度片，
    点了停止却发现它还在算）：CLI 是 node/Electron，ffmpeg 和"深度视频.py"是**它的孩子**，
    只 kill 父进程在 Windows 上不会带走孙子 → 这里用 `taskkill /T`（整棵树）。
    """
    p = _CURRENT.get("proc")
    if not p:
        return False
    kids = proc_children()
    ok = False
    if os.name == "nt":
        try:
            r = subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"],
                               capture_output=True, text=True, errors="replace",
                               timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            ok = r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
    if not ok:
        try:
            p.kill()
            ok = True
        except OSError:
            ok = False
    if ok:
        _CURRENT["stopped_kids"] = len(kids)
    return ok


def current_alive():
    """当前子进程还活着吗（用于"还在跑"的心跳：进程死了就别报"还在跑"）。"""
    p = _CURRENT.get("proc")
    if not p:
        return False
    try:
        return p.poll() is None
    except OSError:
        return False

_CFG_DIR = None


def _can_write(d):
    """目录能不能写（真写一个探针文件，比 os.access 在 Windows 上靠谱）。"""
    p = os.path.join(d, ".heronbo_probe")
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write("")
        os.remove(p)
        return True
    except OSError:
        return False


def _pick_cfg_dir():
    """配置文件放哪：源码运行＝tools/；打包后＝**exe 旁边**。

    ⚠️ 为什么不能直接用 HERE（2026-09-15 实测踩到）：onefile 打包后 `__file__` 落在
    PyInstaller 的临时解包目录 `%TEMP%\\_MEIxxxx`，程序一关整个目录连文件一起删 →
    用户选的 agent 每次都丢、重开退回自动挑选（＝workbuddy，它在候选里排第一）。
    证据：两个临时目录里分别躺着 `{"agent":"zcode"}`、`{"agent":"workbuddy"}`，
    而 exe 旁边一份都没有。
    """
    cands = []
    if getattr(sys, "frozen", False):
        try:
            cands.append(os.path.dirname(os.path.abspath(sys.executable)))
        except (OSError, ValueError):
            pass
    cands.append(HERE)
    for d in cands:
        if d and os.path.isdir(d) and _can_write(d):
            return d
    d = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                     "HeronBoScoreTool")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        return HERE
    return d


def cfg_dir():
    global _CFG_DIR
    if _CFG_DIR is None:
        _CFG_DIR = _pick_cfg_dir()
    return _CFG_DIR


def cfg_path():
    """配置文件的真实路径（界面会显示它，免得再"改了不生效/存哪了"扯不清）。"""
    return os.path.join(cfg_dir(), "agent_bridge.local.json")


def _local_cfg():
    try:
        with open(cfg_path(), encoding="utf-8-sig") as f:
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


def _app_roots():
    """可能装着"带 CLI 的桌面应用"的根目录（WorkBuddy / ZCode …）。

    2026-09-16 别的机器上踩到：ZCode 装在 `F:\\新建文件夹 (3)\\ZCode\\`，原来只搜
    `<盘>\\ZCode\\...` 与 `<盘>\\*\\resources\\...` 两种形状（`*` 只覆盖一层），
    它多了一层 → 探测说"没找到 ZCode 自带的 CLI"，可文件明明是有的（12.3MB）。
    所以这里按"根目录 + 1~3 层通配"多铺几种，浅的先试（`_glob_first` 按返回值排序取第一个）。
    """
    roots = []
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                 os.environ.get("ProgramW6432"),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Python"),
                 "F:\\", "D:\\", "E:\\", "C:\\"):
        if base and os.path.isdir(base) and base not in roots:
            roots.append(base)
    return roots


def _glob_in_roots(rel_parts, max_depth=3):
    """在候选根目录里找 `rel_parts` 这么一段相对路径（逐层加深，浅的先试）。

    例：`resources/glm/zcode.cjs` → `<根>/resources/...`、`<根>/*/resources/...`、
    `<根>/*/*/resources/...`。**故意的**：多一层少一层都能找到，代价只是多几次 glob。
    """
    pats = []
    for r in _app_roots():
        for d in range(0, max_depth + 1):
            pats.append(os.path.join(r, *(["*"] * d), *rel_parts))
    return pats


def cli_js():
    """找 WorkBuddy 的 headless CLI 入口。

    推导顺序：显式配置 → 环境变量给的安装位置 → 常见安装目录 glob（含"多套一层目录"的形状）。
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
    tail = ("resources", "app.asar.unpacked", "cli", "dist", "codebuddy-headless.js")
    pats = [os.path.join(r, *tail) for r in roots]
    pats += [os.path.join(r, "WorkBuddy", *tail) for r in roots]
    pats += _glob_in_roots(tail)
    hit = _glob_first(pats)
    if hit:
        return hit
    # 有些版本把 CLI 打进 asar 里（没有 unpacked 目录）→ 只能靠命令行入口，别硬猜
    return None


def _probe_doubaowork():
    """豆包工作：只看"客户端在哪 + 桥脚本能不能跑"——**探测不启动它**（探测要有无副作用）。

    能不能接管（CDP 通不通）留给 quick_check（那个会真跑一次 `status`）。
    """
    js = doubao_js()
    if not js:
        return False, "找不到豆包桥脚本 tools\\doubao_cdp.mjs（它随本仓库走，别单独拷一半）"
    exe = doubaowork_exe()
    if not exe:
        return False, ("没找到豆包工作客户端（装官方桌面端即可；装在别处就用 "
                       "agent_bridge.local.json 的 \"doubaowork\" 字段写 DoubaoWork.exe 绝对路径）")
    if not lib_runner(js)[0]:
        return False, "跑不了桥脚本：需要 Node（没有独立 node 时，本机的 WorkBuddy/ZCode 自带 Electron 也能当 node）"
    if _doubaowork_running():
        return True, exe + "（正在运行；接管它要**用调试端口起的**那份，否则先退出重开，见 tools\\doubao_cdp.mjs）"
    return True, exe + "（没在跑：点「出提示词」时我会带调试端口把它拉起来）"


def doubaowork_exe():
    """找豆包工作客户端（DoubaoWork.exe）。装的位置很自由（本机在 E:\\DoubaoWork\\app\\）。"""
    cfg = _local_cfg().get("doubaowork")
    if cfg and os.path.isfile(cfg):
        return cfg
    pats = [r"E:\DoubaoWork\app\DoubaoWork.exe", r"F:\DoubaoWork\app\DoubaoWork.exe",
            r"D:\DoubaoWork\app\DoubaoWork.exe", r"C:\DoubaoWork\app\DoubaoWork.exe",
            os.path.join(os.environ.get("ProgramFiles", ""), "DoubaoWork", "app", "DoubaoWork.exe"),
            os.path.join(os.environ.get("ProgramFiles(x86)", ""), "DoubaoWork", "app", "DoubaoWork.exe"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "DoubaoWork", "app", "DoubaoWork.exe"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "DoubaoWork", "app", "DoubaoWork.exe")]
    for r in _app_roots():
        pats.append(os.path.join(r, "DoubaoWork", "app", "DoubaoWork.exe"))
        pats.append(os.path.join(r, "DoubaoWork", "DoubaoWork.exe"))
    return _glob_first(pats)


def _doubaowork_running():
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq DoubaoWork.exe", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, errors="replace", timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "DoubaoWork.exe" in (out or "")


def doubao_js():
    """豆包工作 CDP 桥脚本（tools/doubao_cdp.mjs）。打包后要能在解包目录里找到它。"""
    cfg = _local_cfg().get("doubao_js")
    if cfg and os.path.isfile(cfg):
        return cfg
    cands = [os.path.join(HERE, "doubao_cdp.mjs")]
    for d in (getattr(sys, "_MEIPASS", ""), os.path.dirname(os.path.abspath(sys.executable))):
        if d:
            cands.append(os.path.join(d, "doubao_cdp.mjs"))
    return _glob_first(cands)


def node_exe():
    """找 node：显式配置 → PATH → 常见安装位置（含 winget / nvm / volta / scoop / 各盘）。"""
    cfg = _local_cfg().get("node")
    if cfg and os.path.isfile(cfg):
        return cfg
    hit = shutil.which("node")
    if hit:
        return hit
    pats = [r"C:\Program Files\nodejs\node.exe", r"C:\Program Files (x86)\nodejs\node.exe",
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "nodejs", "node.exe"),
            os.path.join(os.environ.get("ProgramData", ""), "chocolatey", "bin", "node.exe"),
            os.path.join(os.path.expanduser("~"), "scoop", "shims", "node.exe"),
            os.path.join(os.environ.get("APPDATA", ""), "nvm", "node.exe"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Volta", "bin", "node.exe")]
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if d and "node" in d.lower():
            pats.append(os.path.join(d, "node.exe"))
    for r in _app_roots()[len(_app_roots()) - 4:]:     # 各盘根里的 nodejs\node.exe
        pats.append(os.path.join(r, "nodejs", "node.exe"))
    return _glob_first(pats)


# 被我们"当 node 用"的 Electron 宿主 exe（跑的时候要注入 ELECTRON_RUN_AS_NODE=1）。
# 2026-09-16 新增：别的机器上**没有独立 node**，但 WorkBuddy / ZCode 都是 Electron 应用，
# 它们自带的那个 exe 加上这个环境变量就是一个完整的 node（这正是它们 CLI 元信息里写的
# "runtime": "electron-node" 的意思）。不开这条，新机永远接不上 agent 通道。
_ELECTRON_AS_NODE = set()


def _is_node_exe(path):
    return os.path.basename(path or "").lower() in ("node", "node.exe", "node64.exe")


def electron_host(script):
    """给一个 Electron 应用里的脚本，找它的宿主 exe（CLI 就是拿它当 node 跑的）。

    从脚本往上找 `resources` 那一层：它上面就是应用根，根里通常只有一个主 exe
    （`WorkBuddy.exe` / `ZCode.exe`）。卸载器、更新器之类排除掉。
    """
    d = os.path.dirname(os.path.abspath(script))
    for _ in range(6):
        if not d or os.path.dirname(d) == d:
            break
        if os.path.basename(d).lower() == "resources":
            root = os.path.dirname(d)
            skip = ("unins", "uninstall", "update", "setup", "squirrel", "crashpad")
            try:
                exes = [f for f in sorted(os.listdir(root))
                        if f.lower().endswith(".exe")
                        and not any(s in f.lower() for s in skip)]
            except OSError:
                return None
            # 名字跟目录同名的最优先（WorkBuddy.exe / ZCode.exe）
            base = os.path.basename(root).lower()
            exes.sort(key=lambda f: (0 if f.lower() == base + ".exe" else 1, len(f)))
            return os.path.join(root, exes[0]) if exes else None
        d = os.path.dirname(d)
    return None


def lib_runner(script):
    """跑一个 Node CLI 脚本要用什么、带什么环境 → `(exe, env_extra)`。

    优先独立 node；**没有 node 就借该应用自带的 Electron 当 node**（注入
    `ELECTRON_RUN_AS_NODE=1`）——新机器上最省事的一条路，不用额外装 Node.js。
    """
    n = node_exe()
    if n and _is_node_exe(n):
        return n, {}
    host = electron_host(script)
    if host:
        _ELECTRON_AS_NODE.add(os.path.normcase(host))
        return host, {"ELECTRON_RUN_AS_NODE": "1"}
    return n, {}


def runtime_env_for(cmd):
    """跑之前要补的环境变量（只有"拿 Electron 当 node"时才需要）。"""
    if cmd and not _is_node_exe(cmd[0]) and os.path.normcase(cmd[0]) in _ELECTRON_AS_NODE:
        return {"ELECTRON_RUN_AS_NODE": "1"}
    return {}


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
            "cfg": cfg_path(),         # 选择记在哪（界面显示出来，方便核对"存住没有"）
            "running": running_desktop(),   # 哪些客户端正开着（自动跟随的依据）
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
        with open(cfg_path(), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError as e:
        return False, "写不进 %s：%s" % (cfg_path(), e)
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
    """拼命令行（WorkBuddy）。tools=None 表示不限制；tools="" 表示禁用全部工具（自检用）。"""
    js = cli_js()
    if not js:
        raise RuntimeError("headless CLI 不可用：%s" % available()[1])
    runner, _renv = lib_runner(js)
    if not runner:
        raise RuntimeError("WorkBuddy 的 CLI 找到了，但没有 node、也没找到 WorkBuddy 的 exe")
    cmd = [runner, js, "-p", "--output-format", output_format]
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
# 各 agent 的**用户级技能目录**（按"相对 ~ 的路径段"写，末尾再拼 skill 名）。
# 2026-09-23：加了豆包两兄弟——个人版豆包在 `~/Doubao/skills`（本机实测存在，
# 之前只被 scan_hosts 报成"疑似"）；豆包工作（企业版）把用户技能放在它的 Electron
# profile 里（`.user_skills`），路径长但一样是"目录联接"能挂的地方。
HOMES = {
    "zcode": (".zcode", "skills"),
    "codex": (".codex", "skills"),
    "dsh": (".dsh", "skills"),
    "workbuddy": (".workbuddy", "skills"),
    "doubao": ("Doubao", "skills"),
    "doubaowork": ("AppData", "Local", "DoubaoWork", "User Data", "Default", ".doubaowork",
                   "agent_mode", "workspace", ".user_skills"),
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
    for key, parts in HOMES.items():
        p = os.path.join(home, *parts, name)
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


def claude_exe():
    """找 Claude Code 的 CLI（`claude`）。

    2026-09-16 用户要求："有 Claude Code 的用户难道还不给他显示 Claude Code 的接口吗？
    （当然还会有很多不同的 agent，要根据环境而定）"——所以适配器表按"环境里有什么就认什么"。
    三种装法都覆盖：npm 全局（`claude.cmd`）、官方 native 安装（`~/.local/bin/claude.exe`）、
    本地版（`~/.claude/local/claude.exe`）。
    ⚠️ 本机（作者机）**没装 Claude Code**，这条是照官方文档的 CLI 形状写的、**未实测**；
    真跑通了/跑不通都请把回执贴回来，我按实测改。
    """
    cfg = _local_cfg().get("claude")
    if cfg and os.path.isfile(cfg):
        return cfg
    hit = shutil.which("claude.cmd") or shutil.which("claude") or shutil.which("claude.exe")
    if hit:
        return hit
    home = os.path.expanduser("~")
    pats = [os.path.join(os.environ.get("APPDATA", ""), "npm", "claude.cmd"),
            os.path.join(home, "AppData", "Roaming", "npm", "claude.cmd"),
            os.path.join(home, ".local", "bin", "claude.exe"),
            os.path.join(home, ".local", "bin", "claude"),
            os.path.join(home, ".claude", "local", "claude.exe"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "claude", "claude.exe"),
            os.path.join(os.environ.get("ProgramFiles", ""), "Claude", "claude.exe")]
    return _glob_first(pats)


def _probe_claude():
    exe = claude_exe()
    if not exe:
        return False, "没找到 claude（装法：`npm i -g @anthropic-ai/claude-code`，或官方安装脚本；需要 Node.js）"
    if exe.lower().endswith((".cmd", ".bat")) and not node_exe():
        return False, "找到 %s，但它是 npm 脚本、本机没有 node 跑不了（装 Node.js 或用官方 native 安装）" % exe
    return True, exe


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
    r"""找 ZCode 自带的 CLI。

    ZCode 桌面端里**真的带了一个命令行**（我原先只查了桌面端主进程的参数白名单，漏了这个）：
        <ZCode安装目录>\resources\glm\zcode.cjs   （12.6MB Node 单文件，zcode 0.16.5）
    它的无头用法（`zcode --help` 实证）：
        zcode --prompt "<任务>" --cwd <目录> --mode yolo [--resume <sess_...>] [--json]
    注意：`--settings` / `--config` 虽然写在帮助里，但实际报 Unknown option（文档与实现不一致），
    模型配置只能落在它固定的 `~/.zcode/cli/config.json`（要显式 provider）。
    """
    cfg = _local_cfg().get("zcode")
    if cfg and os.path.isfile(cfg):
        return cfg
    # 2026-09-16 修：原来只试 `<盘>\ZCode\...` 与 `<盘>\*\resources\...`，
    # 装在 `F:\新建文件夹 (3)\ZCode\` 这种"多一层"的机器上就找不到 → 改走统一的逐层搜索。
    return _glob_first(_glob_in_roots(("resources", "glm", "zcode.cjs")))


def _probe_zcode():
    cli = zcode_cli()
    if not cli:
        return False, "没找到 ZCode 自带的 CLI（resources/glm/zcode.cjs）"
    runner, renv = lib_runner(cli)
    if not runner:
        return False, ("找到了 CLI 但没东西能跑它：既没有 node，也没在 %s 旁边找到 ZCode 的 exe"
                       % os.path.dirname(cli))
    how = "Electron 当 node（没装 Node.js 也能跑）" if renv else "node"
    # 就绪检查：CLI 明确要求 ~/.zcode/cli/config.json 里有 provider（2026-09-15 实测报
    # "Model config is missing"）。顺手查一下，别让用户"选了才发现"。
    cfgp = os.path.join(os.path.expanduser("~"), ".zcode", "cli", "config.json")
    try:
        cfg = json.load(open(cfgp, encoding="utf-8-sig"))
    except (OSError, ValueError):
        return False, ("CLI 找到了（用 %s 跑），但缺 %s：先跑一次 `zcode login`"
                       "（或把桌面端 ~/.zcode/v2/config.json 的 provider 搬过去）" % (how, cfgp))
    # 两种就绪形态都认：① 有 provider（自己填的 API Key 通道）
    # ② 有 model（`zcode login` 后它自己写的 "provider/model" 引用，如 zai/glm-5.1）
    if cfg.get("provider") or cfg.get("model"):
        return True, cli
    return False, ("CLI 找到了（用 %s 跑），但 %s 里没有 provider 或 model（现在只有 %s）；"
                   "先跑一次 `zcode login`，或把桌面端 ~/.zcode/v2/config.json 的 provider 搬过去"
                   % (how, cfgp, "/".join(cfg.keys())))


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
    pkg = dsh_pkg()
    if not pkg:
        return False, "没找到 DSH（需要 npx @deepseek-ai/dsh 跑过一次，或全局安装）"
    runner, _renv = lib_runner(pkg)
    if not runner:
        return False, "DSH 脚本找到了，但本机没有 node（DSH 不是 Electron 应用，必须装 Node.js）"
    # ⚠️ 命中 npx 缓存 ≠ 装了它（2026-09-22 用户反馈）：「谁以前用 npx 跑过一次」就会在
    # `npm-cache\_npx\…` 留下整包，被当成"装了 DSH 且可用"。这种标成 weak（疑似），
    # 界面显示成"未验证"，要用户点一下「测一下」才认。
    if "npm-cache" in pkg.lower() or "_npx" in pkg.lower():
        return True, ("只找到 npx 缓存里的那份（以前 npx 跑过一次留下的）：%s"
                      "；未验证，点「测一下」确认" % pkg)
    return True, pkg + _dsh_note()


def _probe_codex():
    exe = codex_exe()
    if not exe:
        return False, "没找到 codex（需要先装 Codex CLI）"
    # npm 装的是 .cmd/.bat 壳，没 node 跑不起来——别只看文件在就报"可用"
    # （2026-09-22：用户反馈"只装了 WorkBuddy 却显示别的通道都接入了"，残留 shim 是主因之一）
    if exe.lower().endswith((".cmd", ".bat")) and not node_exe():
        return False, "找到 %s，但它是 npm 脚本、本机没有 node 跑不了（装 Node.js 或改用 native 安装）" % exe
    return True, exe


def _probe_workbuddy():
    js = cli_js()
    if not js:
        return False, "没找到 headless CLI（codebuddy-headless.js）——装 WorkBuddy 桌面端即可"
    runner, renv = lib_runner(js)
    if not runner:
        return False, ("CLI 找到了，但没有 node、也没在 %s 旁边找到 WorkBuddy 的 exe"
                       % os.path.dirname(js))
    return True, (js + ("（用 WorkBuddy 自带 Electron 跑，没装 Node.js 也行）" if renv else ""))


ADAPTERS = {
    "workbuddy": {"label": "WorkBuddy", "probe": _probe_workbuddy,
                  "proc": ["WorkBuddy.exe"],
                  "transcripts": [(".workbuddy", "projects", "*", "{sid}.jsonl")]},
    "codex": {"label": "Codex CLI", "probe": _probe_codex, "proc": [],
              "transcripts": [(".codex", "sessions", "*", "*", "rollout-*{sid}*.jsonl")]},
    # 豆包工作（2026-09-23 加；用户："企业统一要求采用豆包工作"）。**它没有 CLI**——
    # 只有 Electron 桌面端（`E:\DoubaoWork\app\DoubaoWork.exe`，Chrome/147）。所以走 CDP 桥：
    # `tools/doubao_cdp.mjs` 带 `--remote-debugging-port` 把它拉起来 → 往聊天页输入框灌 prompt
    # → 从它自己的会话轨迹里流式读进度与答复（轨迹每行 {role,content,tool_calls}）。
    # transcripts 的 {sid} ＝那串数字会话目录名（如 38439925116803842）。
    # 个人版豆包同款结构（目录名 `.doubao`），需要时把它也照这形状加一条即可。
    "doubaowork": {"label": "豆包工作", "probe": _probe_doubaowork,
                   "proc": ["DoubaoWork.exe"],
                   "transcripts": [("AppData", "Local", "DoubaoWork", "User Data", "Default",
                                    ".doubaowork", "agent_mode", "workspace", ".sessions",
                                    "{sid}", "agents", "*", "system", "trajectory.jsonl")]},
    # 这两个先留探测位：ZCode / DSH 目前没在安装目录暴露无头 CLI。
    # 找得到就把命令写进 agent_bridge.local.json 的 cmd 字段（见 pick_agent 的说明）。
    # ZCode：桌面端主进程参数里没有无头入口，但**安装目录里自带 CLI**（resources/glm/zcode.cjs），
    # 支持 `--prompt` 无头跑一条任务 —— 2026-09-15 实证找到（先前只查了桌面端参数白名单）。
    # 唯一前置：`~/.zcode/cli/config.json` 里要有显式 provider，否则报 "Model config is missing"。
    "zcode": {"label": "ZCode CLI", "probe": _probe_zcode, "proc": ["ZCode.exe"],
              # 2026-09-16 查实：ZCode 的 CLI 会话正文在 ~/.zcode/cli/rollout/model-io-<sess_xxx>.jsonl
              "transcripts": [(".zcode", "cli", "rollout", "model-io-{sid}.jsonl")]},
    "dsh": {"label": "DSH（DeepSeek Harness）", "probe": _probe_dsh, "proc": [],
            "transcripts": [], "weak": True},   # weak：命中 npx 缓存也算"找到"，别当已装
    # Claude Code（2026-09-16 加）：`claude -p` 无头跑，会话正文在 ~/.claude/projects/<cwd>/<sid>.jsonl。
    # **本机未实测**（作者机没装）；命令形状照官方文档：-p + --output-format stream-json --verbose。
    "claude": {"label": "Claude Code", "probe": _probe_claude, "proc": ["claude.exe"],
               "transcripts": [(".claude", "projects", "*", "{sid}.jsonl")]},
}


_RUN_CACHE = {"t": 0.0, "keys": []}


def _foreground_exe():
    """当前前台窗口属于哪个程序（用来判断"你正在用哪个客户端"）。拿不到就返回空串。"""
    if os.name != "nt":
        return ""
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        hwnd = u.GetForegroundWindow()
        if not hwnd:
            return ""
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return ""
        out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid.value, "/FO", "CSV", "/NH"],
                             capture_output=True, timeout=8, **no_window_kwargs())
        txt = (out.stdout or b"").decode("utf-8", "replace").strip()
        return txt.split(",")[0].strip('"') if txt else ""
    except (OSError, subprocess.SubprocessError, ImportError):
        return ""


def running_desktop(refresh=6):
    """哪些 agent 的**桌面客户端正开着**（用于"自动跟随你正在用的软件"）。

    判据是进程名（各适配器的 `proc`，2026-09-16 实测本机是 `WorkBuddy.exe` / `ZCode.exe`）。
    结果缓存几秒：state 每次刷新都会问，别每次都去 tasklist 拉一遍。
    `_active` 记的是**前台窗口那个**（更贴近"我此刻在用它"）——多个都开着时优先它。
    """
    now = time.time()
    if _RUN_CACHE["keys"] and now - _RUN_CACHE["t"] < refresh:
        return list(_RUN_CACHE["keys"])
    keys, active = [], ""
    if os.name == "nt":
        try:
            out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True,
                                 timeout=8, **no_window_kwargs())
            names = (out.stdout or b"").decode("utf-8", "replace").lower()
            fg = _foreground_exe().lower()
            for key, ad in ADAPTERS.items():
                for p in (ad.get("proc") or []):
                    if p.lower() in names:
                        keys.append(key)
                        if fg and fg == p.lower():
                            active = key
                        break
        except (OSError, subprocess.SubprocessError):
            keys, active = [], ""
    if active:                     # 前台那个排最前：你正在 ZCode 里，就先用 ZCode
        keys = [active] + [k for k in keys if k != active]
    _RUN_CACHE.update({"t": now, "keys": keys, "active": active})
    return list(keys)


def transcript_path(key, cwd, session_id):
    """这个 agent 把**它自己的会话正文**写在哪（能找到就给路径，找不到给空）。

    先按 `key` 找；找不到就**在所有适配器里找一遍**——会话 id 是唯一的，别因为"现在选的 agent
    跟当时跑的不是同一个"就找不到（2026-09-16 踩到：项目里的会话是 WorkBuddy 跑的，
    而当前选的是 ZCode，按 key 找就空）。
    """
    if not session_id:
        return ""
    home = os.path.expanduser("~")
    order = ([key] if key in ADAPTERS else []) + [k for k in ADAPTERS if k != key]
    for k in order:
        for pat in ((ADAPTERS.get(k) or {}).get("transcripts") or []):
            p = os.path.join(home, *[x.replace("{sid}", session_id) for x in pat])
            hit = _glob_first([p])
            if hit:
                return hit
    return ""


# ── 「这条通道到底接上没有」的判定（2026-09-22 用户反馈改写）─────────────────
# 用户看到的现象：别人电脑上**只装了 WorkBuddy**，工作台却列出一堆通道且都算"可用"。
# 根因：原来的 ok **只等于"盘上找到了那个文件"**，而这些都会被算成"找到了"——
#   · npm 留下的残留 shim（装过又卸了：`AppData\Roaming\npm\codex.cmd` 还在）；
#   · **npx 缓存里的包**（谁以前 `npx @deepseek-ai/dsh` 跑过一次就永久躺在
#     `npm-cache\_npx\…` 里）→ 被当成"装了 DSH"；
#   · 别的软件同名的目录/可执行文件。
# 现在规矩是三条，界面据此显示三态（不再把"找到文件"说成"接上了"）：
#   ① found ：盘上确实有它（探针通过）——没找到的收进「本机没装的」折叠块；
#   ② 可用   ：found **且没被实测否掉**（没测过也允许用，但界面标「未验证」）；
#   ③ verified：**真跑过**才算"接上了"——`--version` 这种免费自检，或真跑一句
#              （每次成功干完活也会自动记上）。结果与时间戳写进
#              `agent_bridge.local.json` 的 `live` 段，每台机器各记各的。
# 另外**不再预设"支持哪些 agent"**：内置那几条只是为了"开箱能用"，别的 harness
# （opencode / Hermes / …）靠两件事发现——扫盘看宿主目录 + 让用户写一条自定义命令行。
LIVE_KEY = "live"


def live_cache():
    """本机的"测过没有"记录：{key: {ok, why, at, mode}}。"""
    d = _local_cfg().get(LIVE_KEY)
    return d if isinstance(d, dict) else {}


def set_live(key, ok, why="", mode="version"):
    """记下这条通道的实测结果（成功/失败都记，界面靠它区分"未验证"与"真的不行"）。"""
    cfg = _local_cfg()
    d = cfg.get(LIVE_KEY)
    if not isinstance(d, dict):
        d = {}
    d[key] = {"ok": bool(ok), "why": (why or "")[:300], "at": time.time(), "mode": mode}
    cfg[LIVE_KEY] = d
    try:
        os.makedirs(os.path.dirname(cfg_path()), exist_ok=True)
    except OSError:
        pass
    try:
        with open(cfg_path(), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return True
    except OSError:
        return False


def _run_quiet(cmd, timeout=25, cwd=None):
    """跑一条命令收输出（自检用；出错不抛，交调用方判）。"""
    env = child_env()
    env.update(runtime_env_for(cmd))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, env=env,
                           cwd=cwd or HERE, stdin=subprocess.DEVNULL)
        return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return -1, str(e)


def quick_check(key):
    """**免费**自检：跑这条 CLI 自己的版本/帮助命令，看它到底能不能启动。

    返回 (ok, why)：ok 为 True/False 表示测得出来；**None 表示这条通道没有合适的自检命令**
    （自定义通道、或 CLI 不认 `--version`）——这时不算它不行，只是"没验证"，让用户
    决定要不要点「真跑一句」（那个要花一点点额度）。
    为什么要有这道：只查文件在不在，会把"卸载残留 / npx 缓存"当成人能用的通道。
    """
    try:
        if key == "workbuddy":
            js = cli_js()
            runner, renv = lib_runner(js) if js else (None, None)
            if not (js and runner):
                return False, "WorkBuddy 的无头 CLI 找不到或跑不动"
            cmd = [runner, js, "--version"]
        elif key == "codex":
            exe = codex_exe()
            if not exe:
                return False, "没找到 codex 命令"
            cmd = [exe, "--version"]
        elif key == "claude":
            exe = claude_exe()
            if not exe:
                return False, "没找到 claude 命令"
            cmd = [exe, "--version"]
        elif key == "zcode":
            cli = zcode_cli()
            runner, renv = lib_runner(cli) if cli else (None, None)
            if not (cli and runner):
                return False, "ZCode 自带的 CLI 找不到或跑不动"
            cmd = [runner, cli, "--version"]
        elif key == "dsh":
            pkg = dsh_pkg()
            runner, renv = lib_runner(pkg) if pkg else (None, None)
            if not (pkg and runner):
                return False, "DSH 的脚本找不到或没有 node 跑它"
            cmd = [runner, pkg, "--version"]
        elif key == "doubaowork":
            js = doubao_js()
            runner, renv = lib_runner(js) if js else (None, None)
            if not (js and runner):
                return False, "豆包桥脚本找不到、或没有 node/Electron 跑它"
            rc, out = _run_quiet([runner, js, "status"], timeout=60)
            d = {}
            try:
                d = json.loads(out[out.index("{"):])
            except (ValueError, TypeError):
                d = {}
            if d.get("cdp"):
                return True, "豆包工作在跑，且能被接管（调试端口 %s）" % d.get("port")
            if d.get("running"):
                return False, ("豆包工作在跑，但**不是调试端口起的**——我连不上。先退出它，"
                               "或在命令行跑 `node tools\\doubao_cdp.mjs restart --yes` 让它带着端口重启")
            return False, "豆包工作没在跑：点「出提示词」时我会带调试端口把它拉起来"
        else:
            return None, "这条通道没有自检命令（自定义通道请点「真跑一句」）"
        rc, out = _run_quiet(cmd)
        low = out.lower()
        # 不认 --version 的 CLI：不算它不行，只是"没验证"
        for soft in ("unknown option", "unknown argument", "not recognized", "unrecognized",
                     "invalid option", "no such option", "usage:"):
            if soft in low and rc != 0:
                return None, "这条 CLI 不认自检命令（不算失败，只是没验证）"
        if rc == 0:
            return True, (out.splitlines()[0][:120] if out else "自检通过")
        return False, ("命令能启动但自检返回 %d：%s" % (rc, (out or "")[:120])
                       if rc > 0 else ("跑不起来：%s" % (out or "")[:120]))
    except Exception as e:                                       # noqa: BLE001
        return False, "自检出错：%s" % e


def test_agent(key, lite=True, timeout=180):
    """测一下这条通道到底能不能用。

    lite=True（默认）＝免费自检（`--version`）；lite=False＝**真跑一句最小任务**
    （会消耗一点点额度，但这是唯一能证明"接得上"的办法）。
    结果写进 live 段；下次开界面直接显示，不必重测。
    """
    if key not in {r["key"] for r in list_agents()}:
        return {"ok": False, "error": "未知的通道：%s" % key}
    if lite:
        ok, why = quick_check(key)
        if ok is None:
            return {"ok": False, "unsupported": True, "agent": key, "why": why,
                    "error": why}
        set_live(key, ok, why, "version")
        return {"ok": bool(ok), "agent": key, "lite": True, "why": why}
    r = ask("只回一个字：好。不要读任何文件、不要用任何工具。", agent=key,
            timeout=timeout, tools="")
    ok = bool(r.get("ok"))
    why = (r.get("text") or "").strip()[:60] or (r.get("error") or "")[:200]
    set_live(key, ok, why, "run")
    return {"ok": ok, "agent": key, "lite": False, "why": why}


# ── 不预设"支持哪些 agent"：扫盘发现 + 用户自定义 ─────────────────────────────
def custom_agents():
    """本机配置里用户自己写的通道：`agents: [{key,label,cmd:[…],cmd_mode}]`。

    为什么要有：opencode / Hermes / 下个月才出的某个 harness —— 代码里写不完，也不该写。
    用户（或帮他配的 agent）在 `agent_bridge.local.json` 里加一条命令行即可，不用改代码。
    `cmd` 里支持 `{prompt}` / `{cwd}` 两个占位符。
    """
    out = []
    for a in (_local_cfg().get("agents") or []):
        if isinstance(a, dict) and a.get("key") and a.get("cmd"):
            out.append(a)
    return out


def scan_hosts(max_depth=1):
    """扫盘：这台机器上还有哪些"agent 宿主"目录（**不预设名字**）。

    ZCode / Codex / DSH / WorkBuddy 的形状都是 `~/.<名字>/skills/`；opencode、Hermes
    之类多半也一样。这里只扫用户主目录的**一级／二级目录**里的 `skills`（很便宜），
    把不是内置适配器的那些报成"疑似通道"——界面提示用户可以给它们配一条命令行。
    """
    home = os.path.expanduser("~")
    found = {}
    try:
        names = os.listdir(home)
    except OSError:
        return []
    for n in names:
        p = os.path.join(home, n)
        if not os.path.isdir(p):
            continue
        for sub in ("skills", os.path.join("config", "skills")):
            q = os.path.join(p, sub)
            if os.path.isdir(q):
                found.setdefault(n.lstrip("."), q)
    out = []
    known = set(ADAPTERS) | {a["key"] for a in custom_agents()}
    for label, q in sorted(found.items()):
        if label in known or label.lower() in known:
            continue
        out.append({"key": "host:" + label, "label": label + "（疑似）", "found": True,
                    "ok": False, "weak": True, "host": False, "verified": False,
                    "why": "这台机器上有 %s，但工作台还不知道怎么叫它干活——"
                           "在 agent_bridge.local.json 里加一条 agents（key/label/cmd）就能用" % q})
    return out


def list_agents():
    """本机探测结果（**只报盘上真有的**，并区分"测过没有"）。

    每行：key / label / found（盘上有）/ ok（能用＝有且没被实测否掉）/
          verified（真跑过）/ weak（像是残留或缓存，别太当真）/ why / host / at。
    没找到的也会返回（found=False），界面把它们收进折叠块并给出装法。
    """
    hosts = host_agents()
    live = live_cache()
    rows = []
    for key, a in ADAPTERS.items():
        ok, why = a["probe"]()
        lv = live.get(key) or {}
        if lv and lv.get("ok") is False:
            rows.append({"key": key, "label": a["label"], "found": bool(ok), "ok": False,
                         "verified": False, "weak": False, "host": key in hosts,
                         "why": "实测不通：%s" % (lv.get("why") or "（没给原因）"),
                         "at": lv.get("at") or 0})
            continue
        rows.append({"key": key, "label": a["label"], "found": bool(ok), "ok": bool(ok),
                     "verified": bool(lv.get("ok")), "weak": bool(a.get("weak")),
                     "host": key in hosts, "why": why, "at": lv.get("at") or 0})
    for a in custom_agents():
        rows.append({"key": a["key"], "label": a.get("label") or a["key"], "found": True,
                     "ok": True, "verified": bool((live.get(a["key"]) or {}).get("ok")),
                     "weak": False, "host": False,
                     "why": "自定义通道" + ("（已测通）" if (live.get(a["key"]) or {}).get("ok") else "（未验证）")})
    rows += scan_hosts()
    return rows


def pick_agent(prefer=None):
    """选一个 agent 干活，返回 (key, why)。理由写清楚，界面直接显示给用户看。

    顺序（2026-09-16 起加"自动跟随"，用户要求"能不能根据我在用哪个软件自动切"）：
      1. 用户明确指过（界面选过 / 配置里写了 agent）→ 就用它，不猜（选了就尊重）；
      2. **谁的桌面客户端正开着** → 用它（"你在用哪个软件，我就用哪个"）；
      3. 把本 skill 装在自己名下且可用 → 用它；
      4. 本机任意一个可用的。
      2 是新的默认行为；想固定用某个，在界面③栏 agent 徽章里点一行选定即可（1 会盖过 2）。
    """
    cfg = _local_cfg()
    want = prefer or cfg.get("agent")
    rows = {r["key"]: r for r in list_agents()}
    if want and want in rows:
        r = rows[want]
        if r["ok"]:
            return want, ("按配置用 %s" % r["label"]) if prefer or cfg.get("agent")                 else r["label"]
        return None, "配置指定的 %s 不可用：%s" % (r["label"], r["why"])
    for key in running_desktop():                # ① 跟随你正开着的软件
        r = rows.get(key)
        if r and r["ok"]:
            return key, "%s（你正开着它，自动跟随）" % r["label"]
    # ② 其余按"越可信越优先"排队（2026-09-22 改）：
    #   已验证过的 > 只是找到的；本 skill 装在它名下的 > 没装的；非 weak（不是残留/npx 缓存）> weak。
    #   原来写死了 workbuddy→codex→zcode→dsh 的顺序，等于"预设了谁更好"，与"环境里有什么用什么"相悖。
    def _rank(k):
        r = rows[k]
        return (0 if r.get("verified") else 1,
                0 if r.get("host") else 1,
                1 if r.get("weak") else 0)
    for key in sorted([k for k, r in rows.items() if r.get("ok")], key=_rank):
        r = rows[key]
        if r.get("host"):
            return key, "%s（本 skill 就装在它名下%s）" % (
                r["label"], "，已验证" if r.get("verified") else "，还没验证过")
        return key, "%s（本机可用%s）" % (
            r["label"], "，已验证" if r.get("verified") else "，还没验证过；建议点一下「测一下」")
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
    # 用户自己加的通道（agents 段里的任意 agent：opencode / Hermes / …）
    for a in custom_agents():
        if a.get("key") == key:
            cmd = [str(x).replace("{prompt}", prompt).replace("{cwd}", cwd or "")
                   for x in a["cmd"]]
            return cmd, a.get("cmd_mode") or "text"
    if key == "zcode":
        cli = zcode_cli()
        if not cli:
            raise RuntimeError("ZCode CLI 不可用（没找到 resources/glm/zcode.cjs）")
        runner, _renv = lib_runner(cli)
        if not runner:
            raise RuntimeError("ZCode CLI 找到了，但没有 node、也没找到 ZCode 的 exe")
        cmd = [runner, cli, "--prompt", prompt, "--mode", "yolo"]
        if cwd:
            cmd += ["--cwd", cwd]
        if session_id:
            cmd += ["--resume", session_id]      # 钉住本项目自己的会话，不用 -c（不抢用户正在聊的）
        return cmd, "text"
    if key == "dsh":
        pkg = dsh_pkg()
        if not pkg:
            raise RuntimeError("DSH 不可用（没找到 @deepseek-ai/dsh）")
        runner, _renv = lib_runner(pkg)
        if not runner:
            raise RuntimeError("DSH 找到了，但没有 node 可跑（先装 Node.js）")
        cmd = [runner, pkg, "--profile", "headless", prompt]
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
    if key == "claude":
        exe = claude_exe()
        if not exe:
            raise RuntimeError("claude 不可用（没找到 Claude Code 的 CLI）")
        # 无头打印模式：-p 跑完就退；stream-json 才有逐行事件可喂进度（--verbose 是它的前置要求）
        cmd = [exe, "-p", prompt, "--output-format", "stream-json", "--verbose",
               "--permission-mode", "bypassPermissions"]
        if session_id:
            cmd += ["--resume", session_id]
        return cmd, "claude-json"
    if key == "doubaowork":
        # 豆包工作没有 CLI：走 CDP 桥（起客户端 → 灌 prompt → 从会话轨迹读答复）。
        # prompt 可能好几千字，**走临时文件**别塞 argv（Windows 命令行有长度上限，DSH 那条踩过引号被吃）。
        js = doubao_js()
        runner, _renv = lib_runner(js) if js else (None, None)
        if not (js and runner):
            return ["cmd", "/c", "echo 豆包桥不可用（缺 tools\\doubao_cdp.mjs 或 node）1>&2 & exit 9"], "text"
        import tempfile
        pf = os.path.join(tempfile.gettempdir(),
                          "heronbo_doubao_prompt_%d.txt" % int(time.time() * 1000))
        try:
            with open(pf, "w", encoding="utf-8", newline="\n") as f:
                f.write(prompt)
        except OSError as e:
            return ["cmd", "/c", "echo 写不了临时 prompt 文件：%s 1>&2 & exit 9" % e], "text"
        return [runner, js, "ask", "--prompt-file", pf], "text"
    return build_cmd(prompt, session_id=session_id, permission_mode=permission_mode or "acceptEdits",
                     tools=tools, output_format="stream-json", extra=extra), "workbuddy-json"


def _extract_claude_line(line):
    """Claude Code `--output-format stream-json` 的一行 → (可读文本, session_id, 最终答复, 报错)。

    官方形状（照文档写的，**本机未实测**）：
      {"type":"system","subtype":"init","session_id":"…"}
      {"type":"assistant","message":{"content":[{"type":"text","text":"…"}]}}
      {"type":"result","subtype":"success","result":"<最终答复>","session_id":"…","is_error":false}
    与 WorkBuddy 那套很像，所以复用它那两个工具函数；差别：Claude 把最终答复放在 result 字段、
    会话 id 每行都可能带，取最新那个。
    """
    try:
        obj = json.loads(line)
    except ValueError:
        return line.strip()[:400], None, None, None
    if not isinstance(obj, dict):
        return "", None, None, None
    sid = obj.get("session_id") or obj.get("sessionId") or None
    final, err = None, None
    if obj.get("type") == "result":
        r = obj.get("result")
        if isinstance(r, str) and r.strip():
            final = r.strip()
        err = bool(obj.get("is_error")) or (obj.get("subtype") not in (None, "success"))
    txt = _pick_text(obj)
    return (txt or ""), sid, final, err


def _extract_stream_line(line):
    """WorkBuddy `--output-format stream-json` 的一行事件 → (可读文本, session_id, 最终答复, 是否报错)。

    实测（2026-09-16）每行一个 JSON，常见几种：
      {"type":"system","subtype":"init","session_id":"…"}
      {"type":"assistant","message":{"content":[{"type":"text","text":"…"},
                                               {"type":"tool_use","name":"Read","input":{…}}]}}
      {"type":"result","subtype":"success","result":"…","session_id":"…","is_error":false}

    **为什么非要换成 stream-json**：原来用 `--output-format json`（结束时吐一整块），
    2026-09-16 实测一次真跑——190 秒里进度一直是 0%（没有中间事件可喂），最后直接跳 100%；
    而且输出一长就被 tail 截断，连最终答复都抓成了 `"prompt_cache_hit_tokens": 119040`
    这种 token 统计，还被写进了回执。换成 stream-json 后每行都能喂进度，最终答复从
    type=result 那一行取，不会被截断影响。
    """
    try:
        obj = json.loads(line)
    except ValueError:
        return line.strip()[:400], None, None, None
    if not isinstance(obj, dict):
        return "", None, None, None
    sid = obj.get("session_id") or obj.get("sessionId") or None
    final = None
    err = None
    if obj.get("type") == "result":
        r = obj.get("result")
        if isinstance(r, str) and r.strip():
            final = r.strip()
        err = bool(obj.get("is_error"))
    txt = _pick_text(obj)
    if not txt:
        msg = obj.get("message")
        if isinstance(msg, dict):
            c = msg.get("content")
            if isinstance(c, list):
                bits = []
                for b in c:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text" and b.get("text"):
                        bits.append(str(b["text"]))
                    elif b.get("type") == "tool_use":
                        bits.append("· 用 %s %s" % (
                            b.get("name") or "工具",
                            json.dumps(b.get("input") or {}, ensure_ascii=False)[:160]))
                txt = " ".join(bits)
            elif isinstance(c, str):
                txt = c
        elif isinstance(msg, str):
            txt = msg
    return " ".join(str(txt).split())[:400], sid, final, err


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
        permission_mode="acceptEdits", tools=None, on_line=None, on_raw=None, extra=None,
        agent=None):
    """叫 agent 干一件事（**边跑边回调每一行**，界面靠它显示实时进度）。

    返回 dict: {ok, agent, session_id, text, res, returncode, cmd, error, seconds}
    - 输出**真流式**：Popen 逐行读，`on_line(text)` 立刻拿到（原先用 subprocess.run，
      行要等进程结束才一起回调，进度条会"卡在 0% 然后跳到 96%"——2026-09-15 修）。
    - on_line 里改 UI 要小心：它跑在后台线程，GUI 侧要用 after/SSE 转一手。
    - **on_raw(line)**：给的是 stdout 的**原文**（`--output-format stream-json` 时一行一个
      结构化事件，含 thinking / tool_use）。on_line 拿的是**已渲染成可读文本**的版本，
      思考内容在那一步就丢了；工作台的「动作流」要显示思考与工具调用，所以要走 on_raw
      （2026-09-21 用户：工作台上要能返回 agent 返回的这些信息）。
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
    # 拿某个应用的 Electron 当 node 时，必须带这个开关，否则它会去开窗口而不是跑脚本
    env.update(runtime_env_for(cmd))
    snap_before = zcode_sessions() if key == "zcode" else {}
    snap_t0 = time.time()
    text_parts, tail = [], []            # tail 只留给"最后兜底当答复"，别当解析用的全文
    stream_sid, stream_text, stream_err = None, "", None
    try:
        p = subprocess.Popen(cmd, cwd=cwd or HERE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                             encoding="utf-8", errors="replace", env=env,
                             bufsize=1, **no_window_kwargs())
    except OSError as e:
        return {"ok": False, "error": "起不来：%s" % e, "session_id": session_id,
                "text": "", "cmd": cmd, "agent": key}
    _CURRENT["proc"] = p
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
            if on_raw and mode != "text":    # 原文一行（stream-json）→ 工作台的动作流
                try:
                    on_raw(line)
                except Exception:                                # noqa: BLE001
                    pass
            if len(tail) > 3000:          # 留宽点：自定义 cmd 用单块 json 时，截断会让解析失败
                del tail[:-3000]
            if mode == "codex-json":
                txt, final = _extract_codex_line(line)
                if final:
                    text_parts.append(final)
                if on_line and txt:
                    on_line(txt)
            elif mode == "claude-json":
                txt, sid_, fin_, err_ = _extract_claude_line(line)
                if sid_:
                    stream_sid = sid_
                if fin_:
                    stream_text = fin_
                if err_ is not None:
                    stream_err = err_
                if txt and on_line:
                    on_line(txt)
            else:
                txt, sid_, fin_, err_ = _extract_stream_line(line)
                if sid_:
                    stream_sid = sid_
                if fin_:
                    stream_text = fin_
                if err_ is not None:
                    stream_err = err_
                if txt and on_line:
                    on_line(txt)
        p.wait(timeout=20)
    except Exception:                                            # noqa: BLE001
        pass
    finally:
        timer.cancel()
        _CURRENT["proc"] = None
        err = chr(10).join(err_lines)
    rc = p.returncode
    raw = chr(10).join(tail)
    if mode == "text":                     # DSH/ZCode：stdout 就是最终答复
        text = raw.strip()
        ok = (rc == 0) and bool(text)
        sid_out = session_id
        if key == "zcode":
            # 认一下这次的会话（续跑时不变；新开时就是新 id）→ 下次 --resume 回到同一对话，省一次重读
            sid_out = session_id or zcode_session_from_snapshot(snap_before, snap_t0)
        return {"ok": ok, "agent": key, "agent_label": ADAPTERS[key]["label"],
                "session_id": sid_out, "text": text, "cmd": cmd, "returncode": rc,
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
    sid = stream_sid or session_id
    text = ""
    if res:                                    # 老路子：--output-format json 一整块（还认它，兼容自定义 cmd）
        sid = res.get("session_id") or res.get("sessionId") or sid
        text = res.get("result") or _pick_text(res) or ""
        if isinstance(text, list):
            text = "".join(seg.get("text", "") if isinstance(seg, dict) else str(seg)
                           for seg in text)
    if not text:
        text = stream_text or ""               # stream-json：从 type=result 那一行取（完整、不截断）
    if not text:
        text = raw.strip()
    is_err = bool((res and res.get("is_error")) or stream_err)
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
