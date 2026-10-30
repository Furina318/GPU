"""core(SM 顶层)的端到端用例: 把内核镜像装进指令 SRAM, LAUNCH, 观察全局内存写入。"""

import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, RisingEdge, Timer

from harness.lib.tb import Scoreboard

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "sw"))
from assembly import assemble  # noqa: E402

LINE_WORDS = 8
WARPS = 4
LANES = 8


def fresh(dut):
    cocotb.start_soon(Clock(dut.clk, 10, "ns").start())
    dut.rst.value = 1
    dut.launch.value = 0
    dut.launch_pc.value = 0
    dut.launch_warps.value = 1
    dut.launch_flags.value = 0
    dut.sram_load_en.value = 0
    dut.sram_load_addr.value = 0
    dut.sram_load_data.value = 0
    dut.sram_load_last.value = 0
    dut.gmem_rd_ready.value = 1
    dut.gmem_rsp_valid.value = 0
    dut.gmem_rsp_line.value = 0
    for w in range(LANES):
        dut.gmem_rsp_data[w].value = 0
    dut.gmem_rsp_err.value = 0
    dut.gmem_wr_ready.value = 1
    dut.flush_done.value = 0


async def tick(dut, n=1):
    for _ in range(n):
        await RisingEdge(dut.clk)
        await FallingEdge(dut.clk)


async def load_image(dut, img: bytes):
    words = [int.from_bytes(img[i:i + 4], "little") for i in range(0, len(img), 4)]
    for i, w in enumerate(words):
        dut.sram_load_en.value = 1
        dut.sram_load_addr.value = i
        dut.sram_load_data.value = w
        dut.sram_load_last.value = 1 if i == len(words) - 1 else 0
        await RisingEdge(dut.clk)
        await FallingEdge(dut.clk)
        if int(dut.sram_load_done.value):
            break
    dut.sram_load_en.value = 0
    dut.sram_load_last.value = 0


class GMem:
    """简易全局内存: 每 32B 一行, 读回按行, 写按 (line, wen) 落盘。"""

    def __init__(self):
        self.mem = {}
        self.reqs = []
        self.writes = []

    def poke(self, addr, val):
        self.mem[addr & 0xFFFF_FFFF] = val & 0xFFFF_FFFF

    def peek(self, addr):
        return self.mem.get(addr & 0xFFFF_FFFF, 0)

    async def serve(self, dut, max_cycles=20000):
        # 读延迟: 请求被接受的下一拍才回数据(与 global_mem/axi_ram_model 的时序一致;
        # 若在握手当拍就举 rsp_valid, 调度器的 gm_busy 还没置起, 响应会被丢掉)。
        pend = None
        hold = 0
        fl_cnt = None
        for _ in range(max_cycles):
            await FallingEdge(dut.clk)
            # --- 冲刷(WCB 排空): 2 拍后回 done ---
            if fl_cnt is None:
                dut.flush_done.value = 0
                if int(dut.flush_req.value):
                    fl_cnt = 2
            else:
                fl_cnt -= 1
                if fl_cnt == 0:
                    dut.flush_done.value = 1
                    fl_cnt = None
            # --- 先按状态驱动本拍输出 ---
            if pend is not None:
                line = pend
                base = line << 5
                for w in range(LINE_WORDS):
                    dut.gmem_rsp_data[w].value = self.peek(base + 4 * w)
                dut.gmem_rsp_line.value = line
                dut.gmem_rsp_valid.value = 1
                dut.gmem_rsp_err.value = 0
                hold = 1
                pend = None
            elif hold > 0:
                hold -= 1
                if hold == 0:
                    dut.gmem_rsp_valid.value = 0
            # --- 再用本拍主口信号更新状态 ---
            if int(dut.gmem_rd_valid.value) and int(dut.gmem_rd_ready.value):
                line = int(dut.gmem_rd_line.value)
                self.reqs.append(line)
                pend = line
            # 写: 收下即落盘
            if int(dut.gmem_wr_valid.value) and int(dut.gmem_wr_ready.value):
                line = int(dut.gmem_wr_line.value)
                wen = int(dut.gmem_wr_wen.value)
                for w in range(LINE_WORDS):
                    if (wen >> w) & 1:
                        self.mem[(line << 5) + 4 * w] = int(dut.gmem_wr_wdata[w].value)
                self.writes.append((line, wen))

            if int(dut.sm_done.value):
                dut.gmem_rsp_valid.value = 0
                return
        dummy = 0


