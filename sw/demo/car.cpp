// sw/demo/car.cpp —— 小汽车 demo 的 host 驱动
//
// 两种画法, 同一幅画(GFX.md §1 的分工):
//   默认      : **2D 引擎路径** —— shader 只算 6 个顶点(车顶高光), 背景与精灵交给固定功能单元:
//               2D_FILL(草地/公路/虚线) + 2D_BLIT(精灵小车, 颜色键) + 2D_DRAW(高光四边形, 混合)
//   --shader  : **逐像素路径** —— sw/kernels/car.S 每个线程算一批像素(背景公式 + 精灵搬移)
// 两条路的背景与精灵像素必须**完全一致**(同一个参考模型校验), 差别只在 2D 模式多一层高光 ——
// 这正是"固定功能单元把像素工作从 SIMT 核手里拿走"的可观测对比。

#include "demo.hpp"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

namespace {

using namespace simu;

constexpr int      SW = 24, SH = 12;             // 精灵尺寸(与 car.S 的 sprite_w/h 一致)
constexpr uint32_t SPRITE_BASE = 0x8000'0000u;   // 精灵位图落点(DDR)
constexpr uint32_t VTX_BASE    = 0x8000'1000u;   // 顶点缓冲落点(car_vtx.S 的输出 = 2D_DRAW 的输入)
constexpr uint32_t HL_RGBA     = 0xC8FFFFFFu;    // 高光颜色: 白, alpha = 0xC8(帧缓冲是 ARGB8888: A 在最高字节)
constexpr float    HL_ALPHA    = 200.0f / 255.0f; // = 0xC8/255, 与上面的 A 通道一致(顶点里传的就是它)
constexpr int      CAR_Y       = 26;             // 车道(路面 [20,44) 内)

// 把 float 的位型交给内核(常量区里传的是位型, 内核按 FP32 读)
uint32_t fbits(float v) {
    uint32_t u;
    std::memcpy(&u, &v, 4);
    return u;
}

// 精灵图: 24×12 字符画(每行恰好 24 字符)
//   '.' 透明(0)  'R' 车身红  'W' 车窗蓝  'K' 车轮黑
const char* const kSprite[SH] = {
    "........................",
    "..RRRRRRRRRRRRRRRRRRR...",
    "..RRRRRRRRRRRRRRRRRRR...",
    "..RWWWWRRRRRRRRRRRRRR...",
    "..RWWWWRRRRRRRRRRRRRR...",
    "..RWWWWRRRRRRRRRRRRRRR..",
    "..RRRRRRRRRRRRRRRRRRRRR.",
    "..KKKKKRRRRRRRRRRRKKKKK.",
    "..KK.KKRRRRRRRRRRRKK.KK.",
    "..KK.KKRRRRRRRRRRRKK.KK.",
    "..KKKKK...........KKKKK.",
    "........................",
};

uint32_t sprite_color(char c) {
    switch (c) {
    case 'R': return 0xFFC84038u;   // 车身红
    case 'W': return 0xFF8CC8F0u;   // 车窗
    case 'K': return 0xFF1C1C1Cu;   // 车轮
    default:  return 0u;            // '.' 透明键
    }
}

std::vector<uint32_t> build_sprite() {
    std::vector<uint32_t> px(size_t(SW) * size_t(SH), 0);
    for (int y = 0; y < SH; ++y) {
        if (std::strlen(kSprite[y]) != size_t(SW)) {
            std::fprintf(stderr, "精灵图第 %d 行不是 %d 字符\n", y, SW);
            std::exit(1);
        }
        for (int x = 0; x < SW; ++x)
            px[size_t(y) * SW + size_t(x)] = sprite_color(kSprite[y][x]);
    }
    return px;
}

// 道路几何与配色: 两种画法共用同一份(car.S 的常量布局与 2D 命令的矩形都由它导出)
struct Scene {
    int      road_top = 20, road_h = 24, dash_y = 31, dash_h = 2;
    uint32_t grass = 0xFF1E4A1Eu, road = 0xFF4A4A4Au, dash = 0xFFD8D8D8u;
    int      dash_on = 8, dash_period = 16;      // 8px 实 / 8px 虚(与 car.S 的 (x>>3)&1 同图案)
};

// 小车位置: 从画布左侧驶入, 越过右侧后环绕。保持非负 —— 2D 命令的坐标字段是无符号的,
// 而"负坐标 + 源偏移"需要 2D_BLIT 支持源内偏移(当前版本没有, 见 GFX.md §4.2)。
int car_x_of(const demo::FrameCtx& ctx) { return int(uint32_t(ctx.frame) * 5u) % (ctx.w + SW); }

// 参考模型(两种画法共用): 背景公式 + 精灵颜色键合成
struct Ref {
    Scene sc;
    int   car_x = 0, car_y = CAR_Y;
    bool  highlight = false;                     // 2D 模式多一层高光四边形

