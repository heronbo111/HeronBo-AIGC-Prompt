# -*- coding: utf-8 -*-
r"""统一清理两平台发行版：只留完整安装包 + skill 内核 score-tool.exe，第三方依赖大件全删。

GitHub（release tag vendor-2026-09-17）：
  保留 完整 setup v0.4.0、score-tool.exe；删除 depth-model / ffmpeg / wheels-heavy。
Gitee（无法托管 >100MB 完整包）：
  保留 release v0.4.0（score-tool.exe + 正文引导 GitHub）；删除其余旧 release。
令牌向 git credential fill 要，不打印。
"""
import json
import subprocess
import urllib.error
import urllib.parse
import urllib.request


def cred(host):
    p = subprocess.run(["git", "credential", "fill"], capture_output=True, text=True,
                       input="protocol=https\nhost=%s\n\n" % host)
    for line in (p.stdout or "").splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1].strip()
    return ""


def call(url, method="GET", tok=None, tok_in="header"):
    u = url
    if tok and tok_in == "query":
        u = url + ("&" if "?" in url else "?") + urllib.parse.urlencode({"access_token": tok})
    req = urllib.request.Request(u, method=method)
    if tok and tok_in == "header":
        req.add_header("Authorization", "Bearer " + tok)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("User-Agent", "heronbo-rel-clean")
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:300]


def gh_clean():
    print("=== GitHub ===")
    tok = cred("github.com")
    O, R, T = "heronbo111", "HeronBo-AIGC-Prompt", "vendor-2026-09-17"
    api = "https://api.github.com"
    st, rel = call("%s/repos/%s/%s/releases/tags/%s" % (api, O, R, T), tok=tok)
    if st != 200:
        print("读 release 失败", st, rel)
        return
    kept, deleted = [], []
    for a in rel.get("assets") or []:
        n = a.get("name")
        if (n == "score-tool.exe") or ("Setup_v0.4.0" in n):
            kept.append(n)
            continue
        st, b = call("%s/repos/%s/%s/releases/assets/%s" % (api, O, R, a.get("id")),
                     method="DELETE", tok=tok)
        deleted.append((n, st))
    for n, st in deleted:
        print("  删除 %-46s HTTP %s" % (n, st))
    for n in kept:
        print("  保留 %s" % n)


def kee_clean():
    print("=== Gitee ===")
    tok = cred("gitee.com")
    O, R = "HeronBo", "HeronBo-AIGC-Prompt"
    api = "https://gitee.com/api/v5"
    st, rels = call("%s/repos/%s/%s/releases" % (api, O, R), tok=tok, tok_in="query")
    if not isinstance(rels, list):
        print("读 release 失败", st, rels)
        return
    kept, deleted = [], []
    for r in rels:
        tag = r.get("tag_name")
        if tag == "v0.4.0":
            kept.append(tag)
            continue
        st, b = call("%s/repos/%s/%s/releases/%s" % (api, O, R, r.get("id")),
                     method="DELETE", tok=tok, tok_in="query")
        deleted.append((tag, st))
    for tag, st in deleted:
        print("  删除 release %-20s HTTP %s %s" % (tag, st, "" if st in (200, 204) else b))
    for tag in kept:
        print("  保留 release %s（score-tool 内核 + 引导 GitHub 完整包）" % tag)


if __name__ == "__main__":
    gh_clean()
    kee_clean()
    print("=== 完成 ===")
