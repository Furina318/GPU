"""Reference model for IEEE-754 binary32 (single precision).

This is the *golden model* the RTL is compared against.  It is deliberately
independent of the hardware: every arithmetic operation is evaluated exactly
with :class:`fractions.Fraction` and then rounded once, in software, according
to the requested RISC-V rounding mode.  That makes it valid for fused
operations (``a*b+c``) too, where a plain ``numpy.float32`` oracle would
double-round and give wrong answers.

Only binary32 is supported -- the project's FPU is single precision only.

Flag bit positions follow ``hw/rtl/fpu/fpu_config.vh``::

    NV=4  DZ=3  OF=2  UF=1  NX=0
"""

from __future__ import annotations

import math
import struct
from decimal import Decimal, localcontext
from fractions import Fraction

# -- fflags bit positions (mirror fpu_config.vh) ---------------------------
NV, DZ, OF, UF, NX = 4, 3, 2, 1, 0

# -- rounding modes (mirror fpu_config.vh) ---------------------------------
RNE, RTZ, RDN, RUP, RMM = 0b000, 0b001, 0b010, 0b011, 0b100

RM_NAMES = {RNE: "RNE", RTZ: "RTZ", RDN: "RDN", RUP: "RUP", RMM: "RMM"}

# -- opcodes (mirror fpu_config.vh) ----------------------------------------
OP_FADD_S = 0b001_0000
OP_FSUB_S = 0b001_0001
OP_FMUL_S = 0b001_0010
OP_FMADD_S = 0b001_0011
OP_FMSUB_S = 0b001_0100
OP_FNMADD_S = 0b001_0101
OP_FNMSUB_S = 0b001_0110

OP_FCLASS_S = 0b010_0000
OP_FMIN_S = 0b010_0001
OP_FMAX_S = 0b010_0010
OP_FLT_S = 0b010_0011
OP_FEQ_S = 0b010_0100
OP_FLE_S = 0b010_0101

OP_FSGNJ_S = 0b010_1001
OP_FSGNJN_S = 0b010_1010
OP_FSGNJX_S = 0b010_1100

OP_FCVT_W_S = 0b011_0000
OP_FCVT_WU_S = 0b011_0001
OP_FCVT_S_W = 0b011_0010
OP_FCVT_S_WU = 0b011_0011

OP_FDIV_S = 0b100_0000
OP_FMV_W_X = 0b101_0000
OP_FMV_X_W = 0b101_0001
OP_FSQRT_S = 0b110_0000

# -- binary32 constants ----------------------------------------------------
CANONICAL_NAN = 0x7FC00000
POS_INF = 0x7F800000
NEG_INF = 0xFF800000
MAX_FINITE = 0x7F7FFFFF

SIGN_BIT = 0x80000000
_MANT_MASK = 0x007FFFFF
_MANT_BITS = 23
_EXP_MASK = 0x7F800000

_TWO = Fraction(2)
_MIN_NORMAL_EXP = -126
_MAX_NORMAL_EXP = 127
_SUB_ULP_EXP = -149  # exponent of the smallest subnormal ulp


# --------------------------------------------------------------------------
# bit <-> value helpers
# --------------------------------------------------------------------------
def f2b(value: float) -> int:
    """float -> raw binary32 bits."""
    return struct.unpack("<I", struct.pack("<f", value))[0]


def b2f(bits: int) -> float:
    """raw binary32 bits -> float (Python float, exact for binary32)."""
    return struct.unpack("<f", struct.pack("<I", bits & 0xFFFFFFFF))[0]


def is_nan(bits: int) -> bool:
    return (bits & _EXP_MASK) == _EXP_MASK and (bits & _MANT_MASK) != 0


def is_snan(bits: int) -> bool:
    return is_nan(bits) and not (bits & 0x00400000)


def is_qnan(bits: int) -> bool:
    return is_nan(bits) and bool(bits & 0x00400000)


def is_inf(bits: int) -> bool:
    return (bits & _EXP_MASK) == _EXP_MASK and (bits & _MANT_MASK) == 0


def is_zero(bits: int) -> bool:
    return (bits & 0x7FFFFFFF) == 0


def is_subnormal(bits: int) -> bool:
    return (bits & _EXP_MASK) == 0 and (bits & _MANT_MASK) != 0


