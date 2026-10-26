"""Tests for ``hw/rtl/mem/instr_mem.sv`` —— 每 SM 16KB 的指令 SRAM(无 icache)。

两个端口各测一遍:

* **预加载口**(CP 的 ``LOAD_KERNEL``): 逐字写入 + ``load_last`` 那一拍回
  ``load_done``; 越界写 ⇒ ``err_ovf`` 粘滞、且**不写坏 SRAM**;
* **取指口**: 正好 1 拍回读延迟(``fetch_en`` 那拍给地址, 下一拍数据有效),
  与 ``core/fetch.sv`` 的 imem 协议一致 —— 这条是 fetch/集成用例的前提。

另外用真汇编器生成的镜像验证那条"整体放在 SRAM 偏移 0、PC 从 entry_off 起"的
约定(与参考模型 ``Memory::load_img_bytes`` 同一套语义)。
"""

from __future__ import annotations

import random
import subprocess
import sys
from pathlib import Path

import cocotb
from cocotb.triggers import FallingEdge, RisingEdge

from harness.lib.tb import Scoreboard, reset_dut
from tests.core.dut_drivers import start_clock_once
from tests.core.stack_model import parse_image

REPO = Path(__file__).resolve().parents[3]
ASM = REPO / "sw" / "assembly.py"
KERNEL_DIR = REPO / "sim" / "simulator" / "tests"
BUILD_DIR = REPO / "tb" / "build" / "core_tests"

BASE_SEED = 0
NUM_WORDS = 4096


async def idle_inputs(dut) -> None:
    dut.load_en.value = 0
    dut.load_addr.value = 0
    dut.load_data.value = 0
    dut.load_last.value = 0
    dut.fetch_en.value = 0
    dut.fetch_addr.value = 0


async def write_word(dut, addr: int, data: int, last: bool = False) -> bool:
    """一拍预加载写; 返回这一拍有没有看到 load_done。"""
    dut.load_en.value = 1
    dut.load_addr.value = addr
    dut.load_data.value = data
    dut.load_last.value = int(last)
    await RisingEdge(dut.clk)
    dut.load_en.value = 0
    dut.load_last.value = 0
    await FallingEdge(dut.clk)
    return bool(int(dut.load_done.value))


async def read_word(dut, addr: int) -> int:
    """按取指口读一个字: 发地址那一拍数据还是旧的, 下一拍才是这个字。"""
    dut.fetch_en.value = 1
    dut.fetch_addr.value = addr
    await RisingEdge(dut.clk)
    dut.fetch_en.value = 0
    await FallingEdge(dut.clk)
    return int(dut.fetch_data.value)


@cocotb.test()
async def test_preload_and_fetch(dut):
    """预加载 → 回读: 写进去的字必须原样读出来, load_done 只在最后一拍响一次。"""
    sb = Scoreboard("preload")
    await start_clock_once(dut)
    await idle_inputs(dut)
    await reset_dut(dut)
    await idle_inputs(dut)

    rng = random.Random(BASE_SEED + 1)
    words = {i: rng.getrandbits(32) for i in rng.sample(range(NUM_WORDS), 64)}

    done_seen = 0
    for n, (addr, data) in enumerate(sorted(words.items())):
        is_last = n == len(words) - 1
        done_seen += int(await write_word(dut, addr, data, last=is_last))
    sb.check_int(done_seen, 1, "load_done 应恰好响一次(在 load_last 那一拍)")

    for addr, data in sorted(words.items()):
        got = await read_word(dut, addr)
        sb.check(got, data, f"字地址 {addr} 回读")
    sb.report()


