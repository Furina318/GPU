"""CP 的 cocotb 测试环境:内存模型 + AXI4-Lite 主机 + SM/引擎/指令 SRAM 桩。

被测的是 `hw/rtl/cp/`(cp.sv + cp_regs.sv + axil_slave.sv + cmd_decode.sv + crc32_word.sv)。
这里扮演 CP 周围的一切:

* :class:`MemModel` —— 全局内存(命令环 / 镜像 / 常量区 / 信号量), 说 CP 的
  `mem_rd_*`/`mem_wr_*` 行读-字写协议(ack 一拍、rsp 再一拍), 并记录事务日志;
* :func:`axil_write` / :func:`axil_read` —— host 的 AXI4-Lite 读寄存器;
* :class:`SmMock` —— 两个 SM:`sm_launch_valid` 来了就"跑 kernel"(若干拍后置
  `sm_done`), 可以注入 `sm_error`(验证 SM_FAULT 与 STALE_SM_ERROR);
* :class:`EngMock` —— 2D / GEMM:收 `eng_valid` + payload, 回 `eng_ready`,
  再按脚本置 `{ERROR,DONE,BUSY}`;
* :class:`SramModel` —— 指令 SRAM 的预加载口, 逐字收下并回 `load_done`。

时间约定(与 RTL 对齐, 用探针实测过):**在周期中点(FallingEdge)驱动与采样**,
翻转沿在 RisingEdge 生效 —— 这样"某一拍了什么"不会有歧义。
"""

from __future__ import annotations

import struct
from pathlib import Path

import cocotb
from cocotb.triggers import FallingEdge, RisingEdge, ReadOnly, Timer

QUEUE_BASE = 0x0100_0000
QUEUE_SIZE = 4096
SEM_BASE = 0x0300_0000
CONST_BASE = 0x0200_0000
IMG_BASE = 0x0200_1000
FB_BASE = 0x1000_0000
DDR_BASE = 0x8000_0000

# MMIO 偏移(defines.vh)
MMIO_CP_STATUS, MMIO_CP_ERROR, MMIO_CMD_HEAD, MMIO_CMD_TAIL = 0x00, 0x04, 0x08, 0x0c
MMIO_DOORBELL, MMIO_IRQ_STATUS, MMIO_IRQ_ENABLE, MMIO_SM_DONE = 0x10, 0x14, 0x18, 0x1c
MMIO_SM0_ERROR, MMIO_SM1_ERROR, MMIO_SEM_BASE = 0x20, 0x24, 0x28
MMIO_CMD_QUEUE_BASE, MMIO_CMD_QUEUE_SIZE = 0x2c, 0x30
MMIO_CONST_BASE, MMIO_FB_BASE, MMIO_DDR_BASE = 0x34, 0x38, 0x3c
MMIO_CP_RESET, MMIO_TIMEOUT_CYCLES = 0x40, 0x44
MMIO_SM_KERNEL_MASK = 0x48
MMIO_GEMM_STATUS, MMIO_GEMM_ERROR = 0x60, 0x64
MMIO_2D_STATUS, MMIO_2D_ERROR = 0x68, 0x6c

# CP_ERROR(CMDS §8.1)
CPERR_NONE, CPERR_CMD, CPERR_LAUNCH_REJECT, CPERR_BUS = 0, 1, 2, 3
CPERR_TIMEOUT, CPERR_ILLEGAL_STATE, CPERR_STALE_SM_ERROR = 4, 5, 6
CPERR_SM_FAULT, CPERR_ENGINE_FAULT = 7, 8

CMD_NOP, CMD_WRITE_REG, CMD_READ_REG, CMD_LOAD_KERNEL = 0x00, 0x01, 0x02, 0x03
CMD_LAUNCH, CMD_FENCE, CMD_SIGNAL, CMD_WAIT = 0x04, 0x05, 0x06, 0x07
CMD_IRQ, CMD_PARAMS, CMD_GEMM = 0x08, 0x09, 0x18
CMD_2D_FILL, CMD_2D_BLIT, CMD_2D_DRAW, CMD_2D_SET_REG, CMD_2D_FLUSH = 0x10, 0x11, 0x12, 0x13, 0x14
SEM_NONE = 0xFFFF

