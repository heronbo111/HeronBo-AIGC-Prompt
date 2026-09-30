# -*- coding: utf-8 -*-
r"""统一发版：为每个产品版本建一个【不可变】vX.Y.Z Release（Gitee + GitHub），替代旧的
"固定 vendor tag 反复覆盖、仓库看不到迭代"。

为什么（2026-09-29，用户："别人的仓库每个版本迭代都能看见，我想让工作台内置检查更新"）：
  · 旧做法把 score-tool.exe / version.json 挂在固定 tag vendor-日期 上每次覆盖 → 没有版本历史；
  · 新做法：每次发版一个 vX.Y.Z，永久保留、带更新说明；平台 releases/latest 自动指向最新一个，
    工作台经 能力包.latest_release() 取最新，已装用户检查更新即可拿到。

分工：
  · 产品本体（score-tool.exe / version.json / 完整 setup）→ 版本化 vX.Y.Z release；
  · 能力包依赖（wheels / ffmpeg，大件）→ 固定 assets 通道（能力包.ASSETS_TAG），不在此重复挂。

安全：
  · 默认【dry-run】，只打印将做什么；加 --apply 才真正外发；
  · tag 已存在默认报错（防覆盖），除非显式 --force；
  · 令牌用 `git credential fill` 取，全程不打印。

用法：
    python tools\发布版本.py --version 0.4.1                 # 预演（不外发）
    python tools\发布版本.py --version 0.4.1 --apply         # 正式发（双平台）
    python tools\发布版本.py --version 0.4.1 --setup "X:\...\Setup_v0.4.1.exe" --apply
"""
import argparse
import json
import mimetypes
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

OWNER_GH, OWNER_GEE, REPO = "heronbo111", "HeronBo", "HeronBo-AIGC-Prompt"
GH_API = "https://api.github.com"
GEE_API = "https://gitee.com/api/v5"

SCORE = os.path.join(HERE, "dist", "score-tool.exe")
LOCAL_VERSION_JSON = os.path.join(HERE, "_version.json")


def log(m):
    print(m, flush=True)


# ---------------- 令牌（不打印） ----------------
def credential(host):
    p = subprocess.run(
        ["git", "credential", "fill"], capture_output=True, text=True,
        input="protocol=https\nhost=%s\n\n" % host,
        encoding="utf-8", errors="replace", timeout=30)
    for line in (p.stdout or "").splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1].strip()
    return ""


# ---------------- 本地元数据 ----------------
def prepare_local(version, notes):
    """生成与将发布一致的 _version.json（版本号 + 当前 dist 内核的 sha256）。返回 (meta, version.json 路径)。"""
    import 版本 as ver
    meta = ver.write_local(version=version, notes=notes)
    return meta, LOCAL_VERSION_JSON


def make_body(version, notes, has_setup, setup_name):
    lines = ["HeronBo 视频工作台 v%s" % version, ""]
    if notes:
        lines += [notes, ""]
    lines += ["【如何更新 / 安装】",
              "· 已安装：打开工作台 →「检查更新」，自动拉取本版本内核并重启；",
              "· 本版本附件：score-tool.exe（工作台内核）、version.json（版本元数据）。"]
    if has_setup:
        lines.append("· 新安装：下载本页完整安装包 %s（双击即装、依赖全封装）。" % setup_name)
    else:
        lines.append("· 新安装需完整安装包：Gitee 单附件限 100MB，完整包仅放在 GitHub Release。")
    return "\n".join(lines)


# ---------------- GitHub ----------------
def gh_request(url, tok, method="GET", data=None, ctype="application/json", timeout=600):
    req = urllib.request.Request(url, method=method, data=data)
    req.add_header("Authorization", "Bearer " + tok)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "heronbo-rel")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data is not None:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:400]


def gh_tag_exists(tag, tok):
    st, _b = gh_request("%s/repos/%s/%s/releases/tags/%s" % (GH_API, OWNER_GH, REPO, tag),
                        tok, timeout=60)
    return st == 200


def gh_asset_name(name):
    """GitHub 资产名只允许 ASCII 字母数字和 -_.，中文会被替换成 ._ 之类的乱码。
    上传前统一清洗为 ASCII（非合法字符替换为 -），保证下载文件名可读。"""
    base, ext = os.path.splitext(name)
    safe = "".join(c if (c.isascii() and (c.isalnum() or c in "-_.")) else "-" for c in base)
    safe = safe.strip("-.") or "asset"
    while "--" in safe:
        safe = safe.replace("--", "-")
    return safe + (ext.lower() if ext.isascii() else ".bin")


