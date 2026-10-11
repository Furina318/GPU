#!/usr/bin/env python3
"""2D 引擎的独立参考模型 —— 按 docs/GFX.md §2/§3/§5/§6 从零实现一遍,与 C++ 引擎逐像素对拍。

为什么要独立实现: `2D-engine/gfx2d.cpp` 里的每一条规则(定点边方程、top-left 填充规则、
重心插值、混合顺序、格式转换的舍入)都是"错一位就看不出来"的东西。只跑一个"看起来对"的
三角形说明不了什么;真正的判据是**同一条命令流在两个独立实现下产出同一份帧缓冲**。

用法(与模拟器吃同一份命令脚本,所以两边不会各写各的用例):
    python3 tests/gfx2d_ref.py --script s.txt --fb-base 0x10000000 --fb-words 4096 --out exp.txt
可选:
    --preload 0x10001000 d.txt   从模拟器的 --dump 文件预载一段内存(如 shader 产出的顶点)

脚本里认识的命令(其余行忽略): setreg / fill / blit / draw / flush / mem / write_reg 0x38
"""

import argparse
import math
import struct
import sys

FB_REG_OFF = 0x38          # CMDS §7: FB_BASE


def f32(x):
    """把 double 结果截成 FP32 —— C++ 里颜色插值/混合用的是 float(§5.4/§6.2)。"""
    return struct.unpack("<f", struct.pack("<f", x))[0]


def clamp_u8(v):
    if not v > 0.0:            # 同时挡住 NaN
        return 0
    if v > 1.0:
        v = 1.0
    return int((v * 255.0) + 0.5)      # floor(v*255+0.5)(§6.3)


def clamp_bits(v, n):
    if not v > 0.0:
        return 0
    if v > 1.0:
        v = 1.0
    return int(v * ((1 << n) - 1) + 0.5)


