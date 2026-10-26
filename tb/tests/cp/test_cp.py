"""CP(hw/rtl/cp)的 cocotb 测试:CMDS.md §2/§4/§6/§7/§8/§9/§10 的行为验收。

环境在 :mod:`tests.cp.cp_env`(内存模型 + AXI4-Lite 主机 + SM/引擎/指令 SRAM 桩)。
覆盖顺序与 `hw/rtl/cp/cp.md` §16 的 CP-1..CP-4 一致:寄存器 → 命令流 → 依赖/完成/超时
→ 镜像加载与引擎。
"""

from __future__ import annotations

import struct
import zlib

import cocotb
from cocotb.triggers import FallingEdge, RisingEdge

from harness.lib.tb import Scoreboard, reset_dut
from tests.core.dut_drivers import start_clock_once
from tests.cp import cp_env as E


# ─────────────────────────── 环境装配 ───────────────────────────
class CpTb:
    def __init__(self, dut):
        self.dut = dut
        self.mem = E.MemModel(dut)
        self.sm = E.SmMock(dut)
        self.eng = E.EngMock(dut)
        self.sram = E.SramModel(dut)
        self.q = E.QueueHost(dut, self.mem)

    async def bring_up(self, *, timeout_cycles: int = 4096, sm_run: int = 6):
        d = self.dut
        await start_clock_once(d)
        # 输入先归零, 免得复位期间有悬空请求
        for name, val in (("s_awvalid", 0), ("s_wvalid", 0), ("s_bready", 1),
                          ("s_arvalid", 0), ("s_rready", 1),
                          ("mem_rd_ack", 0), ("mem_rd_rsp", 0), ("mem_rd_data", 0),
                          ("mem_rd_err", 0), ("mem_wr_ack", 0), ("mem_wr_rsp", 0),
                          ("mem_wr_err", 0), ("sm_done", 0), ("sm0_error", 0),
                          ("sm1_error", 0), ("sm0_fault_pc", 0), ("sm1_fault_pc", 0),
                          ("sram_load_done", 0), ("sram_err_ovf", 0),
                          ("eng_ready", 1), ("gemm_status", 0), ("gemm_error", 0),
                          ("twod_status", 0), ("twod_error", 0)):
            getattr(d, name).value = val
        await reset_dut(d)
        self.sm.run_cycles = sm_run
        self.mem.start()
        self.sm.start()
        self.eng.start()
        self.sram.start()
        await E.axil_write(d, E.MMIO_TIMEOUT_CYCLES, timeout_cycles)
        await E.wait_idle(d)


# ─────────────────────────── CP-1:寄存器 ───────────────────────────
@cocotb.test()
async def test_mmio_registers(dut):
    """CMDS §7 的寄存器表:复位值、只读、写任意值清零、CP_STATUS 三条位。"""
    sb = Scoreboard("mmio")
    tb = CpTb(dut)
    await tb.bring_up()

    sb.check_int(await E.axil_read(dut, E.MMIO_CMD_QUEUE_BASE), E.QUEUE_BASE, "队列基址复位值")
    sb.check_int(await E.axil_read(dut, E.MMIO_CMD_QUEUE_SIZE), E.QUEUE_SIZE, "队列大小复位值")
    sb.check_int(await E.axil_read(dut, E.MMIO_SEM_BASE), E.SEM_BASE, "SEM_BASE 复位值")
    sb.check_int(await E.axil_read(dut, E.MMIO_CONST_BASE), E.CONST_BASE, "CONST_BASE(只读)")
    sb.check_int(await E.axil_read(dut, E.MMIO_DDR_BASE), E.DDR_BASE, "DDR_BASE(只读)")
    sb.check_int(await E.axil_read(dut, E.MMIO_CMD_HEAD), E.QUEUE_BASE, "CMD_HEAD 复位值")
    sb.check_int(await E.axil_read(dut, E.MMIO_SM_KERNEL_MASK), 0, "SM_KERNEL_MASK 复位值")

    idle, busy, err, code = await E.cp_status(dut)
    sb.check_int((idle, busy, err), (1, 0, 0), "CP_STATUS 复位后 = IDLE")
    sb.check_int(code, E.CPERR_NONE, "CP_ERROR 复位值")

    # R/W 配置寄存器
    await E.axil_write(dut, E.MMIO_FB_BASE, 0x1100_0000)
    sb.check_int(await E.axil_read(dut, E.MMIO_FB_BASE), 0x1100_0000, "FB_BASE 可写")
    await E.axil_write(dut, E.MMIO_SEM_BASE, 0x0300_1000)
    sb.check_int(await E.axil_read(dut, E.MMIO_SEM_BASE), 0x0300_1000, "SEM_BASE 可写")
    await E.axil_write(dut, E.MMIO_SEM_BASE, E.SEM_BASE)

    # IRQ_ENABLE / IRQ_STATUS
    await E.axil_write(dut, E.MMIO_IRQ_ENABLE, 0x1F)
    sb.check_int(await E.axil_read(dut, E.MMIO_IRQ_ENABLE), 0x1F, "IRQ_ENABLE 可写")
    await E.axil_write(dut, E.MMIO_IRQ_STATUS, 0xFF)
    sb.check_int(await E.axil_read(dut, E.MMIO_IRQ_STATUS), 0, "IRQ_STATUS 写任意值清零")

    # 只读寄存器:写不进去
    await E.axil_write(dut, E.MMIO_CONST_BASE, 0xDEAD_BEEF)
    sb.check_int(await E.axil_read(dut, E.MMIO_CONST_BASE), E.CONST_BASE, "CONST_BASE 只读")
    await E.axil_write(dut, E.MMIO_DDR_BASE, 0x1234_5678)
    sb.check_int(await E.axil_read(dut, E.MMIO_DDR_BASE), E.DDR_BASE, "DDR_BASE 只读")

    # TIMEOUT_CYCLES 写 0 ⇒ 回默认值(0x10_0000)
    await E.axil_write(dut, E.MMIO_TIMEOUT_CYCLES, 0)
    sb.check_int(await E.axil_read(dut, E.MMIO_TIMEOUT_CYCLES), 0x0010_0000, "写 0 回默认门限")
    await E.axil_write(dut, E.MMIO_TIMEOUT_CYCLES, 4096)
    sb.report()


