#!/usr/bin/env python3
"""
SimpleGPU 自定义语言(sg)编译器
=============================================
把 grammer.md 定义的结构化 SIMT 汇编编译成 ISA.md 的指令序列, 再交给 assembly.py 汇编成内核镜像。

设计原则(照 grammer.md §1): **只做模板展开、寄存器分配和标签解析, 不做复杂优化** ——
每个语言构造对应一段固定的指令模板, 生成的汇编能一眼对应回源码。

用法:
    python3 compiler.py sg/clear.sg -o build/clear.sg.S      # 生成汇编
    python3 compiler.py sg/clear.sg --image build/clear.bin  # 直接出内核镜像
    python3 compiler.py sg/*.sg --image-dir build/           # 批量
    python3 compiler.py sg/abs.sg --disasm                   # 编译 + 反汇编(人工核对)

错误码与检查见 grammer.md §9(E001..E014); 模板展开见 §6; 内置函数见 §7。
"""

import argparse
import os
import re
import struct
import sys
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import assembly  # noqa: E402  复用仓库里的汇编器: 插入指令校验 + 镜像生成

# ── grammer.md §9 错误码 ────────────────────────────────────────────────
ERRORS = {
    'E001': '寄存器溢出', 'E002': '谓词溢出', 'E003': '未知指令/函数',
    'E004': '类型不匹配', 'E005': '循环次数非标量', 'E006': '发散路径里 bar()',
    'E007': '未定义标签', 'E008': '重复定义标签', 'E009': '未定义符号',
    'E010': '未对齐地址', 'E011': '共享内存溢出', 'E012': 'FP 除法/开方不支持',
    'E013': '共享内存跨 warp 未同步', 'E014': '插入指令非法',
}

SHARED_SIZE = 16 * 1024          # 每 SM 16KB(ISA §3.2)
NUM_REGS = 32                    # r0..r31, r0 恒零
NUM_PREDS = 4                    # p0..p3, p0 恒真不可写
WORD = 4


class SgError(Exception):
    """带错误码的编译错误(code=None 表示语法/用法错误, 不在 §9 的表里)。"""

    def __init__(self, code: Optional[str], line: int, msg: str):
        self.code, self.line, self.msg = code, line, msg
        super().__init__(f"{code}: {msg}" if code else msg)

    def format(self, path: str) -> str:
        tag = f"{self.code} {ERRORS[self.code]}" if self.code else "语法错误"
        return f"{path}:{self.line}: {tag}: {self.msg}"


# ────────────────────────────── 词法(§2) ──────────────────────────────

KEYWORDS = {'kernel', 'var', 'param', 'goto', 'when', 'uniform', 'if', 'else', 'end',
            'while', 'bar', 'exit', 'int', 'float', 'floar', '__shared__', '__global__',
            'ass', 'per_warp', 'shared'}

SYMBOLS = ['-:', '==', '!=', '<=', '>=', '&&', '||', '>>>', '<<', '>>',
           '=', '+', '-', '*', '/', '%', '&', '|', '^', '~', '<', '>',
           '(', ')', '[', ']', ',', ':', '@']

NUM_RE = re.compile(r'0[xX][0-9a-fA-F]+|(?:\d+\.\d*|\.\d+|\d+)[fF]?')
ID_RE = re.compile(r'[A-Za-z_]\w*')


@dataclass
class Token:
    kind: str          # NUM | FLOAT | ID | KW | SYM | NEWLINE | EOF
    text: str
    line: int
    value: object = None


def tokenize(src: str) -> List[Token]:
    toks: List[Token] = []
    lines = src.splitlines()
    for lineno, raw in enumerate(lines, 1):
        line = raw.split('#', 1)[0]                     # `#` 到行尾是注释(§2)
        i = 0
        while i < len(line):
            ch = line[i]
            if ch in ' \t\r':
                i += 1
                continue
            m = NUM_RE.match(line, i)
            if m:
                txt = m.group(0)
                is_float = txt[-1] in 'fF' or '.' in txt
                if is_float:
                    val = float(txt.rstrip('fF'))
                else:
                    val = int(txt, 0)
                toks.append(Token('FLOAT' if is_float else 'NUM', txt, lineno, val))
                i = m.end()
                continue
            m = ID_RE.match(line, i)
            if m:
                txt = m.group(0)
                toks.append(Token('KW' if txt in KEYWORDS else 'ID', txt, lineno, txt))
                i = m.end()
                continue
            for sym in SYMBOLS:
                if line.startswith(sym, i):
                    toks.append(Token('SYM', sym, lineno, sym))
                    i += len(sym)
                    break
            else:
                raise SgError(None, lineno, f"无法识别的字符 {ch!r}")
        toks.append(Token('NEWLINE', '', lineno))
    toks.append(Token('EOF', '', len(lines) + 1))
    return toks


# ────────────────────────────── 语法树 ──────────────────────────────

@dataclass
class Expr:
    line: int


@dataclass
class Num(Expr):
    value: object
    is_float: bool


@dataclass
class Sym(Expr):            # @name / @name[off]
    name: str
    off: Optional[Expr] = None


@dataclass
class Var(Expr):
    name: str
    index: Optional[Expr] = None


@dataclass
class Call(Expr):
    name: str
    args: List[Expr]


@dataclass
class Bin(Expr):
    op: str
    lhs: Expr
    rhs: Expr


@dataclass
class Un(Expr):
    op: str
    operand: Expr


@dataclass
class Stmt:
    line: int


@dataclass
class Assign(Stmt):
    target: Var
    value: Expr


@dataclass
class CallStmt(Stmt):
    call: Call


@dataclass
class Goto(Stmt):
    label: str
    cond: Optional[Expr] = None
    uniform: bool = False


@dataclass
class WhenStmt(Stmt):
    cond: Expr
    body: Stmt


@dataclass
class If(Stmt):
    cond: Expr
    then_body: List[Stmt]
    else_body: List[Stmt]


@dataclass
class While(Stmt):
    cond: Expr
    body: List[Stmt]


@dataclass
class LabelStmt(Stmt):
    name: str


@dataclass
class AssStmt(Stmt):
    text: str


@dataclass
class VarDecl(Stmt):
    names: List[str]
    vtype: str


@dataclass
class ParamDecl(Stmt):
    names: List[str]
    vtype: str
    base: str
    offs: List[Expr]


@dataclass
class ArrayDecl(Stmt):
    name: str
    vtype: str
    length: int
    scope: str          # per_warp | shared
    space: str          # shared | global


@dataclass
class Kernel:
    name: str
    decls: List[Stmt]
    body: List[Stmt]