def is_normal(bits: int) -> bool:
    e = (bits >> 23) & 0xFF
    return 0 < e < 0xFF


def sign_of(bits: int) -> int:
    return (bits >> 31) & 1


def exponent_of(bits: int) -> int:
    """Unbiased exponent, or ``None`` for zero/inf/NaN."""
    e = (bits >> 23) & 0xFF
    if e == 0xFF:
        return None
    if e == 0:
        return None
    return e - 127


def significand_of(bits: int) -> int:
    """Explicit 24-bit significand (``1.fraction``), 0 for zero/subnormal-decoded."""
    e = (bits >> 23) & 0xFF
    frac = bits & _MANT_MASK
    if e == 0:
        return frac
    return frac | 0x00800000


def value_of(bits: int) -> Fraction | None:
    """Exact rational value of a finite operand; ``None`` for inf/NaN."""
    if is_nan(bits) or is_inf(bits):
        return None
    e = (bits >> 23) & 0xFF
    frac = bits & _MANT_MASK
    if e == 0:
        mag = Fraction(frac, 1 << 149)
    else:
        mag = Fraction(frac | 0x00800000, 1 << 23) * _TWO ** (e - 127)
    return -mag if sign_of(bits) else mag


def normalized(bits: int) -> tuple[int, int]:
    """The ``fpu_decoder`` view of a finite operand: ``(exp, sig)``.

    ``sig`` is a 24-bit *integer* significand whose MSB is always set for a
    non-zero operand, and ``exp`` is the exponent of that MSB, so::

        value = (-1)**sign * sig * 2**(exp - 23)

    Subnormals are shifted up and given ``exp < -126``; zero and non-finite
    operands decode to ``(0, 0)``.  Every unit downstream of the decoder
    (``fquick``, ``fcvt``, ``fdiv``, ``fsqrt``, ``fma_unit``) consumes exactly
    this representation, so the reference model shares it.
    """
    e = (bits >> 23) & 0xFF
    f = bits & _MANT_MASK
    if e == 0:
        if f == 0:
            return 0, 0  # zero
        lz = _MANT_BITS - f.bit_length()
        return (-127 - lz), ((f << (lz + 1)) & 0xFFFFFF)
    if e == 0xFF:
        return 0, 0  # inf / NaN
    return (e - 127), (0x800000 | f)


def class_bits(bits: int) -> int:
    """RISC-V ``FCLASS.S`` result bitmask for one operand.

    Bit *i* of the result is set according to the operand's class:
    0 -inf, 1 negative normal, 2 negative subnormal, 3 -0, 4 +0,
    5 positive subnormal, 6 positive normal, 7 +inf, 8 sNaN, 9 qNaN.
    """
    s = sign_of(bits)
    if is_nan(bits):
        return 1 << (8 if is_snan(bits) else 9)
    if is_inf(bits):
        return 1 << (0 if s else 7)
    if is_zero(bits):
        return 1 << (3 if s else 4)
    if is_subnormal(bits):
        return 1 << (2 if s else 5)
    return 1 << (1 if s else 6)


# --------------------------------------------------------------------------
# rounding unit (mirrors hw/rtl/fpu/frm.v)
# --------------------------------------------------------------------------
def frm_round_up(rm: int, sign: int, lsb: int, guard: int, rnd: int, sticky: int) -> int:
    """Decide whether to increment the magnitude, per ``frm.v``.

    ``sign`` is the sign of the *value* being rounded, so RDN raises the
    magnitude of a negative number and RUP raises that of a positive one.
    """
    any_gt = guard | rnd | sticky
    if rm == RNE:
        return guard & (rnd | sticky | lsb)
    if rm == RTZ:
        return 0
    if rm == RDN:
        return sign & any_gt
    if rm == RUP:
        return (not sign) & any_gt
    if rm == RMM:
        return guard
    return guard & (rnd | sticky | lsb)  # unknown rm behaves like RNE, as in frm.v


# --------------------------------------------------------------------------
# exact rounding core
# --------------------------------------------------------------------------
def _floor_log2(x: Fraction) -> int:
    """Return ``e`` with ``2**e <= x < 2**(e+1)`` for ``x > 0``."""
    n, d = x.numerator, x.denominator
    e = n.bit_length() - d.bit_length()
    if x < _TWO**e:
        e -= 1
    elif x >= _TWO ** (e + 1):
        e += 1
    return e


