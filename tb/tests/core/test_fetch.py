"""Tests for ``hw/rtl/core/fetch.sv`` —— 每 warp 的取指单元 + SIMT 栈集成。

测试台替 DUT 准备了三样东西(都在本文件里, 都是"环境"而不是"答案"):

* **假指令 SRAM**: 本周期收到 ``imem_req``, 下一周期给出 ``imem_rsp``(1 拍延迟,
  与 fetch.sv 头注释、SPEC.md §3.2 的 imem 接口一致);
* **假译码**: ``fet_out_ready`` 常高, 记下每条被送出的 (pc, inst_mask, 栈深);
* **假 EXU**: 只做 RTL 交出去的那件事 —— 对**谓词字段不是 p0** 的 br.simt 回送
  ``target_valid`` / ``target_pc`` / ``target_taken``(p0 分支由 fetch 就地判定)。

期望值来自 :mod:`tests.core.stack_model`(§2.3.3 的独立实现)与
``sim/simulator/tests/stack_model_check.py``(仓库里已有的第二份独立模型)——
真实内核(nested7 / err_recov / err_bar_div)的 PC 序列、lane 掩码、栈深逐条对齐。
"""

from __future__ import annotations

import os
import random
import subprocess
import sys
from pathlib import Path

import cocotb
from cocotb.triggers import FallingEdge, RisingEdge, Timer

from harness.lib.tb import Scoreboard, reset_dut
from tests.core import stack_model as M
from tests.core.dut_drivers import start_clock_once

REPO = Path(__file__).resolve().parents[3]
ASM = REPO / "sw" / "assembly.py"
KERNEL_DIR = REPO / "sim" / "simulator" / "tests"
BUILD_DIR = REPO / "tb" / "build" / "core_tests"

MAX_CYCLES = 20000
BASE_SEED = int(os.environ.get("TB_SEED", "0"))
FULL_MASK = M.FULL_MASK
OP_CTRL = M.OP_CTRL
IDLE_INPUTS = (("fet_in_valid", 0), ("fet_out_ready", 1), ("halt", 0),
               ("launch", 0), ("launch_warp", FULL_MASK), ("imem_rsp_valid", 0), ("target_valid", 0))


# --------------------------------------------------------------------------
# 汇编 + 镜像
# --------------------------------------------------------------------------
def assemble(name: str) -> bytes:
    """用仓库的汇编器生成镜像(镜像整体放在 SRAM 偏移 0, PC 从 entry 起)。"""
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    src = Path(name) if os.path.isabs(name) else KERNEL_DIR / name
    img = BUILD_DIR / (src.stem + ".bin")
    proc = subprocess.run([sys.executable, str(ASM), str(src), "--format", "image",
                           "-o", str(img)], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"汇编 {src} 失败: {proc.stdout}{proc.stderr}")
    return img.read_bytes()


def blank_log() -> dict:
    log = {"imem_reqs": [], "issued": [], "insts": [], "resolved": [], "p0_branches": [],
           "ext_branches": [], "br_pending": [], "br_local": [], "br_resolved": [],
           "err_no_recov": [], "err_simt_ovf": [], "err_imem": [], "data_errors": []}
    return log


def drive_inputs(dut, pairs) -> None:
    """只驱动 DUT 真有的端口(fetch 与 fetch_imem 的端口不完全一样)。"""
    for name, value in pairs:
        if hasattr(dut, name):
            getattr(dut, name).value = value


# --------------------------------------------------------------------------
# 假环境(task)
# --------------------------------------------------------------------------
async def imem_responder(dut, words, log, extra_delay: int = 0) -> None:
    """指令 SRAM: 本周期收到请求, ``1 + extra_delay`` 拍后给出指令。"""
    pending: list[list] = []          # [[剩余延迟, inst], ...]
    while True:
        await RisingEdge(dut.clk)
        out = None
        rest = []
        for entry in pending:
            if entry[0] == 0:
                out = entry[1]        # 一次只会有一个在途请求(fetch 会等)
            else:
                entry[0] -= 1
                rest.append(entry)
        pending = rest
        if out is None:
            dut.imem_rsp_valid.value = 0
        else:
            dut.imem_rsp_valid.value = 1
            dut.imem_rsp_inst.value = out
        await FallingEdge(dut.clk)
        if int(dut.imem_req_valid.value) == 1:
            pc = int(dut.imem_req_pc.value)
            log["imem_reqs"].append(pc)
            pending.append([extra_delay, words.get(pc, 0)])


async def decode_monitor(dut, warp, trace, log, stop, stop_on_halt: bool = True) -> None:
    """下游译码: 记录送出的指令, 并维护小数据通路(代替"执行")。"""
    while True:
        await FallingEdge(dut.clk)
        if stop_on_halt and int(dut.halted.value) == 1:
            stop["why"] = stop["why"] or "halted"
            stop["done"] = True
            return
        if int(dut.fet_out_valid.value) == 1 and int(dut.fet_out_ready.value) == 1:
            pc = int(dut.pc.value)
            inst = int(dut.inst.value)
            trace.append((pc, int(dut.inst_mask.value), int(dut.stack_depth.value)))
            op = M.opcode(inst)
            log["issued"].append(pc)
            log["insts"].append((pc, inst))
            if op == M.OP_BRANCH:
                # 谓词字段为 p0 的分支由 fetch 就地判定, 其余要 EXU 回送(契约)
                if M.pred_field(inst)[1] == 0:
                    log["p0_branches"].append(pc)
                else:
                    log["ext_branches"].append(pc)
            if op not in (M.OP_BRANCH, M.OP_IPDOM):
                try:
                    warp.execute(inst)
                except NotImplementedError as exc:
                    log["data_errors"].append(f"@0x{pc:x} inst=0x{inst:08x}: {exc}")
                    stop["why"] = "data-path"
                    stop["done"] = True
                    return
            if op == OP_CTRL and ((inst >> 12) & 0b111) == 0b010:      # exit
                stop["why"] = "exit"
                stop["done"] = True
                return
            if pc in stop["at"]:
                stop["why"] = f"命中 0x{pc:x}"
                stop["done"] = True
                return