class Parser:
    def __init__(self, toks: List[Token]):
        self.toks = toks
        self.pos = 0

    # ── 记号流 ──
    def peek(self, k: int = 0) -> Token:
        return self.toks[min(self.pos + k, len(self.toks) - 1)]

    def next(self) -> Token:
        t = self.toks[self.pos]
        self.pos += 1
        return t

    def skip_newlines(self) -> None:
        while self.peek().kind == 'NEWLINE':
            self.pos += 1

    def at(self, text: str) -> bool:
        t = self.peek()
        return t.text == text and t.kind in ('SYM', 'KW', 'ID')

    def expect(self, text: str) -> Token:
        t = self.peek()
        if t.text != text or t.kind not in ('SYM', 'KW', 'ID'):
            raise SgError(None, t.line, f"期望 {text!r}, 实际 {t.text!r}")
        return self.next()

    def expect_id(self) -> Token:
        t = self.peek()
        if t.kind not in ('ID', 'KW'):
            raise SgError(None, t.line, f"期望标识符, 实际 {t.text!r}")
        return self.next()

    def at_line_end(self) -> bool:
        return self.peek().kind in ('NEWLINE', 'EOF')

    def end_statements(self) -> None:
        if not self.at_line_end():
            t = self.peek()
            raise SgError(None, t.line, f"语句之后出现多余记号 {t.text!r}")

    # ── 顶层: 常量定义 + kernel ──
    def parse(self) -> Tuple[Dict[str, int], Kernel]:
        consts: Dict[str, int] = {}
        kernel: Optional[Kernel] = None
        self.skip_newlines()
        while self.peek().kind != 'EOF':
            t = self.peek()
            if t.kind in ('ID', 'KW') and self.peek(1).text == '-:':
                name = self.next().text
                self.next()
                val = self.next()
                if val.kind not in ('NUM', 'FLOAT'):
                    raise SgError(None, val.line, "常量值必须是数字")
                if name in consts:
                    raise SgError(None, t.line, f"常量重复定义: {name}")
                consts[name] = int(val.value)
                self.end_statements()
            elif t.text == 'kernel':
                if kernel is not None:
                    raise SgError(None, t.line, "一个文件只能有一个 kernel(§8: 无函数调用)")
                kernel = self.parse_kernel()
            else:
                raise SgError(None, t.line, f"顶层只允许 `-:` 常量定义与 kernel, 实际 {t.text!r}")
            self.skip_newlines()
        if kernel is None:
            raise SgError(None, 1, "文件里没有 kernel")
        return consts, kernel

    def parse_kernel(self) -> Kernel:
        self.expect('kernel')
        name = self.expect_id().text
        decls: List[Stmt] = []
        body: List[Stmt] = []
        self.skip_newlines()
        while True:
            t = self.peek()
            if t.kind == 'EOF':
                raise SgError(None, t.line, "kernel 没有对应的 `end`")
            if t.text == 'end':
                self.next()
                self.end_statements()
                break
            st = self.parse_statement()
            if st is None:
                continue
            (decls if isinstance(st, (VarDecl, ParamDecl, ArrayDecl)) else body).append(st)
            self.skip_newlines()
        return Kernel(name, decls, body)

    # ── 语句 ──
    def parse_block(self, stops: Tuple[str, ...] = ('end',)) -> List[Stmt]:
        out: List[Stmt] = []
        self.skip_newlines()
        while True:
            t = self.peek()
            if t.kind == 'EOF':
                raise SgError(None, t.line, "块没有 `end` 收尾")
            if t.text in stops and t.kind in ('KW', 'ID'):
                return out
            st = self.parse_statement()
            if st is not None:
                out.append(st)
            self.skip_newlines()

    def parse_statement(self) -> Optional[Stmt]:
        t = self.peek()
        if t.kind == 'NEWLINE':
            self.next()
            return None
        if t.kind in ('ID', 'KW') and self.peek(1).text == ':' and self.peek(1).kind == 'SYM':
            name = self.next().text
            self.next()
            return LabelStmt(t.line, name)
        if t.text == 'var':
            return self.parse_var()
        if t.text == 'param':
            return self.parse_param()
        if t.text in ('__shared__', '__global__'):
            return self.parse_array(t.text)
        if t.text == 'ass':
            self.next()
            parts = []
            while not self.at_line_end():
                parts.append(self.next().text)
            return AssStmt(t.line, ' '.join(parts))
        if t.text == 'goto':
            self.next()
            lbl = self.expect_id()
            self.end_statements()
            return Goto(t.line, lbl.text)
        if t.text == 'when':
            self.next()
            cond = self.parse_expr()
            if self.at(':'):
                self.next()
                body = self.parse_statement()
                if body is None:
                    raise SgError(None, t.line, "`when ... :` 之后需要有语句")
                return WhenStmt(t.line, cond, body)
            self.expect('goto')
            lbl = self.expect_id()
            self.end_statements()
            return Goto(t.line, lbl.text, cond, False)
        if t.text == 'uniform':
            self.next()
            self.expect('when')
            cond = self.parse_expr()
            self.expect('goto')
            lbl = self.expect_id()
            self.end_statements()
            return Goto(t.line, lbl.text, cond, True)
        if t.text == 'if':
            self.next()
            cond = self.parse_expr()
            self.expect(':')
            then_body = self.parse_block(stops=('end', 'else'))
            else_body: List[Stmt] = []
            if self.at('else'):
                self.next()
                self.expect(':')
                else_body = self.parse_block(stops=('end',))
            self.expect('end')
            self.end_statements()
            return If(t.line, cond, then_body, else_body)
        if t.text == 'while':
            self.next()
            cond = self.parse_expr()
            self.expect(':')
            body = self.parse_block(stops=('end',))
            self.expect('end')
            self.end_statements()
            return While(t.line, cond, body)
        if t.text == 'exit':
            self.next()
            self.end_statements()
            return CallStmt(t.line, Call(t.line, 'exit', []))
        if t.text == 'bar':
            self.next()
            self.expect('(')
            self.expect(')')
            self.end_statements()
            return CallStmt(t.line, Call(t.line, 'bar', []))
        if t.kind in ('ID', 'KW'):
            name = self.next().text
            if self.at('('):
                call = self.parse_call(name, t.line)
                self.end_statements()
                return CallStmt(t.line, call)
            target = Var(t.line, name)
            if self.at('['):
                self.next()
                target.index = self.parse_expr()
                self.expect(']')
            self.expect('=')
            value = self.parse_expr()
            self.end_statements()
            return Assign(t.line, target, value)
        raise SgError(None, t.line, f"无法解析的语句: {t.text!r}")

    def parse_var(self) -> Stmt:
        t = self.expect('var')
        names = [self.expect_id().text]
        while self.at(','):
            self.next()
            names.append(self.expect_id().text)
        vtype = 'int'
        if self.at(':'):
            self.next()
            vtype = self.expect_id().text
        self.end_statements()
        if vtype == 'floar':                    # grammer.md §2 的关键字表里写的是 floar
            vtype = 'float'
        if vtype not in ('int', 'float'):
            raise SgError('E004', t.line, f"未知类型 {vtype!r}(只支持 int / float)")
        return VarDecl(t.line, names, vtype)

    def parse_param(self) -> Stmt:
        t = self.expect('param')
        names, types, bases, offs = [], [], [], []
        while True:
            names.append(self.expect_id().text)
            self.expect(':')
            types.append(self.expect_id().text)
            self.expect('@')
            bases.append(self.expect_id().text)
            off: Expr = Num(t.line, 0, False)
            if self.at('['):
                self.next()
                off = self.parse_expr()
                self.expect(']')
            offs.append(off)
            if self.at(','):
                self.next()
                continue
            break
        self.end_statements()
        if len(set(types)) != 1 or len(set(bases)) != 1:
            raise SgError('E004', t.line, "同一条 param 声明里的类型与基址必须一致")
        for ty in types:
            if ty not in ('int', 'float'):
                raise SgError('E004', t.line, f"未知类型 {ty!r}")
        return ParamDecl(t.line, names, types[0], bases[0], offs)

    def parse_array(self, space: str) -> Stmt:
        t = self.next()
        name = self.expect_id().text
        self.expect(':')
        vtype = self.expect_id().text
        self.expect('[')
        ln = self.next()
        if ln.kind != 'NUM':
            raise SgError(None, ln.line, "数组长度必须是整数字面量")
        self.expect(']')
        scope = 'per_warp'
        if self.peek().kind in ('ID', 'KW') and self.peek().text in ('per_warp', 'shared'):
            scope = self.next().text
        self.end_statements()
        if vtype == 'floar':
            vtype = 'float'
        if vtype not in ('int', 'float'):
            raise SgError('E004', t.line, f"未知类型 {vtype!r}")
        if int(ln.value) <= 0:
            raise SgError(None, ln.line, "数组长度必须 ≥ 1")
        return ArrayDecl(t.line, name, vtype, int(ln.value), scope,
                         'shared' if space == '__shared__' else 'global')

    def parse_call(self, name: str, line: int) -> Call:
        self.expect('(')
        args: List[Expr] = []
        if not self.at(')'):
            args.append(self.parse_expr())
            while self.at(','):
                self.next()
                args.append(self.parse_expr())
        self.expect(')')
        return Call(line, name, args)

    # ── 表达式 ──
    PRECEDENCE = [('||',), ('&&',), ('|',), ('^',), ('&',), ('==', '!='),
                  ('<', '<=', '>', '>='), ('<<', '>>', '>>>'),
                  ('+', '-'), ('*', '/', '%')]

    def parse_expr(self, level: int = 0) -> Expr:
        if level >= len(self.PRECEDENCE):
            return self.parse_unary()
        lhs = self.parse_expr(level + 1)
        while True:
            t = self.peek()
            if t.kind == 'SYM' and t.text in self.PRECEDENCE[level]:
                self.next()
                rhs = self.parse_expr(level + 1)
                lhs = Bin(t.line, t.text, lhs, rhs)
            else:
                return lhs

    def parse_unary(self) -> Expr:
        t = self.peek()
        if t.kind == 'SYM' and t.text in ('-', '+', '!', '~'):
            self.next()
            operand = self.parse_unary()
            if t.text == '+':
                return operand
            if t.text == '!':
                return Un(t.line, 'not', operand)
            if t.text == '~':
                return Un(t.line, 'bitnot', operand)
            if isinstance(operand, Num):
                return Num(t.line, -operand.value, operand.is_float)
            return Un(t.line, '-', operand)
        return self.parse_primary()

    def parse_primary(self) -> Expr:
        t = self.next()
        if t.kind in ('NUM', 'FLOAT'):
            return Num(t.line, t.value, t.kind == 'FLOAT')
        if t.kind == 'SYM' and t.text == '@':
            name = self.expect_id().text
            off = None
            if self.at('['):
                self.next()
                off = self.parse_expr()
                self.expect(']')
            return Sym(t.line, name, off)
        if t.kind == 'SYM' and t.text == '(':
            e = self.parse_expr()
            self.expect(')')
            return e
        if t.kind in ('ID', 'KW'):
            if self.at('('):
                return self.parse_call(t.text, t.line)
            v = Var(t.line, t.text)
            if self.at('['):
                self.next()
                v.index = self.parse_expr()
                self.expect(']')
            return v
        raise SgError(None, t.line, f"表达式里出现意外记号 {t.text!r}")


