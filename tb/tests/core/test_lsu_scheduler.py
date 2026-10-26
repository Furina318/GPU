"""lsu_scheduler 用例: 轮转仲裁、owner 路由、写走 WCB、flush 回执。"""
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, RisingEdge, Timer

from harness.lib.tb import Scoreboard

WARPS = 4
LANES = 8


async def setup(dut):
    cocotb.start_soon(Clock(dut.clk, 10, "ns").start())
    dut.rst.value = 1
    for w in range(WARPS):
        dut.w_smem_req_valid[w].value = 0
        dut.w_smem_req_we[w].value = 0
        dut.w_smem_lane_en[w].value = 0
        for i in range(LANES):
            dut.w_smem_lane_addr[w][i].value = 0
            dut.w_smem_lane_wdata[w][i].value = 0
        dut.w_gmem_req_valid[w].value = 0
        dut.w_gmem_req_we[w].value = 0
        dut.w_gmem_req_line[w].value = 0
        dut.w_gmem_req_wen[w].value = 0
        for i in range(LANES):
            dut.w_gmem_req_wdata[w][i].value = 0
        dut.w_flush_req[w].value = 0
    dut.smem_req_ready.value = 1
    dut.smem_rsp_valid.value = 0
    dut.smem_conflict.value = 0
    dut.smem_err_align.value = 0
    dut.smem_err_oob.value = 0
    dut.gmem_rd_ready.value = 1
    dut.gmem_rsp_valid.value = 0
    dut.gmem_rsp_line.value = 0
    dut.gmem_rsp_err.value = 0
    for i in range(LANES):
        dut.gmem_rsp_data[i].value = 0
        dut.smem_rsp_data[i].value = 0
    dut.gmem_wr_ready.value = 1
    dut.flush_done.value = 0
    await RisingEdge(dut.clk)
    await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)


def set_smem(dut, w, addr0, val):
    dut.w_smem_req_valid[w].value = 1
    dut.w_smem_lane_en[w].value = 0xFF
    for i in range(LANES):
        dut.w_smem_lane_addr[w][i].value = addr0 + 4 * i
        dut.w_smem_lane_wdata[w][i].value = val + i


async def wait_grant(dut, w, max_cycles=50):
    for _ in range(max_cycles):
        await Timer(1, "ns")
        if int(dut.w_smem_req_ready[w].value):
            return True
        await FallingEdge(dut.clk)
    return False


@cocotb.test()
async def test_shared_round_robin(dut):
    await setup(dut)
    sb = Scoreboard("lsu_scheduler")
    # 两个 warp 同时要共享口: 先给低号, 再轮转到下一个
    set_smem(dut, 0, 0x100, 0x1000)
    set_smem(dut, 2, 0x200, 0x2000)
    await Timer(1, "ns")

    order = []
    for _ in range(2):
        for w in range(WARPS):
            if int(dut.w_smem_req_ready[w].value):
                order.append(w)
                # 该 warp 被授予, 下一拍把请求撤掉并给响应
                await RisingEdge(dut.clk)
                getattr(dut, f"w_smem_req_valid")[w].value = 0
                dut.w_smem_lane_en[w].value = 0
                for i in range(LANES):
                    dut.smem_rsp_data[i].value = 0x5000 + i
                dut.smem_rsp_valid.value = 1
                await RisingEdge(dut.clk)
                await Timer(1, "ns")
                sb.check_equal(int(dut.w_smem_rsp_valid[w].value), 1,
                               f"warp{w} 应收到响应")
                sb.check_int(int(dut.w_smem_rsp_data[w][0].value), 0x5000,
                             f"warp{w} 的 lane0 数据")
                dut.smem_rsp_valid.value = 0
                break
        await FallingEdge(dut.clk)

    sb.check_equal(order, [0, 2], f"仲裁顺序应为 [0, 2], 实际 {order}")
    sb.report()


@cocotb.test()
async def test_gmem_store_goes_to_wcb(dut):
    await setup(dut)
    sb = Scoreboard("lsu_scheduler")
    dut.w_gmem_req_valid[1].value = 1
    dut.w_gmem_req_we[1].value = 1
    dut.w_gmem_req_line[1].value = 0x400
    dut.w_gmem_req_wen[1].value = 0xFF
    for i in range(LANES):
        dut.w_gmem_req_wdata[1][i].value = 0xD000 + i

    for _ in range(50):
        await Timer(1, "ns")
        if int(dut.w_gmem_req_ready[1].value):
            await RisingEdge(dut.clk)
            dut.w_gmem_req_valid[1].value = 0
            break
        await FallingEdge(dut.clk)
    # 写不占读通道
    sb.check_equal(int(dut.gmem_rd_valid.value), 0, "store 不应发读请求")
    sb.check_int(int(dut.gmem_wr_line.value), 0x400, "WCB 行号")
    sb.check_int(int(dut.gmem_wr_wen.value), 0xFF, "WCB 字掩码")

    got = 0
    for _ in range(10):
        await Timer(1, "ns")
        if int(dut.w_gmem_rsp_valid[1].value):
            got = 1
            break
        await FallingEdge(dut.clk)
    sb.check_equal(got, 1, "store 应马上回完成")
    sb.report()


@cocotb.test()
async def test_gmem_load_routes_back(dut):
    await setup(dut)
    sb = Scoreboard("lsu_scheduler")
    dut.w_gmem_req_valid[3].value = 1
    dut.w_gmem_req_line[3].value = 0x500
    dut.w_gmem_req_wen[3].value = 0xFF

    for _ in range(50):
        await Timer(1, "ns")
        if int(dut.w_gmem_req_ready[3].value):
            await RisingEdge(dut.clk)
            dut.w_gmem_req_valid[3].value = 0
            break
        await FallingEdge(dut.clk)

    sb.check_equal(int(dut.gmem_rd_valid.value), 1, "load 应发读请求")
    sb.check_int(int(dut.gmem_rd_line.value), 0x500, "读行号")
    for i in range(LANES):
        dut.gmem_rsp_data[i].value = 0x7000 + i
    dut.gmem_rsp_valid.value = 1
    await RisingEdge(dut.clk)
    await Timer(1, "ns")
    sb.check_equal(int(dut.w_gmem_rsp_valid[3].value), 1, "warp3 应收到行数据")
    sb.check_int(int(dut.w_gmem_rsp_data[3][7].value), 0x7007, "lane7 数据")
    sb.report()


@cocotb.test()
async def test_flush_owner(dut):
    await setup(dut)
    sb = Scoreboard("lsu_scheduler")
    dut.w_flush_req[2].value = 1
    await RisingEdge(dut.clk)
    dut.w_flush_req[2].value = 0
    await Timer(1, "ns")
    sb.check_equal(int(dut.flush_req.value), 1, "应发出冲刷请求")
    # flush_done 是当拍脉冲: 在同一拍内判完再放时钟沿
    await FallingEdge(dut.clk)
    await Timer(1, "ns")
    dut.flush_done.value = 1
    await Timer(1, "ns")
    sb.check_equal(int(dut.w_flush_done[2].value), 1, "warp2 应收到 flush done")
    await RisingEdge(dut.clk)
    await Timer(1, "ns")
    sb.check_equal(int(dut.flush_req.value), 0, "冲刷结束后应撤销请求")
    sb.report()
