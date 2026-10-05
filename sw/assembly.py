#!/usr/bin/env python3
"""SimpleGPU 汇编器 (ISA.md v0.2)。

    python3 assembly.py in.S                            # hex 到 stdout
    python3 assembly.py in.S -o out.hex
    python3 assembly.py in.S --format image -o k.bin    # 带内核镜像头 (CMDS.md §6.4)
    python3 assembly.py in.S --disasm                   # 反汇编到 stderr
    python3 assembly.py in.S --disasm out.txt           # 反汇编写到文件
"""

import argparse
import difflib
import re
import sys
import zlib
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

OP_LD, OP_ST = 0b0000011, 0b0100011
OP_IMM, OP_LUI, OP_R = 0b0010011, 0b0110111, 0b0110011
OP_R4, OP_SETP, OP_CTRL, OP_IPDOM, OP_BR = 0b1010011, 0b0001011, 0b0101011, 0b1010111, 0b1100011

F3_INT, F3_FP = 0b000, 0b001
F3_FMA_FP, F3_FMA_INT = 0b000, 0b001
F3_BAR, F3_EXIT, F3_TMOV, F3_WMOV = 0b001, 0b010, 0b011, 0b100

INT_ALU = {'add': 0b0000, 'sub': 0b0001, 'and': 0b0010, 'or': 0b0011, 'xor': 0b0100,
           'sll': 0b0101, 'srl': 0b0110, 'sra': 0b0111, 'slt': 0b1000, 'sltu': 0b1001,
           'mul': 0b1010, 'div': 0b1011, 'divu': 0b1100, 'rem': 0b1101, 'remu': 0b1110}
FP_ALU = {'fadd': 0b0000, 'fsub': 0b0001, 'fmul': 0b0010, 'fmin': 0b0011,
          'fmax': 0b0100, 'f2i': 0b0101, 'i2f': 0b0110}
FMA = {'fma.f32': 0b00, 'fmsub.f32': 0b01, 'fnmsub.f32': 0b10, 'fnmadd.f32': 0b11}
IMM_ALU = {'addi': 0b000, 'slli': 0b001, 'slti': 0b010, 'sltiu': 0b011,
           'xori': 0b100, 'srli': 0b101, 'srai': 0b101, 'ori': 0b110, 'andi': 0b111}
SHIFTS = frozenset({'slli', 'srli', 'srai'})
SETP = {'eq': (0b000, 0), 'ne': (0b001, 0), 'lt': (0b010, 0), 'ge': (0b011, 0),
        'le': (0b100, 0), 'gt': (0b101, 0),
        'ltu': (0b010, 1), 'geu': (0b011, 1), 'leu': (0b100, 1), 'gtu': (0b101, 1),
        'feq': (0b000, 2), 'fne': (0b001, 2), 'flt': (0b010, 2), 'fge': (0b011, 2),
        'fle': (0b100, 2), 'fgt': (0b101, 2)}
PRED = {'@p0': 0b000, '@p1': 0b001, '@p2': 0b010, '@p3': 0b011,
        '@!p0': 0b100, '@!p1': 0b101, '@!p2': 0b110, '@!p3': 0b111}

NO_PRED = frozenset(FMA) | {'imad', 'lui', 'ipdom'}   # 无谓词字段 (ISA §4.2)
MUST_P0 = frozenset({'exit', 'bar.sync'})             # 必须均匀 (ISA §2.4/§2.5)
UNIFORM = frozenset({'@p0', '@!p0'})                  # 可证明不发散

FLAG_BARRIER, FLAG_SHARED, FLAG_GLOBAL_WRITE, FLAG_SINGLE_SM = 1, 2, 4, 8
FLAG_NAMES = {FLAG_BARRIER: 'USES_BARRIER', FLAG_SHARED: 'USES_SHARED',
              FLAG_GLOBAL_WRITE: 'USES_GLOBAL_WRITE', FLAG_SINGLE_SM: 'REQUIRES_SINGLE_SM'}
IMG_MAGIC, IMG_HDR_BYTES = 0x53474B31, 32