# ────────────────────────────── 符号/类型 ──────────────────────────────

@dataclass
class VarInfo:
    name: str
    vtype: str
    line: int
    reg: int = 0
    uniform: bool = True            # 值是否所有 lane/warp 相同 ⇒ bar()/循环次数的判据(§6.8/§8)
    is_array: bool = False
    base: int = 0                   # 数组基址: 共享内存偏移 或 全局地址
    length: int = 0
    scope: str = 'per_warp'
    space: str = 'shared'


# 内置函数(§7): 名字 → (参数个数, 返回类型 / None = 由上下文决定)
BUILTINS = {
    'tid': (0, 'int'), 'wid': (0, 'int'),
    'ldx': (1, None), 'lds': (1, None), 'stx': (2, None), 'sts': (2, None),
    'ceildiv': (2, 'int'), 'f2i': (1, 'int'), 'i2f': (1, 'float'),
    'bar': (0, None), 'exit': (0, None),
}

# ISA 里没有谓词字段的指令(ISA §4.2): 谓词化时只能谓词化"写回/访存"那一端
KNOWN_MNEMONICS = (set(assembly.INT_ALU) | set(assembly.FP_ALU) | set(assembly.FMA) |
                   set(assembly.IMM_ALU) | set(assembly.SETP) |
                   {'mov', 'not', 'neg', 'nop', 'j', 'ipdom', 'br.simt', 'bar.sync',
                    'exit', 'tmov', 'wmov', 'imad', 'lui', 'ld.g', 'ld.s', 'st.g', 'st.s'})


def _walk(node):
    """深度遍历语法树里的所有节点(表达式与语句)。"""
    yield node
    if is_dataclass(node):
        for f in fields(node):
            v = getattr(node, f.name)
            if isinstance(v, (Expr, Stmt)):
                yield from _walk(v)
            elif isinstance(v, list):
                for x in v:
                    if isinstance(x, (Expr, Stmt)):
                        yield from _walk(x)


