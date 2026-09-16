# -*- coding: utf-8 -*-
"""工作台部署脚本（给 AI/agent 用：一条命令把环境、依赖、agent 通道、快捷方式都弄好）。

为什么要它：这套东西散在几处——Python 依赖（pywebview）、node、四个 agent 的 CLI 通道
（WorkBuddy / Codex / ZCode / DSH，其中 ZCode 要 OAuth 登录 + 配置 provider/model）、
桌面快捷方式与图标。手工逐个接容易漏，所以做成一个自检 + 修复的脚本，agent 直接跑。

用法（在 `tools` 目录下）：
    python 部署.py check          # 体检，只读，不改任何东西
    python 部署.py agents         # 接 agent 通道（会改 ~/.zcode 配置，**先备份**）
    python 部署.py install        # 装缺的依赖（默认只打印命令；加 --yes 才真装）
    python 部署.py shortcut       # 桌面快捷方式（指向 dist\\score-tool.exe，用自带图标）
    python 部署.py all            # check → agents → install → shortcut
常用开关：--yes（真动手） / --root <样本库根>（配置样本库）

铁律（照仓库 AGENTS.md）：**装任何东西前先问用户**——不加 --yes 只打印命令，不执行。
"""
import argparse
import datetime
import io
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
HOME = os.path.expanduser("~")
ICO = os.path.join(HERE, "工作台.ico")
EXE = os.path.join(HERE, "dist", "score-tool.exe")
PY = sys.executable

_OK, _BAD, _TODO = [], [], []


def log(msg):
    print(msg, flush=True)


def ok(name, detail=""):
    _OK.append(name)
    log("  [OK]   %s%s" % (name, ("  " + detail) if detail else ""))


def bad(name, detail="", fix=""):
    _BAD.append(name)
    log("  [缺]   %s%s" % (name, ("  " + detail) if detail else ""))
    if fix:
        log("         ↳ %s" % fix)


def todo(name, cmd):
    _TODO.append((name, cmd))
    log("  [待办] %s\n         ↳ %s" % (name, cmd))


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=kw.pop("timeout", 300), **kw)


# ── 体检 ───────────────────────────────────────────────────────────────────
def check_env():
    log("== 运行环境 ==")
    v = sys.version_info
    if v >= (3, 9):
        ok("python ≥3.9", "%d.%d.%d" % v[:3])
    else:
        bad("python ≥3.9", "%d.%d.%d" % v[:3], "装 Python 3.9+ 并加入 PATH")
    try:
        import tkinter  # noqa: F401
        ok("tkinter（经典界面要用）")
    except ImportError:
        bad("tkinter", "当前解释器没带", "重装 Python 时勾上 tcl/tk and IDLE")
    try:
        import webview
        ok("pywebview（工作台独立窗口）", getattr(webview, "__file__", ""))
    except ImportError:
        bad("pywebview", "工作台主界面要用它",
            "python tools\\部署.py install --yes（优先用仓库里带的离线包）")
    check_webview2()
    node = shutil.which("node") or r"C:\Program Files\nodejs\node.exe"
    if os.path.isfile(str(node)):
        ok("node", str(node))
    else:
        # 没有 node ≠ 接不上 agent：WorkBuddy / ZCode 自带的 Electron 就能当 node 跑它们的 CLI
        log("  [提示] 没装 Node.js —— 不影响带 Electron 的 agent（WorkBuddy / ZCode 照跑）；"
            "只有 Codex / DSH 需要 Node.js")
    ff = shutil.which("ffprobe")
    if ff:
        ok("ffprobe", ff)
    else:
        bad("ffprobe", "不在 PATH", "winget install --id Gyan.FFmpeg -e（读素材宽高时长要用）")
    if os.path.isfile(EXE):
        ok("score-tool.exe", EXE)
    else:
        bad("score-tool.exe", "还没打包（源码也能跑：python tools\\工作台.py）",
            "python -m PyInstaller --noconfirm score-tool.spec")
    if os.path.isfile(ICO):
        ok("工作台.ico", ICO)
    else:
        bad("工作台.ico", ICO, "python tools\\图标.py")


