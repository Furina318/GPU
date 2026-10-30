"""``hw/rtl/mem/wcb.sv`` 的用例 —— 写合并缓冲(MEM_LSU.md §5 D1/D2)。

期望值来自一份独立的 Python 模型(Python dict 按行合并), 而不是引用 RTL 内部信号。
覆盖: 分配/命中更新、读旁路整行命中与部分命中、读合并叠加、容量与冲刷、随机序列。
"""

from __future__ import annotations

import random

import cocotb
from cocotb.triggers import FallingEdge, RisingEdge, Timer

from harness.lib.tb import Scoreboard, reset_dut
from tests.core.dut_drivers import start_clock_once

ENTRIES = 8
WORDS = 8
LINE_BYTES = 32


def masked(values: list[int], wen: int) -> list[int]:
    """只保留 `wen` 选中的字: 其余字不在端口契约内(RTL 不保证)。"""
    return [values[w] if (wen >> w) & 1 else None for w in range(WORDS)]


class WcbModel:
    """Python 侧的写合并缓冲: line -> (ok_mask, words)。"""

    def __init__(self) -> None:
        self.items: dict[int, tuple[int, list[int]]] = {}

    def store(self, line: int, wen: int, wdata: list[int]) -> bool:
        if line in self.items:
            ok, dat = self.items[line]
        elif len(self.items) < ENTRIES:
            ok, dat = 0, [0] * WORDS
        else:
            return False                      # 满 ⇒ 不接受
        for w in range(WORDS):
            if (wen >> w) & 1:
                dat[w] = wdata[w]
                ok |= 1 << w
        self.items[line] = (ok, dat)
        return True

    def read(self, line: int, wen: int) -> tuple[bool, list[int]]:
        if line in self.items:
            ok, dat = self.items[line]
            if wen & ~ok == 0:
                return True, list(dat)
        return False, [0] * WORDS

    def merge(self, line: int, data: list[int]) -> list[int]:
        out = list(data)
        if line in self.items:
            ok, dat = self.items[line]
            for w in range(WORDS):
                if (ok >> w) & 1:
                    out[w] = dat[w]
        return out


async def idle(dut) -> None:
    dut.wr_valid.value = 0
    dut.wr_line.value = 0
    dut.wr_wen.value = 0
    for w in range(WORDS):
        dut.wr_wdata[w].value = 0
    dut.rd_line.value = 0
    dut.rd_wen.value = 0
    dut.mg_line.value = 0
    for w in range(WORDS):
        dut.mg_data_in[w].value = 0
    dut.out_ready.value = 0


async def fresh(dut) -> None:
    await start_clock_once(dut)
    await idle(dut)
    await reset_dut(dut)
    await idle(dut)


async def store(dut, line: int, wen: int, wdata: list[int], expect_ready: bool = True):
    await FallingEdge(dut.clk)
    dut.wr_valid.value = 1
    dut.wr_line.value = line
    dut.wr_wen.value = wen
    for w in range(WORDS):
        dut.wr_wdata[w].value = wdata[w]
    await RisingEdge(dut.clk)
    ready = bool(int(dut.wr_ready.value))
    if ready != expect_ready:
        raise AssertionError(f"wr_ready={ready}, 期望 {expect_ready}(line={line})")
    await FallingEdge(dut.clk)
    dut.wr_valid.value = 0
    return ready


async def read(dut, line: int, wen: int):
    await FallingEdge(dut.clk)
    dut.rd_line.value = line
    dut.rd_wen.value = wen
    await RisingEdge(dut.clk)
    await FallingEdge(dut.clk)
    hit = bool(int(dut.rd_hit.value))
    data = [int(dut.rd_data[w].value) for w in range(WORDS)]
    return hit, data


async def merge(dut, line: int, data: list[int]):
    await FallingEdge(dut.clk)
    dut.mg_line.value = line
    for w in range(WORDS):
        dut.mg_data_in[w].value = data[w]
    await RisingEdge(dut.clk)
    await FallingEdge(dut.clk)
    return [int(dut.mg_data_out[w].value) for w in range(WORDS)]


async def drain(dut, max_cycles: int = 64):
    """把缓冲里的项全部交出去(下游恒 ready), 直到缓冲空且输出口不再有效。

    ``out_valid``/``out_*`` 是组合输出: 本拍驱动之后要先让组合逻辑稳定下来
    (cocotb 写值在下一个 delta 才生效), 再在同一拍内采样; 上沿一过被接受的
    那项就被移除了, 晚一拍会漏记。
    """
    await FallingEdge(dut.clk)
    dut.out_ready.value = 1
    await Timer(1, "ns")
    written: list[tuple[int, int, list[int]]] = []
    cycles = 0
    while True:
        if int(dut.out_valid.value) and int(dut.out_ready.value):
            written.append((int(dut.out_line.value),
                            int(dut.out_wen.value),
                            [int(dut.out_wdata[w].value) for w in range(WORDS)]))
        if int(dut.empty.value) and not int(dut.out_valid.value):
            break
        await RisingEdge(dut.clk)
        cycles += 1
        if cycles > max_cycles:
            raise AssertionError("drain 超时")
        await FallingEdge(dut.clk)
        await Timer(1, "ns")
    dut.out_ready.value = 0
    await FallingEdge(dut.clk)
    return written