def round_binary32(
    magnitude: Fraction, rm: int, sign: int
) -> tuple[int, int]:
    """Round a non-negative exact rational to binary32.

    Returns ``(bits, fflags)``.  ``sign`` only affects the directed rounding
    modes (RDN/RUP) and the sign of a zero result, exactly as ``frm.v`` does.
    """
    flags = 0
    if magnitude == 0:
        return (sign << 31), flags

    e = _floor_log2(magnitude)
    # Quantisation exponent: 23 fraction bits for normals, but never finer than
    # the subnormal ulp exponent.
    q = max(e - 23, _SUB_ULP_EXP)
    scaled = magnitude / _TWO**q
    kept = scaled.numerator // scaled.denominator
    rem = scaled - kept

    # guard / round / sticky, mirroring the bit roles frm.v expects
    guard = 1 if rem >= Fraction(1, 2) else 0
    rest = rem * 2 - guard
    rnd = 1 if rest >= Fraction(1, 2) else 0
    sticky = 1 if (rest * 2 - rnd) > 0 else 0
    any_gt = guard | rnd | sticky
    lsb = kept & 1

    round_up = frm_round_up(rm, sign, lsb, guard, rnd, sticky)

    mant = kept + (1 if round_up else 0)
    if any_gt:
        flags |= 1 << NX

    # Renormalise: rounding may have pushed the significand past 2**24.
    if mant >= (1 << 24):
        mant >>= 1
        q += 1

    # Overflow / underflow bookkeeping.
    if q > _MAX_NORMAL_EXP - 23:
        flags |= (1 << OF) | (1 << NX)
        to_inf = rm in (RNE, RMM) or (rm == RDN and sign) or (rm == RUP and not sign)
        if to_inf:
            return ((sign << 31) | _EXP_MASK), flags
        return ((sign << 31) | MAX_FINITE), flags

    if q == _SUB_ULP_EXP:
        # Result is subnormal (or the smallest normal) -- tininess after
        # rounding: UF only when the result is subnormal and inexact.
        if mant < (1 << 23) and any_gt:
            flags |= 1 << UF
        bits = (sign << 31) | mant
        return bits, flags

    biased = q + 23 + 127
    bits = (sign << 31) | (biased << 23) | (mant & _MANT_MASK)
    return bits, flags


# --------------------------------------------------------------------------
# NaN / infinity plumbing shared by the arithmetic ops
# --------------------------------------------------------------------------
def _propagate_nan(operands: list[int]) -> int:
    """Return the NaN a RISC-V op should produce when any operand is NaN.

    RISC-V is deliberately payload-agnostic: *every* NaN-producing arithmetic
    operation yields the canonical quiet NaN (positive sign, all exponent bits
    set, only the MSB of the fraction set), and raises NV only if an input was
    signalling.  ``hw/rtl/fpu/fma_unit.v`` and ``fdiv.v`` both hardcode
    ``32'h7fc00000``, so this matches the RTL as well as the spec.
    """
    return CANONICAL_NAN


def _has_nan(*operands: int) -> bool:
    return any(is_nan(b) for b in operands)


def _invalid(*operands: int) -> bool:
    return any(is_snan(b) for b in operands)


# --------------------------------------------------------------------------
# arithmetic ops
# --------------------------------------------------------------------------
def _round_value(x: Fraction | None, rm: int) -> tuple[int, int]:
    """Round a *signed* exact rational (``None`` means a NaN was returned)."""
    if x is None:
        raise ValueError("use the NaN path instead")
    sign = 1 if x < 0 else 0
    return round_binary32(abs(x), rm, sign)


def op_add(a: int, b: int, rm: int = RNE) -> tuple[int, int]:
    if _has_nan(a, b):
        return _propagate_nan([a, b]), (1 << NV) if _invalid(a, b) else 0
    if is_inf(a) or is_inf(b):
        if is_inf(a) and is_inf(b) and sign_of(a) != sign_of(b):
            return CANONICAL_NAN, 1 << NV  # inf + -inf
        return (a if is_inf(a) else b), 0
    x = value_of(a) + value_of(b)  # type: ignore[operator]
    if x == 0:
        # IEEE: x + (-x) is +0 under every mode except RDN.
        if sign_of(a) == sign_of(b):
            return (sign_of(a) << 31), 0
        return ((1 if rm == RDN else 0) << 31), 0
    return _round_value(x, rm)