class FuncGen:
    """一个 kernel 的代码生成(§5/§6/§7 的模板展开 + 寄存器/谓词分配)。"""

    def __init__(self, kernel: Kernel, consts: Dict[str, int], global_base: int, path: str):
        self.k = kernel
        self.consts = dict(consts)
        self.global_base = global_base
        self.path = path
        self.out: List[str] = []
        self.vars: Dict[str, VarInfo] = {}
        self.labels: Dict[str, int] = {}
        self.label_refs: List[Tuple[str, int]] = []
        self.used_regs: List[int] = []
        self.used_preds: List[int] = []
        self.free_regs = list(range(1, NUM_REGS))
        self.free_preds = list(range(1, NUM_PREDS))
        self.temp_regs: List[int] = []
        self.const_cache: Dict[int, int] = {}
        self.pinned: Dict[int, int] = {}          # 常驻常量(见 plan_pinned)
        self.seq = 0
        self.cur_pred: Optional[int] = None          # §6.4: 当前语句是否被谓词化
        self.shared_total = 0
        self.shared_arrays: List[VarInfo] = []
        self.uses_bar = False
        self.in_divergent = False
        self.n_instr = 0
        self.comments = True

    # ── 输出 ──
    def emit(self, text: str, comment: str = '') -> None:
        line = f"    {text}"
        if comment and self.comments:
            line = f"{line:<36}; {comment}"
        self.out.append(line)
        self.n_instr += 1

    def label(self, name: str) -> None:
        self.out.append(f"{name}:{' ' * max(1, 12 - len(name))}; "
                        f"{'用户标签' if not name.startswith('__') else '编译器内部标签'}")

    def new_label(self, kind: str) -> str:
        self.seq += 1
        return f"__{kind}{self.seq}"

    # ── 寄存器 / 谓词 ──
    def alloc_reg(self, line: int, why: str) -> int:
        if not self.free_regs:
            raise SgError('E001', line, f"寄存器溢出: 32 个寄存器用尽({why}; §8 要求变量放寄存器)")
        r = self.free_regs.pop(0)
        if r not in self.used_regs:
            self.used_regs.append(r)
        return r

    def temp(self, line: int, why: str) -> int:
        r = self.alloc_reg(line, why)
        self.temp_regs.append(r)
        return r

    def release(self, r: int) -> None:
        if r in self.temp_regs:
            self.temp_regs.remove(r)
            if r not in self.free_regs:
                self.free_regs.append(r)
                self.free_regs.sort()

    def save_temps(self) -> List[int]:
        return list(self.temp_regs)

    def restore_temps(self, mark: List[int]) -> None:
        for r in list(self.temp_regs):
            if r not in mark:
                self.release(r)
        for v, r in list(self.const_cache.items()):
            if r not in self.temp_regs:
                del self.const_cache[v]

    def alloc_pred(self, line: int, why: str) -> int:
        if not self.free_preds:
            raise SgError('E002', line, f"谓词溢出: p1..p3 全部在用({why}; p0 恒真不可写)")
        p = self.free_preds.pop(0)
        if p not in self.used_preds:
            self.used_preds.append(p)
        return p

    def free_pred(self, p: int) -> None:
        if p not in self.free_preds:
            self.free_preds.append(p)
            self.free_preds.sort()

    # ── 常量折叠(只折叠纯整型常量表达式; float 不折, 免得舍入语义被改写) ──
    def const_int(self, e: Expr) -> Optional[int]:
        if isinstance(e, Num):
            return None if e.is_float else int(e.value)
        if isinstance(e, Sym) and e.off is None and e.name in self.consts:
            return int(self.consts[e.name])
        if isinstance(e, Var) and e.index is None and e.name in self.consts \
                and e.name not in self.vars:            # 偏移里直接写常量名: @const[slot]
            return int(self.consts[e.name])
        if isinstance(e, Un):
            v = self.const_int(e.operand)
            if v is None:
                return None
            return {'-': lambda x: -x, '~': lambda x: ~x}.get(e.op, lambda x: None)(v)
        if isinstance(e, Bin):
            a, b = self.const_int(e.lhs), self.const_int(e.rhs)
            if a is None or b is None:
                return None
            try:
                v = {'+': a + b, '-': a - b, '*': a * b, '&': a & b, '|': a | b, '^': a ^ b,
                     '<<': a << b, '>>': a >> b, '>>>': (a & 0xFFFFFFFF) >> b,
                     '/': int(a / b) if (b and (a % b == 0 or a * b > 0)) else None,
                     '%': a % b if b else None}[e.op]
            except (KeyError, OverflowError):
                return None
            return None if v is None else int(v)
        return None

    @staticmethod
    def _lui_lit(hi20: int) -> str:
        """lui 的 imm20 按 ISA 是**有符号**的(rd = sext(imm20) << 12), 汇编器要求写成 -0x80000 这种形式。"""
        hi20 &= 0xFFFFF
        return f"0x{hi20:x}" if hi20 < 0x80000 else f"-0x{0x100000 - hi20:x}"

    # ── 常量装载(ISA §5.5: 非 4KB 对齐的常量要 2~3 条指令) ──
    def load_imm(self, rd: int, value: int, comment: str = '') -> None:
        v = value & 0xFFFFFFFF
        sval = v - (1 << 32) if v >= (1 << 31) else v
        if -256 <= sval <= 255:
            self.emit(f"addi r{rd}, r0, {sval}", comment or f"= {sval}")
            return
        hi20, lo12 = (v >> 12) & 0xFFFFF, v & 0xFFF
        if lo12 == 0:
            self.emit(f"lui r{rd}, {self._lui_lit(hi20)}", comment or f"= 0x{v:08x}")
            return
        if lo12 <= 255:
            self.emit(f"lui r{rd}, {self._lui_lit(hi20)}")
            self.emit(f"addi r{rd}, r{rd}, {lo12}", comment or f"= 0x{v:08x}")
            return
        if lo12 >= 3840:                        # lui(hi+1) + addi(lo-4096) ∈ [-256,-1]
            self.emit(f"lui r{rd}, {self._lui_lit(hi20 + 1)}")
            self.emit(f"addi r{rd}, r{rd}, {lo12 - 4096}", comment or f"= 0x{v:08x}")
            return
        # 一般情形: lui + (低 12 位拆成"高位块 << 8"与低 8 位)
        rt = self.alloc_reg(0, '常量构造')
        self.temp_regs.append(rt)
        self.emit(f"lui r{rd}, {self._lui_lit(hi20)}")
        self.emit(f"addi r{rt}, r0, {lo12 >> 8}", "常量构造: 低 12 位的高位块")
        self.emit(f"slli r{rt}, r{rt}, 8")
        self.emit(f"add r{rd}, r{rd}, r{rt}")
        self.emit(f"addi r{rd}, r{rd}, {lo12 & 0xFF}", comment or f"= 0x{v:08x}(4 条)")
        self.release(rt)

    def const_value(self, value: int, line: int, dest: Optional[int] = None
                    ) -> Tuple[int, str, bool]:
        """把一个整型常量放进寄存器(优先复用常驻寄存器, 其次目标寄存器, 最后临时寄存器)。"""
        v = int(value) & 0xFFFFFFFF
        sv = v - (1 << 32) if v >= (1 << 31) else v
        if v in self.pinned:
            return self.pinned[v], 'int', True
        if dest is not None:
            if sv == 0:
                return 0, 'int', True
            self.load_imm(dest, sv)
            return dest, 'int', True
        return self.const_reg(sv, line), 'int', True

    # ── 常驻常量: 把出现最多的两个整型常量长期占住寄存器 ──
    def plan_pinned(self) -> None:
        """统计整型常量出现次数, 最多常驻 2 个(只在还剩 ≥4 个空闲寄存器时)。

        奖励是多轮循环里的重复装载: `imad rd, ri, r4, rb` 需要"4"在寄存器里,
        每轮都 `addi rt, r0, 4` 是纯浪费。
        """
        from collections import Counter
        cnt: Counter = Counter()
        for node in _walk(self.k):
            if isinstance(node, Expr) and not isinstance(node, (Num, Sym)):
                cv = self.const_int(node)
                if cv is not None:
                    cnt[cv & 0xFFFFFFFF] += 1
            elif isinstance(node, Num) and not node.is_float:
                cnt[int(node.value) & 0xFFFFFFFF] += 1
            elif isinstance(node, Sym) and node.off is None and node.name in self.consts:
                cnt[int(self.consts[node.name]) & 0xFFFFFFFF] += 1
        for value, _times in cnt.most_common(2):
            if value == 0 or len(self.free_regs) < 5:       # 变量优先: 留够寄存器
                continue
            r = self.alloc_reg(0, '常驻常量')
            self.load_imm(r, value, f"常驻常量 0x{value:08x}")
            self.pinned[value] = r

    def const_reg(self, value: int, line: int) -> int:
        """常量放寄存器, 同一条语句内复用(§5 的 imad/fma 需要寄存器形式的操作数)。"""
        v = int(value) & 0xFFFFFFFF
        if v in self.pinned:
            return self.pinned[v]
        if v in self.const_cache and self.const_cache[v] in self.temp_regs:
            return self.const_cache[v]
        r = self.temp(line, '常量寄存器')
        self.load_imm(r, value)
        self.const_cache[value] = r
        return r

    # ── 声明 ──
    def declare(self) -> None:
        for d in self.k.decls:
            if isinstance(d, VarDecl):
                for n in d.names:
                    if n in self.vars:
                        continue
                    self.vars[n] = VarInfo(n, d.vtype, d.line, self.alloc_reg(d.line, f"变量 {n}"))
        for d in self.k.decls:
            if isinstance(d, ArrayDecl):
                self.declare_array(d)
        for d in self.k.decls:                  # param 可能引用已声明的名字
            if isinstance(d, ParamDecl):
                for n in d.names:
                    if n not in self.vars:
                        self.vars[n] = VarInfo(n, d.vtype, d.line, self.alloc_reg(d.line, f"参数 {n}"))

    def declare_array(self, d: ArrayDecl) -> None:
        if d.name in self.vars:
            raise SgError(None, d.line, f"{d.name} 重复声明")
        if d.space == 'shared':
            size = d.length * WORD * (4 if d.scope == 'per_warp' else 1)
            off = self.shared_total
            self.shared_total += size
            if self.shared_total > SHARED_SIZE:
                raise SgError('E011', d.line,
                              f"共享内存溢出: {self.shared_total}B > 16KB/SM"
                              f"(per_warp 数组按 ×4 计, §4.4/§8)")
            v = VarInfo(d.name, d.vtype, d.line, is_array=True, base=off, length=d.length,
                        scope=d.scope, space='shared', uniform=(d.scope == 'shared'))
            self.shared_arrays.append(v)
        else:
            v = VarInfo(d.name, d.vtype, d.line, is_array=True, base=self.global_base,
                        length=d.length, scope='shared', space='global', uniform=False)
            self.global_base += d.length * WORD
        self.vars[d.name] = v

    # ── 表达式 → (寄存器, 类型, 是否 uniform) ──
    def gen_expr(self, e: Expr, want: Optional[str] = None,
                 dest: Optional[int] = None) -> Tuple[int, str, bool]:
        """dest 给定时, 能把结果直接算进这个寄存器(省掉一次拷贝)。"""
        if dest is not None and isinstance(e, (Num, Sym)):
            return self.gen_leaf_into(e, want, dest)
        if not isinstance(e, (Num, Sym)):
            cv = self.const_int(e)                      # 常量折叠: 4*8、@base+8 之类
            if cv is not None:
                return self.const_value(cv, e.line, dest)
        if isinstance(e, Num):
            t = 'float' if e.is_float else 'int'
            if float(e.value) == 0.0:            # 0/0.0 的位型都是 0 ⇒ 直接用 r0(§3.1)
                if want == 'float' and t == 'int':
                    pass                          # 走下面的提升路径(0 → 0.0 也是 0)
                else:
                    return 0, t, True
            if want == 'float' and t == 'int':
                r = self.temp(e.line, '字面量提升')
                self.load_imm(r, int(e.value))
                self.emit(f"i2f r{r}, r{r}", "int → float 提升(§3)")
                return r, 'float', True
            if want == 'int' and t == 'float':
                raise SgError('E004', e.line, f"浮点字面量 {e.value} 不能当 int 用(需要 f2i())")
            r = self.temp(e.line, '字面量')
            if t == 'float':
                self.load_imm(r, int.from_bytes(struct.pack('<f', float(e.value)), 'little'),
                              f"= {e.value}(FP32 位型)")
            else:
                self.load_imm(r, int(e.value))
            return r, t, True
        if isinstance(e, Sym):
            if e.name not in self.consts:
                raise SgError('E009', e.line, f"未定义符号 @{e.name}")
            base = self.consts[e.name]
            if e.off is None:
                if base % 4:
                    raise SgError('E010', e.line, f"常量 {e.name}=0x{base:x} 未 4B 对齐")
                r = self.temp(e.line, '常量')
                self.load_imm(r, base, f"@{e.name}")
                return r, 'int', True
            off = self.const_int(e.off)
            if off is None:
                raise SgError('E010', e.line, "常量区偏移必须是编译期常量(要检查对齐)")
            if off % 4:
                raise SgError('E010', e.line, f"@{e.name}[{off}] 未 4B 对齐")
            if off < 0:
                raise SgError('E010', e.line, f"@{e.name}[{off}] 偏移为负")
            ty = want or 'int'
            r = self.temp(e.line, '常量区载入')
            cr = self.const_reg(base, e.line)
            self.emit_pred(f"ld.g r{r}, {off}(r{cr})", f"@{e.name}[{off}] → {ty}")
            return r, ty, True
        if isinstance(e, Var):
            if e.name not in self.vars:
                if e.name in BUILTINS:
                    raise SgError(None, e.line, f"{e.name} 是内置函数, 要写成 {e.name}(...)")
                raise SgError(None, e.line, f"未声明的变量 {e.name}")
            v = self.vars[e.name]
            if not v.is_array:
                return v.reg, v.vtype, v.uniform
            addr = self.array_addr(v, e.index, e.line)
            if e.index is None:
                return addr, 'int', v.uniform
            rd = self.temp(e.line, '数组载入')
            op = 'ld.s' if v.space == 'shared' else 'ld.g'
            ty = want or v.vtype
            self.emit_pred(f"{op} r{rd}, 0(r{addr})", f"{v.name}[i] → {ty}")
            return rd, ty, False
        if isinstance(e, Un):
            if e.op == 'not':
                return self.gen_not(e)
            if e.op == 'bitnot':
                r, t, uni = self.gen_expr(e.operand, 'int')
                if t != 'int':
                    raise SgError('E004', e.line, "~ 只支持 int(按位取反)")
                rd = dest if dest is not None else self.temp(e.line, '按位取反')
                self.emit(f"xori r{rd}, r{r}, -1", "int 按位取反 = 异或全 1(§3)")
                return rd, 'int', uni
            r, t, uni = self.gen_expr(e.operand, want)
            rd = dest if dest is not None else self.temp(e.line, '取负')
            if t == 'int':
                self.emit(f"sub r{rd}, r0, r{r}", "int 取负 = 0 - x(§3)")
                return rd, 'int', uni
            if t == 'float':
                m = self.const_reg(0x80000000, e.line)
                self.emit(f"xor r{rd}, r{r}, r{m}", "float 取负 = 异或符号位(§3)")
                return rd, 'float', uni
            raise SgError('E004', e.line, "取负只能用于 int / float")
        if isinstance(e, Bin):
            return self.gen_bin(e, want, dest)
        if isinstance(e, Call):
            return self.gen_call_expr(e, want, dest)
        raise SgError(None, e.line, "无法生成的表达式")

    def gen_leaf_into(self, e: Expr, want: Optional[str], dest: int) -> Tuple[int, str, bool]:
        """把叶子表达式(Num / @sym)直接放进目标寄存器。"""
        if isinstance(e, Num):
            if float(e.value) == 0.0:
                return 0, ('float' if e.is_float or want == 'float' else 'int'), True
            if want == 'float' and not e.is_float:
                self.load_imm(dest, int(e.value))
                self.emit(f"i2f r{dest}, r{dest}", "int → float 提升(§3)")
                return dest, 'float', True
            if want == 'int' and e.is_float:
                raise SgError('E004', e.line, f"浮点字面量 {e.value} 不能当 int 用(需要 f2i())")
            if e.is_float:
                self.load_imm(dest, int.from_bytes(struct.pack('<f', float(e.value)), 'little'),
                              f"= {e.value}(FP32 位型)")
                return dest, 'float', True
            self.load_imm(dest, int(e.value))
            return dest, 'int', True
        # @name / @name[off]
        assert isinstance(e, Sym)
        if e.name not in self.consts:
            raise SgError('E009', e.line, f"未定义符号 @{e.name}")
        base = self.consts[e.name]
        if e.off is None:
            if base % 4:
                raise SgError('E010', e.line, f"常量 {e.name}=0x{base:x} 未 4B 对齐")
            self.load_imm(dest, base, f"@{e.name}")
            return dest, 'int', True
        off = self.const_int(e.off) if e.off is not None else 0
        if off is None or off % 4 or off < 0:
            raise SgError('E010', e.line, f"@{e.name}[...] 的偏移必须是 4B 对齐的非负编译期常量")
        cr = self.const_reg(base, e.line)
        ty = want or 'int'
        self.emit_pred(f"ld.g r{dest}, {off}(r{cr})", f"@{e.name}[{off}] → {ty}")
        return dest, ty, True

    def gen_not(self, e: Un) -> Tuple[int, str, bool]:
        """`!` 只支持比较式取反(§3 的表里只有比较谓词)。"""
        inner = e.operand
        if isinstance(inner, Bin) and inner.op in ('==', '!=', '<', '<=', '>', '>='):
            flip = {'==': '!=', '!=': '==', '<': '>=', '<=': '>', '>': '<=', '>=': '<'}
            return self.gen_compare(Bin(inner.line, flip[inner.op], inner.lhs, inner.rhs))
        raise SgError('E004', e.line, "`!` 只能用于比较式(§3)")

    def array_addr(self, v: VarInfo, index: Optional[Expr], line: int) -> int:
        """A[i] → 地址寄存器(per_warp: base + wid×分区; 全局: 分配到的地址)"""
        ra = self.temp(line, '数组地址')
        if v.space == 'shared' and v.scope == 'per_warp':
            wr = self.temp(line, 'wid')
            self.emit(f"wmov r{wr}, wid")
            part = self.const_reg(v.length * WORD, line)
            self.emit(f"imad r{ra}, r{wr}, r{part}, r0", "每 warp 私有分区(§4.4)")
            if index is None:
                return ra
            ri, _, _ = self.gen_expr(index, 'int')
            four = self.const_reg(WORD, line)
            self.emit(f"imad r{ra}, r{ri}, r{four}, r{ra}", f"&{v.name}[i]")
            return ra
        br = self.const_reg(v.base, line)
        if index is None:
            self.emit(f"add r{ra}, r{br}, r0", f"&{v.name}")
            return ra
        ri, _, _ = self.gen_expr(index, 'int')
        four = self.const_reg(WORD, line)
        op = 'imad' if v.space == 'shared' else 'imad'
        self.emit(f"{op} r{ra}, r{ri}, r{four}, r{br}",
                  f"&{v.name}[i] = 0x{v.base:x} + i*4")
        return ra

    def gen_call_expr(self, e: Call, want: Optional[str],
                      dest: Optional[int] = None) -> Tuple[int, str, bool]:
        name = e.name
        if name not in BUILTINS:
            raise SgError('E003', e.line, f"未知函数 {name}()")
        nargs, ret = BUILTINS[name]
        if len(e.args) != nargs:
            raise SgError('E003', e.line, f"{name}() 需要 {nargs} 个参数, 实际 {len(e.args)}")
        if name in ('tid', 'wid'):
            rd = self.temp(e.line, name)
            self.emit(f"tmov r{rd}, tid" if name == 'tid' else f"wmov r{rd}, wid",
                      f"{name}()" + ("" if name == 'tid' else "(每个 warp 一个值, 非标量)"))
            return rd, 'int', False
        if name in ('ldx', 'lds'):
            ty = want or 'int'
            ra, _, _ = self.gen_expr(e.args[0], 'int')
            rd = dest if dest is not None else self.temp(e.line, name)
            op = 'ld.g' if name == 'ldx' else 'ld.s'
            self.emit_pred(f"{op} r{rd}, 0(r{ra})", f"{name}() → {ty}")
            return rd, ty, False
        if name == 'ceildiv':
            ra, _, ua = self.gen_expr(e.args[0], 'int')
            rb, _, ub = self.gen_expr(e.args[1], 'int')
            rt = self.temp(e.line, 'ceildiv 临时')
            rd = dest if dest is not None else self.temp(e.line, 'ceildiv')
            self.emit(f"addi r{rt}, r{rb}, -1", "ceildiv = (a + b - 1) / b(§7)")
            self.emit(f"add r{rt}, r{ra}, r{rt}")
            self.emit(f"divu r{rd}, r{rt}, r{rb}", "标量循环次数由标量算出(§8)")
            return rd, 'int', ua and ub
        if name in ('f2i', 'i2f'):
            src, sty, uni = self.gen_expr(e.args[0])
            if name == 'f2i' and sty != 'float':
                raise SgError('E004', e.line, "f2i() 的参数必须是 float")
            if name == 'i2f' and sty != 'int':
                raise SgError('E004', e.line, "i2f() 的参数必须是 int")
            rd = dest if dest is not None else self.temp(e.line, name)
            self.emit(f"{name} r{rd}, r{src}", f"{name}() 显式转换(§3)")
            return rd, ret, uni
        raise SgError('E003', e.line, f"{name}() 不能用在表达式里")

    def gen_bin(self, e: Bin, want: Optional[str],
                dest: Optional[int] = None) -> Tuple[int, str, bool]:
        if e.op in ('==', '!=', '<', '<=', '>', '>='):
            return self.gen_compare(e)
        if e.op in ('&&', '||'):
            raise SgError('E004', e.line, "`&&`/`||` 请改写成比较式的组合或用嵌套 if(§3)")
        lr, lt, lu = self.gen_expr(e.lhs, want)
        rr, rt, ru = self.gen_expr(e.rhs, want if want else lt)
        res_t = 'float' if 'float' in (lt, rt) else 'int'
        uni = lu and ru
        if res_t == 'float' and e.op in ('&', '|', '^', '<<', '>>', '>>>'):
            raise SgError('E004', e.line, f"{e.op} 只支持 int(按位/移位运算)")
        if res_t == 'float':
            if lt == 'int':
                nr = self.temp(e.line, '提升')
                self.emit(f"i2f r{nr}, r{lr}", "int → float 提升(§3)")
                lr = nr
            if rt == 'int':
                nr = self.temp(e.line, '提升')
                self.emit(f"i2f r{nr}, r{rr}", "int → float 提升(§3)")
                rr = nr
        rd = dest if dest is not None else self.temp(e.line, '运算结果')
        if res_t == 'int':
            # §5: a * b + c 合成一条 imad
            if e.op == '+' and isinstance(e.lhs, Bin) and e.lhs.op == '*':
                a, _, _ = self.gen_expr(e.lhs.lhs, 'int')
                b, _, _ = self.gen_expr(e.lhs.rhs, 'int')
                self.emit(f"imad r{rd}, r{a}, r{b}, r{rr}", "a*b + c → imad(§5)")
                return rd, 'int', uni
            table = {'+': 'add', '-': 'sub', '*': 'mul', '/': 'div', '%': 'rem',
                     '&': 'and', '|': 'or', '^': 'xor', '<<': 'sll', '>>': 'sra', '>>>': 'srl'}
            if e.op not in table:
                raise SgError('E004', e.line, f"int 不支持运算符 {e.op}")
            # 立即形式(ISA §5.4): 移位量与位掩码常常是常量 ⇒ 省一条常量装载。
            # `>>` 是算术右移(sra, 符号位扩展), `>>>` 是逻辑右移(srl)。
            imm_op = {'&': 'andi', '|': 'ori', '^': 'xori', '<<': 'slli',
                      '>>': 'srai', '>>>': 'srli'}.get(e.op)
            cv = self.const_int(e.rhs)
            if imm_op and cv is not None and -256 <= cv <= 255 and cv >= 0:
                self.emit(f"{imm_op} r{rd}, r{lr}, {cv}",
                          f"int {e.op} 立即形式(§5.4: 移位量/掩码是常量)")
                return rd, 'int', uni
            self.emit(f"{table[e.op]} r{rd}, r{lr}, r{rr}",
                      f"int {e.op}" + ("(有符号除法)" if e.op in ('/', '%') else
                                       "(算术右移)" if e.op == '>>' else
                                       "(逻辑右移)" if e.op == '>>>' else ""))
            return rd, 'int', uni
        if e.op in ('/', '%'):
            raise SgError('E012', e.line, f"FP {'除法' if e.op == '/' else '取余'}不支持(§8)")
        # §5: a * b + c 合成一条 fma.f32
        if e.op == '+' and isinstance(e.lhs, Bin) and e.lhs.op == '*':
            a, _, _ = self.gen_expr(e.lhs.lhs, 'float')
            b, _, _ = self.gen_expr(e.lhs.rhs, 'float')
            self.emit(f"fma.f32 r{rd}, r{a}, r{b}, r{rr}", "a*b + c → fma.f32(单次舍入, §5)")
            return rd, 'float', uni
        table = {'+': 'fadd', '-': 'fsub', '*': 'fmul'}
        if e.op not in table:
            raise SgError('E004', e.line, f"float 不支持运算符 {e.op}")
        self.emit(f"{table[e.op]} r{rd}, r{lr}, r{rr}", f"float {e.op}")
        return rd, 'float', uni

    def gen_compare(self, e: Bin) -> Tuple[int, str, bool]:
        lr, lt, lu = self.gen_expr(e.lhs)
        rr, rt, ru = self.gen_expr(e.rhs)
        ty = 'float' if 'float' in (lt, rt) else 'int'
        if ty == 'float':
            if lt == 'int':
                nr = self.temp(e.line, '提升')
                self.emit(f"i2f r{nr}, r{lr}", "比较前提升(§3)")
                lr = nr
            if rt == 'int':
                nr = self.temp(e.line, '提升')
                self.emit(f"i2f r{nr}, r{rr}", "比较前提升(§3)")
                rr = nr
            cond = {'==': 'feq', '!=': 'fne', '<': 'flt', '<=': 'fle', '>': 'fgt', '>=': 'fge'}[e.op]
        else:
            cond = {'==': 'eq', '!=': 'ne', '<': 'lt', '<=': 'le', '>': 'gt', '>=': 'ge'}[e.op]
        p = self.alloc_pred(e.line, f"{e.op} 比较")
        self.emit(f"setp.{cond} p{p}, r{lr}, r{rr}", f"{ty} {e.op} → p{p}(§3)")
        return p, 'pred', lu and ru

    # ── 语句 ──
    def gen_stmts(self, stmts: List[Stmt]) -> None:
        for st in stmts:
            self.gen_stmt(st)
            # `when <cond> goto L`(非均匀)之后, 没跳走的 lane 继续走 ⇒ 后续语句在发散路径上
            if isinstance(st, Goto) and st.cond is not None and not st.uniform \
                    and not self.goto_uniform.get(id(st), False):
                self.in_divergent = True

    goto_uniform: Dict[int, bool] = {}

    def gen_stmt(self, st: Stmt) -> None:
        if isinstance(st, LabelStmt):
            if st.name.startswith('__'):
                raise SgError('E008', st.line,
                               f"标签 {st.name} 以 __ 开头: 这是编译器内部标签的前缀(§2)")
            if st.name in self.labels:
                raise SgError('E008', st.line, f"标签重复定义: {st.name}")
            self.labels[st.name] = st.line
            self.label(st.name)
            return
        if isinstance(st, Assign):
            self.gen_assign(st)
            return
        if isinstance(st, CallStmt):
            self.gen_call_stmt(st)
            return
        if isinstance(st, AssStmt):
            self.gen_ass(st)
            return
        if isinstance(st, Goto):
            self.gen_goto(st)
            return
        if isinstance(st, WhenStmt):
            self.gen_when_stmt(st)
            return
        if isinstance(st, If):
            self.gen_if(st)
            return
        if isinstance(st, While):
            self.gen_while(st)
            return
        raise SgError(None, st.line, "无法生成的语句")

    def gen_assign(self, st: Assign) -> None:
        name = st.target.name
        if name not in self.vars:
            if name in self.consts:
                raise SgError(None, st.line, f"{name} 是常量, 不能赋值")
            raise SgError(None, st.line, f"未声明的变量 {name}")
        v = self.vars[name]
        mark = self.save_temps()
        try:
            if v.is_array:
                if st.target.index is None:
                    raise SgError(None, st.line, f"数组 {name} 不能整体赋值")
                addr = self.array_addr(v, st.target.index, st.line)
                val, vt, _ = self.gen_expr(st.value, v.vtype,
                                           dest=self.temp(st.line, '数组元素临时'))
                if vt == 'pred':
                    raise SgError('E004', st.line, "比较式的结果是谓词, 不能直接赋值")
                self.check_assign_type(v, vt, st.line)
                op = 'st.s' if v.space == 'shared' else 'st.g'
                self.emit_pred(f"{op} 0(r{addr}), r{val}", f"{name}[i] = ...  谓词化写(§6.4)")
                return
            # 谓词化时不就地写变量(见下), 其余情形直接算进目标寄存器
            val, vt, uni = self.gen_expr(st.value, v.vtype,
                                         dest=v.reg if self.cur_pred is None else None)
            if vt == 'pred':
                raise SgError('E004', st.line, "比较式的结果是谓词, 不能直接赋值")
            self.check_assign_type(v, vt, st.line)
            if val != v.reg:
                self.emit_pred(f"add r{v.reg}, r{val}, r0", f"{name} = ...  (位型拷贝, 对 float 同样适用)")
            v.uniform = uni and self.cur_pred is None
        finally:
            self.restore_temps(mark)
            self.cur_pred = None

    def check_assign_type(self, v: VarInfo, vt: str, line: int) -> None:
        if v.vtype != vt:
            need = 'f2i()' if vt == 'float' else 'i2f()'
            raise SgError('E004', line, f"{v.name} 是 {v.vtype}, 右侧是 {vt}(需要显式 {need})")

    def emit_pred(self, text: str, comment: str) -> None:
        """在 `when <cond> : <stmt>` 里, 访存与写回要谓词化(§6.4)。

        算术指令**不加**谓词: ISA §4.2 里 fma/imad/lui 根本没有谓词字段, 而且它们不产生副作用 ——
        屏蔽 lane 算出垃圾值, 由最后那次谓词化写回挡住(ISA §3.1 的"启动清零"保证垃圾值可复现)。
        """
        if self.cur_pred is not None:
            self.emit(f"@p{self.cur_pred} {text}", comment)
        else:
            self.emit(text, comment)

    def gen_call_stmt(self, st: CallStmt) -> None:
        c, line = st.call, st.line
        name = c.name
        if name not in BUILTINS:
            raise SgError('E003', line, f"未知函数 {name}()")
        nargs, _ = BUILTINS[name]
        if len(c.args) != nargs:
            raise SgError('E003', line, f"{name}() 需要 {nargs} 个参数, 实际 {len(c.args)}")
        mark = self.save_temps()
        try:
            if name in ('stx', 'sts'):
                ra, _, _ = self.gen_expr(c.args[0], 'int')
                rv, rvt, _ = self.gen_expr(c.args[1])
                if rvt == 'pred':
                    raise SgError('E004', line, "stx/sts 的值不能是比较式")
                op = 'st.g' if name == 'stx' else 'st.s'
                self.emit_pred(f"{op} 0(r{ra}), r{rv}", f"{name}()  谓词化访存(§6.4)")
                return
            if name in ('ldx', 'lds'):
                raise SgError('E003', line, f"{name}() 的返回值必须赋给变量")
            if name == 'bar':
                if self.in_divergent:
                    raise SgError('E006', line, "bar() 出现在发散路径里(§6.8)")
                self.uses_bar = True
                self.emit("bar.sync", "bar(): 所有 warp 必须执行相同次数(§6.8)")
                return
            if name == 'exit':
                self.emit("exit", "内核结束(必须均匀执行, §8)")
                return
            raise SgError(None, line, f"{name}() 的结果没有被使用")
        finally:
            self.restore_temps(mark)

    def gen_ass(self, st: AssStmt) -> None:
        text = st.text.strip()
        if not text:
            raise SgError('E014', st.line, "ass 后面没有指令")
        mn = text.split(None, 1)[0]
        if mn not in KNOWN_MNEMONICS:
            raise SgError('E014', st.line, f"未知指令 {mn!r}(ISA.md §5 的指令表)")
        self.emit(text, "ass: 直接插入(§4.6)")

    def emit_cond_branch(self, cond: Expr, target: str, reconv: str, sense: bool,
                         force_uniform: bool = False) -> bool:
        """把条件编译成"满足 sense 时跳到 target"。

        比较式 ⇒ 一条 `setp` + 一条谓词化 `br.simt`;
        `&&`/`||` ⇒ **短路**:只判到能定论为止(`A && B` 在 A 为假时不再判 B)。
        返回条件是否对所有 lane 一致(均匀), 供 §6.8 的 bar()/循环次数检查使用。
        """
        if isinstance(cond, Bin) and cond.op in ('&&', '||'):
            trivial = (cond.op == '&&' and not sense) or (cond.op == '||' and sense)
            if trivial:                      # 顺序判两次: 短路天然成立
                ua = self.emit_cond_branch(cond.lhs, target, reconv, sense)
                ub = self.emit_cond_branch(cond.rhs, target, reconv, sense)
                return ua and ub
            skip = self.new_label('short')   # 第一个条件已经能定论 ⇒ 跳过第二个
            ua = self.emit_cond_branch(cond.lhs, skip, skip, not sense)
            ub = self.emit_cond_branch(cond.rhs, target, reconv, sense)
            self.label(skip)
            return ua and ub
        mark = self.save_temps()
        try:
            p, ty, uni = self.gen_expr(cond)
            if ty != 'pred':
                raise SgError('E004', cond.line, "条件必须是比较式或它们的 && / || 组合(§3)")
            if not (uni or force_uniform):
                self.emit(f"ipdom {reconv}", f"收敛点 = {reconv}(§6.3)")
            self.emit(f"@p{p} br.simt {target}, {reconv}" if sense
                      else f"@!p{p} br.simt {target}, {reconv}",
                      ("条件成立" if sense else "条件不成立") + f"的 lane 跳到 {target}")
            self.record_label_ref(target, cond.line)
            if reconv in self.labels:
                self.record_label_ref(reconv, cond.line)
            self.free_pred(p)
            return bool(uni or force_uniform)
        finally:
            self.restore_temps(mark)

    def gen_goto(self, st: Goto) -> None:
        if st.cond is None:
            self.emit(f"j {st.label}", f"goto {st.label}(§6.2; j 由汇编器展开成 br.simt)")
            self.record_label_ref(st.label, st.line)
            return
        if st.label in self.labels:                 # 向后跳: 收敛点取之后的下一个标签
            reconv = self.next_label_after(st.line) or st.label
        else:
            reconv = st.label
        uni = self.emit_cond_branch(st.cond, st.label, reconv, True, force_uniform=st.uniform)
        self.goto_uniform[id(st)] = bool(st.uniform or uni)

    def next_label_after(self, line: int) -> Optional[str]:
        best, best_line = None, None
        for st in self.k.body:
            if isinstance(st, LabelStmt) and st.line > line:
                if best_line is None or st.line < best_line:
                    best, best_line = st.name, st.line
        return best

    def gen_when_stmt(self, st: WhenStmt) -> None:
        """§6.4: setp + 谓词化指令(不发散)。

        两种情形退化成"分支"而不是谓词化:
          ① 条件里有 `&&`/`||`(ISA 没有 andp/orp ⇒ 一个谓词存不下两个条件的与);
          ② 已经在另一个 `when` 体内(同理, 外层条件会被丢掉 ⇒ 必须真的分支)。
        """
        simple = (not isinstance(st.cond, Bin) or st.cond.op not in ('&&', '||'))
        if simple and self.cur_pred is None:
            mark = self.save_temps()
            try:
                p, ty, uni = self.gen_expr(st.cond)
                if ty != 'pred':
                    raise SgError('E004', st.line, "when 之后必须是比较式(§6.4)")
                prev, prev_div = self.cur_pred, self.in_divergent
                self.cur_pred = p
                # 谓词化块同样是"部分 lane 执行": 里面 bar() 会死等(§6.8)
                self.in_divergent = prev_div or not uni
                self.gen_stmt(st.body)
                self.cur_pred, self.in_divergent = prev, prev_div
                self.free_pred(p)
            finally:
                self.restore_temps(mark)
            return
        # 分支形式: 条件不成立就跳过整个语句(语义与谓词化等价, 代价是一条 br.simt)
        mark = self.save_temps()
        try:
            skip = self.new_label('wskip')
            uni = self.emit_cond_branch(st.cond, skip, skip, False)
            prev, prev_div = self.cur_pred, self.in_divergent
            self.cur_pred = None
            self.in_divergent = prev_div or not uni
            self.gen_stmt(st.body)
            self.cur_pred, self.in_divergent = prev, prev_div
            self.label(skip)
        finally:
            self.restore_temps(mark)

    def gen_if(self, st: If) -> None:
        """§6.5(极性按手写内核的写法: 条件成立落进 then)"""
        mark = self.save_temps()
        try:
            end_l = self.new_label('end')
            else_l = end_l if not st.else_body else self.new_label('else')
            uni = self.emit_cond_branch(st.cond, else_l, end_l, False)
            was_div = self.in_divergent
            self.in_divergent = was_div or not uni
            self.gen_stmts(st.then_body)
            if st.else_body:
                self.emit(f"j {end_l}", "then 块结束")
                self.label(else_l)
                self.gen_stmts(st.else_body)
            self.in_divergent = was_div
            self.label(end_l)
        finally:
            self.restore_temps(mark)

    def gen_while(self, st: While) -> None:
        """§6.6"""
        loop_l, end_l = self.new_label('loop'), self.new_label('end')
        self.label(loop_l)
        mark = self.save_temps()
        try:
            uni = self.emit_cond_branch(st.cond, end_l, end_l, False)
            if not uni:
                raise SgError('E005', st.line,
                               "循环条件依赖 tid()/wid() 或访存结果(循环次数必须标量, §8)")
            was_div = self.in_divergent
            self.in_divergent = was_div or not uni
            self.gen_stmts(st.body)
            self.in_divergent = was_div
            self.emit(f"j {loop_l}", "回到循环头")
            self.label(end_l)
        finally:
            self.restore_temps(mark)

    def record_label_ref(self, name: str, line: int) -> None:
        """登记标签引用; `__` 开头的是编译器内部标签, 不参与"用户标签是否定义"的检查。"""
        if name.startswith('__'):
            return
        self.label_refs.append((name, line))

    # ── 入口 ──
    def gen(self) -> List[str]:
        k = self.k
        self.out.append(f"; ── 由 sw/compiler.py 从 {os.path.basename(self.path)} 生成(grammer.md)──")
        self.out.append(f"; kernel {k.name}: 每个语言构造按 §6 的模板展开, 不做优化(§1)")
        self.declare()
        self.plan_pinned()
        for d in k.decls:
            if isinstance(d, ParamDecl):
                if d.base not in self.consts:
                    raise SgError('E009', d.line, f"未定义符号 @{d.base}")
                for n, offe in zip(d.names, d.offs):
                    off = self.const_int(offe)          # 字面量或 -: 常量表达式都可以
                    if off is None:
                        raise SgError('E010', d.line, "param 的偏移必须是编译期常量")
                    if off % 4 or off < 0:
                        raise SgError('E010', d.line, f"@{d.base}[{off}] 未 4B 对齐")
                    cr = self.const_reg(self.consts[d.base], d.line)
                    v = self.vars[n]
                    self.emit(f"ld.g r{v.reg}, {off}(r{cr})", f"param {n} ← @{d.base}[{off}]")
        self.gen_stmts(k.body)
        # 内核没有以 exit 收尾 ⇒ 补一条(否则 PC 会走过镜像末尾, 变成 ILLEGAL_INSTR)
        last = next((l for l in reversed(self.out) if l.strip() and not l.strip().startswith(';')), '')
        if 'exit' not in last:
            self.emit("exit", "编译器补的收尾 exit(§7)")
        for name, line in self.label_refs:
            if name not in self.labels:
                raise SgError('E007', line, f"未定义标签 {name}")
        cross = [v for v in self.shared_arrays if v.scope == 'shared']
        if cross and not self.uses_bar:
            raise SgError('E013', cross[0].line,
                          f"共享数组 {cross[0].name} 是 shared(跨 warp 可见), "
                          f"但内核里没有 bar()(§4.4/§6.8)")
        return self.out


