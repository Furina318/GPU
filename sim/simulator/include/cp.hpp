#pragma once

// CP(命令处理器): 命令环 → 校验(CMDS §2.4)→ 执行 → 依赖/信号量 → 完成判定(§4.3)。
// shader 核在 machine.hpp;这里只做"命令流"这一层,以及矩阵引擎的 v1(§6.11)。

#include "config.hpp"
#include "machine.hpp"

#include <cstdint>
#include <functional>
#include <string>
#include <vector>

namespace simu {

constexpr uint32_t CMD_QUEUE_BASE_DEFAULT = 0x0100'0000;   // CMDS §2.1 / PLAN §3
constexpr uint32_t CMD_QUEUE_SIZE_DEFAULT = 4096;
constexpr uint32_t SEM_BASE_DEFAULT       = 0x0300'0000;   // 信号量区(驱动可写 SEM_BASE)
constexpr uint16_t SEM_NONE               = 0xFFFF;

// CMDS §8.1
enum class CpErr : uint32_t {
    NO_ERROR = 0, CMD_ERROR = 1, LAUNCH_REJECT = 2, BUS_ERROR = 3,
    TIMEOUT = 4, ILLEGAL_STATE = 5, STALE_SM_ERROR = 6,
    // 7..15 是文档保留区, 这里占 7 并在 CMDS §8.1 表里登记:
    // SM 架构错误本身没有 CP 错误码(权威信息在 SM*_ERROR/FAULT_PC, §8.3), 但 §4.3 要求
    // "任一 SM 出错 ⇒ 置 CP_ERROR", 所以需要一个取值把 CP 标成错误态。
    SM_FAULT = 7,
};
const char* cp_err_name(CpErr e);
const char* cmd_opcode_name(uint8_t opcode);

// 命令 opcode(CMDS §3)
enum : uint8_t {
    CMD_NOP = 0x00, CMD_WRITE_REG = 0x01, CMD_READ_REG = 0x02, CMD_LOAD_KERNEL = 0x03,
    CMD_LAUNCH = 0x04, CMD_FENCE = 0x05, CMD_SIGNAL = 0x06, CMD_WAIT = 0x07,
    CMD_IRQ = 0x08, CMD_PARAMS = 0x09, CMD_GEMM = 0x18,
};

// 一条命令的摘要(启动时打印 / 日志)
struct CmdInfo {
    uint32_t    addr = 0;                 // 命令在队列里的全局地址
    uint8_t     opcode = 0;
    uint8_t     flags = 0;
    uint16_t    len = 0;
    uint16_t    signal_id = SEM_NONE;
    uint16_t    wait_id = SEM_NONE;
    std::string summary;                  // 人读一行
};

class CommandProcessor {
public:
    explicit CommandProcessor(Machine& m) : machine_(m) { machine_.attach_cp(this); }

    // ── 驱动侧: 往环形队列里追加命令 ──
    bool submit(const std::vector<uint32_t>& words, std::string* err);
    bool submit_nop(uint32_t words_len, std::string* err);
    bool submit_write_reg(uint32_t addr, uint32_t value, std::string* err);
    bool submit_read_reg(uint32_t addr, uint32_t dst_addr, std::string* err);
    bool submit_load_kernel(uint32_t src_addr, uint32_t size_bytes, uint8_t sm_mask, std::string* err);
    bool submit_launch(uint32_t kernel_pc, uint32_t num_warps, uint32_t launch_flags,
                       uint16_t signal_id, uint16_t wait_id, std::string* err);
    bool submit_fence(std::string* err);
    bool submit_signal(uint32_t sem_addr, uint32_t value, std::string* err);
    bool submit_wait(uint32_t sem_addr, uint32_t value, std::string* err);
    bool submit_irq(uint8_t irq_id, std::string* err);
    bool submit_params(uint32_t dst_off, const std::vector<uint32_t>& params, std::string* err);
    bool submit_gemm(const uint32_t desc[16], std::string* err);

    // 驱动写完 tail 后写 DOORBELL(CMDS §2.1);这里等价于直接触发处理
    void doorbell();

