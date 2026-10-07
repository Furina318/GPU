#include "debugger.hpp"
#include "debug.hpp"
#include "log.hpp"

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

namespace simu {

static std::vector<std::string> split(const std::string& s) {
    std::vector<std::string> out;
    std::istringstream is(s);
    std::string t;
    while (is >> t) out.push_back(t);
    return out;
}

static uint32_t parse_u32(const std::string& s, bool* ok) {
    if (ok) *ok = true;
    try {
        size_t idx = 0;
        const unsigned long v = std::stoul(s, &idx, 0);   // base 0: 自动识别 0x / 0 / 十进制
        if (idx != s.size()) { if (ok) *ok = false; return 0; }
        return uint32_t(v);
    } catch (...) {
        if (ok) *ok = false;
        return 0;
    }
}

// ─────────────────────────── 主循环 ───────────────────────────

void Debugger::run() {
    std::printf("进入调试模式。输入 help 看命令表, si 单步, c 继续, q 退出。\n");
    if (!m_.active() && !cp_.launch_pending())
        std::printf("(当前没有正在运行的 kernel: 先 c 让 CP 分发命令流)\n");
    std::string line;
    while (!quit_) {
        std::printf("\033[1;32m(sgpu w%d pc=0x%04x cyc=%llu)\033[0m ", warp_, m_.warps[warp_].pc,
                    (unsigned long long)m_.stats.cycles);
        std::fflush(stdout);
        if (!std::getline(std::cin, line)) break;
        exec_line(line);
    }
    std::printf("退出调试模式。\n");
}

bool Debugger::exec_line(const std::string& line) {
    const auto t = split(line);
    if (t.empty()) return true;
    const std::string& c = t[0];
    auto num = [&](size_t i, int def) { bool ok = false; uint32_t v = parse_u32(t[i], &ok); return ok ? int(v) : def; };

    if (c == "q" || c == "quit" || c == "exit") { quit_ = true; return false; }
    else if (c == "h" || c == "help" || c == "?") help();
    else if (c == "si" || c == "s" || c == "step") cmd_si(t.size() > 1 ? num(1, 1) : 1);
    else if (c == "c" || c == "cont" || c == "continue") cmd_continue();
    else if (c == "u" || c == "until") { if (t.size() < 2) std::printf("用法: u <addr>\n"); else { bool ok; uint32_t a = parse_u32(t[1], &ok); if (ok) cmd_until(a); } }
    else if (c == "p" || c == "regs" || c == "reg") cmd_regs(t.size() > 1 ? t[1] : "");
    else if (c == "w" || c == "warp") { if (t.size() > 1) { bool ok = false; uint32_t v = parse_u32(t[1], &ok); if (ok) set_warp(int(v)); } else cmd_warps(); }
    else if (c == "l" || c == "lanes") cmd_lanes();
    else if (c == "st" || c == "stack") cmd_stack();
    else if (c == "x" || c == "xmem") cmd_mem(t.size() > 1 ? line : "", false);
    else if (c == "xs") cmd_mem(t.size() > 1 ? line : "", true);
    else if (c == "dis" || c == "u/") { bool ok = false; uint32_t a = t.size() > 1 ? parse_u32(t[1], &ok) : m_.warps[warp_].pc; cmd_dis(a, t.size() > 2 ? num(2, 8) : 8); }
    else if (c == "state" || c == "info") cmd_state();
    else if (c == "cmd" || c == "cmds") cmd_cmdlist();
    else if (c == "log") cmd_log(t.size() > 1 ? num(1, 20) : 20);
    else if (c == "stat" || c == "stats") cmd_stat();
    else if (c == "mmio") cmd_mmio();
    else if (c == "b" || c == "break") cmd_break(t.size() > 1 ? t[1] : "");
    else if (c == "bd") {
        bool ok = false; uint32_t a = t.size() > 1 ? parse_u32(t[1], &ok) : 0;
        if (!ok) std::printf("用法: bd <addr>\n");
        else if (bps_.erase(a)) std::printf("删除断点 0x%04x\n", a);
        else std::printf("没有 0x%04x 处的断点\n", a);
    }
    else if (c == "bc") { bps_.clear(); std::printf("断点已清空\n"); }
    else if (c == "trace") cmd_trace(t.size() > 1 ? t[1] : "");
    else if (c == "set") cmd_set(line);
    else std::printf("未知命令 '%s'(输入 help)\n", c.c_str());
    return true;
}

void Debugger::help() const {
    std::printf(
        "单步/运行:\n"
        "  si [n] / s [n]      单步 n 条指令(默认 1), 打印每条的反汇编\n"
        "  c / continue        运行到结束 / 断点 / 错误(会自动推进 CP 命令流)\n"
        "  u <addr>            运行到地址 <addr>(临时断点)\n"
        "  b <addr>            设断点;  b <warp>:<addr> 只对某个 warp;  b 列出\n"
        "  bd <addr> / bc      删一个断点 / 清空\n"
        "查看:\n"
        "  p [reg]             打印当前 warp 的寄存器(8 lane 并排); p r5 只看一个\n"
        "  w [n]               切换当前 warp / 列出所有 warp 状态\n"
        "  l / lanes           当前 warp 的 active_mask 与 4 个谓词(逐 lane)\n"
        "  st / stack          SIMT 栈(recov/fall/mask/pending)\n"
        "  x <addr> [n]        看全局内存(默认 4 个字);  xs <addr> [n] 看共享内存\n"
        "  dis [addr] [n]      反汇编(默认当前 PC 起 8 条)\n"
        "  state / cmd / stat  整机状态 / 命令流摘要 / 运行统计\n"
        "  mmio                打印 MMIO 寄存器表\n"
        "  log [n]             最近 n 条日志(含 trace; 由 --log/--log-level 控制落盘)\n"
        "状态修改:\n"
        "  set r5 0x10 | set r5.3 0x10 | set pc 0x40 | set mask 0x0f\n"
        "  trace on|off        打开/关闭逐条指令日志(TRACE 级)\n"
        "  q / quit            退出\n");
}

void Debugger::set_warp(int w) {
    if (w < 0 || w >= MAX_WARPS) { std::printf("warp 编号必须在 0..%d\n", MAX_WARPS - 1); return; }
    warp_ = w;
    std::printf("当前 warp = %d\n  %s\n", w, m_.dump_warp(w).c_str());
}

// ─────────────────────────── 单步 ───────────────────────────

bool Debugger::step_one(int wid) {
    Warp& w = m_.warps[wid];
    if (!w.alive) { std::printf("warp %d 已退出, 换一个(命令 w 看列表)\n", wid); return false; }
    if (w.stalled) { std::printf("warp %d 在 bar.sync 等待中(需要别的 warp 到达)\n", wid); return false; }

    uint32_t inst = 0;
    MemErr e = MemErr::NONE;
    std::printf("w%-2d 0x%04x  ", wid, w.pc);
    if (!m_.mem.fetch(m_.warp_sm(wid), w.pc, inst, &e)) {
        std::printf("取指失败(%s)\n", e == MemErr::ALIGN ? "PC 未对齐" : "PC 超出 SRAM");
    } else {
        DecodedInst d;
        if (!decode(inst, d)) std::printf("非法指令 0x%08x\n", inst);
        else std::printf("%s\n", itrace(d, w.pc).c_str());
    }
    const uint32_t pc0 = w.pc;
    const uint8_t  m0  = w.active_mask;
    if (!m_.step_warp(wid)) return false;
    std::printf("      → pc=0x%04x mask=0x%02x 栈深=%d%s\n", w.pc, w.active_mask, w.stack.top,
                m0 != w.active_mask || pc0 + 4 != w.pc ? "  ← 控制流变化" : "");
    return true;
}

void Debugger::cmd_si(int n) {
    if (!m_.launched()) { std::printf("没有正在运行的内核: 先 c 让 CP 分发命令流\n"); return; }
    int done = 0;
    for (int i = 0; i < n; ++i) {
        const int wid = warp_;
        if (!step_one(wid)) break;
        ++done;
        if (!m_.active()) { std::printf("内核已结束(所有 warp exit)\n"); break; }
    }
    if (n > 1) std::printf("单步 %d 条完成\n", done);
}

// ─────────────────────────── 断点与运行 ───────────────────────────

bool Debugger::bp_hit(int* wid) const {
    for (int i = 0; i < MAX_WARPS; ++i) {
        const Warp& w = m_.warps[i];
        if (!w.alive || w.stalled) continue;
        uint32_t pc = w.pc;
        if (bps_.count(pc)) { if (wid) *wid = i; return true; }
    }
    return false;
}

void Debugger::run_machine_honoring_bp(uint64_t max_cycles) {
    while (true) {
        int w = -1;
        if (bp_hit(&w)) {
            std::printf("命中断点: warp %d pc=0x%04x\n", w, m_.warps[w].pc);
            char b2[64];
            std::snprintf(b2, sizeof(b2), "断点 0x%04x (warp %d)", m_.warps[w].pc, w);
            m_.set_stop(StopReason::BREAKPOINT, b2);
            return;
        }
        if (m_.stop().kind == StopReason::SM_ERROR) return;
        if (!m_.active()) {                       // 内核跑完了: 补上停止原因, 供 report_stop 显示
            if (m_.stop().kind == StopReason::RUNNING) m_.set_stop(StopReason::ALL_EXIT);
            return;
        }

        std::string why;
        if (m_.barrier_deadlock(&why)) { m_.set_stop(StopReason::DEADLOCK, why); return; }
        if (max_cycles && m_.stats.cycles >= max_cycles) {
            m_.set_stop(StopReason::TIMEOUT, "运行到周期上限 " + std::to_string(max_cycles));
            return;
        }
        m_.step_cycle();
    }
}

void Debugger::report_stop() {
    const StopReason st = m_.stop();
    std::printf("停止: %s\n", st.name());
    if (!st.detail.empty()) std::printf("  原因: %s\n", st.detail.c_str());
    for (int i = 0; i < MAX_WARPS; ++i)
        if (m_.warps[i].alive || m_.stats.retired[i]) std::printf("  %s\n", m_.dump_warp(i).c_str());
    for (int t = 0; t < NUM_SM; ++t) {
        if (m_.sm[t].error == SmErr::NONE) continue;
        std::printf("  SM%d 错误 %s @FAULT_PC=0x%04x (warp %d): 用 x / dis 0x%04x 看看现场\n",
                    t, sm_err_name(m_.sm[t].error), m_.sm[t].fault_pc, m_.sm[t].fault_warp, m_.sm[t].fault_pc);
    }
}

void Debugger::cmd_continue() {
    m_.set_stop(StopReason::RUNNING);
    for (;;) {
        run_machine_honoring_bp(m_.stats.cycles + cp_.timeout_cycles());
        report_stop();
        const StopReason::Kind k = m_.stop().kind;
        if (k != StopReason::ALL_EXIT && k != StopReason::BREAKPOINT && k != StopReason::STEP) return;
        if (k == StopReason::BREAKPOINT) return;
        if (!cp_.launch_pending()) {
            if (cp_.process(false, nullptr) == 0) return;      // 没有后续命令了
            if (!cp_.launch_pending()) { std::printf("命令流处理完毕\n"); return; }
        }
        if (!cp_.finish_launch(nullptr)) { std::printf("LAUNCH 完成判定失败(见日志)\n"); return; }
        if (!cp_.launch_pending()) { std::printf("命令流处理完毕\n"); return; }
    }
}

void Debugger::cmd_until(uint32_t addr) {
    bps_.insert(addr);
    cmd_continue();
    bps_.erase(addr);
}

// ─────────────────────────── 查看 ───────────────────────────

void Debugger::cmd_regs(const std::string& arg) {
    if (arg.empty()) { std::printf("%s", m_.dump_regs(warp_).c_str()); return; }
    std::string a = arg;
    if (a[0] == 'r') a = a.substr(1);
    bool ok = false;
    const uint32_t idx = parse_u32(a, &ok);
    if (!ok || idx >= uint32_t(NUM_GPR)) { std::printf("用法: p r5   (寄存器 0..%d)\n", NUM_GPR - 1); return; }
    std::printf("warp %d  r%u:", warp_, idx);
    for (int l = 0; l < WARP_SIZE; ++l) std::printf(" [L%d]%08x", l, m_.warps[warp_].rf.gpr[l][idx]);
    std::printf("\n");
}

void Debugger::cmd_warps() {
    for (int i = 0; i < MAX_WARPS; ++i) {
        if (!m_.warps[i].alive && m_.stats.retired[i] == 0) continue;
        std::printf("  %s%s\n", i == warp_ ? "* " : "  ", m_.dump_warp(i).c_str());
    }
}

void Debugger::cmd_lanes() {
    const Warp& w = m_.warps[warp_];
    std::printf("warp %d  active_mask=0x%02x  recov=0x%04x(%s)\n", warp_, w.active_mask, w.recov_pc,
                w.recov_valid ? "有效" : "无效");
    std::printf("  lane  :");
    for (int l = 0; l < WARP_SIZE; ++l) std::printf(" %d", l);
    std::printf("\n  active:");
    for (int l = 0; l < WARP_SIZE; ++l) std::printf(" %c", (w.active_mask >> l & 1) ? 'Y' : '.');
    for (int p = 1; p < NUM_PRED; ++p) {
        std::printf("\n  p%d    :", p);
        for (int l = 0; l < WARP_SIZE; ++l) std::printf(" %c", w.rf.pred[l][p] ? '1' : '0');
    }
    std::printf("\n  (p0 恒真; Y = 该 lane 属于当前 active_mask)\n");
}

void Debugger::cmd_stack() {
    const Warp& w = m_.warps[warp_];
    std::printf("warp %d SIMT 栈: %d/%d 帧%s\n", warp_, w.stack.top, SIMT_STACK_DEPTH,
                w.stack.empty() ? "(空 ⇒ 无未收敛的发散)" : "");
    for (int i = w.stack.top - 1; i >= 0; --i) {
        const StackFrame& f = w.stack.frames[i];
        std::printf("  [%d] recov=0x%04x fall=0x%04x mask_full=0x%02x mask_pending=0x%02x pending=%d\n",
                    i, f.recov_pc, f.fall_pc, f.mask_full, f.mask_pending, f.pending ? 1 : 0);
    }
}

void Debugger::cmd_mem(const std::string& line, bool shared) {
    const auto t = split(line);
    if (t.size() < 2) { std::printf("用法: %s <addr> [n]\n", shared ? "xs" : "x"); return; }
    bool ok = false;
    const uint32_t addr = parse_u32(t[1], &ok);
    if (!ok) { std::printf("地址无法解析\n"); return; }
    int n = 4;
    if (t.size() > 2) { bool ok2; uint32_t v = parse_u32(t[2], &ok2); if (ok2 && v < 4096) n = int(v); }
    for (int i = 0; i < n; ++i) {
        const uint32_t a = addr + uint32_t(i) * 4;
        uint32_t v = shared ? m_.mem.ld_shared(0, a) : m_.mem.ld_global(a);
        const char* region = nullptr;
        if (!shared) {
            if (a < MMIO_END) region = "MMIO";
            else {
                uint32_t qb = cp_.queue_base(), qs = cp_.queue_size();
                if (a >= qb && a - qb < qs) region = "命令队列";
                else if (a >= CONST_BASE && a - CONST_BASE < CONST_SIZE) region = "常量区";
                else if (a >= KERNEL_IMG_BASE && a - KERNEL_IMG_BASE < 0x7000) region = "内核镜像区";
                else if (a >= m_.fb_base && a - m_.fb_base < m_.fb_size) region = "帧缓冲";
                else if (a >= DDR_BASE) region = "DDR";
            }
        }
        std::printf("  %s0x%08x = 0x%08x  %11u  %s%s\n", shared ? "shared:" : "", a, v, v,
                    region ? region : "", shared && a >= uint32_t(SHARED_SIZE) ? "  ← 超出 16KB!" : "");
    }
    if (shared) std::printf("  (仅显示 SM0 的共享内存; 每个 SM 的共享内存是私有的, ISA §3.2)\n");
}

void Debugger::cmd_dis(uint32_t addr, int n) {
    for (int i = 0; i < n; ++i) {
        const uint32_t pc = addr + uint32_t(i) * 4;
        uint32_t inst = 0;
        MemErr e = MemErr::NONE;
        if (!m_.mem.fetch(m_.warp_sm(warp_), pc, inst, &e)) { std::printf("  0x%04x  <取指失败>\n", pc); break; }
        DecodedInst d;
        if (!decode(inst, d)) std::printf("  0x%04x  0x%08x  <非法: %s>\n", pc, inst, d.error ? d.error : "?");
        else std::printf("  0x%04x  0x%08x  %s%s\n", pc, inst, itrace(d, pc).c_str(),
                         pc == m_.warps[warp_].pc ? "   ← 当前 PC" : "");
    }
}

void Debugger::cmd_state() {
    int act = 0;
    for (int t = 0; t < NUM_SM; ++t) if (m_.sm[t].resident > 0) ++act;
    std::printf("周期=%llu 已发射=%llu  活跃 SM=%d  IPC/SM≈%.2f\n",
                (unsigned long long)m_.stats.cycles, (unsigned long long)m_.stats.issued, act,
                (m_.stats.cycles && act) ? double(m_.stats.issued) / double(m_.stats.cycles) / act : 0.0);
    std::printf("kernel: launched=%d num_warps=%u kernel_pc=0x%04x flags=0x%x 停止原因=%s\n",
                m_.launched() ? 1 : 0, m_.num_warps(), m_.kernel_pc(), m_.launch_flags(),
                m_.stop().name());
    std::printf("%s", m_.dump_state().c_str());
    std::printf("CP: STATUS=0x%x(%s%s%s) ERROR=%s HEAD=0x%08x TAIL=0x%08x 待处理=%u 字节\n",
                cp_.status(),
                (cp_.status() & 1) ? "IDLE " : "", (cp_.status() & 2) ? "BUSY " : "",
                (cp_.status() & 4) ? "ERROR" : "", cp_err_name(cp_.error()),
                cp_.head(), cp_.tail(), (cp_.tail() - cp_.head() + cp_.queue_size()) % cp_.queue_size());
    std::printf("MMIO: SM_DONE=0x%x SM_KERNEL_MASK=0x%x SM0_ERR=%s SM1_ERR=%s IRQ=0x%x\n",
                m_.mmio_read(MMIO_SM_DONE), m_.mmio_read(MMIO_SM_KERNEL_MASK),
                sm_err_name(m_.sm[0].error), sm_err_name(m_.sm[1].error), cp_.irq_status());
}

void Debugger::cmd_cmdlist() {
    std::printf("命令流(已处理 %llu 条):\n", (unsigned long long)cp_.cmds_done());
    for (const auto& ci : cp_.history())
        std::printf("  @0x%08x  %-12s len=%-3u sig=%-5s wait=%-5s %s\n", ci.addr, cmd_opcode_name(ci.opcode),
                    ci.len, ci.signal_id == SEM_NONE ? "-" : std::to_string(ci.signal_id).c_str(),
                    ci.wait_id == SEM_NONE ? "-" : std::to_string(ci.wait_id).c_str(), ci.summary.c_str());
    std::printf("  (HEAD=0x%08x TAIL=0x%08x)\n", cp_.head(), cp_.tail());
}

void Debugger::cmd_log(int n) {
    const auto& r = logger().recent();
    const int start = int(r.size()) > n ? int(r.size()) - n : 0;
    std::printf("最近 %d 条日志(缓冲共 %zu 条):\n", int(r.size()) - start, r.size());
    for (int i = start; i < int(r.size()); ++i) {
        const auto& x = r[size_t(i)];
        if (x.warp >= 0)
            std::printf("  [%8llu cyc w%-2d pc=0x%04x] %-5s %-18s %s\n", (unsigned long long)x.cycle, x.warp,
                        x.pc, log_level_name(x.lvl), x.where.c_str(), x.msg.c_str());
        else
            std::printf("  [%8llu cyc           ] %-5s %-18s %s\n", (unsigned long long)x.cycle,
                        log_level_name(x.lvl), x.where.c_str(), x.msg.c_str());
    }
    std::printf("  (日志文件: %s)\n", logger().file_open() ? logger().file_path().c_str() : "未开启(--log FILE)");
}

void Debugger::cmd_stat() {
    const RunStats& s = m_.stats;
    int act = 0;
    for (int t = 0; t < NUM_SM; ++t) if (m_.sm[t].resident > 0) ++act;
    std::printf("周期=%llu 发射=%llu (活跃 SM %d 个 ⇒ 每 SM %.2f 条/周期)\n", (unsigned long long)s.cycles,
                (unsigned long long)s.issued, act,
                (s.cycles && act) ? double(s.issued) / double(s.cycles) / act : 0.0);
    std::printf("  按 SM 发射: SM0=%llu SM1=%llu;  barrier=%llu(等待空转 %llu 拍)\n",
                (unsigned long long)s.sm_issued[0], (unsigned long long)s.sm_issued[1],
                (unsigned long long)s.barriers, (unsigned long long)s.issue_stall_barrier);
    std::printf("  发散=%llu 收敛=%llu SIMT 栈最深=%llu;  FP 指令=%llu\n",
                (unsigned long long)s.divergences, (unsigned long long)s.reconvergences,
                (unsigned long long)s.stack_max, (unsigned long long)s.fp_ops);
    std::printf("  访存: ld.g=%llu st.g=%llu(共 %llu 个 32B 行请求)  ld.s=%llu st.s=%llu(bank 冲突额外 %llu 拍)\n",
                (unsigned long long)s.ld_global, (unsigned long long)s.st_global, (unsigned long long)s.gmem_lines,
                (unsigned long long)s.ld_shared, (unsigned long long)s.st_shared,
                (unsigned long long)s.shared_extra_cycles);
    std::printf("  已知区之外的访存=%llu\n", (unsigned long long)s.unmapped_access);
    std::printf("  指令分布:\n");
    for (const auto& kv : s.icount) std::printf("    %-12s %llu\n", kv.first.c_str(), (unsigned long long)kv.second);
}

void Debugger::cmd_mmio() {
    static const struct { uint32_t off; const char* name; const char* attr; } tab[] = {
        {0x00, "CP_STATUS", "R"},        {0x04, "CP_ERROR", "R/W"},      {0x08, "CMD_HEAD", "R"},
        {0x0C, "CMD_TAIL", "R/W"},       {0x10, "DOORBELL", "W"},        {0x14, "IRQ_STATUS", "R/W"},
        {0x18, "IRQ_ENABLE", "R/W"},     {0x1C, "SM_DONE", "R"},         {0x20, "SM0_ERROR", "R/W"},
        {0x24, "SM1_ERROR", "R/W"},      {0x28, "SEM_BASE", "R/W"},      {0x2C, "CMD_QUEUE_BASE", "R/W"},
        {0x30, "CMD_QUEUE_SIZE", "R/W"}, {0x34, "CONST_BASE", "R"},      {0x38, "FB_BASE", "R/W"},
        {0x3C, "DDR_BASE", "R"},         {0x40, "CP_RESET", "W"},        {0x44, "TIMEOUT_CYCLES", "R/W"},
        {0x48, "SM_KERNEL_MASK", "R"},   {0x4C, "SM0_FAULT_PC", "R"},    {0x50, "SM1_FAULT_PC", "R"},
        {0x60, "GEMM_STATUS", "R"},      {0x64, "GEMM_ERROR", "R/W"},
    };
    std::printf("MMIO(CMDS §7, 基址 0x0000_0000):\n");
    for (const auto& e : tab)
        std::printf("  0x%02x  %-16s %-4s = 0x%08x\n", e.off, e.name, e.attr, m_.mmio_read(e.off));
}

void Debugger::cmd_break(const std::string& arg) {
    if (arg.empty()) {
        if (bps_.empty()) std::printf("没有断点\n");
        for (uint32_t a : bps_) std::printf("  断点 0x%04x\n", a);
        return;
    }
    bool ok = false;
    const uint32_t a = parse_u32(arg, &ok);
    if (!ok) { std::printf("用法: b <addr> 或 b <warp>:<addr>\n"); return; }
    bps_.insert(a);
    std::printf("断点 0x%04x 已设置(共 %zu 个)\n", a, bps_.size());
}

void Debugger::cmd_trace(const std::string& arg) {
    if (arg == "on")  { trace_ = true;  logger().set_console_level(LogLevel::TRACE); logger().set_ring_level(LogLevel::TRACE); std::printf("逐条指令日志: 开(控制台 + 环形缓冲)\n"); }
    else if (arg == "off") { trace_ = false; logger().set_console_level(LogLevel::INFO); logger().set_ring_level(LogLevel::INFO); std::printf("逐条指令日志: 关\n"); }
    else std::printf("trace 当前: %s (用法 trace on|off)\n", trace_ ? "on" : "off");
}

void Debugger::cmd_set(const std::string& line) {
    const auto t = split(line);
    if (t.size() < 3) { std::printf("用法: set r5 0x10 | set r5.3 0x10 | set pc 0x40 | set mask 0x0f\n"); return; }
    bool ok = false;
    const uint32_t v = parse_u32(t[2], &ok);
    if (!ok) { std::printf("值无法解析\n"); return; }
    Warp& w = m_.warps[warp_];
    if (t[1] == "pc") { w.pc = v; std::printf("warp %d pc ← 0x%04x\n", warp_, v); return; }
    if (t[1] == "mask") { w.active_mask = uint8_t(v); std::printf("warp %d active_mask ← 0x%02x\n", warp_, w.active_mask); return; }
    if (t[1][0] == 'r') {
        std::string a = t[1].substr(1);
        int lane = -1;
        const size_t dot = a.find('.');
        if (dot != std::string::npos) { bool ok2; lane = int(parse_u32(a.substr(dot + 1), &ok2)); a = a.substr(0, dot); }
        const uint32_t idx = parse_u32(a, &ok);
        if (!ok || idx >= uint32_t(NUM_GPR)) { std::printf("寄存器号非法\n"); return; }
        if (lane >= 0 && lane < WARP_SIZE) {
            w.rf.gpr[lane][idx] = v;
            std::printf("warp %d r%u(L%d) ← 0x%08x\n", warp_, idx, lane, v);
        } else {
            for (int l = 0; l < WARP_SIZE; ++l) w.rf.gpr[l][idx] = v;
            std::printf("warp %d r%u(全部 8 lane) ← 0x%08x\n", warp_, idx, v);
        }
        return;
    }
    std::printf("只支持 set r<idx>[.lane] | set pc | set mask\n");
}

}  // namespace simu
