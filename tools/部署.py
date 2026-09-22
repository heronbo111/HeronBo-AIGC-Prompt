# -*- coding: utf-8 -*-
"""工作台部署脚本（给 AI/agent 用：一条命令把环境、依赖、agent 通道、快捷方式都弄好）。

为什么要它：这套东西散在几处——Python 依赖（pywebview）、node、四个 agent 的 CLI 通道
（WorkBuddy / Claude Code / Codex / ZCode / DSH，其中 ZCode 要 OAuth 登录 + 配置 provider/model）、
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

# 下载与"用到才装"的能力包（2026-09-22）：两个新模块把这两件事从本文件里拆了出去——
#   · 下载器.py：Range 续传 / 同源重试 / sha256 校验（原来这里是 open(dst,"wb")，断一次全白下）
#   · 能力包.py：默认只装 core（wheels + ffmpeg + exe），转写/图像/深度按需拉
# 本文件里与它们对接的地方都写了「2026-09-22」注释，改动尽量小（另一个 agent 同时在改本文件）。
try:
    import 下载器 as DL
except Exception:                                                # noqa: BLE001
    DL = None
try:
    import 能力包 as KP
except Exception:                                                # noqa: BLE001
    KP = None

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
    elif os.path.isfile(os.path.join(FF_BIN, "ffprobe.exe")):
        ok("ffprobe", os.path.join(FF_BIN, "ffprobe.exe") + "（仓库自带）")
    else:
        bad("ffprobe", "不在 PATH、仓库自带的也没解开",
            "python tools/deploy.py install（解开仓库自带那份）；或 winget install --id Gyan.FFmpeg -e")
    vendored_model()
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
        # 没有工作台源码的机器（公开用户只有 exe）跳过这项即可：
        # 工作台启动时会自己检测 WebView2，坏了自动回退 Edge 窗口，不会白屏。
        ok("WebView2 运行库", "跳过自检（本机没有工作台源码）：%s" % e)
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
        log("         想接一个：装 WorkBuddy / ZCode 桌面端（免装 Node）、或 Claude Code / Codex CLI / DSH 任一个，"
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


# ── 仓库自带的"离线资产"（2026-09-17 用户要求：下一台机器 = 装完就有全部环境）──────
VENDOR = os.path.join(HERE, "_vendor")
FF_ZIP = os.path.join(VENDOR, "ffmpeg", "ffmpeg-win64-gpl-shared.zip")
FF_BIN = os.path.join(VENDOR, "ffmpeg", "bin")
HEAVY_WHEELS = os.path.join(VENDOR, "wheels-heavy")
CORE_WHEELS = os.path.join(VENDOR, "wheels")
MODEL_DIR = os.path.join(VENDOR, "models", "depth-anything-v2-small")
# 大件**不进仓库归档**（归档只放代码，约 24MB，SkillHub 之类平台导得进来）→ 想要离线全套时
# 从 Release 拉。**2026-09-22 换到新 tag**：这一版的附件重新拆过（core + 三个能力包，
# 默认安装 304MB → 约 94MB），清单在 tools\能力包.py 里。老 tag（vendor-2026-09-17）的附件
# 一个字都没动 → 别人机器上那份旧脚本照旧能装。
VENDOR_REL = os.environ.get("HERONBO_VENDOR_REL") or     "https://github.com/heronbo111/HeronBo-AIGC-Prompt/releases/download/vendor-2026-09-22/"
# 国内裸连 GitHub Release 很慢（2026-09-22 用户："别的电脑装的时候太慢"）→ 依次试：
# 直连 → 镜像前缀。镜像前缀要拼**完整 URL**（https://gh-proxy.com/https://github.com/…）。
# 一直都能直连的机器不用管；被卡住时脚本会自己换源，也可以用环境变量 HERONBO_VENDOR_REL 指定。
VENDOR_MIRRORS = ["", "https://gh-proxy.com/", "https://ghproxy.net/"]
# (附件名, 目标, MB)；目标是 _vendor 下的子目录名，或 "dist" 表示工作台 exe（落到 tools/dist/）
# ⚠️ 2026-09-22 改了拆分：默认安装只拉 core（wheels-core + ffmpeg），转写/图像/深度变成**能力包**，
# 用到才拉（用户原话："我最想解决的就是安装慢的核心问题"——默认安装 304MB → 约 94MB，
# 机器上已有能力够的系统 ffmpeg 时只剩约 22MB）。清单的唯一真相源是 tools\能力包.py，
# 下面这份只是**老布局的兼容兜底**（别人机器上的旧 tag 还在用这些名字）。
VENDOR_ASSETS = [("wheels-core.zip", "wheels", 4),
                 ("ffmpeg-win64-gpl-shared.zip", "ffmpeg", 76),
                 ("score-tool.exe", "dist", 18)]
VENDOR_ASSETS_OLD = [("wheels-heavy.zip", "wheels-heavy", 116),
                     ("ffmpeg-win64-gpl-shared.zip", "ffmpeg", 82),
                     ("depth-model.zip", "models", 88),
                     ("score-tool.exe", "dist", 18)]
# 工作台 exe 是**每次改代码都会变**的那一个：别只看"在不在"（老机器上它一直在，但它是旧版）
# → 记一份版本戳（大小+下载时间）在 exe 旁边，下次 fetch 时跟远端 HEAD 比一比，变了就换（2026-09-17）。
EXE_DST = os.path.join(HERE, "dist", "score-tool.exe")
EXE_MARK = os.path.join(HERE, "dist", "score-tool.version.json")


def _remote_head(url):
    """远端附件的大小与 Last-Modified（匿名 HEAD 可读；GitHub 会 302 到对象存储）。"""
    import urllib.request
    req = urllib.request.Request(url, method="HEAD",
                                 headers={"User-Agent": "heronbo-deploy",
                                          "Accept": "application/octet-stream"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return (int(r.headers.get("Content-Length") or 0), r.headers.get("Last-Modified") or "")


def exe_refresh_needed():
    """工作台 exe 要不要更新。

    2026-09-22 改：**优先用 Release 的 SHA256SUMS 比 sha256**。
    原来只拿 `VENDOR_REL`（GitHub）发一个 HEAD 比大小——国内常常连不上 api/对象存储，
    结果就是"问不到远端 → 先不换"，等于**永远不更新**。SHA256SUMS 是走 能力包.urls_for()
    的多源（Gitee 优先）取的，而且比大小严格；拿不到那份清单才退回老办法。
    """
    if not os.path.isfile(EXE_DST):
        return True, "还没装"
    want = _expected_sha("score-tool.exe")
    if want and DL is not None:
        try:
            have = DL.sha256_file(EXE_DST)
        except Exception:                                        # noqa: BLE001
            have = ""
        if have == want:
            return False, "已经是最新（sha256 一致）"
        return True, "远端有新版本（sha256 变了）"
    try:
        with open(EXE_MARK, encoding="utf-8") as f:
            mark = json.load(f)
    except (OSError, ValueError):
        return True, "没记录过版本（按最新的下一份）"
    try:
        size, lm = _remote_head(VENDOR_REL + "score-tool.exe")
    except Exception as e:                                       # noqa: BLE001
        return False, "问不到远端（%s），先不换" % str(e)[:70]
    if not size:
        return False, "远端没给大小，先不换"
    if mark.get("size") != size:
        return True, "远端有新版本（%.1f MB → %.1f MB）" % (
            (mark.get("size") or 0) / 1048576.0, size / 1048576.0)
    if mark.get("lm") and lm and mark["lm"] != lm:
        return True, "远端那份更新过（时间变了）"
    return False, "已是最新"


def _workbench_running():
    """工作台还开着吗——**换 exe 前必须确认**：运行中的实例被换掉文件可能崩（2026-09-16 踩过）。"""
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq score-tool.exe", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, errors="replace", timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [l for l in out.splitlines() if l.strip().lower().startswith('"score-tool.exe"')]


def refresh_exe(yes=False, force=False):
    """把工作台 exe 换成远端最新那份（找不到新版本就什么都不做）。"""
    if force:
        need, why = True, "手动指定 --exe"
    else:
        need, why = exe_refresh_needed()
    if not need:
        ok("工作台 exe 已是最新", why)
        return
    if not yes:
        todo("更新工作台程序（exe）：%s" % why,
             "python tools/deploy.py vendor --fetch --yes   （约 19MB；装完就是最新版工作台）")
        return
    run = _workbench_running()
    if run:
        bad("换 exe 前先把工作台关掉", "现在有 %d 个 score-tool.exe 在跑——关掉它，或者双击两次让它显示出来再关，"
            "然后重跑这条命令" % len(run))
        return
    tmp = EXE_DST + ".new"
    log("  下 score-tool.exe（约 18MB；直连不动会自动换镜像源）…")
    try:
        n = _download_any("score-tool.exe", tmp)
    except Exception as e:                                       # noqa: BLE001
        bad("下载 score-tool.exe", str(e)[:150], "网络不通就先跳过：工作台还是你原来那个，功能不受影响")
        return
    try:                                   # 记版本戳用远端的时间（拿不到就只记大小，下次照样能比）
        _sz, _lm = _remote_head(VENDOR_REL + "score-tool.exe")
    except Exception:                                            # noqa: BLE001
        _lm = ""
    try:
        os.replace(tmp, EXE_DST)          # 原子换位；被占用会抛 PermissionError（上面已先拦过一道）
    except OSError as e:
        bad("换位 score-tool.exe", str(e)[:120], "再确认一次工作台真的关掉了")
        return
    try:
        with open(EXE_MARK, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"size": os.path.getsize(EXE_DST), "lm": _lm,
                       "at": time.strftime("%Y-%m-%d %H:%M:%S")}, f, ensure_ascii=False)
    except OSError:
        pass
    ok("工作台 exe 已更新", "%.1f MB（%s）" % (n / 1048576.0, why))


def _assets_for(caps=(), with_exe=True):
    """这次要拉哪些附件：core（wheels + ffmpeg）＋ 指定能力包（＋工作台 exe）。

    清单来自 `能力包.py`（唯一真相源）；没有那个模块就退回本文件里的 VENDOR_ASSETS（老布局）。
    """
    out = []
    if KP is not None:
        out = list(KP.assets_of(list(caps)))
    else:
        out = [(fn, sub, mb) for fn, sub, mb in VENDOR_ASSETS if not fn.endswith(".exe")]
    if with_exe:
        out.append(("score-tool.exe", "dist", 18))
    return out


def _asset_present(fn, sub):
    if fn.endswith(".exe"):
        return os.path.isfile(os.path.join(HERE, "dist", "score-tool.exe"))
    if fn.startswith("ffmpeg"):
        return os.path.isfile(os.path.join(FF_BIN, "ffmpeg.exe"))
    if fn.startswith("cap-depth"):
        return os.path.isfile(os.path.join(MODEL_DIR, "model.onnx"))
    return os.path.isdir(os.path.join(VENDOR, sub))


def vendor_missing(caps=()):
    """还缺哪些大件（归档安装的正常现象：仓库里没带）。

    2026-09-22：默认只看 core；`caps` 里点名了的能力包才算"该有"。老布局（wheels-heavy/
    depth-model 那套）仍然认——别人机器上还可能是旧结构，别把人家已经装好的判成"缺"。
    """
    miss = [a for a in _assets_for(caps) if not _asset_present(a[0], a[1])]
    if os.path.isdir(HEAVY_WHEELS) or os.path.isfile(os.path.join(MODEL_DIR, "model.onnx")):
        # 老机器：重型 wheels/模型已在位 → 不再要求新包名（它已经能用了）
        miss = [a for a in miss if not a[0].startswith("cap-")]
    return miss


def _download(url, dst):
    """下文件，边下边报进度（大件 80–120MB，没进度条会以为卡死）。"""
    import urllib.request
    t0 = time.time()
    with urllib.request.urlopen(url, timeout=120) as r, open(dst, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        last = 0
        while True:
            b = r.read(262144)
            if not b:
                break
            f.write(b)
            got += len(b)
            if time.time() - last > 3:
                last = time.time()
                if total:
                    log("    %5.1f%%  %.1f/%.1f MB  %.1f MB/s" % (got * 100.0 / total, got / 1048576,
                        total / 1048576, got / 1048576 / max(0.1, time.time() - t0)))
                else:
                    log("    已下 %.1f MB  %.1f MB/s" % (got / 1048576, got / 1048576 / max(0.1, time.time() - t0)))
    return got


_SUMS_CACHE = {}


def _expected_sha(rel):
    """Release 上 SHA256SUMS.txt 里这个附件的 sha256（拿不到就返回 ""，只做大小校验）。"""
    if rel in _SUMS_CACHE:
        return _SUMS_CACHE[rel]
    val = ""
    if DL is not None:
        try:
            urls = (KP.urls_for("SHA256SUMS.txt") if KP is not None
                    else [VENDOR_REL + "SHA256SUMS.txt"])
            val = DL.hashes(urls).get(rel, "")
        except Exception:                                        # noqa: BLE001
            val = ""
    _SUMS_CACHE[rel] = val
    return val


def _download_any(rel, dst):
    """按 **Gitee → GitHub 直连 → 镜像** 的顺序下 Release 附件，哪个先下动用哪个。

    2026-09-22 换成 下载器：
      · **断点续传**（`Range`）——300MB 的包弱网断一次，接着下，不再从头；而且**换源也接着下**；
      · **同一个源自己重试**（原来只换源）；
      · **验 sha256**（Release 上有 SHA256SUMS.txt 就比，没有就比大小）——截断/错误页不再当"下好了"。
    源顺序见 能力包.urls_for()（Gitee 排第一的理由也写在那个 docstring 里）。
    """
    if DL is None:
        raise OSError("找不到 tools\\下载器.py（它负责续传与校验），先把它放进 tools/ 再跑")
    sha = _expected_sha(rel)
    urls = KP.urls_for(rel) if KP is not None else [VENDOR_REL + rel]
    return DL.fetch_any(urls, dst, sha256=sha, tries=2, log=log)


def _flatten_dup(sub):
    """解开后若出现 `_vendor/<sub>/<sub>/…` 这种多套一层，摊平它（2026-09-17 打模型包时踩到过）。"""
    inner = os.path.join(VENDOR, sub, sub)
    if not os.path.isdir(inner):
        return
    for name in os.listdir(inner):
        src = os.path.join(inner, name)
        dst = os.path.join(VENDOR, sub, name)
        if not os.path.exists(dst):
            shutil.move(src, dst)
    try:
        os.rmdir(inner)
        log("  （附件里多套了一层 %s/%s/，已自动摊平）" % (sub, sub))
    except OSError:
        pass


def fetch_vendor(yes=False, force_exe=False, caps=(), with_ffmpeg=False):
    """把默认大件（core）从 Release 拉下来并解开，顺带把工作台 exe 更新到最新。

    2026-09-17 加：**exe 每次都会跟远端比一下**（老机器上它一直在，但可能是旧版——
    工作台的界面/流程全在 exe 里，旧 exe = 旧功能）。换位前会拦"工作台还开着"。

    2026-09-22 改：
      · 默认只拉 core（wheels + ffmpeg ≈ 76MB；下载器带续传，断一次不用从头）；
      · `caps=("stt",)` 这类**能力包**按需拉（转写 26MB / 图像 60MB / 深度 88MB，见 能力包.py）；
      · 机器上已有**能力够的系统 ffmpeg**（编码器/滤镜探测过）→ 跳过 ffmpeg 那 76MB，
        想强制用仓库自带那份加 `--with-ffmpeg`。
    """
    use_caps = list(caps)
    skip_ffmpeg = False
    if not with_ffmpeg and KP is not None:
        ok_ff, why_ff = KP.ffmpeg_ok()
        skip_ffmpeg = bool(ok_ff)
        (ok if ok_ff else log)("系统 ffmpeg%s" % ("可以用：%s" % why_ff if ok_ff else "：%s" % why_ff))
        if not ok_ff:
            log("  → 这次会拉仓库自带那份 ffmpeg（约 76MB）")
    miss = [m for m in vendor_missing(use_caps) if m[0] != "score-tool.exe"]
    if skip_ffmpeg:
        miss = [m for m in miss if not m[0].startswith("ffmpeg")]
    if miss:
        names = "、".join("%s（约 %dMB）" % (a[0], a[2]) for a in miss)
        if not yes:
            todo("下载离线大件：%s" % names,
                 "python tools/deploy.py vendor --fetch --yes %s  （从 %s 拉；国内慢会自动换镜像源）"
                 % (" ".join("--" + c for c in use_caps), VENDOR_REL))
        else:
            import zipfile
            for fn, sub, mb in miss:
                zip_dst = os.path.join(VENDOR, fn)
                log("  下 %s（约 %dMB；直连不动会自动换源，断了会接着下）…" % (fn, mb))
                try:
                    n = _download_any(fn, zip_dst)
                except Exception as e:                           # noqa: BLE001
                    bad("下载 " + fn, str(e)[:160],
                        "网络不通就先跳过：用镜像在线装（python tools/deploy.py install --yes）")
                    continue
                ok("下好了 " + fn, "%.1f MB" % (n / 1048576.0))
                try:
                    if fn.startswith("ffmpeg"):
                        os.makedirs(FF_BIN, exist_ok=True)
                        with zipfile.ZipFile(zip_dst) as z:
                            for m in [x for x in z.namelist() if "/bin/" in x and not x.endswith("/")]:
                                with z.open(m) as src, open(os.path.join(FF_BIN, os.path.basename(m)), "wb") as dst:
                                    dst.write(src.read())
                    else:
                        with zipfile.ZipFile(zip_dst) as z:
                            z.extractall(VENDOR)
                        _flatten_dup(sub)          # 附件里若多套了一层（models/models/…）自动摊平
                    ok("就位 " + fn)
                except Exception as e:                           # noqa: BLE001
                    bad("解开 " + fn, str(e)[:160])
    else:
        ok("离线大件已齐", "core（wheels + %s）都在"
           % ("用系统 ffmpeg" if skip_ffmpeg else "自带 ffmpeg"))
    # 没点名、但确实还缺的能力包 → 说清怎么装（别让用户猜）
    if KP is not None:
        rest = [c for c in KP.ORDER if c not in use_caps and not KP.present(c, HERE)]
        if rest:
            log("  [按需] 这几个能力包还没装（用到再装，不装不影响默认流程）：")
            for line in KP.hint(rest).splitlines():
                log("    " + line)
    # 工作台 exe：单独走"比版本"的路（`--exe` 可强制重下）
    refresh_exe(yes, force=bool(force_exe or os.environ.get("HERONBO_FORCE_EXE")))


MODEL_DIR_MARK = True


def vendored_ffmpeg(yes=False):
    """解开仓库自带的 ffmpeg（ffmpeg.exe + ffprobe.exe + 依赖 dll）。已解开就跳过。"""
    if os.path.isfile(os.path.join(FF_BIN, "ffmpeg.exe")):
        ok("仓库自带 ffmpeg", FF_BIN)
        return
    if not os.path.isfile(FF_ZIP):
        bad("仓库自带 ffmpeg", "没有 %s" % FF_ZIP)
        return
    if not yes:
        todo("解开仓库自带的 ffmpeg（免装、免联网）",
             "python tools/deploy.py install --yes  （会解到 %s）" % FF_BIN)
        return
    log("  正在解开 %s …" % os.path.basename(FF_ZIP))
    try:
        import zipfile
        os.makedirs(FF_BIN, exist_ok=True)
        with zipfile.ZipFile(FF_ZIP) as z:
            for n in [x for x in z.namelist() if "/bin/" in x and not x.endswith("/")]:
                with z.open(n) as src, open(os.path.join(FF_BIN, os.path.basename(n)), "wb") as dst:
                    dst.write(src.read())
    except Exception as e:                                       # noqa: BLE001
        bad("解开仓库自带 ffmpeg", str(e))
        return
    if os.path.isfile(os.path.join(FF_BIN, "ffmpeg.exe")):
        ok("仓库自带 ffmpeg 已解开", "%s（%d 个文件）" % (FF_BIN, len(os.listdir(FF_BIN))))
    else:
        bad("解开仓库自带 ffmpeg", "解完没看到 ffmpeg.exe")


def vendored_model():
    """仓库自带的 Depth 模型（深度视频要它，免下 HF、免代理）。"""
    m = os.path.join(MODEL_DIR, "model.onnx")
    if os.path.isfile(m) and os.path.getsize(m) > 90 * 1024 * 1024:
        ok("仓库自带 Depth 模型", "%.0f MB（深度视频免下载）" % (os.path.getsize(m) / 1048576))
    else:
        bad("仓库自带 Depth 模型", "没有或太小：" + m)


def do_install(yes=False, extras=False):
    """装依赖。**分两档**，别让新机一上来就下几百 MB：

    - 必须：pywebview / pythonnet / clr_loader（工作台独立窗口要用）→ 仓库里带了离线 wheel，
      没网也能装、几秒钟装完（2026-09-16 用户反映"安装花很长时间"就是这些+重包一起装的结果）。
    - 按需：numpy / opencv（成片对比、人物遮罩、竖版画布）· faster-whisper（拆解转写）·
      onnxruntime（深度视频）→ 用到哪个功能再装哪个，加 `--extras` 一次装全。
    """
    log("== 依赖 ==")
    vendored_ffmpeg(yes)
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

    # 2026-09-22：按能力包来看（能力包.py 是唯一真相源）。默认**不再**"有离线包就全装"——
    # 用户要的是"装得快"：默认流程压根不用转写/深度，缺了就告诉他一键装哪条命令。
    if KP is not None:
        miss_caps, miss_mods = [], []
        for cap in KP.ORDER:
            if not KP.present(cap, HERE) or KP.missing_mods(PY, cap):
                miss_caps.append(cap)
                miss_mods += [m for m in KP.CAPS[cap]["mods"] if not KP.mod_present(m)]
        if not miss_caps:
            ok("能力包也已齐（转写 / 图像 / 转深度片）")
            return
        log("  [按需] 还没装的能力包：%s" % "、".join("%s(%s)" % (c, KP.CAPS[c]["title"]) for c in miss_caps))
        for line in KP.hint(miss_caps).splitlines():
            log("         " + line)
        if extras:                       # --extras：连能力包一起装（在线装或本地能力包轮子）
            dirs = [os.path.join(VENDOR, d) for c in miss_caps for d in KP.CAPS[c]["wheels_dirs"]
                    if os.path.isdir(os.path.join(VENDOR, d))]
            mods = [m for m in ("numpy", "cv2", "faster_whisper", "onnxruntime") if not KP.mod_present(m)]
            if not mods:
                return
            if dirs:
                cmd = [PY, "-m", "pip", "install", "--no-index"]
                for d in dirs:
                    cmd += ["--find-links", d]
                cmd += mods
                src = "本地能力包（免联网）"
            else:
                cmd = [PY, "-m", "pip", "install", "-i",
                       "https://pypi.tuna.tsinghua.edu.cn/simple"] + mods
                src = "在线（清华源）"
            if not yes:
                todo("装能力包依赖：%s（%s）" % (", ".join(mods), src), '"%s"   （加 --yes）' % '" "'.join(cmd))
                return
            log("  正在装：%s" % ", ".join(mods))
            r = run(cmd, timeout=1800)
            (ok if r.returncode == 0 else bad)("装 %s" % ", ".join(mods), (r.stdout or r.stderr or "")[-200:])
        return

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
    if not extras and os.path.isdir(HEAVY_WHEELS):
        # 仓库自带离线包 → 默认一起装上（不联网、不花流量）：用户要的是"下一个机器=装完即有全部环境"
        log("  [离线包] 仓库自带 wheels-heavy，按需依赖也一并装上：%s" % lines)
        extras = True
    if not extras:
        log("  [按需] 还没装：%s" % lines)
        log("         ↳ 用到哪个功能再装：python tools\\环境检查.py --install --yes %s"
            % ("--extras" if len(miss) > 1 else ""))
        return
    if os.path.isdir(HEAVY_WHEELS):
        cmd = [PY, "-m", "pip", "install", "--no-index", "--find-links", HEAVY_WHEELS] + miss
        src = "仓库自带（免联网）"
    else:
        cmd = [PY, "-m", "pip", "install", "-i", "https://pypi.tuna.tsinghua.edu.cn/simple"] + miss
        src = "在线"
    if not yes:
        todo("装按需依赖：%s（%s）" % (", ".join(miss), src), '"%s"   （加 --yes）' % '" "'.join(cmd))
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
                "python tools\\部署.py vendor --fetch --yes（从 Release 拿官方 exe）")
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
          # ★ 桌面路径必须问系统（GetFolderPath），不能拼 USERPROFILE\Desktop——
          #   OneDrive/企业漫游会把桌面重定向到别处，拼出来的 lnk 建在了一个
          #   用户看不见的目录里（2026-09-17 用户反馈"别的电脑上没看见快捷方式"）。
          "$desk = [Environment]::GetFolderPath('Desktop'); "
          "$lnk = Join-Path $desk 'HeronBo 视频工作台.lnk'; "
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


# ── Defender 误报（2026-09-22 用户在别人电脑上 exe 被删）───────────────────
def defender(yes=False):
    """工作台 exe 被微软 Defender（或其它杀软）删了的检查与修复。

    为什么会被删：PyInstaller **onefile** 打包、又没签名 —— Defender 的启发式
    最爱误报这种组合（Trojan:Win32/Wacatac 之类）。仓库里 exe 每周都重编，签名证书
    又是年费项，所以现阶段给装机端的是「检查 → 加白名单 → 还原/重下」三板斧。
    """
    log("== Defender 误报检查（exe 被删先跑这个）==")
    if os.path.isfile(EXE):
        ok("score-tool.exe 在", EXE + "（本机没被删）")
    else:
        bad("score-tool.exe", "不在 —— 很可能被 Defender 隔离了",
            "先加白名单（--yes），再 python tools\\部署.py vendor --fetch --yes 重下")
    ps = ("Get-MpThreatDetection -ErrorAction SilentlyContinue | "
          "Where-Object { ($_.Resources -join ' ') -match 'score-tool' } | "
          "Select-Object -First 5 InitialDetectionTime,Resources | Format-List | Out-String")
    r = run(["powershell", "-NoProfile", "-Command", ps])
    hits = (r.stdout or "").strip()
    if r.returncode == 0 and hits:
        log("  [实锤] Defender 的保护历史里有它：\n%s" % hits)
    else:
        log("  [说明] 命令行读不到保护历史（常见，不一定是没有）；"
            "手动看：Windows 安全中心 → 病毒和威胁防护 →「保护历史」")
    dist = os.path.dirname(EXE)
    cmd = "Add-MpPreference -ExclusionPath '%s'" % dist
    if not yes:
        todo("把工作台目录加进 Defender 白名单（要管理员权限）",
             "管理员 PowerShell 里跑：%s" % cmd + "；然后到「保护历史」还原被隔离的 "
             "score-tool.exe（或重跑 python tools\\部署.py vendor --fetch --yes 重下）")
        return
    log("  正在提权加白名单（弹出 UAC 请点「是」）…")
    e = run(["powershell", "-NoProfile", "-Command",
             "Start-Process powershell -Verb RunAs -Wait -ArgumentList "
             "'-NoProfile','-Command','%s'" % cmd])
    if e.returncode == 0:
        v = run(["powershell", "-NoProfile", "-Command",
                 "(Get-MpPreference).ExclusionPath -join ';'"])
        if dist.lower() in (v.stdout or "").lower():
            ok("白名单已加", dist + "（Defender 不会再动它）")
        else:
            bad("白名单没验上", (v.stdout or v.stderr or "")[-120:],
                "手动：Windows 安全中心 → 排除项 → 添加文件夹 " + dist)
    else:
        bad("提权失败", (e.stderr or "")[-120:], "手动以管理员身份跑：%s" % cmd)
    if not os.path.isfile(EXE):
        log("  下一步：python tools\\部署.py vendor --fetch --yes  （把 exe 重新下回来）")


def main():
    ap = argparse.ArgumentParser(description="工作台部署（自检 + 接 agent + 依赖 + 快捷方式）")
    ap.add_argument("action", nargs="?", default="check",
                    choices=["check", "install", "agents", "shortcut", "start", "wx", "vendor", "defender", "all"])
    ap.add_argument("--yes", action="store_true", help="真动手（不加就只打印命令）")
    ap.add_argument("--root", help="设置样本库根目录")
    ap.add_argument("--fetch", action="store_true",
                    help="vendor 时从 Release 下大件（默认只下 core，约 76MB；断点续传）")
    ap.add_argument("--exe", action="store_true",
                    help="vendor 时强制重下工作台 exe（默认只在远端有新版本时才换）")
    # 2026-09-22：能力包（用到才装的大件）——默认一个都不下
    ap.add_argument("--stt", action="store_true", help="vendor 时连「转写」能力包一起下（约 26MB）")
    ap.add_argument("--depth", action="store_true", help="vendor 时连「转深度片」能力包一起下（约 88MB）")
    ap.add_argument("--all-caps", dest="all_caps", action="store_true",
                    help="vendor 时把三个能力包都下齐（约 175MB）")
    ap.add_argument("--with-ffmpeg", dest="with_ffmpeg", action="store_true",
                    help="vendor 时强制拉仓库自带的 ffmpeg（默认：系统那份能力够就跳过这 76MB）")
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
    _caps = (list(KP.ORDER) if (getattr(a, "all_caps", False) and KP is not None)
             else [c for c in ("stt", "depth") if getattr(a, c, False)])
    if a.action == "vendor":
        if getattr(a, "fetch", False):
            fetch_vendor(a.yes, force_exe=getattr(a, "exe", False), caps=_caps,
                         with_ffmpeg=getattr(a, "with_ffmpeg", False))
        else:
            vendored_ffmpeg(a.yes)
            vendored_model()
            if vendor_missing(_caps):
                log("  [提示] 还缺大件（归档/SkillHub 安装的正常现象）："
                    "python tools/deploy.py vendor --fetch --yes 可以拉齐"
                    "（默认 core 约 140MB；加 --stt/--depth 连能力包一起）")
    if a.action in ("agents", "all"):
        check_agents(a.yes, ping=a.ping)
    if a.action in ("install", "all"):
        do_install(a.yes, a.extras)
    if a.action == "all":                 # all = 真正"一遍到底"：大件与 exe 缺了就一起拉
        fetch_vendor(a.yes)
    if a.action in ("shortcut", "all"):
        do_shortcut(a.yes)
    if a.action in ("start", "all"):
        launch(a.yes)          # 装完就弹出来：用户不用自己去找入口（2026-09-16 用户要求）
    if a.action == "wx":
        fix_webview2(a.yes)
    if a.action == "defender":
        defender(a.yes)
    log("\n小结：OK %d 项 · 缺 %d 项 · 待办 %d 项" % (len(_OK), len(_BAD), len(_TODO)))
    return 1 if _BAD else 0


if __name__ == "__main__":
    sys.exit(main())