def _needs_exu(dut, pc: int, inst: int) -> bool:
    """这条指令是不是"必须由 EXU 回答"的 br.simt(谓词字段不是 p0 ⇒ 会发散)。"""
    if int(dut.fet_out_valid.value) != 1 or int(dut.fet_out_ready.value) != 1:
        return False
    if M.opcode(inst) != M.OP_BRANCH:
        return False
    return M.pred_field(inst)[1] != 0


def _answer(dut, warp, log, pc: int, inst: int) -> None:
    taken = warp.taken_raw(inst)
    log["resolved"].append((pc, taken))
    dut.target_valid.value = 1
    dut.target_pc.value = M.branch_target(inst, pc)
    dut.target_taken.value = taken


async def exu_responder(dut, warp, log, mode: str = "wait", state: dict | None = None) -> None:
    """假 EXU。三种回答方式, 对应 fetch.sv 头注释允许的三种用法:

    ``wait``  —— 看到 br_pending 再回答(最省事, 与译码流水线深度无关);
    ``early`` —— 分支被送去译码的**同一拍**就组合地回答(不留 S_BR_WAIT 的等待);
    ``level`` —— 回答后把 target_valid 当**电平**保持, 直到看到 br_resolved 回执才撤;
    ``stuck`` —— 等 br_pending 之后回答, 然后**永不撤**(一个不守规矩的 EXU):
                 用来验证"错误只报一拍"不依赖 EXU 是否守规矩。

    都在**周期起点**驱动并保持整拍: 一拍之内发生的事(错误脉冲、就地判定)要能被
    同拍采样的监视任务看到, 不能让两个 task 去抢同一个 delta 的先后。
    """
    while True:
        await RisingEdge(dut.clk)
        await Timer(1, unit="ns")           # 等寄存器/组合输出稳定
        pc, inst = int(dut.pc.value), int(dut.inst.value)
        if mode == "wait":
            if int(dut.br_pending.value) == 1:
                _answer(dut, warp, log, pc, inst)
            else:
                dut.target_valid.value = 0
        elif mode == "stuck":
            if int(dut.target_valid.value) == 0 and int(dut.br_pending.value) == 1:
                _answer(dut, warp, log, pc, inst)      # 拉起之后再不管
        else:
            # 电平: 收到回执才撤(early 模式无条件在下一拍撤; level 模式等回执)
            if mode == "level" and state is not None and state["ack"]:
                dut.target_valid.value = 0
                state["ack"] = False
            elif mode == "early":
                dut.target_valid.value = 0
            if int(dut.target_valid.value) == 0 and _needs_exu(dut, pc, inst):
                _answer(dut, warp, log, pc, inst)


async def ack_monitor(dut, state: dict) -> None:
    """把 br_resolved 当"边沿事件"锁存: 它和 target_valid 同拍, EXU 要锁一拍再撤。"""
    while True:
        await FallingEdge(dut.clk)
        if int(dut.br_resolved.value) == 1:
            state["ack"] = True


async def pulse_monitor(dut, log) -> None:
    """记录控制/错误脉冲: 什么时候真的压栈 / 报错 / 就地判定, 各高了几拍。

    ``log[name]`` = 上升沿列表, ``log[name + "_cycles"]`` = 高电平的拍数(错误必须是
    1 拍; 早期版本遇到"电平式回送的 EXU"会一直高, 靠这个数出来)。
    """
    names = ("br_pending", "br_local", "br_resolved", "err_no_recov", "err_simt_ovf",
             "err_imem")
    prev = {name: 0 for name in names}
    for name in names:
        log[name + "_cycles"] = 0
    while True:
        await FallingEdge(dut.clk)
        for name in names:
            now = int(getattr(dut, name).value)
            if now:
                log[name + "_cycles"] += 1
                if not prev[name]:
                    log[name].append((int(dut.pc.value), now))
            prev[name] = now


# --------------------------------------------------------------------------
# 跑一个内核
# --------------------------------------------------------------------------
async def ready_driver(dut, rng: random.Random, low_prob: float) -> None:
    """随机按住下游 ready: 验证"被背压时 PC/掩码/栈不动"在控制流指令上也成立。"""
    while True:
        await RisingEdge(dut.clk)
        dut.fet_out_ready.value = 0 if rng.random() < low_prob else 1


async def preload_imem(dut, words) -> None:
    """扮演 CP 的 LOAD_KERNEL, 把镜像逐字写进 DUT 内部那块指令 SRAM。

    只在 `fetch_imem`(fetch + 真 instr_mem)上可用; words 以**字节地址**为键。
    """
    ordered = sorted(words.items())
    for idx, (byte_addr, word) in enumerate(ordered):
        dut.load_en.value = 1
        dut.load_addr.value = byte_addr >> 2
        dut.load_data.value = word
        dut.load_last.value = 1 if idx == len(ordered) - 1 else 0
        await RisingEdge(dut.clk)
    dut.load_en.value = 0
    dut.load_last.value = 0
    for _ in range(4):                       # 等 load_done 脉冲过去
        await RisingEdge(dut.clk)
    if int(dut.err_ovf.value):
        raise AssertionError("预加载越界(err_ovf): 镜像比 16KB 指令 SRAM 还大?")


