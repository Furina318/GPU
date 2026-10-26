"""global_mem(行读写 + WCB 写合并 + AXI4 主口)的 cocotb 用例。

DUT 是仿真胶水 `tb/harness/rtl/global_mem_wrap.sv`:global_mem + 一块 Verilog 的
AXI 从设备内存模型(`tb/harness/rtl/axi_ram_model.sv`)。用例只从 SM 侧的行读写口
观察行为,内存内容经 `ram_*` 旁路口铺设与比对 —— 等价于"外部 DDR + host"。

时序约定(与本仓库其它用例一致):在 `FallingEdge` 驱动输入,推进一个时钟沿后
再在 `FallingEdge` 采样输出。
"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, RisingEdge, Timer

from harness.lib.tb import Scoreboard

LINE_WORDS = 8
LINE_BYTES = 32


async def fresh(dut):
    """启动时钟 + 复位 + 空闲所有输入。"""
    cocotb.start_soon(Clock(dut.clk, 10, "ns").start())
    dut.rst.value = 1
    dut.rd_valid.value = 0
    dut.rd_line.value = 0
    dut.rd_wen.value = 0
    dut.wr_valid.value = 0
    dut.wr_line.value = 0
    dut.wr_wen.value = 0
    dut.flush_req.value = 0
    dut.ram_we.value = 0
    dut.ram_addr.value = 0
    dut.ram_wdata.value = 0
    for _ in range(3):
        await RisingEdge(dut.clk)
    dut.rst.value = 0
    await FallingEdge(dut.clk)


async def tick(dut, n=1):
    for _ in range(n):
        await RisingEdge(dut.clk)
        await FallingEdge(dut.clk)


# ---------------------------------------------------------------------------
#  host 旁路:直接读写内存模型
# ---------------------------------------------------------------------------
async def host_write(dut, addr: int, data: int):
    dut.ram_we.value = 1
    dut.ram_addr.value = addr
    dut.ram_wdata.value = data
    await RisingEdge(dut.clk)
    await FallingEdge(dut.clk)
    dut.ram_we.value = 0
    await Timer(1, "ns")


async def host_read(dut, addr: int) -> int:
    dut.ram_addr.value = addr
    await Timer(1, "ns")
    return int(dut.ram_rdata.value)


async def host_fill_line(dut, line: int, base: int):
    for w in range(LINE_WORDS):
        await host_write(dut, (line << 5) + 4 * w, base + w)


# ---------------------------------------------------------------------------
#  SM 侧:行读 / 行写 / 冲刷
# ---------------------------------------------------------------------------
async def line_read(dut, line: int, wen: int = 0xFF, max_cycles: int = 200):
    dut.rd_valid.value = 1
    dut.rd_line.value = line
    dut.rd_wen.value = wen
    await Timer(1, "ns")
    for _ in range(max_cycles):
        await RisingEdge(dut.clk)
        await FallingEdge(dut.clk)
        await Timer(1, "ns")
        if int(dut.rsp_valid.value):
            dut.rd_valid.value = 0
            words = [int(dut.rsp_data[w].value) for w in range(LINE_WORDS)]
            return words, int(dut.rsp_err.value), _
    dut.rd_valid.value = 0
    raise AssertionError("行读超时")


async def line_store(dut, line: int, wen: int, wdata: list[int], max_cycles: int = 200):
    dut.wr_valid.value = 1
    dut.wr_line.value = line
    dut.wr_wen.value = wen
    # 2D 打包数组在 Verilator 侧按元素暴露成 8 个句柄, 必须逐 lane 赋值
    for w in range(LINE_WORDS):
        dut.wr_wdata[w].value = wdata[w]
    await Timer(1, "ns")
    for _ in range(max_cycles):
        # 握手判定必须在时钟沿**之前**做: wr_ready 与 wr_valid 同拍高,
        # 本拍末尾的时钟沿才完成传输。
        if int(dut.wr_ready.value):
            await RisingEdge(dut.clk)
            dut.wr_valid.value = 0
            await FallingEdge(dut.clk)
            return
        await RisingEdge(dut.clk)
        await FallingEdge(dut.clk)
        await Timer(1, "ns")
    dut.wr_valid.value = 0
    raise AssertionError("行写超时")


async def do_flush(dut, max_cycles: int = 400):
    await FallingEdge(dut.clk)
    dut.flush_req.value = 1
    await Timer(1, "ns")
    for _ in range(max_cycles):
        await RisingEdge(dut.clk)
        await FallingEdge(dut.clk)
        await Timer(1, "ns")
        if int(dut.flush_done.value):
            dut.flush_req.value = 0
            await tick(dut)
            return
    dut.flush_req.value = 0
    raise AssertionError("flush 超时")


# ---------------------------------------------------------------------------
@cocotb.test()
async def test_line_read_from_memory(dut):
    """整行读:8 拍 AXI 读, 数据按 lane 顺序拼回来。"""
    await fresh(dut)
    sb = Scoreboard("line_read")
    await host_fill_line(dut, 0x10, 0x1000)
    ar0 = int(dut.stat_ar_cnt.value)

    words, err, _ = await line_read(dut, 0x10)
    sb.check_int(err, 0, "rsp_err")
    for w in range(LINE_WORDS):
        sb.check_int(words[w], 0x1000 + w, f"lane{w}")
    sb.check_int(int(dut.stat_ar_cnt.value) - ar0, 1, "AR 事务数")
    sb.report()


@cocotb.test()
async def test_store_merge_and_flush(dut):
    """行写先进 WCB, flush 后落盘; 未写的字保持原值(按 wstrb 逐字节)。"""
    await fresh(dut)
    sb = Scoreboard("store")
    await host_fill_line(dut, 0x20, 0x2000)

    # 只写低 4 个 lane
    await line_store(dut, 0x20, 0x0F, [0xAA00 + w for w in range(LINE_WORDS)])
    await do_flush(dut)

    for w in range(4):
        sb.check_int(await host_read(dut, (0x20 << 5) + 4 * w), 0xAA00 + w, f"写 lane{w}")
    for w in range(4, LINE_WORDS):
        sb.check_int(await host_read(dut, (0x20 << 5) + 4 * w), 0x2000 + w, f"保留 lane{w}")
    sb.report()


@cocotb.test()
async def test_read_hit_bypasses_bus(dut):
    """读请求的字全在 WCB 里 ⇒ 直接回, 不发 AXI 读(读旁路 D2)。"""
    await fresh(dut)
    sb = Scoreboard("bypass")
    await host_fill_line(dut, 0x30, 0x3000)
    await line_store(dut, 0x30, 0xFF, [0xBB00 + w for w in range(LINE_WORDS)])
    ar0 = int(dut.stat_ar_cnt.value)

    words, err, _ = await line_read(dut, 0x30)
    sb.check_int(err, 0, "rsp_err")
    for w in range(LINE_WORDS):
        sb.check_int(words[w], 0xBB00 + w, f"lane{w}")
    sb.check_int(int(dut.stat_ar_cnt.value) - ar0, 0, "不应有 AR 事务")
    sb.report()


@cocotb.test()
async def test_many_lines_backpressure(dut):
    """写超过 WCB 项数的行:靠自动冲刷腾位置, 全部落盘且不丢数据。"""
    await fresh(dut)
    sb = Scoreboard("many_lines")
    n_lines = 12
    for i in range(n_lines):
        await host_fill_line(dut, 0x40 + i, 0x4000 + (i << 8))

    for i in range(n_lines):
        await line_store(dut, 0x40 + i, 0xFF, [0xC000 + i * 16 + w for w in range(LINE_WORDS)])
    await do_flush(dut)

    for i in range(n_lines):
        for w in range(LINE_WORDS):
            sb.check_int(await host_read(dut, ((0x40 + i) << 5) + 4 * w),
                         0xC000 + i * 16 + w, f"line{i} lane{w}")
    sb.report()