# ────────────────────────────── 驱动 ──────────────────────────────

def compile_source(src: str, path: str, global_base: int, comments: bool = True
                   ) -> Tuple[str, dict]:
    consts, kernel = Parser(tokenize(src)).parse()
    gen = FuncGen(kernel, consts, global_base, path)
    gen.comments = comments
    text = "\n".join(gen.gen()) + "\n"
    info = {
        'kernel': kernel.name,
        'instrs': gen.n_instr,
        'pinned': len(gen.pinned),
        'hist': mnemonic_histogram(text),
        'regs': len(gen.used_regs),
        'preds': len(gen.used_preds),
        'shared': gen.shared_total,
        'global_used': gen.global_base - global_base,
        'labels': len(gen.labels),
    }
    return text, info


def mnemonic_histogram(asm_text: str) -> Dict[str, int]:
    """统计汇编后的指令直方图(含汇编器补的 ipdom) —— 用来核对"这个内核用到了 ISA 的哪些特性"。"""
    asm = assembly.assemble(asm_text)
    hist: Dict[str, int] = {}
    for ins in asm.instrs:
        hist[ins.mnemonic] = hist.get(ins.mnemonic, 0) + 1
    return hist


def build_image(asm_text: str) -> Tuple[bytes, int]:
    asm = assembly.assemble(asm_text)
    if asm.errors:
        raise SgError(None, 0, "汇编器报错:\n    " + "\n    ".join(asm.errors))
    flags = asm.flags(0)
    return asm.image(0), flags


