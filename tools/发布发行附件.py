# -*- coding: utf-8 -*-
r"""发布发行附件：把 `_release\<tag>\` 里的东西同时挂到 GitHub 与 Gitee 的发行版上。

为什么要它（2026-09-22，用户："我最想解决的就是安装慢的核心问题" / "把 300mb 放到 gitee
会不会更快"）：新机装的时候大件走 Release 附件。GitHub 直连在国内**经常一个字节都下不来**
（本机实测 0 字节），Gitee 是国内 CDN、速度稳，还能当"保底源"——
所以两边都挂，`部署.py` 按 Gitee → 直连 → 镜像的顺序自己挑。

凭据：跟 `发布exe附件.py` 同一套——`git credential fill` 向 git 要（Windows 凭据管理器里
push 能成的那份）。**token 只在内存里，不打印、不落盘**，所以本脚本能进公开仓库。

用法：
    python tools\发布发行附件.py --dry                 # 只看：两边现在有什么、本地要传什么
    python tools\发布发行附件.py --hosts gitee        # 只传 Gitee
    python tools\发布发行附件.py                      # 两边都传（外发动作，用户同意后再跑）
"""
import argparse
import json
import mimetypes
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
GH = {"owner": "heronbo111", "repo": "HeronBo-AIGC-Prompt", "api": "https://api.github.com",
      "host": "github.com", "label": "GitHub"}
GT = {"owner": "HeronBo", "repo": "HeronBo-AIGC-Prompt", "api": "https://gitee.com/api/v5",
      "host": "gitee.com", "label": "Gitee"}
DEFAULT_TAG = "vendor-2026-09-22"


def token(host):
    """向 git 要凭据（不打印、不落盘）。"""
    try:
        p = subprocess.run(["git", "credential", "fill"], capture_output=True, text=True,
                           input="protocol=https\nhost=%s\n\n" % host, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    for line in (p.stdout or "").splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1].strip()
    return None


def _req(url, method="GET", data=None, ctype="application/json", tok=None, timeout=300):
    req = urllib.request.Request(url, method=method, data=data)
    if ctype:
        req.add_header("Content-Type", ctype)
    req.add_header("User-Agent", "heronbo-release")
    if tok:
        req.add_header("Authorization", "token %s" % tok)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        return e.code, body
    return 200, body


def multipart(fields, files):
    """极简 multipart/form-data 编码（不引第三方库）。files = [(field, path)]。"""
    boundary = "----heronbo" + uuid.uuid4().hex
    out = bytearray()
    for k, v in fields.items():
        out += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                % (boundary, k, v)).encode("utf-8")
    for field, path in files:
        name = os.path.basename(path)
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        out += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
                "Content-Type: %s\r\n\r\n" % (boundary, field, name, ctype)).encode("utf-8")
        with open(path, "rb") as f:
            out += f.read()
        out += b"\r\n"
    out += ("--%s--\r\n" % boundary).encode("utf-8")
    return bytes(out), "multipart/form-data; boundary=%s" % boundary


def mb(p):
    return os.path.getsize(p) / 1048576.0


# ── GitHub ─────────────────────────────────────────────────────────────────
def _json(txt):
    """宽容解析：Gitee 有的接口"找不到"会给 200 + 一个 null，不能当成功。"""
    try:
        d = json.loads(txt or "null")
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def gh_release(host, tag, body, tok, dry=False):
    url = "%s/repos/%s/%s/releases" % (host["api"], host["owner"], host["repo"])
    code, txt = _req("%s/tags/%s" % (url, tag), tok=tok)
    rel = _json(txt) if code == 200 else None
    if rel and rel.get("id"):
        return rel
    if dry:
        return {"id": None, "assets": [], "_would_create": True}
    payload = json.dumps({"tag_name": tag, "name": tag, "body": body,
                          "draft": False, "prerelease": False}).encode("utf-8")
    code, txt = _req(url, method="POST", data=payload, tok=tok)
    if code not in (200, 201):
        raise SystemExit("GitHub 建发行版失败（%s）：%s" % (code, txt[:300]))
    return json.loads(txt)


