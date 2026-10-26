"""``hw/rtl/mem/shared_mem.sv`` 的用例 —— SM 私有共享内存(ISA §3.2)。

覆盖 MEM_LSU.md §3 不变量 2 的四条:

* 8 lane 连续地址 ⇒ 零冲突(每 lane 一个 bank);
* 全部 lane 同地址 ⇒ 广播, 1 轮;
* 同 bank 异地址 ⇒ 串行化, ``conflict_extra = Σ_b(不同地址数 − 1)``;
* 被谓词屏蔽的 lane 不参与(既不产生读写, 也不计入冲突)。

另外验每条错误的可触发路径对齐(未 4B 对齐 / 越界)与"出错时整体不执行"。

时序约定与 ``test_instr_mem`` 一致: 在 ``FallingEdge``(周期中点)驱动,
RTL 在随后的上升沿采样; ``rsp_valid`` 那一拍的中点读回数据。
"""

from __future__ import annotations

import random

import cocotb
from cocotb.triggers import FallingEdge, RisingEdge, Timer

from harness.lib.tb import Scoreboard, reset_dut
from tests.core.dut_drivers import start_clock_once

LANES = 8
SIZE_BYTES = 16 * 1024


def bank_of(addr: int) -> int:
    return (addr >> 2) & 7


def expected_conflict(addrs: list[int], mask: int) -> int:
    """max_b(非空 bank 的不同地址数) − 1。

    8 个 bank 并行, 每轮每个非空 bank 服务一个地址 ⇒ 轮数 = 最忙那个 bank 的
    不同地址数(ISA §3.2 的"冲突串行化"), 冲突额外拍 = 轮数 − 1。
    """
    distinct: dict[int, set[int]] = {}
    for lane in range(LANES):
        if not (mask >> lane) & 1:
            continue
        distinct.setdefault(bank_of(addrs[lane]), set()).add(addrs[lane])
    if not distinct:
        return 0
    return max(len(s) for s in distinct.values()) - 1


async def idle_inputs(dut) -> None:
    dut.req_valid.value = 0
    dut.req_we.value = 0
    dut.lane_en.value = 0
    for lane in range(LANES):
        dut.lane_addr[lane].value = 0
        dut.lane_wdata[lane].value = 0


async def do_access(
    dut,
    *,
    we: int,
    addrs: list[int],
    wdatas: list[int] | None = None,
    mask: int = 0xFF,
    max_cycles: int = 32,
    expect_rsp: bool = True,
):
    """发一次 warp 访存, 返回 (lane_rdata, conflict_extra, 用时拍数, align/oob 错误)。

    ``expect_rsp=False`` 用于错误用例: 出错时整体不执行, 自然不会有 rsp。
    """
    await FallingEdge(dut.clk)
    dut.req_valid.value = 1
    dut.req_we.value = we
    dut.lane_en.value = mask
    for lane in range(LANES):
        dut.lane_addr[lane].value = addrs[lane]
        dut.lane_wdata[lane].value = 0 if wdatas is None else wdatas[lane]
    # 组合输出(错误检查)要等一个 delta 才稳定
    await Timer(1, "ns")
    err_align = int(dut.err_align.value)
    err_oob = int(dut.err_oob.value)
    await RisingEdge(dut.clk)

    if not expect_rsp:
        await RisingEdge(dut.clk)
        await FallingEdge(dut.clk)
        dut.req_valid.value = 0
        dut.lane_en.value = 0
        await RisingEdge(dut.clk)
        return [0] * LANES, 0, 1, err_align, err_oob

    cycles = 0
    while True:
        await FallingEdge(dut.clk)
        cycles += 1
        if int(dut.rsp_valid.value):
            break
        if cycles > max_cycles:
            raise AssertionError(f"shared_mem 超时({cycles} 拍无 rsp_valid)")
        await RisingEdge(dut.clk)

    rdata = [int(dut.lane_rdata[lane].value) for lane in range(LANES)]
    conflict = int(dut.conflict_extra.value)
    # 请求撤回到下一拍的 IDLE
    dut.req_valid.value = 0
    dut.lane_en.value = 0
    await RisingEdge(dut.clk)
    return rdata, conflict, cycles, err_align, err_oob