INT_REV = {v: k for k, v in INT_ALU.items()}
FP_REV = {v: k for k, v in FP_ALU.items()}
FMA_REV = {v: k for k, v in FMA.items()}
IMM_REV = {v: k for k, v in IMM_ALU.items() if k != 'srai'}
SETP_REV = {(c, f): n for n, (c, f) in SETP.items()}
PRED_REV = {v: k for k, v in PRED.items()}


class AsmError(Exception):
    pass


# ── 位打包 (ISA §4.1) ────────────────────────────────────────────────
def enc_r(pred: int, funct4: int, rs2: int, rs1: int, funct3: int, rd: int) -> int:
    return (((funct4 >> 1) & 7) << 29 | pred << 26 | (funct4 & 1) << 25 |
            (rs2 & 31) << 20 | (rs1 & 31) << 15 | (funct3 & 7) << 12 |
            (rd & 31) << 7 | OP_R)


def enc_i(pred: int, imm: int, rs1: int, funct3: int, rd: int, op: int = OP_IMM) -> int:
    u = imm & 0x1FF
    return ((u >> 6) << 29 | pred << 26 | (u & 0x3F) << 20 | (rs1 & 31) << 15 |
            (funct3 & 7) << 12 | (rd & 31) << 7 | op)


def enc_s(pred: int, imm: int, rs2: int, rs1: int, funct3: int) -> int:
    u = imm & 0x1FF
    return ((u >> 6) << 29 | pred << 26 | ((u >> 5) & 1) << 25 | (rs2 & 31) << 20 |
            (rs1 & 31) << 15 | (funct3 & 7) << 12 | (u & 31) << 7 | OP_ST)


def enc_u(imm20: int, rd: int, op: int) -> int:
    u = imm20 & 0xFFFFF
    return ((u >> 14) << 26 | (u & 0x3FFF) << 12 | (rd & 31) << 7 | op)


def enc_b(pred: int, imm22: int) -> int:
    u = imm22 & 0x3FFFFF
    return ((u >> 19) << 29 | pred << 26 | (u & 0x7FFFF) << 7 | OP_BR)


def enc_r4(funct2: int, rs3: int, rs2: int, rs1: int, funct3: int, rd: int) -> int:
    return (rs3 & 31) << 27 | (funct2 & 3) << 25 | (rs2 & 31) << 20 | \
           (rs1 & 31) << 15 | (funct3 & 7) << 12 | (rd & 31) << 7 | OP_R4


def enc_setp(pred: int, cond: int, rs2: int, rs1: int, family: int, pdest: int) -> int:
    return (cond & 7) << 29 | pred << 26 | 0 << 25 | (rs2 & 31) << 20 | \
           (rs1 & 31) << 15 | (family & 7) << 12 | (pdest & 7) << 7 | OP_SETP


# ── 操作数解析 ──────────────────────────────────────────────────────
REG_RE = re.compile(r'^r(\d+)$')
PDEST_RE = re.compile(r'^p([1-3])$')
IMM_RE = re.compile(r'^([+-]?)(0[xX][0-9a-fA-F]+|\d+)$')
MEM_RE = re.compile(r'^([+-]?(?:0[xX][0-9a-fA-F]+|\d+))?\s*\(\s*(r\d+)\s*\)$')


def parse_imm(s: str) -> int:
    m = IMM_RE.match(s.strip())
    if not m:
        raise AsmError(f"非法立即数: {s}")
    sign, body = m.groups()
    v = int(body, 16) if body[:2].lower() == '0x' else int(body)
    return -v if sign == '-' else v


def parse_reg(s: str) -> int:
    m = REG_RE.match(s.strip())
    if not m:
        raise AsmError(f"非法寄存器: {s}")
    n = int(m.group(1))
    if n > 31:
        raise AsmError(f"寄存器越界: {s}")
    return n


def parse_pdest(s: str) -> int:
    m = PDEST_RE.match(s.strip())
    if not m:
        raise AsmError(f"setp 目标必须是 p1/p2/p3: {s}")
    return int(m.group(1))