    uint32_t base_pixel(int x, int y, const std::vector<uint32_t>& sprite) const {
        const auto u = [](int v) { return uint32_t(v); };
        uint32_t want = sc.grass;
        if (u(y - sc.road_top) < u(sc.road_h)) want = sc.road;
        if (u(y - sc.dash_y) < u(sc.dash_h) && (((u(x) >> 3) & 1u) != 0)) want = sc.dash;
        const int dx = x - car_x, dy = y - car_y;
        if (u(dy) < u(SH) && u(dx) < u(SW)) {
            const uint32_t pix = sprite[size_t(dy) * SW + size_t(dx)];
            if (pix != 0) want = pix;            // 0 = 透明键 ⇒ 保留背景
        }
        return want;
    }
};

// 高光四边形(与 car.cpp 提交给 car_vtx.S 的参数一致): 覆盖小车车窗那几行的斜平行四边形
void highlight_geometry(const Ref& r, double& bx, double& by, double& bw, double& bh, double& shear) {
    bx    = double(r.car_x) + 3.0;
    by    = double(r.car_y) + 1.0;
    bw    = double(SW - 6);
    bh    = 5.0;
    shear = 0.5;
}

// 高光四边形与画布求交后, 采样点落在其中的像素数(以及参与的像素行数)。
// 小车会驶出画布右侧: 这时四边形被裁掉一部分, 断言必须按"可见面积"算, 否则会误报。
int visible_highlight_px(const Ref& r, int w, int h, int* rows) {
    double bx, by, bw, bh, shear;
    highlight_geometry(r, bx, by, bw, bh, shear);
    int n = 0, nr = 0;
    for (int y = 0; y < h; ++y) {
        const double cy = double(y) + 0.5;
        if (cy < by || cy >= by + bh) continue;
        const double lx = bx + shear * (cy - by);
        bool any = false;
        for (int x = 0; x < w; ++x) {
            const double cx = double(x) + 0.5;
            if (cx >= lx && cx < lx + bw) { ++n; any = true; }
        }
        if (any) ++nr;
    }
    if (rows) *rows = nr;
    return n;
}

// 严格在四边形内部(向内缩 margin, 避开光栅化边界规则的 ±1 像素)
bool strictly_inside(const Ref& r, double x, double y, double margin) {
    double bx, by, bw, bh, shear;
    highlight_geometry(r, bx, by, bw, bh, shear);
    if (y < by + margin || y > by + bh - margin) return false;
    const double lx = bx + shear * (y - by);
    return x > lx + margin && x < lx + bw - margin;
}

// 引擎的"混合 + 目标格式转换"(GFX.md §6.2/§6.3): 在这里复现一次, 用于高光区域的期望值
uint32_t blend_over(uint32_t dst_bits, uint32_t src_bits) {
    auto expand = [](uint32_t b) {          // ARGB8888(§2.2)
        return std::array<float, 4>{float((b >> 16) & 0xFF) / 255.0f, float((b >> 8) & 0xFF) / 255.0f,
                                    float(b & 0xFF) / 255.0f, float((b >> 24) & 0xFF) / 255.0f};
    };
    const auto d = expand(dst_bits), s = expand(src_bits);
    const auto r8 = [](float v) {
        if (v > 1.0f) v = 1.0f;
        return uint32_t(std::floor(double(v) * 255.0 + 0.5));
    };
    uint32_t out = 0;
    for (int i = 0; i < 4; ++i) {
        const float v = s[size_t(i)] * HL_ALPHA + d[size_t(i)] * (1.0f - HL_ALPHA);   // src-over
        out |= r8(v) << (16 - 8 * i);        // r→[23:16]、g→[15:8]、b→[7:0]
    }
    return out;
}

}  // namespace

