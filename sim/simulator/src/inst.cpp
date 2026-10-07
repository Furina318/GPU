#include "inst.hpp"

#include <cmath>
#include <cstring>

#ifdef __FAST_MATH__
#error "inst.cpp 依赖 IEEE-754 语义(NaN/次正规/单次舍入),不能用 -ffast-math / -Ofast 编译"
#endif

namespace simu {

// 类型转换
static inline float    as_f(uint32_t u) { float f; std::memcpy(&f, &u, 4); return f; }
static inline uint32_t as_u(float f)    { uint32_t u; std::memcpy(&u, &f, 4); return u; }

static constexpr uint32_t CANON_NAN = 0x7FC00000u;
static constexpr int32_t  I32_MIN   = (-2147483647 - 1);

static inline uint32_t canon_nan(uint32_t u) { return std::isnan(as_f(u)) ? CANON_NAN : u; }

// 算术右移
static inline uint32_t asr(uint32_t x, uint32_t sh) {
    if (!sh) return x;
    if (!(x >> 31)) return x >> sh;
    return ~(~x >> sh);
}

static uint32_t div_s(uint32_t a, uint32_t b) {
    const int32_t x = int32_t(a), y = int32_t(b);
    if (y == 0) return 0xFFFFFFFFu;              
    if (x == I32_MIN && y == -1) return a;      
    return uint32_t(x / y);
}

static uint32_t rem_s(uint32_t a, uint32_t b) {
    const int32_t x = int32_t(a), y = int32_t(b);
    if (y == 0) return a;                        
    if (x == I32_MIN && y == -1) return 0;       
    return uint32_t(x % y);
}

uint32_t alu_r(uint32_t funct4, uint32_t a, uint32_t b) {
    switch (funct4) {
    case 0b0000: return a + b;                                   // add
    case 0b0001: return a - b;                                   // sub
    case 0b0010: return a & b;                                   // and
    case 0b0011: return a | b;                                   // or
    case 0b0100: return a ^ b;                                   // xor
    case 0b0101: return a << (b & 31);                           // sll
    case 0b0110: return a >> (b & 31);                           // srl
    case 0b0111: return asr(a, b & 31);                          // sra
    case 0b1000: return int32_t(a) < int32_t(b) ? 1u : 0u;       // slt
    case 0b1001: return a < b ? 1u : 0u;                         // sltu
    case 0b1010: return uint32_t(uint64_t(a) * uint64_t(b));     // mul(低 32 位)
    case 0b1011: return div_s(a, b);                             // div
    case 0b1100: return b ? a / b : 0xFFFFFFFFu;                 // divu
    case 0b1101: return rem_s(a, b);                             // rem
    case 0b1110: return b ? a % b : a;                           // remu
    default:     return 0;
    }
}

uint32_t alu_imm(uint32_t funct3, uint32_t a, int32_t imm_raw) {
    const uint32_t sh = uint32_t(imm_raw) & 31;
    switch (funct3) {
    case 0b000: return a + uint32_t(imm_raw);                                // addi
    case 0b001: return a << sh;                                              // slli
    case 0b010: return int32_t(a) < imm_raw ? 1u : 0u;                       // slti
    case 0b011: return a < uint32_t(imm_raw) ? 1u : 0u;                      // sltiu(立即数先符号扩展再按无符号比)
    case 0b100: return a ^ uint32_t(imm_raw);                                // xori
    case 0b101: return (uint32_t(imm_raw) & 0x20) ? asr(a, sh) : (a >> sh);  // srai / srli
    case 0b110: return a | uint32_t(imm_raw);                                // ori
    case 0b111: return a & uint32_t(imm_raw);                                // andi
    default:    return 0;
    }
}

uint32_t imad(uint32_t a, uint32_t b, uint32_t c) {
    return uint32_t(uint64_t(a) * uint64_t(b)) + c;
}

// FP32
static uint32_t fp_min(uint32_t a, uint32_t b) {
    const float x = as_f(a), y = as_f(b);
    if (std::isnan(x) && std::isnan(y)) return CANON_NAN;
    if (std::isnan(x)) return b;
    if (std::isnan(y)) return a;
    if (x == y) return (a & 0x80000000u) ? a : b;
    return x < y ? a : b;
}

static uint32_t fp_max(uint32_t a, uint32_t b) {
    const float x = as_f(a), y = as_f(b);
    if (std::isnan(x) && std::isnan(y)) return CANON_NAN;
    if (std::isnan(x)) return b;
    if (std::isnan(y)) return a;
    if (x == y) return (a & 0x80000000u) ? b : a;
    return x > y ? a : b;
}

// FP32 → INT32:RNE
uint32_t fp_f2i(uint32_t a) {
    const float x = as_f(a);
    if (std::isnan(x))        return 0x7FFFFFFFu;   // NaN → 最大正数(与 RV32F fcvt.w.s 一致)
    if (x >= 2147483648.0f)   return 0x7FFFFFFFu;   // 正溢出 / +inf
    if (x <  -2147483648.0f)  return 0x80000000u;   // 负溢出 / -inf
    return uint32_t(int32_t(x));                    // 向零截断
}

// INT32 → FP32:RNE
uint32_t fp_i2f(uint32_t a) { return as_u(float(int32_t(a))); }

uint32_t fp_r(uint32_t funct4, uint32_t a, uint32_t b) {
    switch (funct4) {
    case 0b0000: return canon_nan(as_u(as_f(a) + as_f(b)));    // fadd
    case 0b0001: return canon_nan(as_u(as_f(a) - as_f(b)));    // fsub
    case 0b0010: return canon_nan(as_u(as_f(a) * as_f(b)));    // fmul
    case 0b0011: return fp_min(a, b);                          // fmin
    case 0b0100: return fp_max(a, b);                          // fmax
    case 0b0101: return fp_f2i(a);                             // f2i (rs2 恒为 r0)
    case 0b0110: return fp_i2f(a);                             // i2f
    default:     return 0;
    }
}

uint32_t fp_fma(uint32_t funct2, uint32_t a, uint32_t b, uint32_t c) {
    const float x = as_f(a), y = as_f(b), z = as_f(c);
    switch (funct2) {
    case 0b00: return canon_nan(as_u(std::fmaf( x, y,  z)));   
    case 0b01: return canon_nan(as_u(std::fmaf( x, y, -z)));   
    case 0b10: return canon_nan(as_u(std::fmaf(-x, y,  z)));   
    case 0b11: return canon_nan(as_u(std::fmaf(-x, y, -z)));   
    default:   return 0;
    }
}

// 比较 谓词
bool setp_eval(uint32_t cond, uint32_t family, uint32_t a, uint32_t b) {
    if (family == 0) {                                   // 有符号整数
        const int32_t x = int32_t(a), y = int32_t(b);
        switch (cond) {
        case 0: return x == y;
        case 1: return x != y;
        case 2: return x <  y;
        case 3: return x >= y;
        case 4: return x <= y;
        case 5: return x >  y;
        }
    } else if (family == 1) {                            // 无符号整数
        switch (cond) {
        case 0: return a == b;
        case 1: return a != b;
        case 2: return a <  b;
        case 3: return a >= b;
        case 4: return a <= b;
        case 5: return a >  b;
        }
    } else if (family == 2) {                            // FP32:有序比较,NaN 时除 fne 外全为假
        const float x = as_f(a), y = as_f(b);
        const bool un = std::isnan(x) || std::isnan(y);
        switch (cond) {
        case 0: return !un && x == y;                    // feq
        case 1: return  un || x != y;                    // fne = !feq
        case 2: return !un && x <  y;                    // flt
        case 3: return !un && x >= y;                    // fge
        case 4: return !un && x <= y;                    // fle
        case 5: return !un && x >  y;                    // fgt
        }
    }
    return false;
}

// lane 掩码
uint8_t effective_mask(const Warp& w, const DecodedInst& d) {
    const uint8_t m = w.active_mask;
    if (!d.pred.used) return m; 
    uint8_t pm = 0;
    for (int l = 0; l < simu::WARP_SIZE; ++l) {
        bool v = (d.pred.psel == 0) ? true : (P(w.rf, l, d.pred.psel) != 0);
        if (d.pred.inv) v = !v;
        if (v) pm = uint8_t(pm | (1u << l));
    }
    return uint8_t(m & pm);
}

// warp 级执行
bool exec_alu(Warp& w, const DecodedInst& d) {
    const uint32_t op = d.raw & 0x7F;
    const bool is_int_r = (op == OP_R  && d.funct3 == 0b000);
    const bool is_i_imm = (op == OP_IMM);
    const bool is_lui   = (op == OP_LUI);
    const bool is_imad  = (op == OP_R4 && d.funct3 == 0b001);
    if (!is_int_r && !is_i_imm && !is_lui && !is_imad) return false;

    const uint8_t eff = effective_mask(w, d);
    for (int l = 0; l < simu::WARP_SIZE; ++l) {
        if (!(eff >> l & 1)) continue; // lane 不在有效掩码内
        uint32_t v;
        if (is_int_r)       v = alu_r(d.funct4, R(w.rf, l, d.rs1), R(w.rf, l, d.rs2));
        else if (is_lui)    v = uint32_t(d.imm);             // decode 已按 imm20<<12 算好
        else if (is_imad)   v = imad(R(w.rf, l, d.rs1), R(w.rf, l, d.rs2), R(w.rf, l, d.rs3));
        else if (is_i_imm)  v = alu_imm(d.funct3, R(w.rf, l, d.rs1), d.imm_raw);
        else                v = uint32_t(d.imm);              
        if (d.rd) R(w.rf, l, d.rd) = v;                 
    }
    return true;
}

bool exec_fp32(Warp& w, const DecodedInst& d) {
    const uint32_t op = d.raw & 0x7F;
    const bool is_fp_r = (op == OP_R  && d.funct3 == 0b001);
    const bool is_fma  = (op == OP_R4 && d.funct3 == 0b000);
    if (!is_fp_r && !is_fma) return false;

    const uint8_t eff = effective_mask(w, d);
    for (int l = 0; l < simu::WARP_SIZE; ++l) {
        if (!(eff >> l & 1)) continue;
        const uint32_t v = is_fma ? fp_fma(d.funct2, R(w.rf, l, d.rs1), R(w.rf, l, d.rs2), R(w.rf, l, d.rs3))
                                  : fp_r(d.funct4, R(w.rf, l, d.rs1), R(w.rf, l, d.rs2));
        if (d.rd) R(w.rf, l, d.rd) = v;
    }
    return true;
}

bool exec_setp(Warp& w, const DecodedInst& d) {
    if ((d.raw & 0x7F) != OP_SETP) return false;
    if (d.pdest == 0 || d.pdest >= simu::NUM_PRED) return false;   // p0 不可写(§5.4)

    const uint8_t eff = effective_mask(w, d);
    for (int l = 0; l < simu::WARP_SIZE; ++l) {
        if (!(eff >> l & 1)) continue;    
        P(w.rf, l, d.pdest) = setp_eval(d.cond, d.family, R(w.rf, l, d.rs1), R(w.rf, l, d.rs2)) ? 1 : 0;
}
    return true;
}

}  // namespace simu
