#include "regfile.hpp"
#include "debug.hpp"
#include <cstdio>

void regfile_display(const RegFile& rf, int lane) {
    printf("GPR: \n");
    for (int i = 0; i < simu::NUM_GPR; ++i) {
        printf("r%-2d=0x%08x ", i, rf.gpr[lane][i]);
        if (i % 4 == 3) printf("\n     ");
    }
    printf("\nPRED: \n");
    for (int i = 0; i < simu::NUM_PRED; ++i) {
        printf("p%-2d=%d ", i, rf.pred[lane][i]);
    }
    printf("\n");
}

int check_rf_idx(int idx, bool is_pred) {
    if (is_pred) {
        Assert(idx >= 0 && idx < simu::NUM_PRED, "pred index out of range");
    } else {
        Assert(idx >= 0 && idx < simu::NUM_GPR, "gpr index out of range");
    }
    return idx;
}