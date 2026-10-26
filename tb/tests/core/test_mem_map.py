"""``hw/rtl/mem/mem_map.sv`` 的用例 —— 地址区解码(SPEC §2 / ISA D14)。

断言的期望值来自 SPEC §2 那张表(手抄一遍, 不引用 RTL 内部常量):
MMIO 窗口一律报 `MMIO_ACCESS`; 常量区只读; 不在任何区的地址报 `NO_MEM_REGION`。
"""

from __future__ import annotations

import random

import cocotb
from cocotb.triggers import Timer

from harness.lib.tb import Scoreboard

REG_NONE, REG_MMIO, REG_QUEUE, REG_CONST, REG_IMAGE, REG_SEM, REG_FB, REG_DDR = range(8)


def spec(addr: int):
    """SPEC §2 的权威表(与 RTL 无关的第二份实现)。"""
    if addr < 0x0000_1000:
        return REG_MMIO, False, True, False
    if 0x0100_0000 <= addr < 0x0100_1000:
        return REG_QUEUE, True, False, False
    if 0x0200_0000 <= addr <= 0x0200_0FFF:
        return REG_CONST, False, False, False
    if 0x0200_1000 <= addr <= 0x0200_7FFF:
        return REG_IMAGE, True, False, False
    if 0x0300_0000 <= addr < 0x0400_0000:
        return REG_SEM, True, False, False
    if 0x1000_0000 <= addr < 0x8000_0000:
        return REG_FB, True, False, False
    if addr >= 0x8000_0000:
        return REG_DDR, True, False, False
    return REG_NONE, False, False, True


async def drive(dut, addr: int, is_store: int = 0):
    dut.addr.value = addr
    dut.is_store.value = is_store
    await Timer(1, "ns")
    return (int(dut.region.value), bool(int(dut.writable.value)),
            bool(int(dut.err_mmio.value)), bool(int(dut.err_no_region.value)))


@cocotb.test()
async def test_regions(dut):
    sb = Scoreboard("mem_map")
    probes = [
        0x0000_0000, 0x0000_0FFC,                 # MMIO
        0x0100_0000, 0x0100_0FF8,                 # 命令环
        0x0200_0000, 0x0200_0FFC,                 # 常量区
        0x0200_1000, 0x0200_7FFC,                 # 内核镜像
        0x0300_0000, 0x03FF_FFFC,                 # 信号量
        0x1000_0000, 0x7FFF_FFFC,                 # 帧缓冲/自分配
        0x8000_0000, 0xFFFF_FFFC,                 # DDR
        0x0400_0000, 0x0FFF_FFFC, 0x8000_0000 - 4,  # 空洞
    ]
    for addr in probes:
        got = await drive(dut, addr)
        want = spec(addr)
        sb.check(got, want, f"addr=0x{addr:08x} 的 (region, writable, err_mmio, err_no_region)")
    sb.report()


@cocotb.test()
async def test_random(dut):
    sb = Scoreboard("mem_map-random")
    rng = random.Random(4242)
    for _ in range(500):
        addr = rng.randrange(0, 1 << 32) & 0xFFFF_FFFC
        got = await drive(dut, addr, is_store=rng.getrandbits(1))
        want = spec(addr)
        sb.check(got, want, f"随机 addr=0x{addr:08x}")
    sb.report()
