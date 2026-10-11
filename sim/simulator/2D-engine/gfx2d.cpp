// 2D 引擎的实现 —— 逐条对应 docs/GFX.md 的小节(见每处注释里的 "GFX §x")。
//
// 设计原则(与 GFX.md 一致, 也是本文件存在的理由):
//   · 参数/几何校验在引擎里, 失败必须**报错而不是静默跳过**(GFX §7.3);
//   · 覆盖判定用 s32.16 定点 + top-left 规则(§5.2/§5.3), 颜色用 FP32 重心插值(§5.4):
//     只要有一处舍入没写死, "与参考模型逐像素 diff" 就会长期对不平;
//   · 像素先混合再转格式、只舍入一次(§6.2/§6.3), 写经过 ROP 写缓冲按 32B 行合并(§6.4)。

#include "gfx2d.hpp"

#include "log.hpp"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>

namespace simu {

const char* gfx_err_name(GfxErr e) {
    switch (e) {
    case GfxErr::NONE:         return "NONE";
    case GfxErr::BAD_FORMAT:   return "BAD_FORMAT";
    case GfxErr::BAD_REG:      return "BAD_REG";
    case GfxErr::BAD_ALIGN:    return "BAD_ALIGN";
    case GfxErr::OUT_OF_RANGE: return "OUT_OF_RANGE";
    case GfxErr::BAD_PRIM:     return "BAD_PRIM";
    case GfxErr::BAD_PARAM:    return "BAD_PARAM";
    }
    return "?";
}

static const char* op_name(uint8_t op) {
    switch (op) {
    case GFX_CMD_FILL:    return "2D_FILL";
    case GFX_CMD_BLIT:    return "2D_BLIT";
    case GFX_CMD_DRAW:    return "2D_DRAW";
    case GFX_CMD_SET_REG: return "2D_SET_REG";
    case GFX_CMD_FLUSH:   return "2D_FLUSH";
    default:              return "2D_保留";
    }
}

static inline uint32_t clamp_u8(double v) {          // GFX §6.3
    if (!(v > 0.0)) return 0;                        // 同时挡住 NaN:非法插值不该变成白色
    if (v > 1.0) v = 1.0;
    return uint32_t(std::floor(v * 255.0 + 0.5));
}
static inline uint32_t clamp_bits(double v, int n) { // RGB565 的 5/6 位分量
    if (!(v > 0.0)) return 0;
    if (v > 1.0) v = 1.0;
    return uint32_t(std::floor(v * double((1 << n) - 1) + 0.5));
}
static inline float bits_to_f32(uint32_t w) {           // 顶点字段是 4B 的 FP32(§2.1)
    float f;
    std::memcpy(&f, &w, 4);
    return f;
}
static std::string hex2(uint32_t v) {
    char b[16];
    std::snprintf(b, sizeof(b), "0x%02x", v & 0xFFu);
    return std::string(b);
}

// ──────────────────────────── 状态 / 错误 ────────────────────────────

bool Gfx2D::fail(GfxErr e, const std::string& why) {
    err_ = e;
    err_detail_ = why;
    LogE("2D 引擎错误 %s: %s(GFX.md §7.3)", gfx_err_name(e), why.c_str());
    return false;
}

uint32_t Gfx2D::status() const {
    uint32_t s = 0;
    if (busy_)            s |= 1u << 0;
    if (done_)            s |= 1u << 1;
    if (err_ != GfxErr::NONE) s |= 1u << 2;
    return s;
}

void Gfx2D::reset() {                                 // CMDS §7.3: CP_RESET 复位引擎
    fb_w_ = fb_h_ = fb_stride_ = 0;
    fb_fmt_ = GFX_FMT_ARGB8888;
    scissor_x_ = scissor_y_ = scissor_w_ = scissor_h_ = 0;
    blend_mode_ = 0;
    colkey_ = 0;
    colkey_en_ = false;
    tex_base_ = 0; tex_fmt_ = 0; tex_filter_ = false;
    rop_.clear(); rop_lines_.clear();                 // 未落盘的像素按"丢弃未完成命令"处理
    busy_ = done_ = false;
    err_ = GfxErr::NONE;
    err_detail_.clear();
}

// ─────────────────────── 写缓冲(ROP buffer, §6.4) ───────────────────────

void Gfx2D::flush() {
    if (rop_.empty()) return;
    for (const auto& kv : rop_) mem_.st_global(kv.first, kv.second);
    st_.rop_words  += rop_.size();
    st_.rop_bursts += rop_lines_.size();              // 每 32B 行一次突发
    ++st_.rop_flush;
    LogT("2D 写缓冲冲刷: %zu 字 / %zu 行(突发)", rop_.size(), rop_lines_.size());
    rop_.clear();
    rop_lines_.clear();
}

uint32_t Gfx2D::rop_load(uint32_t word_addr) const {
    auto it = rop_.find(word_addr);
    return it != rop_.end() ? it->second : mem_.ld_global(word_addr);
}

void Gfx2D::rop_store(uint32_t word_addr, uint32_t value) {
    const uint32_t line = word_addr & ~31u;
    if (!rop_lines_.count(line) && rop_lines_.size() >= kRopLines) flush();
    rop_lines_.insert(line);
    rop_[word_addr] = value;
}

// ─────────────────────── 像素: 混合 + 转换(§6.2/§6.3) ───────────────────────

Gfx2D::Rgba Gfx2D::expand(uint32_t bits) const {
    Rgba c{};
    if (fb_fmt_ == GFX_FMT_RGB565) {
        c.r = float((bits >> 11) & 0x1F) / 31.0f;
        c.g = float((bits >> 5) & 0x3F) / 63.0f;
        c.b = float(bits & 0x1F) / 31.0f;
        c.a = 1.0f;                                    // §2.2: RGB565 无 alpha, 当 1.0
    } else {
        // ARGB8888(§2.2): [31:24]=A, [23:16]=R, [15:8]=G, [7:0]=B —— 与既有 shader 内核
        // 写的 0xFF00FF00(不透明绿)以及 SDL/PPM/ASCII 的取色路径完全一致。
        c.a = float((bits >> 24) & 0xFF) / 255.0f;
        c.r = float((bits >> 16) & 0xFF) / 255.0f;
        c.g = float((bits >> 8) & 0xFF) / 255.0f;
        c.b = float(bits & 0xFF) / 255.0f;
    }
    return c;
}

uint32_t Gfx2D::convert(const Rgba& c) const {
    if (fb_fmt_ == GFX_FMT_RGB565)
        return (clamp_bits(c.r, 5) << 11) | (clamp_bits(c.g, 6) << 5) | clamp_bits(c.b, 5);
    return (clamp_u8(c.a) << 24) | (clamp_u8(c.r) << 16) | (clamp_u8(c.g) << 8) | clamp_u8(c.b);
}

Gfx2D::Rgba Gfx2D::load_pixel(uint8_t fmt, uint32_t addr) {
    if (fmt == GFX_FMT_RGB565) {
        const uint32_t word = mem_.ld_global(addr & ~3u);
        return expand((addr & 2u) ? (word >> 16) : (word & 0xFFFFu));
    }
    return expand(mem_.ld_global(addr));
}

void Gfx2D::emit_pixel(uint32_t addr, const Rgba& src) {
    if (!in_fb(addr, bpp())) { ++st_.px_clipped; return; }      // 帧缓冲窗口之外: 计入裁剪
    Rgba out = src;
    if (blend_mode_ != 0) {                                     // §6.2
        const Rgba d = load_pixel(fb_fmt_, addr);
        if (blend_mode_ == 1) {                                 // SRC_OVER
            const float a = src.a;
            out.r = src.r * a + d.r * (1.0f - a);
            out.g = src.g * a + d.g * (1.0f - a);
            out.b = src.b * a + d.b * (1.0f - a);
            out.a = src.a * a + d.a * (1.0f - a);
        } else {                                                // ADD
            out.r = std::min(src.r + d.r, 1.0f);
            out.g = std::min(src.g + d.g, 1.0f);
            out.b = std::min(src.b + d.b, 1.0f);
            out.a = std::min(src.a + d.a, 1.0f);
        }
    }
    const uint32_t bits = convert(out);
    if (bpp() == 4) {                                           // ARGB8888: 校验过 4B 对齐 ⇒ 整字写
        rop_store(addr, bits);
    } else {                                                    // RGB565: 半字合并(读-改-写)
        const uint32_t wa = addr & ~3u;
        const uint32_t cur = rop_load(wa);
        rop_store(wa, (addr & 2u) ? ((cur & 0x0000FFFFu) | (bits << 16))
                                  : ((cur & 0xFFFF0000u) | (bits & 0xFFFFu)));
    }
}

bool Gfx2D::in_fb(uint32_t addr, uint32_t bytes) const {
    const uint32_t base = *fb_base_;
    return addr >= base && (addr - base) + bytes <= *fb_size_;
}

// ──────────────────────────── 2D_SET_REG(§4.4) ────────────────────────────

bool Gfx2D::exec_set_reg(const uint32_t* w) {
    const uint32_t off = w[0] & 0xFFFFu, val = w[1];
    if (w[0] >> 16) return fail(GfxErr::BAD_PARAM, "2D_SET_REG 的 word2[31:16] 保留位必须为 0(§4.4)");
    auto width_ok = [&](uint32_t v, uint32_t max) { return v >= 1 && v <= max; };
    switch (off) {
    case GFX_REG_FB_WIDTH:
        if (val >> 16 || !width_ok(val, 4096)) return fail(GfxErr::BAD_REG, "FB_WIDTH 取值必须是 1..4096(§3)");
        fb_w_ = uint16_t(val); break;
    case GFX_REG_FB_HEIGHT:
        if (val >> 16 || !width_ok(val, 4096)) return fail(GfxErr::BAD_REG, "FB_HEIGHT 取值必须是 1..4096(§3)");
        fb_h_ = uint16_t(val); break;
    case GFX_REG_FB_STRIDE:
        if (val >> 16) return fail(GfxErr::BAD_REG, "FB_STRIDE 只有 16 位(§3)");
        if (val & 3u)  return fail(GfxErr::BAD_REG, "FB_STRIDE 必须 4B 对齐(§3)");
        if (val && fb_w_ && val < uint32_t(fb_w_) * bpp())
            return fail(GfxErr::BAD_REG, "FB_STRIDE 小于一行的字节数, 行会重叠(§3)");
        fb_stride_ = uint16_t(val); break;
    case GFX_REG_FB_FORMAT:
        if (!fmt_ok(uint8_t(val))) return fail(GfxErr::BAD_FORMAT, "FB_FORMAT=" + std::to_string(val) + " 是保留值(§2.2)");
        fb_fmt_ = uint8_t(val); break;
    case GFX_REG_SCISSOR_X: scissor_x_ = uint16_t(val & 0xFFFFu); break;
    case GFX_REG_SCISSOR_Y: scissor_y_ = uint16_t(val & 0xFFFFu); break;
    case GFX_REG_SCISSOR_W: scissor_w_ = uint16_t(val & 0xFFFFu); break;
    case GFX_REG_SCISSOR_H: scissor_h_ = uint16_t(val & 0xFFFFu); break;
    case GFX_REG_BLEND_MODE:
        if (val > 2) return fail(GfxErr::BAD_REG, "BLEND_MODE=3 是保留值(§6.2)");
        blend_mode_ = uint8_t(val); break;
    case GFX_REG_COLKEY:    colkey_ = val; break;
    case GFX_REG_COLKEY_EN:
        if (val > 1) return fail(GfxErr::BAD_REG, "COLKEY_EN 只有 1 位(§3)");
        colkey_en_ = val != 0; break;
    case GFX_REG_TEX_BASE:   tex_base_ = val; break;                     // v2: 允许写, 引擎不用
    case GFX_REG_TEX_FORMAT:
        if (val >> 8) return fail(GfxErr::BAD_REG, "TEX_FORMAT 只有 8 位(§3)");
        tex_fmt_ = uint8_t(val); break;
    case GFX_REG_TEX_FILTER:
        if (val > 1) return fail(GfxErr::BAD_REG, "TEX_FILTER 只有 1 位(§3)");
        tex_filter_ = val != 0; break;
    default:
        return fail(GfxErr::BAD_REG, "2D_SET_REG 偏移 " + hex2(off) + " 未定义(§3)");
    }
    LogI("2D 寄存器 ← 0x%02x = 0x%x", off, val);
    return true;
}

// ──────────────────────────── 2D_FILL(§4.1) ────────────────────────────

bool Gfx2D::exec_fill(const uint32_t* w) {
    const uint32_t dst = w[0], color = w[1];
    const uint32_t x = w[2] & 0xFFFFu, y = w[2] >> 16;
    const uint32_t tw = w[3] & 0xFFFFu, th = w[3] >> 16;
    if (!check_geom())  return fail(GfxErr::BAD_FORMAT, "FB_WIDTH/HEIGHT/FORMAT 必须在任何 2D 命令之前写好(§3)");
    if (!tw || !th)     return fail(GfxErr::BAD_PARAM, "2D_FILL 的 w/h 必须 ≥ 1(§4.1)");
    if (dst & (bpp() - 1)) return fail(GfxErr::BAD_ALIGN, "dst_addr 未按目标格式的像素字节数对齐(§4.1)");
    if (!in_fb(dst, bpp())) return fail(GfxErr::OUT_OF_RANGE, "dst_addr 不在帧缓冲窗口内(§4.1)");
    const bool in_scissor = scissor_w_ && scissor_h_ && x >= scissor_x_ && y >= scissor_y_ &&
                            x + tw <= uint32_t(scissor_x_) + scissor_w_ &&
                            y + th <= uint32_t(scissor_y_) + scissor_h_;
    if ((x + tw > fb_w_ || y + th > fb_h_) && !in_scissor)
        return fail(GfxErr::OUT_OF_RANGE, "2D_FILL 矩形越出帧缓冲, 且未被裁剪框完全包含(§4.1)");

    const uint32_t bits = (fb_fmt_ == GFX_FMT_RGB565) ? (color & 0xFFFFu) : color;   // §2.3
    const Rgba c = expand(bits);
    const uint32_t stride = stride_of(fb_stride_);
    uint64_t px = 0;
    for (uint32_t dy = 0; dy < th; ++dy)
        for (uint32_t dx = 0; dx < tw; ++dx) {
            const uint32_t ax = x + dx, ay = y + dy;
            if (clipped(ax, ay)) { ++st_.px_clipped; continue; }
            emit_pixel(dst + dy * stride + dx * bpp(), c);
            ++px;
        }
    st_.px_fill += px;
    LogI("2D_FILL   x=%u y=%u w=%u h=%u dst=0x%08x color=0x%08x → %llu 像素%s",
         x, y, tw, th, dst, color, (unsigned long long)px,
         (fb_fmt_ == GFX_FMT_RGB565 ? " (RGB565)" : ""));
    return true;
}

// ──────────────────────────── 2D_BLIT(§4.2) ────────────────────────────

bool Gfx2D::exec_blit(const uint32_t* w) {
    const uint32_t src = w[0], dst = w[1];
    const uint32_t sstride = w[2] & 0xFFFFu;
    const uint32_t x = w[3] & 0xFFFFu, y = w[3] >> 16;
    const uint32_t tw = w[4] & 0xFFFFu, th = w[4] >> 16;
    const uint32_t flags = w[5], colkey = w[6];
    if (w[2] >> 16)      return fail(GfxErr::BAD_PARAM, "2D_BLIT 的 word4[31:16] 保留位必须为 0(§4.2)");
    if (flags & ~0xFu)   return fail(GfxErr::BAD_PARAM, "2D_BLIT flags 的保留位必须为 0(§4.2)");
    if (!check_geom())   return fail(GfxErr::BAD_FORMAT, "FB_WIDTH/HEIGHT/FORMAT 未配置(§3)");
    if (!tw || !th)      return fail(GfxErr::BAD_PARAM, "2D_BLIT 的 w/h 必须 ≥ 1(§4.2)");
    const uint8_t sfmt = uint8_t((flags >> 3) & 1u);
    if (!fmt_ok(sfmt))   return fail(GfxErr::BAD_FORMAT, "SRC_FMT 是保留值(§4.2)");
    const uint32_t sbpp = bpp_of(sfmt), dbpp = bpp();
    if (src & (sbpp - 1)) return fail(GfxErr::BAD_ALIGN, "src_addr 未按源格式的像素字节数对齐(§4.2)");
    if (dst & (dbpp - 1)) return fail(GfxErr::BAD_ALIGN, "dst_addr 未按目标格式的像素字节数对齐(§4.2)");
    const uint32_t sstride_eff = sstride ? sstride : tw * sbpp;
    if (sstride & 3u)     return fail(GfxErr::BAD_ALIGN, "src_stride 必须 4B 对齐(§4.2)");
    if (sstride && sstride < tw * sbpp)
        return fail(GfxErr::BAD_PARAM, "src_stride 小于一行的字节数, 行会重叠(§4.2)");
    if (!in_fb(dst, dbpp)) return fail(GfxErr::OUT_OF_RANGE, "dst_addr 不在帧缓冲窗口内(§4.2)");
    if (src < MMIO_END)   return fail(GfxErr::OUT_OF_RANGE, "src_addr 落在 MMIO 窗口(§4.2)");
    if (x + tw > fb_w_ || y + th > fb_h_)
        return fail(GfxErr::OUT_OF_RANGE, "2D_BLIT 目标矩形越出帧缓冲(§4.2)");

    const bool ck = (flags & 1u) != 0, fx = (flags & 2u) != 0, fy = (flags & 4u) != 0;
    const uint32_t mask = (sbpp == 2) ? 0xFFFFu : 0xFFFFFFFFu;
    const uint32_t stride = stride_of(fb_stride_);
    uint64_t px = 0;
    for (uint32_t dy = 0; dy < th; ++dy)
        for (uint32_t dx = 0; dx < tw; ++dx) {
            const uint32_t sx = fx ? tw - 1 - dx : dx;
            const uint32_t sy = fy ? th - 1 - dy : dy;
            const uint32_t saddr = src + sy * sstride_eff + sx * sbpp;
            uint32_t sbits;
            if (sbpp == 2) {
                const uint32_t word = mem_.ld_global(saddr & ~3u);
                sbits = (saddr & 2u) ? (word >> 16) : (word & 0xFFFFu);
            } else {
                sbits = mem_.ld_global(saddr);
            }
            if (ck && (sbits & mask) == (colkey & mask)) { ++st_.px_killed; continue; }   // §4.2
            const uint32_t ax = x + dx, ay = y + dy;
            if (clipped(ax, ay)) { ++st_.px_clipped; continue; }
            const Rgba c = (sfmt == fb_fmt_) ? expand(sbits) : load_pixel(sfmt, saddr);  // §6.3 的格式转换
            emit_pixel(dst + dy * stride + dx * dbpp, c);
            ++px;
        }
    st_.px_blit += px;
    LogI("2D_BLIT   src=0x%08x dst=0x%08x x=%u y=%u w=%u h=%u flags=0x%x → %llu 像素%s",
         src, dst, x, y, tw, th, flags, (unsigned long long)px, ck ? " (颜色键)" : "");
    return true;
}

// ──────────────────────────── 2D_DRAW(§4.3) ────────────────────────────

bool Gfx2D::exec_draw(const uint32_t* w) {
    const uint32_t vtx_addr = w[0], vtx_count = w[1];
    const uint8_t vtx_fmt = uint8_t(w[2] & 0xFFu), prim = uint8_t((w[2] >> 8) & 0xFFu);
    const uint32_t flags = w[3];
    if (w[2] >> 16)     return fail(GfxErr::BAD_PARAM, "2D_DRAW 的 word4[31:16] 保留位必须为 0(§4.3)");
    if (w[4] || w[5])   return fail(GfxErr::BAD_PARAM, "2D_DRAW 的 word6/word7 必须为 0(§4.3)");
    if (flags & ~1u)    return fail(GfxErr::BAD_PARAM, "2D_DRAW flags 的保留位必须为 0(§4.3)");
    if (!check_geom())  return fail(GfxErr::BAD_FORMAT, "FB_WIDTH/HEIGHT/FORMAT 未配置(§3)");
    if (prim != 0)      return fail(GfxErr::BAD_PRIM, "prim=" + std::to_string(prim) + " 未实现(只支持 0=三角形列表, §4.3)");
    if (vtx_count < 3 || (vtx_count % 3))
        return fail(GfxErr::BAD_PRIM, "vtx_count 必须是 3 的倍数且 ≥ 3(§4.3)");
    if (vtx_fmt != 0)
        return fail(GfxErr::BAD_FORMAT, "vtx_format=" + std::to_string(vtx_fmt) + " 属 v2(带 uv), 本实现只有 0 = 6×f32(§2.1)");
    if (vtx_addr & 3u)  return fail(GfxErr::BAD_ALIGN, "vtx_addr 必须 4B 对齐(§4.3)");
    if (vtx_addr < MMIO_END) return fail(GfxErr::OUT_OF_RANGE, "vtx_addr 落在 MMIO 窗口(§4.3)");

    const bool clip_en = (flags & 1u) != 0;
    const uint32_t anchor = *fb_base_;
    for (uint32_t t = 0; t + 2 < vtx_count; t += 3) {
        Vert v[3];
        for (int k = 0; k < 3; ++k) {
            const uint32_t a = vtx_addr + (t + uint32_t(k)) * 24u;   // 6×f32 = 24B(§2.1)
            v[k].x = double(bits_to_f32(mem_.ld_global(a)));
            v[k].y = double(bits_to_f32(mem_.ld_global(a + 4)));
            v[k].r = double(bits_to_f32(mem_.ld_global(a + 8)));
            v[k].g = double(bits_to_f32(mem_.ld_global(a + 12)));
            v[k].b = double(bits_to_f32(mem_.ld_global(a + 16)));
            v[k].a = double(bits_to_f32(mem_.ld_global(a + 20)));
        }
        raster_triangle(v[0], v[1], v[2], anchor, clip_en);
    }
    LogI("2D_DRAW   vtx=0x%08x count=%u(=%u 三角形) flags=0x%x → %llu 像素",
         vtx_addr, vtx_count, vtx_count / 3, flags, (unsigned long long)st_.px_draw);
    return true;
}

// ─────────────────── 光栅化: s32.16 边方程 + top-left(§5) ───────────────────

void Gfx2D::raster_triangle(const Vert& v0, const Vert& v1, const Vert& v2,
                            uint32_t anchor, bool clip_en) {
    auto to_fx = [](double v) { return int64_t(std::llround(v * 65536.0)); };   // §5.2: s32.16
    const int64_t X0 = to_fx(v0.x), Y0 = to_fx(v0.y);
    const int64_t X1 = to_fx(v1.x), Y1 = to_fx(v1.y);
    const int64_t X2 = to_fx(v2.x), Y2 = to_fx(v2.y);
    auto edge = [](int64_t ax, int64_t ay, int64_t bx, int64_t by, int64_t px, int64_t py) {
        return (bx - ax) * (py - ay) - (by - ay) * (px - ax);
    };
    const int64_t area_raw = edge(X0, Y0, X1, Y1, X2, Y2);
    if (area_raw == 0) { ++st_.px_clipped; return; }          // 退化三角形: 整块丢弃(§5.2)
    const int64_t sgn = area_raw < 0 ? -1 : 1;                // 统一成正向(§5.3)
    const int64_t A = sgn * area_raw;

    const int64_t ea[3][4] = {{X0, Y0, X1, Y1}, {X1, Y1, X2, Y2}, {X2, Y2, X0, Y0}};
    bool tl[3];
    for (int i = 0; i < 3; ++i) {
        const int64_t dx = sgn * (ea[i][2] - ea[i][0]), dy = sgn * (ea[i][3] - ea[i][1]);
        tl[i] = (dy > 0) || (dy == 0 && dx < 0);              // top/left 判据(§5.3)
    }

    // bbox 只覆盖三角形与帧缓冲的交集(§5.2 允许顶点落在屏幕外)
    const double minx = std::min({v0.x, v1.x, v2.x}), maxx = std::max({v0.x, v1.x, v2.x});
    const double miny = std::min({v0.y, v1.y, v2.y}), maxy = std::max({v0.y, v1.y, v2.y});
    const int64_t bx0 = std::max<int64_t>(0, int64_t(std::floor(minx)));
    const int64_t bx1 = std::min<int64_t>(int64_t(fb_w_) - 1, int64_t(std::ceil(maxx)));
    const int64_t by0 = std::max<int64_t>(0, int64_t(std::floor(miny)));
    const int64_t by1 = std::min<int64_t>(int64_t(fb_h_) - 1, int64_t(std::ceil(maxy)));
    const uint32_t stride = stride_of(fb_stride_);

    for (int64_t py = by0; py <= by1; ++py)
        for (int64_t px = bx0; px <= bx1; ++px) {
            const int64_t CX = px * 65536 + 32768, CY = py * 65536 + 32768;    // 像素中心(§5.1)
            const int64_t e0 = sgn * edge(X0, Y0, X1, Y1, CX, CY);
            const int64_t e1 = sgn * edge(X1, Y1, X2, Y2, CX, CY);
            const int64_t e2 = sgn * edge(X2, Y2, X0, Y0, CX, CY);
            if (!(e0 > 0 || (e0 == 0 && tl[0]))) continue;
            if (!(e1 > 0 || (e1 == 0 && tl[1]))) continue;
            if (!(e2 > 0 || (e2 == 0 && tl[2]))) continue;
            if (clip_en && clipped(uint32_t(px), uint32_t(py))) { ++st_.px_clipped; continue; }
            const double l0 = double(e1) / double(A), l1 = double(e2) / double(A),
                         l2 = double(e0) / double(A);                          // §5.3 的重心坐标
            Rgba c;
            c.r = float(l0 * v0.r + l1 * v1.r + l2 * v2.r);                    // §5.4: FP32 线性插值
            c.g = float(l0 * v0.g + l1 * v1.g + l2 * v2.g);
            c.b = float(l0 * v0.b + l1 * v1.b + l2 * v2.b);
            c.a = float(l0 * v0.a + l1 * v1.a + l2 * v2.a);
            emit_pixel(anchor + uint32_t(py) * stride + uint32_t(px) * bpp(), c);
            ++st_.px_draw;
        }
}

// ──────────────────────────── 命令分发(§4) ────────────────────────────

bool Gfx2D::exec(uint8_t opcode, const uint32_t* w, uint16_t len, std::string* why) {
    if (err_ != GfxErr::NONE) {                            // 出错后引擎停机(§7.3: 不假装继续)
        if (why) *why = "2D 引擎处于错误态(" + std::string(gfx_err_name(err_)) + "), 需写 2D_ERROR 清零";
        return false;
    }
    busy_ = true;
    bool ok = false;
    switch (opcode) {
    case GFX_CMD_FILL:
        ok = (len == 6) ? exec_fill(w)
                        : fail(GfxErr::BAD_PARAM, "2D_FILL 的 length_words 必须是 6(§4.1)");
        break;
    case GFX_CMD_BLIT:
        ok = (len == 9) ? exec_blit(w)
                        : fail(GfxErr::BAD_PARAM, "2D_BLIT 的 length_words 必须是 9(§4.2)");
        break;
    case GFX_CMD_DRAW:
        ok = (len == 8) ? exec_draw(w)
                        : fail(GfxErr::BAD_PARAM, "2D_DRAW 的 length_words 必须是 8(§4.3)");
        break;
    case GFX_CMD_SET_REG:
        ok = (len == 4) ? exec_set_reg(w)
                        : fail(GfxErr::BAD_PARAM, "2D_SET_REG 的 length_words 必须是 4(§4.4)");
        break;
    case GFX_CMD_FLUSH:
        ok = (len == 2) ? true : fail(GfxErr::BAD_PARAM, "2D_FLUSH 的 length_words 必须是 2(§4.5)");
        break;
    case GFX_CMD_DRAW_INDEXED: case GFX_CMD_BLIT_STRETCH: case GFX_CMD_SET_PALETTE:
        ok = fail(GfxErr::BAD_PARAM,
                  std::string(op_name(opcode)) + " 是保留 opcode, 参数语义未定(§4.6)");
        break;
    default:
        ok = fail(GfxErr::BAD_PARAM, "未知的 2D opcode " + hex2(opcode));
        break;
    }
    if (ok) {
        done_ = true;                                     // §7.1: 完成 = 像素已落盘(写缓冲冲刷)
        flush();
        ++st_.commands;
        LogD("%s 完成: 写缓冲 %llu 次冲刷 / %llu 突发 / %llu 字",
             op_name(opcode), (unsigned long long)st_.rop_flush,
             (unsigned long long)st_.rop_bursts, (unsigned long long)st_.rop_words);
    }
    busy_ = false;
    if (why) *why = err_detail_;
    return ok;
}

}  // namespace simu