@cocotb.test()
async def test_read_latency_is_one_cycle(dut):
    """取指口正好 1 拍延迟(fetch.sv 的 imem 协议就是这么假设的)。

    时序(用探针实测过): 在周期 N 的中点驱动 `fetch_en`/`fetch_addr` ⇒ RTL 在 N 末尾
    采样 ⇒ 周期 N+1 的中点上 `fetch_data` 才是这个字; `fetch_en=0` 时数据保持不变。
    """
    sb = Scoreboard("latency")
    await start_clock_once(dut)
    await idle_inputs(dut)
    await reset_dut(dut)
    await idle_inputs(dut)

    await write_word(dut, 0, 0x1111_1111)
    await write_word(dut, 1, 0x2222_2222)

    # 周期 N: 读出 0 号字
    await FallingEdge(dut.clk)
    dut.fetch_en.value = 1
    dut.fetch_addr.value = 0
    sb.check(int(dut.fetch_data.value), 0x0000_0000, f"周期 N: 数据还没出来")
    # 周期 N+1: 0 号字就位; 同时把 1 号字的地址发出去
    await FallingEdge(dut.clk)
    sb.check(int(dut.fetch_data.value), 0x1111_1111, "周期 N+1: 0 号字")
    dut.fetch_addr.value = 1
    sb.check(int(dut.fetch_data.value), 0x1111_1111, "发新地址那一拍仍是旧数据")
    # 周期 N+2: 1 号字就位; 撤掉 fetch_en
    await FallingEdge(dut.clk)
    sb.check(int(dut.fetch_data.value), 0x2222_2222, "周期 N+2: 1 号字")
    dut.fetch_en.value = 0
    # 周期 N+3: fetch_en=0 ⇒ 数据不变(不是每拍乱刷)
    await FallingEdge(dut.clk)
    sb.check(int(dut.fetch_data.value), 0x2222_2222, "fetch_en=0 时数据保持")
    sb.report()


@cocotb.test()
async def test_overflow_is_reported_and_harmless(dut):
    """越界写: err_ovf 粘滞、SRAM 不被写坏; rst 才清。"""
    sb = Scoreboard("ovf")
    await start_clock_once(dut)
    await idle_inputs(dut)
    await reset_dut(dut)
    await idle_inputs(dut)

    await write_word(dut, 5, 0xDEAD_BEEF)
    await write_word(dut, NUM_WORDS, 0xFFFF_FFFF)         # 字地址 4096 = 越界
    await write_word(dut, NUM_WORDS + 100, 0xFFFF_FFFF)
    sb.check_int(int(dut.err_ovf.value), 1, "越界写应置 err_ovf")
    sb.check(int(await read_word(dut, 5)), 0xDEAD_BEEF, "越界写不该动别的字")

    # 越界之后正常写仍然生效(错误只是粘滞记录, 不锁死 SRAM)
    await write_word(dut, 6, 0x1234_5678)
    sb.check(int(await read_word(dut, 6)), 0x1234_5678, "越界之后仍可继续写")
    sb.check_int(int(dut.err_ovf.value), 1, "err_ovf 应粘滞")

    await reset_dut(dut)
    sb.check_int(int(dut.err_ovf.value), 0, "rst 清 err_ovf")
    sb.report()


@cocotb.test()
async def test_kernel_image_layout(dut):
    """真镜像: 整块放在偏移 0, PC = entry_off —— 取指口读 entry 处的字要等于代码首字。

    这条把"汇编器/参考模型/RTL"三方的镜像布局约定钉在一起(SPEC §3.2 / ISA §2.2)。
    """
    sb = Scoreboard("image")
    await start_clock_once(dut)
    await idle_inputs(dut)
    await reset_dut(dut)
    await idle_inputs(dut)

    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    img = BUILD_DIR / "instr_mem_nested7.bin"
    proc = subprocess.run([sys.executable, str(ASM), str(KERNEL_DIR / "nested7.S"),
                           "--format", "image", "-o", str(img)],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"汇编失败: {proc.stdout}{proc.stderr}")
    words, entry, size = parse_image(img.read_bytes())
    sb.check_equal(entry, 0x20, "镜像 entry_off(头之后就是代码)")

    for n, (byte_addr, word) in enumerate(sorted(words.items())):
        await write_word(dut, byte_addr >> 2, word, last=(byte_addr == max(words)))
    sb.check_int(int(dut.err_ovf.value), 0, "镜像没超过 16KB")

    sb.check(int(await read_word(dut, entry >> 2)), words[entry], "entry 处的首条指令")
    sb.check(int(await read_word(dut, (size - 4) >> 2)), words[size - 4], "最后一条指令")
    sb.check_int(size <= NUM_WORDS * 4, True, f"镜像 {size}B 放得下 16KB")
    dut._log.info("镜像 %d 字节 / entry=0x%x, 逐字写入后回读一致", size, entry)
    sb.report()
