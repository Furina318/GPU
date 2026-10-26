"""Tests for ``hw/rtl/core/simt_stack.sv`` —— SIMT 栈 + 发散/收敛状态机。

三层验证:

1. **定向用例**: ISA.md §2.3.1 的三种情形、§2.3.3 的"taken 路径先执行 / 切到
   fall 路径 / 弹栈"、空路径特例(fall_pc == recov_pc)、ipdom 的"用后即清"。
2. **随机对拍**: 随机事件流(ipdom / br.simt / 普通推进)与
   :mod:`tests.core.stack_model` 的独立实现逐拍比较 —— 组合输出(next_pc_out /
   active_mask_next)与寄存器状态(active_mask / stack_depth / recov_*)都查。
3. **错误路径**: ERR_NO_RECOV(发散但 recov 无效); SIMT_OVF 需要 DEPTH=2 的实例
   才可达, 所以放在 :mod:`tests.core.test_simt_stack_ovf`。

模型的规则只从 ISA.md §2.3.3 抄, 不参考本 RTL。
"""

from __future__ import annotations

import os
import random

import cocotb

from harness.lib.tb import Scoreboard, reset_dut
from tests.core.dut_drivers import bring_up, do_event, idle
from tests.core.stack_model import FULL_MASK

BASE_SEED = int(os.environ.get("TB_SEED", "0"))
RANDOM_EVENTS = 3000
#: 地址都取这个集合 ⇒ 收敛点撞车(路径完成)会自然频繁发生
ADDRS = [0x00, 0x04, 0x08, 0x0C, 0x10, 0x14, 0x18, 0x1C, 0x20, 0x40, 0x60, 0x70]


# --------------------------------------------------------------------------
# 1. 定向: §2.3.1 三情形 + §2.3.3 切路径/弹栈
# --------------------------------------------------------------------------
@cocotb.test()
async def test_three_branch_cases(dut):
    """均匀不跳 / 均匀跳转都不动栈; 发散才压栈并切 mask(ISA §2.3.1)。"""
    sb = Scoreboard("three_cases")
    model = await bring_up(dut)

    sb.check_int(int(dut.active_mask.value), FULL_MASK, "复位: active_mask")
    sb.check_int(int(dut.stack_depth.value), 0, "复位: stack_depth")

    # ipdom: 写 recov 并置有效位
    await do_event(dut, model, sb, "ipdom", next_pc=0x10, ipdom=0x30)
    sb.check_int(int(dut.recov_pc.value), 0x30, "ipdom: recov_pc")

    # 均匀不跳(taken=0): PC ← PC+4, 栈不动, recov 被用掉
    await do_event(dut, model, sb, "均匀不跳", br=(0x80, 0x14, 0x00))
    sb.check_int(int(dut.stack_depth.value), 0, "均匀不跳: 不压栈")
    sb.check_int(int(dut.recov_valid.value), 0, "均匀不跳: recov 用后即清")

    # 均匀跳转(taken = 全 lane): PC ← label, 栈不动
    await idle(dut)
    await do_event(dut, model, sb, "均匀跳转", br=(0x80, 0x18, FULL_MASK))
    sb.check_int(int(dut.stack_depth.value), 0, "均匀跳转: 不压栈")

    # 发散: 压栈 + mask ← taken + PC ← label
    await idle(dut)
    await do_event(dut, model, sb, "ipdom2", next_pc=0x1C, ipdom=0x60)
    await do_event(dut, model, sb, "发散", br=(0x80, 0x20, 0x0F))
    sb.check_int(int(dut.stack_depth.value), 1, "发散: 压 1 帧")
    sb.check_int(int(dut.active_mask.value), 0x0F, "发散: mask ← taken")

    # taken 路径到达收敛点: 不执行收敛点, 切到 fall 路径(mask = mask_pending)
    await do_event(dut, model, sb, "切 fall 路径", next_pc=0x60)
    sb.check_int(int(dut.stack_depth.value), 1, "切路径: 帧还在")
    sb.check_int(int(dut.active_mask.value), 0xF0, "切路径: mask ← mask_pending")

    # 第二条路径到达收敛点: 弹栈, 完整 mask, PC 保持收敛点
    await do_event(dut, model, sb, "弹栈", next_pc=0x60)
    sb.check_int(int(dut.stack_depth.value), 0, "弹栈: 栈空")
    sb.check_int(int(dut.active_mask.value), FULL_MASK, "弹栈: 完整 mask")
    sb.report()


@cocotb.test()
async def test_empty_path_special_case(dut):
    """fall_pc == recov_pc(无 else 的 if): 直接弹栈, 不多花一拍(§2.3.3 第 3 条)。"""
    sb = Scoreboard("empty_path")
    model = await bring_up(dut)

    await do_event(dut, model, sb, "ipdom", next_pc=0x04, ipdom=0x40)
    await do_event(dut, model, sb, "发散(空路径)", br=(0x80, 0x40, 0x0F))
    sb.check_int(int(dut.stack_depth.value), 1, "空路径: 压 1 帧")

    await do_event(dut, model, sb, "回到收敛点", next_pc=0x40)
    sb.check_int(int(dut.stack_depth.value), 0, "空路径: 直接弹栈")
    sb.check_int(int(dut.active_mask.value), FULL_MASK, "空路径: 完整 mask")
    sb.report()