def op_sub(a: int, b: int, rm: int = RNE) -> tuple[int, int]:
    return op_add(a, b ^ SIGN_BIT, rm)


def op_mul(a: int, b: int, rm: int = RNE) -> tuple[int, int]:
    if _has_nan(a, b):
        return _propagate_nan([a, b]), (1 << NV) if _invalid(a, b) else 0
    sign = sign_of(a) ^ sign_of(b)
    if is_inf(a) or is_inf(b):
        if is_zero(a) or is_zero(b):
            return CANONICAL_NAN, 1 << NV  # 0 * inf
        return ((sign << 31) | _EXP_MASK), 0
    x = value_of(a) * value_of(b)  # type: ignore[operator]
    if x == 0:
        return (sign << 31), 0
    return round_binary32(abs(x), rm, sign)


def op_div(a: int, b: int, rm: int = RNE) -> tuple[int, int]:
    if _has_nan(a, b):
        return _propagate_nan([a, b]), (1 << NV) if _invalid(a, b) else 0
    sign = sign_of(a) ^ sign_of(b)
    if is_inf(a) and is_inf(b):
        return CANONICAL_NAN, 1 << NV
    if is_zero(a) and is_zero(b):
        return CANONICAL_NAN, 1 << NV
    if is_inf(a):
        return ((sign << 31) | _EXP_MASK), 0
    if is_inf(b):
        return (sign << 31), 0
    if is_zero(b):
        return ((sign << 31) | _EXP_MASK), 1 << DZ
    if is_zero(a):
        return (sign << 31), 0
    x = value_of(a) / value_of(b)  # type: ignore[operator]
    return round_binary32(abs(x), rm, sign)


def _sqrt_exact(magnitude: Fraction) -> Fraction:
    """A rational approximation of ``sqrt(magnitude)`` good to ~50 digits.

    Sound as an oracle because ``sqrt`` of a non-perfect-square is irrational
    and therefore can never land exactly on a rounding tie; perfect squares are
    produced exactly by :mod:`decimal`.
    """
    with localcontext() as ctx:
        ctx.prec = 60
        d = Decimal(magnitude.numerator) / Decimal(magnitude.denominator)
        return Fraction(d.sqrt())


def op_sqrt(a: int, rm: int = RNE) -> tuple[int, int]:
    if is_nan(a):
        return _propagate_nan([a]), (1 << NV) if is_snan(a) else 0
    if is_zero(a):
        return a, 0  # sqrt(+-0) = +-0
    if sign_of(a):
        return CANONICAL_NAN, 1 << NV  # sqrt of a negative number
    if is_inf(a):
        return a, 0
    x = _sqrt_exact(value_of(a))  # type: ignore[arg-type]
    return round_binary32(x, rm, 0)


def op_fma(a: int, b: int, c: int, rm: int = RNE, *, kind: str = "madd") -> tuple[int, int]:
    """Fused multiply-add family.

    ``kind`` selects the RISC-V variant:
    ``madd`` a*b+c, ``msub`` a*b-c, ``nmadd`` -(a*b)-c, ``nmsub`` -(a*b)+c.
    The product is *not* rounded before the addition.
    """
    neg_prod = kind in ("nmadd", "nmsub")
    neg_c = kind in ("msub", "nmadd")

    if _has_nan(a, b, c):
        return _propagate_nan([a, b, c]), (1 << NV) if _invalid(a, b, c) else 0

    # 0 * inf is invalid regardless of the addend.
    if (is_zero(a) and is_inf(b)) or (is_inf(a) and is_zero(b)):
        return CANONICAL_NAN, 1 << NV

    prod_sign = sign_of(a) ^ sign_of(b) ^ (1 if neg_prod else 0)
    c_sign = sign_of(c) ^ (1 if neg_c else 0)

    if is_inf(a) or is_inf(b):
        if is_inf(c):
            if prod_sign != c_sign:
                return CANONICAL_NAN, 1 << NV  # inf - inf
            return ((prod_sign << 31) | _EXP_MASK), 0
        return ((prod_sign << 31) | _EXP_MASK), 0

    prod = value_of(a) * value_of(b)  # type: ignore[operator]
    if neg_prod:
        prod = -prod

    if is_inf(c):
        return ((c_sign << 31) | _EXP_MASK), 0

    addend = value_of(c)  # type: ignore[assignment]
    if neg_c:
        addend = -addend

    total = prod + addend
    if total == 0:
        # A zero sum takes the sign of the surviving addend, or +0 (RDN: -0).
        if prod == 0 and addend == 0:
            if prod_sign == c_sign:
                return (prod_sign << 31), 0
            return ((1 if rm == RDN else 0) << 31), 0
        if total == 0 and prod != 0:
            return (prod_sign << 31), 0
        return (c_sign << 31), 0
    return _round_value(total, rm)


