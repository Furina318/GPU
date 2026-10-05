#pragma once

#include "debug.hpp"
#include <cstdint>
#include <cstring>
#include <string>

enum class Fmt {
    R,      // rd, rs1, rs2
    I,      // rd, rs1, imm9
    S,      // rs2, rs1, imm9
    B,      // br.simt: imm22 << 2
    U,      // lui / ipdom: rd(ipdom 恒 0), imm20
    R4,     // rs3, rs2, rs1 (无谓词字段)
    SETP,   // pdest, rs1, rs2 + cond/family
    CTRL,   // bar.sync / exit / tmov / wmov (I 型字段)
};

struct PredRef {
    uint8_t psel = 0;
    bool    inv  = false;
    bool    used = false;       // 有无谓词字段 
    int     index() const { return used ? (inv ? (psel | 4) : psel) : -1; }
};

struct DecodedInst {
    const char* name = nullptr;         
    Fmt         fmt  = Fmt::R;
    uint32_t    raw  = 0;

    PredRef pred;
    uint8_t rd = 0, rs1 = 0, rs2 = 0, rs3 = 0;
    int32_t imm     = 0;                
    int32_t imm_raw = 0;                // 原始立即数字段(未移位
    uint8_t shamt   = 0;                // 移位类的移位量

    uint8_t funct3 = 0, funct4 = 0, funct2 = 0;
    uint8_t cond = 0, family = 0, pdest = 0;    // setp

    bool is_branch  = false;            // br.simt
    bool is_ipdom   = false;
    bool is_barrier = false;            // bar.sync
    bool is_exit    = false;
    bool is_fp      = false;            // 走 FPU
    bool is_load    = false;            // ld.g / ld.s
    bool is_store   = false;            // st.g / st.s
    bool is_shared  = false;            // 访存目标是共享内存(ld.s/st.s)
    bool writes_gpr = false;            // 写回 rd(不含 setp/分支/exit/bar/ipdom)
    bool writes_pred = false;           // 写谓词 p1..p3(setp)

    bool        matched = false;
    bool        ok      = false;
    const char* error   = nullptr;      // ok=false 时的原因

    uint32_t target(uint32_t pc) const { return uint32_t(pc + uint32_t(imm)); }
};

constexpr uint32_t OP_LD    = 0b0000011;
constexpr uint32_t OP_ST    = 0b0100011;
constexpr uint32_t OP_IMM   = 0b0010011;
constexpr uint32_t OP_LUI   = 0b0110111;
constexpr uint32_t OP_R     = 0b0110011;
constexpr uint32_t OP_R4    = 0b1010011;
constexpr uint32_t OP_SETP  = 0b0001011;
constexpr uint32_t OP_CTRL  = 0b0101011;
constexpr uint32_t OP_IPDOM = 0b1010111;
constexpr uint32_t OP_BR    = 0b1100011;

uint32_t    field(uint32_t inst, int hi, int lo);
int32_t     sign_extend(uint32_t v, int bits);
void        decode_operands(uint32_t inst, Fmt fmt, DecodedInst& d);
bool        validate(DecodedInst& d);
bool        decode(uint32_t inst, DecodedInst& out);
std::string itrace(const DecodedInst& d, uint32_t pc = 0);

struct PatBits { uint32_t key, mask; };

constexpr bool pat_is_space(char c) { return c == ' ' || c == '\t'; }

constexpr int pat_width(const char* s, int n) {
    int w = 0;
    for (int i = 0; i < n; ++i) w += pat_is_space(s[i]) ? 0 : 1;
    return w;
}

constexpr bool pat_valid(const char* s, int n) {
    if (pat_width(s, n) != 32) return false;
    for (int i = 0; i < n; ++i)
        if (!pat_is_space(s[i]) && s[i] != '0' && s[i] != '1' && s[i] != '?') return false;
    return true;
}

constexpr PatBits pattern_bits(const char* s, int n) {
    PatBits pb{0, 0};
    int bit = 0;                                  // 模式末尾 = inst[0]
    for (int i = n - 1; i >= 0; --i) {
        if (pat_is_space(s[i])) continue;
        if (s[i] == '1') { pb.key |= 1u << bit; pb.mask |= 1u << bit; }
        else if (s[i] == '0') { pb.mask |= 1u << bit; }
        ++bit;
    }
    return pb;
}

inline void pattern_decode(const char* str, uint32_t& key, uint32_t& mask) {
    PatBits pb = pattern_bits(str, int(std::strlen(str)));
    key  = pb.key;
    mask = pb.mask;
}

// ── 模式链 ──
// INSTPAT_START(x) ... INSTPAT(...) ... INSTPAT_END(x): 命中即跳到链尾,
// 之后执行 decode() 里的校验代码。要求作用域内存在 inst 与 out。
#define concat(a, b) a##b

#define INSTPAT_START(name) { const void* __instpat_end = &&concat(__instpat_end_, name);
#define INSTPAT_END(name)   concat(__instpat_end_, name): ; }

#define INSTPAT(pattern, mnemo, kind) do { \
    static_assert(pat_valid(pattern, sizeof(pattern) - 1), \
                  "INSTPAT 模式非法(需 32 位 0/1/?, 空格随意): " pattern); \
    static constexpr PatBits pb = pattern_bits(pattern, sizeof(pattern) - 1); \
    if ((inst & pb.mask) == pb.key) { \
        out.matched = true; \
        out.name = #mnemo; \
        out.fmt  = Fmt::kind; \
        decode_operands(inst, Fmt::kind, out); \
        goto *__instpat_end; \
    } \
} while (0)
