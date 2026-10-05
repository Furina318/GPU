#pragma once

#include <cstdint>
#include <cstring>
#include <vector>

namespace simu {
    constexpr int WARP_SIZE   = 8;
    constexpr int NUM_SM      = 2;
    constexpr int WARPS_PER_SM = 4;
    constexpr int MAX_WARPS   = NUM_SM * WARPS_PER_SM;  // 8

    constexpr int NUM_GPR     = 32;
    constexpr int NUM_PRED    = 4;

    constexpr int SIMT_STACK_DEPTH = 8;
    constexpr int SHARED_SIZE      = 16 * 1024;
    constexpr int INSTR_SRAM_SIZE  = 16 * 1024;

    constexpr uint32_t CONST_BASE = 0x0200'0000;
    constexpr uint32_t FB_BASE    = 0x1000'0000;
    constexpr uint32_t MMIO_BASE  = 0x0000'0000;
    constexpr uint32_t DDR_BASE   = 0x8000'0000;
}

struct StackFrame {
    uint32_t recov_pc;
    uint32_t fall_pc;
    uint8_t  mask_full;
    uint8_t  mask_pending;
    bool     pending;
};

struct SimtStack {
    StackFrame frames[simu::SIMT_STACK_DEPTH];
    int top = 0;   // 0 = 空

    bool empty() const { return top == 0; }
    bool full()  const { return top >= simu::SIMT_STACK_DEPTH; }

    void push(const StackFrame& f) { frames[top++] = f; }
    StackFrame pop() { return frames[--top]; }
    StackFrame& peek() { return frames[top - 1]; }
};