def op_cmp(op: int, a: int, b: int) -> tuple[int, int]:
    """Floating-point compare: returns ``(0|1, fflags)`` like the RTL does."""
    if op == OP_FEQ_S:
        # RISC-V: FEQ signals invalid only for signalling NaNs.
        flags = (1 << NV) if (_invalid(a, b)) else 0
        res = 1 if (not _has_nan(a, b) and _cmp_equal(a, b)) else 0
        return res, flags
    # FLT / FLE signal invalid for any NaN.
    flags = (1 << NV) if _has_nan(a, b) else 0
    if _has_nan(a, b):
        return 0, flags
    if op == OP_FLT_S:
        return (1 if _cmp_lt(a, b) else 0), flags
    return (1 if (_cmp_lt(a, b) or _cmp_equal(a, b)) else 0), flags


def _cmp_equal(a: int, b: int) -> bool:
    if is_zero(a) and is_zero(b):
        return True  # +0 == -0
    if is_inf(a) or is_inf(b):
        # Infinities have no finite value, so they must be compared by bits --
        # otherwise value_of() returns None for both and None == None would
        # make +inf "equal" to -inf.
        return is_inf(a) and is_inf(b) and sign_of(a) == sign_of(b)
    return value_of(a) == value_of(b)


def _cmp_lt(a: int, b: int) -> bool:
    if is_zero(a) and is_zero(b):
        return False
    if is_inf(a) or is_inf(b):
        sa, sb = sign_of(a), sign_of(b)
        if is_inf(a) and is_inf(b):
            return bool(sa and not sb)
        if is_inf(a):
            return bool(sa)
        return bool(not sb)
    return value_of(a) < value_of(b)  # type: ignore[operator]


def op_minmax(op: int, a: int, b: int) -> tuple[int, int]:
    """RISC-V FMIN.S / FMAX.S, including the ±0 and NaN rules."""
    flags = (1 << NV) if _invalid(a, b) else 0
    nan_a, nan_b = is_nan(a), is_nan(b)
    is_min = op == OP_FMIN_S
    if nan_a and nan_b:
        return CANONICAL_NAN, flags
    if nan_a:
        return b, flags
    if nan_b:
        return a, flags
    if is_zero(a) and is_zero(b):
        # min(-0,+0) = -0 ; max(-0,+0) = +0.  Equal-signed zeros are not a
        # special case: max(-0,-0) is simply -0.
        if sign_of(a) == sign_of(b):
            return a, flags
        return (SIGN_BIT if is_min else 0), flags
    if _cmp_lt(a, b):
        return (a if is_min else b), flags
    if _cmp_lt(b, a):
        return (b if is_min else a), flags
    return a, flags


# --------------------------------------------------------------------------
# float <-> integer conversions (mirrors hw/rtl/fpu/fcvt.v)
# --------------------------------------------------------------------------
def _f2i_decompose(src_exp: int, src_sig: int) -> tuple[int, int, int, int]:
    """Truncated integer magnitude plus its guard/round/sticky bits.

    ``src_sig`` is the decoder's 24-bit integer significand and ``src_exp`` the
    exponent of its MSB, so the value is ``src_sig * 2**(src_exp-23)``.
    """
    if src_exp >= 23:
        if src_exp > 86:
            return 0xFFFFFFFFFFFFFFFF, 0, 0, 0  # saturate; caller sees overflow
        mag = src_sig << (src_exp - 23)
        return mag, 0, 0, 0
    rs = 23 - src_exp
    if rs > 24:
        return 0, 0, 0, (1 if src_sig else 0)
    mag = src_sig >> rs
    g = (src_sig >> (rs - 1)) & 1 if rs >= 1 else 0
    r = (src_sig >> (rs - 2)) & 1 if rs >= 2 else 0
    s = 1 if (rs >= 3 and (src_sig & ((1 << (rs - 2)) - 1))) else 0
    return mag, g, r, s


