"""lsu 单 warp 用例:地址检查、共享/全局分派、行合并与回填。

调度器与内存都用 Python 行为模型顶替(lsu_scheduler 是独立模块, 后面单独测),
这里只钉 lsu 自己的语义: 错误码、请求形态、lane 数据回填。
"""
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, RisingEdge, Timer

from harness.lib.tb import Scoreboard

LANES = 8
WORD = 32

# SM 错误码(CMDS §8.2)
ERR_BAD_ALIGN = 3
ERR_MMIO = 6
ERR_NO_REGION = 7


async def setup(dut):
    cocotb.start_soon(Clock(dut.clk, 10, "ns").start())
    for name in ("mem_en", "mem_we", "mem_shared",
                 "smem_rsp_valid", "gmem_rsp_valid",
                 "gmem_rsp_err", "smem_err_align", "smem_err_oob"):
        getattr(dut, name).value = 0
    # 调度器模型恒 ready(背压由调度器自己管, 这里只测 lsu)
    dut.smem_req_ready.value = 1
    dut.gmem_req_ready.value = 1
    dut.mem_pc.value = 0
    dut.mem_inst.value = 0
    dut.mem_rd.value = 0
    dut.eff_mask.value = 0
    for i in range(LANES):
        dut.lane_addr[i].value = 0
        dut.lane_wdata[i].value = 0
        dut.smem_rsp_data[i].value = 0
        dut.gmem_rsp_data[i].value = 0
    dut.rst.value = 1
    await RisingEdge(dut.clk)
    await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)


class Mem:
    """行为内存: 共享区与全局区共用一个 dict(地址本身不重叠)。"""

    def __init__(self):
        self.mem = {}
        self.gmem_reqs = []       # (line, wen, wdata[8])
        self.smem_reqs = []

    def load_words(self, base, words):
        for i, w in enumerate(words):
            self.mem[base + 4 * i] = w & 0xFFFF_FFFF

    def word(self, addr):
        return self.mem.get(addr & ~3, 0)


async def model(dut, mem):
    """AXI/tie 模型: 收到请求的下一拍给 rsp_valid。"""
    pend = None
    hold = 0
    while True:
        await FallingEdge(dut.clk)
        # rsp_valid 要保持到 DUT 真的采到为止(发起请求那一拍 DUT 还在 REQ 状态)
        if pend is not None:
            hold -= 1
            if hold == 0:
                if pend == "g":
                    dut.gmem_rsp_valid.value = 0
                else:
                    dut.smem_rsp_valid.value = 0
                pend = None

        if int(dut.gmem_req_valid.value) and int(dut.gmem_req_ready.value):
            line = int(dut.gmem_req_line.value)
            wen = int(dut.gmem_req_wen.value)
            wdata = [int(dut.gmem_req_wdata[w].value) for w in range(LANES)]
            mem.gmem_reqs.append((line, wen, wdata))
            if int(dut.gmem_req_we.value):
                for w in range(LANES):
                    if (wen >> w) & 1:
                        mem.mem[((line << 5) + 4 * w) & 0xFFFF_FFFF] = wdata[w]
            for w in range(LANES):
                dut.gmem_rsp_data[w].value = mem.word((line << 5) + 4 * w)
            dut.gmem_rsp_valid.value = 1
            pend = "g"
            hold = 2
        elif int(dut.smem_req_valid.value) and int(dut.smem_req_ready.value):
            we = int(dut.smem_req_we.value)
            lanes = int(dut.smem_lane_en.value)
            rec = []
            for i in range(LANES):
                if (lanes >> i) & 1:
                    a = int(dut.smem_lane_addr[i].value)
                    rec.append((i, a, int(dut.smem_lane_wdata[i].value)))
                    if we:
                        mem.mem[a & ~3] = int(dut.smem_lane_wdata[i].value)
            mem.smem_reqs.append((we, rec))
            for i in range(LANES):
                a = int(dut.smem_lane_addr[i].value)
                dut.smem_rsp_data[i].value = mem.word(a)
            dut.smem_rsp_valid.value = 1
            pend = "s"
            hold = 2

        await Timer(1, "ns")


async def issue(dut, mask, addrs, wdata=None, we=0, shared=0, rd=1):
    dut.eff_mask.value = mask
    for i in range(LANES):
        dut.lane_addr[i].value = addrs[i]
        dut.lane_wdata[i].value = (wdata[i] if wdata else 0)
    dut.mem_we.value = we
    dut.mem_shared.value = shared
    dut.mem_rd.value = rd
    dut.mem_pc.value = 0x1234
    dut.mem_inst.value = 0xDEAD_BEEF
    dut.mem_en.value = 1
    # 握手判定在时钟沿之前
    while True:
        await Timer(1, "ns")
        if int(dut.mem_ready.value):
            await RisingEdge(dut.clk)
            dut.mem_en.value = 0
            break
        await FallingEdge(dut.clk)

    for _ in range(200):
        await Timer(1, "ns")
        if int(dut.mem_done.value):
            return {
                "rd": int(dut.mem_done_rd.value),
                "addr": int(dut.mem_done_addr.value),
                "pc": int(dut.mem_done_pc.value),
                "inst": int(dut.mem_done_inst.value),
                "we": int(dut.mem_done_we.value),
                "rdata": [int(dut.mem_done_rdata[i].value) for i in range(LANES)],
                "err": int(dut.mem_err.value),
                "code": int(dut.mem_err_code.value),
            }
        await FallingEdge(dut.clk)
    raise AssertionError("lsu 完成超时")


