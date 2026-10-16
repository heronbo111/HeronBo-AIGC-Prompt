# -*- coding: utf-8 -*-
r"""给已发布的 vX.Y.Z 补挂「迁移引导」：把 score-tool.exe（重装引导）与新版 version.json 传到位。

为什么（2026-10-16）：0.6.2 及更早的单文件形态，「检查更新」拿不到可直接替换的内核——
它需要一个名为 `score-tool.exe` 的附件。这里把 迁移引导（tools\打迁移引导.py 的产物）
以这个名字挂到 Release，并把 version.json 的 exe 字段改指向它（**保留 pkg 不变**）：
  · 老客户端（单文件形态）更新 → 下载引导 → 用户看到"需要重装一次"的说明窗口；
  · 新客户端（目录式）读 pkg（zip 更新包），exe 字段变了也不影响它。

策略（别重传大件）：
  · GitHub：**只**补/换 score-tool.exe 与 version.json（Setup 639MB 不动）；
  · Gitee ：同名附件不能覆盖 → 删掉 release 重建，重传 version.json + Update zip + 引导
            （都是小件；Setup 本来就不在 Gitee）。

用法：
    python tools\补迁移引导.py --version 0.6.3 --apply          # 先打引导：python tools\打迁移引导.py
    python tools\补迁移引导.py --version 0.6.3                  # dry-run
"""
import argparse
import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import 发布版本 as rel                                                # noqa: E402

WIN = os.path.dirname(os.path.dirname(HERE))                          # tools → 仓库 → 工作区
GUIDE = os.path.join(HERE, "build", "迁移引导", "score-tool.exe")


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser(description="给已发布版本补挂迁移引导（score-tool.exe）")
    ap.add_argument("--version", required=True)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    version = a.version.strip().lstrip("vV")
    tag = "v" + version

    if not os.path.isfile(GUIDE):
        print("找不到引导程序：%s —— 先跑 python tools\\打迁移引导.py" % GUIDE)
        return 2
    vj = rel.LOCAL_VERSION_JSON
    if not os.path.isfile(vj):
        print("找不到 %s —— 先跑一次发布脚本或 版本.py write" % vj)
        return 2
    up = rel.find_update_pkg(version)
    if not up:
        print("找不到 Update 包（_安装包\\HeronBo-Workbench-Update-v%s.zip）" % version)
        return 2

    # 改本地 _version.json：exe 字段指向引导；**pkg 必须保住**——打包换位会把 _version.json
    # 重写成"只有本地自述"（无 pkg），这里若发现 pkg 缺失就从本地 Update 包现算补上，
    # 否则新客户端（目录式）会失去 zip 更新包的指向、检查更新直接失效。
    d = json.load(open(vj, encoding="utf-8-sig"))
    if not (d.get("pkg") or {}).get("sha256"):
        d["pkg"] = {"name": os.path.basename(up), "size": os.path.getsize(up),
                    "sha256": sha256_file(up)}
        print("（pkg 缺失，已从本地 Update 包现算补上）")
    d["sumsPkgSha256"] = (d.get("pkg") or {}).get("sha256") or ""
    d["exe"] = {"name": "score-tool.exe", "size": os.path.getsize(GUIDE),
                "sha256": sha256_file(GUIDE)}
    with open(vj, "w", encoding="utf-8", newline="\n") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    print("已更新 %s：exe → score-tool.exe（%.1f MB，sha %s…）"
          % (vj, os.path.getsize(GUIDE) / 1048576.0, d["exe"]["sha256"][:12]))
    print("pkg 保留：", json.dumps(d.get("pkg"), ensure_ascii=False)[:110], "…")

    body = rel.make_body(version, d.get("notes") or "", False, "", True)
    if not a.apply:
        print("\n将上传：")
        print("  GitHub：score-tool.exe（%.1f MB）+ version.json（不碰 Setup）"
              % (os.path.getsize(GUIDE) / 1048576.0))
        print("  Gitee ：删掉 %s 重建 → version.json + Update zip + score-tool.exe" % tag)
        print("\n这是预演，未外发。加 --apply 正式补挂。")
        return 0

    # ---- GitHub：只补/换 score-tool.exe 与 version.json ----
    tok = rel.credential("github.com")
    if not tok:
        print("拿不到 GitHub 令牌")
        return 4
    st, r = rel.gh_request("%s/repos/%s/%s/releases/tags/%s" % (rel.GH_API, rel.OWNER_GH, rel.REPO, tag), tok)
    if st != 200:
        print("GitHub 上没有 %s（HTTP%s）" % (tag, st))
        return 4
    rid = r["id"]
    for path, name in ((GUIDE, "score-tool.exe"), (vj, "version.json")):
        for a_ in (r.get("assets") or []):
            if a_.get("name") == name:
                rel.gh_request("%s/repos/%s/%s/releases/assets/%s" % (rel.GH_API, rel.OWNER_GH, rel.REPO, a_["id"]),
                               tok, method="DELETE", timeout=60)
                print("  [GitHub] 旧 %s 已删" % name)
        url = "https://uploads.github.com/repos/%s/%s/releases/%s/assets?name=%s" % (
            rel.OWNER_GH, rel.REPO, rid, name)
        with open(path, "rb") as f:
            blob = f.read()
        st2, b2 = rel.gh_request(url, tok, method="POST", data=blob,
                                 ctype="application/octet-stream", timeout=900)
        if st2 not in (200, 201):
            print("  [GitHub] 上传 %s 失败 HTTP%s %s" % (name, st2, str(b2)[:160]))
            return 4
        print("  [GitHub] 已上传 %s（%.1f MB）" % (name, os.path.getsize(path) / 1048576.0))

    # ---- Gitee：删掉重建（同名不能覆盖），三个小件全传 ----
    err = rel.gee_publish(tag, rel.current_branch(), body, [vj, up, GUIDE], force=True)
    if err:
        print("[Gitee] " + err)
        return 5

    print("\n== 验证匿名直链 ==")
    rel.verify_links(tag, [vj, up, GUIDE])
    print("\n完成：老客户端「检查更新」现在能下到迁移引导；新客户端不受影响（走 pkg）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