async def run_kernel_rtl(dut, words, entry, *, wid=0, stop_at=(), max_cycles=MAX_CYCLES,
                         exu_mode: str = "wait", imem_delay: int = 0,
                         ready_low_prob: float | None = None, seed: int = 0,
                         halt_after: int | None = None, linger: int = 0,
                         imem: str = "fake"):
    """跑 fetch: 复位 → (可选)预加载 → launch → 放开取指授权。

    返回 (trace, log, stop, cycles)。``imem="fake"`` 用测试台的假指令 SRAM(可以
    人为加延迟), ``imem="sram"`` 用 DUT 内部那块真 instr_mem(协议同为 1 拍)。
    """
    await start_clock_once(dut)
    drive_inputs(dut, IDLE_INPUTS)                  # 复位前先把授权收掉
    await reset_dut(dut)

    trace: list[tuple[int, int, int]] = []
    log = blank_log()
    stop = {"done": False, "why": "", "at": set(stop_at)}
    warp = M.MiniWarp(wid=wid)

    if imem == "sram":
        await preload_imem(dut, words)

    ack_state = {"ack": False}
    tasks = [
        cocotb.start_soon(decode_monitor(dut, warp, trace, log, stop,
                                         stop_on_halt=halt_after is None)),
        cocotb.start_soon(exu_responder(dut, warp, log, exu_mode, ack_state)),
        cocotb.start_soon(ack_monitor(dut, ack_state)),
        cocotb.start_soon(pulse_monitor(dut, log)),
    ]
    if imem == "fake":
        tasks.append(cocotb.start_soon(imem_responder(dut, words, log, imem_delay)))
    if ready_low_prob is not None:
        tasks.append(cocotb.start_soon(ready_driver(dut, random.Random(seed),
                                                    ready_low_prob)))

    dut.launch_pc.value = entry                     # LAUNCH: 一拍脉冲
    dut.launch.value = 1
    await RisingEdge(dut.clk)
    dut.launch.value = 0
    dut.fet_in_valid.value = 1                      # 同拍放开授权(请求要在整拍内可见)

    cycles = 0
    halt_done = False
    while not stop["done"] and cycles < max_cycles:
        await RisingEdge(dut.clk)
        cycles += 1
        if halt_after is not None and not halt_done and len(trace) >= halt_after:
            # 外部停机: 记录停机时的状态, 保持若干拍, 然后放开 halt 看它是否仍然停着
            dut.halt.value = 1
            await Timer(1, unit="ns")
            obs = {"pc": int(dut.pc.value), "valid": int(dut.fet_out_valid.value),
                   "halted": int(dut.halted.value), "ready": int(dut.fet_in_ready.value),
                   "reqs": len(log["imem_reqs"]), "depth": int(dut.stack_depth.value)}
            for _ in range(4):
                await RisingEdge(dut.clk)
                cycles += 1
            obs["valid_held"] = int(dut.fet_out_valid.value)
            obs["reqs_held"] = len(log["imem_reqs"])
            dut.halt.value = 0
            for _ in range(3):
                await RisingEdge(dut.clk)
                cycles += 1
            obs["halted_after"] = int(dut.halted.value)
            obs["reqs_after"] = len(log["imem_reqs"])
            obs["pc_after"] = int(dut.pc.value)
            log["halt_obs"] = obs
            halt_done = True

    for _ in range(linger):          # 停机之后再数几拍(给脉冲宽度检查留观察窗)
        await RisingEdge(dut.clk)
        cycles += 1

    for task in tasks:
        task.cancel()
    dut.fet_in_valid.value = 0
    dut.fet_out_ready.value = 1
    await Timer(1, unit="ns")
    return trace, log, stop, cycles


def compare_trace(sb: Scoreboard, trace, model_trace: M.Trace, tag: str) -> None:
    """逐条比较 (pc, inst_mask, 栈深)。"""
    sb.check_int(len(trace), len(model_trace.steps), f"{tag}: 执行条数")
    for i, ((pc, mask, depth), (epc, emask, edepth)) in enumerate(
            zip(trace, model_trace.steps)):
        if pc != epc:
            sb.errors.append(f"{tag}: 第 {i} 条的 pc 是 0x{pc:x}, 期望 0x{epc:x}")
            break
        sb.checks += 1
        if mask != emask:
            sb.errors.append(f"{tag}: 第 {i} 条(0x{pc:x})的执行掩码是 0x{mask:02x}, "
                             f"期望 0x{emask:02x}")
        if depth != edepth:
            sb.errors.append(f"{tag}: 第 {i} 条(0x{pc:x})的栈深是 {depth}, 期望 {edepth}")


# --------------------------------------------------------------------------
# 1. 顺序取指 / 握手
# --------------------------------------------------------------------------
@cocotb.test()
async def test_sequential_fetch(dut):
    """普通指令: PC 每条 +4、掩码不变、栈不动; 取指请求与送出的 PC 一一对应。"""
    sb = Scoreboard("sequential")
    n = 6
    words = {0x20 + 4 * i: 0x00100093 for i in range(n)}        # addi r1, r0, 1
    words[0x20 + 4 * n] = 0x0000202B                            # exit
    trace, log, stop, cycles = await run_kernel_rtl(dut, words, 0x20)

    sb.check_int(stop["why"], "exit", "结束原因")
    sb.check_equal(log["data_errors"], [], "数据通路不应报错")
    compare_trace(sb, trace, M.run_kernel(words, 0x20), "顺序取指")
    sb.check_equal(log["imem_reqs"][:n + 1], [0x20 + 4 * i for i in range(n + 1)],
                   "imem 请求序列")
    sb.check_equal(log["resolved"], [], "普通指令不该走 EXU 回送")
    sb.check_equal(log["err_no_recov"], [], "不应有 ERR_NO_RECOV")
    dut._log.info("顺序取指: %d 条指令, %d 周期(每级 3 拍: 请求/回读/送出)",
                  len(trace), cycles)
    sb.report()