# ─────────────────────────── CP-2:命令流 ───────────────────────────
@cocotb.test()
async def test_nop_walk_and_irq_flag(dut):
    """NOP 链 + `IRQ_ON_DONE`:HEAD 走到 TAIL, 门铃被消费, IRQ_STATUS 置位。"""
    sb = Scoreboard("nop")
    tb = CpTb(dut)
    await tb.bring_up()

    for n in (2, 3, 5):                                  # 变长 NOP
        await tb.q.submit(E.make_cmd(E.CMD_NOP, [], length=n, flags=E.CFLAG_IRQ_ON_DONE))
    await E.axil_write(dut, E.MMIO_IRQ_ENABLE, 0x2)      # 使能 IRQ_CMD_DONE
    await tb.q.run()

    sb.check_int(await E.axil_read(dut, E.MMIO_CMD_HEAD), tb.q.tail, "CMD_HEAD 追平 TAIL")
    sb.check_int((await E.axil_read(dut, E.MMIO_IRQ_STATUS)) & 0x2, 0x2, "IRQ_CMD_DONE 置位")
    sb.check_int(int(dut.irq.value), 1, "irq = IRQ_STATUS & IRQ_ENABLE")
    sb.check_int((await E.axil_read(dut, E.MMIO_CP_ERROR)), E.CPERR_NONE, "没有错误")
    sb.report()


@cocotb.test()
async def test_signal_wait_readreg_irq_fence(dut):
    """SIGNAL / WAIT / READ_REG / IRQ / FENCE 的实际效果(内存与寄存器两侧都查)。"""
    sb = Scoreboard("misc")
    tb = CpTb(dut)
    await tb.bring_up()

    sem = E.SEM_BASE + 3 * 4
    dst = E.DDR_BASE + 0x40

    # SIGNAL [sem] = 7
    await tb.q.submit(E.make_cmd(E.CMD_SIGNAL, [sem, 7]))
    # WAIT [sem] >= 7(已满足)
    await tb.q.submit(E.make_cmd(E.CMD_WAIT, [sem, 7]))
    # READ_REG(SM_KERNEL_MASK) → DDR
    await tb.q.submit(E.make_cmd(E.CMD_READ_REG, [E.MMIO_SM_KERNEL_MASK, dst]))
    # IRQ id=5
    await tb.q.submit(E.make_cmd(E.CMD_IRQ, [5]))
    # FENCE
    await tb.q.submit(E.make_cmd(E.CMD_FENCE, []))
    await tb.q.run()

    sb.check_int(tb.mem.peek(sem), 7, "SIGNAL 写入信号量")
    sb.check_int(tb.mem.peek(dst), 0, "READ_REG 把 SM_KERNEL_MASK(=0)落到内存")
    sb.check_int((await E.axil_read(dut, E.MMIO_IRQ_STATUS)) & (1 << 5), 1 << 5, "IRQ id=5 置位")
    sb.check_int(await E.axil_read(dut, E.MMIO_CP_ERROR), E.CPERR_NONE, "没有错误")
    sb.report()