class Ref2D:
    def __init__(self):
        self.mem = {}                  # 字地址 → 值(未写过 = 0)
        self.fb_base = 0x1000_0000
        self.fb_size = 8 << 20
        # 寄存器(GFX.md §3)
        self.fb_w = self.fb_h = self.fb_stride = 0
        self.fb_fmt = 0
        self.sx = self.sy = self.sw = self.sh = 0
        self.blend = 0
        self.colkey = 0
        self.colkey_en = False
        self.stats = dict(fill=0, blit=0, draw=0, killed=0, clipped=0)

    # ── 内存 ──
    def ld(self, addr):
        return self.mem.get(addr & ~3, 0)

    def st(self, addr, v):
        self.mem[addr & ~3] = v & 0xFFFF_FFFF

    def ld16(self, addr):
        w = self.ld(addr)
        return (w >> 16) if (addr & 2) else (w & 0xFFFF)

    # ── 像素 ──
    def bpp(self):
        return 2 if self.fb_fmt == 1 else 4

    def stride(self):
        return self.fb_stride or (self.fb_w * self.bpp())

    def expand(self, bits):
        if self.fb_fmt == 1:
            return ((bits >> 11) & 0x1F) / 31.0, ((bits >> 5) & 0x3F) / 63.0, (bits & 0x1F) / 31.0, 1.0
        # ARGB8888(§2.2): [31:24]=A, [23:16]=R, [15:8]=G, [7:0]=B
        return (((bits >> 16) & 0xFF) / 255.0, ((bits >> 8) & 0xFF) / 255.0,
                (bits & 0xFF) / 255.0, ((bits >> 24) & 0xFF) / 255.0)

    def convert(self, c):
        r, g, b, a = c
        if self.fb_fmt == 1:
            return (clamp_bits(r, 5) << 11) | (clamp_bits(g, 6) << 5) | clamp_bits(b, 5)
        return (clamp_u8(a) << 24) | (clamp_u8(r) << 16) | (clamp_u8(g) << 8) | clamp_u8(b)

    def load_pixel(self, fmt, addr):
        if fmt == 1:
            return self.expand(self.ld16(addr))
        return self.expand(self.ld(addr))

    def emit(self, addr, c):
        if not (self.fb_base <= addr and (addr - self.fb_base) + self.bpp() <= self.fb_size):
            self.stats["clipped"] += 1
            return
        out = c
        if self.blend:                                    # §6.2, 逐通道 FP32
            d = self.load_pixel(self.fb_fmt, addr)
            if self.blend == 1:                           # SRC_OVER
                a = f32(c[3])
                out = tuple(f32(f32(c[i] * a) + f32(d[i] * f32(1.0 - a))) for i in range(4))
            else:                                         # ADD
                out = tuple(min(f32(c[i] + d[i]), 1.0) for i in range(4))
        bits = self.convert(out)
        if self.bpp() == 4:
            self.st(addr, bits)
        else:                                             # RGB565: 半字合并
            cur = self.ld(addr)
            self.st(addr, ((cur & 0x0000_FFFF) | (bits << 16)) if (addr & 2)
                    else ((cur & 0xFFFF_0000) | (bits & 0xFFFF)))

    def clipped(self, x, y):
        if not (self.sw and self.sh):
            return False
        return not (self.sx <= x < self.sx + self.sw and self.sy <= y < self.sy + self.sh)

    # ── 命令 ──
    def fill(self, dst, color, x, y, w, h):
        bits = (color & 0xFFFF) if self.fb_fmt == 1 else color
        c = self.expand(bits)
        stride, bpp = self.stride(), self.bpp()
        for dy in range(h):
            for dx in range(w):
                ax, ay = x + dx, y + dy
                if self.clipped(ax, ay):
                    self.stats["clipped"] += 1
                    continue
                self.emit(dst + dy * stride + dx * bpp, c)
                self.stats["fill"] += 1

    def blit(self, src, dst, sstride, x, y, w, h, flags, colkey):
        sfmt = (flags >> 3) & 1
        sbpp = 2 if sfmt == 1 else 4
        dbpp = self.bpp()
        sstride_eff = sstride or (w * sbpp)
        ck, fx, fy = (flags & 1) != 0, (flags & 2) != 0, (flags & 4) != 0
        mask = 0xFFFF if sbpp == 2 else 0xFFFF_FFFF
        stride = self.stride()
        for dy in range(h):
            for dx in range(w):
                sx = (w - 1 - dx) if fx else dx
                sy = (h - 1 - dy) if fy else dy
                saddr = src + sy * sstride_eff + sx * sbpp
                sbits = self.ld16(saddr) if sbpp == 2 else self.ld(saddr)
                if ck and (sbits & mask) == (colkey & mask):
                    self.stats["killed"] += 1
                    continue
                ax, ay = x + dx, y + dy
                if self.clipped(ax, ay):
                    self.stats["clipped"] += 1
                    continue
                c = self.expand(sbits) if sfmt == self.fb_fmt else self.load_pixel(sfmt, saddr)
                self.emit(dst + dy * stride + dx * dbpp, c)
                self.stats["blit"] += 1

    def draw(self, vtx_addr, count, flags):
        clip_en = (flags & 1) != 0
        for t in range(0, count - 2, 3):
            vs = []
            for k in range(3):
                a = vtx_addr + (t + k) * 24
                vals = [struct.unpack("<f", struct.pack("<I", self.ld(a + j * 4)))[0] for j in range(6)]
                vs.append([float(v) for v in vals])
            self.triangle(*vs, clip_en=clip_en)
        self.stats["draw"] += 0

    def triangle(self, v0, v1, v2, clip_en):
        def to_fx(v):                                       # §5.2: s32.16, 与 C++ 的 llround 一致(半值远离 0)
            x = v * 65536.0
            return int(math.floor(x + 0.5)) if x >= 0 else int(math.ceil(x - 0.5))
        X0, Y0, X1, Y1, X2, Y2 = (to_fx(v0[0]), to_fx(v0[1]), to_fx(v1[0]),
                                 to_fx(v1[1]), to_fx(v2[0]), to_fx(v2[1]))

        def edge(ax, ay, bx, by, px, py):
            return (bx - ax) * (py - ay) - (by - ay) * (px - ax)

        area = edge(X0, Y0, X1, Y1, X2, Y2)
        if area == 0:
            self.stats["clipped"] += 1
            return
        sgn = -1 if area < 0 else 1
        A = sgn * area
        e = [(X0, Y0, X1, Y1), (X1, Y1, X2, Y2), (X2, Y2, X0, Y0)]
        tl = []
        for (ax, ay, bx, by) in e:
            dx, dy = sgn * (bx - ax), sgn * (by - ay)
            tl.append((dy > 0) or (dy == 0 and dx < 0))    # §5.3 top/left
        bx0 = max(0, int(math.floor(min(v0[0], v1[0], v2[0]))))
        bx1 = min(self.fb_w - 1, int(math.ceil(max(v0[0], v1[0], v2[0]))))
        by0 = max(0, int(math.floor(min(v0[1], v1[1], v2[1]))))
        by1 = min(self.fb_h - 1, int(math.ceil(max(v0[1], v1[1], v2[1]))))
        stride = self.stride()
        for py in range(by0, by1 + 1):
            for px in range(bx0, bx1 + 1):
                CX, CY = px * 65536 + 32768, py * 65536 + 32768      # 像素中心(§5.1)
                es = [sgn * edge(*e[i], CX, CY) for i in range(3)]
                if not all(es[i] > 0 or (es[i] == 0 and tl[i]) for i in range(3)):
                    continue
                if clip_en and self.clipped(px, py):
                    self.stats["clipped"] += 1
                    continue
                l0, l1, l2 = es[1] / A, es[2] / A, es[0] / A         # §5.3 的重心坐标
                c = tuple(f32(l0 * v0[2 + i] + l1 * v1[2 + i] + l2 * v2[2 + i]) for i in range(4))
                self.emit(self.fb_base + py * stride + px * self.bpp(), c)
                self.stats["draw"] += 1

    # ── 脚本(解析成命令表: 预载内存之后再按序执行, 因为混合/颜色键不是幂等的) ──
    def parse(self, path):
        ops = []
        for line in open(path, encoding="utf-8"):
            line = line.split("#")[0].split()
            if not line:
                continue
            try:
                n = [int(t, 0) for t in line[1:]]
            except ValueError:
                continue
            ops.append((line[0], n))
        return ops

    def execute(self, ops):
        for c, n in ops:
            if c == "mem" and len(n) >= 2:
                for i, v in enumerate(n[1:]):
                    self.st(n[0] + i * 4, v)
            elif c == "write_reg" and len(n) >= 2 and (n[0] & 0xFF) == FB_REG_OFF:
                self.fb_base = n[1]
            elif c == "setreg" and len(n) >= 2:
                self.set_reg(n[0], n[1])
            elif c == "fill" and len(n) >= 6:
                self.fill(n[0], n[1], n[2], n[3], n[4], n[5])
            elif c == "blit" and len(n) >= 9:
                self.blit(n[0], n[1], n[2], n[3], n[4], n[5], n[6], n[7], n[8])
            elif c == "draw" and len(n) >= 3:
                self.draw(n[0], n[1], n[2])

    def set_reg(self, off, val):
        off &= 0xFFFF
        if off == 0x00:
            self.fb_w = val
        elif off == 0x04:
            self.fb_h = val
        elif off == 0x08:
            self.fb_stride = val
        elif off == 0x0C:
            self.fb_fmt = val
        elif off == 0x10:
            self.sx = val
        elif off == 0x14:
            self.sy = val
        elif off == 0x18:
            self.sw = val
        elif off == 0x1C:
            self.sh = val
        elif off == 0x20:
            self.blend = val
        elif off == 0x24:
            self.colkey = val
        elif off == 0x28:
            self.colkey_en = val != 0


