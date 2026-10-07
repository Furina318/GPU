#pragma once

// Machine = 2 个 SM 的 shader 核 + 它们看到的 MMIO 窗口。
// CP(命令流)在 cp.hpp 里, 通过 CommandProcessor* 反向挂进来: MMIO 寄存器的归属
// 按 CMDS.md §7 切分 —— CP 域寄存器由 CP 自己读写, SM 域寄存器(错误码/FAULT_PC/DONE)在这里。

#include "config.hpp"
#include "decode.hpp"
#include "memory.hpp"
#include "warp.hpp"

#include <cstdint>
#include <map>
#include <string>

namespace simu {

class CommandProcessor;

// CMDS.md §8.2 的 SM 错误码(0..15 为文档取值;>=16 是模拟器补充, 不占文档的保留值)
enum class SmErr : uint32_t {
    // 0..5 来自 CMDS §8.2;6..15 是文档的保留区, 这里占用 6/7/8 并在 CMDS §8.2 表里登记(否则
    // 就得用一个文档没定义的码, 或者把"内核越界访问"错报成别的错误)
    NONE = 0, ILLEGAL_INSTR = 1, SIMT_OVF = 2, BAD_ALIGN = 3,
    BAR_DIVERGENT = 4, ERR_NO_RECOV = 5,
    MMIO_ACCESS = 6,        // 内核访问 MMIO 窗口: MMIO 属于 CP/host(CMDS §7), 内核碰它必是算错的地址
    NO_MEM_REGION = 7,      // 访问了任何已知内存区之外的地址(CMDS §7.2 的映射总表)
    INTERNAL = 8,           // 模拟器自身的一致性检查失败(出现即为模拟器 bug)
};
const char* sm_err_name(SmErr e);

// MMIO 偏移(CMDS.md §7)
enum : uint32_t {
    MMIO_CP_STATUS = 0x00, MMIO_CP_ERROR = 0x04, MMIO_CMD_HEAD = 0x08, MMIO_CMD_TAIL = 0x0C,
    MMIO_DOORBELL = 0x10,  MMIO_IRQ_STATUS = 0x14, MMIO_IRQ_ENABLE = 0x18,
    MMIO_SM_DONE = 0x1C,   MMIO_SM0_ERROR = 0x20, MMIO_SM1_ERROR = 0x24,
    MMIO_SEM_BASE = 0x28,  MMIO_CMD_QUEUE_BASE = 0x2C, MMIO_CMD_QUEUE_SIZE = 0x30,
    MMIO_CONST_BASE = 0x34, MMIO_FB_BASE = 0x38, MMIO_DDR_BASE = 0x3C,
    MMIO_CP_RESET = 0x40,  MMIO_TIMEOUT_CYCLES = 0x44, MMIO_SM_KERNEL_MASK = 0x48,
    MMIO_SM0_FAULT_PC = 0x4C, MMIO_SM1_FAULT_PC = 0x50,
    MMIO_GEMM_STATUS = 0x60,  MMIO_GEMM_ERROR = 0x64,
};

struct SmState {
    bool     alive[WARPS_PER_SM] = {false, false, false, false};
    int      resident   = 0;        // 本 kernel 在该 SM 常驻的 warp 数(barrier 判据)
    int      arrived    = 0;        // barrier 已到达计数
    uint32_t generation = 0;
    SmErr    error      = SmErr::NONE;
    uint32_t fault_pc   = 0;
    int      fault_warp = -1;

    bool halted() const { return error != SmErr::NONE; }
    bool done() const {
        for (int i = 0; i < WARPS_PER_SM; ++i) if (alive[i]) return false;
        return true;
    }
    int alive_count() const {
        int n = 0;
        for (int i = 0; i < WARPS_PER_SM; ++i) if (alive[i]) ++n;
        return n;
    }
};

struct RunStats {
    uint64_t cycles = 0;
    uint64_t issued = 0;
    uint64_t retired[MAX_WARPS] = {};
    uint64_t sm_issued[NUM_SM] = {};
    uint64_t issue_stall_barrier = 0;   // 因 barrier 挂起而空转的 warp 周期数
    uint64_t idle_sm_cycles = 0;        // SM 有 warp 但一条都没发出的周期数
    uint64_t barriers = 0;
    uint64_t divergences = 0, reconvergences = 0;
    uint64_t stack_max = 0;
    uint64_t ld_global = 0, st_global = 0, ld_shared = 0, st_shared = 0;
    uint64_t gmem_lines = 0;            // ld.g/st.g 发出的 32B 行请求数(合并度指标)
    uint64_t shared_extra_cycles = 0;   // 共享 bank 冲突带来的额外串行拍
    uint64_t unmapped_access = 0;
    uint64_t mmio_from_kernel = 0;   // 内核碰 MMIO 窗口的次数(架构错误)
    uint64_t fp_ops = 0;
    std::map<std::string, uint64_t> icount;