@cocotb.test()
async def test_params_writes_constant_region(dut):
    """PARAMS:参数块按序写进常量区(CMDS §6.10)。"""
    sb = Scoreboard("params")
    tb = CpTb(dut)
    await tb.bring_up()

    vals = [0x1111_2222, 0x3333_4444, 0x5555_6666]
    await tb.q.submit(E.make_cmd(E.CMD_PARAMS, [0x20, len(vals)] + vals))
    await tb.q.run()
    for i, v in enumerate(vals):
        sb.check_int(tb.mem.peek(E.CONST_BASE + 0x20 + i * 4), v, f"常量区 +0x{0x20+i*4:02x}")
    # 越界(dst_off + N*4 > 4096)⇒ CMD_ERROR
    await tb.q.submit(E.make_cmd(E.CMD_PARAMS, [0x0FFC, 2, 1, 2]))
    code = await tb.q.run_expect_error()
    sb.check_int(code, E.CPERR_CMD, "PARAMS 越界 ⇒ CMD_ERROR")
    sb.report()


# ─────────────────────────── CP-2:错误路径 ───────────────────────────
@cocotb.test()
async def test_queue_boundary_errors(dut):
    """长度 < 2 / 越过 CMD_TAIL / 保留 opcode / cmd_flags 保留位 都必须是 CMD_ERROR。"""
    sb = Scoreboard("queue_err")
    tb = CpTb(dut)
    await tb.bring_up()

    # 长度 < 2
    await tb.q.submit([0x0001_0000 | E.CMD_NOP, 0xFFFF_FFFF])
    sb.check_int(await tb.q.run_expect_error(), E.CPERR_CMD, "长度 < 2 ⇒ CMD_ERROR")
    await E.axil_write(dut, E.MMIO_CP_RESET, 1)
    await E.axil_write(dut, E.MMIO_TIMEOUT_CYCLES, 4096)
    await E.wait_idle(dut)

    # 命令越过 CMD_TAIL(头说 5 字, 但只写进去 2 字)
    await tb.q.submit([(5 << 16) | E.CMD_NOP, 0xFFFF_FFFF])
    sb.check_int(await tb.q.run_expect_error(), E.CPERR_CMD, "越过 TAIL ⇒ CMD_ERROR")
    await E.axil_write(dut, E.MMIO_CP_RESET, 1)
    await E.wait_idle(dut)

    # 保留 opcode(0x0A..0x0F)
    await tb.q.submit(E.make_cmd(0x0A, []))
    sb.check_int(await tb.q.run_expect_error(), E.CPERR_CMD, "保留 opcode ⇒ CMD_ERROR")
    await E.axil_write(dut, E.MMIO_CP_RESET, 1)
    await E.wait_idle(dut)

    # cmd_flags 的保留位
    await tb.q.submit(E.make_cmd(E.CMD_NOP, [], flags=0x04))
    sb.check_int(await tb.q.run_expect_error(), E.CPERR_CMD, "cmd_flags 保留位 ⇒ CMD_ERROR")
    sb.report()


