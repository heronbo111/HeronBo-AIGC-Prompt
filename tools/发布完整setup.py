# -*- coding: utf-8 -*-
r"""把完整 setup 安装包上传到 GitHub Release（vendor tag），附件名＝文件真实名；同名先删后传。

与 发布exe附件.py 的区别：那个固定上传 score-tool.exe（vendor 内核），本脚本上传
~792MB 的完整 setup（双击即装、免环境），用于把 Release 的主交付换成完整安装包。
凭据同样向 git credential fill 要，token 只在内存、不打印不落盘。

用法：
    python tools\发布完整setup.py --dry
    python tools\发布完整setup.py --file "<完整 setup.exe>"
"""
import argparse
import glob
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DELIVER_DIR = r"C:\Users\WUKONG\DoubaoWork\chats\2026-09-28\new-chat"
OWNER, REPO = "heronbo111", "HeronBo-AIGC-Prompt"
TAG = "vendor-2026-09-17"
API = "https://api.github.com"


def token():
    p = subprocess.run(["git", "credential", "fill"], capture_output=True, text=True,
                       input="protocol=https\nhost=github.com\n\n")
    if p.returncode != 0:
        return None
    for line in (p.stdout or "").splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1].strip()
    return None


def req(url, tok, method="GET", data=None, ctype="application/json"):
    r = urllib.request.Request(url, method=method, data=data)
    r.add_header("Authorization", "Bearer %s" % tok)
    r.add_header("Accept", "application/vnd.github+json")
    r.add_header("User-Agent", "heronbo-release")
    if data is not None:
        r.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(r, timeout=1200) as resp:
            body = resp.read()
            return resp.status, json.loads(body or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:400]
    except Exception as e:                                        # noqa: BLE001
        return 0, str(e)


def auto_pick():
    cands = glob.glob(os.path.join(DELIVER_DIR, "HeronBo视频工作台_完整版_Setup_v0.4.0*.exe"))
    cands = [c for c in cands if os.path.isfile(c)]
    if not cands:
        return ""
    return max(cands, key=os.path.getmtime)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--file", default="")
    a = ap.parse_args()

    up = os.path.abspath(a.file or auto_pick())
    if not up or not os.path.isfile(up):
        print("!! 找不到完整 setup，用 --file 指定")
        return 2
    asset_name = os.path.basename(up)
    local = os.path.getsize(up)
    print("要上传：%s（%.2f MB）" % (up, local / 1048576.0))

    tok = token()
    if not tok:
        print("!! 拿不到 GitHub 凭据，先手动 push 一次让它记住")
        return 3

    st, rel = req("%s/repos/%s/%s/releases/tags/%s" % (API, OWNER, REPO, TAG), tok)
    if st != 200:
        print("!! 读 Release 失败 HTTP %s：%s" % (st, rel))
        return 4
    rid = rel.get("id")
    assets = rel.get("assets") or []
    print("Release（id=%s，%d 个附件），现有：" % (rid, len(assets)))
    old = None
    for x in assets:
        mark = ""
        if x.get("name") == asset_name:
            old = x
            mark = "  ← 同名，将被替换"
        print("   %-46s %10.2f MB%s" % (x.get("name"), (x.get("size") or 0) / 1048576.0, mark))

    if a.dry:
        print("\n[dry] 到此为止")
        return 0

    if old:
        print("\n[1/2] 删除同名旧附件 id=%s …" % old.get("id"))
        st, body = req("%s/repos/%s/%s/releases/assets/%s" % (API, OWNER, REPO, old.get("id")),
                       tok, method="DELETE")
        if st not in (204, 200):
            print("    删除失败 HTTP %s：%s" % (st, body))
            return 6
        print("    已删除")
    else:
        print("\n[1/2] 无同名附件，直接上传")

    print("[2/2] 上传完整 setup（%.2f MB，可能要 3–8 分钟）…" % (local / 1048576.0))
    with open(up, "rb") as f:
        blob = f.read()
    q = urllib.parse.quote(asset_name)
    url = "https://uploads.github.com/repos/%s/%s/releases/%s/assets?name=%s" % (
        OWNER, REPO, rid, q)
    st, body = req(url, tok, method="POST", data=blob, ctype="application/octet-stream")
    if st not in (200, 201):
        print("!! 上传失败 HTTP %s：%s" % (st, body))
        return 7
    print("    上传成功：%s（%.2f MB）" % (body.get("browser_download_url"),
                                        (body.get("size") or 0) / 1048576.0))
    print("\n完成。Release 现提供完整 setup 双击安装。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
