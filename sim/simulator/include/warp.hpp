#pragma once

#include "config.hpp"
#include "regfile.hpp" 

struct Warp {
    int      wid;               // 全局编号 0..7
    int      slot;              // wid mod 4
    uint32_t pc;
    uint8_t  active_mask;
    RegFile  rf;
    SimtStack stack;
    bool     alive;
    bool     stalled;           // 等 barrier / 访存

    uint32_t recov_pc = 0;
    bool     recov_valid = false;

    void reset(int id) {
        wid         = id;
        slot        = id % simu::WARPS_PER_SM;
        pc          = 0;
        active_mask = 0xFF;
        rf.reset();
        stack.top   = 0;
        alive       = true;
        stalled     = false;
        recov_valid = false;
    }
};