def gh_upload(host, rel, path, tok, log=print):
    name = os.path.basename(path)
    size = os.path.getsize(path)
    for a in rel.get("assets") or []:
        if a.get("name") != name:
            continue
        if a.get("size") == size and size > 1024 * 1024:
            # **只有大件（>1MB）才按大小跳**（2026-09-22 补刀：version.json/SHA256SUMS 这类
            # 文本"内容变了但字节数一样"，按大小跳会把改过的元数据错跳掉 → 面板永远读到旧的）
            log("      （%s 已经在远端且大小一致（大件）→ 跳过）" % name)
            return
        _req("%s/repos/%s/%s/releases/assets/%s" % (host["api"], host["owner"], host["repo"], a["id"]),
             method="DELETE", tok=tok)
    url = ("https://uploads.github.com/repos/%s/%s/releases/%s/assets?name=%s"
           % (host["owner"], host["repo"], rel["id"], urllib.parse.quote(name)))
    data = open(path, "rb").read()
    code, txt = _req(url, method="POST", data=data, ctype="application/octet-stream", tok=tok)
    if code not in (200, 201):
        raise SystemExit("GitHub 传 %s 失败（%s）：%s" % (name, code, txt[:200]))


# ── Gitee ──────────────────────────────────────────────────────────────────
def gt_release(host, tag, body, tok, dry=False, recreate=False):
    url = "%s/repos/%s/%s/releases/tags/%s" % (host["api"], host["owner"], host["repo"], tag)
    code, txt = _req("%s?access_token=%s" % (url, tok))
    rel = _json(txt) if code == 200 else None
    if rel and rel.get("id") and recreate and not dry:
        # ⚠️ Gitee 的 attach_files 是"再挂一个"，同名不会覆盖；而它的发行版 API **不返回附件 id**
        #    → 想删单个附件删不掉。重传一次就会变成"旧包 + 新包"两份，下载链接给你哪份说不准
        #    （2026-09-22 实测踩到：新机可能下到旧包 → sha256 校验不过，装不上）。
        #    所以重传的正确做法是**把发行版整个删掉重建**（附件是我们自己的，没有外部引用）。
        code, txt = _req("%s/repos/%s/%s/releases/%s?access_token=%s"
                         % (host["api"], host["owner"], host["repo"], rel["id"], tok), method="DELETE")
        if code not in (200, 204):
            raise SystemExit("Gitee 删旧发行版失败（%s）：%s" % (code, txt[:200]))
        print("   已删掉旧的发行版（id=%s），重建一份干净的" % rel["id"])
        rel = None
    if rel and rel.get("id"):
        return rel
    if dry:
        return {"id": None, "assets": [], "_would_create": True}
    data = urllib.parse.urlencode({"access_token": tok, "tag_name": tag, "name": tag,
                                   "body": body, "target_commitish": "main",
                                   "prerelease": "false"}).encode("utf-8")
    code, txt = _req("%s/repos/%s/%s/releases" % (host["api"], host["owner"], host["repo"]),
                     method="POST", data=data,
                     ctype="application/x-www-form-urlencoded")
    if code not in (200, 201):
        raise SystemExit("Gitee 建发行版失败（%s）：%s" % (code, txt[:300]))
    return json.loads(txt)


def gt_attachments(host, rel_id, tok):
    """列出附件（**带 id**）——`releases/tags/<tag>` 返回的 assets 里没有 id，删不掉单个附件，
    得走 `releases/<id>/attach_files`（2026-09-22 试出来的）。"""
    u = ("%s/repos/%s/%s/releases/%s/attach_files?per_page=100&access_token=%s"
         % (host["api"], host["owner"], host["repo"], rel_id, tok))
    code, txt = _req(u)
    try:
        d = json.loads(txt or "null")
    except ValueError:
        d = None
    return d if isinstance(d, list) else []


def gt_upload(host, rel, path, tok, log=print):
    name = os.path.basename(path)
    url = ("%s/repos/%s/%s/releases/%s/attach_files"
           % (host["api"], host["owner"], host["repo"], rel["id"]))
    # Gitee 的同名附件是**再挂一个**（不像 GitHub 会覆盖）→ 先删掉同名的，免得新机下到旧包。
    # 大小一样就当同一份跳过（跟 GitHub 侧对齐：重试整批时别白传 271MB）
    for a in gt_attachments(host, rel["id"], tok):
        if a.get("name") != name:
            continue
        _sz = os.path.getsize(path)
        if a.get("size") == _sz and _sz > 1024 * 1024:
            log("      （%s 已在远端且大小一致（大件）→ 跳过）" % name)
            return
        _req("%s/repos/%s/%s/releases/%s/attach_files/%s?access_token=%s"
             % (host["api"], host["owner"], host["repo"], rel["id"], a["id"], tok),
             method="DELETE")
        log("      （先删掉同名的旧附件 %s，id=%s）" % (name, a.get("id")))
    body, ctype = multipart({"access_token": tok}, [("file", path)])
    log("      （%.1f MB 上传中…）" % mb(path))
    code, txt = _req(url, method="POST", data=body, ctype=ctype, timeout=1800)
    if code not in (200, 201):
        raise SystemExit("Gitee 传 %s 失败（%s）：%s" % (name, code, txt[:300]))


