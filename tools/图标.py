# -*- coding: utf-8 -*-
"""生成工作台图标（纯标准库，不依赖 Pillow）。

图形：圆角方块 + 四根竖条 —— 就是工作台的四栏（窄/窄/宽/窄），一眼认得出。
输出：
    tools/工作台.ico        （16/32/48/64/128/256 多尺寸，PNG 载荷；Win10/11 与 PyInstaller 都认）
    tools/工作台_256.png    （给文档/README 用）
用法：python tools\\图标.py
"""
import io
import os
import struct
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ICO = os.path.join(HERE, "工作台.ico")
PNG = os.path.join(HERE, "工作台_256.png")

BG = (47, 111, 237)          # 强调蓝（浅色主题的 accent）
FG = (255, 255, 255)         # 四栏用白
SS = 3                       # 每像素 3×3 超采样（圆角与竖条边缘抗锯齿）


def _row_bytes(size):
    """画一张 size×size 的 RGBA 行数据。"""
    r = size * 0.22                       # 圆角半径
    gap = size * 0.075                    # 竖条间距
    # 四栏宽度比例（照工作台：项目 窄 / 素材 窄 / 分镜 宽 / 评分 窄）
    weights = [2, 2, 5, 3]
    total = sum(weights)
    inner = size * 0.62                   # 四栏占据的总宽
    bar_w = (inner - gap * (len(weights) - 1)) / total
    x0 = (size - inner) / 2.0
    bars = []
    cx = x0
    for w in weights:
        bars.append((cx, cx + bar_w * w))
        cx += bar_w * w + gap
    by0, by1 = size * 0.30, size * 0.70   # 竖条上下边（居中）

    def inside_round_rect(px, py):
        dx = max(r - px, 0.0, px - (size - r))
        dy = max(r - py, 0.0, py - (size - r))
        return (dx * dx + dy * dy) <= r * r

    rows = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            acc_bg = acc_fg = 0
            for sy in range(SS):
                for sx in range(SS):
                    px = x + (sx + 0.5) / SS
                    py = y + (sy + 0.5) / SS
                    if not inside_round_rect(px, py):
                        continue
                    acc_bg += 1
                    if by0 <= py <= by1 and any(b0 <= px <= b1 for b0, b1 in bars):
                        acc_fg += 1
            n = SS * SS
            a_bg = acc_bg / float(n)
            if a_bg <= 0:
                row += bytes((0, 0, 0, 0))
                continue
            a_fg = acc_fg / float(n)
            k = min(1.0, a_fg / a_bg)     # 条子在方块内的占比
            col = tuple(int(round(BG[i] + (FG[i] - BG[i]) * k)) for i in range(3))
            row += bytes((col[0], col[1], col[2], int(round(a_bg * 255))))
        rows.append(bytes(row))
    return rows


def png_bytes(size):
    rows = _row_bytes(size)
    raw = b"".join(b"\x00" + r for r in rows)

    def chunk(tag, data):
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)   # 8bit RGBA
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def _pngs_from_image(src, sizes, keep_bg=False, white_similarity=0.12):
    """用 ffmpeg 把一张图片转成多档 PNG（居中裁方 + 可选抠白底）。返回 [(size, bytes)]。"""
    import subprocess
    import tempfile
    out = []
    tmp = tempfile.mkdtemp(prefix="icon_")
    for s in sizes:
        fp = os.path.join(tmp, "%d.png" % s)
        vf = ("crop='min(iw,ih)':'min(iw,ih)'"          # 居中裁成正方形
              ",scale=%d:%d:flags=lanczos" % (s, s))
        if not keep_bg:
            vf += ",colorkey=0xFFFFFF:%.3f:0.02" % white_similarity   # 白底抠成透明
        fmt = "rgba" if not keep_bg else "rgb24"
        r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", src, "-vf", vf,
                            "-pix_fmt", fmt, fp], capture_output=True, text=True)
        if r.returncode != 0 or not os.path.isfile(fp):
            raise SystemExit("ffmpeg 转图失败：%s" % (r.stderr or "")[-300:])
        out.append((s, open(fp, "rb").read()))
    return out


def _write_ico(blobs):
    out = io.BytesIO()
    out.write(struct.pack("<HHH", 0, 1, len(blobs)))            # ICONDIR
    offset = 6 + 16 * len(blobs)
    for s, data in blobs:
        out.write(struct.pack("<BBBBHHII", s if s < 256 else 0, s if s < 256 else 0,
                              0, 0, 1, 32, len(data), offset))
        offset += len(data)
    for _s, data in blobs:
        out.write(data)
    io.open(ICO, "wb").write(out.getvalue())
    io.open(PNG, "wb").write(dict(blobs)[max(dict(blobs))])
    print("已生成：%s（%d 尺寸，%d 字节）" % (ICO, len(blobs), os.path.getsize(ICO)))
    print("已生成：%s" % PNG)


def main():
    import argparse
    ap = argparse.ArgumentParser(description="生成工作台图标")
    ap.add_argument("--from", dest="src", default="", help="用现成图片做图标（jpg/png 都行）")
    ap.add_argument("--keep-bg", action="store_true", help="保留原图背景（默认把纯白背景抠成透明）")
    a = ap.parse_args()
    sizes = [16, 24, 32, 48, 64, 128, 256]
    if a.src:
        if not os.path.isfile(a.src):
            raise SystemExit("找不到图片：%s" % a.src)
        _write_ico(_pngs_from_image(a.src, sizes, keep_bg=a.keep_bg))
        return
    blobs = [(s, png_bytes(s)) for s in sizes]
    blobs = [(s, png_bytes(s)) for s in sizes]
    io.open(PNG, "wb").write(dict(blobs)[256])
    out = io.BytesIO()
    out.write(struct.pack("<HHH", 0, 1, len(blobs)))            # ICONDIR
    offset = 6 + 16 * len(blobs)
    for s, data in blobs:
        out.write(struct.pack("<BBBBHHII", s if s < 256 else 0, s if s < 256 else 0,
                              0, 0, 1, 32, len(data), offset))
        offset += len(data)
    for _s, data in blobs:
        out.write(data)
    io.open(ICO, "wb").write(out.getvalue())
    print("已生成：%s（%d 尺寸，%d 字节）" % (ICO, len(blobs), os.path.getsize(ICO)))
    print("已生成：%s" % PNG)


if __name__ == "__main__":
    main()