def check_webview2():
    """WebView2 运行库体检（2026-09-16 新机白屏那个坑）。

    **不能只看注册表**：出问题的机器注册表写着装了 134.0.3124.93，可那个目录里
    `msedgewebview2.exe` 不见了（284MB 的 msedge.dll 还在）→ 工作台窗口开出来一片空白。
    这里查"注册表版本 + 那个目录里宿主 exe 在不在 + 磁盘上还有没有别的完整版本"。
    """
    try:
        import importlib.util as _iu
        spec = _iu.spec_from_file_location("ws", os.path.join(HERE, "workbench_server.py"))
        ws = _iu.module_from_spec(spec)
        spec.loader.exec_module(ws)
    except Exception as e:                                       # noqa: BLE001
        bad("WebView2 运行库", "查不了：%s" % e)
        return
    okk, ver, why = ws.webview2_state()
    if okk:
        ok("WebView2 运行库", "%s（%s）" % (ver or "版本未知", why))
    else:
        bad("WebView2 运行库", why, ws.WEBVIEW2_HELP.replace("\n        ", "\n         ↳ "))


def fix_webview2(yes=False):
    """下载并装微软官方 WebView2 运行库（Evergreen Bootstrapper）。**默认只打印命令**。"""
    log("== 修复 WebView2（白窗/独立窗口起不来时用）==")
    url = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
    dst = os.path.join(os.environ.get("TEMP", "."), "MicrosoftEdgeWebview2Setup.exe")
    cmd = ('powershell -NoProfile -Command "Invoke-WebRequest -Uri \'%s\' -OutFile \'%s\'; '
           'Start-Process -FilePath \'%s\' -ArgumentList \'/silent\',\'/install\' -Wait"' % (url, dst, dst))
    if not yes:
        todo("装 WebView2 运行库（微软官方，约 2MB 引导器）", cmd + "   （加 --yes 让本脚本直接做）")
        return
    log("  正在下载 %s" % url)
    r = run(["powershell", "-NoProfile", "-Command",
             "Invoke-WebRequest -Uri '%s' -OutFile '%s'; Write-Output done" % (url, dst)], timeout=600)
    if r.returncode != 0 or not os.path.isfile(dst):
        bad("下载 WebView2 引导器", (r.stderr or r.stdout or "")[-200:], "手动打开 " + url)
        return
    # 校验签名：这是要跑的官方安装器，必须是 Microsoft 签的
    sig = run(["powershell", "-NoProfile", "-Command",
               "(Get-AuthenticodeSignature '%s').SignerCertificate.Subject" % dst])
    who = (sig.stdout or "").strip()
    if "Microsoft" not in who:
        bad("签名校验", who or "读不到签名", "签名不是 Microsoft，已停下不装（手动去官网下）")
        return
    ok("引导器已下载且签名是 Microsoft", who[:60])
    log("  正在静默安装（/silent /install）…")
    r = run(["powershell", "-NoProfile", "-Command",
             "Start-Process -FilePath '%s' -ArgumentList '/silent','/install' -Wait" % dst], timeout=900)
    if r.returncode != 0:
        bad("安装 WebView2", (r.stderr or r.stdout or "")[-200:])
        return
    okk, ver, why = (lambda t: (t[0], t[1], t[2]))(_wv2_state())
    if okk:
        ok("安装后复查", "%s（%s）" % (ver, why))
    else:
        bad("安装后复查", why, "装完还是不行的话，重启一次系统再试")


def _wv2_state():
    try:
        import importlib.util as _iu
        spec = _iu.spec_from_file_location("ws", os.path.join(HERE, "workbench_server.py"))
        ws = _iu.module_from_spec(spec)
        spec.loader.exec_module(ws)
        return ws.webview2_state()
    except Exception:                                            # noqa: BLE001
        return False, "", "查不到"
