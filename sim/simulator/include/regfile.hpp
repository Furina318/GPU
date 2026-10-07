#pragma once

#include "config.hpp"

struct RegFile {
    uint32_t gpr[simu::WARP_SIZE][simu::NUM_GPR];   // 每 lane 32 个
    uint8_t  pred[simu::WARP_SIZE][simu::NUM_PRED]; // 每 lane 4 位谓词

    void reset() {
        std::memset(gpr, 0, sizeof(gpr));
        for (int l = 0; l < simu::WARP_SIZE; ++l) {
            pred[l][0] = 1;  // p0 恒真
            pred[l][1] = pred[l][2] = pred[l][3] = 0;
        }
    }
};

void isa_rf_display(const RegFile& rf, int lane);
int  check_rf_idx(int idx, bool is_pred);

#define R(rf, lane, idx) ((rf).gpr[(lane)][check_rf_idx((idx), false)])
#define P(rf, lane, idx) ((rf).pred[(lane)][check_rf_idx((idx), true)])