def parse_mem(s: str) -> Optional[Tuple[int, int]]:
    m = MEM_RE.match(s.strip())
    if not m:
        return None
    imm = parse_imm(m.group(1)) if m.group(1) else 0
    return imm, parse_reg(m.group(2))


def sext(v: int, bits: int) -> int:
    return v - (1 << bits) if v >> (bits - 1) else v


# ── 数据结构 ────────────────────────────────────────────────────────
@dataclass
class Instruction:
    line_no: int
    mnemonic: str
    ops: List[str]
    pred: str = '@p0'
    pc: int = 0
    encoding: int = 0
    auto_ipdom: bool = False


class Assembler:
    def __init__(self) -> None:
        self.instrs: List[Instruction] = []
        self.errors: List[str] = []
        self.label_index: Dict[str, int] = {}
        self.labels: Dict[str, int] = {}

    def err(self, line_no: int, msg: str) -> None:
        self.errors.append(f"line {line_no}: {msg}")

    # ── Pass 1: 解析、展开伪指令、自动插 ipdom ──
    def parse(self, source: str) -> None:
        pending: List[str] = []
        for line_no, raw in enumerate(source.splitlines(), 1):
            line = re.sub(r'[;#].*$', '', raw).strip()
            line = re.sub(r'^[0-9a-fA-F]{4}:\s+[0-9a-fA-F]{8}\s*', '', line)  # 允许回灌反汇编
            while line:
                m = re.match(r'^([A-Za-z_]\w*)\s*:\s*(.*)$', line)
                if not m:
                    break
                name, line = m.group(1), m.group(2).strip()
                if name in self.label_index:
                    self.err(line_no, f"标签重复定义: {name}")
                else:
                    pending.append(name)
            start = len(self.instrs)          # 标签指向本行第一条指令
            if line:
                try:
                    new = self.parse_instr(line, line_no)
                except AsmError as e:
                    self.err(line_no, str(e))
                    new = []
                if new and self.needs_ipdom(new[0]):
                    reconv = new[0].ops[1]
                    prev = self.instrs[-1] if self.instrs else None
                    if not (prev and prev.mnemonic == 'ipdom' and len(prev.ops) == 1
                            and self.same_target(prev.ops[0], reconv)):
                        self.instrs.append(Instruction(line_no, 'ipdom', [reconv],
                                                       '@p0', auto_ipdom=True))
                self.instrs.extend(new)
            for name in pending:
                self.label_index.setdefault(name, start)
            pending = []

        for i, ins in enumerate(self.instrs):
            ins.pc = i * 4
        self.labels = {n: i * 4 for n, i in self.label_index.items()}

    @staticmethod
    def needs_ipdom(ins: Instruction) -> bool:
        return ins.mnemonic == 'br.simt' and len(ins.ops) > 1 and ins.pred not in UNIFORM

    @staticmethod
    def same_target(a: str, b: str) -> bool:
        if a == b:
            return True
        try:
            return parse_imm(a) == parse_imm(b)     # 0x6c 与 0x006c 视为同一目标
        except AsmError:
            return False

    @staticmethod
    def check_arity(ops: List[str], want, mn: str) -> None:
        if not (len(ops) in want if isinstance(want, tuple) else len(ops) == want):
            raise AsmError(f"{mn} 操作数个数错误: 期望 {want}, 实际 {len(ops)}")

    def parse_instr(self, line: str, line_no: int) -> List[Instruction]:
        pred = '@p0'
        m = re.match(r'^(@!?p[0-3])\s+(.*)$', line)
        if m:
            pred, line = m.group(1), m.group(2).strip()
        parts = line.split(None, 1)
        mn = parts[0]
        ops = [o.strip() for o in parts[1].split(',')] if len(parts) > 1 else []

        if mn in ('mov', 'not', 'neg'):
            self.check_arity(ops, 2, mn)
            if mn == 'mov':
                return [Instruction(line_no, 'add', [ops[0], ops[1], 'r0'], pred)]
            if mn == 'not':
                return [Instruction(line_no, 'xori', [ops[0], ops[1], '-1'], pred)]
            return [Instruction(line_no, 'sub', [ops[0], 'r0', ops[1]], pred)]
        if mn == 'nop':
            return [Instruction(line_no, 'addi', ['r0', 'r0', '0'], pred)]
        if mn == 'j':
            self.check_arity(ops, 1, mn)
            return [Instruction(line_no, 'br.simt', ops, '@p0')]
        if mn == 'br.simt':
            self.check_arity(ops, (1, 2), mn)
            if len(ops) == 1 and pred not in UNIFORM:
                raise AsmError("br.simt 可能发散,必须显式给收敛点: br.simt label, reconv")
        return [Instruction(line_no, mn, ops, pred)]

    # ── Pass 2: 编码 ──
    def encode(self) -> None:
        for ins in self.instrs:
            try:
                ins.encoding = self.encode_one(ins)
            except Exception as e:
                self.err(ins.line_no, f"{ins.mnemonic} {', '.join(ins.ops)}: {e}")
                ins.encoding = 0

    def target_offset(self, ins: Instruction, tok: str) -> int:
        if tok in self.labels:
            return self.labels[tok] - ins.pc
        try:
            return parse_imm(tok) - ins.pc
        except AsmError:
            raise AsmError(f"未定义标签或非法跳转目标: {tok}")

    def encode_one(self, ins: Instruction) -> int:
        mn, ops, pred = ins.mnemonic, ins.ops, PRED[ins.pred]
        if mn in NO_PRED and pred:
            raise AsmError(f"{mn} 没有谓词字段,不能谓词化 (ISA §4.2)")
        if mn in MUST_P0 and pred:
            raise AsmError(f"{mn} 必须无谓词执行 (ISA §2.4/§2.5)")

        if mn in INT_ALU:
            self.check_arity(ops, 3, mn)
            return enc_r(pred, INT_ALU[mn], parse_reg(ops[2]), parse_reg(ops[1]), F3_INT,
                         parse_reg(ops[0]))

        if mn in FP_ALU:
            if mn in ('f2i', 'i2f'):
                self.check_arity(ops, 2, mn)
                return enc_r(pred, FP_ALU[mn], 0, parse_reg(ops[1]), F3_FP, parse_reg(ops[0]))
            self.check_arity(ops, 3, mn)
            return enc_r(pred, FP_ALU[mn], parse_reg(ops[2]), parse_reg(ops[1]), F3_FP,
                         parse_reg(ops[0]))

        if mn in FMA or mn == 'imad':
            self.check_arity(ops, 4, mn)
            is_fma = mn in FMA
            return enc_r4(FMA[mn] if is_fma else 0, parse_reg(ops[3]), parse_reg(ops[2]),
                          parse_reg(ops[1]), F3_FMA_FP if is_fma else F3_FMA_INT,
                          parse_reg(ops[0]))

        if mn in IMM_ALU:
            self.check_arity(ops, 3, mn)
            imm = parse_imm(ops[2])
            if mn in SHIFTS:
                if not 0 <= imm <= 31:
                    raise AsmError(f"移位量必须是 0..31: {imm}")
                if mn == 'srai':
                    imm |= 0x20
            elif not -256 <= imm <= 255:
                raise AsmError(f"imm9 越界 (有符号 ±255): {imm}")
            return enc_i(pred, imm, parse_reg(ops[1]), IMM_ALU[mn], parse_reg(ops[0]))

        if mn == 'lui':
            self.check_arity(ops, 2, mn)
            imm = parse_imm(ops[1])
            if not -0x80000 <= imm <= 0x7FFFF:
                raise AsmError(f"lui 立即数是 imm20 字段(地址>>12),应在 -0x80000..0x7FFFF;"
                               f"要装地址 0x0200_0000 请写 lui rd, 0x2000")
            return enc_u(imm, parse_reg(ops[0]), OP_LUI)

        if mn in ('ld.g', 'ld.s', 'st.g', 'st.s'):
            self.check_arity(ops, 2, mn)
            mem = parse_mem(ops[0]) or parse_mem(ops[1])
            if mem is None:
                raise AsmError(f"{mn} 需要一个内存操作数 imm(rs1)")
            other = ops[1] if parse_mem(ops[0]) else ops[0]     # 两种写法都接受
            imm, rs1 = mem
            if not -256 <= imm <= 255:
                raise AsmError(f"imm9 越界 (字节偏移 ±255): {imm}")
            if mn.startswith('ld'):
                return enc_i(pred, imm, rs1, 0b000 if mn == 'ld.g' else 0b001,
                             parse_reg(other), OP_LD)
            return enc_s(pred, imm, parse_reg(other), rs1,
                         0b000 if mn == 'st.g' else 0b001)

        if mn.startswith('setp.'):
            suffix = mn[5:]
            if suffix not in SETP:
                raise AsmError(f"未知 setp 条件: {suffix}")
            self.check_arity(ops, 3, mn)
            cond, family = SETP[suffix]
            return enc_setp(pred, cond, parse_reg(ops[2]), parse_reg(ops[1]), family,
                            parse_pdest(ops[0]))

        if mn == 'bar.sync':
            return enc_i(pred, 0, 0, F3_BAR, 0, OP_CTRL)
        if mn == 'exit':
            return enc_i(pred, 0, 0, F3_EXIT, 0, OP_CTRL)
        if mn in ('tmov', 'wmov'):
            self.check_arity(ops, 2, mn)
            want = 'tid' if mn == 'tmov' else 'wid'
            if ops[1] != want:
                raise AsmError(f"{mn} 的第二个操作数必须是 {want}")
            return enc_i(pred, 0, 0, F3_TMOV if mn == 'tmov' else F3_WMOV,
                         parse_reg(ops[0]), OP_CTRL)

        if mn == 'ipdom':
            self.check_arity(ops, 1, mn)
            off = self.target_offset(ins, ops[0])
            if off % 4:
                raise AsmError(f"ipdom 目标未 4B 对齐: {off}")
            words = off >> 2
            if not -(1 << 19) <= words < (1 << 19):
                raise AsmError(f"ipdom 目标超出 ±2MB: {off}")
            return enc_u(words, 0, OP_IPDOM)

        if mn == 'br.simt':
            self.check_arity(ops, (1, 2), mn)
            off = self.target_offset(ins, ops[0])
            if off % 4:
                raise AsmError(f"br.simt 目标未 4B 对齐: {off}")
            words = off >> 2
            if not -(1 << 21) <= words < (1 << 21):
                raise AsmError(f"br.simt 目标超出 ±8MB: {off}")
            return enc_b(pred, words)

        raise AsmError(f"未知指令: {mn}")

    # ── 内核 flags 推导 (CMDS.md §5) 与镜像 (CMDS.md §6.4) ──
    def flags(self, extra: int = 0) -> int:
        mns = {i.mnemonic for i in self.instrs}
        f = FLAG_BARRIER if 'bar.sync' in mns else 0
        if f and ({'ld.s', 'st.s'} & mns):
            f |= FLAG_SHARED
        if 'st.g' in mns:
            f |= FLAG_GLOBAL_WRITE
        return f | extra

    def image(self, extra: int = 0) -> bytes:
        code = b''.join(i.encoding.to_bytes(4, 'little') for i in self.instrs)
        hdr = (IMG_MAGIC, IMG_HDR_BYTES, IMG_HDR_BYTES + len(code), self.flags(extra),
               zlib.crc32(code) & 0xFFFFFFFF, len(code), 0, 0)
        return b''.join(w.to_bytes(4, 'little') for w in hdr) + code