CFLAG_SYNC = 1 << 0
CFLAG_IRQ_ON_DONE = 1 << 1
FLAG_USES_BARRIER = 1 << 0
FLAG_USES_SHARED = 1 << 1
FLAG_REQUIRES_SINGLE_SM = 1 << 3


# ─────────────────────────── 内存模型 ───────────────────────────
class MemModel:
    """全局内存:稀疏字寻址, 说 CP 的行读/字写协议, 并记账。"""

    def __init__(self, dut):
        self.dut = dut
        self.mem: dict[int, int] = {}
        self.rd_log: list[tuple[int, int]] = []      # (addr, line?)
        self.wr_log: list[tuple[int, int]] = []      # (addr, data)
        self.bus_err_next = False
        self.rd_delay = 0                            # 额外的响应延迟(拍)
        self._task = None

    # ── host/测试用:直接访问内存 ──
    def poke(self, addr: int, data: int) -> None:
        self.mem[addr >> 2] = data & 0xFFFF_FFFF

    def peek(self, addr: int) -> int:
        return self.mem.get(addr >> 2, 0)

    def poke_bytes(self, addr: int, data: bytes) -> None:
        for i in range(0, len(data) - 3, 4):
            self.poke(addr + i, struct.unpack_from("<I", data, i)[0])

    def peek_bytes(self, addr: int, n: int) -> bytes:
        out = bytearray()
        for i in range(0, n, 4):
            out += struct.pack("<I", self.peek(addr + i))
        return bytes(out[:n])

    def poke_line(self, base: int) -> int:
        """把 64B 行打包成 RTL 的 512 bit 总线值。"""
        v = 0
        for i in range(16):
            v |= (self.peek(base + i * 4) & 0xFFFF_FFFF) << (i * 32)
        return v

    # ── 后台任务:回应 CP 的内存请求 ──
    def start(self):
        self._task = cocotb.start_soon(self._run())

    async def _run(self):
        d = self.dut
        while True:
            await FallingEdge(d.clk)
            if int(d.mem_rd_req.value) == 1:
                addr = int(d.mem_rd_addr.value)
                line = int(d.mem_rd_line.value)
                self.rd_log.append((addr, line))
                err = self.bus_err_next
                self.bus_err_next = False
                d.mem_rd_ack.value = 1
                await RisingEdge(d.clk)               # ack 被采
                d.mem_rd_ack.value = 0
                for _ in range(self.rd_delay):        # 可配的额外延迟
                    await RisingEdge(d.clk)
                d.mem_rd_rsp.value = 1
                d.mem_rd_err.value = err
                d.mem_rd_data.value = self.poke_line(addr & ~0x3F) if line else self.peek(addr)
                await RisingEdge(d.clk)               # rsp 被采
                d.mem_rd_rsp.value = 0
                d.mem_rd_err.value = 0
            elif int(d.mem_wr_req.value) == 1:
                addr = int(d.mem_wr_addr.value)
                data = int(d.mem_wr_data.value)
                self.wr_log.append((addr, data))
                self.poke(addr, data)
                err = self.bus_err_next
                self.bus_err_next = False
                d.mem_wr_ack.value = 1
                await RisingEdge(d.clk)
                d.mem_wr_ack.value = 0
                d.mem_wr_rsp.value = 1
                d.mem_wr_err.value = err
                await RisingEdge(d.clk)
                d.mem_wr_rsp.value = 0
                d.mem_wr_err.value = 0


