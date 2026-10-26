"""axi_arb: 多主仲裁 + 响应路由 + 写通道(用例与 DDR 模型对拍)。

仲裁口径(SPEC §3.4 / MEM_LSU.md §4):
  · 读/写通道各自独立, 一次只有一个主在飞;
  · CP(索引 0)最高优先, 其余主从上次服务者之后轮转;
  · 未授予的主 s_*ready 恒 0;
  · 响应只回给当前在飞的主。
"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, RisingEdge, Timer

from harness.lib.tb import Scoreboard

NM = 3          # CP / SM0 / SM1
LINE = 8


def poke_write(dut, addr, data):
    dut.ram_we.value = 1
    dut.ram_addr.value = addr
    dut.ram_wdata.value = data


async def ram_write(dut, addr, data):
    poke_write(dut, addr, data)
    await RisingEdge(dut.clk)
    dut.ram_we.value = 0


async def ram_read(dut, addr):
    dut.ram_addr.value = addr
    await Timer(1, "ns")
    return int(dut.ram_rdata.value)


async def fresh(dut):
    cocotb.start_soon(Clock(dut.clk, 10, "ns").start())
    dut.rst.value = 1
    dut.ram_we.value = 0
    dut.ram_addr.value = 0
    dut.ram_wdata.value = 0
    for i in range(NM):
        dut.s_arvalid[i].value = 0
        dut.s_araddr[i].value = 0
        dut.s_arlen[i].value = 0
        dut.s_rready[i].value = 0
        dut.s_awvalid[i].value = 0
        dut.s_awaddr[i].value = 0
        dut.s_awlen[i].value = 0
        dut.s_wvalid[i].value = 0
        dut.s_wdata[i].value = 0
        dut.s_wstrb[i].value = 0
        dut.s_wlast[i].value = 0
        dut.s_bready[i].value = 0
    await RisingEdge(dut.clk)
    await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)


async def master_read(dut, m, addr, beats, sb, tag):
    """发一次 len=beats-1 的读, 收 beats 拍数据。"""
    dut.s_araddr[m].value = addr
    dut.s_arlen[m].value = beats - 1
    dut.s_arvalid[m].value = 1
    dut.s_rready[m].value = 1

    # 握手: ready 在沿前可见, 沿后撤 valid
    for _ in range(400):
        await RisingEdge(dut.clk)
        if int(dut.s_arready[m].value):
            break
    else:
        sb.check(False, f"{tag}: AR handshake timeout")
        return []
    await FallingEdge(dut.clk)
    dut.s_arvalid[m].value = 0

    got = []
    for _ in range(400):
        await RisingEdge(dut.clk)
        if int(dut.s_rvalid[m].value):
            got.append(int(dut.s_rdata[m].value))
            if int(dut.s_rlast[m].value):
                break
    else:
        sb.check(False, f"{tag}: R timeout ({len(got)} beats)")
    await FallingEdge(dut.clk)
    dut.s_rready[m].value = 0
    return got


async def master_write(dut, m, addr, words, sb, tag):
    """发一次写 burst(每拍一个 32b 字, strb 全 1)。"""
    dut.s_awaddr[m].value = addr
    dut.s_awlen[m].value = len(words) - 1
    dut.s_awvalid[m].value = 1
    for _ in range(400):
        await RisingEdge(dut.clk)
        if int(dut.s_awready[m].value):
            break
    else:
        sb.check(False, f"{tag}: AW handshake timeout")
        return
    await FallingEdge(dut.clk)
    dut.s_awvalid[m].value = 0

    # W 通道
    dut.s_bready[m].value = 1
    for k, w in enumerate(words):
        dut.s_wdata[m].value = w
        dut.s_wstrb[m].value = 0xF
        dut.s_wlast[m].value = 1 if k == len(words) - 1 else 0
        dut.s_wvalid[m].value = 1
        await RisingEdge(dut.clk)
        dut.s_wvalid[m].value = 0
    # B 通道
    for _ in range(400):
        await RisingEdge(dut.clk)
        if int(dut.s_bvalid[m].value):
            break
    else:
        sb.check(False, f"{tag}: B timeout")
    await FallingEdge(dut.clk)
    dut.s_bready[m].value = 0


@cocotb.test()
async def test_two_masters_read(dut):
    """两个主同时读: 各自拿到自己的数据, AR 计数 = 2。"""
    sb = Scoreboard("axi_arb read")
    await fresh(dut)
    for i in range(LINE):
        await ram_write(dut, 0x100 + 4 * i, 0x1000 + i)
        await ram_write(dut, 0x200 + 4 * i, 0x2000 + i)

    t0 = cocotb.start_soon(master_read(dut, 0, 0x100, LINE, sb, "m0"))
    t1 = cocotb.start_soon(master_read(dut, 2, 0x200, LINE, sb, "m2"))
    d0 = await t0
    d2 = await t1
    sb.check_equal(d0, [0x1000 + i for i in range(LINE)], "m0 data")
    sb.check_equal(d2, [0x2000 + i for i in range(LINE)], "m2 data")
    sb.check_equal(int(dut.stat_ar_cnt.value), 2, "AR count")
    sb.report()


@cocotb.test()
async def test_cp_priority(dut):
    """CP(主 0)与 SM0(主 1)同时请求: CP 先被服务(响应先到 CP)。"""
    sb = Scoreboard("axi_arb priority")
    await fresh(dut)
    for i in range(LINE):
        await ram_write(dut, 0x100 + 4 * i, 0xA0 + i)
        await ram_write(dut, 0x200 + 4 * i, 0xB0 + i)

    t0 = cocotb.start_soon(master_read(dut, 0, 0x100, LINE, sb, "cp"))
    await RisingEdge(dut.clk)
    t1 = cocotb.start_soon(master_read(dut, 1, 0x200, LINE, sb, "sm0"))
    d0 = await t0
    d1 = await t1
    sb.check_equal(d0, [0xA0 + i for i in range(LINE)], "cp data")
    sb.check_equal(d1, [0xB0 + i for i in range(LINE)], "sm0 data")
    sb.check_equal(int(dut.stat_ar_cnt.value), 2, "AR count")
    sb.report()


@cocotb.test()
async def test_write_burst(dut):
    """写通道: AW/W/B 握手, 数据真的落进 DDR。"""
    sb = Scoreboard("axi_arb write")
    await fresh(dut)
    words = [0xDEAD_0000 + i for i in range(4)]
    await master_write(dut, 1, 0x300, words, sb, "w")
    await Timer(1, "ns")
    got = [await ram_read(dut, 0x300 + 4 * i) for i in range(4)]
    sb.check_equal(got, words, "DDR content")
    sb.check_equal(int(dut.stat_aw_cnt.value), 1, "AW count")
    sb.report()


@cocotb.test()
async def test_back_to_back_same_master(dut):
    """同一个主连发两次: 第二次在第一次 R 收完后立刻被接受。"""
    sb = Scoreboard("axi_arb back-to-back")
    await fresh(dut)
    for i in range(LINE):
        await ram_write(dut, 0x400 + 4 * i, 0x4000 + i)
        await ram_write(dut, 0x440 + 4 * i, 0x4400 + i)
    d0 = await master_read(dut, 0, 0x400, LINE, sb, "b0")
    d1 = await master_read(dut, 0, 0x440, LINE, sb, "b1")
    sb.check_equal(d0, [0x4000 + i for i in range(LINE)], "first")
    sb.check_equal(d1, [0x4400 + i for i in range(LINE)], "second")
    sb.check_equal(int(dut.stat_ar_cnt.value), 2, "AR count")
    sb.report()