@cocotb.test()
async def test_queue_cfg_rules(dut):
    """CMDS §2.1/§6.2/§7:队列指针类寄存器的取值规则、时机规则与"两条写路径的错误报告方式不同"。

    - 取值:`CMD_QUEUE_SIZE` 必须 ≥ 8 且为 8 的倍数(4 的倍数会让回绕时剩下 4 字节而没有合法 NOP 可填);
      `CMD_QUEUE_BASE` 必须 4B 对齐且在 MMIO 窗口外;`CMD_HEAD` 必须落在队列内且 4B 对齐;
    - 时机:`CMD_QUEUE_BASE/SIZE` 只允许在 CP IDLE 且队列为空时改;
    - 路径:经 `WRITE_REG` 命令 ⇒ `CMD_ERROR` / `ILLEGAL_STATE`;host 直写 MMIO ⇒ 该写被忽略、寄存器保持原值、不置 `CP_ERROR`;
      `CMD_HEAD` 只有 host 能写(命令流写它 ⇒ `CMD_ERROR`);
    - 附带守着"head 被搬回已消费位置后,CP 必须重新从内存取指"(§2.1 的环形复用)。
    """
    sb = Scoreboard("queue_cfg")
    tb = CpTb(dut)
    await tb.bring_up()

    async def reset_cp():
        """CP_RESET(CMDS §7.3):head 置为 tail、清错误、配置寄存器不动 —— **驱动侧指针不用重设**。"""
        await E.axil_write(dut, E.MMIO_CP_RESET, 1)
        await E.axil_write(dut, E.MMIO_TIMEOUT_CYCLES, 4096)
        await E.wait_idle(dut)

    async def wr_reg(addr, value, *, more_after=False):
        """发一条 WRITE_REG;`more_after=True` 时后面再跟一条 NOP,使它不是队列里的**最后**一条命令。"""
        await tb.q.submit(E.make_cmd(E.CMD_WRITE_REG, [addr, value]))
        if more_after:
            await tb.q.submit(E.make_cmd(E.CMD_NOP, []))

    # ── ① WRITE_REG 命令路径:取值非法 ⇒ CMD_ERROR(4100 是 4 的倍数但不是 8 的倍数) ──
    await wr_reg(E.MMIO_CMD_QUEUE_SIZE, 4100)
    sb.check_int(await tb.q.run_expect_error(), E.CPERR_CMD, "WRITE_REG SIZE=4100 ⇒ CMD_ERROR")
    await reset_cp()
    await wr_reg(E.MMIO_CMD_QUEUE_SIZE, 4)
    sb.check_int(await tb.q.run_expect_error(), E.CPERR_CMD, "WRITE_REG SIZE=4(<8)⇒ CMD_ERROR")
    await reset_cp()
    sb.check_int(await E.axil_read(dut, E.MMIO_CMD_QUEUE_SIZE), E.QUEUE_SIZE, "非法值不改寄存器")

    # ── ② WRITE_REG 命令路径:时机不对 ⇒ ILLEGAL_STATE(队列里还有后续命令) ──
    await wr_reg(E.MMIO_CMD_QUEUE_SIZE, 8192, more_after=True)
    sb.check_int(await tb.q.run_expect_error(), E.CPERR_ILLEGAL_STATE,
                 "不是最后一条 ⇒ ILLEGAL_STATE")
    await reset_cp()
    sb.check_int(await E.axil_read(dut, E.MMIO_CMD_QUEUE_SIZE), E.QUEUE_SIZE,
                 "ILLEGAL_STATE 也不改寄存器")

    # ── ③ WRITE_REG 命令路径:合法且是最后一条 ⇒ 生效,并把 head/tail 复位到基址 ──
    await wr_reg(E.MMIO_CMD_QUEUE_SIZE, 8192)
    await tb.q.run()
    sb.check_int(await E.axil_read(dut, E.MMIO_CMD_QUEUE_SIZE), 8192, "合法 WRITE_REG 生效")
    sb.check_int((await E.axil_read(dut, E.MMIO_CMD_HEAD),
                  await E.axil_read(dut, E.MMIO_CMD_TAIL)),
                 (E.QUEUE_BASE, E.QUEUE_BASE), "改 SIZE 后 head/tail 一起回基址")
    tb.q.tail = E.QUEUE_BASE

    # ── ④ host 直写 MMIO:取值非法 ⇒ 被忽略、且不置 CP_ERROR ──
    await E.axil_write(dut, E.MMIO_CMD_QUEUE_SIZE, 4100)
    sb.check_int(await E.axil_read(dut, E.MMIO_CMD_QUEUE_SIZE), 8192, "host 直写非法值被忽略")
    sb.check_int(await E.axil_read(dut, E.MMIO_CP_ERROR), E.CPERR_NONE, "host 直写非法值不报错")
    await E.axil_write(dut, E.MMIO_CMD_QUEUE_SIZE, 0)
    sb.check_int(await E.axil_read(dut, E.MMIO_CMD_QUEUE_SIZE), 8192, "host 直写 0 被忽略")

    # ── ⑤ host 直写 MMIO:合法 ⇒ 生效 ──
    await E.axil_write(dut, E.MMIO_CMD_QUEUE_SIZE, E.QUEUE_SIZE)
    sb.check_int(await E.axil_read(dut, E.MMIO_CMD_QUEUE_SIZE), E.QUEUE_SIZE, "host 直写合法值生效")

    # ── ⑥ host 直写 MMIO:CP 忙时被忽略(只允许 IDLE 且队列为空时改)──
    tb.eng.run_cycles = 800
    await tb.q.submit(E.make_cmd(E.CMD_2D_FILL, [E.FB_BASE, 0xFF00_FF00, 0, (2 << 16) | 2]))
    await tb.q.doorbell()
    await E.axil_write(dut, E.MMIO_CMD_QUEUE_SIZE, 8192)
    sb.check_int(await E.axil_read(dut, E.MMIO_CMD_QUEUE_SIZE), E.QUEUE_SIZE,
                 "CP 忙时 host 直写 SIZE 被忽略")
    await E.axil_write(dut, E.MMIO_CMD_QUEUE_BASE, E.QUEUE_BASE + 0x2000)
    sb.check_int(await E.axil_read(dut, E.MMIO_CMD_QUEUE_BASE), E.QUEUE_BASE,
                 "CP 忙时 host 直写 BASE 被忽略")
    await E.wait_idle(dut)
    tb.eng.run_cycles = 4

    # ── ⑦ host 直写 MMIO:IDLE 时改 BASE 生效,并把 head/tail 一起搬过去 ──
    await E.axil_write(dut, E.MMIO_CMD_QUEUE_BASE, E.QUEUE_BASE + 0x2000)
    sb.check_int(await E.axil_read(dut, E.MMIO_CMD_QUEUE_BASE), E.QUEUE_BASE + 0x2000,
                 "IDLE 时 host 直写 BASE 生效")
    sb.check_int((await E.axil_read(dut, E.MMIO_CMD_HEAD),
                  await E.axil_read(dut, E.MMIO_CMD_TAIL)),
                 (E.QUEUE_BASE + 0x2000, E.QUEUE_BASE + 0x2000), "改 BASE 后 head/tail 一起搬")

    # 搬回原基址, 确认队列还能正常跑(寄存器改了以后驱动必须重新同步 tail,§2.1)
    await E.axil_write(dut, E.MMIO_CMD_QUEUE_BASE, E.QUEUE_BASE)
    await E.axil_write(dut, E.MMIO_CMD_QUEUE_SIZE, E.QUEUE_SIZE)
    tb.q.tail = E.QUEUE_BASE
    dst = E.DDR_BASE + 0x80
    await tb.q.submit(E.make_cmd(E.CMD_READ_REG, [E.MMIO_CMD_QUEUE_SIZE, dst]))
    await tb.q.run()
    sb.check_int(tb.mem.peek(dst), E.QUEUE_SIZE, "搬回后队列照常工作")
    sb.check_int(await E.axil_read(dut, E.MMIO_CP_ERROR), E.CPERR_NONE, "全程没有 CP_ERROR")

    # ── ⑧ CMD_HEAD:只有 host 能写;取值必须落在队列内且 4B 对齐(§7/§6.2) ──
    sem = E.SEM_BASE + 9 * 4
    head_now = await E.axil_read(dut, E.MMIO_CMD_HEAD)          # READ_REG 跑完 = base+0x10
    await E.axil_write(dut, E.MMIO_CMD_HEAD, head_now + 3)      # 未对齐 ⇒ 忽略
    sb.check_int(await E.axil_read(dut, E.MMIO_CMD_HEAD), head_now, "未对齐的 head 写被忽略")
    await E.axil_write(dut, E.MMIO_CMD_HEAD, E.QUEUE_BASE + 0x2000)   # 越界(队列已改回 4096B)
    sb.check_int(await E.axil_read(dut, E.MMIO_CMD_HEAD), head_now, "越界的 head 写被忽略")

    # host 把 head 搬回已消费的位置, 并在那里写一条**不同的**命令:CP 必须读内存里的新字节
    # (取指行缓存只能在一条命令内有效, CMDS §2.1 的环形复用)
    tb.mem.poke(E.QUEUE_BASE, (4 << 16) | E.CMD_SIGNAL)
    tb.mem.poke(E.QUEUE_BASE + 4, 0xFFFF_FFFF)
    tb.mem.poke(E.QUEUE_BASE + 8, sem)
    tb.mem.poke(E.QUEUE_BASE + 12, 0x5A)
    await E.axil_write(dut, E.MMIO_CMD_HEAD, E.QUEUE_BASE)
    sb.check_int(await E.axil_read(dut, E.MMIO_CMD_HEAD), E.QUEUE_BASE, "合法的 head 写生效")
    await tb.q.doorbell()
    await E.wait_idle(dut)
    sb.check_int(tb.mem.peek(sem), 0x5A, "host 搬 head 后, CP 执行的是内存里的新命令(不是缓存里的旧命令)")

    # 命令流不能写 CMD_HEAD(§6.2 的只读寄存器规则)
    await reset_cp()
    await wr_reg(E.MMIO_CMD_HEAD, E.QUEUE_BASE)
    sb.check_int(await tb.q.run_expect_error(), E.CPERR_CMD, "WRITE_REG 写 CMD_HEAD ⇒ CMD_ERROR")
    sb.report()