def assemble(source: str) -> Assembler:
    asm = Assembler()
    asm.parse(source)
    asm.encode()
    return asm


# ── 反汇编 ──────────────────────────────────────────────────────────
def branch_target(code: int, pc: int) -> Optional[int]:
    """br.simt / ipdom 的绝对目标地址。"""
    if code & 0x7F == OP_BR:
        imm = sext(((code >> 29) & 7) << 19 | ((code >> 7) & 0x7FFFF), 22)
        return pc + imm * 4
    if code & 0x7F == OP_IPDOM:
        return pc + sext((code >> 12) & 0xFFFFF, 20) * 4
    return None


def disasm_body(code: int, pc: int) -> str:
    op = code & 0x7F
    f3 = (code >> 12) & 7
    rd, rs1, rs2 = (code >> 7) & 31, (code >> 15) & 31, (code >> 20) & 31
    has_pred = op in (OP_R, OP_IMM, OP_LD, OP_ST, OP_SETP, OP_CTRL, OP_BR)
    p = (code >> 26) & 7
    tag = (PRED_REV.get(p, '?') if has_pred else '')
    tag = '' if tag == '@p0' else tag

    if op == OP_R:
        f4 = ((code >> 29) & 7) << 1 | ((code >> 25) & 1)
        mn = INT_REV.get(f4) if f3 == F3_INT else FP_REV.get(f4) if f3 == F3_FP else None
        body = f"{mn} r{rd}, r{rs1}, r{rs2}" if mn else f"illegal R f3={f3} f4={f4}"
    elif op == OP_R4:
        f2, rs3 = (code >> 25) & 3, (code >> 27) & 31
        if f3 == F3_FMA_FP and f2 in FMA_REV:
            mn = FMA_REV[f2]
        elif f3 == F3_FMA_INT and f2 == 0:
            mn = 'imad'
        else:
            mn = None
        body = f"{mn} r{rd}, r{rs1}, r{rs2}, r{rs3}" if mn else f"illegal R4 f3={f3}"
    elif op in (OP_IMM, OP_LD):
        imm = sext(((code >> 29) & 7) << 6 | ((code >> 20) & 0x3F), 9)
        if op == OP_LD:
            body = f"{'ld.g' if f3 == 0 else 'ld.s' if f3 == 1 else 'illegal'} r{rd}, {imm}(r{rs1})"
        elif f3 == 0b101:
            body = f"{'srai' if imm & 0x20 else 'srli'} r{rd}, r{rs1}, {imm & 31}"
        elif f3 in IMM_REV:
            body = f"{IMM_REV[f3]} r{rd}, r{rs1}, {imm}"
        else:
            body = f"illegal I f3={f3}"
    elif op == OP_ST:
        imm = sext(((code >> 29) & 7) << 6 | ((code >> 25) & 1) << 5 | ((code >> 7) & 31), 9)
        body = f"{'st.g' if f3 == 0 else 'st.s' if f3 == 1 else 'illegal'} r{rs2}, {imm}(r{rs1})"
    elif op == OP_LUI:
        imm = sext((code >> 12) & 0xFFFFF, 20)
        body = f"lui r{rd}, 0x{imm & 0xFFFFF:x}          ; → 0x{(imm << 12) & 0xFFFFFFFF:08x}"
    elif op == OP_IPDOM:
        body = f"ipdom 0x{branch_target(code, pc) & 0xFFFFFFFF:04x}"
    elif op == OP_BR:
        body = f"br.simt 0x{branch_target(code, pc) & 0xFFFFFFFF:04x}"
    elif op == OP_SETP:
        cond, fam, pd = (code >> 29) & 7, (code >> 12) & 7, (code >> 7) & 7
        mn = SETP_REV.get((cond, fam))
        if mn and pd in (1, 2, 3) and not (code >> 25) & 1:
            body = f"setp.{mn} p{pd}, r{rs1}, r{rs2}"
        else:
            body = f"illegal setp cond={cond} fam={fam} p{pd}"
    elif op == OP_CTRL:
        body = {F3_BAR: 'bar.sync', F3_EXIT: 'exit',
                F3_TMOV: f'tmov r{rd}, tid', F3_WMOV: f'wmov r{rd}, wid'}.get(
                    f3, f'illegal ctrl f3={f3}')
    else:
        body = f"illegal opcode 0x{op:02x}"
    return f"{tag:5s} {body}"


