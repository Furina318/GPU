"""Tests for ``hw/rtl/fpu/fpu_decoder.v``.

The decoder is the cheapest DUT in the FPU and the one every other unit
depends on, so it gets exhaustive treatment over the *field structure* of
binary32 (all 256 exponents x interesting fractions) plus random values, and a
value-consistency check against the exact rational model.
"""

from __future__ import annotations

import random

import cocotb

from harness.lib import fp32 as R
from harness.lib.tb import Scoreboard, settle

MAX_RANDOM = 20000


def signed9(value: int) -> int:
    """Interpret a 9-bit field as a signed integer (works on any simulator)."""
    value &= 0x1FF
    return value - 0x200 if value & 0x100 else value


def clz23(f: int) -> int:
    """Leading zeros of the 23-bit fraction ``f`` (f must be non-zero)."""
    return 23 - f.bit_length()


def decode_model(a: int) -> dict:
    """Golden model of ``fpu_decoder``, transcribed from the RTL."""
    s = (a >> 31) & 1
    e = (a >> 23) & 0xFF
    f = a & 0x7FFFFF

    is_zero = (e == 0) and (f == 0)
    is_subnormal = (e == 0) and (f != 0)
    is_normal = (e != 0) and (e != 0xFF)
    is_inf = (e == 0xFF) and (f == 0)
    is_nan = (e == 0xFF) and (f != 0)

    if is_normal:
        sig = 0x800000 | f
        exp = e - 127
    elif is_subnormal:
        lz = clz23(f)
        sig = (f << (lz + 1)) & 0xFFFFFF
        exp = -127 - lz
    else:
        sig = 0
        exp = 0

    return {
        "sign": s,
        "exp": exp,
        "sig": sig,
        "is_zero": int(is_zero),
        "is_subnormal": int(is_subnormal),
        "is_normal": int(is_normal),
        "is_inf": int(is_inf),
        "is_nan": int(is_nan),
        "is_snan": int(is_nan and not (f & 0x400000)),
        "is_qnan": int(is_nan and bool(f & 0x400000)),
    }


async def read_decoder(dut, a: int) -> dict:
    dut.a.value = a
    await settle()
    return {
        "sign": int(dut.sign.value),
        "exp": signed9(int(dut.exp.value)),
        "sig": int(dut.sig.value),
        "is_zero": int(dut.is_zero.value),
        "is_subnormal": int(dut.is_subnormal.value),
        "is_normal": int(dut.is_normal.value),
        "is_inf": int(dut.is_inf.value),
        "is_nan": int(dut.is_nan.value),
        "is_snan": int(dut.is_snan.value),
        "is_qnan": int(dut.is_qnan.value),
    }


async def check_decode(dut, sb: Scoreboard, a: int, note: str = "") -> None:
    got = await read_decoder(dut, a)
    exp = decode_model(a)
    ctx = f"{note} a=0x{a:08x}"
    for field in exp:
        sb.check_equal(got[field], exp[field], f"{ctx} .{field}")


# --------------------------------------------------------------------------
# directed corner cases: one representative of every binary32 class
# --------------------------------------------------------------------------
DIRECTED = [
    ("+0", 0x00000000),
    ("-0", 0x80000000),
    ("smallest +subnormal", 0x00000001),
    ("smallest -subnormal", 0x80000001),
    ("largest +subnormal", 0x007FFFFF),
    ("largest -subnormal", 0x807FFFFF),
    ("smallest +normal", 0x00800000),
    ("smallest -normal", 0x80800000),
    ("+1.0", 0x3F800000),
    ("-1.0", 0xBF800000),
    ("largest +normal", 0x7F7FFFFF),
    ("largest -normal", 0xFF7FFFFF),
    ("+inf", 0x7F800000),
    ("-inf", 0xFF800000),
    ("qNaN", 0x7FC00000),
    ("-qNaN", 0xFFC00000),
    ("sNaN", 0x7F800001),
    ("-sNaN", 0xFF800001),
    ("sNaN with max payload", 0x7FBFFFFF),
    ("qNaN with max payload", 0x7FFFFFFF),
    ("+pi", R.f2b(3.14159274101257324)),
    ("-1e-38 (subnormal-ish)", R.f2b(-1e-38)),
    ("+1e38", R.f2b(1e38)),
]


