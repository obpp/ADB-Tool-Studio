#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 icon.ico —— 只用标准库（zlib + struct），不需要 Pillow。

图标 = 蓝色圆角方块 + 白色闪电。渲染时先按 3 倍超采样再缩回来，边缘就自然抗锯齿了。

    python3 desktop/make_icon.py                 # 写 ADB-Tool-Studio/icon.ico
    python3 desktop/make_icon.py --preview p.png # 额外导一张 256 预览图

生成的 icon.ico 已经提交进仓库；除非要改图标，平时不用跑这个脚本。
"""

import argparse
import os
import struct
import sys
import zlib

SIZES = (16, 24, 32, 48, 64, 128, 256)
SUPERSAMPLE = 3

ACCENT = (42, 120, 214)          # --accent，和网页版同一个蓝
WHITE = (255, 255, 255)

# 闪电的多边形顶点，坐标是 0..1 的相对值。
#
# 两个「缺口」开得浅一些（横档从 0.18 加宽到 ~0.28），笔画就粗了 —— 这一版是拿
# 16/24/32 三个尺寸放大对比挑出来的：细闪电在 16px 下会糊成一团，而托盘图标
# 恰好就是 16px。另外整体收在 0.11~0.89 之间，顶到边会被圆角切掉。
BOLT = (
    (0.640, 0.115),
    (0.215, 0.545),
    (0.500, 0.545),
    (0.370, 0.885),
    (0.785, 0.455),
    (0.500, 0.455),
)


# ---------------------------------------------------------------- 画图

def _in_rounded_square(x, y, size, radius):
    """(x, y) 在不在圆角方块里。"""
    if x < 0 or y < 0 or x > size or y > size:
        return False
    # 先按内缩过的直边判断，落在四角区域再看圆
    cx = min(max(x, radius), size - radius)
    cy = min(max(y, radius), size - radius)
    dx = x - cx
    dy = y - cy
    return dx * dx + dy * dy <= radius * radius


def _in_polygon(x, y, points):
    """射线法判断点是否在多边形内。"""
    inside = False
    count = len(points)
    for i in range(count):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % count]
        if (y1 > y) != (y2 > y):
            xint = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < xint:
                inside = not inside
    return inside


def render(size):
    """渲染一张 size×size 的 RGBA 图，返回按行排列的字节。"""
    big = size * SUPERSAMPLE
    radius = big * 0.22
    bolt = tuple((px * big, py * big) for px, py in BOLT)

    # 超采样：先算 big×big 的覆盖率，再缩到 size×size
    accum = [[0] * 4 for _ in range(size * size)]
    for by in range(big):
        y0 = (by * size) // big
        for bx in range(big):
            if not _in_rounded_square(bx + 0.5, by + 0.5, big, radius):
                continue
            color = WHITE if _in_polygon(bx + 0.5, by + 0.5, bolt) else ACCENT
            slot = accum[y0 * size + (bx * size) // big]
            slot[0] += color[0]
            slot[1] += color[1]
            slot[2] += color[2]
            slot[3] += 255

    per_pixel = SUPERSAMPLE * SUPERSAMPLE
    rows = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            r, g, b, a = accum[y * size + x]
            if a == 0:
                row += b"\x00\x00\x00\x00"
                continue
            # 颜色取平均；alpha 单独按覆盖率算（a 已经是「有多少个采样点算数」）
            n = a // 255
            row += bytes((r // n, g // n, b // n, min(255, a // per_pixel)))
        rows.append(bytes(row))
    return rows


# ---------------------------------------------------------------- PNG / ICO 封装

def _chunk(tag, data):
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def to_png(size, rows):
    raw = b"".join(b"\x00" + row for row in rows)
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(raw, 9))
            + _chunk(b"IEND", b""))


def to_ico(images):
    """images: [(size, png_bytes)]。Vista 之后的 ICO 允许直接内嵌 PNG。"""
    count = len(images)
    header = struct.pack("<HHH", 0, 1, count)
    offset = 6 + 16 * count
    entries = b""
    payload = b""
    for size, data in images:
        dim = 0 if size >= 256 else size     # 256 在 ICO 里记作 0
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
        payload += data
    return header + entries + payload


# ---------------------------------------------------------------- 入口

def build_ico():
    images = []
    for size in SIZES:
        images.append((size, to_png(size, render(size))))
    return to_ico(images)


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    default_out = os.path.join(os.path.dirname(here), "icon.ico")

    parser = argparse.ArgumentParser(description="生成 ADB Tool Studio 的图标")
    parser.add_argument("--out", default=default_out, help="输出的 .ico 路径")
    parser.add_argument("--preview", help="额外导出一张 256 的 PNG，方便肉眼看效果")
    args = parser.parse_args()

    data = build_ico()
    with open(args.out, "wb") as fp:
        fp.write(data)
    print("已写入 %s（%d 字节，含 %s）"
          % (args.out, len(data), "/".join(str(s) for s in SIZES)))

    if args.preview:
        with open(args.preview, "wb") as fp:
            fp.write(to_png(256, render(256)))
        print("预览图 %s" % args.preview)
    return 0


if __name__ == "__main__":
    sys.exit(main())
