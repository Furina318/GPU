"""SIMT_OVF(ISA §2.6): 压栈时栈深已达 DEPTH。

按 ISA §2.6 的说明, **DEPTH=8 / 8 lane 的配置下这条错误不可达**(压栈要求
taken 与 not_taken 都非空 ⇒ 每层至少吃掉一个 lane, 最多压 7 层)。所以这个测试
用 ``DEPTH=2`` 的实例(``tb/harness/duts.py`` 里的 ``simt_stack_ovf``)把这条
"RTL 自检"路径真正跑出来: 它防的是栈指针写错、栈深参数被改小这类实现事故。
"""

from __future__ import annotations

import cocotb
from cocotb.triggers import RisingEdge

from harness.lib.tb import Scoreboard, reset_dut
from tests.core.dut_drivers import bring_up, do_event, idle
from tests.core.stack_model import FULL_MASK

DEPTH = 2


@cocotb.test()
async def test_simt_ovf(dut):
    """栈满(2 帧)后再来一次发散 ⇒ SIMT_OVF 脉冲, 且栈/掩码不被破坏。"""
    sb = Scoreboard("simt_ovf")
    model = await bring_up(dut, depth=DEPTH)

    recov = 0x50
    # 第 1 层: 0xFF → taken 0x0F
    await do_event(dut, model, sb, "ipdom 1", next_pc=0x04, ipdom=recov)
    await do_event(dut, model, sb, "发散 1", br=(0x10, 0x08, 0x0F))
    sb.check_int(int(dut.stack_depth.value), 1, "第 1 层: 栈深 1")
    # 第 2 层: 0x0F → taken 0x03
    await do_event(dut, model, sb, "ipdom 2", next_pc=0x0C, ipdom=recov)
    await do_event(dut, model, sb, "发散 2", br=(0x14, 0x0C, 0x03))
    sb.check_int(int(dut.stack_depth.value), 2, "第 2 层: 栈深 2(DEPTH)")

    # 第 3 层: 掩码 0x03, taken 0x01 ⇒ 仍会发散, 但栈已满
    await do_event(dut, model, sb, "ipdom 3", next_pc=0x10, ipdom=recov)
    await do_event(dut, model, sb, "发散 3(溢出)", br=(0x18, 0x10, 0x01))
    sb.check_int(int(dut.err_simt_ovf.value), 0, "溢出脉冲已过")
    sb.check_int(int(dut.stack_depth.value), DEPTH, "溢出: 栈深不变")
    sb.check_int(int(dut.active_mask.value), 0x03, "溢出: 掩码不变")
    sb.check_int(int(dut.err_internal.value), 0, "溢出: 不是 INTERNAL")

    # 栈内容仍可用: 回到收敛点 ⇒ 切到第 2 层未执行的 lane(0x0C)
    await do_event(dut, model, sb, "回到收敛点", next_pc=recov)
    sb.check_int(int(dut.stack_depth.value), DEPTH, "恢复: 帧还在")
    sb.check_int(int(dut.active_mask.value), 0x0C, "恢复: mask ← mask_pending")
    sb.report()


@cocotb.test()
async def test_reset_clears_depth(dut):
    """复位/launch 之后栈深归零、掩码全 1(新内核的起点, ISA §3.1)。"""
    sb = Scoreboard("reset_depth")
    model = await bring_up(dut, depth=DEPTH)

    await do_event(dut, model, sb, "ipdom", next_pc=0x04, ipdom=0x30)
    await do_event(dut, model, sb, "发散", br=(0x10, 0x08, 0x0F))
    sb.check_int(int(dut.stack_depth.value), 1, "压栈后: 栈深 1")

    await reset_dut(dut)
    await idle(dut)
    sb.check_int(int(dut.stack_depth.value), 0, "复位后: 栈空")
    sb.check_int(int(dut.active_mask.value), FULL_MASK, "复位后: 全 lane 活跃")

    # launch 同样清栈(新 kernel 的起点); 复位之后模型也要跟着回到起点
    model.launch()
    await do_event(dut, model, sb, "ipdom", next_pc=0x04, ipdom=0x30)
    await do_event(dut, model, sb, "发散", br=(0x10, 0x08, 0x0F))
    sb.check_int(int(dut.stack_depth.value), 1, "launch 前: 栈深 1")
    model.launch()
    dut.launch_mask.value = FULL_MASK      # launch 的 lane 掩码输入必须驱动(全 warp 常驻, ISA §2.2)
    dut.launch.value = 1
    await RisingEdge(dut.clk)
    dut.launch.value = 0
    await idle(dut)
    sb.check_int(int(dut.stack_depth.value), 0, "launch 后: 栈空")
    sb.check_int(int(dut.active_mask.value), FULL_MASK, "launch 后: 全 lane 活跃")
    sb.check_int(int(dut.recov_valid.value), 0, "launch 后: recov 无效")
    sb.report()