# ─────────────────────────── AXI4-Lite 主机 ───────────────────────────
async def axil_write(dut, addr: int, data: int, timeout: int = 60) -> None:
    """一拍一个 AXI4-Lite 写:AW/W 同拍发, 等 B 收到。

    时序约定:在**周期中点**驱动、也在周期中点看 ready —— ready=1 表示"本拍末尾会握手",
    而不是"上一拍握过了"(这一点在写测试台时最容易搞反)。
    """
    await FallingEdge(dut.clk)
    dut.s_awvalid.value = 1
    dut.s_awaddr.value = addr
    dut.s_wvalid.value = 1
    dut.s_wdata.value = data
    dut.s_wstrb.value = 0xF
    dut.s_bready.value = 1
    got = False
    for _ in range(timeout):
        if int(dut.s_awready.value) == 1 and int(dut.s_wready.value) == 1:
            got = True
            break
        await FallingEdge(dut.clk)
    assert got, f"AXI4-Lite 写 0x{addr:02x}:awready/wready 一直没起来"
    await RisingEdge(dut.clk)                 # 本拍末尾完成 AW/W 握手
    dut.s_awvalid.value = 0
    dut.s_wvalid.value = 0
    for _ in range(timeout):
        await FallingEdge(dut.clk)
        if int(dut.s_bvalid.value) == 1:
            await RisingEdge(dut.clk)         # bready=1 ⇒ B 也被收下
            dut.s_bready.value = 0
            return
    raise AssertionError(f"AXI4-Lite 写 0x{addr:02x}:没等到 B 响应")


async def axil_read(dut, addr: int, timeout: int = 60) -> int:
    await FallingEdge(dut.clk)
    dut.s_arvalid.value = 1
    dut.s_araddr.value = addr
    dut.s_rready.value = 1
    got = False
    for _ in range(timeout):
        if int(dut.s_arready.value) == 1:
            got = True
            break
        await FallingEdge(dut.clk)
    assert got, f"AXI4-Lite 读 0x{addr:02x}:arready 一直没起来"
    await RisingEdge(dut.clk)
    dut.s_arvalid.value = 0
    for _ in range(timeout):
        await FallingEdge(dut.clk)
        if int(dut.s_rvalid.value) == 1:
            v = int(dut.s_rdata.value)
            await RisingEdge(dut.clk)         # rready=1 ⇒ R 被收下
            dut.s_rready.value = 0
            return v
    raise AssertionError(f"AXI4-Lite 读 0x{addr:02x}:没等到 R 响应")


async def cp_status(dut) -> tuple[int, int, int, int]:
    """返回 (idle, busy, error, cp_error_code)。"""
    st = await axil_read(dut, MMIO_CP_STATUS)
    err = await axil_read(dut, MMIO_CP_ERROR)
    return (st & 1, (st >> 1) & 1, (st >> 2) & 1, err)