def gt_drop(host, rel, names, tok, log=print):
    """按名字删掉远端附件（重打包后清理不再需要的那几件）。"""
    for a in gt_attachments(host, rel["id"], tok):
        if a.get("name") in names:
            _req("%s/repos/%s/%s/releases/%s/attach_files/%s?access_token=%s"
                 % (host["api"], host["owner"], host["repo"], rel["id"], a["id"], tok),
                 method="DELETE")
            log("   [删] %s（清单里已经不需要它了）" % a["name"])


HOSTS = {"github": (GH, gh_release, gh_upload), "gitee": (GT, gt_release, gt_upload)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default=DEFAULT_TAG)
    ap.add_argument("--dir", default="")
    ap.add_argument("--hosts", default="github,gitee")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--only", default="", help="只传名字里含这个串的附件（调试用）")
    ap.add_argument("--drop", default="",
                    help="（Gitee 上删不动单个附件，见 gt_release 注释）")
    ap.add_argument("--recreate", action="store_true",
                    help="Gitee 重传时先把发行版整个删掉重建（避免新旧附件并存）")
    a = ap.parse_args()
    d = a.dir or os.path.join(HERE, "_release", a.tag)
    if not os.path.isdir(d):
        raise SystemExit("没有这个目录：%s（先跑 python tools\\打包发行.py）" % d)
    files = sorted(os.path.join(d, f) for f in os.listdir(d) if not f.endswith(".parts.json"))
    exe = os.path.join(HERE, "dist", "score-tool.exe")
    if os.path.isfile(exe):
        files.append(exe)                    # 工作台也是附件，跟这套一起挂
    if a.only:
        files = [f for f in files if a.only in os.path.basename(f)]
    total = sum(os.path.getsize(f) for f in files)
    print("要传 %d 个附件，合计 %.1f MB：%s" % (len(files), total / 1048576.0,
                                              "、".join(os.path.basename(f) for f in files)))
    body = ("大件附件（仓库只放代码）。**默认安装只需 wheels-core + ffmpeg**（系统已有能力够的\n"
            "ffmpeg 时可跳过），转写 / 图像 / 转深度片是**能力包**，用到再装：\n"
            "`python tools\\deploy.py vendor --fetch --yes [--stt|--vision|--depth|--all-caps]`\n")
    for key in [h.strip() for h in a.hosts.split(",") if h.strip()]:
        host, rel_fn, up_fn = HOSTS[key]
        tok = token(host["host"])
        print("\n== %s（%s）==" % (host["label"], a.tag))
        if not tok:
            print("   !! 拿不到凭据（git credential fill 没给 %s 的 token）→ 跳过" % host["host"])
            continue
        rel = rel_fn(host, a.tag, body, tok, dry=a.dry,
                     recreate=a.recreate) if key == "gitee" else rel_fn(host, a.tag, body, tok, dry=a.dry)
        if rel.get("_would_create"):
            print("   会新建发行版 %s；现有附件 %d 个" % (a.tag, len(rel.get("assets") or [])))
        else:
            print("   发行版 id=%s，现有附件 %d 个" % (rel.get("id"), len(rel.get("assets") or [])))
        if a.dry:
            for f in files:
                print("   会传 %-30s %6.1f MB" % (os.path.basename(f), mb(f)))
            continue
        fails = []
        for f in files:
            t0 = time.time()
            try:
                up_fn(host, rel, f, tok)
                print("   [OK] %-30s %6.1f MB  %.0f 秒" % (os.path.basename(f), mb(f), time.time() - t0))
            except BaseException as e:                    # noqa: BLE001  一个附件失败别拖垮整批
                fails.append(os.path.basename(f))
                print("   [!!] %-30s 失败：%s" % (os.path.basename(f), str(e)[:200]))
        if fails:
            print("   → 这些没传上：%s（可以 --only <名字片段> 单独重试）" % "、".join(fails))
    if a.drop and key == "gitee":
        gt_drop(host, rel, [x.strip() for x in a.drop.split(",") if x.strip()], tok, log=print)
    print("\n传完记得验一次：新机实际走的是**匿名直链**，用 curl 拉一个附件看 HTTP 200 + 速度。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