def load_dump(path):
    """读模拟器 --dump 的输出(每行 "地址 十六进制 浮点"), 返回 [(addr, [word...])]。"""
    words = []
    base = None
    for line in open(path, encoding="utf-8"):
        p = line.split()
        if len(p) < 2:
            continue
        a, v = int(p[0], 0), int(p[1], 16)
        if base is None:
            base = a
        words.append(v)
    return [(base, words)] if base is not None else []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--script", required=True)
    ap.add_argument("--fb-base", default="0x10000000")
    ap.add_argument("--fb-words", type=int, default=4096)
    ap.add_argument("--out", required=True)
    ap.add_argument("--preload", nargs=2, action="append", default=[],
                    metavar=("ADDR", "DUMPFILE"))
    a = ap.parse_args()

    ref = Ref2D()
    ref.fb_base = int(a.fb_base, 0)
    ops = ref.parse(a.script)
    # --preload 进来的内存(shader 产出的顶点等)先就位, 之后按脚本顺序执行命令
    for addr, path in a.preload:
        for base, words in load_dump(path):
            for i, v in enumerate(words):
                ref.st(int(addr, 0) + i * 4, v)
    ref.execute(ops)

    with open(a.out, "w", encoding="utf-8") as f:
        for i in range(a.fb_words):
            addr = ref.fb_base + i * 4
            v = ref.ld(addr)
            f.write("0x%08x 0x%08x\n" % (addr, v))
    print("2D 参考模型: 帧缓冲 %d 字 → %s (fill=%d blit=%d draw=%d 颜色键丢弃=%d 裁剪=%d)"
          % (a.fb_words, a.out, ref.stats["fill"], ref.stats["blit"], ref.stats["draw"],
             ref.stats["killed"], ref.stats["clipped"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