async def wait_idle(dut, timeout: int = 20000) -> None:
    """等 CP 回到 IDLE(队列空 && 没有在跑的命令):读 CP_STATUS 的 IDLE 位。"""
    for _ in range(max(4, timeout // 24)):
        st = await axil_read(dut, MMIO_CP_STATUS)
        if (st & 1) == 1:
            return
    raise AssertionError("CP 没有回到 IDLE")


class SmMock:
    """两个 SM:收到 launch 就跑 kernel(可配拍数), 之后置 sm_done。"""

    def __init__(self, dut, run_cycles: int = 6):
        self.dut = dut
        self.run_cycles = run_cycles
        self.launches: list[tuple[int, int, int]] = []   # (pc, warps, flags)
        self.error0 = 0
        self.error1 = 0
        self.fault_pc0 = 0
        self.fault_pc1 = 0
        self.done = 0
        self._task = None

    def start(self):
        self._task = cocotb.start_soon(self._run())

    async def _run(self):
        d = self.dut
        while True:
            await FallingEdge(d.clk)
            # 把当前状态推给 DUT(便于测试中途注入 SM 错误)
            d.sm0_error.value = self.error0
            d.sm1_error.value = self.error1
            d.sm0_fault_pc.value = self.fault_pc0
            d.sm1_fault_pc.value = self.fault_pc1
            d.sm_done.value = self.done
            if int(d.sm_launch_valid.value) != 0:
                self.launches.append((int(d.sm_launch_pc.value),
                                      int(d.sm_launch_warps.value),
                                      int(d.sm_launch_flags.value)))
                self.done = 0                            # 分发时清 SM_DONE(§4.3)
                sel = int(d.sm_launch_valid.value)
                await RisingEdge(d.clk)
                for _ in range(self.run_cycles):         # "跑 kernel"
                    d.sm_done.value = 0
                    await RisingEdge(d.clk)
                    if (self.error0 and (sel & 1)) or (self.error1 and (sel & 2)):
                        break                            # 出错 ⇒ 不再置 done
                if not ((self.error0 and (sel & 1)) or (self.error1 and (sel & 2))):
                    self.done = sel
                    d.sm_done.value = sel


class EngMock:
    """2D / GEMM:收命令头 + payload, 回状态 {ERROR,DONE,BUSY}。

    约定(与 cp.sv 一致):收到新命令时自己清 DONE;payload 用 ready 背压逐字收;
    跑完后置 DONE 并清 BUSY。`error` 一置位就表示这条命令失败。
    """

    def __init__(self, dut, run_cycles: int = 4):
        self.dut = dut
        self.run_cycles = run_cycles
        self.cmds: list[tuple[int, int, int, list[int]]] = []   # (engine, opcode, len, payload)
        self.engine = 0              # 最近一次命令的引擎(给状态寄存器选路)
        self.busy = 0
        self.done = 0
        self.error = 0
        self.err_code = 0
        self._task = None

    def start(self):
        self._task = cocotb.start_soon(self._run())

    def status(self) -> int:
        return ((self.error & 1) << 2) | ((self.done & 1) << 1) | (self.busy & 1)

    async def _run(self):
        d = self.dut
        d.eng_ready.value = 1
        while True:
            await FallingEdge(d.clk)
            # 状态寄存器(0x60/0x64 给 GEMM, 0x68/0x6c 给 2D)
            if self.engine == 2:
                d.gemm_status.value = self.status(); d.gemm_error.value = self.err_code
                d.twod_status.value = 0;             d.twod_error.value = 0
            else:
                d.twod_status.value = self.status(); d.twod_error.value = self.err_code
                d.gemm_status.value = 0;             d.gemm_error.value = 0
            if int(d.eng_valid.value) == 1:
                self.engine = int(d.eng_engine.value)
                opcode = int(d.eng_opcode.value)
                length = int(d.eng_len.value)
                payload: list[int] = []
                self.done = 0                            # 收新命令 ⇒ 清 DONE
                self.busy = 1
                await RisingEdge(d.clk)                  # 头被收下
                for _ in range(length):                  # 收 payload(最多 len 个字, 够用)
                    await FallingEdge(d.clk)
                    if int(d.eng_wvalid.value) == 1:
                        payload.append(int(d.eng_wdata.value))
                        if int(d.eng_wlast.value) == 1:
                            break
                self.cmds.append((self.engine, opcode, length, payload))
                for _ in range(self.run_cycles):
                    await RisingEdge(d.clk)
                self.busy = 0
                self.done = 1
                d.eng_ready.value = 1


class SramModel:
    """指令 SRAM 的预加载口(每股一份):逐字收下, last 之后回一拍 load_done。"""

    def __init__(self, dut):
        self.dut = dut
        self.words: list[dict[int, int]] = [{}, {}]
        self.loaded = [0, 0]
        self._task = None

    def start(self):
        self._task = cocotb.start_soon(self._run())

    async def _run(self):
        d = self.dut
        while True:
            await FallingEdge(d.clk)
            if int(d.sram_load_en.value) != 0:
                addr = int(d.sram_load_addr.value)
                data = int(d.sram_load_data.value)
                last = int(d.sram_load_last.value)
                en = int(d.sram_load_en.value)
                for i in range(2):
                    if en >> i & 1:
                        self.words[i][addr] = data
                        self.loaded[i] += 1
                if last:
                    await RisingEdge(d.clk)
                    d.sram_load_done.value = last
                    await RisingEdge(d.clk)
                    d.sram_load_done.value = 0
                else:
                    d.sram_load_done.value = 0


# ─────────────────────────── 命令流工具 ───────────────────────────
def cmd(words: list[int]) -> list[int]:
    """把 (word0, word1, ...) 组装成一条命令:调用者给 opcode/len 与参数。"""
    return words


def make_cmd(opcode: int, params: list[int], *, signal_id: int = SEM_NONE,
             wait_id: int = SEM_NONE, flags: int = 0, length: int | None = None) -> list[int]:
    """按 CMDS §2.2 组装一条命令(自动补 length_words 与依赖字)。"""
    body = [0, (signal_id << 16) | wait_id] + params
    n = length if length is not None else len(body)
    body[0] = (n << 16) | (flags << 8) | opcode
    if n > len(body):
        body += [0] * (n - len(body))
    return body


class QueueHost:
    """扮演驱动:往命令环里写命令、更新 CMD_TAIL、敲 DOORBELL。"""

    def __init__(self, dut, mem: MemModel):
        self.dut = dut
        self.mem = mem
        self.tail = QUEUE_BASE
        self.pad = 0            # 每条命令后的填充粒度(0 = 不填充)

    async def submit(self, words: list[int]) -> int:
        addr = self.tail
        for i, w in enumerate(words):
            self.mem.poke(addr + i * 4, w)
        self.tail += len(words) * 4
        if self.pad:
            off = (self.tail - QUEUE_BASE) % self.pad
            if off:
                pad = self.pad - off
                if pad == 4:
                    pad += self.pad
                nop = make_cmd(CMD_NOP, [], length=pad // 4)
                for i, w in enumerate(nop):
                    self.mem.poke(self.tail + i * 4, w)
                self.tail += pad
        await axil_write(self.dut, MMIO_CMD_TAIL, self.tail)
        return addr

    async def doorbell(self) -> None:
        await axil_write(self.dut, MMIO_DOORBELL, 1)

    async def run(self, timeout: int = 20000) -> None:
        """敲门并等 CP 把队列处理完(回到 IDLE)。"""
        await self.doorbell()
        await wait_idle(self.dut, timeout)

    async def run_expect_error(self, timeout: int = 4000) -> int:
        """敲门并等 CP 进错误态 —— **出错后 CP 按设计就停在 ERROR**, 不会再回 IDLE。"""
        await self.doorbell()
        for _ in range(max(4, timeout // 16)):
            code = await axil_read(self.dut, MMIO_CP_ERROR)
            if code != 0:
                return code
        return await axil_read(self.dut, MMIO_CP_ERROR)


# ─────────────────────────── 镜像工具 ───────────────────────────
REPO = Path(__file__).resolve().parents[3]
ASM = REPO / "sw" / "assembly.py"
KERNEL_DIR = REPO / "sim" / "simulator" / "tests"
BUILD_DIR = REPO / "tb" / "build" / "cp_tests"


def assemble_image(name: str) -> bytes:
    import subprocess
    import sys
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    src = name if Path(name).is_absolute() else KERNEL_DIR / name
    img = BUILD_DIR / (Path(src).stem + ".bin")
    proc = subprocess.run([sys.executable, str(ASM), str(src), "--format", "image",
                           "-o", str(img)], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"汇编 {src} 失败: {proc.stdout}{proc.stderr}")
    return img.read_bytes()


def image_fields(raw: bytes) -> dict:
    magic, entry, size, flags, crc, csize = struct.unpack_from("<6I", raw, 0)
    return {"magic": magic, "entry": entry, "size": size, "flags": flags,
            "crc": crc, "csize": csize, "raw": raw}
