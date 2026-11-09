"""`hw/rtl/cache/cmd_cache.sv` 的用例:命令行的缓存命中、缺失回填、预取、失效。

它替代了 CP 原来"每取一条命令就沿总线读一次 64B 行"的做法(见 `cp.sv` 的
`ST_MEM/OP_RD_LINE`)。这里校验缓存本身的契约:

1. 同一行第二次要就是命中 —— 不再发总线读;
2. 缺失时把行装进来,并且**出错的行不装**(否则总线错误会被变成数据错误);
3. 失效按**范围**:只有 `pub_valid` 公布的 [start, end) 那几行作废,别的行还留着;
4. `pub_valid` 同时踢一脚预取:CP 还没来要,缓存就把新区域填好(含环尾折返);
5. 内存口被 CP 的单字读写占着时缓存不抢口 —— 少了它两边会互锁。
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
    dut.ring_base_line.value = 0
    dut.ring_end_line.value = 0        # 默认环是空的 ⇒ 不预取(各用例按需打开)
    dut.pub_valid.value = 0
    dut.pub_start_line.value = 0
    dut.pub_end_line.value = 0
    dut.inval_all.value = 0
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


async def publish(dut, start: int, end: int):
    """模拟 host 发布命令环的 [start, end) 行(写 TAIL / DOORBELL 时 CP 发的那个事件)。"""
    await FallingEdge(dut.clk)
    dut.pub_start_line.value = start & LINE_MASK
    dut.pub_end_line.value = end & LINE_MASK
    dut.pub_valid.value = 1
    await RisingEdge(dut.clk)
    await FallingEdge(dut.clk)
    dut.pub_valid.value = 0


async def wait_quiet(dut, mem: LineMem, cycles: int = 400):
    """等预取把活干完(缓存空闲且没有再发新的行读)。"""
    last = len(mem.reads)
    stable = 0
    for _ in range(cycles):
        await RisingEdge(dut.clk)
        if not int(dut.busy.value) and not int(dut.m_rd_req.value):
            if len(mem.reads) == last:
                stable += 1
                if stable >= 8:
                    return
            else:
                stable = 0
                last = len(mem.reads)


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
    """真实请求拿到 L 之后,缓存自己把 L+1 装进来:后面要它时直接命中。"""
    sb = Scoreboard("cmd-cache-prefetch")
    mem = LineMem()
    mem.set_line(0x31, [0xC000 + i for i in range(BLOCK_WORDS)])
    task = await setup(dut, mem)
    dut.ring_base_line.value = 0x20
    dut.ring_end_line.value = 0x40        # 环 [0x20, 0x40)

    await publish(dut, 0x30, 0x32)        # 发布 [0x30, 0x32):缓存会自己把两行填好
    await wait_quiet(dut, mem)
    sb.check_equal(sorted(mem.reads), [0x30, 0x31], "发布之后缓存自己把新区域读进来")

    data, err, cycles = await read_line(dut, 0x31)
    sb.check_int(err, 0, "预取行的 rsp_err")
    sb.check_equal(data, mem.words(0x31), "预取行的数据")
    sb.check_int(cycles <= 4, True, f"预取过的行应命中(实测 {cycles} 拍)")
    sb.check_int(len(mem.reads), 2, "已经预取过的行不应再读总线")

    task.kill()
    sb.report()


@cocotb.test()
async def test_publish_prefetch_whole_batch(dut):
    """一批命令跨 3 行:发布一次就把 3 行全填好,CP 逐行要时全部命中。"""
    sb = Scoreboard("cmd-cache-batch")
    mem = LineMem()
    task = await setup(dut, mem)
    dut.ring_base_line.value = 0x40
    dut.ring_end_line.value = 0x80

    await publish(dut, 0x40, 0x43)
    await wait_quiet(dut, mem)
    sb.check_equal(sorted(mem.reads), [0x40, 0x41, 0x42], "发布后主动预取整批 3 行")

    for ln in (0x40, 0x41, 0x42):
        data, err, cycles = await read_line(dut, ln)
        sb.check_int(err, 0, f"行 {ln:#x} 的 rsp_err")
        sb.check_equal(data, mem.words(ln), f"行 {ln:#x} 的数据")
        sb.check_int(cycles <= 4, True, f"行 {ln:#x} 应命中(实测 {cycles} 拍)")
    sb.check_int(len(mem.reads), 3, "整批都已预取 ⇒ 不应再有总线读")

    task.kill()
    sb.report()


@cocotb.test()
async def test_prefetch_wraps_at_ring_end(dut):
    """预取走到环尾的下一条要折返回环首(命令环回绕),不能停在环尾。"""
    sb = Scoreboard("cmd-cache-wrap")
    mem = LineMem()
    task = await setup(dut, mem)
    dut.ring_base_line.value = 0x40
    dut.ring_end_line.value = 0x44

    await publish(dut, 0x42, 0x41)         # 回绕发布:[0x42, 0x44) + [0x40, 0x41)
    await wait_quiet(dut, mem)
    sb.check_equal(sorted(mem.reads), [0x40, 0x42, 0x43], "回绕区域的行都要读到")

    for ln in (0x42, 0x43, 0x40):
        data, err, _ = await read_line(dut, ln)
        sb.check_int(err, 0, f"行 {ln:#x} 的 rsp_err")
        sb.check_equal(data, mem.words(ln), f"行 {ln:#x} 的数据")
    sb.check_int(len(mem.reads), 3, "回绕区域已预取 ⇒ 不应再有总线读")

    task.kill()
    sb.report()


@cocotb.test()
async def test_prefetch_stops_at_ring_end(dut):
    """发布范围到环尾就停:不会去读环外(未发布)的行。"""
    sb = Scoreboard("cmd-cache-prefetch-end")
    mem = LineMem()
    task = await setup(dut, mem)
    dut.ring_base_line.value = 0x20
    dut.ring_end_line.value = 0x32        # 环 [0x20, 0x32)

    await publish(dut, 0x31, 0x32)        # 只发布最后一行
    await wait_quiet(dut, mem)
    sb.check_equal(mem.reads, [0x31], "只发布一行 ⇒ 只读一行, 不到环外去")

    task.kill()
    sb.report()


@cocotb.test()
async def test_range_invalidate_only_touches_published(dut):
    """范围失效只作废 [start, end) 的行:别的行(比如上一次的邻居)还留着。"""
    sb = Scoreboard("cmd-cache-range-inval")
    mem = LineMem()
    mem.set_line(0x50, [0xD000 + i for i in range(BLOCK_WORDS)])
    mem.set_line(0x51, [0xD100 + i for i in range(BLOCK_WORDS)])
    task = await setup(dut, mem)
    dut.ring_base_line.value = 0x50
    dut.ring_end_line.value = 0x60

    await read_line(dut, 0x50)
    await read_line(dut, 0x51)
    before = len(mem.reads)

    # host 只重写了 0x51 那一行并重新发布它
    mem.set_line(0x51, [0xE100 + i for i in range(BLOCK_WORDS)])
    await publish(dut, 0x51, 0x52)
    await wait_quiet(dut, mem)

    d51, _, _ = await read_line(dut, 0x51)
    sb.check_equal(d51, mem.words(0x51), "重新发布的行必须读到新数据")
    d50, _, _ = await read_line(dut, 0x50)
    sb.check_equal(d50, mem.words(0x50), "没被发布覆盖的行还是原来的数据")
    sb.check_int(len(mem.reads), before + 1, "只重读被重新发布的那一行(0x50 仍命中)")

    task.kill()
    sb.report()


@cocotb.test()
async def test_inval_all_forces_refill(dut):
    """`inval_all`(CP_RESET / 改 QUEUE_BASE 之类)之后所有行都必须重读。"""
    sb = Scoreboard("cmd-cache-inval-all")
    mem = LineMem()
    mem.set_line(0x22, [0xD000 + i for i in range(BLOCK_WORDS)])
    task = await setup(dut, mem)

    await read_line(dut, 0x22)
    mem.set_line(0x22, [0xE000 + i for i in range(BLOCK_WORDS)])
    await pulse(dut, "inval_all")
    data, _, _ = await read_line(dut, 0x22)
    sb.check_equal(data, mem.words(0x22), "全片失效后应读到新数据")
    sb.check_int(len(mem.reads), 2, "全片失效后必须重读一次总线")

    task.kill()
    sb.report()


@cocotb.test()
async def test_publish_during_fill_drops_stale_bytes(dut):
    """回填在飞时被发布事件作废 ⇒ 不能把旧字节装进缓存,请求要重取到新字节。

    这条对应 CP 的真实危险窗口:驱动改写 `[tail_pub, 新 tail)` 的同时,缓存可能正在
    预取/回填同一行。若不丢弃这次回填,旧命令会被当成有效行留下。
    """
    sb = Scoreboard("cmd-cache-rearm")
    mem = LineMem(latency=10)                       # 拉长"缺失 → 第一个字"的窗口
    runner = await setup(dut, mem)
    dut.ring_end_line.value = 0                      # 不预取, 只测 demand 回的 rearm
    line = 0x40
    old = [(line << 16) | i for i in range(BLOCK_WORDS)]
    new = [(line << 16) | (0x100 + i) for i in range(BLOCK_WORDS)]
    mem.set_line(line, old)

    await FallingEdge(dut.clk)
    dut.req_line.value = line & LINE_MASK
    dut.req_valid.value = 1
    # 等到缓存真的开始回填这一行(m_rd_req 起来),再改内存内容 + 发布这一行
    for _ in range(80):
        await RisingEdge(dut.clk)
        if int(dut.m_rd_req.value) or line in mem.reads:
            break
    else:
        raise AssertionError("缓存没有发起回填")
    mem.set_line(line, new)                          # 驱动把新命令写进这一行
    await publish(dut, line, line + 1)               # 并发布它

    data = None
    for _ in range(400):
        await RisingEdge(dut.clk)
        if int(dut.rsp_valid.value):
            data = _unpack(int(dut.rsp_data.value))
            dut.req_valid.value = 0
            break
    else:
        raise AssertionError("请求没有返回")

    sb.check_int(data[0], new[0], "回填被作废后必须重取新字节(第 0 字)")
    sb.check_int(data[BLOCK_WORDS - 1], new[BLOCK_WORDS - 1], "回填被作废后必须重取新字节(最后一字)")
    sb.check_int(len(mem.reads) >= 2, True, "同一行至少读了两次(旧回填被丢弃)")
    runner.kill()
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
