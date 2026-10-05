#pragma once

#include "config.hpp"

#include <cstdint>
#include <string>
#include <unordered_map>
#include <vector>

namespace simu {

constexpr uint32_t IMG_MAGIC       = 0x53474B31;             // "SGK1"
constexpr uint32_t IMG_HDR_BYTES   = 32;         
constexpr uint32_t RESET_VECTOR    = 0x0;                    // 指令 SRAM 内的 PC 起点
constexpr uint32_t KERNEL_IMG_BASE = 0x0200'1000;            // 全局内存里的内核镜像区
constexpr uint32_t CONST_SIZE      = 0x1000;                 // 常量区 4KB
constexpr uint32_t MMIO_END        = 0x0000'1000;            
constexpr int      INSTR_WORDS     = INSTR_SRAM_SIZE / 4;    // 4096 条 = 16KB

enum class MemErr { NONE, ALIGN, OOB, NO_SM };

// 每个 SM 当前常驻的镜像(CP 的 SM_KERNEL_MASK 与 LAUNCH 交叉核对都读它)
struct KernelImage {
    bool     loaded     = false;
    uint32_t entry      = 0;        // = 头里的 entry_off: 镜像整体放在 SRAM 偏移 0, PC 从 entry 起
    uint32_t image_size = 0;
    uint32_t code_size  = 0;
    uint32_t flags      = 0;        // image_flags, LAUNCH 时必须与 launch_flags 逐位一致
    uint32_t crc        = 0;
};

uint32_t crc32_iso_hdlc(const uint8_t* data, size_t n);      // 与 zlib.crc32 同值

}  // namespace simu

struct Memory {
    // 全局内存: 稀疏字寻址。32 位地址空间里实际只用到几段(常量/镜像/帧缓冲/驱动缓冲),
    // 用 vector 铺满要么铺不下(DDR 在 0x8000_0000),要么白占几百 MB。
    std::unordered_map<uint32_t, uint32_t> global;
    uint32_t shared[simu::NUM_SM][simu::SHARED_SIZE / 4];
    std::vector<uint32_t> instr_sram[simu::NUM_SM];          // 每 SM 4096 条
    simu::KernelImage img[simu::NUM_SM];

    Memory();

    uint32_t* guest_to_host(uint32_t paddr);                 // 全局字地址 → 宿主指针(4B 对齐)

    uint32_t ld_global(uint32_t addr, simu::MemErr* err = nullptr);
    void     st_global(uint32_t addr, uint32_t data, simu::MemErr* err = nullptr);
    uint32_t ld_shared(int sm, uint32_t addr, simu::MemErr* err = nullptr);
    void     st_shared(int sm, uint32_t addr, uint32_t data, simu::MemErr* err = nullptr);
    bool     fetch(int sm, uint32_t pc, uint32_t& inst, simu::MemErr* err = nullptr) const;

    // 把 bin 镜像读进指令 SRAM: 校验镜像头后按 sm_mask 写入(成功返回字节数, 失败 -1 并填 *err)
    long load_img(const char* img_file, uint8_t sm_mask = 0x3, std::string* err = nullptr);
    long load_img_bytes(const uint8_t* data, size_t nbytes, uint8_t sm_mask = 0x3,
                        std::string* err = nullptr);
    long load_file_to_global(const char* path, uint32_t dst, std::string* err = nullptr);

    uint32_t ld_const(uint32_t off);                          // 常量区
    void     st_const(uint32_t off, uint32_t v);
    uint8_t  sm_kernel_mask() const;                          // CMDS.md §7 SM_KERNEL_MASK
    void     clear_instr(int sm);                             // 镜像作废 + SRAM 归零
    void     reset();                                         // 清共享/指令 SRAM/镜像, 保留全局内存
};

long load_img(Memory& mem, const char* img_file, uint8_t sm_mask = 0x3);