def gh_publish(tag, target, body, files, force):
    tok = credential("github.com")
    if not tok:
        return "拿不到 GitHub 令牌（git credential fill）"
    if gh_tag_exists(tag, tok):
        if not force:
            return "GitHub 已存在 %s（不可变，拒绝覆盖；确需重发请加 --force）" % tag
        log("  [GitHub] %s 已存在，--force 模式：复用并刷新附件" % tag)
        st, rel = gh_request("%s/repos/%s/%s/releases/tags/%s" % (GH_API, OWNER_GH, REPO, tag), tok)
        rid = rel["id"]
    else:
        payload = json.dumps({"tag_name": tag, "target_commitish": target,
                              "name": "HeronBo 视频工作台 %s" % tag,
                              "body": body, "prerelease": False, "draft": False}).encode()
        st, rel = gh_request("%s/repos/%s/%s/releases" % (GH_API, OWNER_GH, REPO),
                             tok, method="POST", data=payload, timeout=60)
        if st not in (200, 201):
            return "GitHub 创建 release 失败 HTTP%s %s" % (st, rel)
        rid = rel["id"]
        log("  [GitHub] 已创建 release id=%s" % rid)
    for path in files:
        name = gh_asset_name(os.path.basename(path))
        # 同名旧附件先删（幂等；不可变版本正常不会有）
        st, rel = gh_request("%s/repos/%s/%s/releases/%s" % (GH_API, OWNER_GH, REPO, "tags/"+tag), tok)
        for a in (rel.get("assets") or []) if isinstance(rel, dict) else []:
            if a.get("name") == name:
                gh_request("%s/repos/%s/%s/releases/assets/%s" % (GH_API, OWNER_GH, REPO, a["id"]),
                           tok, method="DELETE", timeout=60)
        with open(path, "rb") as f:
            blob = f.read()
        url = "https://uploads.github.com/repos/%s/%s/releases/%s/assets?name=%s" % (
            OWNER_GH, REPO, rid, urllib.parse.quote(name))
        log("  [GitHub] 上传 %s（%.1f MB）…" % (name, os.path.getsize(path) / 1048576.0))
        last_err = ""
        ok = False
        for attempt in (1, 2, 3):                              # 大文件上传易被网络中断，重试兜底
            try:
                st, b = gh_request(url, tok, method="POST", data=blob,
                                   ctype="application/octet-stream", timeout=1800)
                if st in (200, 201):
                    ok = True
                    break
                last_err = "HTTP%s %s" % (st, str(b)[:150])
            except Exception as e:                              # noqa: BLE001
                last_err = str(e)[:200]
            log("    上传第%d次失败（%s），重试…" % (attempt, last_err))
        if not ok:
            return "GitHub 上传 %s 失败 %s" % (name, last_err)
    return ""


# ---------------- Gitee ----------------
def gee_get(url, timeout=60):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def gee_post_form(url, fields, timeout=60):
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def gee_attach(url, tok, name, path):
    boundary = "----" + uuid.uuid4().hex
    crlf = "\r\n"
    out = b""
    for k, v in (("access_token", tok), ("name", name)):
        out += ('--%s%sContent-Disposition: form-data; name="%s"%s%s%s%s'
                % (boundary, crlf, k, crlf, crlf, v, crlf)).encode()
    with open(path, "rb") as f:
        blob = f.read()
    ct = mimetypes.guess_type(name)[0] or "application/octet-stream"
    out += ('--%s%sContent-Disposition: form-data; name="file"; filename="%s"%s'
            'Content-Type: %s%s%s' % (boundary, crlf, name, crlf, ct, crlf, crlf)).encode()
    out += blob + ("%s--%s--%s" % (crlf, boundary, crlf)).encode()
    req = urllib.request.Request(url, data=out, method="POST")
    req.add_header("Content-Type", "multipart/form-data; boundary=" + boundary)
    with urllib.request.urlopen(req, timeout=1800) as r:
        return json.loads(r.read() or b"{}")


def gee_publish(tag, target, body, files, force):
    tok = credential("gitee.com")
    if not tok:
        return "拿不到 Gitee 令牌（git credential fill）"
    base = "%s/repos/%s/%s" % (GEE_API, OWNER_GEE, REPO)
    q = urllib.parse.urlencode({"access_token": tok, "page": 1, "per_page": 100})
    rels = gee_get(base + "/releases?" + q)
    hit = next((x for x in rels if x.get("tag_name") == tag), None)
    if hit:
        if not force:
            return "Gitee 已存在 %s（不可变，拒绝覆盖；确需重发请加 --force）" % tag
        rid = hit["id"]
        log("  [Gitee] %s 已存在，--force 模式：补传缺失附件" % tag)
    else:
        hit = gee_post_form(base + "/releases", {
            "access_token": tok, "tag_name": tag, "target_commitish": target,
            "name": "HeronBo 视频工作台 %s" % tag, "body": body, "prerelease": "false"})
        if not hit.get("id"):
            return "Gitee 创建 release 失败：%s" % str(hit)[:200]
        rid = hit["id"]
        log("  [Gitee] 已创建 release id=%s" % rid)
    detail = gee_get(base + "/releases/%s?%s" % (rid, urllib.parse.urlencode({"access_token": tok})))
    have = {a.get("name") for a in (detail.get("assets") or [])}
    for path in files:
        name = os.path.basename(path)
        if os.path.getsize(path) > 100 * 1048576:
            log("  [Gitee] 跳过 %s（%.1f MB 超单附件 100MB 上限；请到 GitHub 取）"
                % (name, os.path.getsize(path) / 1048576.0))
            continue
        if name in have:
            log("  [Gitee] %s 已在附件，跳过" % name)
            continue
        log("  [Gitee] 上传 %s（%.1f MB）…" % (name, os.path.getsize(path) / 1048576.0))
        res = gee_attach(base + "/releases/%s/attach_files" % rid, tok, name, path)
        if not (res.get("browser_download_url") or res.get("url")):
            return "Gitee 上传 %s 失败：%s" % (name, str(res)[:200])
    return ""