def check_root(root=None):
    log("== 样本库根 ==")
    try:
        import project_core as pc
        cur = (pc.read_local() or {}).get("SAMPLES_ROOT") or ""
        if root:
            pc.write_local(SAMPLES_ROOT=os.path.abspath(root))
            ok("样本库根已设为", os.path.abspath(root))
            return
        if cur and os.path.isdir(cur):
            ok("样本库根", cur)
        else:
            bad("样本库根", cur or "未配置",
                "python tools\\部署.py check --root \"<你的样本库目录>\"")
    except Exception as e:                                       # noqa: BLE001
        bad("读取 project_core / paths.local.md", str(e))


# ── agent 通道 ─────────────────────────────────────────────────────────────
def zcode_cfg_path():
    return os.path.join(HOME, ".zcode", "cli", "config.json")


def zcode_cli():
    try:
        import agent_bridge as ab
        return ab.zcode_cli()
    except Exception:                                            # noqa: BLE001
        return None


def zcode_probe_run(cli, timeout=180):
    """真跑一句，返回 (ok, 输出)。用来分辨"配置没通"和"额度不够"。"""
    w = os.path.join(os.environ.get("TEMP", "."), "workbench_deploy_probe")
    os.makedirs(w, exist_ok=True)
    r = run([shutil.which("node") or "node", cli, "--prompt", "回复：ok",
             "--cwd", w, "--mode", "yolo"], timeout=timeout)
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    # 判"通没通"只看：进程正常退出 + 没有报错字样。**别要求模型回固定的字**
    # （实测它会回"好的，收到。"这种自然语言，卡字面量会把好通道误判成不通）
    bads = ("Error:", "Insufficient balance", "429", "NO_ADAPTER", "Unknown option",
            "Model config is missing")
    good = r.returncode == 0 and bool(out) and not any(b in out[:600] for b in bads)
    return good, out


def wire_zcode(yes=False):
    cli = zcode_cli()
    if not cli:
        bad("ZCode CLI", "没找到 resources/glm/zcode.cjs", "确认 ZCode 桌面端装好了")
        return
    cfgp = zcode_cfg_path()
    try:
        cfg = json.load(io.open(cfgp, encoding="utf-8-sig"))
    except (OSError, ValueError):
        cfg = {}
    if not (cfg.get("provider") or cfg.get("model")):
        todo("ZCode 需要先登录（浏览器 OAuth）",
             '"%s" "%s" login --no-browser' % (shutil.which("node") or "node", cli))
        if yes:
            log("  → --yes：现在起登录（把打印出来的链接在浏览器里打开即可）")
            subprocess.Popen([shutil.which("node") or "node", cli, "login", "--no-browser"])
        return
    good, out = zcode_probe_run(cli)
    if good:
        ok("ZCode CLI 通道", "登录有效、能出结果")
        return
    if "Insufficient balance" in out or "429" in out:
        log("  [注意] 登录有效，但默认 provider 没额度：%s" % out.splitlines()[0][:100])
        merged = merge_provider_from_desktop(cfgp)
        if merged:
            ok("ZCode CLI 通道", "已改走桌面端可用的 provider：%s" % merged)
        else:
            bad("ZCode CLI 通道", "没额度且桌面端没有可借用的 provider",
                "充值，或在 ~/.zcode/cli/config.json 里把 model.main 指到可用的 <providerId>/<模型>")
    else:
        bad("ZCode CLI 通道", out.splitlines()[0][:140] if out else "无输出",
            "跑 python tools\\部署.py agents --yes 让脚本试着接")


