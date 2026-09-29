# -*- coding: utf-8 -*-
r"""在 Gitee 建 v0.4.0 发行版并上传 score-tool 内核；完整 setup 因 Gitee 单附件 100MB
上限无法托管，在正文引导到 GitHub Release 下载。令牌向 git credential fill 要，不打印。"""
import json
import mimetypes
import os
import subprocess
import sys
import urllib.parse
import urllib.request
import uuid

OWNER, REPO = "HeronBo", "HeronBo-AIGC-Prompt"
API = "https://gitee.com/api/v5"
SCORE = r"C:\Users\WUKONG\.zcode\skills\HeronBo-AIGC-Prompt\tools\dist\score-tool.exe"
TAG = "v0.4.0"
GH_PAGE = "https://github.com/heronbo111/HeronBo-AIGC-Prompt/releases/tag/vendor-2026-09-17"

BODY = """HeronBo 视频工作台 v0.4.0

【新功能】
- 工作台新增「让豆包生成视频」按钮：提示词+首帧+音色经 CDP 在豆包对话调内置 Seedance + 声音工具，画面/配音分头生成、本地 ffmpeg 合成；模型/画幅/时长可选，提交前弹窗确认
- 驱动窗口全程静默最小化、按 /chat/<sid> 锁定不串台
- 三通道融合 cdp / plugin / mcp
- 估时只按台词（双节台词 72 单位、建议 14s）

【附件】
- score-tool.exe：vendor 内核（约 18MB），配合 tools/deploy.py 使用

【完整安装包（约 792MB，双击即装、免环境、依赖全封装）】
Gitee 发行版单附件上限 100MB，无法托管完整包，请到 GitHub Release 下载：
%S
""" .replace("%S", GH_PAGE)


def token():
    p = subprocess.run(["git", "credential", "fill"], capture_output=True, text=True,
                       input="protocol=https\nhost=gitee.com\n\n")
    for line in (p.stdout or "").splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1].strip()
    return ""


def get_json(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.loads(r.read() or b"[]")


def post_form(url, fields):
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read() or b"{}")


def upload(url, tok, name, file_path):
    boundary = "----" + uuid.uuid4().hex
    crlf = "\r\n"
    out = b""
    for k, v in (("access_token", tok), ("name", name)):
        out += ('--%s%sContent-Disposition: form-data; name="%s"%s%s%s'
                % (boundary, crlf, k, crlf, crlf, v) + crlf).encode()
    with open(file_path, "rb") as f:
        blob = f.read()
    ct = mimetypes.guess_type(name)[0] or "application/octet-stream"
    out += ('--%s%sContent-Disposition: form-data; name="file"; filename="%s"%s'
            'Content-Type: %s%s%s' % (boundary, crlf, name, crlf, ct, crlf, crlf)).encode()
    out += blob + ("%s--%s--%s" % (crlf, boundary, crlf)).encode()
    req = urllib.request.Request(url, data=out, method="POST")
    req.add_header("Content-Type", "multipart/form-data; boundary=" + boundary)
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read() or b"{}")


def main():
    tok = token()
    if not tok:
        print("!! 拿不到 Gitee 令牌")
        return 3
    base = "%s/repos/%s/%s" % (API, OWNER, REPO)
    q = urllib.parse.urlencode({"access_token": tok, "page": 1, "per_page": 50})
    rels = get_json(base + "/releases?" + q)
    hit = next((x for x in rels if x.get("tag_name") == TAG), None)
    if hit:
        rid = hit["id"]
        print("发行版已存在 id=%s" % rid)
    else:
        print("创建发行版 tag=%s …" % TAG)
        hit = post_form(base + "/releases", {
            "access_token": tok, "tag_name": TAG, "target_commitish": "main",
            "name": "HeronBo 视频工作台 v0.4.0", "body": BODY, "prerelease": "false"})
        rid = hit["id"]
        print("已创建 id=%s" % rid)

    detail = get_json(base + "/releases/%s?%s" % (rid, urllib.parse.urlencode({"access_token": tok})))
    names = [a.get("name") for a in (detail.get("assets") or [])]
    if "score-tool.exe" in names:
        print("score-tool.exe 已在附件，跳过")
    else:
        print("上传 score-tool.exe（%.2f MB）…" % (os.path.getsize(SCORE) / 1048576.0))
        res = upload(base + "/releases/%s/attach_files" % rid, tok, "score-tool.exe", SCORE)
        print("上传结果：%s" % (res.get("browser_download_url") or res))
    print("\nGitee 发行版：https://gitee.com/%s/%s/releases/%s" % (OWNER, REPO, rid))
    print("完整包请走 GitHub：%s" % GH_PAGE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