@cocotb.test()
async def test_ready_backpressure(dut):
    """下游不 ready: pc/inst/inst_mask 原地保持, 且不再发新的 imem 请求。"""
    sb = Scoreboard("backpressure")
    await start_clock_once(dut)
    drive_inputs(dut, IDLE_INPUTS)
    await reset_dut(dut)

    words = {0x20: 0x00100093, 0x24: 0x00100093, 0x28: 0x0000202B}
    log = blank_log()
    trace: list = []
    stop = {"done": False, "why": "", "at": set()}
    warp = M.MiniWarp()
    tasks = [
        cocotb.start_soon(imem_responder(dut, words, log)),
        cocotb.start_soon(decode_monitor(dut, warp, trace, log, stop)),
        cocotb.start_soon(pulse_monitor(dut, log)),
    ]

    dut.launch_pc.value = 0x20
    dut.launch.value = 1
    await RisingEdge(dut.clk)
    dut.launch.value = 0
    dut.fet_in_valid.value = 1

    for _ in range(20):                     # 等到"某一拍的中点"指令正被送出
        await FallingEdge(dut.clk)
        if int(dut.fet_out_valid.value) == 1:
            break
    sb.check_int(int(dut.fet_out_valid.value), 1, "第一条指令已送出")

    dut.fet_out_ready.value = 0             # 同一拍内按住下游 ⇒ 这一拍不握手
    pc_held = int(dut.pc.value)
    inst_held = int(dut.inst.value)
    mask_held = int(dut.inst_mask.value)
    reqs_before = len(log["imem_reqs"])
    for _ in range(5):
        await RisingEdge(dut.clk)
        await Timer(1, unit="ns")
        sb.check_int(int(dut.fet_out_valid.value), 1, "按住期间 valid 应保持")
        sb.check_int(int(dut.pc.value), pc_held, "按住期间 pc 应保持")
        sb.check_int(int(dut.inst.value), inst_held, "按住期间 inst 应保持")
        sb.check_int(int(dut.inst_mask.value), mask_held, "按住期间 inst_mask 应保持")
        sb.check_int(int(dut.imem_req_valid.value), 0, "按住期间不应再发 imem 请求")
    sb.check_int(len(log["imem_reqs"]), reqs_before, "按住期间 imem 请求数不变")

    dut.fet_out_ready.value = 1             # 放开后应继续
    await Timer(1, unit="ns")
    sb.check_int(int(dut.fet_out_valid.value), 1, "放开后 valid 仍为 1")
    await RisingEdge(dut.clk)
    await Timer(1, unit="ns")
    sb.check_int(int(dut.pc.value) != pc_held, True, "放开后 pc 继续前进")

    dut.fet_in_valid.value = 0
    for task in tasks:
        task.cancel()
    sb.report()


# --------------------------------------------------------------------------
# 2. 真实内核: PC 序列 / 掩码 / 栈深 逐条对齐
# --------------------------------------------------------------------------
@cocotb.test()
async def test_nested7_against_models(dut):
    """nested7.S(7 层嵌套发散): 与两份独立模型逐条对齐 + 分支路径的分工。"""
    sb = Scoreboard("nested7")
    words, entry, _size = M.parse_image(assemble("nested7.S"))
    expected = M.run_kernel(words, entry)
    trace, log, stop, cycles = await run_kernel_rtl(dut, words, entry)

    sb.check_int(stop["why"], "exit", "结束原因")
    sb.check_equal(log["data_errors"], [], "数据通路不应报错")
    compare_trace(sb, trace, expected, "nested7")
    sb.check_equal(expected.errors, [], "模型侧不应有错误")
    sb.check_int(int(dut.err_no_recov.value), 0, "不应有 ERR_NO_RECOV")
    sb.check_int(int(dut.err_simt_ovf.value), 0, "不应有 SIMT_OVF")

    # 与仓库里已有的第二份独立模型(§2.3.3)对照
    sb.check_equal([pc for pc, _, _ in trace], stack_model_check_pcs("nested7.S"),
                   "与 stack_model_check.py 的 PC 序列")

    # 分工: @p0 分支(j)前端就地判定; 带谓词的分支才走 EXU 回送
    sb.check_int(len(log["resolved"]), len(log["ext_branches"]),
                 "EXU 只回答谓词字段非 p0 的分支")
    sb.check_int(len(log["br_local"]), len(log["p0_branches"]),
                 "p0 分支由 br_local 就地判定")
    sb.check_equal(all(taken not in (0, FULL_MASK) for _pc, taken in log["resolved"]), True,
                   "nested7 的带谓词分支都是部分 lane 满足(都会发散)")
    sb.check_int(max(d for _, _, d in trace), 7, "nested7 的栈最深 7/8")
    dut._log.info("nested7: %d 条指令 / %d 周期; 带谓词分支 %d 条(EXU 回送 %d 次), "
                  "p0 分支 %d 条(就地判定)",
                  len(trace), cycles, len(log["ext_branches"]),
                  len(log["resolved"]), len(log["p0_branches"]))
    sb.report()


