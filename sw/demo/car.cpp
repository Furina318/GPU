// sw/demo/car.cpp —— 小汽车 demo 的 host 驱动(精灵搬移)

#include "demo.hpp"

#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

namespace {

using namespace simu;

constexpr int      SW = 24, SH = 12;             // 精灵尺寸(与 car.S 的 sprite_w/h 一致)
constexpr uint32_t SPRITE_BASE = 0x8000'0000u;   // 精灵位图落点(DDR)

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

// 道路几何与配色(与 car.S 的常量布局对应; 校验模型共用同一份)
struct Scene {
    int      road_top = 20, road_h = 24, dash_y = 31, dash_h = 2;
    uint32_t grass = 0xFF1E4A1Eu, road = 0xFF4A4A4Au, dash = 0xFFD8D8D8u;
};

// ── 每帧的参数块(与 sw/kernels/car.S 的常量布局一一对应) ──
std::vector<uint32_t> frame_params(const demo::FrameCtx& ctx, const Scene& sc) {
    // 小车每帧前进 5px, 从画布左侧外(-SW)驶入, 越过右侧后环绕回左侧外
    const int car_x = int(ctx.frame * 5) % (ctx.w + SW) - SW;
    std::vector<uint32_t> p(17, 0);
    p[0]  = uint32_t(ctx.w) * uint32_t(ctx.h);               // N
    p[1]  = uint32_t(ctx.warps) * uint32_t(WARP_SIZE);       // stride_elems(驱动契约)
    p[2]  = ctx.fb_base;                                     // fb_base
    p[3]  = uint32_t(car_x);                                 // car_x(补码, 可为负)
    p[4]  = 26;                                              // car_y(路面 [20,44) 内)
    p[5]  = uint32_t(ctx.w - 1);                             // W_mask
    p[6]  = uint32_t(std::log2(double(ctx.w)));              // H_shift
    p[7]  = SPRITE_BASE;                                     // sprite_base
    p[8]  = SW; p[9] = SH;                                   // sprite_w/h
    p[10] = sc.grass; p[11] = sc.road; 
    p[12] = sc.dash;      // 配色
    p[13] = uint32_t(sc.road_top); 
    p[14] = uint32_t(sc.road_h);
    p[15] = uint32_t(sc.dash_y);    
    p[16] = uint32_t(sc.dash_h);
    return p;
}

}  // namespace

int main(int argc, char** argv) {
    const std::vector<uint32_t> sprite = build_sprite();
    Scene sc;

    demo::Spec spec;
    spec.image = "build/car.bin";
    spec.w = 128; spec.h = 64;               // 宽画布: W 是 2 的幂, H 任意
    spec.about = "精灵小车在公路上行驶: 背景逐像素公式 + 小车位图搬移";
    spec.check_canvas = [](int w, int h) -> std::string {
        (void)h;
        const bool ok = w > 0 && (w & (w - 1)) == 0;
        return ok ? "" : "--w 必须是 2 的幂(car.S 用 and/srl 取 x/y; --h 任意)";
    };
    // 启动时把精灵位图写进全局内存(0x8000_0000), 内核 ld.g 从这里搬
    spec.setup = [&](simu::Machine& m) {
        for (size_t i = 0; i < sprite.size(); ++i)
            m.mem.st_global(SPRITE_BASE + uint32_t(i) * 4, sprite[i]);
    };
    spec.params_for = [&](const demo::FrameCtx& ctx) { return frame_params(ctx, sc); };
    spec.extra_usage = "  --fps 0 --no-verify\n";
    return demo::run(argc, argv, spec);
}