async def run_kernel(dut, src, num_warps=1, max_cycles=20000, gmem=None):
    img = assemble(src).image()
    entry = int.from_bytes(img[4:8], "little")          # 头 word1 = 入口偏移
    await load_image(dut, img)
    await tick(dut, 2)
    server = cocotb.start_soon((gmem or GMem()).serve(dut, max_cycles))
    dut.launch_warps.value = num_warps
    dut.launch_pc.value = entry
    dut.launch.value = 1
    await RisingEdge(dut.clk)
    await FallingEdge(dut.clk)
    dut.launch.value = 0
    for _ in range(max_cycles):
        await RisingEdge(dut.clk)
        await FallingEdge(dut.clk)
        if int(dut.sm_done.value) or int(dut.sm_error.value) != 0:
            break
    server.kill()
    dut.gmem_rsp_valid.value = 0
    await Timer(1, "ns")
    return int(dut.sm_done.value), int(dut.sm_error.value), int(dut.sm_fault_pc.value)


@cocotb.test()
async def test_store_constant(dut):
    """一条 store: 8 lane 同址 ⇒ 只写一个字。"""
    sb = Scoreboard("core-store")
    fresh(dut)
    await tick(dut, 3)
    dut.rst.value = 0
    g = GMem()
    done, err, pc = await run_kernel(dut, """
        lui   r10, 0x10000
        addi  r12, r0, 42
        st.g  0(r10), r12
        exit
    """, gmem=g)
    sb.check_int(err, 0, "sm_error")
    sb.check_int(done, 1, "sm_done")
    sb.check_int(g.peek(0x1000_0000), 42, "写回的值")
    sb.report()


@cocotb.test()
async def test_load_add_store(dut):
    """读常量 → 加 → 写回: 覆盖 ld.g / 行读 / ALU。"""
    sb = Scoreboard("core-ldst")
    fresh(dut)
    await tick(dut, 3)
    dut.rst.value = 0
    g = GMem()
    g.poke(0x1000_0000, 5)
    g.poke(0x1000_0004, 7)
    done, err, pc = await run_kernel(dut, """
        lui   r10, 0x10000
        ld.g  r11, 0(r10)
        ld.g  r12, 4(r10)
        add   r13, r11, r12
        st.g  64(r10), r13
        exit
    """, gmem=g)
    sb.check_int(err, 0, "sm_error")
    sb.check_int(done, 1, "sm_done")
    sb.check_int(g.peek(0x1000_0040), 12, "和")
    sb.report()


@cocotb.test()
async def test_four_warps_tid(dut):
    """4 个 warp 各自用 tid 写不同地址, 验证 launch_warps 与 wmov/tmov。"""
    sb = Scoreboard("core-warp")
    fresh(dut)
    await tick(dut, 3)
    dut.rst.value = 0
    g = GMem()
    done, err, pc = await run_kernel(dut, """
        lui   r10, 0x10000
        tmov  r11, tid
        wmov  r12, wid
        slli  r12, r12, 3
        add   r11, r11, r12
        slli  r11, r11, 2
        add   r10, r10, r11
        st.g  0(r10), r11
        exit
    """, num_warps=4, gmem=g)
    sb.check_int(err, 0, "sm_error")
    sb.check_int(done, 1, "sm_done")
    for i in range(32):
        sb.check_int(g.peek(0x1000_0000 + 4 * i), 4 * i, f"lane{i} 写的值")
    sb.report()


@cocotb.test()
async def test_shared_and_barrier(dut):
    """2 个 warp 走共享内存 + bar.sync: 每个 lane 读邻居 lane 的值写回全局。

    覆盖: st.s / ld.s / 共享内存 bank 广播 / bar.sync 的"等所有 warp 到齐 +
    冲刷 WCB"路径。
    """
    sb = Scoreboard("core-shared")
    fresh(dut)
    await tick(dut, 3)
    dut.rst.value = 0
    g = GMem()
    done, err, pc = await run_kernel(dut, """
        tmov  r11, tid           # idx = tid
        wmov  r12, wid           #      + wid*8
        slli  r12, r12, 3
        add   r11, r11, r12
        slli  r13, r11, 2        # addr = idx*4
        st.s  0(r13), r11
        bar.sync
        addi  r14, r11, 1
        andi  r14, r14, 15       # 邻居 lane(环形)
        slli  r15, r14, 2
        ld.s  r16, 0(r15)
        lui   r17, 0x10000
        slli  r18, r11, 2
        add   r17, r17, r18
        st.g  0(r17), r16
        exit
    """, num_warps=2, gmem=g, max_cycles=4000)
    sb.check_int(err, 0, "sm_error")
    sb.check_int(done, 1, "sm_done")
    for i in range(16):
        sb.check_int(g.peek(0x1000_0000 + 4 * i), (i + 1) & 15, f"lane{i} 读到的邻居")
    sb.report()