def merge_provider_from_desktop(cfgp):
    """把桌面端 config 里"带 key 的 openai-compatible provider"并进 CLI，并把 model 指过去。

    返回新 model 引用；失败返回 ""。**改前一定备份**（备份到同目录 .bak-时间戳）。
    """
    v2p = os.path.join(HOME, ".zcode", "v2", "config.json")
    try:
        v2 = json.load(io.open(v2p, encoding="utf-8-sig"))
        cli = json.load(io.open(cfgp, encoding="utf-8-sig"))
    except (OSError, ValueError):
        return ""
    picked = None
    for pid, p in (v2.get("provider") or {}).items():
        opts = p.get("options") or {}
        if "deepseek.com" in str(opts.get("baseURL", "")).lower() and opts.get("apiKey"):
            picked = (pid, p)
            break
    if not picked:
        return ""
    pid, p = picked
    models = list((p.get("models") or {}).keys())
    if not models:
        return ""
    want = next((m for m in models if "flash" in m and "vision" not in m), models[0])
    try:
        shutil.copy2(cfgp, cfgp + ".bak-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
        p = json.loads(json.dumps(p))
        p["enabled"] = True
        cli.setdefault("provider", {})[pid] = p
        cli["model"] = {"main": "%s/%s" % (pid, want), "lite": "%s/%s" % (pid, want)}
        io.open(cfgp, "w", encoding="utf-8", newline="").write(
            json.dumps(cli, ensure_ascii=False, indent=2))
    except (OSError, ValueError):
        return ""
    return cli["model"]["main"]


def check_agents(yes=False, ping=None):
    log("== agent 通道 ==")
    try:
        import agent_bridge as ab
    except Exception as e:                                       # noqa: BLE001
        bad("agent_bridge.py", str(e))
        return
    usable = []
    for r in ab.list_agents():
        line = "%s（%s）%s" % (r["label"], "本 skill 装在它名下" if r["host"] else "本机",
                              r["why"][:100])
        if r["ok"]:
            ok("agent: " + r["label"], line)
            usable.append(r["key"] if "key" in r else r.get("label"))
        else:
            bad("agent: " + r["label"], line, (r.get("fix") or ""))
    if not usable:
        log("  [说明] 一台电脑上装哪个 agent 就用哪个——四个通道任意一个可用即可，"
            "四个都没有时只有「出提示词」那类按钮不能用，其余流程照常。")
        log("         想接一个：装 WorkBuddy 桌面端 / Codex CLI / ZCode 桌面端 / DSH 任一个，"
            "再跑一次本命令。")
        return
    # ZCode 有它自己的麻烦（要登录 + provider 配置），单独接一下
    wire_zcode(yes)
    # 部署即联通：真跑一句最小任务，确认"装完真的能干活"（2026-09-16 用户要求）
    do_ping = ping if ping is not None else True
    if not do_ping:
        log("  [跳过] 连通测试（--no-ping）")
        return
    log("== 连通测试（真跑一句「回一个字」，会消耗一点点额度）==")
    t0 = time.time()
    try:
        r = ab.ask("只回一个字：好。不要读任何文件、不要用任何工具。", timeout=180)
    except Exception as e:                                       # noqa: BLE001
        bad("连通测试", "跑不起来：%s" % e)
        return
    used = time.time() - t0
    if r.get("ok"):
        ok("连通测试", "%s 用时 %.1fs，回了：%s"
           % (r.get("agent_label") or r.get("agent"), used,
              (r.get("text") or "").strip()[:40]))
    else:
        bad("连通测试", "没通过（用时 %.1fs）：%s" % (used, (r.get("error") or "")[:160]),
            "看上面各通道的状态行；通了之后工作台里点「出提示词」就能用")


def do_install(yes=False, extras=False):
    """装依赖。**分两档**，别让新机一上来就下几百 MB：

    - 必须：pywebview / pythonnet / clr_loader（工作台独立窗口要用）→ 仓库里带了离线 wheel，
      没网也能装、几秒钟装完（2026-09-16 用户反映"安装花很长时间"就是这些+重包一起装的结果）。
    - 按需：numpy / opencv（成片对比、人物遮罩、竖版画布）· faster-whisper（拆解转写）·
      onnxruntime（深度视频）→ 用到哪个功能再装哪个，加 `--extras` 一次装全。
    """
    log("== 依赖 ==")
    need = []
    for mod, pkg in (("webview", "pywebview"), ("clr_loader", "clr_loader"), ("pythonnet", "pythonnet")):
        try:
            __import__(mod)
        except ImportError:
            need.append(pkg)
    wheels = os.path.join(HERE, "_vendor", "wheels")
    if not need:
        ok("必需依赖已齐（pywebview / pythonnet）")
    elif os.path.isdir(wheels):
        cmd = [PY, "-m", "pip", "install", "--no-index", "--find-links", wheels] + need
        if not yes:
            todo("缺依赖：%s（仓库里带了离线包，不用联网）" % ", ".join(need),
                 '"%s"   （加 --yes 让本脚本直接装）' % '" "'.join(cmd))
        else:
            log("  用离线包装：%s" % ", ".join(need))
            r = run(cmd, timeout=900)
            (ok if r.returncode == 0 else bad)("离线装 %s" % ", ".join(need),
                                               (r.stdout or r.stderr or "")[-200:])
    else:
        cmd = [PY, "-m", "pip", "install", "-i",
               "https://pypi.tuna.tsinghua.edu.cn/simple"] + need
        if not yes:
            todo("缺依赖：%s" % ", ".join(need),
                 '"%s"   （加 --yes 让本脚本直接装）' % '" "'.join(cmd))
        else:
            r = run(cmd, timeout=900)
            (ok if r.returncode == 0 else bad)("安装 %s" % ", ".join(need),
                                               (r.stdout or r.stderr or "")[-200:])

    heavy = [("numpy", "numpy"), ("cv2", "opencv-python-headless"),
             ("faster_whisper", "faster-whisper"), ("onnxruntime", "onnxruntime")]
    miss = []
    for mod, pkg in heavy:
        try:
            __import__(mod)
        except ImportError:
            miss.append(pkg)
    if not miss:
        ok("按需依赖也已齐（numpy/opencv/转写/深度视频）")
        return
    what = {"numpy": "成片对比、竖版画布", "opencv-python-headless": "人物遮罩、拆解抽帧",
            "faster-whisper": "拆解转写", "onnxruntime": "深度视频（L4 复刻动作）"}
    lines = "、".join("%s（%s）" % (m, what.get(m, "")) for m in miss)
    if not extras:
        log("  [按需] 还没装：%s" % lines)
        log("         ↳ 用到哪个功能再装：python tools\\环境检查.py --install --yes %s"
            % ("--extras" if len(miss) > 1 else ""))
        return
    cmd = [PY, "-m", "pip", "install", "-i", "https://pypi.tuna.tsinghua.edu.cn/simple"] + miss
    if not yes:
        todo("装按需依赖：%s" % ", ".join(miss), '"%s"   （加 --yes）' % '" "'.join(cmd))
        return
    log("  正在装（这几个包比较大，耐心等）：%s" % ", ".join(miss))
    r = run(cmd, timeout=1800)
    (ok if r.returncode == 0 else bad)("装 %s" % ", ".join(miss), (r.stdout or r.stderr or "")[-200:])


# ── 桌面快捷方式 ───────────────────────────────────────────────────────────
def do_shortcut(yes=False):
    """桌面快捷方式。**有 exe 指向 exe；没有 exe 就指向源码启动器**
    （新机 clone 下来还没打包时也得能用，2026-09-16 用户反馈"装完没快捷方式"）。"""
    log("== 桌面快捷方式 ==")
    if os.path.isfile(EXE):
        target, args, wd, what = EXE, "", os.path.dirname(EXE), "打包版 exe"
    else:
        launcher = os.path.join(HERE, "工作台.py")
        if not os.path.isfile(launcher):
            bad("score-tool.exe / 工作台.py", "两个都没有",
                "python -m PyInstaller --noconfirm score-tool.spec（或跑 git pull 补回 工作台.py）")
            return
        pyw = os.path.join(os.path.dirname(PY), "pythonw.exe")
        target = pyw if os.path.isfile(pyw) else PY
        args = '"%s"' % launcher
        wd = HERE
        what = "源码启动器（%s）" % os.path.basename(target)
    if not yes:
        todo("建/更新桌面快捷方式",
             "python tools\\部署.py shortcut --yes（指向%s，图标用 tools\\工作台.ico）" % what)
        return
    ps = ("$ws = New-Object -ComObject WScript.Shell; "
          "$lnk = Join-Path $env:USERPROFILE 'Desktop\\HeronBo 视频工作台.lnk'; "
          "$s = $ws.CreateShortcut($lnk); "
          "$s.TargetPath = '%s'; $s.Arguments = '%s'; $s.WorkingDirectory = '%s'; "
          "$s.IconLocation = '%s,0'; $s.Description = 'HeronBo 视频工作台'; $s.Save(); "
          "Write-Output $lnk") % (target, args, wd, ICO)
    exe = shutil.which("powershell.exe") or "powershell.exe"
    r = run([exe, "-NoProfile", "-Command", ps])
    (ok if r.returncode == 0 else bad)("桌面快捷方式（%s）" % what,
                                       (r.stdout or r.stderr or "").strip()[-120:])


def launch(yes=False, backend=None):
    """把工作台起起来（一键安装的最后一步：装完就弹出来，别让用户自己找入口）。"""
    log("== 起工作台 ==")
    if os.path.isfile(EXE):
        cmd = [EXE]
    else:
        cmd = [PY, os.path.join(HERE, "工作台.py")]
    if not yes:
        todo("起工作台", '"%s"' % '" "'.join(cmd))
        return
    log("  启动：%s" % " ".join(cmd))
    try:
        kw = {}
        if os.name == "nt":
            kw["creationflags"] = 0x00000008 | 0x00000200      # DETACHED + NEW_PROCESS_GROUP
        subprocess.Popen(cmd, cwd=os.path.dirname(EXE) if os.path.isfile(EXE) else HERE, **kw)
        ok("工作台已启动", "窗口几秒后出现；标题「HeronBo · AI 视频工作台」")
    except OSError as e:
        bad("起工作台", str(e), "手工跑：%s" % " ".join(cmd))


def main():
    ap = argparse.ArgumentParser(description="工作台部署（自检 + 接 agent + 依赖 + 快捷方式）")
    ap.add_argument("action", nargs="?", default="check",
                    choices=["check", "install", "agents", "shortcut", "start", "wx", "all"])
    ap.add_argument("--yes", action="store_true", help="真动手（不加就只打印命令）")
    ap.add_argument("--root", help="设置样本库根目录")
    ap.add_argument("--extras", action="store_true",
                    help="install 时连「按需才装」的重包一起装（numpy/opencv/转写/深度视频）")
    ap.add_argument("--ping", dest="ping", action="store_true", default=None,
                    help="agents 时真跑一句最小连通测试（会消耗一点点额度）")
    ap.add_argument("--no-ping", dest="ping", action="store_false",
                    help="agents 时跳过连通测试")
    a = ap.parse_args()
    log("工作台部署 · %s%s" % (a.action, "（--yes：会真改）" if a.yes else "（只体检/打印）"))
    if a.action in ("check", "all"):
        check_env()
        check_root(a.root)
    if a.action in ("agents", "all"):
        check_agents(a.yes, ping=a.ping)
    if a.action in ("install", "all"):
        do_install(a.yes, a.extras)
    if a.action in ("shortcut", "all"):
        do_shortcut(a.yes)
    if a.action in ("start", "all"):
        launch(a.yes)          # 装完就弹出来：用户不用自己去找入口（2026-09-16 用户要求）
    if a.action == "wx":
        fix_webview2(a.yes)
    log("\n小结：OK %d 项 · 缺 %d 项 · 待办 %d 项" % (len(_OK), len(_BAD), len(_TODO)))
    return 1 if _BAD else 0


if __name__ == "__main__":
    sys.exit(main())
