// sw/demo/anim.cpp —— 动画 demo 的 host 驱动
//
// 公共框架(demo.hpp)提供: 选项解析、LOAD_KERNEL、逐帧 PARAMS→LAUNCH→WAIT 循环、
// 帧缓冲读回、--ascii/--ppm-dir/--view 三种显示、统计与限速。这里只有 anim 独有的一半:
//   参数块布局(与 sw/kernels/anim.S 的常量区一一对应)、每帧参数、参考模型校验、--box 选项。

#include "demo.hpp"

#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>

namespace {

using namespace simu;

// HSV → 0xAARRGGBB(每帧转个色相, 让"动态变化"在静态截图里也看得出来)
uint32_t hsv(double h, double s, double v) {
    h = std::fmod(h, 360.0); if (h < 0) h += 360.0;
    const double c = v * s, x = c * (1 - std::fabs(std::fmod(h / 60.0, 2.0) - 1)), m = v - c;
    double r = 0, g = 0, b = 0;
    if (h < 60)       { r = c; g = x; }
    else if (h < 120) { r = x; g = c; }
    else if (h < 180) { g = c; b = x; }
    else if (h < 240) { g = x; b = c; }
    else if (h < 300) { r = x; b = c; }
    else              { r = c; b = x; }
    const uint32_t R = uint32_t((r + m) * 255.0 + 0.5);
    const uint32_t G = uint32_t((g + m) * 255.0 + 0.5);
    const uint32_t B = uint32_t((b + m) * 255.0 + 0.5);
    return 0xFF000000u | R << 16 | G << 8 | B;
}

// ── 每帧的参数块 ──
std::vector<uint32_t> frame_params(const demo::FrameCtx& ctx, int box) {
    const double hue = double(ctx.frame % 360) * 1.0;          // 色相每帧转 1°, 20 秒一轮
    std::vector<uint32_t> p(10, 0);
    p[0] = uint32_t(ctx.w) * uint32_t(ctx.h);                  // N
    p[1] = uint32_t(ctx.warps) * uint32_t(WARP_SIZE);          // stride_elems(驱动契约)
    p[2] = ctx.fb_base;                                        // fb_base
    p[3] = ctx.frame;                                          // frame
    p[4] = uint32_t(ctx.w - 1);                                // W_mask
    p[5] = uint32_t(std::log2(double(ctx.w)));                 // H_shift
    p[6] = uint32_t(box);                                      // box_size
    p[7] = hsv(hue, 0.55, 0.22);                               // bg0
    p[8] = hsv(hue + 45, 0.80, 0.55);                          // bg1
    p[9] = hsv(hue + 180, 0.90, 1.00);                         // box_color
    return p;
}

// 参考模型: 按 anim.S 的公式重算一帧(确认"驱动推的参数"与"内核算的画面"真的一致)
bool verify_frame(const std::vector<uint32_t>& fb, const std::vector<uint32_t>& params,
                  const demo::FrameCtx& ctx) {
    const int W = ctx.w;
    const uint32_t box = params[6];
    const uint32_t bx = (ctx.frame * 3) & uint32_t(W - 1), by = (ctx.frame * 2) & uint32_t(W - 1);
    const uint32_t bg0 = params[7], bg1 = params[8], boxc = params[9];
    for (int i = 0; i < W * ctx.h; ++i) {
        const uint32_t idx = uint32_t(i), x = idx & uint32_t(W - 1), y = idx >> uint32_t(std::log2(double(W)));
        uint32_t expect = ((((x + y + ctx.frame * 4) >> 5) & 1) ? bg1 : bg0);
        if (((x - bx) & uint32_t(W - 1)) + ((y - by) & uint32_t(W - 1)) < box) expect = boxc;
        if (fb[size_t(i)] != expect) {
            std::fprintf(stderr, "参考模型不符: 像素 %d (x=%u y=%u): 得到 0x%08x, 期望 0x%08x\n",
                         i, x, y, fb[size_t(i)], expect);
            return false;
        }
    }
    return true;
}

}  // namespace

int main(int argc, char** argv) {
    int box = 12;                                   // --box 的落点

    demo::Spec spec;
    spec.image  = "build/anim.bin";
    spec.about  = "shader 逐帧重算画面: 斜条纹背景 + 沿对角线弹跳的菱形块";
    spec.check_canvas = [](int w, int h) -> std::string {
        const bool ok = w > 0 && h > 0 && (w & (w - 1)) == 0 && w == h;
        return ok ? "" : "--w/--h 必须是 2 的幂且相等(anim.S 用 and/srl 取 x/y)";
    };
    spec.params_for = [&](const demo::FrameCtx& ctx) { return frame_params(ctx, box); };
    spec.verify     = verify_frame;
    spec.custom_opt = [&](const std::string& opt, const std::string& val) {
        if (opt != "--box") return 0;
        if (val.empty() || val[0] == '-') { std::fprintf(stderr, "--box 缺少参数\n"); return -1; }
        box = std::atoi(val.c_str());
        return 1;
    };
    spec.extra_usage = "  --box N             菱形块半径(默认 12)\n"
                       "                      画布必须方形且边长为 2 的幂(用 and/srl 取 x/y)\n";
    return demo::run(argc, argv, spec);
}