def disasm_one(code: int, pc: int) -> str:
    return f"{pc:04x}: {code:08x}  {disasm_body(code, pc)}"


def disasm_report(asm: 'Assembler', flags: int) -> str:
    """--disasm 的唯一输出格式: 头部 + 逐条反汇编(带地址/机器码)+ 标签表。

    br.simt 的收敛点不在编码里(它存在前一条 ipdom 中),这里补回打印,
    于是整份输出可以直接喂回汇编器。
    """
    names = [n for b, n in sorted(FLAG_NAMES.items()) if flags & b]
    out = [f"; {len(asm.instrs)} 条指令, {len(asm.instrs) * 4} 字节代码, "
           f"镜像 {IMG_HDR_BYTES + len(asm.instrs) * 4} 字节",
           f"; flags = 0x{flags:x}" + (f" ({', '.join(names)})" if names else ''),
           "; disassembly"]
    for i, ins in enumerate(asm.instrs):
        line = disasm_one(ins.encoding, ins.pc)
        if ins.mnemonic == 'br.simt' and ins.pred not in UNIFORM:
            prev = asm.instrs[i - 1] if i else None
            reconv = (branch_target(prev.encoding, prev.pc)
                      if prev and prev.mnemonic == 'ipdom'
                      else asm.labels.get(ins.ops[1]) if len(ins.ops) > 1 else None)
            if reconv is not None:
                line += f", 0x{reconv:04x}"
        out.append(line)
    if asm.labels:
        out.append("; labels")
        out += [f";   {lbl}: 0x{pc:04x}"
                for lbl, pc in sorted(asm.labels.items(), key=lambda x: x[1])]
    return '\n'.join(out) + '\n'


