#pragma once

#include "decode.hpp"
#include "regfile.hpp"
#include "warp.hpp"

#include <cstdint>

namespace simu {

uint32_t alu_r(uint32_t funct4, uint32_t a, uint32_t b);        // 整数 R 型    
uint32_t alu_imm(uint32_t funct3, uint32_t a, int32_t imm_raw); // 立即数 I 型 
uint32_t imad(uint32_t a, uint32_t b, uint32_t c);              // a*b + c   

uint32_t fp_r(uint32_t funct4, uint32_t a, uint32_t b);                // fadd/fsub/fmul/fmin/fmax/f2i/i2f 
uint32_t fp_fma(uint32_t funct2, uint32_t a, uint32_t b, uint32_t c);  // fma 家族 

bool setp_eval(uint32_t cond, uint32_t family, uint32_t a, uint32_t b);

// lane 掩码与写回
uint8_t effective_mask(const Warp& w, const DecodedInst& d);

bool exec_alu(Warp& w, const DecodedInst& d);     // 整数 ALU / 立即数 / lui / imad
bool exec_fp32(Warp& w, const DecodedInst& d);    // FP32 算术 / 转换 / fma 家族
bool exec_setp(Warp& w, const DecodedInst& d);    // 比较 → 谓词

}  // namespace simu