@cocotb.test()
async def test_err_no_recov_kernel(dut):
    """err_recov.S 抹掉 ipdom ⇒ 发散时报 ERR_NO_RECOV、停机并记下 FAULT_PC。"""
    sb = Scoreboard("err_recov")
    raw = assemble("err_recov.S")
    words, entry, _size = M.parse_image(raw)
    # 与 sim/simulator/tests/run_tests.py 同一手法: 把第一条 ipdom 换成 addi r0,r0,0
    idx = next(i for i in range(len(words))
               if entry + 4 * i in words and M.opcode(words[entry + 4 * i]) == M.OP_IPDOM)
    words = M.parse_image(M.patch_word(raw, idx, 0x00000013))[0]
    expected = M.run_kernel(words, entry)
    sb.check_equal([e[0] for e in expected.errors], ["ERR_NO_RECOV"], "模型: 期望的报错")
    branch_pc = expected.errors[0][1]

    trace, log, stop, _cycles = await run_kernel_rtl(dut, words, entry)
    sb.check_int(stop["why"], "halted", "结束原因(应停机)")
    sb.check_int(int(dut.halted.value), 1, "halted 应为 1")
    sb.check_int(int(dut.fault_pc.value), branch_pc, "FAULT_PC = 分支指令 PC")
    sb.check_int(len(log["err_no_recov"]), 1, "ERR_NO_RECOV 应恰好脉冲一次")
    compare_trace(sb, trace, expected, "err_recov")

    reqs = len(log["imem_reqs"])                # 停机后不应再取指
    for _ in range(5):
        await RisingEdge(dut.clk)
    sb.check_int(len(log["imem_reqs"]), reqs, "停机后不应再取指")
    sb.report()


@cocotb.test()
async def test_err_bar_div_kernel(dut):
    """err_bar_div.S(发散路径里的 bar.sync): fetch 暴露的状态足以判定 BAR_DIVERGENT。

    bar.sync 的检查是 SM 的事(``inst_mask != 全 1 || 栈非空`` ⇒ BAR_DIVERGENT,
    ISA §2.4/§2.6); 这里验证 fetch **确实把这两个状态递到了模块边界** —— 在模型
    报 BAR_DIVERGENT 的那条指令上, DUT 的掩码/栈深必须已经能看出来。
    """
    sb = Scoreboard("err_bar_div")
    words, entry, _size = M.parse_image(assemble("err_bar_div.S"))
    expected = M.run_kernel(words, entry)
    sb.check_equal([e[0] for e in expected.errors], ["BAR_DIVERGENT"],
                   "模型: 期望 BAR_DIVERGENT")
    bad_pc = expected.errors[0][1]

    trace, log, stop, _cycles = await run_kernel_rtl(dut, words, entry, stop_at=(bad_pc,))
    sb.check_int(stop["why"], f"命中 0x{bad_pc:x}", "结束原因")
    sb.check_equal(log["data_errors"], [], "数据通路不应报错")
    sb.check_equal([pc for pc, _, _ in trace], expected.pcs, "BAR_DIVERGENT 之前的 PC 序列")
    pc, mask, depth = trace[-1]
    sb.check_int(pc, bad_pc, "停在 bar.sync 上")
    sb.check_equal(mask != FULL_MASK, True,
                   f"bar.sync 处掩码应已发散(0x{mask:02x}, 栈深 {depth})")
    sb.report()


# --------------------------------------------------------------------------
# 3. PC 合法性(ISA §2.6 的错误路径)
# --------------------------------------------------------------------------
@cocotb.test()
async def test_bad_pc(dut):
    """PC 未 4B 对齐 / 超出指令 SRAM ⇒ err_imem 脉冲 + 停机 + FAULT_PC。"""
    sb = Scoreboard("bad_pc")
    await start_clock_once(dut)

    for bad in (0x22, 0x4000):
        drive_inputs(dut, IDLE_INPUTS)
        await reset_dut(dut)
        dut.launch_pc.value = bad
        dut.launch.value = 1
        await RisingEdge(dut.clk)
        dut.launch.value = 0
        dut.fet_in_valid.value = 1              # 授权到手才发现 PC 非法

        seen = 0
        for _ in range(8):
            await FallingEdge(dut.clk)
            seen |= int(dut.err_imem.value)
        sb.check_int(seen, 1, f"pc=0x{bad:x}: err_imem 应报一次")
        sb.check_int(int(dut.halted.value), 1, f"pc=0x{bad:x}: 应停机")
        sb.check_int(int(dut.fault_pc.value), bad, f"pc=0x{bad:x}: FAULT_PC")
        sb.check_int(int(dut.imem_req_valid.value), 0, f"pc=0x{bad:x}: 不应发取指请求")
        sb.check_int(int(dut.fet_out_valid.value), 0, f"pc=0x{bad:x}: 不应送出指令")
    dut.fet_in_valid.value = 0
    sb.report()


# --------------------------------------------------------------------------
# 辅助
# --------------------------------------------------------------------------
def stack_model_check_pcs(name: str) -> list[int]:
    """调用仓库里已有的第二份独立模型(``sim/simulator/tests/stack_model_check.py``)。"""
    tests_dir = str(REPO / "sim" / "simulator" / "tests")
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)
    import stack_model_check as smc                     # noqa: PLC0415

    return smc.model_run(smc.disassemble(str(KERNEL_DIR / name)))


# --------------------------------------------------------------------------
# 4. 随机内核: 程序级差分测试
# --------------------------------------------------------------------------
CONDS = ("lt", "le", "gt", "ge", "eq", "ne")