class _ArgParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        m = re.search(r'unrecognized arguments: (--\S+)', message)
        if m:
            opts = sorted({o for a in self._actions for o in a.option_strings})
            near = difflib.get_close_matches(m.group(1).split('=')[0], opts, n=1, cutoff=0.6)
            if near:
                message += f"（是不是想用 {near[0]}?）"
        super().error(message)


def main() -> None:
    ap = _ArgParser(description='SimpleGPU Assembler (ISA.md v0.2)')
    ap.add_argument('input', nargs='?')
    ap.add_argument('-o', '--output')
    ap.add_argument('--format', choices=['hex', 'bin', 'coe', 'image'], default='hex')
    ap.add_argument('--disasm', nargs='?', const='', metavar='FILE',
                    help='反汇编: 不带值 = 带地址/机器码打印到 stderr;'
                         '带 FILE = 写入该文件(- 表示 stdout),可再喂回汇编器')
    ap.add_argument('--flags', help='覆盖自动推导的 launch_flags (0x 前缀)')
    ap.add_argument('--single-sm', action='store_true', help='置 REQUIRES_SINGLE_SM')
    a = ap.parse_args()
    if not a.input:
        ap.error('缺少输入文件(用了 --disasm FILE 时,请把输入文件写在前面:'
                 ' assembly.py in.S --disasm out.txt)')

    asm = assemble(open(a.input).read())
    if asm.errors:
        for e in asm.errors:
            print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    extra = (parse_imm(a.flags) if a.flags else 0) | (FLAG_SINGLE_SM if a.single_sm else 0)
    flags = asm.flags(extra)
    if a.format == 'image':
        data = asm.image(extra)
    else:
        fmt = {'hex': '{:08x}', 'bin': '{:032b}', 'coe': '{:08x},'}[a.format]
        data = '\n'.join(fmt.format(i.encoding) for i in asm.instrs) + '\n'

    if a.output:
        with open(a.output, 'wb' if isinstance(data, bytes) else 'w') as f:
            f.write(data)
    elif a.disasm == '-':
        pass                                        # stdout 留给反汇编
    elif isinstance(data, bytes):
        sys.stdout.buffer.write(data)
    else:
        sys.stdout.write(data)

    if a.disasm is None:
        return
    report = disasm_report(asm, flags)              # 三种去向内容完全一致
    if not a.disasm:                                # --disasm         → stderr
        sys.stderr.write(report)
    elif a.disasm == '-':                           # --disasm -       → stdout
        sys.stdout.write(report)
    else:                                           # --disasm FILE    → 文件
        with open(a.disasm, 'w') as f:
            f.write(report)
        print(f"; 反汇编已写入 {a.disasm} ({len(asm.instrs)} 条指令)", file=sys.stderr)


if __name__ == '__main__':
    main()