@cocotb.test()
async def test_nested_and_multipop(dut):
    """嵌套发散 + 一次推进里连续弹两帧(下层 pending=1 时)。

    构造: A 发散(pending=0) → A 切到 fall 路径(pending=1) → 在 A 的 fall 路径上
    再发散出 B → B 切到自己的 fall 路径(pending=1) → B 的 fall 路径抵达收敛点:
    同一拍先弹 B, 再弹 A(两帧的 recov_pc 相同)。
    """
    sb = Scoreboard("multipop")
    model = await bring_up(dut)

    recov = 0x60
    # A: 压栈
    await do_event(dut, model, sb, "ipdom A", next_pc=0x04, ipdom=recov)
    await do_event(dut, model, sb, "发散 A", br=(0x10, 0x08, 0x0F))
    sb.check_int(int(dut.stack_depth.value), 1, "A: 栈深 1")
    # A 的 taken 路径到收敛点 ⇒ 切到 A 的 fall 路径(pending=1)
    await do_event(dut, model, sb, "A 切 fall", next_pc=recov)
    sb.check_int(int(dut.active_mask.value), 0xF0, "A 切 fall: mask ← mask_pending")

    # 在 A 的 fall 路径上再发散出 B
    await do_event(dut, model, sb, "ipdom B", next_pc=0x0C, ipdom=recov)
    await do_event(dut, model, sb, "发散 B", br=(0x14, 0x0C, 0x30))
    sb.check_int(int(dut.stack_depth.value), 2, "B: 栈深 2")
    sb.check_int(int(dut.active_mask.value), 0x30, "B: mask ← taken")

    # B 的 taken 路径到收敛点 ⇒ 切到 B 的 fall 路径
    await do_event(dut, model, sb, "B 切 fall", next_pc=recov)
    sb.check_int(int(dut.stack_depth.value), 2, "B 切 fall: 帧还在")
    sb.check_int(int(dut.active_mask.value), 0xC0, "B 切 fall: mask ← mask_pending")

    # B 的 fall 路径到收敛点: 弹 B(pending=1), 紧接着 A 也 pending=1 且同收敛点 ⇒ 连弹两帧
    await do_event(dut, model, sb, "连弹两帧", next_pc=recov)
    sb.check_int(int(dut.stack_depth.value), 0, "连弹两帧: 栈空")
    sb.check_int(int(dut.active_mask.value), FULL_MASK, "连弹两帧: 完整 mask")
    sb.report()


@cocotb.test()
async def test_no_recov_error(dut):
    """发散但 recov 有效位为 0 ⇒ ERR_NO_RECOV 脉冲, 且不压栈(ISA §2.6)。"""
    sb = Scoreboard("no_recov")
    model = await bring_up(dut)

    await do_event(dut, model, sb, "无 ipdom 的发散", br=(0x80, 0x20, 0x0F))
    sb.check_int(int(dut.err_no_recov.value), 0, "脉冲已过: 无 recov 错误")
    sb.check_int(int(dut.stack_depth.value), 0, "ERR_NO_RECOV: 不压栈")
    sb.check_int(int(dut.active_mask.value), FULL_MASK, "ERR_NO_RECOV: mask 不变")

    await reset_dut(dut)
    sb.check_int(int(dut.err_no_recov.value), 0, "复位: 错误清零")
    sb.report()


@cocotb.test()
async def test_exu_sends_unmasked_taken(dut):
    """EXU 送来的 taken 若未与 active_mask 相交, RTL 必须自己再交一次(§2.3.1)。"""
    sb = Scoreboard("unmasked_taken")
    model = await bring_up(dut)

    await do_event(dut, model, sb, "ipdom", next_pc=0x04, ipdom=0x40)
    await do_event(dut, model, sb, "发散", br=(0x80, 0x08, 0x0F))     # 0xFF → 0x0F
    # 此时 mask=0x0F, EXU 却送来超集 taken=0x7F: 相交后 = active ⇒ 退化成均匀跳转
    await do_event(dut, model, sb, "超集 taken", br=(0x90, 0x0C, 0x7F))
    sb.check_int(int(dut.stack_depth.value), 1, "超集 taken: 不再压栈")
    sb.report()