int main(int argc, char** argv) {
    const std::vector<uint32_t> sprite = build_sprite();
    Ref ref;

    demo::Spec spec;
    spec.image  = "build/car_vtx.bin";       // 默认: 顶点内核(2D 引擎路径)
    spec.w = 128; spec.h = 64;               // 宽画布: W 是 2 的幂, H 任意
    spec.warps = 1;                          // car_vtx 只用 6 个 lane
    spec.about = "小汽车行驶: 2D 引擎(FILL/BLIT/DRAW)或逐像素 shader, 两种画法同一幅画";
    spec.check_canvas = [](int w, int h) -> std::string {
        (void)h;
        const bool ok = w > 0 && (w & (w - 1)) == 0;
        return ok ? "" : "--w 必须是 2 的幂(car.S 用 and/srl 取 x/y; --h 任意)";
    };

    // --shader: 切回逐像素内核(sw/kernels/car.S)
    bool legacy = false;
    spec.custom_opt = [&](const std::string& opt, const std::string&) -> int {
        if (opt == "--shader") { legacy = true; return 1; }
        return 0;
    };
    spec.extra_usage = "  --shader            用逐像素 shader 内核(car.S)画, 而不是 2D 引擎\n";
    spec.image_override = [&]() -> std::string {
        if (!legacy) return "";
        spec.warps = 4;                      // 逐像素内核: 4 warp × 8 lane 覆盖 128×64
        return "build/car.bin";
    };

    // 启动时把精灵位图写进全局内存: 逐像素内核 ld.g 搬它, 2D_BLIT 也从这里读
    simu::Machine* gpu = nullptr;
    spec.setup = [&](simu::Machine& m) {
        gpu = &m;
        for (size_t i = 0; i < sprite.size(); ++i)
            m.mem.st_global(SPRITE_BASE + uint32_t(i) * 4, sprite[i]);
    };

    spec.params_for = [&](const demo::FrameCtx& ctx) {
        ref.sc = Scene{};
        ref.car_x = car_x_of(ctx);
        ref.highlight = !legacy;
        const auto f = [](float v) { return fbits(v); };
        if (!legacy) {                        // car_vtx.S 的常量布局(17 字)
            std::vector<uint32_t> p(17, 0);
            p[0]  = VTX_BASE;
            p[1]  = f(float(ref.car_x) + 3.0f);                     // 高光四边形左上角 x
            p[2]  = f(float(ref.car_y) + 1.0f);                     // 左上角 y
            p[3]  = f(float(SW - 6));                               // body_w
            p[4]  = f(5.0f);                                        // body_h
            p[5]  = f(0.5f);                                        // shear
            // 六条顶点的颜色取同一份: 2D_DRAW 按重心插值(GFX.md §5.4), 同色 ⇒ 逐像素为常量,
            // demo 的参考模型才能精确对拍(渐变插值本身由 make test 的 draw_blend 用例守)
            for (int c = 0; c < 4; ++c) p[6 + c] = p[10 + c] = f(c == 3 ? HL_ALPHA : 1.0f);
            return p;
        }
        std::vector<uint32_t> p(17, 0);       // car.S 的常量布局(与改造前完全一致)
        p[0]  = uint32_t(ctx.w) * uint32_t(ctx.h);                  // N
        p[1]  = uint32_t(ctx.warps) * uint32_t(WARP_SIZE);          // stride_elems
        p[2]  = ctx.fb_base;
        p[3]  = uint32_t(ref.car_x);
        p[4]  = uint32_t(ref.car_y);
        p[5]  = uint32_t(ctx.w - 1);                                // W_mask
        p[6]  = uint32_t(std::log2(double(ctx.w)));                 // H_shift
        p[7]  = SPRITE_BASE;
        p[8]  = SW; p[9] = SH;
        p[10] = ref.sc.grass; p[11] = ref.sc.road; p[12] = ref.sc.dash;
        p[13] = uint32_t(ref.sc.road_top); p[14] = uint32_t(ref.sc.road_h);
        p[15] = uint32_t(ref.sc.dash_y);   p[16] = uint32_t(ref.sc.dash_h);
        return p;
    };

    // ── 2D 引擎路径: 每帧 FILL(背景) → BLIT(精灵) → DRAW(高光, wait_id=0 等本帧 LAUNCH) ──
    spec.extra_cmds = [&](CommandProcessor& cp, const demo::FrameCtx& ctx, std::string* err) -> bool {
        if (legacy) return true;             // 逐像素路径不碰 2D 引擎
        const Scene&   sc = ref.sc;
        const uint32_t fb = ctx.fb_base;
        const int      car_x = ref.car_x;
        const int      bw = std::min(SW, ctx.w - car_x);          // 精灵右边界可能出画布
        const auto reg = [&](uint16_t off, uint32_t v) {
            return cp.submit_2d(GFX_CMD_SET_REG, {off, v}, SEM_NONE, SEM_NONE, err);
        };
        const auto fill = [&](int x, int y, int w, int h, uint32_t color) {
            return cp.submit_2d(GFX_CMD_FILL,
                                {fb + (uint32_t(y) * uint32_t(ctx.w) + uint32_t(x)) * 4, color,
                                 uint32_t(x) | (uint32_t(y) << 16), uint32_t(w) | (uint32_t(h) << 16)},
                                SEM_NONE, SEM_NONE, err);
        };
        // ① 每帧重新配置引擎: 任何一帧都能独立重放, 不依赖上一帧残留的寄存器状态(GFX.md §3)
        if (!reg(GFX_REG_FB_WIDTH, uint32_t(ctx.w)) || !reg(GFX_REG_FB_HEIGHT, uint32_t(ctx.h)) ||
            !reg(GFX_REG_FB_FORMAT, GFX_FMT_ARGB8888) || !reg(GFX_REG_BLEND_MODE, 0) ||
            !reg(GFX_REG_COLKEY, 0) || !reg(GFX_REG_COLKEY_EN, 1))
            return false;
        // ② 背景: 草地 → 公路 → 虚线(8px 实 / 8px 虚)
        if (!fill(0, 0, ctx.w, ctx.h, sc.grass)) return false;
        if (!fill(0, sc.road_top, ctx.w, sc.road_h, sc.road)) return false;
        // 相位与 car.S 的 (x>>3)&1 对齐: 实线段落在 x ∈ [8,16), [24,32), …(两种画法像素级可比)
        for (int x = sc.dash_period / 2; x < ctx.w; x += sc.dash_period)
            if (!fill(x, sc.dash_y, sc.dash_on, sc.dash_h, sc.dash)) return false;
        // ③ 精灵小车: 固定功能搬移 + 颜色键透明(GFX.md §4.2)
        // src_stride 必须给**真实行跨距**(SW*4), 不能用 0: 0 表示"紧凑 = 搬运宽度 × 4",
        // 而小车驶到右边界外时搬运宽度 bw < SW ⇒ 源行会错位(GFX.md §4.2)
        if (bw > 0 &&
            !cp.submit_2d(GFX_CMD_BLIT,
                          {SPRITE_BASE,
                           fb + (uint32_t(ref.car_y) * uint32_t(ctx.w) + uint32_t(car_x)) * 4,
                           uint32_t(SW) * 4,
                           uint32_t(car_x) | (uint32_t(ref.car_y) << 16),
                           uint32_t(bw) | (uint32_t(SH) << 16), 0x1 /* COLKEY_EN */, 0},
                          SEM_NONE, SEM_NONE, err))
            return false;
        // ④ 高光四边形: shader 算出的 6 个顶点 → 2D_DRAW, wait_id=0 显式表达"等本帧 LAUNCH"
        if (!reg(GFX_REG_BLEND_MODE, 1)) return false;              // SRC_OVER
        return cp.submit_2d(GFX_CMD_DRAW, {VTX_BASE, 6, 0 /* vtx_format=0,prim=三角形列表 */, 0, 0, 0},
                            2 /* signal_id: 完成 ⇒ SEM[2] += 1 */, 0 /* wait_id: 等 SEM[0](LAUNCH) */, err);
    };

    // ── 参考模型: 两种画法共用(所以 --shader 与默认路径画出来的背景/精灵必须逐像素相同) ──
    spec.verify = [&](const std::vector<uint32_t>& fb, const std::vector<uint32_t>&, const demo::FrameCtx& ctx) {
        if (ctx.frame == 0 && !legacy && gpu && std::getenv("CAR_DUMP_VTX")) {
            std::fprintf(stderr, "[vtx] 6 个顶点(x y r g b a, 来自 car_vtx.S):\n");
            for (uint32_t i = 0; i < 6; ++i) {
                std::fprintf(stderr, "[vtx]  ");
                for (uint32_t j = 0; j < 6; ++j) {
                    const uint32_t w = gpu->mem.ld_global(VTX_BASE + (i * 6 + j) * 4);
                    float fv; std::memcpy(&fv, &w, 4);
                    std::fprintf(stderr, " %9.4f", double(fv));
                }
                std::fprintf(stderr, "\n");
            }
        }
        uint32_t bad = 0, inner_px = 0, hl_px = 0;
        int shown = 0;
        for (int y = 0; y < ctx.h; ++y)
            for (int x = 0; x < ctx.w; ++x) {
                const size_t i = size_t(y) * size_t(ctx.w) + size_t(x);
                const uint32_t base = ref.base_pixel(x, y, sprite);
                const auto note = [&](const char* kind, uint32_t want) {
                    if (shown < 6) {
                        ++shown;
                        std::fprintf(stderr, "  [%s] x=%d y=%d 期望 0x%08x 得到 0x%08x(背景 0x%08x)\n",
                                     kind, x, y, want, fb[i], base);
                    }
                };
                if (!ref.highlight) {                    // 逐像素路径: 全图精确比对
                    if (fb[i] != base) { ++bad; note("bg", base); }
                    continue;
                }
                const uint32_t hl = blend_over(base, HL_RGBA);
                if (fb[i] == hl) ++hl_px;            // 统计高光实际覆盖到的像素
                if (strictly_inside(ref, double(x) + 0.5, double(y) + 0.5, 1.5)) {
                    ++inner_px;                          // 四边形严格内部: 必须是混合后的颜色
                    if (fb[i] != hl) { ++bad; note("hl", hl); }
                } else if (fb[i] != base && fb[i] != hl) {
                    ++bad; note("edge", base);           // 其余像素只能是"背景"或"高光"
                }
            }
        if (bad) {
            std::fprintf(stderr, "参考模型不符: %u 个像素(示例见上, 格式 x,y 期望/得到)\n", bad);
            return false;
        }
        if (ref.highlight) {
            // 高光是平行四边形 ⇒ 覆盖像素数必须与"几何面积 ∩ 画布"对得上(shear 不改变面积,
            // 但小车驶出右边时可见部分会变小)。这条断言抓的是"2D_DRAW 少画/多画/位置算错"。
            int rows = 0;
            const int expected = visible_highlight_px(ref, ctx.w, ctx.h, &rows);
            const int tol = 2 * rows + 2;                  // top-left 规则让每行边界有 ±1 像素的浮动
            if (expected >= 8 && std::abs(int(hl_px) - expected) > tol) {
                std::fprintf(stderr, "高光覆盖异常: 实际 %u 像素, 可见几何面积 %d 像素(%d 行, 容差 ±%d)\n",
                             hl_px, expected, rows, tol);
                return false;
            }
            if (expected >= 8 && inner_px == 0) {
                std::fprintf(stderr, "高光严格内部 0 像素, 但可见面积有 %d 像素: 2D_DRAW 可能没画出来\n",
                             expected);
                return false;
            }
        }
        return true;
    };

    return demo::run(argc, argv, spec);
}