def gen_block(rng: random.Random, tag: str, depth: int, budget: list[int],
              lines: list[str]) -> None:
    """递归生成一个 if/else 块(可嵌套), 只会前向跳转 ⇒ 程序必然终止。

    形状完全对得上 ISA.md §2.3.4 的编译约定::

        setp.<cond> p1, r1, r2       ; 谓词(决定这次是不是发散)
        ipdom L<tag>e                ; 八成概率(否则故意漏掉 ⇒ ERR_NO_RECOV)
        @p1   br.simt L<tag>x, L<tag>e
        <then 路径: 可嵌套一个块>     ; 未跳转 lane
        j     L<tag>e                ; 必须: 让 then 路径抵达收敛点
        L<tag>x:
        <else 路径: 可嵌套一个块>     ; 跳转 lane
        L<tag>e:                      ; 两路径在此会合, 完整 mask 执行一次
    """
    end, els = f"L{tag}e", f"L{tag}x"
    lines.append(f"        setp.{rng.choice(CONDS)} p1, r1, r2")
    if rng.random() < 0.8:
        lines.append(f"        ipdom {end}")
    lines.append(f"        @p1   br.simt {els}, {end}")

    if depth < 2 and budget[0] > 0:                 # then 路径里嵌一层
        budget[0] -= 1
        gen_block(rng, tag + "t", depth + 1, budget, lines)
    lines.append(f"        addi  r2, r2, {rng.randint(-1, 2)}")
    lines.append(f"        j     {end}")

    lines.append(f"{els}:")
    if depth < 2 and budget[0] > 0:                 # else 路径里嵌一层
        budget[0] -= 1
        gen_block(rng, tag + "f", depth + 1, budget, lines)
    lines.append(f"        addi  r2, r2, {rng.randint(-1, 2)}")

    lines.append(f"{end}:")
    # 每块重新给 r2 装一个小常量: 否则 r2 会漂出 lane 范围(0..7)之外, 分支就全退化成
    # 均匀分支, 随机覆盖也就没有意义了
    lines.append(f"        addi  r2, r0, {rng.randint(-2, 9)}")


def random_kernel(rng: random.Random, blocks: int) -> str:
    """生成一个随机小内核(嵌套 if/else + 随机谓词 + 随机收敛点)。"""
    lines = ["        tmov  r1, tid",
             f"        addi  r2, r0, {rng.randint(-2, 9)}"]
    lines.append("        setp.lt p1, r1, r2")
    lines.append("        ipdom Ltop")
    lines.append("        @p1   br.simt Lbody, Ltop")
    lines.append("        j     Ltop")
    lines.append("Lbody:")
    budget = [blocks]
    for i in range(blocks):
        gen_block(rng, f"b{i}", 0, budget, lines)
    lines.append("Ltop:")
    lines.append("        exit")
    return "\n".join(lines) + "\n"


def no_recov_kernel() -> str:
    """必然发散(lane 0..3 满足)的内核; 汇编器会自动补一条 ipdom, 调用方再把它抹掉。"""
    return "\n".join([
        "        tmov  r1, tid",
        "        addi  r2, r0, 4              ; lane 0..3 为真 ⇒ 一定发散(不是均匀分支)",
        "        setp.lt p1, r1, r2",
        "        @p1   br.simt Lelse, Lend",
        "        j     Lend",
        "Lelse:",
        "        addi  r2, r2, 1",
        "Lend:",
        "        exit",
    ]) + "\n"


def assemble_source(name: str, text: str) -> bytes:
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    src = BUILD_DIR / f"{name}.S"
    src.write_text(text)
    img = BUILD_DIR / f"{name}.bin"
    proc = subprocess.run([sys.executable, str(ASM), str(src), "--format", "image",
                           "-o", str(img)], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"汇编随机内核失败: {proc.stdout}{proc.stderr}\n{text}")
    return img.read_bytes()


@cocotb.test()
async def test_random_kernels_vs_model(dut):
    """随机小内核(前向跳转 + 随机谓词/收敛点)与 §2.3.3 模型逐条对拍。

    手写内核只覆盖几种控制流形状; 这里用生成器撒 16 个程序, 每个都逐条比较
    PC / lane 掩码 / 栈深, 并比较结束原因(exit 还是某个架构错误)。
    """
    sb = Scoreboard("random_kernels")
    rng = random.Random(BASE_SEED + 0xBEEF)
    n_programs = 16
    total_steps = 0
    divergent = 0
    deep = 0
    errors_seen: dict[str, int] = {}

    for p in range(n_programs + 1):
        forced_error = p == n_programs
        if forced_error:
            raw = assemble_source(f"rand{p}", no_recov_kernel())
            words, entry, _size = M.parse_image(raw)
            # 抹掉汇编器自动补的那条 ipdom ⇒ 发散时必须是 ERR_NO_RECOV(ISA §5.7/§2.6)
            idx = next(i for i in range(len(words))
                       if entry + 4 * i in words
                       and M.opcode(words[entry + 4 * i]) == M.OP_IPDOM)
            raw = M.patch_word(raw, idx, 0x00000013)      # addi r0, r0, 0
        else:
            raw = assemble_source(f"rand{p}", random_kernel(rng, blocks=rng.randint(2, 6)))
        words, entry, _size = M.parse_image(raw)
        expected = M.run_kernel(words, entry)
        trace, log, stop, cycles = await run_kernel_rtl(dut, words, entry)

        tag = f"rand{p}"
        sb.check_equal(log["data_errors"], [], f"{tag}: 数据通路")
        compare_trace(sb, trace, expected, tag)
        want = "exit" if expected.finished else (expected.errors[0][0] if expected.errors
                                                else "?")
        got = stop["why"]
        if want == "exit":
            sb.check_int(got, "exit", f"{tag}: 结束原因")
        else:
            sb.check_int(got, "halted", f"{tag}: 结束原因(期望 {want} 停机)")
            sb.check_int(int(dut.halted.value), 1, f"{tag}: halted")
            sb.check_int(int(dut.fault_pc.value), expected.errors[0][1],
                         f"{tag}: FAULT_PC({want})")
            errors_seen[want] = errors_seen.get(want, 0) + 1
        total_steps += len(trace)
        max_depth = max((d for _, _, d in trace), default=0)
        divergent += int(max_depth > 0)
        deep += int(max_depth > 1)
        dut._log.info("%s: %d 条指令 / %d 周期, 栈最深 %d, 结束=%s",
                      tag, len(trace), cycles, max_depth, got)

    dut._log.info("随机内核: %d 个程序共 %d 条指令; %d 个出现发散(%d 个压到 2 层以上); "
                  "命中的错误类型 %s", n_programs, total_steps, divergent, deep, errors_seen)
    sb.check_equal(total_steps > 100, True, "随机内核应真的跑起来")
    sb.check_equal(divergent > 0, True, "随机内核应覆盖发散")
    sb.check_equal(deep > 0, True, "随机内核应覆盖嵌套发散(栈深 > 1)")
    sb.check_equal(errors_seen.get("ERR_NO_RECOV", 0), 1, "随机内核应覆盖 ERR_NO_RECOV")
    sb.report()