async def fresh(dut) -> None:
    await start_clock_once(dut)
    await idle_inputs(dut)
    await reset_dut(dut)
    await idle_inputs(dut)


@cocotb.test()
async def test_conflict_free_and_store_roundtrip(dut):
    """8 lane 连续地址: 零冲突写入 → 零冲突读出, 数据逐 lane 正确。"""
    sb = Scoreboard("smem-conflictfree")
    await fresh(dut)

    addrs = [lane * 4 for lane in range(LANES)]          # 同一 32B 行, 8 个 bank
    wdatas = [0x1000_0000 + lane for lane in range(LANES)]
    _, conflict, _, ea, eo = await do_access(dut, we=1, addrs=addrs, wdatas=wdatas)
    sb.check_int(conflict, 0, "8 lane 连续地址应当零冲突")
    sb.check_int(ea, 0, "对齐检查")
    sb.check_int(eo, 0, "越界检查")

    rdata, conflict, _, _, _ = await do_access(dut, we=0, addrs=addrs)
    sb.check_int(conflict, 0, "读出同样零冲突")
    for lane in range(LANES):
        sb.check(rdata[lane], wdatas[lane], f"lane{lane} 读回")
    sb.report()


@cocotb.test()
async def test_broadcast_same_address(dut):
    """全部 lane 同地址: 1 轮广播, 8 个 lane 拿到同一份数据。"""
    sb = Scoreboard("smem-broadcast")
    await fresh(dut)

    addr = [0x40] * LANES
    await do_access(dut, we=1, addrs=addr, wdatas=[0xDEAD_BEEF] * LANES)
    rdata, conflict, _, _, _ = await do_access(dut, we=0, addrs=addr)
    sb.check_int(conflict, 0, "同地址是广播, 不算冲突")
    for lane in range(LANES):
        sb.check(rdata[lane], 0xDEAD_BEEF, f"lane{lane} 广播读回")
    sb.report()


@cocotb.test()
async def test_bank_conflict_serializes(dut):
    """同 bank 异地址: 冲突拍数 = Σ_b(不同地址数 − 1), 且每个 lane 拿到自己的字。"""
    sb = Scoreboard("smem-conflict")
    await fresh(dut)

    # bank0 的两个地址: 0x00 与 0x20(addr[4:2] 都是 0)
    addrs = [0x00 if lane % 2 == 0 else 0x20 for lane in range(LANES)]
    sb.check_int(bank_of(0x00), bank_of(0x20), "用例前提: 两地址同 bank")
    wdatas = [0xA000_0000 + lane for lane in range(LANES)]
    _, conflict, _, _, _ = await do_access(dut, we=1, addrs=addrs, wdatas=wdatas)
    sb.check_int(conflict, expected_conflict(addrs, 0xFF), "冲突拍数")
    sb.check_int(conflict, 1, "一个 bank 两个地址 ⇒ 多 1 拍")

    rdata, conflict, _, _, _ = await do_access(dut, we=0, addrs=addrs)
    sb.check_int(conflict, 1, "读同样多 1 拍")
    # 同地址多 lane 写时取最低 lane 的数据(写入语义)
    sb.check(rdata[0], wdatas[0], "lane0 读回")
    sb.check(rdata[2], wdatas[0], "lane2 与 lane0 同地址 ⇒ 读到最低 lane 写的值")
    sb.check(rdata[1], wdatas[1], "lane1 读回")
    sb.check(rdata[3], wdatas[1], "lane3 与 lane1 同地址")
    sb.report()


