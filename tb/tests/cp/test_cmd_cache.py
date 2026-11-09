"""`hw/rtl/cache/cmd_cache.sv` 的用例:命令行的缓存命中、缺失回填、预取、失效。

它替代了 CP 原来"每取一条命令就沿总线读一次 64B 行"的做法(见 `cp.sv` 的
`ST_MEM/OP_RD_LINE`)。这里只校验缓存本身的三条契约:

1. 同一行第二次要就是命中 —— 不再发总线读;
2. 缺失时把行装进来,并且**出错的行不装**(否则总线错误会被变成数据错误);
3. `invalidate` 之后必须重新读(命令环的字节在 host 敲 DOORBELL 之后才可能变)。

另外单独钉住"内存口被 CP 的单字读写占着时,缓存不抢口"这条握手 —— 少了它,
CP 发起单字读写、缓存正在回填,两边会互锁。
"""

from __future__ import annotations

import cocotb
from cocotb.triggers import FallingEdge, RisingEdge

from harness.lib.tb import Scoreboard, reset_dut
from tests.core.dut_drivers import start_clock_once

BLOCK_WORDS = 16
LINE_BYTES = 64
LINE_MASK = (1 << 26) - 1


def _pack(words) -> int:
    v = 0
    for i, w in enumerate(words):
        v |= (w & 0xFFFF_FFFF) << (32 * i)
    return v


def _unpack(v: int) -> list[int]:
    return [(v >> (32 * i)) & 0xFFFF_FFFF for i in range(BLOCK_WORDS)]


