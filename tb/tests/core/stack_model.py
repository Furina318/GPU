"""ISA.md §2.3.3 的独立 Python 模型 + 一个够用的小数据通路, 供 RTL 测试当金标准。

分两层:

* :class:`SimtStackModel` —— **只按文档写的**发散/收敛状态机(栈帧、压栈/弹栈、
  切路径、recov 有效位、三类错误)。它不看 RTL, 也不看 C++ 模拟器, 与
  ``sim/simulator/tests/stack_model_check.py`` 是同一套规则的第二次实现。
* :class:`MiniWarp` + :func:`run_kernel` —— 把上面那台状态机接到一个**只认识
  测试内核用到的指令**的每 lane 数据通路上, 于是可以"跑一个内核", 逐条给出
  执行过的 PC / lane 掩码 / 栈深。RTL 侧(fetch + simt_stack)用同一份
  ``MiniWarp`` 扮演 EXU(算谓词掩码), 两边唯一共享的就是这个小数据通路;
  §2.3.3 的机械部分是两条独立路径。

指令编码取自 ISA.md §4.1 / §5.5~§5.9。
"""

from __future__ import annotations

from dataclasses import dataclass, field

LANES = 8
FULL_MASK = (1 << LANES) - 1

# opcode(ISA.md §6.1 的表)
OP_ALU_IMM = 0x13   # addi / slti / ...
OP_LUI     = 0x37
OP_LOAD    = 0x03
OP_STORE   = 0x23
OP_ALU     = 0x33
OP_FMA     = 0x53
OP_SETP    = 0x0B
OP_CTRL    = 0x2B   # bar.sync / exit / tmov / wmov
OP_IPDOM   = 0x57
OP_BRANCH  = 0x63


# --------------------------------------------------------------------------
# 指令字段解码(与 RTL 的 decode.sv / fetch.sv 同一张表)
# --------------------------------------------------------------------------
def u32(x: int) -> int:
    return x & 0xFFFFFFFF


def s32(x: int) -> int:
    x &= 0xFFFFFFFF
    return x - (1 << 32) if x & 0x80000000 else x


def opcode(inst: int) -> int:
    return inst & 0x7F


def pred_field(inst: int) -> tuple[bool, int]:
    """返回 (inv, psel) —— ISA.md §4.1: pred 恒在 [28:26]。"""
    return bool((inst >> 28) & 1), (inst >> 26) & 3


def imm22(inst: int) -> int:
    """B 型 22 位立即数(已符号扩展, 未 <<2)。"""
    raw = (((inst >> 29) & 0x7) << 19) | ((inst >> 7) & 0x7FFFF)
    return raw - (1 << 22) if raw & (1 << 21) else raw


def imm20(inst: int) -> int:
    """U 型 20 位立即数(已符号扩展)。"""
    raw = (((inst >> 26) & 0x3F) << 14) | ((inst >> 12) & 0x3FFF)
    return raw - (1 << 20) if raw & (1 << 19) else raw


def imm9(inst: int) -> int:
    raw = (((inst >> 29) & 0x7) << 6) | ((inst >> 20) & 0x3F)
    return raw - (1 << 9) if raw & (1 << 8) else raw


def branch_target(inst: int, pc: int) -> int:
    return u32(pc + (imm22(inst) << 2))


def ipdom_recov(inst: int, pc: int) -> int:
    return u32(pc + (imm20(inst) << 2))


# --------------------------------------------------------------------------
# §2.3.3 状态机
# --------------------------------------------------------------------------
@dataclass
class Frame:
    recov_pc: int
    fall_pc: int
    mask_full: int
    mask_pending: int
    pending: bool = False


@dataclass
class StepResult:
    """一次 step(一条指令的推进)之后的样子。"""

    next_pc: int
    mask: int
    errors: set[str] = field(default_factory=set)