def main() -> int:
    ap = argparse.ArgumentParser(description='SimpleGPU 自定义语言编译器(grammer.md)')
    ap.add_argument('inputs', nargs='+', help='.sg 源文件')
    ap.add_argument('-o', '--output', help='输出汇编文件(单个输入时)')
    ap.add_argument('--image', help='直接产出内核镜像 .bin(单个输入时)')
    ap.add_argument('--asm-dir', help='批量: 汇编写到 DIR/<名字>.S')
    ap.add_argument('--image-dir', help='批量: 镜像写到 DIR/<名字>.bin')
    ap.add_argument('--global-base', default='0x80000000',
                    help='__global__ 数组的分配基址(默认 DDR 0x80000000)')
    ap.add_argument('--no-comments', action='store_true', help='生成的汇编不带注释')
    ap.add_argument('--disasm', action='store_true', help='编译后打印反汇编')
    ap.add_argument('--stats', action='store_true',
                    help='打印编译报告: 寄存器/共享/全局布局 + ISA 指令直方图')
    ap.add_argument('-q', '--quiet', action='store_true')
    a = ap.parse_args()

    if len(a.inputs) > 1 and (a.output or a.image):
        ap.error('-o/--image 只用于单个输入; 批量请用 --asm-dir/--image-dir')
    gbase = int(a.global_base, 0)

    rc = 0
    for src_path in a.inputs:
        name = os.path.splitext(os.path.basename(src_path))[0]
        try:
            text, info = compile_source(open(src_path).read(), src_path, gbase,
                                        comments=not a.no_comments)
            data = flags = None
            if a.image or a.image_dir or a.disasm:
                data, flags = build_image(text)
            if a.output:
                open(a.output, 'w').write(text)
            elif a.asm_dir:
                os.makedirs(a.asm_dir, exist_ok=True)
                open(os.path.join(a.asm_dir, name + '.S'), 'w').write(text)
            elif not (a.image or a.image_dir):
                sys.stdout.write(text)
            if a.image:
                open(a.image, 'wb').write(data)
            if a.image_dir:
                os.makedirs(a.image_dir, exist_ok=True)
                open(os.path.join(a.image_dir, name + '.bin'), 'wb').write(data)
            if not a.quiet:
                print(f"[sg] {src_path} → kernel {info['kernel']}: {info['instrs']} 条指令, "
                      f"寄存器 {info['regs']}/31, 谓词 {info['preds']}/3, "
                      f"共享 {info['shared']}B, 全局 {info['global_used']}B, "
                      f"用户标签 {info['labels']}"
                      + (f", flags=0x{flags:x}" if flags is not None else ""), file=sys.stderr)
            if a.stats:
                hist = info['hist']
                print(f"[sg] {info['kernel']}: 汇编后共 {sum(hist.values())} 条指令, "
                      f"用到 {len(hist)} 种;寄存器 {info['regs']}/31(常驻常量 {info['pinned']}), "
                      f"谓词 {info['preds']}/3, 共享 {info['shared']}B, "
                      f"全局 {info['global_used']}B(基址 0x{gbase:08x})", file=sys.stderr)
                for mn, c in sorted(hist.items(), key=lambda kv: (-kv[1], kv[0])):
                    print(f"       {mn:<10} {c}", file=sys.stderr)
            if a.disasm:
                asm = assembly.assemble(text)
                sys.stderr.write(assembly.disasm_report(asm, asm.flags(0)))
        except SgError as e:
            print(e.format(src_path), file=sys.stderr)
            rc = 1
    return rc


if __name__ == '__main__':
    sys.exit(main())