def op_fcvt_w_s(a: int, rm: int = RNE) -> tuple[int, int]:
    """FCVT.W.S -- float to signed 32-bit integer."""
    # NaN / infinity saturate and signal invalid.
    if is_nan(a) or is_inf(a):
        if is_nan(a):
            return 0x7FFFFFFF, 1 << NV
        return (0x80000000 if sign_of(a) else 0x7FFFFFFF), 1 << NV
    if is_zero(a):
        return 0, 0

    neg = sign_of(a)
    _e, _s = normalized(a)
    mag, g, r, s = _f2i_decompose(_e, _s)
    round_up = frm_round_up(rm, neg, mag & 1, g, r, s)
    inexact = g | r | s
    mag_final = mag + round_up

    limit = 1 << 31
    if mag > limit or (mag == limit and not neg):
        return (0x80000000 if neg else 0x7FFFFFFF), 1 << NV
    if mag_final > limit or (mag_final == limit and not neg):
        return (0x80000000 if neg else 0x7FFFFFFF), 1 << NV
    if neg:
        # The inexact flag applies to negative results too -- forgetting it here
        # was caught by the RTL, which sets NX for e.g. fcvt.w.s(-1.5).
        return (~mag_final + 1) & 0xFFFFFFFF, (1 << NX) if inexact else 0
    return mag_final & 0xFFFFFFFF, (1 << NX) if inexact else 0


def op_fcvt_wu_s(a: int, rm: int = RNE) -> tuple[int, int]:
    """FCVT.WU.S -- float to unsigned 32-bit integer."""
    if is_nan(a) or is_inf(a):
        if is_inf(a) and sign_of(a):
            return 0, 1 << NV
        return 0xFFFFFFFF, 1 << NV
    if is_zero(a):
        return 0, 0

    neg = sign_of(a)
    _e, _s = normalized(a)
    mag, g, r, s = _f2i_decompose(_e, _s)
    round_up = frm_round_up(rm, neg, mag & 1, g, r, s)
    inexact = g | r | s
    mag_final = mag + round_up

    if (not neg) and mag > 0xFFFFFFFF:
        return 0xFFFFFFFF, 1 << NV
    if neg and mag_final != 0:
        return 0, 1 << NV
    if neg:  # negative value that rounds to zero: only inexact
        return 0, (1 << NX) if inexact else 0
    if mag_final > 0xFFFFFFFF:
        return 0xFFFFFFFF, 1 << NV
    return mag_final & 0xFFFFFFFF, (1 << NX) if inexact else 0


def _i2f_decompose(mag: int) -> tuple[int, int, int, int, int]:
    """24-bit significand, exponent, and guard/round/sticky for an integer."""
    msb = mag.bit_length() - 1
    if msb >= 23:
        rs = msb - 23
        sig = (mag >> rs) & 0xFFFFFF
        g = (mag >> (rs - 1)) & 1 if rs >= 1 else 0
        r = (mag >> (rs - 2)) & 1 if rs >= 2 else 0
        s = 1 if (rs >= 3 and (mag & ((1 << (rs - 2)) - 1))) else 0
    else:
        sig = (mag << (23 - msb)) & 0xFFFFFF
        g = r = s = 0
    return sig, msb, g, r, s


def op_fcvt_s_w(a: int, rm: int = RNE) -> tuple[int, int]:
    """FCVT.S.W -- signed 32-bit integer to float."""
    neg = (a >> 31) & 1
    mag = ((~a + 1) & 0xFFFFFFFF) if neg else (a & 0xFFFFFFFF)
    if mag == 0:
        return 0x00000000, 0  # integer zero is always +0

    sig, exp, g, r, s = _i2f_decompose(mag)
    round_up = frm_round_up(rm, neg, sig & 1, g, r, s)
    inexact = g | r | s
    rounded = sig + round_up
    exp_final = exp
    sig_final = rounded & 0xFFFFFF
    if rounded & 0x1000000:
        sig_final = 0x800000
        exp_final = exp + 1
    bits = (neg << 31) | (((exp_final + 127) & 0xFF) << 23) | (sig_final & 0x7FFFFF)
    return bits, (1 << NX) if inexact else 0


