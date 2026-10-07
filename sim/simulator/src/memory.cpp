#include "memory.hpp"
#include "debug.hpp"

#include <cstdio>
#include <cstring>

namespace simu {

static uint32_t rd32le(const uint8_t* p) {
    return uint32_t(p[0]) | uint32_t(p[1]) << 8 | uint32_t(p[2]) << 16 | uint32_t(p[3]) << 24;
}

// CRC-32/ISO-HDLC: poly 0xEDB88320, init/final 取反 —— 与 zlib.crc32(汇编器用)一致
// 用于镜像头校验, 也可用于全局内存/帧缓冲/驱动缓冲的内容校验
uint32_t crc32_iso_hdlc(const uint8_t* data, size_t n) {
    static uint32_t table[256];
    static bool ready = false;
    if (!ready) {
        for (uint32_t i = 0; i < 256; ++i) {
            uint32_t c = i;
            for (int k = 0; k < 8; ++k) c = (c & 1) ? (0xEDB88320u ^ (c >> 1)) : (c >> 1);
            table[i] = c;
        }
        ready = true;
    }
    uint32_t crc = 0xFFFFFFFFu;
    for (size_t i = 0; i < n; ++i) crc = table[(crc ^ data[i]) & 0xFF] ^ (crc >> 8);
    return crc ^ 0xFFFFFFFFu;
}

}  // namespace simu

Memory::Memory() {
    for (int sm = 0; sm < simu::NUM_SM; ++sm) {
        instr_sram[sm].assign(simu::INSTR_WORDS, 0);
        img[sm] = simu::KernelImage{};
    }
    std::memset(shared, 0, sizeof(shared));
    global.reserve(4096);
}

uint32_t* Memory::guest_to_host(uint32_t paddr) {
    if ((paddr & 3u) || paddr < simu::MMIO_END) return nullptr;
    return &global[paddr >> 2];              // 未写过的字自动建成 0
}

uint32_t Memory::ld_global(uint32_t addr, simu::MemErr* err) {
    if (err) *err = simu::MemErr::NONE;
    if (addr & 3u)             { if (err) *err = simu::MemErr::ALIGN; return 0; }
    if (addr < simu::MMIO_END) { if (err) *err = simu::MemErr::OOB;   return 0; }  // MMIO 不是内存
    auto it = global.find(addr >> 2);
    return it == global.end() ? 0 : it->second;
}

void Memory::st_global(uint32_t addr, uint32_t data, simu::MemErr* err) {
    if (err) *err = simu::MemErr::NONE;
    if (addr & 3u)             { if (err) *err = simu::MemErr::ALIGN; return; }
    if (addr < simu::MMIO_END) { if (err) *err = simu::MemErr::OOB;   return; }
    global[addr >> 2] = data;
}

uint32_t Memory::ld_shared(int sm, uint32_t addr, simu::MemErr* err) {
    if (err) *err = simu::MemErr::NONE;
    if (sm < 0 || sm >= simu::NUM_SM) { if (err) *err = simu::MemErr::NO_SM; return 0; }
    if (addr & 3u)                    { if (err) *err = simu::MemErr::ALIGN; return 0; }
    if (addr >= simu::SHARED_SIZE)    { if (err) *err = simu::MemErr::OOB;   return 0; }
    return shared[sm][addr >> 2];
}

void Memory::st_shared(int sm, uint32_t addr, uint32_t data, simu::MemErr* err) {
    if (err) *err = simu::MemErr::NONE;
    if (sm < 0 || sm >= simu::NUM_SM) { if (err) *err = simu::MemErr::NO_SM; return; }
    if (addr & 3u)                    { if (err) *err = simu::MemErr::ALIGN; return; }
    if (addr >= simu::SHARED_SIZE)    { if (err) *err = simu::MemErr::OOB;   return; }
    shared[sm][addr >> 2] = data;
}

bool Memory::fetch(int sm, uint32_t pc, uint32_t& inst, simu::MemErr* err) const {
    if (err) *err = simu::MemErr::NONE;
    if (sm < 0 || sm >= simu::NUM_SM) { if (err) *err = simu::MemErr::NO_SM; return false; }
    if (pc & 3u)                      { if (err) *err = simu::MemErr::ALIGN; return false; }
    if (pc >= simu::INSTR_SRAM_SIZE)  { if (err) *err = simu::MemErr::OOB;   return false; }
    inst = instr_sram[sm][pc >> 2];
    return true;
}