class LineMem:
    """行级内存模型:回应 `m_rd_req`(ack 一拍 → rsp 一拍),并记录读过哪些行。"""

    def __init__(self, latency: int = 2):
        self.lines: dict[int, list[int]] = {}
        self.latency = latency
        self.reads: list[int] = []
        self.err_lines: set[int] = set()

    def set_line(self, line: int, words) -> None:
        self.lines[line] = list(words)

    def words(self, line: int) -> list[int]:
        """没铺过的行按行号造一组可辨识的数据(便于"读串行"这类错误立刻暴露)。"""
        return self.lines.get(line, [(line << 8) | i for i in range(BLOCK_WORDS)])

    async def serve(self, dut, max_cycles: int = 6000):
        dut.m_rd_ack.value = 0
        dut.m_rd_vld.value = 0
        dut.m_rd_word.value = 0
        dut.m_rd_last.value = 0
        dut.m_rd_err.value = 0
        await FallingEdge(dut.clk)
        pend: list | None = None          # [行号, 还要等几拍就发第一个字]
        for _ in range(max_cycles):
            # 1) 先采样 DUT 的请求(req 是寄存器输出,半周期内稳定)
            if int(dut.m_rd_req.value) and pend is None:
                line = (int(dut.m_rd_addr.value) // LINE_BYTES) & LINE_MASK
                self.reads.append(line)
                dut.m_rd_ack.value = 1
                pend = [line, self.latency]
            else:
                dut.m_rd_ack.value = 0

            # 2) 再按上一拍定下的状态驱动本拍输出:数据按 AXI R 的节奏一拍一个字
            if pend is not None and pend[1] == 0:
                words = self.words(pend[0])
                for b in range(BLOCK_WORDS):
                    dut.m_rd_vld.value = 1
                    dut.m_rd_word.value = words[b] & 0xFFFF_FFFF
                    dut.m_rd_last.value = 1 if b == BLOCK_WORDS - 1 else 0
                    dut.m_rd_err.value = 1 if (b == BLOCK_WORDS - 1 and pend[0] in self.err_lines) else 0
                    await FallingEdge(dut.clk)
                dut.m_rd_vld.value = 0
                dut.m_rd_last.value = 0
                dut.m_rd_err.value = 0
                pend = None
                continue
            else:
                dut.m_rd_vld.value = 0
                dut.m_rd_last.value = 0
                dut.m_rd_err.value = 0
                if pend is not None:
                    pend[1] -= 1
            await FallingEdge(dut.clk)


async def setup(dut, mem: LineMem):
    await start_clock_once(dut)
    dut.req_valid.value = 0
    dut.req_line.value = 0
    dut.ring_end_line.value = 0        # 0 = 关闭顺序预取(各用例按需打开)
    dut.invalidate.value = 0
    dut.port_free.value = 1
    await reset_dut(dut)
    await FallingEdge(dut.clk)
    return cocotb.start_soon(mem.serve(dut))


async def read_line(dut, line: int, max_cycles: int = 300):
    """要一整行,返回 (数据, 错误位, 用了多少拍)。"""
    await FallingEdge(dut.clk)
    dut.req_line.value = line & LINE_MASK
    dut.req_valid.value = 1
    data = None
    err = 0
    for n in range(1, max_cycles + 1):
        await RisingEdge(dut.clk)
        if int(dut.rsp_valid.value):
            got_line = int(dut.rsp_line.value)
            assert got_line == (line & LINE_MASK), f"rsp_line 应回 {line:#x}, 实际 {got_line:#x}"
            data = _unpack(int(dut.rsp_data.value))
            err = int(dut.rsp_err.value)
            dut.req_valid.value = 0          # rsp 之后立刻撤,避免被当成新请求
            break
    else:
        raise AssertionError(f"行 {line:#x} 的读请求超时")
    await FallingEdge(dut.clk)
    return data, err, n


async def pulse(dut, sig, cycles: int = 1):
    await FallingEdge(dut.clk)
    getattr(dut, sig).value = 1
    for _ in range(cycles):
        await RisingEdge(dut.clk)
    await FallingEdge(dut.clk)
    getattr(dut, sig).value = 0


async def idle(dut, mem: LineMem, cycles: int = 40):
    """等到缓存空闲(没有在飞的回填),再返回。"""
    for _ in range(cycles):
        await RisingEdge(dut.clk)
        if not int(dut.busy.value):
            return
    raise AssertionError("缓存一直没有回到空闲")


@cocotb.test()
async def test_miss_then_hit(dut):
    """第一次缺失要读总线,第二次同一行必须命中(不再读总线)。"""
    sb = Scoreboard("cmd-cache-hit")
    mem = LineMem()
    mem.set_line(0x20, [0x1111_0000 + i for i in range(BLOCK_WORDS)])
    task = await setup(dut, mem)

    data, err, _ = await read_line(dut, 0x20)
    sb.check_int(err, 0, "缺失行的 rsp_err")
    sb.check_equal(data, mem.words(0x20), "缺失回填的数据")
    sb.check_int(len(mem.reads), 1, "缺失只应读一次总线")

    data2, err2, cycles2 = await read_line(dut, 0x20)
    sb.check_int(err2, 0, "命中行的 rsp_err")
    sb.check_equal(data2, mem.words(0x20), "命中的数据")
    sb.check_int(len(mem.reads), 1, "命中不应再读总线")
    sb.check_int(cycles2 <= 4, True, f"命中应在 4 拍内回来(实测 {cycles2} 拍)")

    task.kill()
    sb.report()


@cocotb.test()
async def test_conflicting_lines_refill(dut):
    """同组不同标签的两行会互相顶掉,读完第一行再读第二行必须各自重新回填。"""
    sb = Scoreboard("cmd-cache-conflict")
    mem = LineMem()
    mem.set_line(0x00, [0xA000 + i for i in range(BLOCK_WORDS)])
    mem.set_line(0x40, [0xB000 + i for i in range(BLOCK_WORDS)])
    task = await setup(dut, mem)

    d0, _, _ = await read_line(dut, 0x00)
    d1, _, _ = await read_line(dut, 0x40)
    d2, _, _ = await read_line(dut, 0x00)
    sb.check_equal(d0, mem.words(0x00), "第一行的数据")
    sb.check_equal(d1, mem.words(0x40), "第二行(同组)的数据")
    sb.check_equal(d2, mem.words(0x00), "第一行重新回填后的数据")
    sb.check_int(len(mem.reads), 3, "两行互相顶掉 ⇒ 一共读三次")

    task.kill()
    sb.report()


@cocotb.test()
async def test_auto_prefetch_next_line(dut):
    """缓存自己把"下一行"提前装进来:后面要它时直接命中, 不再读总线。"""
    sb = Scoreboard("cmd-cache-prefetch")
    mem = LineMem()
    mem.set_line(0x31, [0xC000 + i for i in range(BLOCK_WORDS)])
    task = await setup(dut, mem)
    dut.ring_end_line.value = 0x40        # 环一直到 0x40 行, 允许预取

    await read_line(dut, 0x30)            # 真实请求 ⇒ 顺带把 0x31 挂上预取
    for _ in range(200):
        await RisingEdge(dut.clk)
        if 0x31 in mem.reads and not int(dut.busy.value):
            break
    else:
        raise AssertionError("顺序预取没有启动")
    sb.check_equal(mem.reads, [0x30, 0x31], "真实请求 + 预取各读一次总线")

    data, err, cycles = await read_line(dut, 0x31)
    sb.check_int(err, 0, "预取行的 rsp_err")
    sb.check_equal(data, mem.words(0x31), "预取行的数据")
    sb.check_int(len(mem.reads), 2, "预取过的行不应再读总线")
    sb.check_int(cycles <= 4, True, f"预取过的行应命中(实测 {cycles} 拍)")

    task.kill()
    sb.report()


@cocotb.test()
async def test_prefetch_stops_at_ring_end(dut):
    """到了环的上界就不再预取(否则会去读环外的行, 白吃带宽)。"""
    sb = Scoreboard("cmd-cache-prefetch-end")
    mem = LineMem()
    task = await setup(dut, mem)
    dut.ring_end_line.value = 0x32        # 环的最后一行是 0x31

    await read_line(dut, 0x31)            # 它的下一行 0x32 已经在环外
    for _ in range(30):
        await RisingEdge(dut.clk)
    sb.check_equal(mem.reads, [0x31], "环外那一行不应被预取")

    task.kill()
    sb.report()


@cocotb.test()
async def test_invalidate_forces_refill(dut):
    """invalidate 之后同一行必须重新从总线读(命令环的字节可能已经变了)。"""
    sb = Scoreboard("cmd-cache-invalidate")
    mem = LineMem()
    mem.set_line(0x22, [0xD000 + i for i in range(BLOCK_WORDS)])
    task = await setup(dut, mem)

    await read_line(dut, 0x22)
    mem.set_line(0x22, [0xE000 + i for i in range(BLOCK_WORDS)])   # host 改了这一行
    await pulse(dut, "invalidate")
    data, _, _ = await read_line(dut, 0x22)
    sb.check_equal(data, mem.words(0x22), "失效后应读到新数据")
    sb.check_int(len(mem.reads), 2, "失效后必须重读一次总线")

    task.kill()
    sb.report()


@cocotb.test()
async def test_port_busy_defers_fill(dut):
    """内存口被 CP 的单字读写占着(port_free=0)时,缓存不许抢口。"""
    sb = Scoreboard("cmd-cache-portbusy")
    mem = LineMem()
    mem.set_line(0x23, [0xF000 + i for i in range(BLOCK_WORDS)])
    task = await setup(dut, mem)

    dut.port_free.value = 0
    await FallingEdge(dut.clk)
    dut.req_line.value = 0x23
    dut.req_valid.value = 1
    for _ in range(10):
        await RisingEdge(dut.clk)
    sb.check_int(len(mem.reads), 0, "port_free=0 期间不应发总线读")
    sb.check_int(int(dut.rsp_valid.value), 0, "port_free=0 期间不应回 rsp")
    sb.check_int(int(dut.busy.value), 0, "等端口时 busy 必须是 0(否则 CP 会互锁)")

    dut.port_free.value = 1
    for _ in range(200):
        await RisingEdge(dut.clk)
        if int(dut.rsp_valid.value):
            break
    else:
        raise AssertionError("放开 port_free 之后仍然没有回 rsp")
    sb.check_int(len(mem.reads), 1, "放开端口后应读一次总线")
    sb.check_equal(_unpack(int(dut.rsp_data.value)), mem.words(0x23), "回的数据")
    dut.req_valid.value = 0

    task.kill()
    sb.report()


@cocotb.test()
async def test_error_line_not_cached(dut):
    """总线读出错:错误要报给请求者,而且这条坏行不许装进缓存。"""
    sb = Scoreboard("cmd-cache-err")
    mem = LineMem()
    mem.set_line(0x24, [0x1234_0000 + i for i in range(BLOCK_WORDS)])
    mem.err_lines.add(0x24)
    task = await setup(dut, mem)

    _, err, _ = await read_line(dut, 0x24)
    sb.check_int(err, 1, "总线错误要报给请求者")

    mem.err_lines.clear()                    # 第二次总线读就正常了
    data, err2, _ = await read_line(dut, 0x24)
    sb.check_int(err2, 0, "第二次不应再报错")
    sb.check_equal(data, mem.words(0x24), "第二次要拿到真正的数据")
    sb.check_int(len(mem.reads), 2, "坏行没被装进缓存 ⇒ 第二次必须重读总线")

    task.kill()
    sb.report()
