# -*- coding: utf-8 -*-
"""画一个原创 Q 版小火人，输出多尺寸 spark.ico（纯标准库，无依赖）。

ICO 内用传统 32bpp BMP 而不是 PNG，PyInstaller 和 Windows 都稳妥支持。
"""
import struct
from pathlib import Path

SIZES = [16, 24, 32, 48, 64, 128, 256]
SS = 2

OUTLINE = (0.78, 0.19, 0.10)
EYE = (0.17, 0.10, 0.12)


def ell(x, y, cx, cy, rx, ry):
    return ((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2 <= 1


def flame(x, y, s=1.0):
    x = 0.5 + (x - 0.5) / s
    y = 0.58 + (y - 0.58) / s
    if ell(x, y, 0.5, 0.635, 0.305, 0.315):
        return True
    if 0.085 <= y <= 0.62 and abs(x - 0.5) <= (y - 0.085) * 0.575:
        return True
    return ell(x, y, 0.27, 0.52, 0.095, 0.15) or ell(x, y, 0.73, 0.52, 0.095, 0.15)


def blend(dst, src):
    r, g, b, a = dst
    sr, sg, sb, sa = src
    if sa >= 1:
        return (sr, sg, sb, 1)
    out = sa + a * (1 - sa)
    if out <= 0:
        return (0, 0, 0, 0)
    return ((sr * sa + r * a * (1 - sa)) / out,
            (sg * sa + g * a * (1 - sa)) / out,
            (sb * sa + b * a * (1 - sa)) / out, out)


def sample(x, y):
    c = (0, 0, 0, 0)
    if flame(x, y, 1.075):
        c = (*OUTLINE, 1)
    # 两只小手
    for hx in (0.145, 0.855):
        if ell(x, y, hx, 0.755, 0.078, 0.064):
            c = (*OUTLINE, 1)
        if ell(x, y, hx, 0.755, 0.058, 0.045):
            c = (1.0, 0.42, 0.22, 1)
    if flame(x, y, 1.0):
        t = min(1.0, max(0.0, (y - 0.10) / 0.82))
        c = (1.0, 0.60 - 0.30 * t, 0.23 - 0.07 * t, 1)
    # 内焰
    if ell(x, y, 0.5, 0.715, 0.155, 0.170):
        c = (1.0, 0.85, 0.36, 1)
    # 腮红
    for bx in (0.285, 0.715):
        if ell(x, y, bx, 0.690, 0.052, 0.030):
            c = blend(c, (1.0, 0.42, 0.48, 0.72))
    # 眼睛 + 高光
    for ex in (0.405, 0.595):
        if ell(x, y, ex, 0.590, 0.046, 0.060):
            c = (*EYE, 1)
        if ell(x, y, ex - 0.014, 0.570, 0.017, 0.019):
            c = (1.0, 1.0, 1.0, 1)
    # 微笑
    d = ((x - 0.5) ** 2 + (y - 0.655) ** 2) ** 0.5
    if y > 0.673 and 0.040 < d < 0.054:
        c = (*EYE, 1)
    return c


def render(size):
    out = bytearray()
    n = SS * SS
    for py in range(size):
        for px in range(size):
            sr = sg = sb = sa = 0.0
            for sy in range(SS):
                for sx in range(SS):
                    u = (px + (sx + 0.5) / SS) / size
                    v = (py + (sy + 0.5) / SS) / size
                    r, g, b, a = sample(u, v)
                    sr += r * a
                    sg += g * a
                    sb += b * a
                    sa += a
            if sa > 0:
                out += bytes((min(255, int(sr / sa * 255 + 0.5)),
                              min(255, int(sg / sa * 255 + 0.5)),
                              min(255, int(sb / sa * 255 + 0.5)),
                              min(255, int(sa / n * 255 + 0.5))))
            else:
                out += b"\x00\x00\x00\x00"
    return bytes(out)


def to_bmp(size, rgba):
    xor = bytearray()
    for y in range(size - 1, -1, -1):
        row = rgba[y * size * 4:(y + 1) * size * 4]
        for x in range(size):
            r, g, b, a = row[x * 4:x * 4 + 4]
            xor += bytes((b, g, r, a))
    mask_stride = ((size + 31) // 32) * 4
    and_mask = bytes(mask_stride * size)
    hdr = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                      len(xor) + len(and_mask), 0, 0, 0, 0)
    return bytes(hdr) + bytes(xor) + and_mask


def to_png(size, rgba):
    import zlib

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)

    raw = bytearray()
    for y in range(size):
        raw.append(0)
        raw += rgba[y * size * 4:(y + 1) * size * 4]
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


def main():
    # 在仓库里从 tools/ 运行时，产物写到 assets/，和 README 引用的路径一致
    assets = Path(__file__).resolve().parent.parent / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    images = []
    for s in SIZES:
        px = render(s)
        if s == 256:
            (assets / "spark_preview.png").write_bytes(to_png(s, px))
        images.append((s, to_bmp(s, px)))
        print("rendered", s)
    ico = bytearray(struct.pack("<HHH", 0, 1, len(images)))
    offset = 6 + 16 * len(images)
    for s, data in images:
        w = 0 if s >= 256 else s
        ico += struct.pack("<BBBBHHII", w, w, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    for _, data in images:
        ico += data
    out = assets / "spark.ico"
    out.write_bytes(bytes(ico))
    print("wrote", out, out.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
