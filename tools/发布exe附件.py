# -*- coding: utf-8 -*-
r"""把最新的工作台 exe 换到 GitHub Release 附件里（vendor-* 那个 Release）。

为什么要它：仓库只放代码（exe 不进 git），同事靠 `python tools\deploy.py vendor --fetch`
从 Release 附件拉 exe。所以**每打一次 exe，附件也得跟着换**，否则同事永远拿到旧界面。

怎么拿凭据：git push 能成说明本机已存 GitHub 凭据（Windows 凭据管理器 / GCM）。
这里用 `git credential fill` 向 git 要一份——**token 只在这个进程的内存里，不落盘、不打印**。
（这也是为什么本脚本可以安全地放在仓库里。）

用法：
    python tools\发布exe附件.py --dry        # 只看现状：Release 里现在是什么、本地是什么
    python tools\发布exe附件.py              # 真换（删旧附件 → 传新附件）
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
EXE = os.path.join(HERE, "dist", "score-tool.exe")
OWNER, REPO = "heronbo111", "HeronBo-AIGC-Prompt"
TAG = "vendor-2026-09-17"
ASSET = "score-tool.exe"
API = "https://api.github.com"


def token():
    """向 git 要 GitHub 凭据（不打印、不落盘）。"""
    p = subprocess.run(["git", "credential", "fill"], capture_output=True, text=True,
                       input="protocol=https\nhost=github.com\n\n")
    if p.returncode != 0:
        return None
    for line in (p.stdout or "").splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1].strip()
    return None


def req(url, tok, method="GET", data=None, ctype="application/json", raw=False):
    r = urllib.request.Request(url, method=method, data=data)
    r.add_header("Authorization", "Bearer %s" % tok)
    r.add_header("Accept", "application/vnd.github+json")
    r.add_header("User-Agent", "heronbo-release")
    if data is not None:
        r.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(r, timeout=600) as resp:
            body = resp.read()
            return resp.status, (body if raw else json.loads(body or b"{}"))
    except urllib.error.HTTPError as e:
        return e.code, (e.read().decode("utf-8", "replace")[:400])
    except Exception as e:                                        # noqa: BLE001
        return 0, str(e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只看现状，不改")
    ap.add_argument("--file", default=EXE, help="要上传哪个 exe（默认 tools/dist/score-tool.exe）")
    ap.add_argument("--only-upload", action="store_true",
                    help="附件已被删掉时用：跳过删除，直接补传")
    a = ap.parse_args()

    up = os.path.abspath(a.file)
    if not os.path.isfile(up):
        print("!! 找不到 %s" % up)
        return 2
    local = os.path.getsize(up)
    print("要上传的 exe：%s（%.2f MB）" % (up, local / 1048576.0))

    tok = token()
    if not tok:
        print("!! 拿不到 GitHub 凭据（git credential fill 没给）。")
        print("   办法：先手动 push 一次让它记住凭据，或装 gh 后 gh auth login 再改用 gh。")
        return 3

    st, rel = req("%s/repos/%s/%s/releases/tags/%s" % (API, OWNER, REPO, TAG), tok)
    if st != 200:
        print("!! 读 Release 失败 HTTP %s：%s" % (st, rel))
        return 4
    rid = rel.get("id")
    assets = rel.get("assets") or []
    print("Release：%s（id=%s，%d 个附件）" % (rel.get("name") or TAG, rid, len(assets)))
    old = None
    for x in assets:
        mark = ""
        if x.get("name") == ASSET:
            old = x
            mark = "  ← 就是它"
        print("   %-34s %10.2f MB  %s%s" % (x.get("name"), (x.get("size") or 0) / 1048576.0,
                                           x.get("created_at", "")[:19], mark))
    if not old:
        print("（该 Release 里当前没有 %s 附件——按补传处理）" % ASSET)
    if old and old.get("size") == local:
        print("\n附件与本地大小一致，可能已经是最新的（仍可再传一次覆盖）。")

    if a.dry:
        print("\n[dry] 到此为止。")
        return 0

    if a.only_upload or not old:
        print("\n[1/2] 跳过删除（补传模式）")
        old = None
    else:
        print("\n[1/2] 删除旧附件 id=%s …" % old.get("id"))
    if old:
        st, body = req("%s/repos/%s/%s/releases/assets/%s" % (API, OWNER, REPO,
                                                              old.get("id")),
                       tok, method="DELETE")
        if st not in (204, 200):
            print("    删除失败 HTTP %s：%s" % (st, body))
            return 6
        print("    已删除")

    print("[2/2] 上传新附件（%.2f MB，可能要 1–3 分钟）…" % (local / 1048576.0))
    with open(up, "rb") as f:
        blob = f.read()
    url = ("https://uploads.github.com/repos/%s/%s/releases/%s/assets?name=%s"
           % (OWNER, REPO, rid, ASSET))
    st, body = req(url, tok, method="POST", data=blob, ctype="application/octet-stream")
    if st not in (200, 201):
        print("!! 上传失败 HTTP %s：%s" % (st, body))
        print("   注意：旧附件已经删了，请立刻重跑本脚本把新附件补上。")
        return 7
    print("    上传成功：%s（%.2f MB）"
          % (body.get("browser_download_url"),
             (body.get("size") or 0) / 1048576.0))
    print("\n完成。同事现在跑 python tools\\deploy.py vendor --fetch 就会拿到新 exe。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