    void clear() { *this = RunStats{}; }
};

// 周期模型: ISA §8.1 只规定了"1 条指令/周期/SM"(4 warp FGMT)。其余数值文档未定,
// 在这里假设并在启动时打印 —— 这样"实测周期数"的前提是显式的, 不会变成没人信的文档。
struct CycleModel {
    uint32_t issue_per_sm     = 1;    // 每 SM 每周期发射的指令数(§8.1)
    uint32_t div_cost         = 8;    // div/divu/rem/remu 占用周期(文档只说"多周期")
    bool     shared_conflicts = true; // ld.s/st.s 同 bank 异地址按 +1 拍/额外地址串行化
};

struct StopReason {
    enum Kind { RUNNING, ALL_EXIT, SM_ERROR, TIMEOUT, DEADLOCK, CP_ERROR, STEP, BREAKPOINT };
    Kind        kind = RUNNING;
    std::string detail;

    bool ok() const { return kind == ALL_EXIT || kind == STEP || kind == BREAKPOINT || kind == RUNNING; }
    const char* name() const {
        switch (kind) {
        case RUNNING:    return "运行中";
        case ALL_EXIT:   return "全部 warp 已 exit";
        case SM_ERROR:   return "SM 架构错误";
        case TIMEOUT:    return "超出周期上限";
        case DEADLOCK:   return "barrier 死锁";
        case CP_ERROR:   return "CP 错误";
        case STEP:       return "单步完成";
        case BREAKPOINT: return "命中断点";
        }
        return "?";
    }
};

class Machine {
public:
    Memory     mem;
    Warp       warps[MAX_WARPS];
    SmState    sm[NUM_SM];
    RunStats   stats;
    CycleModel model;

    Machine();

    void attach_cp(CommandProcessor* cp) { cp_ = cp; }

    // ── LAUNCH (CMDS.md §4) ──
    // 检查 §4.1 的第 3/4/5/6/7 条, 通过则按 §4.2 放置 warp 并清 warp_alive/SM_DONE。
    // 失败返回 false, 填 *err(人读原因) 与 *cp_err(CMDS §8.1 取值)。
    // §4.1 的前置检查(#2..#7, 按文档顺序)与实际分发分成两步: 因为文档要求 #9(依赖等待)
    // 排在所有前置条件之后, 而检查与等待必须都发生在分发之前。
    bool launch_precheck(uint32_t kernel_pc, uint32_t num_warps, uint32_t launch_flags,
                         std::string* err, uint32_t* cp_err) const;
    void launch_commit(uint32_t kernel_pc, uint32_t num_warps, uint32_t launch_flags);
    bool launch(uint32_t kernel_pc, uint32_t num_warps, uint32_t launch_flags,
                std::string* err, uint32_t* cp_err);
    void reset();                                 // CP_RESET / 复位 SM 与统计

    bool     launched() const { return launched_; }
    uint32_t num_warps() const { return num_warps_; }
    uint32_t kernel_pc() const { return kernel_pc_; }
    uint32_t launch_flags() const { return launch_flags_; }
    int      threads() const { return int(num_warps_) * WARP_SIZE; }
    int      warp_sm(int wid) const { return wid / WARPS_PER_SM; }
    int      warp_slot(int wid) const { return wid % WARPS_PER_SM; }

    bool       active() const;                    // 已分发且还没跑完
    bool       finished() const;                  // 所有涉及 SM 都 DONE 或出错
    StopReason run(uint64_t max_cycles);          // 跑到结束/错误/超时/死锁
    bool       step_cycle();                      // 一个周期: 每 SM 发射 ≤1 条
    bool       step_warp(int wid);                // 单步一个 warp 的一条指令(调试器 si)
    StopReason stop() const { return stop_; }
    void       set_stop(StopReason::Kind k, std::string d = "");
    uint64_t   cycles() const { return stats.cycles; }

    // 每 SM 的 barrier 计数(§2.4)
    void barrier_arrive(int smid, int wid);
    bool barrier_deadlock(std::string* why) const;

    void sm_error(int smid, SmErr e, uint32_t pc, int wid);

    // ── MMIO(CMDS.md §7) ──
    uint32_t mmio_read(uint32_t off);
    void     mmio_write(uint32_t off, uint32_t v);

    // ── 访存(exec.cpp 用): 处理 MMIO 路由 + 已知内存区检查 ──
    bool load_word(int smid, uint32_t addr, uint32_t& out, SmErr* e);
    bool store_word(int smid, uint32_t addr, uint32_t v, SmErr* e);

    bool     strict_mem = false;    // true: 越出已知内存区即报错; false: 只警告一次(驱动可分配缓冲)
    uint32_t fb_base = FB_BASE;
    uint32_t fb_size = 8u * 1024 * 1024;

    std::string dump_warp(int wid) const;
    std::string dump_state() const;
    std::string dump_regs(int wid) const;

private:
    int  pick_warp(int smid);                     // FGMT 轮转(§8.1)
    bool execute_one(Warp& w, const DecodedInst& d, uint32_t& next_pc, uint32_t& extra);
    uint32_t advance_pc(Warp& w, uint32_t next_pc);
    void     count_inst(const DecodedInst& d);
    const char* region_name(uint32_t addr) const; // nullptr = 不在任何已知区

    CommandProcessor* cp_ = nullptr;
    int      next_slot_[NUM_SM] = {0, 0};
    uint32_t busy_[MAX_WARPS] = {};               // 多周期指令剩余占用拍数
    bool     launched_ = false;
    uint32_t num_warps_ = 0, kernel_pc_ = 0, launch_flags_ = 0;
    StopReason stop_;
    bool     warned_unmapped_ = false;
};

}  // namespace simu
