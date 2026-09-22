# -*- coding: utf-8 -*-
r"""下载器：给安装/部署用的**能续传、会同源重试、验 sha256** 的文件下载。

为什么单独成文件（2026-09-22，用户："我最想解决的就是安装慢的核心问题"）：

  原来 `部署.py` 的 `_download()` 是 `open(dst,"wb")` **从 0 字节写**：
    · 不发 `Range` → 300MB 的包在弱网下**断一次就前面全白下**（体感最差的一环）；
    · 同一个源**不重试**，只换下一个镜像；
    · 下完**不校验**，截断/被网关塞了错误页也会当成"下好了"。

  实测这台机器（2026-09-22 12:20，直连无代理）：
    GitHub 直连 0 字节（读超时）｜ gh-proxy 10.94 MB/s ｜ ghproxy.net 0.36 MB/s ｜ Gitee 2.4–3.3 MB/s
  —— 源会飘，所以"**断了能接着下**"比"换个快源"更值钱。

对外：
    fetch(url, dst, mirrors=(), sha256="", tries=3, progress=None, log=print) -> 文件字节数
    remote_info(url, timeout=30) -> (大小, Last-Modified)
    split(path, part_mb=90) -> 分卷清单 dict（给 Gitee 100MB 单附件上限用）
    join(parts_json, dst)   -> 校验后合并，返回字节数

续传的正确性靠 sidecar：下载中途会在 `<dst>.part.json` 记下**当时的远端大小/Last-Modified**。
下次续传前先比一次，远端换过（重打了包）就把断点丢掉重下——
否则"旧包的前 60MB + 新包的后 40MB"会拼出一个**校验不过、还很难查**的坏包。
"""
import hashlib
import json
import os
import time
import urllib.error
import urllib.request

CHUNK = 262144
UA = "heronbo-deploy/2"


def _noop(*_a, **_k):
    pass


def _sidecar(dst):
    return dst + ".part.json"