# --------------------------------------------------------------------------
# 2. 随机对拍
# --------------------------------------------------------------------------
@cocotb.test()
async def test_random_events_vs_model(dut):
    """随机事件流 vs 独立模型: 组合输出与寄存器状态逐拍比较。"""
    sb = Scoreboard("random")
    model = await bring_up(dut)
    rng = random.Random(BASE_SEED + 0xC0FFEE)

    stats = {"plain": 0, "ipdom": 0, "branch": 0, "diverge": 0, "reconv": 0}
    pending_ipdom: int | None = None

    for ev in range(RANDOM_EVENTS):
        where = f"ev{ev}"
        roll = rng.random()
        reconv_before = model.reconvergences
        if roll < 0.35:
            await do_event(dut, model, sb, where, next_pc=rng.choice(ADDRS))
            stats["plain"] += 1
        elif roll < 0.55:
            recov = rng.choice(ADDRS)
            await do_event(dut, model, sb, where, next_pc=rng.choice(ADDRS), ipdom=recov)
            stats["ipdom"] += 1
            pending_ipdom = recov
        else:
            # 三成概率刻意"漏 ipdom"⇒ 覆盖 ERR_NO_RECOV 与正常路径混跑
            if pending_ipdom is None and rng.random() < 0.3:
                taken = rng.choice([0x0F, 0x03, 0x55, FULL_MASK, 0x00])
            else:
                taken = rng.choice([0x0F, 0x03, 0x55, 0xAA, 0xF0, 0x01, 0x80, FULL_MASK, 0x00])
            before = model.depth_now
            await do_event(dut, model, sb, where,
                           br=(rng.choice(ADDRS), rng.choice(ADDRS), taken))
            pending_ipdom = None
            stats["branch"] += 1
            if model.depth_now > before:
                stats["diverge"] += 1
        if model.reconvergences != reconv_before:
            stats["reconv"] += 1

    sb.check_equal(stats["diverge"] > 100, True, f"随机激励应覆盖发散({stats})")
    sb.check_equal(stats["reconv"] > 100, True, f"随机激励应覆盖收敛({stats})")
    sb.check_equal(stats["ipdom"] > 100, True, f"随机激励应覆盖 ipdom({stats})")
    sb.report()
    dut._log.info("随机事件统计 %s, 最终栈深 %d(共 %d 次检查)",
                  stats, model.depth_now, sb.checks)


# --------------------------------------------------------------------------
# 3. 栈深: 8 lane 最多压 7 层(ISA §2.6 的结论)
# --------------------------------------------------------------------------
@cocotb.test()
async def test_depth_seven_is_enough(dut):
    """8 lane + 8 深栈: 7 层嵌套发散能压满到 7, 第 8 层必然退化成均匀分支。

    这正是 ISA §2.6 的结论("SIMT_OVF 在 v0.3 的配置下不可达"): 压栈要求
    taken 与 not_taken 都非空 ⇒ 每压一层至少吃掉一个 lane。
    """
    sb = Scoreboard("depth7")
    model = await bring_up(dut)

    recov = 0x7C
    mask = FULL_MASK
    for level in range(7):
        taken = mask & ~(1 << level)
        await do_event(dut, model, sb, f"L{level} ipdom", next_pc=0x04, ipdom=recov)
        await do_event(dut, model, sb, f"L{level} 发散",
                       br=(0x20 + 4 * level, 0x40, taken))
        mask = taken
    sb.check_int(int(dut.stack_depth.value), 7, "7 层嵌套: 栈深 7")
    sb.check_int(int(dut.err_simt_ovf.value), 0, "7 层嵌套: 没有溢出")

    # 第 8 层: mask 只剩 1 个 lane(0x80), taken 只能取 0 或 0x80 ⇒ 均匀分支, 不压栈
    await do_event(dut, model, sb, "第 8 层(退化)", br=(0x44, 0x48, 0x80))
    sb.check_int(int(dut.stack_depth.value), 7, "第 8 层: 不压栈")
    sb.check_int(int(dut.err_simt_ovf.value), 0, "第 8 层: 仍无 SIMT_OVF")
    sb.report()


@cocotb.test()
async def test_divergent_branch_lands_on_recov(dut):
    """边角情形: 分支目标正好就是收敛点(压栈与"切到 fall 路径"落在同一帧上)。

    期望与模型一致: 帧在、pending=1、掩码切成 mask_pending、PC 走 fall 路径;
    再到收敛点时弹栈回到完整 mask。RTL 侧对应"同一次写回里就把 pending 置上" ——
    对同一个槽发两次写会依赖工具的位写合并语义, 不该赌这一点。
    """
    sb = Scoreboard("land_on_recov")
    model = await bring_up(dut)

    recov = 0x40
    await do_event(dut, model, sb, "ipdom", next_pc=0x04, ipdom=recov)
    await do_event(dut, model, sb, "发散且目标==收敛点", br=(recov, 0x08, 0x0F))
    sb.check_int(int(dut.stack_depth.value), 1, "压栈 + 立即切路径: 帧还在")
    sb.check_int(int(dut.active_mask.value), 0xF0, "压栈 + 立即切路径: mask ← mask_pending")

    await do_event(dut, model, sb, "fall 路径到收敛点", next_pc=recov)
    sb.check_int(int(dut.stack_depth.value), 0, "弹栈: 栈空")
    sb.check_int(int(dut.active_mask.value), FULL_MASK, "弹栈: 完整 mask")
    sb.report()
