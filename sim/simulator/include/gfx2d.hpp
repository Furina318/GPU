#pragma once

// 2D 引擎(固定功能): 填充 / 位块传送 / 三角形光栅化 / 混合。
// 规格的唯一权威是 docs/GFX.md —— 本文件的每个常量、每条校验、每条舍入规则都能在
// GFX.md 里找到对应的行(§2 格式、§3 寄存器、§4 命令、§5 光栅化、§6 混合与写像素、§7 完成与错误)。
//
// 与 CP 的关系: CP 只按 length_words 取走命令并转交(GFX.md §4), 参数/几何校验由本引擎做,
// 失败时引擎报错 ⇒ CP 置 CP_ERROR = ENGINE_FAULT(GFX.md §7.3 与 CMDS.md §8.1)。

#include "memory.hpp"

#include <cstdint>
#include <set>
#include <string>
#include <unordered_map>

namespace simu {

// ── GFX.md §3: 引擎寄存器(只能经 2D_SET_REG 写) ──
enum : uint16_t {
    GFX_REG_FB_WIDTH   = 0x00,  GFX_REG_FB_HEIGHT  = 0x04,
    GFX_REG_FB_STRIDE  = 0x08,  GFX_REG_FB_FORMAT  = 0x0C,
    GFX_REG_SCISSOR_X  = 0x10,  GFX_REG_SCISSOR_Y  = 0x14,
    GFX_REG_SCISSOR_W  = 0x18,  GFX_REG_SCISSOR_H  = 0x1C,
    GFX_REG_BLEND_MODE = 0x20,  GFX_REG_COLKEY     = 0x24,
    GFX_REG_COLKEY_EN  = 0x28,
    GFX_REG_TEX_BASE   = 0x2C,  GFX_REG_TEX_FORMAT = 0x30,  GFX_REG_TEX_FILTER = 0x34,
};

// ── GFX.md §4: 命令 opcode(长度=字, 参数从 word2 起) ──
enum : uint8_t {
    GFX_CMD_FILL    = 0x10,  // 6 字
    GFX_CMD_BLIT    = 0x11,  // 9 字
    GFX_CMD_DRAW    = 0x12,  // 8 字
    GFX_CMD_SET_REG = 0x13,  // 4 字
    GFX_CMD_FLUSH   = 0x14,  // 2 字
    GFX_CMD_DRAW_INDEXED  = 0x15,   // 保留(GFX.md §4.6)
    GFX_CMD_BLIT_STRETCH  = 0x16,   // 保留
    GFX_CMD_SET_PALETTE   = 0x17,   // 保留
};

// GFX.md §2.2 的帧缓冲格式
// GFX.md §2.2: 帧缓冲格式 0 = ARGB8888([31:24]=A, [23:16]=R, [15:8]=G, [7:0]=B)
enum : uint8_t { GFX_FMT_ARGB8888 = 0, GFX_FMT_RGB565 = 1 };

// GFX.md §7.3: 引擎错误码(2D_ERROR 的取值)
enum class GfxErr : uint32_t {
    NONE          = 0,
    BAD_FORMAT    = 1,   // FB_FORMAT/SRC_FMT/vtx_format 非法; FB_WIDTH/HEIGHT 为 0
    BAD_REG       = 2,   // SET_REG 的偏移非法或保留位非 0
    BAD_ALIGN     = 3,   // 地址未按像素/顶点字节数对齐
    OUT_OF_RANGE  = 4,   // 矩形或几何越出帧缓冲
    BAD_PRIM      = 5,   // vtx_count 非 3 的倍数, 或 prim 未实现
    BAD_PARAM     = 6,   // 尺寸为 0、length_words 不符、保留字段非 0
};
const char* gfx_err_name(GfxErr e);

// 运行统计(启动/结束时打印, 也是测试的断言来源)
struct GfxStats {
    uint64_t commands = 0;      // 成功执行的命令数
    uint64_t px_fill  = 0, px_blit = 0, px_draw = 0;
    uint64_t px_killed = 0;     // 被颜色键丢弃的像素
    uint64_t px_clipped = 0;    // 被裁剪框/帧缓冲边界丢掉的像素
    uint64_t rop_flush = 0;     // 写缓冲冲刷次数
    uint64_t rop_bursts = 0;    // 按 32B 行计的总线突发数
    uint64_t rop_words = 0;     // 落盘的字数
};

class Gfx2D {
public:
    // fb_base/fb_size 与 Machine 的 MMIO 0x38(`FB_BASE`, R/W)是同一份数据
    Gfx2D(Memory& mem, uint32_t* fb_base, uint32_t* fb_size)
        : mem_(mem), fb_base_(fb_base), fb_size_(fb_size) {}