@cocotb.test()
async def test_wait_timeout_and_wait_id(dut):
    """WAIT 等不到 ⇒ TIMEOUT;带 `wait_id` 的命令会真的等, 条件满足后继续。"""
    sb = Scoreboard("wait")
    tb = CpTb(dut)
    await tb.bring_up(timeout_cycles=200)

    sem = E.SEM_BASE + 5 * 4
    await tb.q.submit(E.make_cmd(E.CMD_WAIT, [sem, 1]))
    sb.check_int(await tb.q.run_expect_error(), E.CPERR_TIMEOUT, "WAIT 等不到 ⇒ TIMEOUT")

    # 复位后:wait_id 不满足 ⇒ 卡住;由测试台把信号量写上去 ⇒ 继续
    await E.axil_write(dut, E.MMIO_CP_RESET, 1)
    await E.axil_write(dut, E.MMIO_TIMEOUT_CYCLES, 4096)
    await E.wait_idle(dut)
    dst = E.DDR_BASE + 0x80
    await tb.q.submit(E.make_cmd(E.CMD_SIGNAL, [dst, 0xA5A5_A5A5], wait_id=7))
    await E.axil_write(dut, E.MMIO_CMD_TAIL, tb.q.tail)
    await tb.q.doorbell()
    for _ in range(60):
        await FallingEdge(dut.clk)
    sb.check_int(tb.mem.peek(dst), 0, "wait_id 未满足 ⇒ 命令还没执行")
    tb.mem.poke(E.SEM_BASE + 7 * 4, 1)                   # 满足依赖
    await E.wait_idle(dut)
    sb.check_int(tb.mem.peek(dst), 0xA5A5_A5A5, "依赖满足后命令执行")
    sb.report()