class SimtStackModel:
    """ISA.md §2.3.2/§2.3.3 的状态机。

    ``step()`` 就是一次"指令推进": 候选下一条 PC + 可选的本周期事件(ipdom 写
    recov / br.simt 解析结果), 返回收敛处理后的下一条 PC 与新掩码。
    """

    def __init__(self, depth: int = 8, lanes: int = LANES):
        self.depth = depth
        self.lanes = lanes
        self.full_mask = (1 << lanes) - 1
        self.stack: list[Frame] = []
        self.mask = self.full_mask
        self.recov_pc = 0
        self.recov_valid = False
        self.max_depth = 0
        self.pushes = 0
        self.reconvergences = 0

    # -- 生命周期 ---------------------------------------------------------
    def launch(self) -> None:
        self.stack.clear()
        self.mask = self.full_mask
        self.recov_pc = 0
        self.recov_valid = False

    @property
    def empty(self) -> bool:
        return not self.stack

    @property
    def depth_now(self) -> int:
        return len(self.stack)

    # -- 一次推进 ---------------------------------------------------------
    def step(
        self,
        next_pc: int,
        *,
        ipdom_pc: int | None = None,
        br: tuple[int, int, int] | None = None,
    ) -> StepResult:
        """``br`` = (target_pc, fall_pc, taken_raw); ``ipdom_pc`` = 收敛点。"""
        errors: set[str] = set()
        frames = [Frame(f.recov_pc, f.fall_pc, f.mask_full, f.mask_pending, f.pending)
                  for f in self.stack]
        sp = len(frames)
        mask = self.mask
        np = u32(next_pc)
        recov_pc, recov_valid = self.recov_pc, self.recov_valid

        if ipdom_pc is not None:
            recov_pc, recov_valid = u32(ipdom_pc), True

        if br is not None:
            target, fall, taken_raw = br
            taken = taken_raw & mask                   # 一律按 effective_mask 判定(§2.3.1)
            not_taken = mask & ~taken
            if taken == 0:
                np = u32(fall)                         # 均匀不跳
            elif not_taken == 0:
                np = u32(target)                       # 均匀跳转
            else:
                if not recov_valid:
                    errors.add("ERR_NO_RECOV")
                    np = u32(fall)
                elif sp >= self.depth:
                    errors.add("SIMT_OVF")
                    np = u32(fall)
                else:
                    frames.append(Frame(recov_pc, u32(fall), mask, not_taken, False))
                    sp += 1
                    self.pushes += 1
                    np = u32(target)
                    mask = taken
            recov_valid = False                        # recov 用后即清(§5.7)

        if errors:
            # 出错那一拍**什么都不更新**(连 recov 有效位也不清): 参考模型出错后不再走
            # advance_pc(exec.cpp:378-379), RTL 也是同样的门控。该 SM 随即停机。
            return StepResult(np, self.mask, errors)

        # ── 收敛推进(§2.3.3 第 2/3 条) ─────────────────────────────────────
        guard = 0
        while sp and frames[sp - 1].recov_pc == np:
            f = frames[sp - 1]
            if f.pending:
                mask = f.mask_full
                sp -= 1
            elif f.fall_pc == f.recov_pc:
                mask = f.mask_full
                sp -= 1
            else:
                f.pending = True
                mask = f.mask_pending
                np = f.fall_pc
            self.reconvergences += 1
            guard += 1
            if guard > self.depth + 2:
                errors.add("INTERNAL")
                break

        # -- 提交 --
        self.stack = frames[:sp]
        self.mask = mask
        self.recov_pc, self.recov_valid = recov_pc, recov_valid
        self.max_depth = max(self.max_depth, sp)
        return StepResult(np, mask, errors)


