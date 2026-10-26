"""cp_mem_adapter: CP 的行/字访存协议 → AXI4(与 DDR 模型对拍)。"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, RisingEdge, Timer

from harness.lib.tb import Scoreboard

LINE = 8


async def fresh(dut):
    cocotb.start_soon(Clock(dut.clk, 10, "ns").start())
    dut.rst.value = 1
    dut.rd_req.value = 0
    dut.rd_addr.value = 0
    dut.rd_line.value = 0
    dut.wr_req.value = 0
    dut.wr_addr.value = 0
    dut.wr_data.value = 0
    dut.wr_strb.value = 0xF
    dut.ram_we.value = 0
    dut.ram_addr.value = 0
    dut.ram_wdata.value = 0
    await RisingEdge(dut.clk)
    await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)
    await FallingEdge(dut.clk)


async def host_write(dut, addr, data):
    dut.ram_we.value = 1
    dut.ram_addr.value = addr
    dut.ram_wdata.value = data
    await RisingEdge(dut.clk)
    dut.ram_we.value = 0


async def host_read(dut, addr):
    dut.ram_addr.value = addr
    await Timer(1, "ns")
    return int(dut.ram_rdata.value)


async def cp_line_read(dut, addr, max_cycles=400):
    """发一次 64B 行读, 返回 16 个字。"""
    dut.rd_addr.value = addr
    dut.rd_line.value = 1
    dut.rd_req.value = 1
    await RisingEdge(dut.clk)
    dut.rd_req.value = 0
    # 等 ack
    acked = False
    for _ in range(max_cycles):
        await RisingEdge(dut.clk)
        if int(dut.rd_ack.value):
            acked = True
            break
    assert acked, "rd_ack 超时"
    words = []
    for _ in range(max_cycles):
        await RisingEdge(dut.clk)
        if int(dut.rd_rsp.value):
            for w in range(16):
                words.append((int(dut.rd_data.value) >> (32 * w)) & 0xFFFF_FFFF)
            break
    else:
        raise AssertionError("rd_rsp 超时")
    await FallingEdge(dut.clk)
    return words, int(dut.rd_err.value)


async def cp_word_read(dut, addr, max_cycles=400):
    dut.rd_addr.value = addr
    dut.rd_line.value = 0
    dut.rd_req.value = 1
    await RisingEdge(dut.clk)
    dut.rd_req.value = 0
    for _ in range(max_cycles):
        await RisingEdge(dut.clk)
        if int(dut.rd_rsp.value):
            w0 = int(dut.rd_data.value) & 0xFFFF_FFFF
            await FallingEdge(dut.clk)
            return w0
    raise AssertionError("rd_rsp 超时")


async def cp_word_write(dut, addr, data, max_cycles=400):
    dut.wr_addr.value = addr
    dut.wr_data.value = data
    dut.wr_strb.value = 0xF
    dut.wr_req.value = 1
    await RisingEdge(dut.clk)
    dut.wr_req.value = 0
    for _ in range(max_cycles):
        await RisingEdge(dut.clk)
        if int(dut.wr_rsp.value):
            ok = int(dut.wr_err.value) == 0
            await FallingEdge(dut.clk)
            return ok
    raise AssertionError("wr_rsp 超时")


@cocotb.test()
async def test_line_read(dut):
    """64B 行读: 16 个字全对, AR 计数 = 2(两段 32B)。"""
    sb = Scoreboard("cp_adapter line read")
    await fresh(dut)
    for i in range(16):
        await host_write(dut, 0x200 + 4 * i, 0xC000 + i)
    words, err = await cp_line_read(dut, 0x200)
    sb.check_equal(words, [0xC000 + i for i in range(16)], "line data")
    sb.check_equal(err, 0, "no error")
    sb.check_equal(int(dut.stat_ar_cnt.value), 2, "两段 burst")
    sb.report()


@cocotb.test()
async def test_line_read_unaligned_offset(dut):
    """行地址不在 64B 边界: 仍从 64B 边界开始取整行。"""
    sb = Scoreboard("cp_adapter line read offset")
    await fresh(dut)
    for i in range(16):
        await host_write(dut, 0x300 + 4 * i, 0xD000 + i)
    words, _ = await cp_line_read(dut, 0x310)
    sb.check_equal(words, [0xD000 + i for i in range(16)], "整行数据")
    sb.report()


@cocotb.test()
async def test_word_read_write(dut):
    """字读写: 单个 4B 事务。"""
    sb = Scoreboard("cp_adapter word")
    await fresh(dut)
    await host_write(dut, 0x400, 0x1234_5678)
    sb.check_equal(await cp_word_read(dut, 0x400), 0x1234_5678, "word read")
    ok = await cp_word_write(dut, 0x404, 0xDEAD_BEEF)
    sb.check_equal(ok, True, "word write ack")
    await Timer(1, "ns")
    sb.check_equal(await host_read(dut, 0x404), 0xDEAD_BEEF, "word write landed")
    sb.check_equal(int(dut.stat_aw_cnt.value), 1, "AW count")
    sb.report()

