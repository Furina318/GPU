// CP: 命令环 → 校验(CMDS §2.4)→ 执行 → 信号量/依赖 → 完成判定(§4.3)。
// 执行模型是 in-order 且阻塞式: LAUNCH 之后 CP 一直等到该 kernel 的 SM_DONE 齐(§2.5),
// 这也是为什么 process() 里 LAUNCH 会直接把 machine_.run() 跑完。

#include "cp.hpp"
#include "log.hpp"

#include <algorithm>
#include <cstring>
#include <vector>

namespace simu {

const char* cp_err_name(CpErr e) {
    switch (e) {
    case CpErr::NO_ERROR:      return "NO_ERROR";
    case CpErr::CMD_ERROR:     return "CMD_ERROR";
    case CpErr::LAUNCH_REJECT: return "LAUNCH_REJECT";
    case CpErr::BUS_ERROR:     return "BUS_ERROR";
    case CpErr::TIMEOUT:       return "TIMEOUT";
    case CpErr::ILLEGAL_STATE: return "ILLEGAL_STATE";
    case CpErr::STALE_SM_ERROR:return "STALE_SM_ERROR";
    case CpErr::SM_FAULT:      return "SM_FAULT(模拟器补充)";
    }
    return "?";
}

const char* cmd_opcode_name(uint8_t op) {
    switch (op) {
    case CMD_NOP:         return "NOP";
    case CMD_WRITE_REG:   return "WRITE_REG";
    case CMD_READ_REG:    return "READ_REG";
    case CMD_LOAD_KERNEL: return "LOAD_KERNEL";
    case CMD_LAUNCH:      return "LAUNCH";
    case CMD_FENCE:       return "FENCE";
    case CMD_SIGNAL:      return "SIGNAL";
    case CMD_WAIT:        return "WAIT";
    case CMD_IRQ:         return "IRQ";
    case CMD_PARAMS:      return "PARAMS";
    case CMD_GEMM:        return "GEMM";
    }
    if (op >= 0x10 && op <= 0x17) return "2D_*";
    return "保留";
}

// IRQ 位(CMDS §7.4)
static const uint32_t IRQ_CP_ERROR = 1u << 0, IRQ_CMD_DONE = 1u << 1,
                      IRQ_LAUNCH_DONE = 1u << 2, IRQ_GEMM_DONE = 1u << 4;

bool CommandProcessor::set_err(CpErr e, const std::string& why) {
    if (err_ == CpErr::NO_ERROR) {        // 首个错误保留(§8.3)
        err_ = e;
        err_detail_ = why;
        LogE("CP_ERROR ← %s: %s(CMDS §8.1)", cp_err_name(e), why.c_str());
        set_irq(IRQ_CP_ERROR);
    } else {
        LogD("CP 又出错(%s: %s), 保留首个错误 %s", cp_err_name(e), why.c_str(), cp_err_name(err_));
    }
    return false;
}

void CommandProcessor::set_irq(uint32_t bit) {
    if (!(irq_status_ & bit)) LogI("IRQ_STATUS 置位 bit%u", __builtin_ctz(bit));
    irq_status_ |= bit;
    if (irq_enable_ & bit) LogI("IRQ %u 已使能 ⇒ 向 host 发中断", __builtin_ctz(bit));
}

uint32_t CommandProcessor::qread(uint32_t addr)                { return machine_.mem.ld_global(addr); }
void     CommandProcessor::qwrite(uint32_t addr, uint32_t v)   { machine_.mem.st_global(addr, v); }
uint32_t CommandProcessor::sem_read(uint32_t id)               { return machine_.mem.ld_global(sem_base_ + id * 4); }

void CommandProcessor::sem_add(uint32_t id) {
    if (id >= 0x100) { set_err(CpErr::CMD_ERROR, "signal_id " + std::to_string(id) + " 超出 0..255(§10.2)"); return; }
    const uint32_t v = sem_read(id) + 1;
    qwrite(sem_base_ + id * 4, v);
    LogI("信号量 SEM[%u] += 1 ⇒ %u (地址 0x%08x)", id, v, sem_base_ + id * 4);
}

uint32_t CommandProcessor::status() const {
    uint32_t s = 0;
    if (!machine_.active() && !launch_pending_ && queue_empty() && !in_error()) s |= 1u << 0;
    if (machine_.active() || launch_pending_ || !queue_empty())               s |= 1u << 1;
    if (in_error())                                                          s |= 1u << 2;
    return s;
}

uint32_t CommandProcessor::gemm_status() const {
    uint32_t s = 0;
    if (gemm_busy_)             s |= 1u << 0;
    if (gemm_done_)             s |= 1u << 1;
    if (gemm_error_)            s |= 1u << 2;
    return s;
}

// ─────────────────────────── 驱动侧: 追加命令 ───────────────────────────

bool CommandProcessor::queue_full_for(uint32_t bytes) {
    const uint32_t used = (tail_ - head_ + queue_size_) % queue_size_;
    if (used + bytes + 4 > queue_size_) {
        set_err(CpErr::CMD_ERROR, "命令队列满(used=" + std::to_string(used) +
                                  ", 需 " + std::to_string(bytes) + ", 容量 " + std::to_string(queue_size_) + ")");
        return true;
    }
    return false;
}

bool CommandProcessor::submit(const std::vector<uint32_t>& words, std::string* err) {
    auto fail = [&](const std::string& why) { if (err) *err = why; return false; };
    if (words.size() < 2)            return fail("命令至少 2 个字(CMDS §2.2)");
    if (machine_.active())           return fail("内核运行中不能追加命令");
    if (queue_full_for(uint32_t(words.size() * 4))) return fail(err_detail_);

    const uint32_t end = queue_base_ + queue_size_;
    const uint32_t bytes = uint32_t(words.size() * 4);
    if (tail_ + bytes > end) {                      // §2.1: 回绕前必须用 NOP 填到末尾
        const uint32_t rest = end - tail_;
        if (rest < 8) return fail("队列尾部剩 " + std::to_string(rest) + " 字节, 放不下 NOP 填充(建议 queue_size 是 8 的倍数)");
        const uint32_t nop_words = rest / 4;
        qwrite(tail_, (nop_words << 16) | (0u << 8) | CMD_NOP);
        qwrite(tail_ + 4, 0xFFFF'FFFFu);
        for (uint32_t i = 2; i < nop_words; ++i) qwrite(tail_ + i * 4, 0u);
        LogD("队列回绕: 在 0x%08x 处写入 %u 字的 NOP 填充", tail_, nop_words);
        tail_ = queue_base_;
    }
    for (size_t i = 0; i < words.size(); ++i) qwrite(tail_ + uint32_t(i) * 4, words[i]);
    tail_ += bytes;
    if (tail_ >= end) tail_ -= queue_size_;
    return true;
}

static std::vector<uint32_t> hdr(uint8_t op, uint16_t len, uint8_t flags,
                                 uint16_t signal_id, uint16_t wait_id) {
    std::vector<uint32_t> w(len, 0);
    w[0] = (uint32_t(len) << 16) | (uint32_t(flags) << 8) | op;
    w[1] = (uint32_t(signal_id) << 16) | wait_id;
    return w;
}

bool CommandProcessor::submit_nop(uint32_t n, std::string* err) {
    if (n < 2) n = 2;
    return submit(hdr(CMD_NOP, uint16_t(n), 0, SEM_NONE, SEM_NONE), err);
}

bool CommandProcessor::submit_write_reg(uint32_t addr, uint32_t value, std::string* err) {
    auto w = hdr(CMD_WRITE_REG, 4, 0, SEM_NONE, SEM_NONE);
    w[2] = addr;
    w[3] = value;
    return submit(w, err);
}

bool CommandProcessor::submit_read_reg(uint32_t addr, uint32_t dst_addr, std::string* err) {
    auto w = hdr(CMD_READ_REG, 4, 0, SEM_NONE, SEM_NONE);
    w[2] = addr;
    w[3] = dst_addr;
    return submit(w, err);
}

bool CommandProcessor::submit_load_kernel(uint32_t src_addr, uint32_t size_bytes,
                                          uint8_t sm_mask, std::string* err) {
    auto w = hdr(CMD_LOAD_KERNEL, 5, 0, SEM_NONE, SEM_NONE);
    w[2] = src_addr;
    w[3] = size_bytes;
    w[4] = sm_mask;
    return submit(w, err);
}

bool CommandProcessor::submit_launch(uint32_t kernel_pc, uint32_t num_warps, uint32_t launch_flags,
                                     uint16_t signal_id, uint16_t wait_id, std::string* err) {
    auto w = hdr(CMD_LAUNCH, 5, 0, signal_id, wait_id);
    w[2] = kernel_pc;
    w[3] = num_warps & 0xFF;
    w[4] = launch_flags;
    return submit(w, err);
}

bool CommandProcessor::submit_fence(std::string* err) {
    return submit(hdr(CMD_FENCE, 2, 0, SEM_NONE, SEM_NONE), err);
}

bool CommandProcessor::submit_signal(uint32_t sem_addr, uint32_t value, std::string* err) {
    auto w = hdr(CMD_SIGNAL, 4, 0, SEM_NONE, SEM_NONE);
    w[2] = sem_addr;
    w[3] = value;
    return submit(w, err);
}

bool CommandProcessor::submit_wait(uint32_t sem_addr, uint32_t value, std::string* err) {
    auto w = hdr(CMD_WAIT, 4, 0, SEM_NONE, SEM_NONE);
    w[2] = sem_addr;
    w[3] = value;
    return submit(w, err);
}

bool CommandProcessor::submit_irq(uint8_t irq_id, std::string* err) {
    auto w = hdr(CMD_IRQ, 3, 0, SEM_NONE, SEM_NONE);
    w[2] = irq_id;
    return submit(w, err);
}

bool CommandProcessor::submit_params(uint32_t dst_off, const std::vector<uint32_t>& params,
                                     std::string* err) {
    if (params.empty() || params.size() > 256) {
        if (err) *err = "PARAMS 的 N 必须在 1..256(§6.10)";
        return false;
    }
    auto w = hdr(CMD_PARAMS, uint16_t(4 + params.size()), 0, SEM_NONE, SEM_NONE);
    w[2] = dst_off;
    w[3] = uint32_t(params.size());
    for (size_t i = 0; i < params.size(); ++i) w[4 + i] = params[i];
    return submit(w, err);
}

bool CommandProcessor::submit_gemm(const uint32_t desc[16], std::string* err) {
    std::vector<uint32_t> w(desc, desc + 16);
    w[0] = (16u << 16) | (w[0] & 0xFF00u) | CMD_GEMM;
    return submit(w, err);
}

void CommandProcessor::doorbell() {
    if (err_ != CpErr::NO_ERROR) {
        LogW("DOORBELL 被忽略: CP 处于错误态 %s, 需要先写 CP_RESET(CMDS §7.3)", cp_err_name(err_));
        return;
    }
    const uint32_t bytes = (tail_ - head_ + queue_size_) % queue_size_;
    LogI("DOORBELL: 处理命令区间 [0x%08x, 0x%08x) 共 %u 字节", head_, tail_, bytes);
    process(true, nullptr);
}

// ─────────────────────────── 处理命令 ───────────────────────────

int CommandProcessor::process(bool run_kernels, std::string* err) {
    int n = 0;
    for (;;) {
        if (err_ != CpErr::NO_ERROR) break;
        if (launch_pending_) {
            if (!run_kernels) break;                 // 调试模式: 内核留给用户单步
            if (!complete_launch(err)) break;
        }
        if (queue_empty()) break;                    // 注意: 队列空但还有未完成的 LAUNCH 时不能退出

        CmdInfo ci;
        ci.addr = head_;
        const uint32_t w0 = qread(head_);
        ci.len      = uint16_t(w0 >> 16);
        ci.flags    = uint8_t(w0 >> 8);
        ci.opcode   = uint8_t(w0);
        const uint32_t w1 = qread(head_ + 4);
        ci.signal_id = uint16_t(w1 >> 16);
        ci.wait_id   = uint16_t(w1);

        if (ci.len < 2) { set_err(CpErr::CMD_ERROR, "命令长度 < 2 个字"); break; }
        if (head_ + ci.len * 4u > queue_base_ + queue_size_) {
            set_err(CpErr::CMD_ERROR, "命令跨越队列末尾(驱动应当用 NOP 填充, §2.1)");
            break;
        }
        {   // CP 只处理 [HEAD, TAIL) 区间(§2.1): 命令本体不得越过 TAIL
            const uint32_t after = head_ + ci.len * 4u;
            const bool past_tail = tail_ >= head_ ? (after > tail_) : (after > queue_base_ + queue_size_ - 1 + 1);
            if (past_tail && !(tail_ < head_ && after - queue_size_ <= tail_)) {
                char e[112];
                std::snprintf(e, sizeof(e), "命令(0x%08x 起 %u 字)越过 CMD_TAIL=0x%08x(§2.1)", head_, ci.len, tail_);
                set_err(CpErr::CMD_ERROR, e);
                break;
            }
        }
        if (ci.flags & ~0x3u) { set_err(CpErr::CMD_ERROR, "cmd_flags 的 bit2..7 必须为 0"); break; }
        if (ci.signal_id != SEM_NONE && ci.signal_id >= 0x100u) {
            char e[96];
            std::snprintf(e, sizeof(e), "signal_id=%u 落在保留区 0x0100..0xFFFE(CMDS §10.2)", ci.signal_id);
            set_err(CpErr::CMD_ERROR, e);
            break;
        }

        std::vector<uint32_t> words(ci.len);
        for (uint16_t i = 0; i < ci.len; ++i) words[i] = qread(head_ + i * 4u);

        ++n;
        // §2.1 的 "队列为空": 正在执行的这条命令之后队列就空了(它自己不算待处理工作)
        exec_is_last_ = ((head_ + ci.len * 4u - queue_base_) % queue_size_) + queue_base_ == tail_;

        if (ci.opcode != CMD_LAUNCH && ci.opcode != CMD_GEMM && !check_wait(ci.wait_id, ci)) {
            history_.push_back(ci); break;         // LAUNCH/GEMM 的依赖检查在各自前置条件之后做
        }
        const bool ok = exec_cmd(ci, words.data());
        history_.push_back(ci);
        // §9 的状态机: HEAD 在执行判定结束(UPDATE 相)之后推进。失败时 CP 进错误态,
        // 后续 process() 直接返回 ⇒ 失败的命令不会被重复执行。
        head_ += ci.len * 4u;
        if (head_ >= queue_base_ + queue_size_) head_ -= queue_size_;
        if (ok && (ci.flags & 0x2u)) set_irq(IRQ_CMD_DONE);
        ++cmds_done_;
        if (ci.signal_id != SEM_NONE && ok && ci.opcode != CMD_LAUNCH && ci.opcode != CMD_GEMM)
            sem_add(ci.signal_id);                   // LAUNCH/GEMM 在完成判定里打信号量(§4.3/§6.11)
        if (!ok) break;
    }
    (void)err;
    return n;
}

bool CommandProcessor::check_wait(uint16_t wait_id, const CmdInfo& ci) {
    if (wait_id == SEM_NONE) return true;
    if (wait_id >= 0x100) {
        set_err(CpErr::CMD_ERROR, "wait_id " + std::to_string(wait_id) + " 落在保留区 0x0100..0xFFFE(§10.2)");
        return false;
    }
    // §10.2 没有给 wait_id 显式的比较值, 按"该信号量至少完成过一次"解释
    if (sem_read(wait_id) == 0) {
        set_err(CpErr::TIMEOUT, "命令 0x" + std::to_string(ci.addr) + " 的 wait_id=" + std::to_string(wait_id) +
                                " 未满足(SEM=0): 本模拟器的命令顺序执行, 没有别的执行体能在等待期间推进它");
        return false;
    }
    LogD("wait_id=%u 满足(SEM=%u)", wait_id, sem_read(wait_id));
    return true;
}

void CommandProcessor::signal_done(uint16_t signal_id, uint32_t cmd_addr) {
    if (signal_id == SEM_NONE) {
        LogD("命令 0x%08x 完成(signal_id=0xFFFF, 不打信号量)", cmd_addr);
        return;
    }
    sem_add(signal_id);
}

bool CommandProcessor::exec_cmd(CmdInfo& ci, const uint32_t* w) {
    switch (ci.opcode) {
    case CMD_NOP:
        ci.summary = "NOP(" + std::to_string(ci.len) + " 字)";
        return true;

    case CMD_WRITE_REG: {
        if (ci.len != 4) return set_err(CpErr::CMD_ERROR, "WRITE_REG 长度必须为 4");
        const uint32_t addr = w[2], value = w[3];
        if (addr & 3u)                 return set_err(CpErr::CMD_ERROR, "WRITE_REG 地址未 4B 对齐");
        if (addr >= MMIO_END)          return set_err(CpErr::CMD_ERROR, "WRITE_REG 地址不在 MMIO 窗口(§6.2)");
        if (!writable_reg(addr)) {
            char e[96];
            std::snprintf(e, sizeof(e), "WRITE_REG 目标是只读寄存器 0x%02x(CMDS §7)", addr);
            return set_err(CpErr::CMD_ERROR, e);
        }
        // §2.1: 只允许在 CP IDLE 且"队列为空"时改 CMD_QUEUE_*。这里的"队列为空"按
        // "本命令是最后一条"理解 —— 正在执行的这条命令本身不算待处理工作。
        if ((addr == MMIO_CMD_QUEUE_BASE || addr == MMIO_CMD_QUEUE_SIZE) &&
            (machine_.active() || !exec_is_last_))
            return set_err(CpErr::ILLEGAL_STATE, "只允许在 CP IDLE 且队列为空时改 CMD_QUEUE_*(§2.1)");
        // 取值校验: 0 或非 8 的倍数会让回绕填充/取模出问题(§2.1 + §6.1 的 NOP ≥ 2 字)
        if (addr == MMIO_CMD_QUEUE_SIZE && (value < 8 || (value & 7u)))
            return set_err(CpErr::CMD_ERROR, "CMD_QUEUE_SIZE 必须是 ≥ 8 且为 8 的倍数(§2.1/§6.1)");
        if (addr == MMIO_CMD_QUEUE_BASE && (value < MMIO_END || (value & 3u)))
            return set_err(CpErr::CMD_ERROR, "CMD_QUEUE_BASE 必须 4B 对齐且不在 MMIO 窗口");
        char b[96];
        std::snprintf(b, sizeof(b), "WRITE_REG 0x%02x ← 0x%08x", addr, value);
        ci.summary = b;
        machine_.mmio_write(addr, value);
        return true;
    }

    case CMD_READ_REG: {
        if (ci.len != 4) return set_err(CpErr::CMD_ERROR, "READ_REG 长度必须为 4");
        const uint32_t addr = w[2], dst = w[3];
        if (addr & 3u)        return set_err(CpErr::CMD_ERROR, "READ_REG 的 addr 未 4B 对齐");
        if (addr >= MMIO_END) return set_err(CpErr::CMD_ERROR, "READ_REG 的 addr 不在 MMIO 窗口");
        if ((dst & 3u) || dst < MMIO_END)
            return set_err(CpErr::CMD_ERROR, "READ_REG 的 dst_addr 必须 4B 对齐且不在 MMIO 窗口");
        const uint32_t v = machine_.mmio_read(addr);
        qwrite(dst, v);
        char b[128];
        std::snprintf(b, sizeof(b), "READ_REG 0x%02x(=0x%08x) → 0x%08x", addr, v, dst);
        ci.summary = b;
        return true;
    }

    case CMD_LOAD_KERNEL: {
        if (ci.len != 5) return set_err(CpErr::CMD_ERROR, "LOAD_KERNEL 长度必须为 5");
        char b[160];
        std::snprintf(b, sizeof(b), "LOAD_KERNEL src=0x%08x size=%u sm_mask=0x%x", w[2], w[3], w[4] & 3u);
        ci.summary = b;
        return exec_load_kernel(w);
    }

    case CMD_LAUNCH: {
        if (ci.len != 5) return set_err(CpErr::CMD_ERROR, "LAUNCH 长度必须为 5(§4)");
        return exec_launch(w, ci);
    }

    case CMD_FENCE:
        if (ci.len != 2) return set_err(CpErr::CMD_ERROR, "FENCE 长度必须为 2");
        ci.summary = "FENCE(写直达内存模型, 无需冲刷)";
        LogD("FENCE: 本模拟器的全局写是直达的, 没有写缓冲需要冲刷(§10.3)");
        return true;

    case CMD_SIGNAL: {
        if (ci.len != 4) return set_err(CpErr::CMD_ERROR, "SIGNAL 长度必须为 4");
        if ((w[2] & 3u) || w[2] < MMIO_END) return set_err(CpErr::CMD_ERROR, "SIGNAL 的 sem_addr 非法");
        qwrite(w[2], w[3]);
        char b[128];
        std::snprintf(b, sizeof(b), "SIGNAL [0x%08x] ← 0x%08x", w[2], w[3]);
        ci.summary = b;
        return true;
    }

    case CMD_WAIT: {
        if (ci.len != 4) return set_err(CpErr::CMD_ERROR, "WAIT 长度必须为 4");
        if (w[2] & 3u)   return set_err(CpErr::CMD_ERROR, "WAIT 的 sem_addr 未 4B 对齐");
        const uint32_t cur = qread(w[2]);
        char b[128];
        std::snprintf(b, sizeof(b), "WAIT [0x%08x] >= %u (当前 %u)", w[2], w[3], cur);
        ci.summary = b;
        if (cur < w[3])
            return set_err(CpErr::TIMEOUT, "WAIT 条件不满足且本模拟器没有并发的推进者(§6.7)");
        return true;
    }

    case CMD_IRQ: {
        if (ci.len != 3)      return set_err(CpErr::CMD_ERROR, "IRQ 长度必须为 3");
        if (w[2] & ~0xFFu)    return set_err(CpErr::CMD_ERROR, "IRQ 的 reserved[31:8] 必须为 0");
        if ((w[2] & 0xFFu) > 7u) return set_err(CpErr::CMD_ERROR, "irq_id ≤ 7(§6.8)");
        char b[96];
        std::snprintf(b, sizeof(b), "IRQ id=%u", w[2] & 0xFFu);
        ci.summary = b;
        set_irq(1u << (w[2] & 0xFFu));
        return true;
    }

    case CMD_PARAMS: {
        if (ci.len < 5)                        return set_err(CpErr::CMD_ERROR, "PARAMS 至少 5 字");
        if (w[2] & 0xFFFF0000u)                return set_err(CpErr::CMD_ERROR, "PARAMS word2 的 reserved[31:16] 必须为 0");
        if (w[3] & 0xFFFF0000u)                return set_err(CpErr::CMD_ERROR, "PARAMS word3 的 reserved[31:16] 必须为 0");
        const uint32_t off = w[2] & 0xFFFFu, n = w[3] & 0xFFFFu;
        if (off & 3u || off >= CONST_SIZE)     return set_err(CpErr::CMD_ERROR, "PARAMS 的 dst_off 必须 4B 对齐且 < 4096");
        if (n < 1 || n > 256)                  return set_err(CpErr::CMD_ERROR, "PARAMS 的 N 必须在 1..256");
        if (ci.len != 4 + n)                   return set_err(CpErr::CMD_ERROR, "PARAMS 的 length_words 必须等于 4+N");
        if (off + n * 4u > CONST_SIZE) {
            char e[128];
            std::snprintf(e, sizeof(e), "PARAMS 的 dst_off=0x%x + %u 字越出 4KB 常量区(§6.10/§2.4)", off, n);
            return set_err(CpErr::CMD_ERROR, e);
        }
        for (uint32_t i = 0; i < n; ++i) machine_.mem.st_const(off + i * 4u, w[4 + i]);
        LogD("PARAMS: %u 个参数写入常量区偏移 0x%x", n, off);
        char b[128];
        std::snprintf(b, sizeof(b), "PARAMS off=0x%03x N=%u", off, n);
        ci.summary = b;
        return true;
    }

    case CMD_GEMM:
        if (ci.len != 16) return set_err(CpErr::CMD_ERROR, "GEMM 长度必须为 16(§6.11)");
        return exec_gemm(w, ci);

    default:
        if (ci.opcode >= 0x10 && ci.opcode <= 0x17) {
            // 2D 命令(§6.9): CMDS 只规定"按 length_words 跳过并转交 2D 引擎"
            char b[96];
            std::snprintf(b, sizeof(b), "2D 命令 0x%02x(%u 字): 转交 2D 引擎(GFX.md, 本模拟器未实现)", ci.opcode, ci.len);
            ci.summary = b;
            LogW("2D 命令 0x%02x 被跳过: 2D 引擎不在本模拟器范围", ci.opcode);
            return true;
        }
        if (ci.opcode >= 0x80) {
            ci.summary = "厂商自定义 opcode 0x" + std::to_string(ci.opcode) + "(" +
                         std::to_string(ci.len) + " 字): 按长度跳过(§3)";
            LogW("厂商自定义命令 0x%02x 被跳过(CP 只按 length_words 跳过)", ci.opcode);
            return true;
        }
        return set_err(CpErr::CMD_ERROR, "未定义/保留 opcode 0x" + std::to_string(ci.opcode));
    }
}

bool CommandProcessor::exec_load_kernel(const uint32_t* w) {
    if (machine_.active())
        return set_err(CpErr::ILLEGAL_STATE, "CP BUSY(内核正在跑)时不能 LOAD_KERNEL(§6.4)");
    const uint32_t src = w[2], size = w[3], sm_mask = w[4] & 3u;
    if (w[4] & ~3u)  return set_err(CpErr::CMD_ERROR, "LOAD_KERNEL 的 reserved[31:2] 必须为 0(§6.4)");
    if (!sm_mask)    return set_err(CpErr::CMD_ERROR, "sm_mask 不能为 0");
    if (src & 3u)    return set_err(CpErr::CMD_ERROR, "src_addr 未 4B 对齐");
    if (size == 0 || (size & 3u) || size > uint32_t(INSTR_SRAM_SIZE))
        return set_err(CpErr::CMD_ERROR, "size_bytes 必须是 4 的倍数且 ≤ 16384(§6.4)");

    std::vector<uint8_t> buf(size);
    for (uint32_t i = 0; i < size / 4; ++i) {
        const uint32_t v = qread(src + i * 4u);
        buf[i * 4 + 0] = uint8_t(v);
        buf[i * 4 + 1] = uint8_t(v >> 8);
        buf[i * 4 + 2] = uint8_t(v >> 16);
        buf[i * 4 + 3] = uint8_t(v >> 24);
    }
    std::string why;
    if (machine_.mem.load_img_bytes(buf.data(), buf.size(), uint8_t(sm_mask), &why) < 0)
        return set_err(CpErr::CMD_ERROR, "镜像头校验失败: " + why);
    LogI("LOAD_KERNEL 完成: 0x%08x +%u 字节 → sm_mask=0x%x, SM_KERNEL_MASK=0x%x (entry=0x%x flags=0x%x crc=0x%08x)",
         src, size, sm_mask, machine_.mem.sm_kernel_mask(), machine_.mem.img[0].entry,
         machine_.mem.img[0].flags, machine_.mem.img[0].crc);
    return true;
}

bool CommandProcessor::exec_launch(const uint32_t* w, CmdInfo& ci) {
    if (w[3] & 0xFFFFFF00u) return set_err(CpErr::CMD_ERROR, "LAUNCH 的 word3 reserved[31:8] 必须为 0(§4)");
    const uint32_t kernel_pc = w[2], num_warps = w[3] & 0xFFu, flags = w[4];

    std::string why;
    uint32_t code = 0;
    if (!machine_.launch_precheck(kernel_pc, num_warps, flags, &why, &code)) {    
        const CpErr e = code == 2 ? CpErr::LAUNCH_REJECT : code == 5 ? CpErr::ILLEGAL_STATE
                      : code == 6 ? CpErr::STALE_SM_ERROR : CpErr::CMD_ERROR;
        return set_err(e, why);
    }
    if (!check_wait(ci.wait_id, ci)) return false;                                
    machine_.launch_commit(kernel_pc, num_warps, flags);
    pending_signal_ = ci.signal_id;
    pending_addr_   = ci.addr;
    launch_pending_ = true;

    char b[256];
    std::snprintf(b, sizeof(b), "LAUNCH kernel_pc=0x%03x num_warps=%u(= %u 线程) flags=0x%x%s",
                  kernel_pc, num_warps, num_warps * WARP_SIZE, flags,
                  (flags & 1) ? " USES_BARRIER" : "");
    ci.summary = b;

    LogI("LAUNCH 分发: kernel_pc=0x%03x(= 镜像 entry 0x%x), %u 个 warp × %d lane = %d 线程",
         kernel_pc, machine_.mem.img[0].entry, num_warps, WARP_SIZE, num_warps * WARP_SIZE);
    LogI("  launch_flags=0x%x(%s%s%s%s), 与镜像 image_flags 交叉核对通过",
         flags,
         (flags & 1) ? "USES_BARRIER " : "", (flags & 2) ? "USES_SHARED " : "",
         (flags & 4) ? "USES_GLOBAL_WRITE " : "", (flags & 8) ? "REQUIRES_SINGLE_SM" : "");
    for (uint32_t i = 0; i < num_warps; ++i)
        LogI("  warp %u ⇒ SM%d 槽 %d (槽号 = wid mod %d)", i, machine_.warp_sm(int(i)),
             machine_.warp_slot(int(i)), WARPS_PER_SM);
    if (launch_hook_) launch_hook_();
    return true;
}

bool CommandProcessor::finish_launch(std::string* err) {
    if (!launch_pending_) return true;
    return complete_launch(err);
}

bool CommandProcessor::complete_launch(std::string* err) {
    launch_pending_ = false;
    if (!machine_.launched()) return true;

    const StopReason st = machine_.run(timeout_cycles_);
    const RunStats& s = machine_.stats;
    LogI("LAUNCH 完成判定: %s; %llu 周期, %llu 条指令(%llu warp-指令), IPC(SM)≈%.2f",
         st.name(), (unsigned long long)s.cycles, (unsigned long long)s.issued,
         (unsigned long long)s.issued, s.cycles ? double(s.issued) / double(s.cycles) / NUM_SM : 0.0);

    switch (st.kind) {
    case StopReason::ALL_EXIT:
        break;
    case StopReason::SM_ERROR:
        sm_fault_ = true;
        set_err(CpErr::SM_FAULT, "kernel 因 " + st.detail + " 失败; 不写信号量(§4.3)");
        return false;
    case StopReason::TIMEOUT:
    case StopReason::DEADLOCK:
        set_err(CpErr::TIMEOUT, st.detail);
        return false;
    default:
        set_err(CpErr::ILLEGAL_STATE, std::string("kernel 停在异常状态: ") + st.name());
        return false;
    }

    // 完成条件: 所有涉及的 SM 的 SM_DONE=1 且无错误(§4.3)
    for (int t = 0; t < NUM_SM; ++t) {
        if (machine_.sm[t].resident == 0) continue;
        if (!machine_.sm[t].done()) {
            set_err(CpErr::TIMEOUT, "SM" + std::to_string(t) + " 的 warp_alive 位图未清零");
            return false;
        }
    }
    signal_done(uint16_t(pending_signal_), pending_addr_);
    set_irq(IRQ_LAUNCH_DONE);
    LogI("LAUNCH 完成: SM_DONE=0x%x, 无错误 ⇒ 释放依赖该 kernel 的后续命令",
         machine_.mmio_read(MMIO_SM_DONE));
    machine_.set_stop(StopReason::ALL_EXIT);
    if (err) err->clear();
    return true;
}

// ─────────────────────────── 矩阵引擎 v1 (§6.11) ───────────────────────────

static int32_t ld_int8(Memory& mem, uint32_t base, uint32_t byte_off) {
    const uint32_t addr = base + (byte_off & ~3u);
    const uint32_t word = mem.ld_global(addr);
    return int32_t(int8_t((word >> ((byte_off & 3u) * 8)) & 0xFFu));
}

bool CommandProcessor::exec_gemm(const uint32_t* w, CmdInfo& ci) {
    const uint32_t A = w[2], B = w[3], C = w[4];
    const uint32_t M = w[5] & 0xFFFFu, N = w[5] >> 16;
    const uint32_t K = w[6] & 0xFFFFu;
    const uint32_t lda = w[7] & 0xFFFFu, ldb = w[7] >> 16;
    const uint32_t ldc = w[8] & 0xFFFFu;
    const uint32_t dtype = w[9] & 0xFFu, gflags = (w[9] >> 8) & 0xFFu;
    const uint32_t zp_a = w[10] & 0xFFu, zp_b = (w[10] >> 8) & 0xFFu;

    char b[256];
    std::snprintf(b, sizeof(b), "GEMM A=0x%08x B=0x%08x C=0x%08x M=%u N=%u K=%u flags=0x%x",
                  A, B, C, M, N, K, gflags);
    ci.summary = b;

    if ((A | B | C) & 0xFu)                 return set_err(CpErr::CMD_ERROR, "GEMM 基址必须 16B 对齐(§2.4)");
    if (!M || M > 4096 || !N || N > 4096 || !K || K > 4096)
        return set_err(CpErr::CMD_ERROR, "GEMM 要求 1 ≤ M,N,K ≤ 4096");
    if ((w[10] >> 16) || (w[12] >> 8) || w[13] || w[14] || w[15])
        return set_err(CpErr::CMD_ERROR, "GEMM 描述符的保留字段必须为 0(§2.4)");
    if (dtype != 0)
        return set_err(CpErr::CMD_ERROR, "GEMM 的 dtype=0x" + std::to_string(dtype) +
                                         " 未实现; v1 只实现 0=INT8(A/B) → INT32(C)(§6.11)");
    if (gflags & 0x4u)
        return set_err(CpErr::CMD_ERROR, "GEMM 的 REQUANT 属 Phase 5, 未实现(§6.11)");
    if (gflags & ~0x7u) return set_err(CpErr::CMD_ERROR, "GEMM flags 的保留位非 0");

    const bool accum = gflags & 0x1u, bT = gflags & 0x2u;
    if (lda && lda < K)                    return set_err(CpErr::CMD_ERROR, "GEMM 的 lda < K, 行会重叠");
    if (ldb && ldb < (bT ? K : N))         return set_err(CpErr::CMD_ERROR, "GEMM 的 ldb 小于 B 的行长");
    if (ldc && ldc < N)                    return set_err(CpErr::CMD_ERROR, "GEMM 的 ldc < N, 行会重叠");
    const uint32_t sa = lda ? lda : K;                 // A: M×K 行主序, 行跨距 K
    const uint32_t sb = ldb ? ldb : (bT ? K : N);      // B: K×N 行主序(转置时按 N×K 存)
    const uint32_t sc = ldc ? ldc : N;                 // C: M×N 行主序

    const double macs = double(M) * double(N) * double(K);
    if (macs > 1e8)
        LogW("GEMM %u×%u×%u = %.3g 次 MAC: 本模拟器的矩阵引擎是**同步标量参考实现**, "
             "这个规模会跑很久(引擎的时序/流水不在建模范围内)", M, N, K, macs);
    gemm_busy_ = true;
    gemm_done_ = false;                                  // 新命令开始 ⇒ 清 DONE(否则旧 DONE 会一直挂着)
    gemm_error_ = 0;
    for (uint32_t m = 0; m < M; ++m) {
        for (uint32_t n = 0; n < N; ++n) {
            int32_t acc = accum ? int32_t(machine_.mem.ld_global(C + (m * sc + n) * 4u)) : 0;
            for (uint32_t k = 0; k < K; ++k) {
                const int32_t a = ld_int8(machine_.mem, A, m * sa + k);
                const int32_t bv = bT ? ld_int8(machine_.mem, B, n * sb + k)
                                      : ld_int8(machine_.mem, B, k * sb + n);
                acc += (a - int32_t(zp_a)) * (bv - int32_t(zp_b));
            }
            machine_.mem.st_global(C + (m * sc + n) * 4u, uint32_t(acc));
        }
    }
    gemm_busy_ = false;
    gemm_done_ = true;
    gemm_ops_ += 2ull * M * N * K;
    ++gemm_cmds_;
    LogI("GEMM 完成: %u×%u×%u, %s, zp=(%u,%u), 累计 %.3f MOP",
         M, N, K, accum ? "C = A×B + C" : "C = A×B", zp_a, zp_b, double(gemm_ops_) / 1e6);
    set_irq(IRQ_GEMM_DONE);
    signal_done(ci.signal_id, ci.addr);
    return true;
}

// ─────────────────────────── MMIO 寄存器 ───────────────────────────

bool CommandProcessor::writable_reg(uint32_t off) const {
    switch (off) {
    case MMIO_CP_ERROR: case MMIO_CMD_TAIL: case MMIO_DOORBELL: case MMIO_IRQ_STATUS:
    case MMIO_IRQ_ENABLE: case MMIO_SEM_BASE: case MMIO_CMD_QUEUE_BASE: case MMIO_CMD_QUEUE_SIZE:
    case MMIO_FB_BASE: case MMIO_CP_RESET: case MMIO_TIMEOUT_CYCLES: case MMIO_GEMM_ERROR:
    // CMDS §7: SM*_ERROR 是 "R / 写任意值清零", CMD_HEAD 允许 host 写用于异常恢复(§7.3 的恢复流程要用)
    case MMIO_SM0_ERROR: case MMIO_SM1_ERROR: case MMIO_CMD_HEAD:
        return true;
    default:
        return false;
    }
}

uint32_t CommandProcessor::read_reg(uint32_t off) {
    switch (off) {
    case MMIO_CP_STATUS:       return status();
    case MMIO_CP_ERROR:        return uint32_t(err_);
    case MMIO_CMD_HEAD:        return head_;
    case MMIO_CMD_TAIL:        return tail_;
    case MMIO_DOORBELL:        return 0;
    case MMIO_IRQ_STATUS:      return irq_status_;
    case MMIO_IRQ_ENABLE:      return irq_enable_;
    case MMIO_SEM_BASE:        return sem_base_;
    case MMIO_CMD_QUEUE_BASE:  return queue_base_;
    case MMIO_CMD_QUEUE_SIZE:  return queue_size_;
    case MMIO_TIMEOUT_CYCLES:  return timeout_cycles_;
    case MMIO_CP_RESET:        return 0;
    case MMIO_GEMM_ERROR:      return gemm_error_;
    case MMIO_GEMM_STATUS:     return gemm_status();     // §6.11: BUSY/DONE/ERROR
    default:
        LogW("MMIO 读 0x%02x: 未定义的寄存器(§7 的保留区), 返回 0", off);
        return 0;
    }
}

void CommandProcessor::write_reg(uint32_t off, uint32_t v) {
    switch (off) {
    case MMIO_CP_ERROR:
        if (v) LogI("host 清除 CP_ERROR(原值 %s: %s)", cp_err_name(err_), err_detail_.c_str());
        err_ = CpErr::NO_ERROR;
        err_detail_.clear();
        sm_fault_ = false;
        return;
    case MMIO_CMD_TAIL:
        tail_ = v;
        LogD("CMD_TAIL ← 0x%08x", v);
        return;
    case MMIO_IRQ_STATUS:
        LogD("IRQ_STATUS 清零(原值 0x%x)", irq_status_);
        irq_status_ = 0;
        return;
    case MMIO_IRQ_ENABLE:
        irq_enable_ = v;
        LogD("IRQ_ENABLE ← 0x%x", v);
        return;
    case MMIO_SEM_BASE:
        sem_base_ = v;
        LogI("SEM_BASE ← 0x%08x", v);
        return;
    case MMIO_CMD_QUEUE_BASE:
        if (v < MMIO_END || (v & 3u)) { LogW("CMD_QUEUE_BASE ← 0x%08x 被拒绝(必须 4B 对齐且不在 MMIO 窗口)", v); return; }
        LogI("CMD_QUEUE_BASE ← 0x%08x", v);
        queue_base_ = v;
        head_ = tail_ = v;
        return;
    case MMIO_CMD_HEAD:
        LogI("CMD_HEAD ← 0x%08x(host 异常恢复, CMDS §7)", v);
        head_ = v >= queue_base_ && v < queue_base_ + queue_size_ ? v : tail_;
        return;
    case MMIO_CMD_QUEUE_SIZE:
        // §2.1 要求是 4 的倍数, 但 §6.1 规定 NOP ≥ 2 字 ⇒ 回绕填充需要 8 的倍数;
        // 0 会让 (head_ - tail_ + queue_size_) % queue_size_ 除零 ⇒ 必须挡住
        if (v < 8 || (v & 7u)) {
            LogW("CMD_QUEUE_SIZE ← %u 被拒绝: 必须是 ≥ 8 且为 8 的倍数(§2.1 + §6.1 的 NOP 填充), 保持 %u",
                 v, queue_size_);
            return;
        }
        LogI("CMD_QUEUE_SIZE ← %u 字节", v);
        queue_size_ = v;
        head_ = tail_ = queue_base_;
        return;
    case MMIO_TIMEOUT_CYCLES:
        timeout_cycles_ = v ? v : 0x100000;
        LogI("TIMEOUT_CYCLES ← %u", timeout_cycles_);
        return;
    case MMIO_GEMM_ERROR:
        LogI("host 清除 GEMM_ERROR(原值 0x%x)", gemm_error_);
        gemm_error_ = 0;
        return;
    default:
        // FB_BASE / CP_RESET / DOORBELL 在 Machine::mmio_write 里就处理掉了, 不会走到这里
        LogW("MMIO 0x%02x 是只读/未定义寄存器, 写 0x%x 被忽略(CMDS §7)", off, v);
        return;
    }
}

void CommandProcessor::reset() {
    LogI("CP_RESET: 状态机回 IDLE, 清错误/中断, HEAD←TAIL, 镜像失效, 引擎复位(§7.3)");
    err_ = CpErr::NO_ERROR;
    err_detail_.clear();
    sm_fault_ = false;
    sm_fault_id_ = -1;
    head_ = tail_;                       // 丢弃未处理命令(§7.3 第 2 条)
    irq_status_ = 0;
    launch_pending_ = false;
    pending_signal_ = SEM_NONE;
    gemm_busy_ = false;
    gemm_done_ = false;
    gemm_error_ = 0;
    machine_.reset();                    // 清 SM 错误/FAULT_PC/共享/指令 SRAM, KERNEL_MASK 归零
}

void CommandProcessor::on_sm_error(int smid) {
    sm_fault_ = true;
    sm_fault_id_ = smid;
    LogE("CP 判定 kernel 失败: SM%d 处于错误态 ⇒ 不释放信号量、不执行依赖它的命令(§4.3/ISA §2.5)", smid);
    LogW("CP_ERROR 置 SM_FAULT(=7, CMDS §8.1): CP 只知道「这个 kernel 失败了」; "
         "具体原因与现场在 SM%d_ERROR / SM%d_FAULT_PC(§8.3)", smid, smid);
}

}  // namespace simu
