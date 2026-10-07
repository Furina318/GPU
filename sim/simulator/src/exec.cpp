// 执行引擎: 取指 → 译码 → 执行 → PC 推进(含 §2.3.3 的收敛切路径)。
// ALU/FP32/setp 的 lane 语义在 inst.cpp; 这里管控制流、访存、barrier、错误与调度。

#include "machine.hpp"
#include "cp.hpp"
#include "inst.hpp"

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <set>
#include <vector>

namespace simu {

const char* sm_err_name(SmErr e) {
    switch (e) {
    case SmErr::NONE:          return "NO_ERROR";
    case SmErr::ILLEGAL_INSTR: return "ILLEGAL_INSTR";
    case SmErr::SIMT_OVF:      return "SIMT_OVF";
    case SmErr::BAD_ALIGN:     return "BAD_ALIGN";
    case SmErr::BAR_DIVERGENT: return "BAR_DIVERGENT";
    case SmErr::ERR_NO_RECOV:  return "ERR_NO_RECOV";
    case SmErr::MMIO_ACCESS:   return "MMIO_ACCESS";
    case SmErr::NO_MEM_REGION: return "NO_MEM_REGION";
    case SmErr::INTERNAL:      return "INTERNAL";
    }
    return "?";
}

static const uint32_t LAUNCH_F_USES_BARRIER       = 1u << 0;
static const uint32_t LAUNCH_F_USES_SHARED        = 1u << 1;
static const uint32_t LAUNCH_F_REQUIRES_SINGLE_SM = 1u << 3;

// CMDS.md §8.1
static const uint32_t CPERR_CMD_ERROR      = 1;
static const uint32_t CPERR_LAUNCH_REJECT  = 2;
static const uint32_t CPERR_ILLEGAL_STATE  = 5;
static const uint32_t CPERR_STALE_SM_ERROR = 6;

static int sms_used(uint32_t nw) { return int((nw + WARPS_PER_SM - 1) / WARPS_PER_SM); }

static bool is_div_like(const DecodedInst& d) {
    // fit4: 11=div 12=divu 13=rem 14=remu(funct4=15 已被译码校验拒绝)
    return (d.raw & 0x7F) == OP_R && d.funct3 == 0b000 && d.funct4 >= 11 && d.funct4 <= 14;
}

Machine::Machine() {
    for (int i = 0; i < MAX_WARPS; ++i) {
        warps[i].reset(i);
        warps[i].alive = false;
    }
}

// ─────────────────────────── LAUNCH (CMDS.md §4) ───────────────────────────

bool Machine::launch_precheck(uint32_t kernel_pc, uint32_t nw, uint32_t flags,
                              std::string* err, uint32_t* cp_err) const {
    auto fail = [&](uint32_t code, const std::string& why) {
        if (err) *err = why;
        if (cp_err) *cp_err = code;
        return false;
    };
    // 涉及哪些 SM 取决于 num_warps;num_warps 越界时按"最多涉及"来查, 这样第 2 条(§4.1)
    // 仍然排在第 3 条之前 —— 文档明确要求"按顺序检查"。
    const int nsm = sms_used(nw < 1 ? 1u : (nw > uint32_t(MAX_WARPS) ? uint32_t(MAX_WARPS) : nw));

    if (active())                                             // 非文档条目: 状态机(§8.1 ILLEGAL_STATE)
        return fail(CPERR_ILLEGAL_STATE, "上一个 kernel 还在运行");
    for (int s = 0; s < nsm; ++s)                             // §4.1 第 2 条
        if (sm[s].error != SmErr::NONE)
            return fail(CPERR_STALE_SM_ERROR, "SM" + std::to_string(s) + " 仍有未清除的错误码 " +
                                              sm_err_name(sm[s].error) + "(host 需写 SM*_ERROR 或 CP_RESET 清除)");
    if (nw < 1 || nw > MAX_WARPS)                             // §4.1 第 3 条
        return fail(CPERR_CMD_ERROR, "num_warps 必须在 1..8(ISA §2.2)");
    if (flags & ~0xFu)                                        // §4.1 第 1 条的保留位
        return fail(CPERR_CMD_ERROR, "launch_flags 的 bit4..31 必须为 0");
    if (kernel_pc & 3u)                                       // §4.1 第 4 条(对齐部分)
        return fail(CPERR_CMD_ERROR, "kernel_pc 未 4B 对齐");
    for (int s = 0; s < nsm; ++s)                             // §4.1 第 5 条
        if (!mem.img[s].loaded)
            return fail(CPERR_CMD_ERROR, "SM" + std::to_string(s) + " 上没有有效镜像(SM_KERNEL_MASK 对应位为 0)");
    for (int s = 0; s < nsm; ++s) {                           // §4.1 第 4 条(范围部分)
        if (kernel_pc >= mem.img[s].image_size) {             // 文档: kernel_pc ∈ [0, image_size)
            char b[160];
            std::snprintf(b, sizeof(b), "kernel_pc=0x%03x 超出镜像 [0, image_size=0x%x)(§4.1 第 4 条)",
                          kernel_pc, mem.img[s].image_size);
            return fail(CPERR_CMD_ERROR, b);
        }
        if (mem.img[s].crc != mem.img[0].crc || mem.img[s].code_size != mem.img[0].code_size) {
            // CMDS §4.2: 一个 LAUNCH 只跑一个 kernel, 两个 SM 不得跑不同 kernel
            char b[192];
            std::snprintf(b, sizeof(b),
                          "SM%d 上的镜像与 SM0 不是同一个(code_crc32 0x%08x vs 0x%08x): "
                          "一个 LAUNCH 只能跑一个 kernel(CMDS §4.2)", s, mem.img[s].crc, mem.img[0].crc);
            return fail(CPERR_CMD_ERROR, b);
        }
    }
    for (int s = 0; s < nsm; ++s) {                           // §4.1 第 6 条
        if (mem.img[s].flags != flags) {
            char b[160];
            std::snprintf(b, sizeof(b), "launch_flags=0x%x 与镜像 image_flags=0x%x 不一致(§4.1 第 6 条)",
                          flags, mem.img[s].flags);
            return fail(CPERR_CMD_ERROR, b);
        }
    }
    if ((flags & (LAUNCH_F_USES_BARRIER | LAUNCH_F_USES_SHARED | LAUNCH_F_REQUIRES_SINGLE_SM)) &&
        nw > uint32_t(WARPS_PER_SM))                          // §4.1 第 7 条
        return fail(CPERR_LAUNCH_REJECT, "flags 要求单 SM 常驻(barrier/shared/single-SM), 但 num_warps=" +
                                         std::to_string(nw) + " > 4");
    if (err) err->clear();
    return true;
}

void Machine::launch_commit(uint32_t kernel_pc, uint32_t nw, uint32_t flags) {
    num_warps_    = nw;
    kernel_pc_    = kernel_pc;
    launch_flags_ = flags;
    launched_     = true;
    stats.clear();
    std::fill(busy_, busy_ + MAX_WARPS, 0u);
    for (int s = 0; s < NUM_SM; ++s) {
        sm[s].resident = 0;
        sm[s].arrived = 0;
        sm[s].generation = 0;
        for (int i = 0; i < WARPS_PER_SM; ++i) sm[s].alive[i] = false;
        next_slot_[s] = 0;
    }
    for (int i = 0; i < MAX_WARPS; ++i) {
        warps[i].reset(i);
        warps[i].alive = false;
    }
    for (uint32_t i = 0; i < nw; ++i) {          // §4.2: 先填满 SM0 的 4 个槽, 再填 SM1
        Warp& w = warps[i];
        w.reset(int(i));
        w.pc = kernel_pc;
        sm[warp_sm(int(i))].alive[warp_slot(int(i))] = true;
        sm[warp_sm(int(i))].resident++;
    }
    set_stop(StopReason::RUNNING);
}

bool Machine::launch(uint32_t kernel_pc, uint32_t nw, uint32_t flags,
                     std::string* err, uint32_t* cp_err) {
    if (!launch_precheck(kernel_pc, nw, flags, err, cp_err)) return false;
    launch_commit(kernel_pc, nw, flags);
    return true;
}

void Machine::reset() {
    for (int i = 0; i < MAX_WARPS; ++i) {
        warps[i].reset(i);
        warps[i].alive = false;
        busy_[i] = 0;
    }
    for (int s = 0; s < NUM_SM; ++s) {
        sm[s] = SmState{};
        next_slot_[s] = 0;
    }
    mem.reset();                 // 共享内存 + 指令 SRAM 归零, 镜像失效(CMDS §7.3 第 4 条)
    // 全局内存(帧缓冲/顶点缓冲/命令队列)不因 CP_RESET 丢失 —— 与真实硬件一致(§7.3 第 6 条)
    stats.clear();
    launched_ = false;
    num_warps_ = kernel_pc_ = launch_flags_ = 0;
    warned_unmapped_ = false;
    set_stop(StopReason::RUNNING);
}

// ─────────────────────────── 状态查询 ───────────────────────────

bool Machine::finished() const {
    if (!launched_) return true;
    for (int s = 0; s < NUM_SM; ++s) {
        if (sm[s].resident == 0) continue;
        if (!sm[s].done() && !sm[s].halted()) return false;
    }
    return true;
}

bool Machine::active() const { return launched_ && !finished(); }

void Machine::set_stop(StopReason::Kind k, std::string d) {
    stop_.kind = k;
    stop_.detail = std::move(d);
}

void Machine::sm_error(int smid, SmErr e, uint32_t pc, int wid) {
    SmState& s = sm[smid];
    if (s.error != SmErr::NONE) {                       // 首个错误保留(CMDS §8.3)
        LogD("SM%d 又发生错误 %s @pc=0x%04x(warp %d), 保留首个错误 %s",
             smid, sm_err_name(e), pc, wid, sm_err_name(s.error));
        return;
    }
    s.error     = e;
    s.fault_pc  = pc;
    s.fault_warp = wid;
    LogE("SM%d 架构错误: %s(= %u) @ pc=0x%04x (warp %d, active_mask=0x%02x); 该 SM 停止取指(ISA §2.6)",
         smid, sm_err_name(e), uint32_t(e), pc, wid, warps[wid].active_mask);
    if (cp_) cp_->on_sm_error(smid);
    set_stop(StopReason::SM_ERROR, "SM" + std::to_string(smid) + " " + sm_err_name(e));
}

std::string Machine::dump_warp(int wid) const {
    if (wid < 0 || wid >= MAX_WARPS) return "warp 编号越界";
    const Warp& w = warps[wid];
    char buf[288];
    std::snprintf(buf, sizeof(buf),
                  "w%-2d SM%d/slot%d %-6s pc=0x%04x mask=0x%02x 栈深=%d recov=0x%04x%s 退役=%llu",
                  wid, warp_sm(wid), warp_slot(wid),
                  w.alive ? (w.stalled ? "bar等待" : "存活") : "已退出",
                  w.pc, w.active_mask, w.stack.top, w.recov_pc,
                  w.recov_valid ? "(有效)" : "",
                  (unsigned long long)stats.retired[wid]);
    return buf;
}

std::string Machine::dump_state() const {
    std::string s;
    char buf[320];
    for (int i = 0; i < int(num_warps_); ++i) s += "  " + dump_warp(i) + "\n";
    for (int t = 0; t < NUM_SM; ++t) {
        if (sm[t].resident == 0) continue;
        std::snprintf(buf, sizeof(buf),
                      "  SM%d: 常驻=%d 存活=%d barrier arrived=%d/%d gen=%u 已发射=%llu 错误=%s\n",
                      t, sm[t].resident, sm[t].alive_count(), sm[t].arrived, sm[t].resident,
                      sm[t].generation, (unsigned long long)stats.sm_issued[t],
                      sm_err_name(sm[t].error));
        s += buf;
        if (sm[t].error != SmErr::NONE) {
            std::snprintf(buf, sizeof(buf), "       FAULT_PC=0x%04x (warp %d)\n",
                          sm[t].fault_pc, sm[t].fault_warp);
            s += buf;
        }
    }
    return s;
}

std::string Machine::dump_regs(int wid) const {
    if (wid < 0 || wid >= MAX_WARPS) return "warp 编号越界\n";
    const Warp& w = warps[wid];
    std::string s;
    char buf[256];
    std::snprintf(buf, sizeof(buf), "warp %d (SM%d slot%d) 每行一个寄存器, 8 列 = 8 个 lane:\n",
                  wid, warp_sm(wid), warp_slot(wid));
    s += buf;
    for (int i = 0; i < NUM_GPR; ++i) {
        std::snprintf(buf, sizeof(buf), "  r%-2d ", i);
        s += buf;
        for (int l = 0; l < WARP_SIZE; ++l) {
            std::snprintf(buf, sizeof(buf), "%08x ", w.rf.gpr[l][i]);
            s += buf;
        }
        s += "\n";
    }
    for (int p = 0; p < NUM_PRED; ++p) {
        std::snprintf(buf, sizeof(buf), "  p%-2d ", p);
        s += buf;
        for (int l = 0; l < WARP_SIZE; ++l) {
            std::snprintf(buf, sizeof(buf), "       %d ", w.rf.pred[l][p] ? 1 : 0);
            s += buf;
        }
        s += "\n";
    }
    return s;
}

// ─────────────────────────── 调度 ───────────────────────────

int Machine::pick_warp(int smid) {
    for (int k = 0; k < WARPS_PER_SM; ++k) {
        const int slot = (next_slot_[smid] + k) % WARPS_PER_SM;
        if (!sm[smid].alive[slot]) continue;
        Warp& w = warps[smid * WARPS_PER_SM + slot];
        if (w.stalled || busy_[w.wid] != 0) continue;
        next_slot_[smid] = (slot + 1) % WARPS_PER_SM;
        return slot;
    }
    return -1;
}

void Machine::count_inst(const DecodedInst& d) {
    stats.icount[d.name ? d.name : "?"]++;
    if (d.is_fp || (d.raw & 0x7F) == OP_R4) ++stats.fp_ops;
}

bool Machine::step_cycle() {
    bool any = false;
    for (int s = 0; s < NUM_SM; ++s) {
        if (sm[s].halted() || sm[s].resident == 0 || sm[s].done()) continue;
        bool issued_here = false;
        for (uint32_t k = 0; k < model.issue_per_sm; ++k) {
            const int slot = pick_warp(s);
            if (slot < 0) break;
            if (step_warp(s * WARPS_PER_SM + slot)) { any = true; issued_here = true; }
        }
        if (!issued_here) {
            ++stats.idle_sm_cycles;
            for (int i = 0; i < WARPS_PER_SM; ++i)
                if (sm[s].alive[i] && warps[s * WARPS_PER_SM + i].stalled) ++stats.issue_stall_barrier;
        }
    }
    for (int i = 0; i < MAX_WARPS; ++i) if (busy_[i]) --busy_[i];
    ++stats.cycles;
    return any;
}

StopReason Machine::run(uint64_t max_cycles) {
    if (!launched_) {
        set_stop(StopReason::CP_ERROR, "还没有 LAUNCH 过的内核");
        return stop_;
    }
    if (stop_.kind != StopReason::RUNNING && stop_.kind != StopReason::STEP &&
        stop_.kind != StopReason::BREAKPOINT)
        return stop_;                     // 上次已停机(错误/死锁/超时), 不重复跑
    set_stop(StopReason::RUNNING);

    while (true) {
        for (int s = 0; s < NUM_SM; ++s) {
            if (sm[s].error == SmErr::NONE) continue;
            char b[128];
            std::snprintf(b, sizeof(b), "SM%d %s @pc=0x%04x", s, sm_err_name(sm[s].error), sm[s].fault_pc);
            set_stop(StopReason::SM_ERROR, b);
            return stop_;                                                  // ISA §2.5: 错误不算完成
        }
        if (finished()) { set_stop(StopReason::ALL_EXIT); return stop_; }

        std::string why;
        if (barrier_deadlock(&why)) { set_stop(StopReason::DEADLOCK, why); return stop_; }

        if (max_cycles && stats.cycles >= max_cycles) {
            set_stop(StopReason::TIMEOUT, "运行 " + std::to_string(stats.cycles) + " 个周期仍未结束");
            return stop_;
        }
        step_cycle();
    }
}

bool Machine::step_warp(int wid) {
    if (wid < 0 || wid >= MAX_WARPS) return false;
    Warp& w = warps[wid];
    const int smid = warp_sm(wid);

    if (!launched_)        { LogW("还没有内核在运行, 无法单步"); return false; }
    if (sm[smid].halted()) { LogW("warp %d 所在 SM%d 已因 %s 停机", wid, smid, sm_err_name(sm[smid].error)); return false; }
    if (!w.alive)          { LogW("warp %d 已 exit, 无法单步", wid); return false; }
    if (w.stalled)         { LogW("warp %d 正在 bar.sync 等待(generation=%u), 无法单步", wid, sm[smid].generation); return false; }

    uint32_t inst = 0;
    MemErr fe = MemErr::NONE;
    if (!mem.fetch(smid, w.pc, inst, &fe)) {
        LogE("warp %d 取指失败 @pc=0x%04x: %s", wid, w.pc,
             fe == MemErr::ALIGN ? "PC 未 4B 对齐" : "PC 超出指令 SRAM(16KB)");
        sm_error(smid, SmErr::ILLEGAL_INSTR, w.pc, wid);
        return false;
    }

    DecodedInst d;
    logger().set_context(stats.cycles, wid, w.pc);
    if (!decode(inst, d)) {
        LogE("warp %d 非法指令 0x%08x @pc=0x%04x: %s", wid, inst, w.pc, d.error ? d.error : "未定义");
        logger().clear_context();
        sm_error(smid, SmErr::ILLEGAL_INSTR, w.pc, wid);
        return false;
    }

    LogT("pc=0x%04x  %s", w.pc, itrace(d, w.pc).c_str());
    count_inst(d);
    ++stats.retired[w.wid];
    ++stats.issued;
    ++stats.sm_issued[smid];

    uint32_t next_pc = w.pc + 4, extra = 0;
    const bool ok = execute_one(w, d, next_pc, extra);
    if (ok && w.alive) w.pc = advance_pc(w, next_pc);
    busy_[wid] = is_div_like(d) && model.div_cost > 1 ? model.div_cost - 1 + extra : extra;
    stats.stack_max = std::max<uint64_t>(stats.stack_max, uint64_t(w.stack.top));
    logger().clear_context();
    return ok;
}

// ─────────────────────────── §2.3.3 收敛切路径 ───────────────────────────

uint32_t Machine::advance_pc(Warp& w, uint32_t next_pc) {
    int guard = 0;
    while (!w.stack.empty() && w.stack.peek().recov_pc == next_pc) {
        StackFrame& f = w.stack.peek();
        if (f.pending) {                       // 第二条路径到达收敛点: 弹栈, 完整 mask 执行一次
            const uint8_t full = f.mask_full;
            w.stack.pop();
            w.active_mask = full;
            LogD("warp %d 收敛(pending=1): 弹栈, mask=0x%02x, pc 保持 0x%04x", w.wid, full, next_pc);
        } else if (f.fall_pc == f.recov_pc) {  // 空路径特例(省一拍, 不是正确性要求)
            const uint8_t full = f.mask_full;
            w.stack.pop();
            w.active_mask = full;
            LogD("warp %d 收敛(空路径): 弹栈, mask=0x%02x, pc 保持 0x%04x", w.wid, full, next_pc);
        } else {                               // taken 路径到达: 切到未跳转路径(此刻不执行收敛点)
            f.pending = true;
            w.active_mask = f.mask_pending;
            next_pc = f.fall_pc;
            LogD("warp %d 切到 fall 路径: mask=0x%02x, pc=0x%04x (pending=1)", w.wid, w.active_mask, next_pc);
        }
        ++stats.reconvergences;
        if (++guard > SIMT_STACK_DEPTH + 2) {
            sm_error(warp_sm(w.wid), SmErr::INTERNAL, next_pc, w.wid);
            break;
        }
    }
    return next_pc;
}

// ─────────────────────────── barrier (§2.4) ───────────────────────────

void Machine::barrier_arrive(int smid, int wid) {
    SmState& s = sm[smid];
    ++s.arrived;
    ++stats.barriers;
    LogD("warp %d 到达 bar.sync: SM%d arrived=%d/%d gen=%u", wid, smid, s.arrived, s.resident, s.generation);
    if (s.arrived >= s.resident) {
        s.arrived = 0;
        ++s.generation;
        for (int i = 0; i < WARPS_PER_SM; ++i) {
            Warp& w = warps[smid * WARPS_PER_SM + i];
            if (w.alive && w.stalled) {
                w.stalled = false;
                LogD("warp %d 从 bar.sync 释放(gen=%u)", w.wid, s.generation);
            }
        }
    } else {
        warps[wid].stalled = true;
    }
}

bool Machine::barrier_deadlock(std::string* why) const {
    for (int s = 0; s < NUM_SM; ++s) {
        if (sm[s].halted() || sm[s].resident == 0 || sm[s].done()) continue;
        int alive = 0, stalled = 0;
        for (int i = 0; i < WARPS_PER_SM; ++i) {
            if (!sm[s].alive[i]) continue;
            ++alive;
            if (warps[s * WARPS_PER_SM + i].stalled) ++stalled;
        }
        if (alive > 0 && stalled == alive) {
            if (why)
                *why = "SM" + std::to_string(s) + ": " + std::to_string(alive) +
                       " 个存活 warp 全部卡在 bar.sync, 但 barrier 要求常驻的 " +
                       std::to_string(sm[s].resident) + " 个 warp 全部到达(现到 " +
                       std::to_string(sm[s].arrived) + ") —— 已 exit 的 warp 不会再到达";
            return true;
        }
    }
    return false;
}

// ─────────────────────────── 访存 ───────────────────────────

const char* Machine::region_name(uint32_t addr) const {
    if (addr < MMIO_END) return "MMIO";
    uint32_t qb = CMD_QUEUE_BASE_DEFAULT, qs = CMD_QUEUE_SIZE_DEFAULT;
    if (cp_) { qb = cp_->queue_base(); qs = cp_->queue_size(); }
    if (addr >= qb && addr - qb < qs) return "命令队列";
    if (addr >= CONST_BASE && addr - CONST_BASE < CONST_SIZE) return "常量区";
    if (addr >= KERNEL_IMG_BASE && addr - KERNEL_IMG_BASE < 0x7000) return "内核镜像区";
    if (addr >= fb_base && addr - fb_base < fb_size) return "帧缓冲";
    if (addr >= DDR_BASE) return "DDR";
    return nullptr;
}

bool Machine::load_word(int smid, uint32_t addr, uint32_t& out, SmErr* err) {
    (void)smid;                     // 全局内存对所有 SM 相同; 参数留着便于将来按 SM 归属错误
    if (err) *err = SmErr::NONE;
    if (addr & 3u) { if (err) *err = SmErr::BAD_ALIGN; return false; }
    if (addr < MMIO_END) {
        LogE("内核读了 MMIO 窗口(地址 0x%08x): MMIO 属于 CP/host(CMDS §7), shader 不该碰它 —— "
             "这几乎总是「基址/参数没初始化」算出来的地址 ⇒ MMIO_ACCESS", addr);
        if (err) *err = SmErr::MMIO_ACCESS;
        stats.mmio_from_kernel++;
        return false;
    }
    if (!region_name(addr)) {
        ++stats.unmapped_access;
        if (!warned_unmapped_) {
            warned_unmapped_ = true;
            LogW("访问了已知内存区之外的地址 0x%08x(区域表见 CMDS §7.2); 驱动自行分配的缓冲属正常, "
                 "只有越界访问才会表现为错数据", addr);
        }
        if (strict_mem) { if (err) *err = SmErr::NO_MEM_REGION; return false; }
    }
    out = mem.ld_global(addr);
    return true;
}

bool Machine::store_word(int smid, uint32_t addr, uint32_t v, SmErr* err) {
    (void)smid;
    if (err) *err = SmErr::NONE;
    if (addr & 3u) { if (err) *err = SmErr::BAD_ALIGN; return false; }
    if (addr < MMIO_END) {
        LogE("内核写了 MMIO 窗口(地址 0x%08x = 0x%08x): MMIO 属于 CP/host(CMDS §7)。放行会让内核"
             "误写 CP_RESET/DOORBELL 之类的寄存器(比报错难查一个数量级) ⇒ MMIO_ACCESS", addr, v);
        if (err) *err = SmErr::MMIO_ACCESS;
        stats.mmio_from_kernel++;
        return false;
    }
    if (!region_name(addr)) {
        ++stats.unmapped_access;
        if (!warned_unmapped_) {
            warned_unmapped_ = true;
            LogW("写入了已知内存区之外的地址 0x%08x(区域表见 CMDS §7.2)", addr);
        }
        if (strict_mem) { if (err) *err = SmErr::NO_MEM_REGION; return false; }
    }
    mem.st_global(addr, v);
    return true;
}

// ─────────────────────────── MMIO (CMDS.md §7) ───────────────────────────

uint32_t Machine::mmio_read(uint32_t off) {
    switch (off) {
    case MMIO_SM_DONE: {
        uint32_t v = 0;
        for (int s = 0; s < NUM_SM; ++s) if (sm[s].done()) v |= 1u << s;
        return v;
    }
    case MMIO_SM0_ERROR:
    case MMIO_SM1_ERROR:      return uint32_t(sm[(off - MMIO_SM0_ERROR) / 4].error);
    case MMIO_SM_KERNEL_MASK: return mem.sm_kernel_mask();
    case MMIO_SM0_FAULT_PC:
    case MMIO_SM1_FAULT_PC:   return sm[(off - MMIO_SM0_FAULT_PC) / 4].fault_pc;
    case MMIO_CONST_BASE:     return CONST_BASE;
    case MMIO_DDR_BASE:       return DDR_BASE;
    case MMIO_FB_BASE:        return fb_base;
    default: break;
    }
    if (cp_) return cp_->read_reg(off);
    LogW("MMIO 读 0x%02x: 没有挂 CP, 返回 0", off);
    return 0;
}

void Machine::mmio_write(uint32_t off, uint32_t v) {
    switch (off) {
    case MMIO_SM0_ERROR:
    case MMIO_SM1_ERROR: {
        const int s = int((off - MMIO_SM0_ERROR) / 4);
        if (sm[s].error != SmErr::NONE)
            LogI("host 清除 SM%d_ERROR(原值 %s)", s, sm_err_name(sm[s].error));
        sm[s].error  = SmErr::NONE;
        sm[s].fault_pc = 0;
        sm[s].fault_warp = -1;
        return;
    }
    case MMIO_FB_BASE:
        LogI("FB_BASE ← 0x%08x", v);
        fb_base = v;
        return;
    case MMIO_DOORBELL:
        if (cp_) cp_->doorbell();
        else LogW("DOORBELL 被写但没挂 CP");
        return;
    case MMIO_CP_RESET:
        if (cp_) cp_->reset();
        else reset();
        return;
    case MMIO_CONST_BASE:
    case MMIO_DDR_BASE:
    case MMIO_SM_KERNEL_MASK:
    case MMIO_SM_DONE:
    case MMIO_SM0_FAULT_PC:
    case MMIO_SM1_FAULT_PC:
        LogW("MMIO 0x%02x 是只读寄存器, 写 0x%x 被忽略(CMDS §7)", off, v);
        return;
    default: break;
    }
    if (cp_) { cp_->write_reg(off, v); return; }
    LogW("MMIO 写 0x%02x = 0x%x: 没有挂 CP, 忽略", off, v);
}

// ─────────────────────────── 指令执行 ───────────────────────────

bool Machine::execute_one(Warp& w, const DecodedInst& d, uint32_t& next_pc, uint32_t& extra) {
    const int smid = warp_sm(w.wid);
    const uint32_t pc = w.pc;
    const uint32_t op = d.raw & 0x7F;
    next_pc = pc + 4;
    extra = 0;

    // ── exit(§2.5, warp 级、无谓词) ──
    if (d.is_exit) {
        if (!w.stack.empty())
            LogW("warp %d exit 时 SIMT 栈还剩 %d 帧: 有未收敛的发散路径(§2.3.4 第 2 条)", w.wid, w.stack.top);
        if (w.active_mask != 0xFF)
            LogW("warp %d exit 时 active_mask=0x%02x != 0xFF: exit 是 warp 级指令, 却在发散路径里执行(汇编器应禁止)",
                 w.wid, w.active_mask);
        sm[smid].alive[warp_slot(w.wid)] = false;
        w.alive = false;
        w.stalled = false;
        LogI("warp %d exit @pc=0x%04x: 共退役 %llu 条指令; SM%d 剩余存活 %d warp%s",
             w.wid, pc, (unsigned long long)stats.retired[w.wid], smid, sm[smid].alive_count(),
             sm[smid].done() ? " ⇒ SM_DONE=1" : "");
        return true;
    }

    // ── bar.sync(§2.4) ──
    if (d.is_barrier) {
        if (w.active_mask != 0xFF || !w.stack.empty()) {
            LogE("warp %d 在发散状态下执行 bar.sync(active_mask=0x%02x, 栈深 %d) ⇒ BAR_DIVERGENT"
                 "(死锁比报错难查一个数量级, 所以这里硬报错)", w.wid, w.active_mask, w.stack.top);
            sm_error(smid, SmErr::BAR_DIVERGENT, pc, w.wid);
            return false;
        }
        barrier_arrive(smid, w.wid);
        return true;
    }

    // ── ipdom(§5.7): 只对紧随其后的第一条 br.simt 生效 ──
    if (d.is_ipdom) {
        w.recov_pc = d.target(pc);
        w.recov_valid = true;
        LogT("ipdom: recov_pc ← 0x%04x", w.recov_pc);
        return true;
    }

    // ── br.simt(§2.3.1) ──
    if (d.is_branch) {
        const uint8_t taken     = effective_mask(w, d);              // 有效 mask 内谓词为真的 lane
        const uint8_t not_taken = uint8_t(w.active_mask & ~taken);
        const uint32_t target   = d.target(pc);
        const bool have_recov   = w.recov_valid;
        w.recov_valid = false;                                       // 用后即清

        if (taken == 0) {                                            // 均匀不跳
            LogT("br.simt 均匀不跳 → 0x%04x", next_pc);
            return true;
        }
        if (not_taken == 0) {                                        // 均匀跳转
            next_pc = target;
            LogT("br.simt 均匀跳转 → 0x%04x", target);
            return true;
        }
        if (!have_recov) {
            LogE("warp %d br.simt @pc=0x%04x 发散(taken=0x%02x/未跳=0x%02x)但 recov 无效位为 0: "
                 "汇编器漏生成了 ipdom ⇒ ERR_NO_RECOV", w.wid, pc, taken, not_taken);
            sm_error(smid, SmErr::ERR_NO_RECOV, pc, w.wid);
            return false;
        }
        if (w.stack.full()) {
            LogE("warp %d br.simt @pc=0x%04x 需要压栈但栈已满(%d 层) ⇒ SIMT_OVF",
                 w.wid, pc, SIMT_STACK_DEPTH);
            sm_error(smid, SmErr::SIMT_OVF, pc, w.wid);
            return false;
        }
        StackFrame f;
        f.recov_pc     = w.recov_pc;
        f.fall_pc      = pc + 4;
        f.mask_full    = w.active_mask;
        f.mask_pending = not_taken;
        f.pending      = false;
        w.stack.push(f);
        w.active_mask = taken;
        next_pc = target;
        ++stats.divergences;
        LogD("br.simt 发散: taken=0x%02x pending=0x%02x recov=0x%04x fall=0x%04x 栈深=%d",
             taken, not_taken, f.recov_pc, f.fall_pc, w.stack.top);
        return true;
    }

    // ── tmov / wmov(§5.7): 每 lane 写自己的 tid/wid ──
    if (op == OP_CTRL && (d.funct3 == 0b011 || d.funct3 == 0b100)) {
        const uint8_t eff = effective_mask(w, d);
        for (int l = 0; l < WARP_SIZE; ++l) {
            if (!(eff >> l & 1)) continue;
            if (!d.rd) continue;
            R(w.rf, l, d.rd) = (d.funct3 == 0b011) ? uint32_t(l) : uint32_t(w.wid);
        }
        return true;
    }

    // ── 访存 ──
    if (d.is_load || d.is_store) {
        const uint8_t eff = effective_mask(w, d);
        if (!eff) { LogT("访存: 有效掩码为 0, 不发请求"); return true; }

        uint32_t addr[WARP_SIZE] = {};
        for (int l = 0; l < WARP_SIZE; ++l) {
            if (!(eff >> l & 1)) continue;
            addr[l] = R(w.rf, l, d.rs1) + uint32_t(d.imm);
            if (addr[l] & 3u) {
                LogE("warp %d %s @pc=0x%04x lane %d: 地址 0x%08x 未 4B 对齐 ⇒ BAD_ALIGN(ISA §3.2)",
                     w.wid, d.name, pc, l, addr[l]);
                sm_error(smid, SmErr::BAD_ALIGN, pc, w.wid);
                return false;
            }
        }

        // 合并度 / bank 冲突统计
        if (d.is_shared) {
            std::set<uint32_t> per_bank[8];
            for (int l = 0; l < WARP_SIZE; ++l)
                if (eff >> l & 1) per_bank[(addr[l] >> 2) & 7].insert(addr[l]);
            int conflicts = 0;
            for (int b = 0; b < 8; ++b) conflicts += int(per_bank[b].size()) - 1;
            if (conflicts > 0 && model.shared_conflicts) {
                extra = uint32_t(conflicts);
                stats.shared_extra_cycles += uint64_t(conflicts);
            }
        } else {
            std::set<uint32_t> lines;
            for (int l = 0; l < WARP_SIZE; ++l)
                if (eff >> l & 1) lines.insert(addr[l] >> 5);
            stats.gmem_lines += lines.size();
        }

        if (d.is_load) {
            uint32_t val[WARP_SIZE] = {};
            for (int l = 0; l < WARP_SIZE; ++l) {
                if (!(eff >> l & 1)) continue;
                SmErr e = SmErr::NONE;
                if (d.is_shared) {
                    MemErr me = MemErr::NONE;
                    val[l] = mem.ld_shared(smid, addr[l], &me);
                    if (me != MemErr::NONE) {
                        LogE("warp %d ld.s @pc=0x%04x lane %d: 共享地址 0x%05x %s", w.wid, pc, l, addr[l],
                             me == MemErr::ALIGN ? "未 4B 对齐" : "超出 16KB 共享内存");
                        sm_error(smid, me == MemErr::ALIGN ? SmErr::BAD_ALIGN : SmErr::NO_MEM_REGION, pc, w.wid);
                        return false;
                    }
                } else if (!load_word(smid, addr[l], val[l], &e)) {
                    LogE("warp %d ld.g @pc=0x%04x lane %d: 地址 0x%08x 访问失败(%s)",
                         w.wid, pc, l, addr[l], sm_err_name(e));
                    sm_error(smid, e, pc, w.wid);
                    return false;
                }
            }
            if (d.is_shared) ++stats.ld_shared; else ++stats.ld_global;
            if (d.rd)
                for (int l = 0; l < WARP_SIZE; ++l)
                    if (eff >> l & 1) R(w.rf, l, d.rd) = val[l];
            LogT("%s: 有效 lane %d, 首地址 0x%08x", d.name, __builtin_popcount(eff), addr[__builtin_ctz(eff)]);
        } else {
            for (int l = 0; l < WARP_SIZE; ++l) {
                if (!(eff >> l & 1)) continue;
                const uint32_t v = R(w.rf, l, d.rs2);
                if (d.is_shared) {
                    MemErr me = MemErr::NONE;
                    mem.st_shared(smid, addr[l], v, &me);
                    if (me != MemErr::NONE) {
                        LogE("warp %d st.s @pc=0x%04x lane %d: 共享地址 0x%05x %s", w.wid, pc, l, addr[l],
                             me == MemErr::ALIGN ? "未 4B 对齐" : "超出 16KB 共享内存");
                        sm_error(smid, me == MemErr::ALIGN ? SmErr::BAD_ALIGN : SmErr::NO_MEM_REGION, pc, w.wid);
                        return false;
                    }
                } else {
                    SmErr e = SmErr::NONE;
                    if (!store_word(smid, addr[l], v, &e)) {
                        LogE("warp %d st.g @pc=0x%04x lane %d: 地址 0x%08x 写入失败(%s)",
                             w.wid, pc, l, addr[l], sm_err_name(e));
                        sm_error(smid, e, pc, w.wid);
                        return false;
                    }
                }
            }
            if (d.is_shared) ++stats.st_shared; else ++stats.st_global;
            LogT("%s: 有效 lane %d, 首地址 0x%08x", d.name, __builtin_popcount(eff), addr[__builtin_ctz(eff)]);
        }
        return true;
    }

    // ── ALU / FP32 / setp(inst.cpp) ──
    if (exec_alu(w, d) || exec_fp32(w, d) || exec_setp(w, d)) return true;

    LogE("warp %d @pc=0x%04x: 指令 %s(0x%08x) 译码通过但没有任何执行单元认领 —— 模拟器 bug",
         w.wid, pc, d.name ? d.name : "?", d.raw);
    sm_error(smid, SmErr::INTERNAL, pc, w.wid);
    return false;
}

}  // namespace simu