def op_fcvt_s_wu(a: int, rm: int = RNE) -> tuple[int, int]:
    """FCVT.S.WU -- unsigned 32-bit integer to float."""
    mag = a & 0xFFFFFFFF
    if mag == 0:
        return 0x00000000, 0

    sig, exp, g, r, s = _i2f_decompose(mag)
    round_up = frm_round_up(rm, 0, sig & 1, g, r, s)
    inexact = g | r | s
    rounded = sig + round_up
    exp_final = exp
    sig_final = rounded & 0xFFFFFF
    if rounded & 0x1000000:
        sig_final = 0x800000
        exp_final = exp + 1
    bits = ((exp_final + 127) & 0xFF) << 23 | (sig_final & 0x7FFFFF)
    return bits, (1 << NX) if inexact else 0


# --------------------------------------------------------------------------
# bit-manipulation ops that need no arithmetic model
# --------------------------------------------------------------------------
def op_sgnj(op: int, a: int, b: int) -> int:
    """FSGNJ / FSGNJN / FSGNJX -- take the magnitude of ``a``, sign from ``b``."""
    if op == OP_FSGNJ_S:
        return (b & SIGN_BIT) | (a & ~SIGN_BIT)
    if op == OP_FSGNJN_S:
        return ((~b) & SIGN_BIT) | (a & ~SIGN_BIT)
    return ((a ^ b) & SIGN_BIT) | (a & ~SIGN_BIT)  # FSGNJX


def op_mv_x_w(a: int) -> int:
    """FMV.X.W -- the raw bit pattern, sign-extended like the RTL does."""
    return a


def op_mv_w_x(a: int) -> int:
    """FMV.W.X -- the raw bit pattern, unchanged."""
    return a


# --------------------------------------------------------------------------
# helpers for classifying discrepancies (used by the defect-pinning tests)
# --------------------------------------------------------------------------
def exact_mul_add(a: int, b: int, c: int, kind: str = "madd") -> Fraction | None:
    """Exact value of a fused multiply-add, or ``None`` if any input is non-finite."""
    bits = (a, b, c)
    if any(is_nan(x) or is_inf(x) for x in bits):
        return None
    prod = value_of(a) * value_of(b)
    add = value_of(c)
    if kind in ("nmadd", "nmsub"):
        prod = -prod
    if kind in ("msub", "nmadd"):
        add = -add
    return prod + add


def is_rounding_tie(magnitude: Fraction) -> bool:
    """True when ``magnitude`` sits exactly on a binary32 rounding midpoint.

    At such a point RNE and RMM disagree: RNE picks the neighbour with an even
    significand, RMM always picks the larger magnitude.
    """
    if magnitude == 0:
        return False
    e = _floor_log2(magnitude)
    q = max(e - 23, _SUB_ULP_EXP)
    scaled = magnitude / _TWO**q
    kept = scaled.numerator // scaled.denominator
    rem = scaled - kept
    guard = 1 if rem >= Fraction(1, 2) else 0
    return guard == 1 and (rem * 2 - guard) == 0


def rounding_gap_ratio(magnitude: Fraction) -> Fraction:
    """Relative distance from ``magnitude`` to the nearest binary32 midpoint.

    A value whose gap is astronomically small (say below 2**-30) can only be
    rounded correctly by an adder that carries a *fully exact* sticky bit; such
    vectors are the ones that expose sticky-bit bugs.
    """
    if magnitude == 0:
        return Fraction(1)
    mag = abs(magnitude)
    e = _floor_log2(mag)
    q = max(e - 23, _SUB_ULP_EXP)
    scaled = mag / _TWO**q          # in units of the result's ulp
    kept = scaled.numerator // scaled.denominator
    rem = scaled - kept
    # Relative gap: the distance from the midpoint, measured in ulps, divided by
    # the value itself (which is `scaled` ulps).
    return abs(rem - Fraction(1, 2)) / scaled