def _read_sidecar(dst):
    try:
        with open(_sidecar(dst), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_sidecar(dst, info):
    try:
        with open(_sidecar(dst), "w", encoding="utf-8", newline="\n") as f:
            json.dump(info, f, ensure_ascii=False)
    except OSError:
        pass


def _drop_sidecar(dst):
    try:
        os.remove(_sidecar(dst))
    except OSError:
        pass


def sha256_file(path, chunk=1024 * 1024):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def remote_info(url, timeout=30):
    """远端文件的大小与 Last-Modified（匿名 HEAD 可读；GitHub 会 302 到对象存储）。"""
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return int(r.headers.get("Content-Length") or 0), r.headers.get("Last-Modified") or ""


def _mb(x):
    return x / 1048576.0


def fetch(url, dst, mirrors=(), sha256="", tries=3, progress=None, log=print,
          timeout=120, chunk=CHUNK):
    """下载 url → dst。mirrors 是**URL 前缀**（如 "https://gh-proxy.com/"），依次尝试。

    续传规则：dst 已有 N 字节、远端比它大、且 sidecar 记的远端大小/时间**没变** → 发
    `Range: bytes=N-` 接着下；服务端不认 Range（回 200 而不是 206）就当没有断点、从头下。
    同一个源内部重试 tries 次（每次接着上次的断点），全都不行才换下一个源。
    下完：先比大小、再比 sha256（给了的话）；**校验不过就删掉重来一次**，再不过就报错。
    返回最终文件字节数。异常只在"所有源都失败"时抛出（OSError）。
    """
    urls = []
    for m in (("",) + tuple(mirrors)):
        urls.append((m + url) if m else url)
        # 兼容把完整 URL 当镜像的写法（`https://gh-proxy.com/https://…`）
    last = ""
    for u in urls:
        for attempt in range(1, max(1, tries) + 1):
            label = u if len(u) < 96 else u[:93] + "…"
            try:
                n = _once(u, dst, sha256=sha256, progress=progress, log=log,
                          timeout=timeout, chunk=chunk,
                          tag="" if attempt == 1 else "第 %d 次" % attempt)
                return n
            except _Restart as e:                    # 校验不过 / 远端换过 → 已删干净，重来
                last = str(e)
                log("    %s：%s（重来一次）" % (label, str(e)[:110]))
            except Exception as e:                   # noqa: BLE001  断流/超时/网络错误
                last = "%s: %s" % (type(e).__name__, e)
                if os.path.isfile(dst):
                    have = os.path.getsize(dst)
                    log("    %s 断了（%s）→ 已存 %.1f MB，接着下" % (label, last[:80], _mb(have)))
        log("    源 %s 试了 %d 次都不行 → 换下一个源" % (label, max(1, tries)))
    raise OSError(last or "没有可用的源")


class _Restart(Exception):
    """内部信号：当前文件不能用（校验不过/远端换过），重头下。"""


def _once(url, dst, sha256="", progress=None, log=print, timeout=120, chunk=CHUNK, tag=""):
    # 目标目录可能还不存在（2026-09-22 新机实测踩到：<tools>\dist\ 还没建，
    # 下工作台 exe 时 open(".new","wb") 直接 FileNotFoundError）→ 先补目录
    _d = os.path.dirname(os.path.abspath(dst))
    if _d:
        try:
            os.makedirs(_d, exist_ok=True)
        except OSError:
            pass
    have = os.path.getsize(dst) if os.path.isfile(dst) else 0
    side = _read_sidecar(dst)
    rsz, rlm = 0, ""
    try:
        rsz, rlm = remote_info(url, timeout=min(timeout, 30))
    except Exception:                                # noqa: BLE001  有些镜像不支持 HEAD
        pass
    if have and side and rsz and (side.get("size") not in (None, rsz) or
                                  (side.get("lm") and rlm and side.get("lm") != rlm)):
        log("    远端这个包换过了（重打过）→ 丢掉旧断点重下")
        _rm(dst)
        have = 0
    if rsz and have >= rsz:
        ok, why = _verify(dst, sha256, rsz)
        if ok:
            log("    已经在本地且完好（%.1f MB）" % _mb(have))
            _drop_sidecar(dst)
            return have
        log("    本地这份不对（%s）→ 重下" % why)
        _rm(dst)
        have = 0

    req = urllib.request.Request(url, headers={"User-Agent": UA})
    if have:
        req.add_header("Range", "bytes=%d-" % have)
    t0 = time.time()
    got = have
    total = rsz or 0
    last_t = 0.0
    with urllib.request.urlopen(req, timeout=timeout) as r:
        code = getattr(r, "status", 200) or 200
        if have and code != 206:                     # 服务端不认 Range：从头下
            log("    这个源不支持续传 → 从头下")
            have = got = 0
        left = int(r.headers.get("Content-Length") or 0)
        if left:
            total = have + left
        if have:
            log("    续传：从 %.1f MB 接着下（共约 %s）"
                % (_mb(have), ("%.1f MB" % _mb(total)) if total else "?"))
        _write_sidecar(dst, {"url": url, "size": rsz, "lm": rlm, "at": time.strftime("%Y-%m-%d %H:%M:%S")})
        with open(dst, "ab" if have else "wb") as f:
            while True:
                b = r.read(chunk)
                if not b:
                    break
                f.write(b)
                got += len(b)
                if progress and progress(got, total) is False:      # 调用方可以喊停
                    raise OSError("调用方中止")
                if time.time() - last_t > 3:
                    last_t = time.time()
                    el = max(0.1, time.time() - t0)
                    if total:
                        log("    %5.1f%%  %.1f/%.1f MB  %.1f MB/s"
                            % (got * 100.0 / total, _mb(got), _mb(total), _mb(got - have) / el))
                    else:
                        log("    已下 %.1f MB  %.1f MB/s" % (_mb(got), _mb(got - have) / el))
    ok, why = _verify(dst, sha256, total)
    if not ok:
        _rm(dst)
        _drop_sidecar(dst)
        raise _Restart(why)
    _drop_sidecar(dst)
    if got:
        log("    下好了（%.1f MB，平均 %.1f MB/s）%s"
            % (_mb(got), _mb(got - have) / max(0.1, time.time() - t0), ("  " + tag) if tag else ""))
    return got


def _rm(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _verify(path, sha256, total):
    """(是否可用, 原因)。大小对不上＝截断；sha256 对不上＝内容错。"""
    if not os.path.isfile(path):
        return False, "文件不在"
    sz = os.path.getsize(path)
    if total and sz != total:
        return False, "大小不对（%.1f/%.1f MB）" % (_mb(sz), _mb(total))
    if sha256:
        h = sha256_file(path)
        if h.lower() != sha256.lower():
            return False, "sha256 不对（%s…）" % h[:12]
    return True, ""


def probe(urls, timeout=8, nbytes=262144, min_bytes=65536, log=_noop):
    """给几个地址打个小样（读头 256KB 就断开），按**能拿到且快**排序返回。

    为什么要有它（2026-09-22 实测）：源的速度每台机器、每个时段都不一样——
    本机同一天测得：GitHub 直连 0 字节（连不上）、gh-proxy 10.9MB/s、ghproxy.net 0.36MB/s、
    Gitee 附件 2.2MB/s（连接阶段还会被卡十几秒）。谁快就先用谁，别写死顺序干等。
    取不到样本（连不上/超时/给的东西太少）的排到后面，仍然保留——它可能是对手的临时抽风。

    ⚠️ **不要用 `Range` 头探测**（2026-09-22 踩到）：几个 GitHub 镜像不认 Range，会直接回
    错误码 → 最快的那个源反被判成"不通"。改成普通 GET 读到够量就主动断开。
    """
    if len(urls) < 2 or os.environ.get("HERONBO_NO_PROBE") == "1":
        return list(urls)
    import threading

    res = {}

    def one(u):
        t0 = time.time()
        got = 0
        try:
            req = urllib.request.Request(u, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                while got < nbytes:
                    b = r.read(65536)
                    if not b:
                        break
                    got += len(b)
            el = max(time.time() - t0, 0.001)
            res[u] = (got / el) if got >= min_bytes else 0.0
            log("    源探测 %-44s %6.2f MB/s" % (u[:44], res[u] / 1048576.0))
        except Exception as e:                                    # noqa: BLE001
            res[u] = 0.0
            log("    源探测 %-44s 不通（%s）" % (u[:44], type(e).__name__))

    ths = [threading.Thread(target=one, args=(u,), daemon=True) for u in urls]
    for t in ths:
        t.start()
    for t in ths:
        t.join(timeout + 1)
    return sorted(urls, key=lambda u: -res.get(u, 0.0))


def fetch_any(urls, dst, sha256="", tries=2, progress=None, log=print, timeout=120,
              rank=True):
    """一串**具体 URL** 依次试（同一个断点在换源后接着用，不重下）。

    为什么要有它：同一个附件在 Gitee / GitHub 直连 / 两个镜像上是四个不同地址，
    "换源"不该等于"重新开始下"。`rank=True` 时先 probe 一下，快的先下。
    """
    cand = probe(list(urls), log=log) if rank else list(urls)
    last = ""
    for u in cand:
        try:
            return fetch(u, dst, sha256=sha256, tries=tries, progress=progress,
                         log=log, timeout=timeout)
        except Exception as e:                                    # noqa: BLE001
            last = "%s：%s" % (u[:70], str(e)[:80])
            log("    %s → 换下一个源" % str(e)[:70])
    raise OSError(last or "没有可用的源")


def hashes(urls, timeout=30):
    """从这几个 URL 里取 SHA256SUMS.txt（第一个能取到的）→ {文件名: sha256}。

    附件下完拿它对 sha256（比只看大小严得多：断流截断、网关塞错误页都能当场抓住）。
    取不到就返回 {}——**不阻塞安装**，调用方退化成只比大小。每次都重新下（文件才几百字节，
    但要是拿了上次的旧清单，就会拿错哈希把好包判成坏包）。
    """
    import tempfile
    tmp = os.path.join(tempfile.gettempdir(), "heronbo_SHA256SUMS.txt")
    _rm(tmp)
    try:
        fetch_any(list(urls), tmp, tries=1, log=_noop, timeout=timeout)
    except Exception:                                            # noqa: BLE001
        return {}
    out = {}
    try:
        with open(tmp, encoding="utf-8", errors="replace") as f:
            for line in f:
                p = line.split()
                if len(p) == 2 and len(p[0]) == 64:
                    out[p[1].lstrip("*")] = p[0].lower()
    except OSError:
        return {}
    return out


# ── 分卷：Gitee 的发行版附件单文件上限 100MB（2026-09-22 核实）───────────────────
# 300MB 里 wheels-heavy.zip 是 116MB，超了；切成 90MB 一卷 + 一份 parts.json（记每卷与整包 sha256），
# 装的时候自动合并校验。GitHub 那边不受限，可以继续放整包——两条路都支持。
def split(path, part_mb=90, log=print):
    """把 path 切成 path.part01/.part02… 并写 path.parts.json，返回清单 dict。"""
    if not os.path.isfile(path):
        raise OSError("没有这个文件：%s" % path)
    size = os.path.getsize(path)
    whole = sha256_file(path)
    step = int(part_mb) * 1024 * 1024
    base = os.path.basename(path)
    parts = []
    with open(path, "rb") as f:
        i = 0
        done = 0
        while done < size:
            i += 1
            fn = "%s.part%02d" % (path, i)
            n = 0
            with open(fn, "wb") as o:
                while n < step:
                    b = f.read(min(CHUNK, step - n))
                    if not b:
                        break
                    o.write(b)
                    n += len(b)
            done += n
            parts.append({"file": "%s.part%02d" % (base, i), "size": n, "sha256": sha256_file(fn)})
            log("    分卷 %s（%.1f MB）" % (os.path.basename(fn), _mb(n)))
    man = {"name": base, "size": size, "sha256": whole, "part_mb": int(part_mb), "parts": parts}
    with open(path + ".parts.json", "w", encoding="utf-8", newline="\n") as f:
        json.dump(man, f, ensure_ascii=False, indent=1)
    log("    清单：%s.parts.json（%d 卷，共 %.1f MB）" % (base, len(parts), _mb(size)))
    return man


def join(parts_json, dst, log=print):
    """按清单把分卷合并成 dst 并校验（先逐卷、再整包）。返回字节数。"""
    with open(parts_json, encoding="utf-8") as f:
        man = json.load(f)
    d = os.path.dirname(os.path.abspath(parts_json))
    tmp = dst + ".joining"
    with open(tmp, "wb") as o:
        for p in man.get("parts") or []:
            fp = os.path.join(d, p["file"])
            if not os.path.isfile(fp):
                raise OSError("缺分卷：%s" % p["file"])
            if p.get("sha256") and sha256_file(fp) != p["sha256"]:
                raise OSError("分卷校验不过：%s" % p["file"])
            with open(fp, "rb") as i:
                while True:
                    b = i.read(1024 * 1024)
                    if not b:
                        break
                    o.write(b)
    if os.path.getsize(tmp) != man.get("size"):
        _rm(tmp)
        raise OSError("合并后大小不对（应 %d 字节）" % man.get("size", -1))
    if man.get("sha256") and sha256_file(tmp) != man["sha256"]:
        _rm(tmp)
        raise OSError("合并后整包 sha256 不对")
    os.replace(tmp, dst)
    log("    合并成 %s（%.1f MB，校验通过）" % (os.path.basename(dst), _mb(man["size"])))
    return man["size"]
