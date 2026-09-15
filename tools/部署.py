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
        bad("pywebview", "工作台主界面要用它", "python -m pip install pywebview")
    node = shutil.which("node") or r"C:\Program Files\nodejs\node.exe"
    if os.path.isfile(str(node)):
        ok("node", str(node))
    else:
        bad("node", str(node), "装 Node.js（agent CLI 都要用它）")
    ff = shutil.which("ffprobe")
    if ff:
        ok("ffprobe", ff)
    else:
        bad("ffprobe", "不在 PATH", "winget install --id Gyan.FFmpeg -e（读素材宽高时长要用）")
    if os.path.isfile(EXE):
        ok("score-tool.exe", EXE)
    else:
        bad("score-tool.exe", "还没打包",
            "python -m PyInstaller --noconfirm score-tool.spec")
    if os.path.isfile(ICO):
        ok("工作台.ico", ICO)
    else:
        bad("工作台.ico", ICO, "python tools\\图标.py")
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


def check_agents(yes=False):
    log("== agent 通道 ==")
    try:
        import agent_bridge as ab
    except Exception as e:                                       # noqa: BLE001
        bad("agent_bridge.py", str(e))
        return
    for r in ab.list_agents():
        line = "%s（%s）%s" % (r["label"], "本 skill 装在它名下" if r["host"] else "本机",
                              r["why"][:80])
        if r["ok"]:
            ok("agent: " + r["label"], line)
        else:
            bad("agent: " + r["label"], line)
    wire_zcode(yes)


# ── 依赖安装（默认只打印）──────────────────────────────────────────────────
def do_install(yes=False):
    log("== 依赖 ==")
    need = []
    try:
        import webview  # noqa: F401
    except ImportError:
        need.append("pywebview")
    if not need:
        ok("依赖已齐（pywebview）")
        return
    cmd = [PY, "-m", "pip", "install", "-i",
           "https://pypi.tuna.tsinghua.edu.cn/simple"] + need
    if not yes:
        todo("缺依赖：%s" % ", ".join(need),
             '"%s"' % '" "'.join(cmd) + "   （加 --yes 让本脚本直接装）")
        return
    log("  正在装：%s" % " ".join(need))
    r = run(cmd, timeout=600)
    (ok if r.returncode == 0 else bad)("安装 %s" % ", ".join(need),
                                       (r.stdout or r.stderr or "")[-200:])


# ── 桌面快捷方式 ───────────────────────────────────────────────────────────
def do_shortcut(yes=False):
    log("== 桌面快捷方式 ==")
    if not os.path.isfile(EXE):
        bad("score-tool.exe", EXE, "先打包：python -m PyInstaller --noconfirm score-tool.spec")
        return
    if not yes:
        todo("建/更新桌面快捷方式",
             "python tools\\部署.py shortcut --yes（指向 %s，图标用 tools\\工作台.ico）" % EXE)
        return
    ps = ("$ws = New-Object -ComObject WScript.Shell; "
          "$lnk = Join-Path $env:USERPROFILE 'Desktop\\HeronBo 视频工作台.lnk'; "
          "$s = $ws.CreateShortcut($lnk); "
          "$s.TargetPath = '%s'; $s.WorkingDirectory = '%s'; "
          "$s.IconLocation = '%s,0'; $s.Description = 'HeronBo 视频工作台'; $s.Save(); "
          "Write-Output $lnk") % (EXE, os.path.dirname(EXE), ICO)
    exe = shutil.which("powershell.exe") or "powershell.exe"
    r = run([exe, "-NoProfile", "-Command", ps])
    (ok if r.returncode == 0 else bad)("桌面快捷方式", (r.stdout or r.stderr or "").strip()[-120:])


def main():
    ap = argparse.ArgumentParser(description="工作台部署（自检 + 接 agent + 依赖 + 快捷方式）")
    ap.add_argument("action", nargs="?", default="check",
                    choices=["check", "install", "agents", "shortcut", "all"])
    ap.add_argument("--yes", action="store_true", help="真动手（不加就只打印命令）")
    ap.add_argument("--root", help="设置样本库根目录")
    a = ap.parse_args()
    log("工作台部署 · %s%s" % (a.action, "（--yes：会真改）" if a.yes else "（只体检/打印）"))
    if a.action in ("check", "all"):
        check_env()
        check_root(a.root)
    if a.action in ("agents", "all"):
        check_agents(a.yes)
    if a.action in ("install", "all"):
        do_install(a.yes)
    if a.action in ("shortcut", "all"):
        do_shortcut(a.yes)
    log("\n小结：OK %d 项 · 缺 %d 项 · 待办 %d 项" % (len(_OK), len(_BAD), len(_TODO)))
    return 1 if _BAD else 0


if __name__ == "__main__":
    sys.exit(main())
