"""``hw/rtl/core/lsu_coalescer.sv`` 的用例 —— 8 lane → N 个 32B 行请求。

纯组合模块, 期望值来自独立实现的 Python 金标准(只抄 ISA §3.2 的四种情形),
覆盖: 8 lane 连续、同地址广播、部分屏蔽、全分散、同行不同字数、随机对拍。
"""

from __future__ import annotations

import random

import cocotb
from cocotb.triggers import Timer

from harness.lib.tb import Scoreboard

LANES = 8


def model(lane_en: int, addrs: list[int], is_store: int):
    """按 ISA §3.2 重新实现一遍: 按行号升序分组。"""
    groups: list[tuple[int, int, int]] = []       # (line, lane_mask, wen)
    done = 0
    while True:
        best = None
        for lane in range(LANES):
            if not (lane_en >> lane) & 1 or (done >> lane) & 1:
                continue
            line = addrs[lane] >> 5
            if best is None or line < best:
                best = line
        if best is None:
            break
        lane_mask = 0
        wen = 0
        for lane in range(LANES):
            if not (lane_en >> lane) & 1 or (done >> lane) & 1:
                continue
            if (addrs[lane] >> 5) == best:
                lane_mask |= 1 << lane
                wen |= is_store * (1 << ((addrs[lane] & 0x1F) >> 2))
                done |= 1 << lane
        groups.append((best << 5, lane_mask, wen))
    return groups


async def drive(dut, lane_en: int, addrs: list[int], is_store: int):
    dut.lane_en.value = lane_en
    dut.is_store.value = is_store
    for lane in range(LANES):
        dut.lane_addr[lane].value = addrs[lane]
    await Timer(1, "ns")            # 等组合稳定
    n = int(dut.req_num.value)
    out = []
    for r in range(n):
        line = int(dut.req_line[r].value) & 0xFFFF_FFFF
        lanes = int(dut.req_lanes[r].value) & 0xFF
        wen = int(dut.req_wen[r].value) & 0xFF
        out.append((line, lanes, wen))
    lane_word = [int(dut.lane_word[lane].value) for lane in range(LANES)]
    return out, lane_word


async def check_case(sb, dut, name: str, lane_en: int, addrs: list[int], is_store: int = 0):
    got, lane_word = await drive(dut, lane_en, addrs, is_store)
    want = model(lane_en, addrs, is_store)
    sb.check_int(len(got), len(want), f"{name}: 请求数")
    for idx, (g, w) in enumerate(zip(got, want)):
        sb.check(g[0], w[0], f"{name}: 第 {idx} 个请求的行地址")
        sb.check(g[1], w[1], f"{name}: 第 {idx} 个请求的 lane 集合")
        sb.check(g[2], w[2], f"{name}: 第 {idx} 个请求的 wen")
    for lane in range(LANES):
        sb.check(lane_word[lane], (addrs[lane] & 0x1C) >> 2, f"{name}: lane{lane} 行内字号")


@cocotb.test()
async def test_lsu_coalescer_four_cases(dut):
    sb = Scoreboard("coalescer")

    # ① 8 lane 连续 32B 对齐 ⇒ 1 个请求
    await check_case(sb, dut, "连续", 0xFF, [0x100 + 4 * lane for lane in range(8)])
    # ② 全部同地址 ⇒ 1 个请求
    await check_case(sb, dut, "广播", 0xFF, [0x200] * 8)
    # ③ 部分屏蔽: 只剩 lane 1/3/5, 且 3/5 同行
    await check_case(sb, dut, "屏蔽", 0b0010_1010, [0x300, 0x304, 0x308, 0x30C,
                                                  0x310, 0x314, 0x318, 0x31C])
    # ④ 全分散 ⇒ 8 个请求
    await check_case(sb, dut, "分散", 0xFF, [lane * 0x40 for lane in range(8)])
    # 跨两行(32B 边界)
    await check_case(sb, dut, "跨行", 0xFF, [0x3E0 + 4 * lane for lane in range(8)])
    # store 版: 同一行不同字, wen 逐字亮
    await check_case(sb, dut, "写 wen", 0xFF, [0x400 + 8 * lane for lane in range(8)], is_store=1)
    sb.report()


@cocotb.test()
async def test_lsu_coalescer_random(dut):
    sb = Scoreboard("coalescer-random")
    rng = random.Random(777)
    for _ in range(300):
        lane_en = rng.getrandbits(8)
        base = rng.randrange(0, 0x1000, 4)
        addrs = [base + rng.choice([0, 4, 8, 0x20, 0x40, rng.randrange(0, 256, 4)])
                 for _ in range(LANES)]
        await check_case(sb, dut, "随机", lane_en, addrs, is_store=rng.getrandbits(1))
    sb.report()