# --------------------------------------------------------------------------
# 5. 与 EXU 的三种握手方式 / 停机 / 时序扰动(评审提出的风险点)
# --------------------------------------------------------------------------
@cocotb.test()
async def test_exu_same_cycle_answer(dut):
    """EXU 在"分支被送去译码的同一拍"就组合地回答 —— 不能因此挂死。

    fetch.sv 头注释说 EXU 不必等 fetch 先动; 这个用例就是那句话的可执行版本:
    同一个内核(``nested7.S``)在三种回答方式下都必须得到同一条指令序列, 且
    ``early`` 方式下分支根本不需要进 S_BR_WAIT(``br_pending`` 从头到尾为 0)。
    """
    sb = Scoreboard("exu_early")
    words, entry, _size = M.parse_image(assemble("nested7.S"))
    expected = M.run_kernel(words, entry)

    trace, log, stop, cycles = await run_kernel_rtl(dut, words, entry, exu_mode="early")
    sb.check_int(stop["why"], "exit", "结束原因")
    sb.check_equal(log["data_errors"], [], "数据通路不应报错")
    compare_trace(sb, trace, expected, "nested7/early")
    sb.check_equal(log["br_pending"], [], "同拍回答时不应进 S_BR_WAIT")
    sb.check_equal(len(log["resolved"]), len(log["ext_branches"]), "EXU 回答次数")
    dut._log.info("early: %d 条指令 / %d 周期, br_pending 脉冲 %d 次",
                  len(trace), cycles, len(log["br_pending"]))

    # 同一件事在"电平保持 + br_resolved 回执清掉"的用法下也必须一致:
    # 回执让 EXU 能把 valid 撤干净, 下一条分支不会被上一条的判定解析。
    trace2, log2, stop2, _cycles2 = await run_kernel_rtl(dut, words, entry,
                                                         exu_mode="level")
    sb.check_int(stop2["why"], "exit", "level: 结束原因")
    compare_trace(sb, trace2, expected, "nested7/level")
    sb.check_equal([pc for pc, _ in log2["br_resolved"]],
                   [pc for pc, _ in log2["resolved"]],
                   "每个回答都应配一个回执(按 PC 对齐)")
    sb.report()


@cocotb.test()
async def test_error_pulse_is_single_cycle(dut):
    """电平式 target_valid(直到 br_resolved 才撤)也不能把错误拖成持续高电平。

    ``err_no_recov`` 按 fetch.sv / simt_stack.sv 的说明必须是**单周期事件脉冲**
    (SM 靠它锁存首个错误码; 拉成电平会让"计数"式实现重复计数)。
    """
    sb = Scoreboard("err_pulse")
    raw = assemble("err_recov.S")
    words, entry, _size = M.parse_image(raw)
    idx = next(i for i in range(len(words))
               if entry + 4 * i in words and M.opcode(words[entry + 4 * i]) == M.OP_IPDOM)
    words = M.parse_image(M.patch_word(raw, idx, 0x00000013))[0]
    expected = M.run_kernel(words, entry)
    branch_pc = expected.errors[0][1]

    # linger: 停机后继续数拍, 让"错误只报一拍"这件事真的被观察到
    trace, log, stop, _cycles = await run_kernel_rtl(dut, words, entry, exu_mode="stuck",
                                                     linger=6)
    sb.check_int(stop["why"], "halted", "结束原因(应停机)")
    sb.check_int(int(dut.halted.value), 1, "halted 应为 1")
    sb.check_int(int(dut.fault_pc.value), branch_pc, "FAULT_PC = 分支指令 PC")
    sb.check_int(log["err_no_recov_cycles"], 1, "ERR_NO_RECOV 只能高 1 拍")
    sb.check_int(len(log["err_no_recov"]), 1, "ERR_NO_RECOV 只该报一次")
    sb.check_equal(len(log["br_resolved"]) > 0, True, "应给出 br_resolved 回执")
    compare_trace(sb, trace, expected, "err_recov/stuck")
    sb.report()


@cocotb.test()
async def test_halt_is_sticky(dut):
    """外部 halt: 立即停止取指, 并且**锁存**(半途放开也不恢复, ISA §2.6)。

    半途放开而不复位会让 S_RESP 里在途的请求永远等不到第二次响应, 所以停机只能由
    复位解除; 同时停机不许改写 FSM 状态 —— 已被译码接收的指令不能被再送一次。
    """
    sb = Scoreboard("halt")
    words, entry, _size = M.parse_image(assemble("nested7.S"))
    expected = M.run_kernel(words, entry)

    trace, log, stop, _cycles = await run_kernel_rtl(dut, words, entry, halt_after=5)
    obs = log["halt_obs"]
    sb.check_int(obs["valid"], 0, "停机当拍不应再送指令")
    sb.check_int(obs["halted"], 1, "halt 当拍 halted 就应为 1")
    sb.check_int(obs["ready"], 0, "停机时不应再接受取指授权")
    sb.check_int(obs["valid_held"], 0, "停机期间不应送指令")
    sb.check_int(obs["reqs_held"], obs["reqs"], "停机期间不应发取指请求")
    sb.check_int(obs["halted_after"], 1, "放开 halt 之后仍然停机(锁存)")
    sb.check_int(obs["reqs_after"], obs["reqs"], "放开 halt 之后也不应继续取指")
    sb.check_int(obs["pc_after"], obs["pc"], "停机期间 PC 冻结")
    # 停机前执行过的指令与模型一致(停机不改变已发生的控制流)
    compare_trace(sb, trace, M.Trace(steps=expected.steps[:len(trace)]), "halt 之前")
    sb.check_equal(len(trace) >= 5, True, "停机前应已执行若干条指令")
    dut._log.info("halt: 停在 pc=0x%x, 已执行 %d 条, 取指请求 %d 次",
                  obs["pc"], len(trace), obs["reqs"])
    sb.report()