@cocotb.test()
async def test_decoder_directed(dut):
    """Every binary32 class, decoded one by one."""
    sb = Scoreboard("decoder.directed")
    for note, a in DIRECTED:
        await check_decode(dut, sb, a, note)
    sb.log_summary(dut)
    sb.report()


@cocotb.test()
async def test_decoder_field_sweep(dut):
    """All 256 exponents x a spread of fractions (the interesting structure)."""
    sb = Scoreboard("decoder.field_sweep")
    fractions = [0x000000, 0x000001, 0x000002, 0x000003, 0x400000, 0x400001,
                 0x7FFFFE, 0x7FFFFF, 0x0000FF, 0x000100, 0x123456, 0x200000,
                 0x600000, 0x000800]
    for e in range(256):
        for f in fractions:
            for s in (0, 1):
                a = (s << 31) | (e << 23) | f
                await check_decode(dut, sb, a)
    sb.log_summary(dut)
    sb.report()


@cocotb.test()
async def test_decoder_random(dut):
    """Random bit patterns, biased towards extremes of the exponent range."""
    seed = int(cocotb.plusargs.get("seed", 0)) or 0xC0FFEE
    rng = random.Random(seed)
    sb = Scoreboard("decoder.random")
    for _ in range(MAX_RANDOM):
        if rng.random() < 0.4:
            a = rng.getrandbits(32)
        else:
            e = rng.choice([0, 0, 1, 2, 126, 127, 128, 253, 254, 255,
                            rng.randrange(256)])
            f = rng.getrandbits(23) if rng.random() < 0.5 else rng.choice(
                [0, 1, 2, 0x400000, 0x7FFFFF])
            a = (rng.getrandbits(1) << 31) | (e << 23) | f
        await check_decode(dut, sb, a)
    sb.log_summary(dut)
    sb.report()


@cocotb.test()
async def test_decoder_value_consistency(dut):
    """``sig * 2**exp`` must equal the exact value of the operand.

    This is the property the rest of the FPU relies on: normals and subnormals
    both come out as a unified 24-bit significand scaled by a signed exponent.
    """
    sb = Scoreboard("decoder.value_consistency")
    rng = random.Random(0xBEEF)
    values = [0x00800000, 0x00000001, 0x007FFFFF, 0x3F800000, 0x7F7FFFFF]
    values += [rng.getrandbits(32) for _ in range(4000)]

    for a in values:
        got = await read_decoder(dut, a)
        # The reference library shares this representation with every other
        # FPU unit's model, so keep the two pinned to each other.
        sb.check_equal(
            (got["exp"], got["sig"]), R.normalized(a),
            f"a=0x{a:08x} RTL decoder vs harness.lib.fp32.normalized()",
        )
        if R.is_nan(a) or R.is_inf(a):
            sb.check_equal((got["exp"], got["sig"]), (0, 0),
                           f"a=0x{a:08x} non-finite must decode to exp=sig=0")
            continue
        if R.is_zero(a):
            sb.check_equal((got["exp"], got["sig"]), (0, 0),
                           f"a=0x{a:08x} zero must decode to exp=sig=0")
            continue
        # The decoder's contract: sig is a 24-bit *integer* significand and
        # value = (-1)**sign * sig * 2**(exp-23).  (The RTL header comment
        # writes this as "sig * 2^exp", which reads as if sig were a fraction;
        # the exponent is really the exponent of sig's MSB.)
        reconstructed = R.Fraction(got["sig"]) * R.Fraction(2) ** (got["exp"] - 23)
        if got["sign"]:
            reconstructed = -reconstructed
        expected = R.value_of(a)
        sb.check_equal(
            reconstructed, expected,
            f"a=0x{a:08x} sig*2^exp mismatch (sig=0x{got['sig']:06x} exp={got['exp']})",
        )
        # The unified significand always has its MSB set for non-zero operands.
        sb.check_equal((got["sig"] >> 23) & 1, 1,
                       f"a=0x{a:08x} sig=0x{got['sig']:06x} MSB must be 1")
    sb.log_summary(dut)
    sb.report()