# --------------------------------------------------------------------------
# 够用的小数据通路(测试内核只用到标量整数指令)
# --------------------------------------------------------------------------
class MiniWarp:
    """每 lane 的 r0..r31 / p0..p3, 只实现测试内核用到的指令。

    它同时充当 RTL 测试里的"EXU": :meth:`taken_raw` 给出 br.simt 的谓词掩码
    (未与 active_mask 相交 —— 相交是 simt_stack 的职责, 这里故意不替它做)。
    """

    def __init__(self, lanes: int = LANES, wid: int = 0):
        self.lanes = lanes
        self.wid = wid
        self.rf = [[0] * lanes for _ in range(32)]
        self.pf = [[False] * lanes for _ in range(4)]
        self.pf[0] = [True] * lanes             # p0 恒真
        self.executed = 0

    def _cond(self, cond: int, a: int, b: int, unsigned: bool) -> bool:
        if unsigned:
            a, b = a & 0xFFFFFFFF, b & 0xFFFFFFFF
        if cond == 0:
            return a == b
        if cond == 1:
            return a != b
        if cond == 2:
            return a < b
        if cond == 3:
            return a >= b
        if cond == 4:
            return a <= b
        if cond == 5:
            return a > b
        raise NotImplementedError(f"保留的 setp cond={cond}(ISA §4.1)")

    def pred_mask(self, psel: int, inv: bool) -> int:
        vals = self.pf[psel]
        m = 0
        for lane in range(self.lanes):
            bit = bool(vals[lane])
            if inv:
                bit = not bit
            if bit:
                m |= 1 << lane
        return m

    def execute(self, inst: int) -> None:
        """执行一条非控制流指令(控制流由模型负责, 这里只更新数据)。"""
        op = opcode(inst)
        rd = (inst >> 7) & 0x1F
        rs1 = (inst >> 15) & 0x1F
        rs2 = (inst >> 20) & 0x1F
        if op == OP_CTRL:
            funct3 = (inst >> 12) & 0x7
            if funct3 == 0b011:                                  # tmov rd, tid
                self.rf[rd] = [lane for lane in range(self.lanes)]
            elif funct3 == 0b100:                                # wmov rd, wid
                self.rf[rd] = [self.wid] * self.lanes
            elif funct3 in (0b001, 0b010):                       # bar.sync / exit
                pass
            else:
                raise NotImplementedError(f"保留的控制指令 funct3={funct3}")
        elif op == OP_ALU_IMM:
            funct3 = (inst >> 12) & 0x7
            if funct3 != 0:
                raise NotImplementedError(f"只实现 addi, funct3={funct3}")
            imm = imm9(inst)
            self.rf[rd] = [u32(v + imm) for v in self.rf[rs1]]
        elif op == OP_SETP:
            cond = (inst >> 29) & 0x7
            family = (inst >> 12) & 0x7
            if family == 2:
                raise NotImplementedError("测试内核不用 FP 谓词(setp.f*)")
            pdst = rd
            if pdst not in (1, 2, 3):
                raise NotImplementedError(f"setp 目标只能是 p1/p2/p3, got p{pdst}")
            self.pf[pdst] = [
                self._cond(cond, s32(self.rf[rs1][lane]) if family == 0 else self.rf[rs1][lane],
                           s32(self.rf[rs2][lane]) if family == 0 else self.rf[rs2][lane],
                           unsigned=(family == 1))
                for lane in range(self.lanes)
            ]
        else:
            raise NotImplementedError(f"MiniWarp 不实现 opcode=0x{op:02x} 的指令")
        self.executed += 1
        self.rf[0] = [0] * self.lanes                            # r0 恒 0

    def taken_raw(self, inst: int) -> int:
        """br.simt 的谓词掩码(未与 active_mask 相交)。"""
        inv, psel = pred_field(inst)
        if psel == 0:
            return 0 if inv else (1 << self.lanes) - 1            # p0: 与 lane 无关(§5.8)
        return self.pred_mask(psel, inv)