@cocotb.test()
async def test_shared_load(dut):
    await setup(dut)
    mem = Mem()
    mem.load_words(0x100, [0x1000 + i for i in range(LANES)])
    cocotb.start_soon(model(dut, mem))

    addrs = [0x100 + 4 * i for i in range(LANES)]
    r = await issue(dut, 0xFF, addrs, shared=1, rd=3)
    sb = Scoreboard("lsu")
    sb.check_equal(r["rd"], 1, "load 的 done_rd")
    sb.check_int(r["addr"], 3, "rd 地址")
    sb.check_int(r["we"], 0xFF, "8 lane 都应有效")
    for i in range(LANES):
        sb.check_int(r["rdata"][i], 0x1000 + i, f"lane{i}")
    sb.check_equal(r["err"], 0, "不应报错")
    sb.check_int(len(mem.smem_reqs), 1, "共享请求次数")
    sb.report()


@cocotb.test()
async def test_shared_store(dut):
    await setup(dut)
    mem = Mem()
    cocotb.start_soon(model(dut, mem))
    addrs = [0x200 + 4 * i for i in range(LANES)]
    wdata = [0xAA00 + i for i in range(LANES)]
    r = await issue(dut, 0xFF, addrs, wdata=wdata, we=1, shared=1, rd=4)
    sb = Scoreboard("lsu")
    sb.check_int(r["we"], 0xFF, "store 的每 lane 写入")
    sb.check_equal(r["err"], 0, "不应报错")
    for i in range(LANES):
        sb.check_int(mem.word(0x200 + 4 * i), 0xAA00 + i, f"mem[{i}]")
    sb.report()


@cocotb.test()
async def test_global_load_two_lines(dut):
    await setup(dut)
    mem = Mem()
    base = 0x8000_0000          # DDR 区, mem_map 认作可读写
    mem.load_words(base, [0x5000 + i for i in range(2 * LANES)])
    cocotb.start_soon(model(dut, mem))
    # 前 4 lane 在 0x4000 行, 后 4 lane 在 0x4020 行
    addrs = [base + 4 * i for i in range(4)] + [base + 0x20 + 4 * i for i in range(4)]
    r = await issue(dut, 0xFF, addrs, shared=0, rd=5)
    sb = Scoreboard("lsu")
    sb.check_int(len(mem.gmem_reqs), 2, "行请求数")
    sb.check_int(mem.gmem_reqs[0][0], (base >> 5) & 0x7FF_FFFF, "第一个请求行号")
    sb.check_int(mem.gmem_reqs[1][0], ((base + 0x20) >> 5) & 0x7FF_FFFF, "第二个请求行号")
    word_of_lane = [0, 1, 2, 3, 8, 9, 10, 11]     # 后 4 lane 落在下一行的前 4 字
    for i in range(LANES):
        sb.check_int(r["rdata"][i], 0x5000 + word_of_lane[i], f"lane{i}")
    sb.check_int(r["we"], 0xFF, "8 lane 都应有效")
    sb.report()


@cocotb.test()
async def test_global_store_maps_word_index(dut):
    await setup(dut)
    mem = Mem()
    cocotb.start_soon(model(dut, mem))
    # lane 顺序打乱: lane0 落行内第 3 字, lane1 落在第 0 字
    base = 0x8000_1000
    word_of_lane = [3, 0, 1, 2, 4, 5, 6, 7]      # 故意打乱, 钉住"按行内字号落盘"
    addrs = [base + 4 * word_of_lane[i] for i in range(LANES)]
    wdata = [0xBB00 + i for i in range(LANES)]
    r = await issue(dut, 0xFF, addrs, wdata=wdata, we=1, shared=0, rd=0)
    sb = Scoreboard("lsu")
    sb.check_int(len(mem.gmem_reqs), 1, "同一行请求数")
    line, wen, wd = mem.gmem_reqs[0]
    sb.check_int(line, (base >> 5) & 0x7FF_FFFF, "行号")
    sb.check_int(wen, 0xFF, "行内写入的字")
    for i in range(LANES):
        word = word_of_lane[i]
        sb.check_int(mem.mem.get(base + 4 * word, -1), 0xBB00 + i,
                 f"lane{i} → 行内第 {word} 字")
    sb.check_equal(r["err"], 0, "不应报错")
    sb.report()


@cocotb.test()
async def test_bad_align(dut):
    await setup(dut)
    mem = Mem()
    cocotb.start_soon(model(dut, mem))
    addrs = [0x8000 + 4 * i for i in range(LANES)]
    addrs[2] = 0x8008 + 1          # bit0 = 1
    r = await issue(dut, 0xFF, addrs, shared=1, rd=6)
    sb = Scoreboard("lsu")
    sb.check_equal(r["err"], 1, "未对齐应报错")
    sb.check_int(r["code"], ERR_BAD_ALIGN, "错误码")
    sb.check_int(len(mem.smem_reqs) + len(mem.gmem_reqs), 0, "出错时不应发请求")
    sb.report()


@cocotb.test()
async def test_mmio_and_unmapped(dut):
    await setup(dut)
    mem = Mem()
    cocotb.start_soon(model(dut, mem))
    addrs = [0x8000 + 4 * i for i in range(LANES)]
    addrs[0] = 0x04                     # MMIO 窗口
    r = await issue(dut, 0xFF, addrs, shared=0, rd=7)
    sb = Scoreboard("lsu")
    sb.check_int(r["code"], ERR_MMIO, "MMIO 错误码")

    addrs[0] = 0x4000_0000              # 未映射空洞
    r = await issue(dut, 0xFF, addrs, shared=0, rd=7)
    sb.check_int(r["code"], ERR_NO_REGION, "未映射错误码")
    sb.report()
