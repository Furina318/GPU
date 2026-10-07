#pragma once

// 交互式调试器: 单步(si)、断点、寄存器/内存/SIMT 栈/SIMT 掩码查看、日志回看。
// 从 stdin 读命令, 所以既能交互用, 也能脚本化(printf 'si\nsi\nq\n' | simulator -d ...)。

#include "cp.hpp"
#include "machine.hpp"

#include <set>
#include <string>

namespace simu {

class Debugger {
public:
    Debugger(Machine& m, CommandProcessor& cp) : m_(m), cp_(cp) {}

    void run();                      // REPL 主循环(读到 EOF 退出)
    bool exec_line(const std::string& line);   // 返回 false 表示要求退出

    int      cur_warp() const { return warp_; }
    void     set_warp(int w);
    bool     trace_on() const { return trace_; }

private:
    void help() const;
    void cmd_si(int n);
    void cmd_continue();
    void cmd_until(uint32_t addr);
    void cmd_regs(const std::string& arg);
    void cmd_warps();
    void cmd_lanes();
    void cmd_stack();
    void cmd_mem(const std::string& arg, bool shared);
    void cmd_dis(uint32_t addr, int n);
    void cmd_state();
    void cmd_cmdlist();
    void cmd_log(int n);
    void cmd_stat();
    void cmd_mmio();
    void cmd_break(const std::string& arg);
    void cmd_trace(const std::string& arg);
    void cmd_set(const std::string& arg);

    bool     bp_hit(int* wid) const;
    bool     step_one(int wid);                 // 单步 + 打印
    void     run_machine_honoring_bp(uint64_t max_cycles);
    void     report_stop();

    Machine&          m_;
    CommandProcessor& cp_;
    int               warp_ = 0;
    bool              trace_ = false;
    std::set<uint32_t> bps_;
    bool              quit_ = false;
};

}  // namespace simu