long Memory::load_img_bytes(const uint8_t* data, size_t nbytes, uint8_t sm_mask,
                            std::string* err) {
    auto fail = [&](const char* why) { if (err) *err = why; return -1L; };

    if (!data)                         return fail("镜像数据为空");
    if (!sm_mask || (sm_mask & ~0x3u)) return fail("sm_mask 只能取 bit0/bit1 且非 0");
    if (nbytes < simu::IMG_HDR_BYTES)  return fail("镜像小于 32 字节的镜像头");
    if (nbytes & 3u)                   return fail("镜像长度不是 4 的倍数");

    const uint32_t magic = simu::rd32le(data + 0);
    const uint32_t entry = simu::rd32le(data + 4);
    const uint32_t size  = simu::rd32le(data + 8);
    const uint32_t flags = simu::rd32le(data + 12);
    const uint32_t crc   = simu::rd32le(data + 16);
    const uint32_t csize = simu::rd32le(data + 20);

    if (magic != simu::IMG_MAGIC)     return fail("镜像头 magic 不是 SGK1");
    if (simu::rd32le(data + 24) || simu::rd32le(data + 28))
                                      return fail("镜像头保留字必须为 0");
    if (size != nbytes)               return fail("image_size 与文件长度不一致");
    if (size > simu::INSTR_SRAM_SIZE) return fail("镜像超过指令 SRAM 的 16KB");
    if (entry & 3u)                   return fail("entry_off 未 4B 对齐");
    if (entry < simu::IMG_HDR_BYTES || entry > size)
                                      return fail("entry_off 超出镜像范围");
    if (csize != size - entry)        return fail("code_size 与 image_size - entry_off 不符");
    if (simu::crc32_iso_hdlc(data + entry, csize) != crc)
                                      return fail("指令区 CRC-32 不符");
    if (flags & ~0xFu)                return fail("image_flags 的 bit4..bit31 必须为 0");

    for (int sm = 0; sm < simu::NUM_SM; ++sm) {
        if (!(sm_mask >> sm & 1)) continue;
        clear_instr(sm);                       // 先归零: 越出镜像的取指必然取到 0x00000000(非法指令)
        for (size_t i = 0; i < size / 4; ++i)  // 整个镜像(头+代码)放在 SRAM 偏移 0
            instr_sram[sm][i] = simu::rd32le(data + i * 4);
        img[sm] = simu::KernelImage{true, entry, size, csize, flags, crc};
    }
    if (err) err->clear();
    return long(size);
}

long Memory::load_img(const char* img_file, uint8_t sm_mask, std::string* err) {
    if (!img_file) { if (err) *err = "镜像文件名为空"; return -1L; }
    FILE* fp = std::fopen(img_file, "rb");
    if (!fp) { if (err) *err = std::string("打不开镜像文件 ") + img_file; return -1L; }

    std::fseek(fp, 0, SEEK_END);
    long size = std::ftell(fp);
    std::fseek(fp, 0, SEEK_SET);
    if (size <= 0) { std::fclose(fp); if (err) *err = "镜像文件为空"; return -1L; }

    const size_t nbytes = size_t(size);
    std::vector<uint8_t> buf(nbytes);
    size_t got = std::fread(buf.data(), 1, nbytes, fp);
    std::fclose(fp);
    if (got != nbytes) { if (err) *err = "读取镜像文件不完整"; return -1L; }

    long ret = load_img_bytes(buf.data(), buf.size(), sm_mask, err);
    if (ret > 0)
        Log("load_img: %s, size=%ld, entry=0x%x, flags=0x%x, sm_mask=0x%x",
            img_file, ret, img[0].entry, img[0].flags, sm_mask);
    return ret;
}

long Memory::load_file_to_global(const char* path, uint32_t dst, std::string* err) {
    auto fail = [&](const char* why) { if (err) *err = why; return -1L; };
    if (!path)                                return fail("文件名为空");
    if ((dst & 3u) || dst < simu::MMIO_END)   return fail("目标地址必须 4B 对齐且不在 MMIO 窗口");

    FILE* fp = std::fopen(path, "rb");
    if (!fp) { if (err) *err = std::string("打不开文件 ") + path; return -1L; }
    std::fseek(fp, 0, SEEK_END);
    long size = std::ftell(fp);
    std::fseek(fp, 0, SEEK_SET);
    if (size < 0) { std::fclose(fp); return fail("取文件长度失败"); }

    std::vector<uint8_t> buf(size_t(size) + 3, 0);     // 补零, 允许长度不是 4 的倍数
    size_t got = std::fread(buf.data(), 1, size_t(size), fp);
    std::fclose(fp);
    if (got != size_t(size)) return fail("读取文件不完整");

    for (long i = 0; i < size; i += 4)
        global[(dst + uint32_t(i)) >> 2] = simu::rd32le(buf.data() + i);
    if (err) err->clear();
    return size;
}

uint32_t Memory::ld_const(uint32_t off) {
    Assert(off < simu::CONST_SIZE, "常量区偏移越界");
    return ld_global(simu::CONST_BASE + off);
}

void Memory::st_const(uint32_t off, uint32_t v) {
    Assert(off < simu::CONST_SIZE, "常量区偏移越界");
    st_global(simu::CONST_BASE + off, v);
}

uint8_t Memory::sm_kernel_mask() const {
    uint8_t m = 0;
    for (int sm = 0; sm < simu::NUM_SM; ++sm)
        if (img[sm].loaded) m |= uint8_t(1u << sm);
    return m;
}

void Memory::clear_instr(int sm) {
    if (sm < 0 || sm >= simu::NUM_SM) return;
    instr_sram[sm].assign(simu::INSTR_WORDS, 0);
    img[sm] = simu::KernelImage{};
}

void Memory::reset() {
    std::memset(shared, 0, sizeof(shared));                 // 共享内存随 SM 复位
    for (int sm = 0; sm < simu::NUM_SM; ++sm) clear_instr(sm);
    // 全局内存(DDR/常量/帧缓冲)不因 CP_RESET 丢失, 与真实硬件一致(CMDS.md §7.3)
}

long load_img(Memory& mem, const char* img_file, uint8_t sm_mask) {
    return mem.load_img(img_file, sm_mask);
}
