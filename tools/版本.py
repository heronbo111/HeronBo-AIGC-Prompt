# -*- coding: utf-8 -*-
r"""版本身份 + 「远端有没有新版」——工作台更新功能的唯一判断入口。

为什么要有它（2026-09-22 用户：「我想要给工作台做一个更新功能，能够下载我推送的新版本」）：
  界面、本地服务、流程代码**全在 exe 里**，旧 exe 就等于旧功能；而此前工作台里
  **没有任何"我这是哪一版、远端有没有新版"的入口**，用户只能自己想起来跑命令行。

三件事：
  current(tools_dir)  → 我这一版是谁：优先读打包进 exe 的 `_version.json`；
                        从源码跑时回退（技能根 SKILL.md 的 version + git 短 hash + exe 修改时间）。
  remote(urls)        → 远端那一版是谁：拉 Release 附件里的 `version.json`（Gitee 优先）。
                        取不到就返回 {}（**不抛异常**，调用方按"离线"处理）。
  compare(cur, rem)   → 要不要更新：先比 version 数字（2.10 > 2.9）；版本号相同再比 exe 的 sha256
                        （远端重发过同一个版本号时也能认出来）。

命令行（不只有界面能用）：
    python tools\版本.py current
    python tools\版本.py remote
    python tools\版本.py check
    python tools\版本.py compare 2.9 2.10     # 自测版本号比法
    python tools\版本.py write [--version 2.10] [--notes "…"]   # 生成 tools\_version.json（打包前用）
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
LOCAL_JSON = os.path.join(HERE, "_version.json")          # 打包进 exe（spec 的 datas 里）
EXE_DEV = os.path.join(HERE, "dist", "score-tool.exe")
SCHEMA = "heronbo.version/1"


def _say(m):
    print(m, flush=True)


def _frozen():
    return bool(getattr(sys, "frozen", False))


def exe_path():
    """当前跑的是哪个 exe：打包后＝自己；源码运行时＝tools/dist/score-tool.exe。"""
    if _frozen():
        return sys.executable
    return EXE_DEV


def skill_root(tools_dir=None):
    """技能根目录（tools 的上一层）。

    ⚠️ 打包后 HERE 是临时解包目录（_MEIxxxx），它的上一层当然没有 SKILL.md——老实现到这就
    返回 ""，于是「检查更新」里"技能源码"那节在 exe 里**永远**报"不是 git 工作树"，
    拉取按钮和小圆点也点不出来（2026-09-22 实测：控制台同一函数返回正常、exe 里返回 ""）。
    exe 装在 <仓库>/tools/dist/ 下 → 再从**exe 自己的位置**向上找几层就有根了。
    发行包装到没有仓库的机器上（纯 vendor 安装）→ 两路都找不到，照旧返回 ""。
    """
    bases = [os.path.dirname(os.path.abspath(tools_dir or HERE))]
    if _frozen():
        bases.append(os.path.dirname(os.path.abspath(sys.executable)))
    for base in bases:
        d = base
        for _ in range(6):                      # tools/dist → tools → 仓库根 → …（最多 6 层）
            if os.path.isfile(os.path.join(d, "SKILL.md")):
                return d
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
    return ""


def skill_version(tools_dir=None):
    """版本号真相源：SKILL.md 的 frontmatter `version:`（其次仓库根 VERSION 文件）。"""
    root = skill_root(tools_dir)
    if root:
        try:
            with open(os.path.join(root, "SKILL.md"), encoding="utf-8", errors="replace") as f:
                head = f.read(4000)
            m = re.search(r"^version:\s*([0-9][^\s#]*)", head, re.M)
            if m:
                return m.group(1).strip().strip('"\'')
        except OSError:
            pass
        try:
            with open(os.path.join(root, "VERSION"), encoding="utf-8", errors="replace") as f:
                v = f.read().strip()
            if v:
                return v
        except OSError:
            pass
    return ""


def git_commit(tools_dir=None):
    """短 hash；不是 git 工作树（别人解压的 zip）就返回 ""。"""
    cwd = skill_root(tools_dir) or os.path.dirname(os.path.abspath(tools_dir or HERE))
    try:
        r = subprocess.run(["git", "-C", cwd, "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=15)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _mtime_str(p):
    try:
        return datetime.datetime.fromtimestamp(os.path.getmtime(p)).strftime("%Y-%m-%d %H:%M")
    except OSError:
        return ""


def _read_local_json():
    try:
        with open(LOCAL_JSON, encoding="utf-8-sig") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def current(tools_dir=None, with_hash=False):
    """我这一版是谁。exe 里读 _version.json；源码运行时用 SKILL.md + git 兜。"""
    d = _read_local_json()
    if d.get("version"):
        out = {"version": str(d["version"]), "commit": d.get("commit") or "",
               "builtAt": d.get("builtAt") or "", "source": "exe" if _frozen() else "local",
               "exe": d.get("exe") or {}, "repo": d.get("repo") or {},
               "notes": d.get("notes") or ""}
    else:
        ex = exe_path()
        out = {"version": skill_version(tools_dir) or "0.0",
               "commit": git_commit(tools_dir), "builtAt": _mtime_str(ex),
               "source": "source", "exe": {"name": os.path.basename(ex),
                                           "size": os.path.getsize(ex) if os.path.isfile(ex) else 0,
                                           "sha256": ""},
               "repo": {"branch": "main", "commit": git_commit(tools_dir)}, "notes": ""}
    if with_hash:
        out["exe"] = dict(out.get("exe") or {})
        ex = exe_path()
        if os.path.isfile(ex):
            # **每次都按正在跑的这个文件重算**（别信 _version.json 里那份）：
            # 它是打包前写的，装进 exe 之后就已经"过期一轮"；拿它去跟远端比会得出
            # "远端有新版本"的假结论（明明装的就是最新那版）。
            import 下载器 as dl
            out["exe"]["size"] = os.path.getsize(ex)
            out["exe"]["sha256"] = dl.sha256_file(ex)
    return out


def remote(urls=None, timeout=12):
    """远端那一版：拉 Release 附件 version.json。取不到返回 {}（离线不算错误）。"""
    import 下载器 as dl
    if urls is None:
        import 能力包 as kp
        urls = kp.urls_for("version.json")
    tmp = os.path.join(tempfile.gettempdir(), "heronbo_version_remote.json")
    try:
        if os.path.isfile(tmp):
            os.remove(tmp)
    except OSError:
        pass
    try:
        dl.fetch_any(list(urls), tmp, tries=1, log=lambda *_a: None, timeout=timeout)
        with open(tmp, encoding="utf-8-sig") as f:
            d = json.load(f)
        if isinstance(d, dict) and d.get("version"):
            return d
        return {}
    except Exception:                                            # noqa: BLE001
        return {}


def vkey(v):
    """版本号比法：按 . 切段，数字段按数比（2.10 > 2.9），缺位补 0；非数字段兜底成字符串比。

    为什么不直接比字符串：`"2.10" < "2.9"` 会得出"远端更旧"的错结论。
    """
    parts = str(v or "0").split(".")
    out = []
    for p in parts:
        p = p.strip()
        if p.isdigit():
            out.append((1, int(p), ""))
        else:
            m = re.match(r"^(\d+)(.*)$", p)
            if m:
                out.append((1, int(m.group(1)), m.group(2)))
            else:
                out.append((0, 0, p))
    return out


def cmp_version(a, b):
    ka, kb = vkey(a), vkey(b)
    n = max(len(ka), len(kb))
    zero = (1, 0, "")
    for i in range(n):
        x = ka[i] if i < len(ka) else zero
        y = kb[i] if i < len(kb) else zero
        if x != y:
            return 1 if x > y else -1
    return 0


def compare(cur, rem, with_hash=True):
    """要不要更新。返回 {'need': bool, 'why': str}。"""
    if not rem:
        return {"need": False, "why": "取不到远端（离线？）"}
    cv, rv = cur.get("version") or "0.0", rem.get("version") or "0.0"
    c = cmp_version(rv, cv)
    if c > 0:
        return {"need": True, "why": "远端 %s（你 %s）" % (rv, cv)}
    if c < 0:
        return {"need": False, "why": "你这份比远端还新（你 %s，远端 %s）" % (cv, rv)}
    # 版本号一样：远端可能重发过同一版（改了内容没改号）→ 比 exe 的 sha256
    rh = ((rem.get("exe") or {}).get("sha256") or "").lower()
    if with_hash and rh:
        cur = current(with_hash=True)
        lh = ((cur.get("exe") or {}).get("sha256") or "").lower()
        if lh and lh != rh:
            return {"need": True, "why": "版本号没变（%s），但远端的包换过内容" % rv}
    return {"need": False, "why": "已是最新（%s）" % cv}


def _notes_from_changelog(tools_dir=None):
    """从 CHANGELOG 顶部取一段给人看的更新说明（标题 + 头两条）。取不到就返回 ""。"""
    root = skill_root(tools_dir)
    if not root:
        return ""
    p = os.path.join(root, "CHANGELOG.md")
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return ""
    out, started = [], False
    for ln in lines:
        s = ln.strip()
        if not started:
            if s.startswith("## "):
                started = True
                out.append(s.lstrip("# ").strip())
            continue
        if s.startswith("## "):
            break
        if s.startswith("- "):
            out.append(s[2:].strip()[:90])
            if len(out) >= 4:
                break
    txt = " ｜ ".join(out)[:400]
    # 更新说明是要显示在界面上的，去掉 markdown 记号（**粗体**、`代码`、[文字](链接)）
    txt = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", txt)
    txt = txt.replace("**", "").replace("`", "")
    return txt


def write_local(tools_dir=None, version="", notes="", log=_say):
    """生成 tools\\_version.json（打包前调；spec 的 datas 会把它打进 exe）。

    一份两用：**运行中的版本**与**发行版元数据**是同一份东西，不会对不上。
    """
    import 下载器 as dl
    old = _read_local_json()
    ver = version or skill_version(tools_dir) or old.get("version") or "0.0"
    ex = exe_path()
    ent = {"name": os.path.basename(ex), "size": 0, "sha256": ""}
    if os.path.isfile(ex):
        ent["size"] = os.path.getsize(ex)
        ent["sha256"] = dl.sha256_file(ex)
    d = {"schema": SCHEMA, "version": ver, "commit": git_commit(tools_dir),
         "builtAt": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
         "minLauncher": old.get("minLauncher") or ver,
         "exe": ent,
         "repo": {"branch": "main", "commit": git_commit(tools_dir)},
         "notes": notes or _notes_from_changelog(tools_dir) or old.get("notes") or ""}
    p = os.path.join(tools_dir or HERE, "_version.json")
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    log("  写好 %s：版本 %s · commit %s · exe %.1f MB" % (p, d["version"], d["commit"] or "—",
                                                        ent["size"] / 1048576.0))
    return d


def repo_behind(tools_dir=None, timeout=25):
    """技能源码落后远端几个提交（**只报不改**；真要拉取走面板上的「拉取」按钮）。

    2026-09-22 修（用户："要能接受到我在 git 仓库上的推送"）：技能仓**没有 origin**
    （只有 gitee / github 两个 remote），老代码 fetch origin 永远失败 → 界面永远显示
    "取不到远端"，推了也收不到。现在按 origin → gitee → github 挑一个存在的；也不再
    `--depth 1`（浅取会把落后数压成 1，推了 3 个只报 1 个），直接全量取（仓库只有几 MB），
    用 FETCH_HEAD 比（不受 refspec 细节影响）。取不到返回 (None, 原因)，界面按行给原因。
    """
    root = skill_root(tools_dir)
    if not (root and os.path.isdir(os.path.join(root, ".git"))):
        return None, "不是 git 工作树"
    def _g(*args):
        return subprocess.run(["git", "-C", root] + list(args), capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=timeout)
    try:
        remotes = (_g("remote").stdout or "").split()
        rem = next((r for r in ("origin", "gitee", "github") if r in remotes), "")
        if not rem:
            return None, "没有配置远程仓库"
        br = ""
        try:
            br = _g("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "main"
        except subprocess.SubprocessError:
            br = "main"
        f = _g("fetch", rem, br)
        if f.returncode != 0:
            return None, "取不到远端（网络？）"
        c = _g("rev-list", "--count", "HEAD..FETCH_HEAD")
        if c.returncode != 0:
            return None, "比不出来"
        return int((c.stdout or "0").strip() or 0), ""
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        return None, str(e)[:80]


def payload(tools_dir=None, urls=None, timeout=12, with_repo=True):
    """给界面用的一份体检：当前版 + 远端版 + 要不要更新 + 体积。只读，不下载不写盘。"""
    cur = current(tools_dir, with_hash=False)
    rem = remote(urls, timeout=timeout)
    res = compare(cur, rem)
    ex = rem.get("exe") or {}
    out = {"ok": True, "current": {"version": cur.get("version"), "commit": cur.get("commit"),
                                   "source": cur.get("source")},
           "remote": {}, "need": bool(res["need"]), "why": res["why"],
           "exe": {"mb": round((ex.get("size") or 0) / 1048576.0, 1), "size": ex.get("size") or 0},
           "repo": {"behind": None, "error": None}}
    if rem:
        out["remote"] = {"version": rem.get("version"), "builtAt": rem.get("builtAt") or "",
                         "notes": rem.get("notes") or "", "sha256": ex.get("sha256") or ""}
    if with_repo:
        n, err = repo_behind(tools_dir)
        out["repo"] = {"behind": n, "error": err or None}
    return out


def main():
    ap = argparse.ArgumentParser(description="版本身份与更新判断")
    ap.add_argument("cmd", nargs="?", default="check",
                    choices=["current", "remote", "check", "compare", "write"])
    ap.add_argument("a", nargs="?")
    ap.add_argument("b", nargs="?")
    ap.add_argument("--version", default="")
    ap.add_argument("--notes", default="")
    a = ap.parse_args()
    if a.cmd == "current":
        _say(json.dumps(current(with_hash=True), ensure_ascii=False, indent=2))
    elif a.cmd == "remote":
        r = remote()
        _say(json.dumps(r, ensure_ascii=False, indent=2) if r else "取不到远端 version.json（离线？）")
        return 0 if r else 1
    elif a.cmd == "check":
        r = payload()
        _say("当前 %s（%s）· %s · %s" % (r["current"]["version"], r["current"]["commit"] or "—",
                                        r["current"]["source"], r["why"]))
        if r["need"]:
            _say("要更新：约 %.1f MB，更新说明：%s" % (r["exe"]["mb"],
                                                    (r["remote"] or {}).get("notes") or "—"))
        return 0
    elif a.cmd == "compare":
        if not (a.a and a.b):
            _say("用法：python tools\\版本.py compare 2.9 2.10")
            return 2
        c = cmp_version(a.a, a.b)
        _say("%s %s %s" % (a.a, ">" if c > 0 else ("<" if c < 0 else "=="), a.b))
    elif a.cmd == "write":
        write_local(version=a.version, notes=a.notes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