# ---------------- 匿名直链验证 ----------------
def verify_links(tag, files):
    import 下载器 as dl
    ok = True
    for path in files:
        name = os.path.basename(path)
        if os.path.getsize(path) > 100 * 1048576:
            continue
        for url in ("https://gitee.com/%s/%s/releases/download/%s/%s" % (OWNER_GEE, REPO, tag, name),
                    "https://github.com/%s/%s/releases/download/%s/%s" % (OWNER_GH, REPO, tag, name)):
            try:
                size, _lm = dl.remote_info(url, timeout=25)
                log("  验链 %s %s → %d 字节" % ("Gitee" if "gitee" in url else "GitHub", name, size))
            except Exception as e:                                  # noqa: BLE001
                log("  验链失败 %s %s：%s" % ("Gitee" if "gitee" in url else "GitHub", name, str(e)[:80]))
                ok = False
    return ok


def current_branch():
    r = subprocess.run(["git", "-C", HERE, "rev-parse", "--abbrev-ref", "HEAD"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    br = (r.stdout or "").strip()
    return br if br and r.returncode == 0 else "main"


def main():
    ap = argparse.ArgumentParser(description="统一发布不可变版本 vX.Y.Z（默认 dry-run）")
    ap.add_argument("--version", required=True, help="产品版本号 X.Y.Z（tag 自动加 v 前缀）")
    ap.add_argument("--setup", default="", help="完整安装包路径（可选；仅 GitHub，Gitee 超 100MB 不传）")
    ap.add_argument("--target", default="", help="release 指向的分支（默认当前分支）")
    ap.add_argument("--notes", default="", help="更新说明（默认取 CHANGELOG 顶部）")
    ap.add_argument("--apply", action="store_true", help="真正外发（默认只预演）")
    ap.add_argument("--force", action="store_true", help="tag 已存在也继续（默认拒绝覆盖）")
    a = ap.parse_args()

    version = a.version.strip().lstrip("vV")
    tag = "v" + version
    if not all(p.isdigit() for p in version.split(".")):
        log("版本号应为 X.Y.Z 数字段，收到：%s" % version)
        return 2
    target = a.target or current_branch()

    if not os.path.isfile(SCORE):
        log("找不到内核 %s——请先构建 score-tool.exe（build_exe）" % SCORE)
        return 3
    setup = a.setup.strip()
    if setup and not os.path.isfile(setup):
        log("找不到完整安装包：%s" % setup)
        return 3

    # 1) 本地元数据（dry-run 也生成，便于核对；与将发布一致）
    meta, vj = prepare_local(version, a.notes)
    body = make_body(version, meta.get("notes") or "", bool(setup),
                     gh_asset_name(os.path.basename(setup)) if setup else "")

    # 2) 上传清单（产品本体）
    files = [SCORE, vj]
    if setup:
        files.append(setup)

    log("== 发版预演 %s（target=%s） ==" % (tag, target))
    log("将上传：")
    for f in files:
        note = "（Gitee 将跳过，超 100MB）" if os.path.getsize(f) > 100 * 1048576 else ""
        log("  · %-22s %8.2f MB %s" % (os.path.basename(f), os.path.getsize(f) / 1048576.0, note))
    log("端点：")
    log("  GitHub POST %s/repos/%s/%s/releases → uploads.github.com 附件" % (GH_API, OWNER_GH, REPO))
    log("  Gitee  POST %s/repos/%s/%s/releases → /releases/{id}/attach_files" % (GEE_API, OWNER_GEE, REPO))

    if not a.apply:
        log("\n这是预演，未外发。确认无误后加 --apply 正式发布。")
        return 0

    log("\n== 正式发布 ==")
    err = gh_publish(tag, target, body, files, a.force)
    if err:
        log("[GitHub] " + err)
        return 4
    err = gee_publish(tag, target, body, files, a.force)
    if err:
        log("[Gitee] " + err)
        return 5

    log("\n== 验证匿名直链 ==")
    verify_links(tag, files)
    log("\nGitHub：https://%s/%s/releases/tag/%s" % ("github.com", OWNER_GH + "/" + REPO, tag))
    log("Gitee ：https://%s/%s/releases/tag/%s" % ("gitee.com", OWNER_GEE + "/" + REPO, tag))
    log("平台 releases/latest 现应指向 %s；已装用户「检查更新」即可发现。" % tag)
    return 0


if __name__ == "__main__":
    sys.exit(main())