    // 执行一条 2D 命令(参数 w 指向 word2, len 是 length_words)。
    // 返回 false ⇒ 引擎报错, 原因在 why 与本对象的 error() 里; 调用方(CP)必须把它转成
    // CP_ERROR = ENGINE_FAULT, 且**不释放信号量**(GFX.md §7.3)。
    bool exec(uint8_t opcode, const uint32_t* w, uint16_t len, std::string* why);

    void flush();                       // 2D_FLUSH(§4.5) / 命令收尾 / 缓冲满
    void reset();                       // CP_RESET(CMDS.md §7.3)

    uint32_t status() const;            // §7.2: bit0 BUSY, bit1 DONE, bit2 ERROR
    uint32_t error() const { return uint32_t(err_); }     // 2D_ERROR 的取值
    GfxErr   error_code() const { return err_; }
    const std::string& error_detail() const { return err_detail_; }
    void clear_error() { err_ = GfxErr::NONE; err_detail_.clear(); }

    const GfxStats& stats() const { return st_; }
    uint16_t fb_w() const { return fb_w_; }
    uint16_t fb_h() const { return fb_h_; }
    uint8_t  fb_fmt() const { return fb_fmt_; }
    int      rop_pending() const { return int(rop_.size()); }

private:
    struct Vert { double x, y, r, g, b, a; };          // §2.1: 6×f32
    struct Rgba { float r = 0, g = 0, b = 0, a = 1; };

    // 校验与工具
    bool fail(GfxErr e, const std::string& why);
    uint32_t bpp() const { return fb_fmt_ == GFX_FMT_RGB565 ? 2u : 4u; }
    uint32_t bpp_of(uint8_t fmt) const { return fmt == GFX_FMT_RGB565 ? 2u : 4u; }
    uint32_t stride_of(uint16_t override_stride) const {
        return override_stride ? override_stride : uint32_t(fb_w_) * bpp();
    }
    bool fmt_ok(uint8_t fmt) const { return fmt <= GFX_FMT_RGB565; }
    bool in_fb(uint32_t addr, uint32_t bytes) const;
    bool check_geom() const { return fb_w_ > 0 && fb_h_ > 0 && fmt_ok(fb_fmt_); }
    bool clipped(uint32_t x, uint32_t y) const {         // §5.5 逐像素裁剪
        if (!scissor_w_ || !scissor_h_) return false;
        return x < scissor_x_ || x >= uint32_t(scissor_x_) + scissor_w_ ||
               y < scissor_y_ || y >= uint32_t(scissor_y_) + scissor_h_;
    }

    // 像素读写(§6.1 的像素流水: 颜色键 → 混合 → 格式转换 → 写缓冲)
    Rgba   load_pixel(uint8_t fmt, uint32_t addr);
    void   emit_pixel(uint32_t addr, const Rgba& c);     // 混合 + 转换 + 进写缓冲
    void   rop_store(uint32_t word_addr, uint32_t value);
    uint32_t rop_load(uint32_t word_addr) const;
    uint32_t convert(const Rgba& c) const;               // §6.3 的目标格式像素位型
    Rgba   expand(uint32_t bits) const;

    // 各命令
    bool exec_fill(const uint32_t* w);
    bool exec_blit(const uint32_t* w);
    bool exec_draw(const uint32_t* w);
    bool exec_set_reg(const uint32_t* w);

    void raster_triangle(const Vert& v0, const Vert& v1, const Vert& v2,
                         uint32_t dst_anchor, bool clip_en);

    // 寄存器(§3)
    uint16_t fb_w_ = 0, fb_h_ = 0, fb_stride_ = 0;
    uint8_t  fb_fmt_ = GFX_FMT_ARGB8888;
    uint16_t scissor_x_ = 0, scissor_y_ = 0, scissor_w_ = 0, scissor_h_ = 0;
    uint8_t  blend_mode_ = 0;                            // 0 NONE, 1 SRC_OVER, 2 ADD
    uint32_t colkey_ = 0;
    bool     colkey_en_ = false;
    uint32_t tex_base_ = 0; uint8_t tex_fmt_ = 0; bool tex_filter_ = false;   // v2 占位

    Memory&   mem_;
    uint32_t* fb_base_;
    uint32_t* fb_size_;

    // 写缓冲(ROP buffer, §6.4): 字地址 → 待写值; 按 32B 行合并, 容量 8 行
    std::unordered_map<uint32_t, uint32_t> rop_;
    std::set<uint32_t> rop_lines_;
    static constexpr size_t kRopLines = 8;

    bool     busy_ = false, done_ = false;
    GfxErr   err_ = GfxErr::NONE;
    std::string err_detail_;
    GfxStats st_;
};

}  // namespace simu
