"""Tests for the ``fetch_imem`` DUT —— ``core/fetch.sv`` 接**真的**指令 SRAM。

`test_fetch.py` 里的取指单元面对的是测试台的假 imem(可以人为加延迟);这里是
真正的 ``mem/instr_mem.sv``:测试台扮演 CP 的 ``LOAD_KERNEL`` 把镜像逐字写进去,
之后取指单元就只能靠这块 SRAM 取指了。

这样一条链就闭环了:汇编器 → 镜像 → SRAM 预加载 → PC → 取指 → SIMT 栈 →
(假 EXU 回送分支判定) → 与 §2.3.3 模型逐条对齐。少了任何一环,``fetch.sv`` 的
imem 协议(1 拍回读)或镜像布局约定(整体放偏移 0、PC 从 entry_off 起)都会当场露馅。
"""

from __future__ import annotations

import cocotb

from harness.lib.tb import Scoreboard
from tests.core import stack_model as M
from tests.core.test_fetch import (assemble, compare_trace, run_kernel_rtl,
                                   stack_model_check_pcs)


@cocotb.test()
async def test_nested7_on_real_instr_sram(dut):
    """nested7.S: 预加载进真 SRAM 后跑一遍, 指令/PC/掩码/栈深都要对得上。"""
    sb = Scoreboard("nested7/sram")
    words, entry, size = M.parse_image(assemble("nested7.S"))
    expected = M.run_kernel(words, entry)

    trace, log, stop, cycles = await run_kernel_rtl(dut, words, entry, imem="sram")

    sb.check_int(stop["why"], "exit", "结束原因")
    sb.check_equal(log["data_errors"], [], "数据通路不应报错")
    compare_trace(sb, trace, expected, "nested7/sram")
    sb.check_equal([pc for pc, _, _ in trace], stack_model_check_pcs("nested7.S"),
                   "与 stack_model_check.py 的 PC 序列")
    sb.check_int(int(dut.err_ovf.value), 0, "预加载不应越界")
    sb.check_int(int(dut.err_imem.value), 0, "不应有 err_imem")

    # 取到的指令必须就是镜像里那几个字(证明数据真的来自 SRAM, 而不是巧合)
    for pc, inst in log["insts"]:
        sb.check(inst, words[pc], f"@0x{pc:x} 的指令字来自 SRAM")

    dut._log.info("nested7@真SRAM: %d 条指令 / %d 周期, 镜像 %d 字节, entry=0x%x",
                  len(trace), cycles, size, entry)
    sb.report()


@cocotb.test()
async def test_random_kernels_on_real_instr_sram(dut):
    """随机生成的小内核也走真 SRAM: 顺带覆盖"镜像里有多段跳转目标"的取指路径。"""
    import random

    from tests.core.test_fetch import BASE_SEED, assemble_source, random_kernel

    sb = Scoreboard("random/sram")
    rng = random.Random(BASE_SEED + 0x5A17)
    for p in range(4):
        raw = assemble_source(f"sram_rand{p}", random_kernel(rng, blocks=rng.randint(2, 4)))
        words, entry, _size = M.parse_image(raw)
        expected = M.run_kernel(words, entry)
        trace, log, stop, _cycles = await run_kernel_rtl(dut, words, entry, imem="sram")
        tag = f"sram_rand{p}"
        sb.check_equal(log["data_errors"], [], f"{tag}: 数据通路")
        compare_trace(sb, trace, expected, tag)
        want = "exit" if expected.finished else "halted"
        sb.check_int(stop["why"], want, f"{tag}: 结束原因")
        sb.check_equal([inst for _pc, inst in log["insts"]],
                       [words[pc] for pc, _ in log["insts"]], f"{tag}: 指令字")
    sb.report()
