"""Direct unit tests for ``hw/rtl/fpu/fcvt.v``.

``fcvt`` is purely combinational, so driving it directly runs orders of
magnitude faster than pushing conversions through the ``fpu`` top level
(no clock, no arbitration).  The float-to-int path consumes the *decoder's*
view of the operand, so the test rebuilds it with
:func:`harness.lib.fp32.normalized` -- the same representation ``fpu_decoder``
produces and which ``test_fpu_decoder`` pins against the RTL.
"""

from __future__ import annotations

import random

import cocotb

from harness.lib import fp32 as R
from harness.lib.tb import Scoreboard, settle

RANDOM_VECTORS = 6000

#: The 64-bit-shift truncation window documented in ``test_fpu_top``.
TRUNCATION_EXP_LO = 64
TRUNCATION_EXP_HI = 86


def decoder_inputs(bits: int) -> dict:
    """Rebuild the operand fields exactly as ``fpu_decoder`` would present them."""
    exp, sig = R.normalized(bits)
    return {
        "src_sign": R.sign_of(bits),
        "src_exp": exp,
        "src_sig": sig,
        "src_is_zero": int(R.is_zero(bits)),
        "src_is_subnormal": int(R.is_subnormal(bits)),
        "src_is_normal": int(R.is_normal(bits)),
        "src_is_inf": int(R.is_inf(bits)),
        "src_is_nan": int(R.is_nan(bits)),
        "src_is_snan": int(R.is_snan(bits)),
        "src_is_qnan": int(R.is_qnan(bits)),
    }


async def run_fcvt(dut, bits: int, op: int, rm: int = R.RNE) -> tuple[int, int]:
    dut.src.value = bits
    fields = decoder_inputs(bits)
    for name, value in fields.items():
        getattr(dut, name).value = value
    dut.rm.value = rm
    dut.is_w_s.value = 1 if op == R.OP_FCVT_W_S else 0
    dut.is_wu_s.value = 1 if op == R.OP_FCVT_WU_S else 0
    dut.is_s_w.value = 1 if op == R.OP_FCVT_S_W else 0
    dut.is_s_wu.value = 1 if op == R.OP_FCVT_S_WU else 0
    await settle()
    return int(dut.result.value), int(dut.fflags.value)


def model(op: int, bits: int, rm: int = R.RNE) -> tuple[int, int]:
    if op == R.OP_FCVT_W_S:
        return R.op_fcvt_w_s(bits, rm)
    if op == R.OP_FCVT_WU_S:
        return R.op_fcvt_wu_s(bits, rm)
    if op == R.OP_FCVT_S_W:
        return R.op_fcvt_s_w(bits, rm)
    return R.op_fcvt_s_wu(bits, rm)


def in_truncation_window(bits: int) -> bool:
    if R.is_nan(bits) or R.is_inf(bits) or R.is_zero(bits):
        return False
    exp, _ = R.normalized(bits)
    return TRUNCATION_EXP_LO <= exp <= TRUNCATION_EXP_HI


@cocotb.test()
async def test_fcvt_directed(dut):
    """Directed corners for all four conversions and every rounding mode."""
    sb = Scoreboard("fcvt.directed")

    floats = [0.0, -0.0, 0.5, -0.5, 1.5, -1.5, 2.5, -2.5, 1.0, -1.0, 3.7, -3.7,
              2.0 ** 31 - 128.0, -2.0 ** 31, 1e10, -1e10, 1e-45, -1e-45,
              3.4e38, -3.4e38]
    bits_pool = [R.f2b(v) for v in floats] + [R.POS_INF, R.NEG_INF,
                                              R.CANONICAL_NAN, 0x7F800001,
                                              0xFF800001]
    for bits in bits_pool:
        for op in (R.OP_FCVT_W_S, R.OP_FCVT_WU_S):
            got = await run_fcvt(dut, bits, op)
            exp = model(op, bits)
            sb.check(got[0], exp[0], f"op=0b{op:07b} src=0x{bits:08x} result")
            sb.check_flags(got[1], exp[1], f"op=0b{op:07b} src=0x{bits:08x} fflags")

    ints = [0, 1, -1, 2 ** 31 - 1, -2 ** 31, 123456789, 2 ** 24, 2 ** 24 + 1,
            16777217, -16777217, 2 ** 30, 255, 256, 2 ** 23 - 1, 0x80000001]
    for v in ints:
        bits = v & 0xFFFFFFFF
        for op in (R.OP_FCVT_S_W, R.OP_FCVT_S_WU):
            got = await run_fcvt(dut, bits, op)
            exp = model(op, bits)
            sb.check(got[0], exp[0], f"op=0b{op:07b} src=0x{bits:08x} result")
            sb.check_flags(got[1], exp[1], f"op=0b{op:07b} src=0x{bits:08x} fflags")

    # Directed rounding, both signs: RNE ties-to-even vs RZ/RDN/RUP/RMM.
    for rm in (R.RNE, R.RTZ, R.RDN, R.RUP, R.RMM):
        for v in (2.5, -2.5, 1.5, -1.5, 0.5, -0.5, 3.7, -3.7, -0.0):
            bits = R.f2b(v)
            for op in (R.OP_FCVT_W_S, R.OP_FCVT_WU_S):
                got = await run_fcvt(dut, bits, op, rm)
                exp = model(op, bits, rm)
                sb.check(got[0], exp[0],
                         f"rm={R.RM_NAMES[rm]} op=0b{op:07b} src={v!r} result")
                sb.check_flags(got[1], exp[1],
                               f"rm={R.RM_NAMES[rm]} op=0b{op:07b} src={v!r} fflags")

    sb.log_summary(dut)
    sb.report()


@cocotb.test()
async def test_fcvt_random(dut):
    """Random operands across all four conversions."""
    sb = Scoreboard("fcvt.random")
    rng = random.Random(0xF00D)
    ops = [R.OP_FCVT_W_S, R.OP_FCVT_WU_S, R.OP_FCVT_S_W, R.OP_FCVT_S_WU]
    deferred = 0

    for i in range(RANDOM_VECTORS):
        bits = rng.getrandbits(32) if rng.random() < 0.6 else \
            ((rng.getrandbits(1) << 31) |
             (rng.choice([0, 1, 2, 126, 127, 128, 191, 254, 255]) << 23) |
             (rng.getrandbits(23) if rng.random() < 0.5 else 0))
        if in_truncation_window(bits):
            deferred += 1  # known defect, pinned in test_fpu_top
            continue
        op = ops[i % len(ops)]
        got = await run_fcvt(dut, bits, op)
        exp = model(op, bits)
        sb.check(got[0], exp[0], f"op=0b{op:07b} src=0x{bits:08x} result")
        sb.check_flags(got[1], exp[1], f"op=0b{op:07b} src=0x{bits:08x} fflags")

    dut._log.info(f"deferred {deferred} operand(s) inside the fcvt truncation window")
    sb.log_summary(dut)
    sb.report()
