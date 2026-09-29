# -*- coding: utf-8 -*-
r"""在 GitHub 新建 release v0.4.0（指向 main HEAD），上传完整 setup + score-tool。
本脚本不删旧 vendor-2026-09-17；核对新 release 后再单独删除。令牌不打印。"""
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

OWNER, REPO = "heronbo111", "HeronBo-AIGC-Prompt"
API = "https://api.github.com"
TAG = "v0.4.0"
SCORE = r"C:\Users\WUKONG\.zcode\skills\HeronBo-AIGC-Prompt\tools\dist\score-tool.exe"
SETUP = r"C:\Users\WUKONG\DoubaoWork\chats\2026-09-28\new-chat\HeronBo视频工作台_完整版_Setup_v0.4.0.exe"
BODY = """HeronBo 视频工作台 v0.4.0

【新功能】
- 工作台新增「让豆包生成视频」按钮：提示词+首帧+音色经 CDP 在豆包对话调内置 Seedance + 声音工具，画面/配音分头生成、本地 ffmpeg 合成；模型/画幅/时长可选，提交前弹窗确认
- 驱动窗口全程静默最小化、按 /chat/<sid> 锁定不串台
- 三通道融合 cdp / plugin / mcp
- 估时只按台词（双节台词 72 单位、建议 14s）

【附件】
- HeronBo视频工作台_完整版_Setup_v0.4.0.exe：完整安装包（约 792MB，双击即装、免环境、依赖全封装）
- score-tool.exe：skill 内核（约 18MB）
"""


def token():
    p = subprocess.run(["git", "credential", "fill"], capture_output=True, text=True,
                       input="protocol=https\nhost=github.com\n\n")
    for line in (p.stdout or "").splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1].strip()
    return ""


def req(url, tok, method="GET", data=None, ctype="application/json", timeout=1800):
    r = urllib.request.Request(url, method=method, data=data)
    r.add_header("Authorization", "Bearer " + tok)
    r.add_header("Accept", "application/vnd.github+json")
    r.add_header("User-Agent", "heronbo-rel")
    if data is not None:
        r.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:300]


def upload(rid, tok, path):
    name = os.path.basename(path)
    st, rel = req("%s/repos/%s/%s/releases/tags/%s" % (API, OWNER, REPO, TAG), tok)
    for a in rel.get("assets") or []:
        if a.get("name") == name:
            req("%s/repos/%s/%s/releases/assets/%s" % (API, OWNER, REPO, a.get("id")),
                tok, method="DELETE")
    size = os.path.getsize(path)
    print("  上传 %s（%.2f MB）…" % (name, size / 1048576.0))
    with open(path, "rb") as f:
        blob = f.read()
    url = "https://uploads.github.com/repos/%s/%s/releases/%s/assets?name=%s" % (
        OWNER, REPO, rid, urllib.parse.quote(name))
    st, body = req(url, tok, method="POST", data=blob, ctype="application/octet-stream")
    print("   -> HTTP %s %s" % (st, body.get("browser_download_url") if isinstance(body, dict) else body))
    return st in (200, 201)


def main():
    tok = token()
    st, rel = req("%s/repos/%s/%s/releases/tags/%s" % (API, OWNER, REPO, TAG), tok, timeout=60)
    if st == 200:
        rid = rel["id"]
        print("release %s 已存在 id=%s" % (TAG, rid))
    else:
        print("创建 release %s …" % TAG)
        payload = json.dumps({"tag_name": TAG, "target_commitish": "main",
                              "name": "HeronBo 视频工作台 v0.4.0", "body": BODY,
                              "prerelease": False}).encode()
        st, rel = req("%s/repos/%s/%s/releases" % (API, OWNER, REPO), tok,
                      method="POST", data=payload, timeout=60)
        if st not in (200, 201):
            print("创建失败", st, rel)
            return 4
        rid = rel["id"]
        print("已创建 id=%s" % rid)

    ok1 = upload(rid, tok, SCORE)
    ok2 = upload(rid, tok, SETUP)
    print("\n新 release：https://github.com/%s/%s/releases/tag/%s" % (OWNER, REPO, TAG))
    print("score OK=%s  setup OK=%s（核对后再删旧 vendor-2026-09-17）" % (ok1, ok2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