    // 处理队列: run_kernels=false 时遇到 LAUNCH 就停下(内核交给调试器单步)
    int  process(bool run_kernels, std::string* err);
    bool launch_pending() const { return launch_pending_; }
    void set_launch_hook(std::function<void()> f) { launch_hook_ = std::move(f); }
    bool finish_launch(std::string* err);      // 内核跑完后的收尾 + 继续处理后续命令

    // ── MMIO 里属于 CP/引擎的寄存器(CMDS §7) ──
    uint32_t read_reg(uint32_t off);
    void     write_reg(uint32_t off, uint32_t v);
    void     reset();                          // CP_RESET(§7.3)
    void     on_sm_error(int smid);
    void     set_timeout_cycles(uint32_t c) { timeout_cycles_ = c ? c : 0x100000; }

    uint32_t status() const;                   // CP_STATUS: bit0 IDLE, bit1 BUSY, bit2 ERROR
    bool     in_error() const { return err_ != CpErr::NO_ERROR || sm_fault_; }
    bool     idle() const { return !machine_.active() && !in_error(); }
    CpErr    error() const { return err_; }
    const std::string& error_detail() const { return err_detail_; }
    bool     sm_fault() const { return sm_fault_; }
    int      sm_fault_id() const { return sm_fault_id_; }

    uint32_t queue_base() const { return queue_base_; }
    uint32_t queue_size() const { return queue_size_; }
    uint32_t head() const { return head_; }
    uint32_t tail() const { return tail_; }
    uint32_t sem_base() const { return sem_base_; }
    uint32_t timeout_cycles() const { return timeout_cycles_; }
    uint32_t irq_status() const { return irq_status_; }
    uint32_t irq_enable() const { return irq_enable_; }
    uint64_t cmds_done() const { return cmds_done_; }

    const std::vector<CmdInfo>& history() const { return history_; }

    // 矩阵引擎
    uint32_t gemm_status() const;
    uint32_t gemm_error() const { return gemm_error_; }
    uint64_t gemm_ops() const { return gemm_ops_; }
    uint32_t gemm_cmds() const { return gemm_cmds_; }

private:
    bool     set_err(CpErr e, const std::string& why);
    bool     writable_reg(uint32_t off) const;
    uint32_t qread(uint32_t addr);
    void     qwrite(uint32_t addr, uint32_t v);
    uint32_t sem_read(uint32_t id);
    void     sem_add(uint32_t id);
    bool     check_wait(uint16_t wait_id, const CmdInfo& ci);
    void     signal_done(uint16_t signal_id, uint32_t cmd_addr);
    bool     exec_cmd(CmdInfo& ci, const uint32_t* w);
    bool     exec_load_kernel(const uint32_t* w);
    bool     exec_launch(const uint32_t* w, CmdInfo& ci);
    bool     exec_gemm(const uint32_t* w, CmdInfo& ci);
    bool     queue_full_for(uint32_t bytes);
    bool     complete_launch(std::string* err);
    void     set_irq(uint32_t bit);
    bool     queue_empty() const { return head_ == tail_; }
    bool     exec_is_last_ = false;   // 正在执行的命令是队列里最后一条(§2.1 的 "队列为空" 判据)

    Machine&   machine_;
    uint32_t   queue_base_ = CMD_QUEUE_BASE_DEFAULT;
    uint32_t   queue_size_ = CMD_QUEUE_SIZE_DEFAULT;
    uint32_t   sem_base_   = SEM_BASE_DEFAULT;
    uint32_t   timeout_cycles_ = 0x100000;      // ≈5ms @200MHz
    uint32_t   head_ = queue_base_, tail_ = CMD_QUEUE_BASE_DEFAULT;
    uint32_t   irq_status_ = 0, irq_enable_ = 0;
    CpErr      err_ = CpErr::NO_ERROR;
    std::string err_detail_;
    bool       sm_fault_ = false;
    int        sm_fault_id_ = -1;
    bool       launch_pending_ = false;
    uint32_t   pending_signal_ = SEM_NONE;
    uint32_t   pending_addr_ = 0;
    uint64_t   cmds_done_ = 0;
    std::vector<CmdInfo> history_;

    std::function<void()> launch_hook_;
    bool       gemm_busy_ = false, gemm_done_ = false;
    uint32_t   gemm_error_ = 0;
    uint64_t   gemm_ops_ = 0;
    uint32_t   gemm_cmds_ = 0;
};

}  // namespace simu
