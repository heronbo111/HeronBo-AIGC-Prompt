# -*- coding: utf-8 -*-
"""B站成片下载助手（**仅供拆解分析**：把链接变成可喂给 `tools\\成片拆解.py` 的本地 mp4）。

设计（2026-09-12 v1.0，配合 `references/parody-teardown.md`）：
- 用户发来的二创常是 B站链接；拆解要跑本地工具（抽帧/人脸/音轨/转写），先得把片子落到本地。
- 只取**匿名可达**的清晰度（通常 480P DASH）——拆解看结构够用；1080P 需登录，不碰账号。
- 下载视频流 + 音频流后用 ffmpeg 合成 mp4；`--audio-only` 只落音频（做配乐/节拍分析或转写更快）。
- **用途边界**：下载的成片只作分析对象，不得直接当素材上传再生成（版权，见 rules 第7条）。

用法：
    python 下载B站成片.py "https://www.bilibili.com/video/BV1kuKE66Eds/"          # → ./BV1kuKE66Eds_<标题>.mp4
    python 下载B站成片.py BV1kuKE66Eds --out "D:\\拆解素材"                        # 指定输出目录
    python 下载B站成片.py BV1kuKE66Eds --audio-only                               # 只出 m4a（省时间）

依赖：ffmpeg 在 PATH；Python 标准库（urllib）。
只做本地下载：不登录账号、不提交生成、不消耗积分。
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


def need(tool):
    p = shutil.which(tool)
    if not p:      # 仓库自带的那份（装机时 python tools/deploy.py install 会解到 tools/_vendor/ffmpeg/bin）
        _c = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "_vendor", "ffmpeg", "bin", tool + ".exe")
        p = _c if os.path.isfile(_c) else None
    if not p:
        sys.exit("[错误] 找不到 %s：系统 PATH 里没有，仓库自带的 tools/_vendor/ffmpeg/bin 里也没有。" % tool
                 + "\n        跑一次 python tools/deploy.py install 会自动解开仓库自带的那份，"
                 + "或者自己装 ffmpeg 并加入 PATH。")
    return p

def get(url, referer="https://www.bilibili.com/"):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": referer})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def api_json(url, referer="https://www.bilibili.com/"):
    return json.loads(get(url, referer).decode("utf-8"))


def parse_bvid(s):
    m = re.search(r"(BV[0-9A-Za-z]{10})", s)
    if not m:
        sys.exit("[错误] 从输入里没找到 BV 号：%s" % s)
    return m.group(1)


def safe_name(s):
    return re.sub(r"[\\/:*?\"<>|\r\n]+", "_", s).strip()[:80]


def download(url, dst, referer):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": referer})
    with urllib.request.urlopen(req, timeout=120) as r, open(dst, "wb") as f:
        shutil.copyfileobj(r, f)
    return os.path.getsize(dst)


def main():
    ap = argparse.ArgumentParser(description="B站成片下载（拆解分析用；只取匿名可达清晰度）")
    ap.add_argument("target", help="视频链接或 BV 号")
    ap.add_argument("--out", default=".", help="输出目录，默认当前目录")
    ap.add_argument("--audio-only", action="store_true", help="只下载音频（m4a）")
    args = ap.parse_args()

    ffmpeg = need("ffmpeg")
    bvid = parse_bvid(args.target)
    referer = "https://www.bilibili.com/video/%s/" % bvid
    view = api_json("https://api.bilibili.com/x/web-interface/view?bvid=%s" % bvid, referer)
    if view.get("code") != 0:
        sys.exit("[错误] 取视频信息失败：%s（可能已下架或需要登录）" % view.get("message"))
    d = view["data"]
    cid = d["cid"]
    title = safe_name(d["title"])
    print("标题：%s\nUP：%s ｜ 时长 %.1fs ｜ cid=%s" % (d["title"], d["owner"]["name"], d["duration"], cid))

    play = api_json("https://api.bilibili.com/x/player/playurl?bvid=%s&cid=%s&fnval=16&fnver=0&fourk=0"
                    % (bvid, cid), referer)
    if play.get("code") != 0 or "dash" not in (play.get("data") or {}):
        sys.exit("[错误] 取播放地址失败：%s（这条可能限制匿名播放，需要登录下载）" % play.get("message"))
    dash = play["data"]["dash"]
    audio = sorted(dash["audio"], key=lambda a: a["bandwidth"])[-1]

    os.makedirs(args.out, exist_ok=True)
    tmp = os.environ.get("TEMP") or "/tmp"
    a_tmp = os.path.join(tmp, "bili_a.m4s")
    print("下载音频流（id=%s）…" % audio["id"])
    download(audio["baseUrl"], a_tmp, referer)

    if args.audio_only:
        out = os.path.join(args.out, "%s_%s.m4a" % (bvid, title))
        subprocess.run([ffmpeg, "-y", "-v", "error", "-i", a_tmp, "-vn", "-c:a", "copy", out], check=True)
        print("完成 → %s" % out)
        return

    vids = sorted(dash.get("video") or [], key=lambda v: (v.get("id", 0), v.get("bandwidth", 0)))
    if not vids:
        sys.exit("[错误] 没有可用的视频流（可能只提供付费清晰度）")
    vid = vids[-1]
    v_tmp = os.path.join(tmp, "bili_v.m4s")
    print("下载视频流（id=%s，约 %dP）…" % (vid["id"], vid.get("height") or 0))
    download(vid["baseUrl"], v_tmp, referer)

    out = os.path.join(args.out, "%s_%s.mp4" % (bvid, title))
    subprocess.run([ffmpeg, "-y", "-v", "error", "-i", v_tmp, "-i", a_tmp, "-c", "copy", out], check=True)
    try:
        os.remove(v_tmp)
        os.remove(a_tmp)
    except Exception:
        pass
    print("完成 → %s（%dP）\n拆解下一步：python tools\\成片拆解.py -i \"%s\" --asr" % (
        out, vid.get("height") or 0, out))


if __name__ == "__main__":
    main()