@cocotb.test()
async def test_timing_perturbations(dut):
    """随机背压 + 指令 SRAM 2 拍延迟: 指令序列仍必须与模型逐条一致。

    取指协议只保证"一问一答", 不保证下游 ready 常高、也不保证响应恰好 1 拍到;
    这两个扰动一起上, 是 fetch FSM 与 simt_stack 之间"什么时候推进"的最强检查。
    """
    sb = Scoreboard("perturb")
    words, entry, _size = M.parse_image(assemble("nested7.S"))
    expected = M.run_kernel(words, entry)

    for seed, low_prob, delay in ((1, 0.4, 2), (2, 0.7, 0), (3, 0.3, 3)):
        trace, log, stop, cycles = await run_kernel_rtl(
            dut, words, entry, ready_low_prob=low_prob, imem_delay=delay, seed=seed)
        tag = f"seed{seed}(ready低{low_prob}, imem+{delay})"
        sb.check_int(stop["why"], "exit", f"{tag}: 结束原因")
        sb.check_equal(log["data_errors"], [], f"{tag}: 数据通路")
        compare_trace(sb, trace, expected, tag)
        sb.check_int(int(dut.err_no_recov.value), 0, f"{tag}: 不应有错误")
        dut._log.info("%s: %d 条指令 / %d 周期", tag, len(trace), cycles)
    sb.report()


UNIFORM_KERNEL = """        tmov  r1, tid
        addi  r2, r0, 100
        setp.lt p1, r1, r2            ; p1 全真(100 > 7)
        ipdom La
        @!p0  br.simt Lbad, La         ; @!p0 恒假 ⇒ 均匀不跳(fetch 就地判定)
        addi  r2, r0, 1
La:     setp.lt p1, r1, r2            ; r2=1 ⇒ 只有 lane 0 满足
        ipdom Lb
        @p1   br.simt Lelse, Lb        ; 发散点
        j     Lb
Lelse:  addi  r2, r2, 1
Lb:     addi  r2, r0, 100
        setp.lt p1, r1, r2            ; p1 又全真
        ipdom Lc
        @!p1  br.simt Lbad, Lc         ; !p1 全假 ⇒ 均匀不跳(EXU 判定的均匀分支)
        addi  r2, r0, 100              ; 保持全真, 让下一句成为"均匀跳转"
Lc:     setp.lt p1, r1, r2
        ipdom Ld
        @p1   br.simt Lbad2, Ld        ; 全真 ⇒ 均匀跳转(EXU 判定的均匀分支)
        addi  r2, r0, 77               ; 不该执行
        j     Ld
Lbad:   addi  r2, r0, 55
Lbad2:  addi  r2, r0, 66
Ld:     exit
"""


@cocotb.test()
async def test_uniform_branch_encodings(dut):
    """均匀分支的四种走法都要对: @!p0 就地不跳、谓词全假不跳、谓词全真跳、部分满足发散。

    均匀分支**不该压栈**(ISA §2.3.1), 所以除去那一次真发散, 栈深应当一直是 0;
    同时这段内核让 EXU 也回答"均匀"的分支(taken 全 1), 覆盖前端不去猜的另一种情形。
    """
    sb = Scoreboard("uniform")
    raw = assemble_source("uniform_branches", UNIFORM_KERNEL)
    words, entry, _size = M.parse_image(raw)
    expected = M.run_kernel(words, entry)

    trace, log, stop, _cycles = await run_kernel_rtl(dut, words, entry)
    sb.check_int(stop["why"], "exit", "结束原因")
    sb.check_equal(log["data_errors"], [], "数据通路")
    compare_trace(sb, trace, expected, "uniform")

    # 真发散只有 @p1 br.simt 那一条; 均匀分支一律不压栈
    depths = [d for _, _, d in trace]
    sb.check_int(max(depths), 1, "只有那一次发散压栈")
    sb.check_int(depths.count(1) >= 1, True, "发散那一段栈深为 1")
    # EXU 回答里既有"全 lane 满足"(均匀跳转)也有"无 lane 满足"(均匀不跳)
    taken_set = {taken for _pc, taken in log["resolved"]}
    sb.check_equal(FULL_MASK in taken_set, True, "EXU 应回过一次均匀跳转(taken 全 1)")
    sb.check_equal(0 in taken_set, True, "EXU 应回过一次均匀不跳(taken=0)")
    # 就地判定(@p0/@!p0)与"交给 EXU"两类分支在 PC 上互不重叠
    local_pcs = {pc for pc, _ in log["br_local"]}
    wait_pcs = {pc for pc, _ in log["br_pending"]}
    sb.check_equal(local_pcs & wait_pcs, set(), "就地判定的分支不该再等 EXU")
    sb.check_equal(len(local_pcs) > 0, True, "@!p0 分支应由前端就地判定")
    dut._log.info("均匀分支: %d 条指令, EXU 回答 %s", len(trace), sorted(taken_set))
    sb.report()