@cocotb.test()
async def test_masked_lanes_ignored(dut):
    """被屏蔽的 lane: 不读写、不计冲突、读回保持 0。"""
    sb = Scoreboard("smem-mask")
    await fresh(dut)

    addrs = [lane * 4 for lane in range(LANES)]
    wdatas = [0x5555_0000 + lane for lane in range(LANES)]
    mask = 0b0101_0101        # 偶数 lane 参与
    _, conflict, _, _, _ = await do_access(dut, we=1, addrs=addrs, wdatas=wdatas, mask=mask)
    sb.check_int(conflict, 0, "屏蔽一半 lane 后仍零冲突")

    rdata, _, _, _, _ = await do_access(dut, we=0, addrs=addrs, mask=mask)
    for lane in range(LANES):
        if (mask >> lane) & 1:
            sb.check(rdata[lane], wdatas[lane], f"参与 lane{lane} 读回")
        else:
            sb.check(rdata[lane], 0, f"屏蔽 lane{lane} 不产生数据")
    sb.report()


@cocotb.test()
async def test_align_and_oob_errors(dut):
    """两条架构错误的可触发路径, 且出错时整体不执行(不写坏 SRAM)。"""
    sb = Scoreboard("smem-err")
    await fresh(dut)

    bad_align = [0x02] * LANES
    _, _, _, ea, _ = await do_access(dut, we=1, addrs=bad_align, wdatas=[1] * LANES,
                                     expect_rsp=False)
    sb.check_int(ea, 1, "未 4B 对齐必须报 err_align")

    oob = [SIZE_BYTES] * LANES
    _, _, _, _, eo = await do_access(dut, we=1, addrs=oob, wdatas=[1] * LANES,
                                     expect_rsp=False)
    sb.check_int(eo, 1, "超出 16KB 必须报 err_oob")

    # 出错的那两次没有写进 SRAM: 0 号字仍是 0
    rdata, _, _, _, _ = await do_access(dut, we=0, addrs=[0] * LANES)
    sb.check(rdata[0], 0, "出错的写不生效")
    sb.report()


@cocotb.test()
async def test_random_differential(dut):
    """随机命令流对独立数组模型逐次对拍(含冲突拍数)。"""
    sb = Scoreboard("smem-random")
    await fresh(dut)
    rng = random.Random(20251026)
    model = [0] * (SIZE_BYTES // 4)

    for step in range(120):
        mask = rng.choice([0xFF, 0xFF, 0x0F, 0x81, 0x3C])
        we = rng.getrandbits(1)
        base = rng.randrange(0, SIZE_BYTES // 4, 8) * 4
        addrs, wdatas = [], []
        for lane in range(LANES):
            if rng.random() < 0.75:
                word = (base // 4 + lane) if rng.random() < 0.7 else rng.randrange(0, SIZE_BYTES // 4)
            else:
                word = rng.randrange(0, SIZE_BYTES // 4)
            addrs.append(word * 4)
            wdatas.append(rng.getrandbits(32))

        rdata, conflict, _, ea, eo = await do_access(dut, we=we, addrs=addrs, wdatas=wdatas, mask=mask)
        sb.check_int(ea, 0, f"step{step} 不应对齐错")
        sb.check_int(eo, 0, f"step{step} 不应越界")
        sb.check_int(conflict, expected_conflict(addrs, mask), f"step{step} 冲突拍数")

        if we:
            # 同地址多写: 最低 lane 胜(与 RTL 一致)
            for lane in range(LANES):
                if (mask >> lane) & 1:
                    first = next(x for x in range(LANES) if ((mask >> x) & 1) and addrs[x] == addrs[lane])
                    model[addrs[first] // 4] = wdatas[first]
        else:
            for lane in range(LANES):
                if (mask >> lane) & 1:
                    sb.check(rdata[lane], model[addrs[lane] // 4], f"step{step} lane{lane} 数据")
    sb.report()