# --------------------------------------------------------------------------
# 跑一个内核: 模型侧的期望序列
# --------------------------------------------------------------------------
@dataclass
class Trace:
    """逐条指令的期望: pc / 该指令执行时的 lane 掩码 / 执行前的栈深。"""

    steps: list[tuple[int, int, int]] = field(default_factory=list)   # (pc, mask, depth)
    errors: list[tuple[str, int]] = field(default_factory=list)       # (错误名, pc)
    finished: bool = False                                            # 见到 exit

    @property
    def pcs(self) -> list[int]:
        return [pc for pc, _, _ in self.steps]


def run_kernel(words: dict[int, int], entry: int, *, lanes: int = LANES, depth: int = 8,
               wid: int = 0, max_steps: int = 20000) -> Trace:
    """用 §2.3.3 模型跑内核; ``words`` = {pc: inst}。"""
    model = SimtStackModel(depth=depth, lanes=lanes)
    warp = MiniWarp(lanes=lanes, wid=wid)
    model.launch()
    trace = Trace()
    pc = entry

    for _ in range(max_steps):
        inst = words.get(pc)
        if inst is None:
            break
        trace.steps.append((pc, model.mask, model.depth_now))
        op = opcode(inst)

        if op == OP_IPDOM:
            res = model.step(pc + 4, ipdom_pc=ipdom_recov(inst, pc))
        elif op == OP_BRANCH:
            res = model.step(pc + 4,
                             br=(branch_target(inst, pc), pc + 4, warp.taken_raw(inst)))
        elif op == OP_CTRL and ((inst >> 12) & 0x7) == 0b010:      # exit
            trace.finished = True
            break
        elif op == OP_CTRL and ((inst >> 12) & 0x7) == 0b001:      # bar.sync
            if model.mask != model.full_mask or not model.empty:
                trace.errors.append(("BAR_DIVERGENT", pc))        # ISA §2.4 / §2.6
                break
            res = model.step(pc + 4)
        else:
            warp.execute(inst)
            res = model.step(pc + 4)

        if res.errors:
            for e in sorted(res.errors):
                trace.errors.append((e, pc))
            break
        pc = res.next_pc

    return trace


# --------------------------------------------------------------------------
# 镜像文件(sw/assembly.py --format image): 头 32B + 代码, 整块放在 SRAM 偏移 0
# --------------------------------------------------------------------------
IMG_HDR_BYTES = 32
IMG_MAGIC = 0x53474B31          # "SGK1"(与 sw/assembly.py、memory.hpp 一致)


def parse_image(raw: bytes) -> tuple[dict[int, int], int, int]:
    """返回 (words, entry, size): words = {pc: inst}, PC 就是镜像内的字节偏移。"""
    import struct

    magic, entry, size, _flags, _crc, csize = struct.unpack_from("<6I", raw, 0)
    if magic != IMG_MAGIC:
        raise ValueError(f"镜像 magic 不是 SGK1: 0x{magic:08x}")
    if entry < IMG_HDR_BYTES or entry + csize != size:
        raise ValueError(f"镜像头不自洽: entry=0x{entry:x} size=0x{size:x} csize=0x{csize:x}")
    words = {
        i * 4: struct.unpack_from("<I", raw, i * 4)[0]
        for i in range(size // 4)
    }
    return words, entry, size


def patch_word(raw: bytes, word_index: int, new_word: int) -> bytes:
    """把镜像里第 word_index 个 32 位字(从 entry 起算)换掉, 并重算 CRC-32。

    与 ``sim/simulator/tests/run_tests.py`` 的 patch_image 同一手法: 用来做
    "把汇编器生成的 ipdom 抹掉"这类错误注入。
    """
    import struct
    import zlib

    data = bytearray(raw)
    _magic, entry, _size, _flags, _crc, csize = struct.unpack_from("<6I", data, 0)
    off = entry + word_index * 4
    if off + 4 > entry + csize:
        raise ValueError("patch 越出代码区")
    struct.pack_into("<I", data, off, new_word)
    struct.pack_into("<I", data, 16, zlib.crc32(bytes(data[entry:entry + csize])) & 0xFFFFFFFF)
    return bytes(data)