@cocotb.test()
async def test_predicated_write(dut):
    """谓词写回: setp.ltu 之后 @p2 mov 只让谓词为真的 lane 改寄存器。

    回归 core 曾经没有实例化 wbu —— 每 lane 的写使能只看 SIMT 活跃掩码, 不看指令谓词,
    于是 @p2 mov 会把 p2 为假的 lane 也写掉(anim 的菱形块因此铺满整幅画面)。"""
    sb = Scoreboard("core-pred-write")
    fresh(dut)
    await tick(dut, 3)
    dut.rst.value = 0
    src = """
        lui   r9, 0x10000
        tmov  r1, tid
        addi  r2, r0, 4
        setp.ltu p2, r1, r2
        addi  r3, r0, 0
        addi  r4, r0, 1
        @p2   mov r3, r4
        slli  r5, r1, 2
        add   r5, r5, r9
        st.g  0(r5), r3
        exit
    """
    g = GMem()
    server = cocotb.start_soon(g.serve(dut, 40000))
    done, err, pc = await run_kernel(dut, src, num_warps=1, max_cycles=40000, gmem=g)
    server.kill()
    dut.gmem_rsp_valid.value = 0
    sb.check_int(err, 0, "sm_error")
    for i in range(8):
        sb.check_int(g.peek(0x1000_0000 + 4 * i), 1 if i < 4 else 0, f"lane{i} 谓词写回")
    sb.report()