# ─────────────────────────── CP-4:LOAD_KERNEL + LAUNCH ───────────────────────────
def _load_kernel_cmd(mem: E.MemModel, raw: bytes, sm_mask: int = 3) -> list[int]:
    mem.poke_bytes(E.IMG_BASE, raw)
    return E.make_cmd(E.CMD_LOAD_KERNEL, [E.IMG_BASE, len(raw), sm_mask])


@cocotb.test()
async def test_load_kernel_and_launch(dut):
    """LOAD_KERNEL:镜像头 + CRC 校验通过 ⇒ 写进两个 SM 的指令 SRAM + 置 KERNEL_MASK;
    随后 LAUNCH 跑到 SM_DONE ⇒ 打信号量 + IRQ_LAUNCH_DONE(CMDS §6.4/§4.3)。"""
    sb = Scoreboard("loadk")
    tb = CpTb(dut)
    await tb.bring_up()

    raw = E.assemble_image("nested7.S")
    f = E.image_fields(raw)
    await tb.q.submit(_load_kernel_cmd(tb.mem, raw))
    await tb.q.run()

    sb.check_int(await E.axil_read(dut, E.MMIO_CP_ERROR), E.CPERR_NONE, "LOAD_KERNEL 无错")
    sb.check_int(await E.axil_read(dut, E.MMIO_SM_KERNEL_MASK), 0x3, "两个 SM 都置了 KERNEL_MASK")
    # 指令 SRAM 里应该是整份镜像(与参考模型"整块放在偏移 0"一致)
    words = struct.unpack_from("<%dI" % (f["size"] // 4), raw, 0)
    for i in (0, f["entry"] // 4, len(words) - 1):
        sb.check_int(tb.sram.words[0].get(i, None), words[i], f"SM0 指令 SRAM 第 {i} 字")
        sb.check_int(tb.sram.words[1].get(i, None), words[i], f"SM1 指令 SRAM 第 {i} 字")

    # LAUNCH:flags 必须与镜像头一致(§4.1 第 6 条)
    sem = E.SEM_BASE + 1 * 4
    await tb.q.submit(E.make_cmd(E.CMD_LAUNCH,
                                 [f["entry"], 4, f["flags"]], signal_id=1))
    await tb.q.run()

    sb.check_int(await E.axil_read(dut, E.MMIO_CP_ERROR), E.CPERR_NONE, "LAUNCH 无错")
    sb.check_int(len(tb.sm.launches), 1, "分发了一次 LAUNCH")
    sb.check_int(tb.sm.launches[0][0], f["entry"], "kernel_pc = 镜像 entry")
    sb.check_int(tb.sm.launches[0][1], 4, "num_warps = 4")
    sb.check_int(tb.mem.peek(sem), 1, "完成且无错 ⇒ SEM[1] += 1")
    sb.check_int((await E.axil_read(dut, E.MMIO_IRQ_STATUS)) & 0x4, 0x4, "IRQ_LAUNCH_DONE")
    sb.report()


@cocotb.test()
async def test_load_kernel_crc_and_flag_checks(dut):
    """镜像头/CRC 不符 ⇒ CMD_ERROR;LAUNCH 的 flags 与镜像头不一致也 ⇒ CMD_ERROR。"""
    sb = Scoreboard("loadk_err")
    tb = CpTb(dut)
    await tb.bring_up()

    raw = bytearray(E.assemble_image("nested7.S"))
    struct.pack_into("<I", raw, 16, 0xDEAD_BEEF)          # 改掉 code_crc32
    await tb.q.submit(_load_kernel_cmd(tb.mem, bytes(raw)))
    sb.check_int(await tb.q.run_expect_error(), E.CPERR_CMD, "CRC 不符 ⇒ CMD_ERROR")
    sb.check_int(await E.axil_read(dut, E.MMIO_SM_KERNEL_MASK), 0, "失败的加载不置 KERNEL_MASK")

    # 复位后:CRC 正确但 LAUNCH 的 flags 与镜像头不一致
    await E.axil_write(dut, E.MMIO_CP_RESET, 1)
    await E.axil_write(dut, E.MMIO_TIMEOUT_CYCLES, 4096)
    await E.wait_idle(dut)
    raw = E.assemble_image("nested7.S")
    f = E.image_fields(raw)
    await tb.q.submit(_load_kernel_cmd(tb.mem, raw))
    await tb.q.run()
    sb.check_int(await E.axil_read(dut, E.MMIO_CP_ERROR), E.CPERR_NONE, "正常镜像加载通过")
    bad_flags = f["flags"] ^ 0x1
    await tb.q.submit(E.make_cmd(E.CMD_LAUNCH, [f["entry"], 4, bad_flags]))
    sb.check_int(await tb.q.run_expect_error(), E.CPERR_CMD, "flags 交叉核对失败")
    sb.report()


@cocotb.test()
async def test_launch_reject_and_stale_sm_error(dut):
    """§4.1 的检查顺序:`USES_BARRIER` + 5 warp ⇒ LAUNCH_REJECT;SM 有未清错误 ⇒
    STALE_SM_ERROR(SM 错误优先于 warp 数)。"""
    sb = Scoreboard("launch_pre")
    tb = CpTb(dut)
    await tb.bring_up()

    raw = bytearray(E.assemble_image("nested7.S"))
    f = E.image_fields(bytes(raw))
    flags = f["flags"] | E.FLAG_USES_BARRIER
    struct.pack_into("<I", raw, 12, flags)
    raw = bytes(raw)               # 镜像头也声明 USES_BARRIER
    await tb.q.submit(_load_kernel_cmd(tb.mem, raw))
    await tb.q.run()
    sb.check_int(await E.axil_read(dut, E.MMIO_CP_ERROR), E.CPERR_NONE, "加载通过")

    await tb.q.submit(E.make_cmd(E.CMD_LAUNCH, [f["entry"], 5, flags]))
    sb.check_int(await tb.q.run_expect_error(), E.CPERR_LAUNCH_REJECT,
                 "barrier + 5 warp ⇒ LAUNCH_REJECT")

    await E.axil_write(dut, E.MMIO_CP_RESET, 1)
    await E.axil_write(dut, E.MMIO_TIMEOUT_CYCLES, 4096)
    await E.wait_idle(dut)
    await tb.q.submit(_load_kernel_cmd(tb.mem, raw))
    await tb.q.run()

    tb.sm.error0 = 5                                     # SM0 有未清错误(ERR_NO_RECOV)
    await FallingEdge(dut.clk)
    await tb.q.submit(E.make_cmd(E.CMD_LAUNCH, [f["entry"], 5, flags]))
    sb.check_int(await tb.q.run_expect_error(), E.CPERR_STALE_SM_ERROR,
                 "SM 有错优先于 warp 数检查(§4.1 第 2 条)")
    sb.report()


@cocotb.test()
async def test_sm_fault_does_not_signal(dut):
    """kernel 运行中 SM 报错 ⇒ CP_ERROR = SM_FAULT、**不写信号量**(CMDS §4.3/ISA §2.5)。"""
    sb = Scoreboard("sm_fault")
    tb = CpTb(dut)
    await tb.bring_up(sm_run=20)

    raw = E.assemble_image("nested7.S")
    f = E.image_fields(raw)
    await tb.q.submit(_load_kernel_cmd(tb.mem, raw))
    await tb.q.run()

    sem = E.SEM_BASE + 2 * 4
    await tb.q.submit(E.make_cmd(E.CMD_LAUNCH, [f["entry"], 4, f["flags"]], signal_id=2))
    await E.axil_write(dut, E.MMIO_CMD_TAIL, tb.q.tail)
    await tb.q.doorbell()
    for _ in range(30):                                  # 等它真的跑起来
        await FallingEdge(dut.clk)
    tb.sm.error0 = 3                                     # BAD_ALIGN
    code = 0
    for _ in range(400):
        await FallingEdge(dut.clk)
        code = int(dut.cp_error.value)
        if code != 0:
            break
    sb.check_int(code, E.CPERR_SM_FAULT, "SM_FAULT")
    sb.check_int(tb.mem.peek(sem), 0, "出错 ⇒ 不写信号量")
    sb.report()


# ─────────────────────────── 引擎 ───────────────────────────
@cocotb.test()
async def test_gemm_and_2d_dispatch(dut):
    """GEMM 描述符 + 2D 命令:只按长度校验后把 payload 原样转给引擎;完成 ⇒ 打信号量。"""
    sb = Scoreboard("engine")
    tb = CpTb(dut)
    await tb.bring_up()

    # GEMM:A=0x1000_0000/B/C、M=N=K=2、dtype=0
    desc = [E.CMD_GEMM, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
    desc[0] = (16 << 16) | E.CMD_GEMM                         # word0:len=16, flags=0, opcode
    desc[1] = (7 << 16) | E.SEM_NONE                          # word1:signal_id=7, wait_id=none
    desc[2] = E.DDR_BASE
    desc[3] = E.DDR_BASE + 0x100
    desc[4] = E.DDR_BASE + 0x200
    desc[5] = (2 << 16) | 2                                   # M=2 | N=2
    desc[6] = 2                                               # K=2
    await tb.q.submit(desc)
    # 2D_FILL(GFX §4.1:6 字 = 头 2 字 + dst_addr/color/x|y/w|h 四个参数)
    fill_params = [E.FB_BASE, 0xFF00_FF00, (0 << 16) | 0, (8 << 16) | 8]
    await tb.q.submit(E.make_cmd(E.CMD_2D_FILL, fill_params))
    await tb.q.run()

    sb.check_int(await E.axil_read(dut, E.MMIO_CP_ERROR), E.CPERR_NONE, "引擎命令无错")
    sb.check_int(len(tb.eng.cmds), 2, "两条命令都到了引擎")
    eng0, op0, len0, pay0 = tb.eng.cmds[0]
    sb.check_int((eng0, op0, len0), (2, E.CMD_GEMM, 16), "GEMM 头")
    sb.check_int(pay0, desc[2:16], "GEMM payload 原样转交(word2..word15)")
    eng1, op1, len1, pay1 = tb.eng.cmds[1]
    sb.check_int((eng1, op1, len1), (1, E.CMD_2D_FILL, 6), "2D 头")
    sb.check_int(pay1, fill_params, "2D payload")
    sb.check_int(tb.mem.peek(E.SEM_BASE + 7 * 4), 1, "GEMM 完成 ⇒ SEM[7] += 1")
    sb.check_int((await E.axil_read(dut, E.MMIO_IRQ_STATUS)) & 0x18, 0x18,
                 "IRQ_GEMM_DONE + IRQ_2D_DONE")

    # 2D 长度必须等于规定值 ⇒ 否则 CMD_ERROR
    await tb.q.submit(E.make_cmd(E.CMD_2D_FILL, [0, 0, 8]))         # 5 字(应为 6)
    sb.check_int(await tb.q.run_expect_error(), E.CPERR_CMD, "2D 长度不符 ⇒ CMD_ERROR")
    sb.report()


@cocotb.test()
async def test_engine_fault_does_not_signal(dut):
    """引擎报错 ⇒ CP_ERROR = ENGINE_FAULT 且不写信号量(细节在引擎自己的错误寄存器)。"""
    sb = Scoreboard("eng_fault")
    tb = CpTb(dut)
    await tb.bring_up()
    tb.eng.error = 1
    tb.eng.err_code = 6                                    # BAD_PARAM

    await tb.q.submit(E.make_cmd(E.CMD_2D_FLUSH, [], signal_id=4))
    sb.check_int(await tb.q.run_expect_error(), E.CPERR_ENGINE_FAULT, "ENGINE_FAULT")
    sb.check_int(tb.mem.peek(E.SEM_BASE + 4 * 4), 0, "引擎出错 ⇒ 不写信号量")
    sb.check_int(await E.axil_read(dut, E.MMIO_2D_ERROR), 6, "引擎错误码透传到 MMIO")
    sb.report()


# ─────────────────────────── CP_RESET / 总线错误 ───────────────────────────
@cocotb.test()
async def test_cp_reset_semantics(dut):
    """CP_RESET(§7.3):回 IDLE、清错误/中断/HEAD/KERNEL_MASK, 但**保持**配置寄存器。"""
    sb = Scoreboard("reset")
    tb = CpTb(dut)
    await tb.bring_up()

    await E.axil_write(dut, E.MMIO_FB_BASE, 0x1200_0000)
    await E.axil_write(dut, E.MMIO_SEM_BASE, 0x0300_2000)
    raw = E.assemble_image("nested7.S")
    await tb.q.submit(_load_kernel_cmd(tb.mem, raw))
    await tb.q.run()
    sb.check_int(await E.axil_read(dut, E.MMIO_SM_KERNEL_MASK), 0x3, "加载后 KERNEL_MASK=3")

    await E.axil_write(dut, E.MMIO_CP_RESET, 1)
    await E.wait_idle(dut)
    sb.check_int(await E.axil_read(dut, E.MMIO_SM_KERNEL_MASK), 0, "复位后镜像失效")
    sb.check_int(await E.axil_read(dut, E.MMIO_CP_ERROR), E.CPERR_NONE, "复位清错误")
    sb.check_int(await E.axil_read(dut, E.MMIO_IRQ_STATUS), 0, "复位清中断")
    sb.check_int(await E.axil_read(dut, E.MMIO_FB_BASE), 0x1200_0000, "配置寄存器保持(FB_BASE)")
    sb.check_int(await E.axil_read(dut, E.MMIO_SEM_BASE), 0x0300_2000, "配置寄存器保持(SEM_BASE)")
    await E.axil_write(dut, E.MMIO_SEM_BASE, E.SEM_BASE)
    sb.report()


@cocotb.test()
async def test_bus_error_on_queue_read(dut):
    """命令环读回 BUS_ERROR(§8.1 值 3):总线错误 ⇒ 停机而不是静默继续。"""
    sb = Scoreboard("bus_err")
    tb = CpTb(dut)
    await tb.bring_up()

    await tb.q.submit(E.make_cmd(E.CMD_NOP, []))
    tb.mem.bus_err_next = True
    await tb.q.doorbell()
    for _ in range(400):
        await FallingEdge(dut.clk)
        if int(dut.irq.value) == 1:
            break
    sb.check_int(await E.axil_read(dut, E.MMIO_CP_ERROR), E.CPERR_BUS, "BUS_ERROR")
    sb.report()
