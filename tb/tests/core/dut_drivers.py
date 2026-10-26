"""simt_stack / fetch 两个 cocotb DUT 共用的驱动与比较助手。

``do_event`` 是核心: 一拍之内驱动一次"指令推进"(可带 ipdom / br.simt 事件),
先与独立模型比较组合输出(next_pc_out / active_mask_next / 错误脉冲), 再过一个
时钟沿比较寄存器状态(active_mask / stack_depth / recov_*)。
"""

from __future__ import annotations

import cocotb
from cocotb.triggers import RisingEdge, Timer

from harness.lib.tb import Scoreboard, reset_dut, start_clock
from tests.core.stack_model import SimtStackModel

CLK_NS = 10


async def start_clock_once(dut) -> None:
    """确保时钟在跑。

    cocotb 在 test 结束时会清掉它起的 task, 所以每个 test 的第一次调用要重新起一个;
    同一个 test 里重复调用则不能重复驱动同一个信号 —— 这里用"半拍后再看一次"来判断
    时钟是不是已经在跳。
    """
    before = dut.clk.value
    await Timer(CLK_NS / 2, unit="ns")
    if dut.clk.value != before:
        return
    start_clock(dut.clk, CLK_NS)


async def idle(dut) -> None:
    """一拍无事发生(事件输入全 0), 并推进一个时钟。"""
    dut.launch.value = 0
    dut.ipdom_valid.value = 0
    dut.ipdom_recov_pc.value = 0
    dut.br_valid.value = 0
    dut.br_target_pc.value = 0
    dut.br_fall_pc.value = 0
    dut.br_taken_mask.value = 0
    dut.step_valid.value = 0
    dut.next_pc_in.value = 0
    await RisingEdge(dut.clk)


async def bring_up(dut, depth: int = 8, **init) -> SimtStackModel:
    """启动时钟、复位 DUT、拉高额外的输入, 并返回一个同步的模型。"""
    await start_clock_once(dut)
    for name, value in init.items():
        getattr(dut, name).value = value
    # 启动掩码: 正常内核是全 lane(8 个 lane 都活); 老版本 RTL 没有这个端口
    if hasattr(dut, "launch_mask"):
        dut.launch_mask.value = (1 << depth) - 1 if depth < 8 else 0xFF
    if hasattr(dut, "launch_warp"):
        dut.launch_warp.value = 0xFF
    await reset_dut(dut)
    model = SimtStackModel(depth=depth)
    model.launch()
    await idle(dut)
    return model


async def do_event(dut, model: SimtStackModel, sb: Scoreboard, ctx: str, *,
                   next_pc: int | None = None, ipdom: int | None = None,
                   br: tuple[int, int, int] | None = None) -> None:
    """驱动一次 step(可带 ipdom / br 事件), 与模型逐拍比较。

    ``br`` = (target_pc, fall_pc, taken_raw); ``next_pc`` 省略时按 0 处理。
    """
    step = (next_pc is not None) or (br is not None)
    dut.step_valid.value = int(step)
    dut.next_pc_in.value = (next_pc if next_pc is not None else 0)
    dut.ipdom_valid.value = int(ipdom is not None)
    dut.ipdom_recov_pc.value = (ipdom if ipdom is not None else 0)
    dut.br_valid.value = int(br is not None)
    dut.br_target_pc.value = br[0] if br else 0
    dut.br_fall_pc.value = br[1] if br else 0
    dut.br_taken_mask.value = br[2] if br else 0

    # 模型: 先算出"这次推进之后"的样子(模型状态随之推进)
    exp = model.step(next_pc if next_pc is not None else 0, ipdom_pc=ipdom, br=br)

    await Timer(1, unit="ns")            # 组合输出稳定
    sb.check_int(int(dut.next_pc_out.value), exp.next_pc, f"{ctx}: next_pc_out")
    sb.check_int(int(dut.active_mask_next.value), exp.mask, f"{ctx}: active_mask_next")
    sb.check_int(int(dut.err_no_recov.value), int("ERR_NO_RECOV" in exp.errors),
                 f"{ctx}: err_no_recov")
    sb.check_int(int(dut.err_simt_ovf.value), int("SIMT_OVF" in exp.errors),
                 f"{ctx}: err_simt_ovf")
    sb.check_int(int(dut.err_internal.value), int("INTERNAL" in exp.errors),
                 f"{ctx}: err_internal")

    # 时钟沿: 寄存器状态跟模型对齐
    await RisingEdge(dut.clk)
    await Timer(1, unit="ns")
    sb.check_int(int(dut.active_mask.value), model.mask, f"{ctx}: active_mask")
    sb.check_int(int(dut.stack_depth.value), model.depth_now, f"{ctx}: stack_depth")
    sb.check_int(int(dut.stack_empty.value), int(model.empty), f"{ctx}: stack_empty")
    sb.check_int(int(dut.recov_valid.value), int(model.recov_valid), f"{ctx}: recov_valid")
    sb.check_int(int(dut.recov_pc.value), model.recov_pc, f"{ctx}: recov_pc")

    # 清事件输入, 便于下一拍观察(清完要等组合逻辑重新稳定, 否则会读到旧值)
    dut.step_valid.value = 0
    dut.ipdom_valid.value = 0
    dut.br_valid.value = 0
    await Timer(1, unit="ns")