@cocotb.test()
async def test_write_read_bypass_merge(dut):
    sb = Scoreboard("wcb-basic")
    await fresh(dut)
    m = WcbModel()

    line = 0x10                                   # addr[31:5] = 0x10
    wen = 0b0000_0101                             # 字 0 与字 2
    wdata = [0xAAAA_0000 + w for w in range(WORDS)]
    await store(dut, line, wen, wdata)
    m.store(line, wen, wdata)

    hit, data = await read(dut, line, wen)
    want_hit, want_data = m.read(line, wen)
    sb.check_equal(hit, want_hit, "整行命中应当直接返回")
    sb.check_equal(masked(data, wen), masked(want_data, wen), "读旁路返回的数据")

    # 部分命中: 请求一个没写过的字 ⇒ 不命中
    hit, _ = await read(dut, line, 0b0100_0000)
    want_hit, _ = m.read(line, 0b0100_0000)
    sb.check_equal(hit, want_hit, "未写过的字不命中, 必须去内存取")

    # 读合并: 内存回来的行 + 本缓冲的字
    mem = [0xDEAD_0000 + w for w in range(WORDS)]
    got = await merge(dut, line, mem)
    sb.check_equal(got, m.merge(line, mem), "读合并结果")

    # 另一个行不命中
    hit, _ = await read(dut, 0x11, 0xFF)
    sb.check_equal(hit, False, "没写过的行不命中")
    sb.report()


@cocotb.test()
async def test_capacity_and_drain(dut):
    sb = Scoreboard("wcb-flush")
    await fresh(dut)
    m = WcbModel()

    wdata = [0x1234_0000 + w for w in range(WORDS)]
    for i in range(ENTRIES):
        await store(dut, 0x20 + i, 0xFF, wdata)
        m.store(0x20 + i, 0xFF, wdata)

    # 满: 第 9 个不同的行不接受(ready=0), 相同行仍然合并
    await store(dut, 0x99, 0xFF, wdata, expect_ready=False)
    await store(dut, 0x20, 0x01, wdata)
    m.store(0x20, 0x01, wdata)

    written = await drain(dut)
    want = {ln: (ok, tuple(dat[w] for w in range(WORDS) if (ok >> w) & 1))
            for ln, (ok, dat) in m.items.items()}
    got = {}
    for line, ok, dat in written:
        if line in got:
            raise AssertionError(f"行 0x{line:x} 被写出两次")
        got[line] = (ok, tuple(dat[w] for w in range(WORDS) if (ok >> w) & 1))
    sb.check_equal(got == want, True, "冲刷写出的内容是缓冲的全部有效行")

    # 冲刷之后缓冲为空、可以再接受
    sb.check_equal(bool(int(dut.empty.value)), True, "冲刷后 empty")
    await store(dut, 0x30, 0xFF, wdata)
    sb.report()


@cocotb.test()
async def test_random_sequence(dut):
    sb = Scoreboard("wcb-random")
    await fresh(dut)
    m = WcbModel()
    rng = random.Random(99)

    for step in range(400):
        op = rng.random()
        line = rng.choice([0x40, 0x41, 0x42, 0x50, 0x60])
        wen = rng.getrandbits(WORDS)
        wdata = [rng.getrandbits(32) for _ in range(WORDS)]
        if op < 0.5:
            ok = m.store(line, wen, wdata)
            await store(dut, line, wen, wdata, expect_ready=ok)
        elif op < 0.8:
            hit, data = await read(dut, line, wen)
            want_hit, want_data = m.read(line, wen)
            sb.check_equal(hit, want_hit, f"step{step} 读命中")
            if want_hit:
                sb.check_equal(masked(data, wen), masked(want_data, wen),
                               f"step{step} 读数据")
        else:
            mem = [rng.getrandbits(32) for _ in range(WORDS)]
            got = await merge(dut, line, mem)
            sb.check_equal(masked(got, m.read(line, 0xFF)[0] and 0xFF or 0xFF),
                           masked(m.merge(line, mem), 0xFF), f"step{step} 读合并")
        if step % 97 == 96:
            written = await drain(dut)
            want = {ln: (ok, tuple(dat[w] for w in range(WORDS) if (ok >> w) & 1))
                    for ln, (ok, dat) in m.items.items()}
            got = {ln: (ok, tuple(dat[w] for w in range(WORDS) if (ok >> w) & 1))
                   for ln, ok, dat in written}
            sb.check_equal(got == want, True, f"step{step} 冲刷内容")
            m.items.clear()
    sb.report()