@cocotb.test()
async def test_divu(dut):
    """divu 商与除零(全 1)是 clear.S 的循环次数来源。"""
    sb = Scoreboard("core-divu")
    fresh(dut)
    await tick(dut, 3)
    dut.rst.value = 0
    gmem = GMem()
    src = """
        lui   r10, 0x10000
        addi  r1, r0, 95
        addi  r2, r0, 32
        divu  r3, r1, r2
        st.g  0(r10), r3
        addi  r4, r0, 7
        addi  r5, r0, 0
        divu  r6, r4, r5
        st.g  4(r10), r6
        addi  r7, r0, 6
        addi  r8, r0, 7
        mul   r9, r7, r8
        st.g  8(r10), r9
        exit
    """
    done, err, _pc = await run_kernel(dut, src, gmem=gmem)
    sb.check_int(done, 1, "sm_done")
    sb.check_int(err, 0, "sm_error")
    sb.check_int(gmem.peek(0x1000_0000), 95 // 32, "95/32")
    sb.check_int(gmem.peek(0x1000_0004), 0xFFFF_FFFF, "x/0 全 1")
    sb.check_int(gmem.peek(0x1000_0008), 42, "mul 6*7")
    sb.report()




def _fbits(x: float) -> int:
    import struct

    return struct.unpack("<I", struct.pack("<f", x))[0]


def _as_float(w: int) -> float:
    import struct

    return struct.unpack("<f", struct.pack("<I", w & 0xFFFF_FFFF))[0]


@cocotb.test()
async def test_fp_alu(dut):
    """fmul / fadd / fma.f32 走 SM 级共享 FPU(逐 lane 串行发起)。"""
    sb = Scoreboard("core-fp")
    fresh(dut)
    await tick(dut, 3)
    dut.rst.value = 0
    gmem = GMem()
    base = 0x1000_0000
    gmem.poke(base + 0x00, _fbits(1.5))
    gmem.poke(base + 0x04, _fbits(2.0))
    gmem.poke(base + 0x08, _fbits(0.25))
    src = """
        lui   r10, 0x10000
        ld.g  r1, 0(r10)
        ld.g  r2, 4(r10)
        ld.g  r3, 8(r10)
        fmul  r4, r1, r2
        st.g  0x40(r10), r4
        fadd  r5, r1, r2
        st.g  0x44(r10), r5
        fma.f32 r6, r1, r2, r3
        st.g  0x48(r10), r6
        imad  r7, r1, r2, r3
        st.g  0x4c(r10), r7
        exit
    """
    done, err, _pc = await run_kernel(dut, src, gmem=gmem)
    sb.check_int(done, 1, "sm_done")
    sb.check_int(err, 0, "sm_error")
    sb.check_int(gmem.peek(base + 0x40), _fbits(3.0), "fmul 1.5*2.0")
    sb.check_int(gmem.peek(base + 0x44), _fbits(3.5), "fadd 1.5+2.0")
    sb.check_int(gmem.peek(base + 0x48), _fbits(3.25), "fma 1.5*2.0+0.25")
    # imad 是整数乘加: 1.5 的位模式 0x3FC00000 当整数用
    a = 0x3FC00000
    b = 0x40000000
    c = 0x3E800000
    sb.check_int(gmem.peek(base + 0x4C), (a * b + c) & 0xFFFF_FFFF, "imad 整数乘加")
    sb.report()


@cocotb.test()
async def test_fp_per_lane(dut):
    """每 lane 用不同输入做 FP:验证 SM 级共享 FPU 的逐 lane 串行发起与结果回填。"""
    sb = Scoreboard("core-fp-lane")
    fresh(dut)
    await tick(dut, 3)
    dut.rst.value = 0
    gmem = GMem()
    base = 0x1000_0000
    src = """
        lui   r10, 0x10000
        tmov  r1, tid
        slli  r11, r1, 2
        add   r12, r10, r11
        i2f   r2, r1
        addi  r3, r1, 1
        i2f   r3, r3
        fmul  r4, r2, r3
        st.g  0x40(r12), r4
        fmul  r5, r2, r2
        st.g  0x80(r12), r5
        fma.f32 r6, r2, r2, r3
        st.g  0xC0(r12), r6
        exit
    """
    done, err, _pc = await run_kernel(dut, src, gmem=gmem)
    sb.check_int(done, 1, "sm_done")
    sb.check_int(err, 0, "sm_error")
    for lane in range(8):
        t = float(lane)
        sb.check_int(gmem.peek(base + 0x40 + 4 * lane), _fbits(t * (t + 1)), f"x*y lane{lane}")
        sb.check_int(gmem.peek(base + 0x80 + 4 * lane), _fbits(t * t), f"x*x lane{lane}")
        sb.check_int(gmem.peek(base + 0xC0 + 4 * lane), _fbits(t * t + (t + 1)), f"fma lane{lane}")
    sb.report()


@cocotb.test()
async def test_strided_load_across_lines(dut):
    """8 lane 以 24B 步长读一列(gouraud 顶点布局): 跨 6 条 32B 行, 每行 lane 数不等。"""
    sb = Scoreboard("core-stride24")
    fresh(dut)
    await tick(dut, 3)
    dut.rst.value = 0
    gmem = GMem()
    base = 0x1000_0000
    for i in range(8):
        gmem.poke(base + i * 24, 0x100 + i)
        gmem.poke(base + i * 24 + 4, 0x200 + i)
    src = """
        lui   r10, 0x10000
        lui   r18, 0x10001
        tmov  r14, tid
        addi  r13, r0, 24
        imad  r16, r14, r13, r10
        slli  r11, r14, 2
        add   r12, r18, r11
        ld.g  r30, 0(r16)
        st.g  0(r12), r30
        ld.g  r31, 4(r16)
        st.g  0x20(r12), r31
        exit
    """
    done, err, _pc = await run_kernel(dut, src, gmem=gmem)
    sb.check_int(done, 1, "sm_done")
    sb.check_int(err, 0, "sm_error")
    for lane in range(8):
        sb.check_int(gmem.peek(base + 0x1000 + 4 * lane), 0x100 + lane, f"x lane{lane}")
    for lane in range(8):
        sb.check_int(gmem.peek(base + 0x1020 + 4 * lane), 0x200 + lane, f"y lane{lane}")
    sb.report()


@cocotb.test()
async def test_kernel_gouraud(dut):
    """直接跑 ``sw/kernels/gouraud.S``(单 warp, N=8): 仿射变换 + 颜色透传逐位校验。

    这个内核把 24B 步长的顶点按 6×f32 读入再写回 ⇒ 一次访存会拆成 6 条行请求,
    不同行里的 lane 还会撞同一个行内字号(回归 ``lsu.sv`` 的 pack_wdata 必须按行过滤)。
    """
    sb = Scoreboard("core-gouraud")
    fresh(dut)
    await tick(dut, 3)
    dut.rst.value = 0
    gmem = GMem()
    gmem.poke(0x0200_0000, 0x1000_0000)          # [0]  in_base
    gmem.poke(0x0200_0004, 0x1000_1000)          # [4]  out_base
    gmem.poke(0x0200_0008, 8)                    # [8]  N
    gmem.poke(0x0200_000c, _fbits(1.5))          # [12] m00
    gmem.poke(0x0200_0010, _fbits(0.25))         # [16] m01
    gmem.poke(0x0200_0014, _fbits(-0.5))         # [20] m10
    gmem.poke(0x0200_0018, _fbits(2.0))          # [24] m11
    gmem.poke(0x0200_001c, _fbits(3.0))          # [28] tx
    gmem.poke(0x0200_0020, _fbits(-1.0))         # [32] ty
    gmem.poke(0x0200_0024, 24)                   # [36] stride_bytes
    for i in range(8):
        gmem.poke(0x1000_0000 + i * 24, _fbits(0.5 * i))
        gmem.poke(0x1000_0000 + i * 24 + 4, _fbits(0.25 * i))
        for c in range(4):
            gmem.poke(0x1000_0000 + i * 24 + 8 + 4 * c, _fbits(float(i + c)))
    src = (ROOT / "sw/kernels/gouraud.S").read_text(encoding="utf-8")
    done, err, _pc = await run_kernel(dut, src, gmem=gmem)
    sb.check_int(done, 1, "sm_done")
    sb.check_int(err, 0, "sm_error")
    for i in range(8):
        # 这些取值在 f32 与 f64 下都精确 ⇒ 直接用 double 算再取位模式即可
        x, y = 0.5 * i, 0.25 * i
        ex = 1.5 * x + 0.25 * y + 3.0
        ey = 2.0 * y - 0.5 * x - 1.0
        sb.check_int(gmem.peek(0x1000_1000 + i * 24), _fbits(ex), f"v{i}.x")
        sb.check_int(gmem.peek(0x1000_1000 + i * 24 + 4), _fbits(ey), f"v{i}.y")
        for c in range(4):
            sb.check_int(gmem.peek(0x1000_1000 + i * 24 + 8 + 4 * c), _fbits(float(i + c)),
                         f"v{i}.c{c}")
    sb.report()


@cocotb.test()
async def test_pred_p3_loop(dut):
    """p3 上的循环条件: setp 写 p3 必须真的写进 p3。

    回归: exu 曾把 setp_pdst(one-hot)直接截成 [1:0], p3 的 3'b100 变成 p0,
    于是"写 p3"实际写进了只读的 p0, 循环条件永远读到旧值 —— 循环只跑一轮。
    """
    sb = Scoreboard("core-p3loop")
    fresh(dut)
    await tick(dut, 3)
    dut.rst.value = 0
    g = GMem()
    src = """
        lui   r10, 0x10000
        tmov  r16, tid
        slli  r18, r16, 2
        add   r18, r18, r10
        addi  r21, r0, 0
        addi  r20, r0, 8
loop:
        st.g  0(r18), r21
        addi  r18, r18, 32
        addi  r21, r21, 1
        setp.ne p3, r21, r20
        ipdom done
        @p3   br.simt loop, done
done:
        exit
    """
    done, err, pc = await run_kernel(dut, src, num_warps=1, max_cycles=20000, gmem=g)
    sb.check_int(done, 1, "kernel 正常结束")
    sb.check_int(err, 0, "无错误")
    for i in range(8):
        for lane in range(8):
            sb.check_int(g.peek(0x1000_0000 + i * 32 + lane * 4), i,
                         f"第 {i} 轮第 {lane} lane")
    sb.report()


@cocotb.test()
async def test_relaunch(dut):
    """同一个 SM 连跑两个内核。

    回归:`hw/rtl/core/core.sv` 里每 warp 的状态机在 launch 时不回 `W_IDLE`;
    不修的话上一轮停在 `W_EXIT` 的 warp 永远留在那儿,第二个内核根本不执行。
    """
    sb = Scoreboard("core-relaunch")
    fresh(dut)
    await tick(dut, 3)
    dut.rst.value = 0

    prog_a = """
        lui  r10, 0x10000
        addi r1, r0, 7
        tmov r2, tid
        slli r3, r2, 2
        add  r3, r10, r3
        add  r4, r1, r2
        st.g 0(r3), r4
        exit
    """
    ga = GMem()
    done, err, pc = await run_kernel(dut, prog_a, num_warps=1, gmem=ga)
    sb.check_int(done, 1, "第一个内核应当完成")
    sb.check_int(err, 0, "第一个内核不该报错")
    for lane in range(8):
        sb.check_int(ga.peek(0x1000_0000 + 4 * lane), 7 + lane,
                     f"第一个内核 lane{lane} 写入 {7 + lane}")

    prog_b = """
        lui  r10, 0x10000
        addi r1, r0, 100
        st.g 0x10(r10), r1
        exit
    """
    gb = GMem()
    done, err, pc = await run_kernel(dut, prog_b, num_warps=1, gmem=gb)
    sb.check_int(done, 1, "第二个内核应当完成")
    sb.check_int(err, 0, "第二个内核不该报错")
    sb.check_int(gb.peek(0x1000_0010), 100, "第二个内核真的执行了")

    sb.report()
