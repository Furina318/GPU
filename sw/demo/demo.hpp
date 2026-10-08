// sw/demo/demo.hpp —— host 驱动 demo 的公共框架
//
// "host 逐帧推参数 + 显示"这一大类 demo, 独有的部分只有三件:
//   1. 参数块: 布局(与内核常量区一一对应)与每帧怎么算
//   2. 参考模型校验
//   3. demo 特有选项
// 其余样板 —— 选项解析、镜像加载(LOAD_KERNEL)、逐帧 PARAMS→LAUNCH→WAIT 循环、
// 帧缓冲读回、--ascii/--ppm-dir/--view 三种显示、统计与限速 —— 全在本文件。
//
// 框架约定:
//   - 一帧 = PARAMS(0, params) → LAUNCH(entry, warps, flags, signal_id=0) → WAIT(SEM[0] >= frame+1)
//   - signal_id=0 被框架占用: LAUNCH 完成时 SEM[0] += 1(CMDS §4.3), 出错不打信号量
//   - 退出码: 0 正常 / 1 用法·命令错误 / 2 SM 架构错误 / 3 画面校验不符

#pragma once

#include "cp.hpp"
#include "log.hpp"
#include "machine.hpp"
#if defined(SIMU_HAVE_SDL)
#include "sdl_show.hpp"
#endif

#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <functional>
#include <string>
#include <thread>
#include <vector>

namespace demo {

// 每帧推参数/校验时的上下文(由框架填充)
struct FrameCtx {
    uint32_t frame = 0;      // 帧号(从 0 起)
    int      w = 0, h = 0;   // 画布
    int      warps = 0;      // 本帧 LAUNCH 的 warp 数
    uint32_t fb_base = 0;    // 帧缓冲基址
};

struct Spec {
    std::string image = "build/anim.bin";
    int      w = 64, h = 64, warps = 8;      // 默认画布/warp(可被 --w/--h/--warps 覆盖)
    int      frames = 120, fps = 30;         // 0 = 一直跑(给 --view 时默认 0)/不限速
    int      scale = 6;                      // --view 的放大倍数
    std::string about;                       // demo描述

    // 预置: Machine 建好之后、加载镜像之前调用(如把精灵位图写进全局内存)
    std::function<void(simu::Machine&)> setup;

    // 画布合法性: 返回空 = 合法, 否则返回错误信息(框架会补上"当前 WxH")
    std::function<std::string(int w, int h)> check_canvas;
    // 每帧参数块: 与内核常量布局一一对应(驱动契约: stride 类参数必须 = warps*8)
    std::function<std::vector<uint32_t>(const FrameCtx&)> params_for;
    // 参考模型: 逐像素校验(空 = 不校验)
    std::function<bool(const std::vector<uint32_t>& fb, const std::vector<uint32_t>& params,
                       const FrameCtx&)> verify;
    // 自定义选项: opt 如 "--box", val 为紧随的参数(无参数时 val 为空字符串)。
    //   返回 1  = 已处理(val 不以 '-' 开头时框架顺带消费它)
    //   返回 0  = 不是我的选项(框架报"未知选项")
    //   返回 -1 = 已自行打印错误, 解析中止
    std::function<int(const std::string& opt, const std::string& val)> custom_opt;
    std::string extra_usage;                 // 追加进 --help 的段落
};

// 运行 demo: 解析选项 → LOAD_KERNEL → 逐帧循环 → 显示/校验 → 返回退出码
int run(int argc, char** argv, const Spec& spec);

namespace detail {

struct Options {
    std::string image;
    int      w = 0, h = 0, warps = 0, frames = 0, fps = 0, scale = 0;
    int      ascii_cols = 64, ascii_rows = 32;
    bool     view = false, ascii = false, stats = false, quiet = false, verify = true;
    std::string ppm_dir, log_file;
    simu::LogLevel log_level = simu::LogLevel::DEBUG;
    bool     help = false;
};

inline void usage(const char* a0, const Spec& spec) {
    std::printf(
        "用法: %s [选项]      %s\n\n"
        "显示:\n"
        "  --view              SDL2 窗口(需要 SDL2)\n"
        "  --ascii             终端真彩色输出(不需要图形环境)\n"
        "  --ppm-dir DIR       每帧写 DIR/frame_0000.ppm...\n"
        "  --scale N           SDL 窗口放大倍数(默认 %d)\n"
        "  --ascii-cols/-rows  终端字符网格(默认 64x32)\n"
        "\n画布与内容:\n"
        "  --w N --h N         画布尺寸(默认 %dx%d)\n"
        "  --warps N           启动的 warp 数(默认 %d)\n"
        "  --frames N          渲染多少帧(默认 %d;0 = 一直跑;给 --view 时默认 0)\n"
        "  --fps N             目标帧率(默认 %d;0 = 不限速)\n"
        "%s"
        "\n其他:\n"
        "  --stats             每帧打印周期数/指令数\n"
        "  --no-verify         不做画面参考模型校验\n"
        "  --quiet             只打印错误\n"
        "  --log FILE          写运行日志(--log-level trace 可看每条命令与每条指令)\n"
        "  --image FILE        内核镜像(默认 %s)\n"
        "  --help\n",
        a0, spec.about.c_str(), spec.scale, spec.w, spec.h, spec.warps, spec.frames, spec.fps,
        spec.extra_usage.c_str(), spec.image.c_str());
}

inline bool parse_args(int argc, char** argv, const Spec& spec, Options& o) {
    auto need = [&](int i, const char* what) {
        if (i + 1 >= argc) { std::fprintf(stderr, "%s 缺少参数\n", what); return false; }
        return true;
    };
    for (int i = 1; i < argc; ++i) {
        const std::string a = argv[i];
        if (a == "-h" || a == "--help") { o.help = true; return true; }
        else if (a == "--image"     ) { if (!need(i, "--image"     )) return false; o.image      = argv[++i]; }
        else if (a == "--w"         ) { if (!need(i, "--w"         )) return false; o.w          = std::atoi(argv[++i]); }
        else if (a == "--h"         ) { if (!need(i, "--h"         )) return false; o.h          = std::atoi(argv[++i]); }
        else if (a == "--warps"     ) { if (!need(i, "--warps"     )) return false; o.warps      = std::atoi(argv[++i]); }
        else if (a == "--frames"    ) { if (!need(i, "--frames"    )) return false; o.frames     = std::atoi(argv[++i]); }
        else if (a == "--fps"       ) { if (!need(i, "--fps"       )) return false; o.fps        = std::atoi(argv[++i]); }
        else if (a == "--scale"     ) { if (!need(i, "--scale"     )) return false; o.scale      = std::atoi(argv[++i]); }
        else if (a == "--ascii-cols") { if (!need(i, "--ascii-cols")) return false; o.ascii_cols = std::atoi(argv[++i]); }
        else if (a == "--ascii-rows") { if (!need(i, "--ascii-rows")) return false; o.ascii_rows = std::atoi(argv[++i]); }
        else if (a == "--ppm-dir"   ) { if (!need(i, "--ppm-dir   ")) return false; o.ppm_dir    = argv[++i]; }
        else if (a == "--log"       ) { if (!need(i, "--log"       )) return false; o.log_file   = argv[++i]; }
        else if (a == "--log-level" ) {
            if (!need(i, "--log-level")) return false;
            if (!simu::parse_log_level(argv[++i], o.log_level)) { std::fprintf(stderr, "未知日志级别\n"); return false; }
        }
        else if (a == "--view"     ) { o.view   = true; if (o.frames == 120) o.frames = 0; }   // 给窗口就默认一直跑
        else if (a == "--ascii"    )   o.ascii  = true;
        else if (a == "--stats"    )   o.stats  = true;
        else if (a == "--quiet"    )   o.quiet  = true;
        else if (a == "--no-verify")   o.verify = false;
        else if (spec.custom_opt) {
            const std::string val = i + 1 < argc ? argv[i + 1] : "";
            const int r = spec.custom_opt(a, val);
            if (r < 0) return false;
            if (r == 0) { std::fprintf(stderr, "未知选项 %s(用 --help)\n", a.c_str()); return false; }
            if (!val.empty() && val[0] != '-') ++i;
        }
        else { std::fprintf(stderr, "未知选项 %s(用 --help)\n", a.c_str()); return false; }
    }
    return true;
}

inline bool write_ppm(const std::string& path, const std::vector<uint32_t>& px, int w, int h) {
    FILE* f = std::fopen(path.c_str(), "wb");
    if (!f) return false;
    std::fprintf(f, "P6\n%d %d\n255\n", w, h);
    for (uint32_t c : px) {
        const uint8_t rgb[3] = {uint8_t(c >> 16), uint8_t(c >> 8), uint8_t(c)};
        std::fwrite(rgb, 1, 3, f);
    }
    std::fclose(f);
    return true;
}

inline std::string ascii_frame(const std::vector<uint32_t>& px, int w, int h, int cols, int rows) {
    std::string out = "\033[H";                             // 光标回左上角
    for (int r = 0; r < rows; ++r) {
        for (int c = 0; c < cols; ++c) {
            const int x = (c * w) / cols, y = (r * h) / rows;
            const uint32_t p = px[size_t(y) * size_t(w) + size_t(x)];
            char buf[40];
            std::snprintf(buf, sizeof(buf), "\033[48;2;%u;%u;%um ", (p >> 16) & 0xFF, (p >> 8) & 0xFF, p & 0xFF);
            out += buf;
        }
        out += "\033[0m\n";
    }
    return out;
}

}  // namespace detail

inline int run(int argc, char** argv, const Spec& spec) {
    using namespace simu;
    using Clock = std::chrono::steady_clock;

    // 解析选项
    detail::Options o;
    o.image = spec.image;
    o.w = spec.w; o.h = spec.h; o.warps = spec.warps;
    o.frames = spec.frames; o.fps = spec.fps; o.scale = spec.scale;
    if (!detail::parse_args(argc, argv, spec, o)) { detail::usage(argv[0], spec); return 1; }
    if (o.help) { detail::usage(argv[0], spec); return 0; }
    if (spec.check_canvas) {
        const std::string why = spec.check_canvas(o.w, o.h);
        if (!why.empty()) { std::fprintf(stderr, "%s(当前 %dx%d)\n", why.c_str(), o.w, o.h); return 1; }
    }

    logger().set_file_level(o.log_level);
    logger().set_console_level(o.quiet ? LogLevel::OFF : LogLevel::WARN);   // demo 只在自己打印进度
    if (!o.log_file.empty() && !logger().open_file(o.log_file)) {
        std::fprintf(stderr, "打不开日志文件 %s\n", o.log_file.c_str());
        return 1;
    }

    Machine gpu;
    CommandProcessor cp(gpu);
    gpu.model.div_cost = 8;
    std::string err;

    if (spec.setup) spec.setup(gpu);                     // demo 预置全局内存(精灵位图等)

    // 驱动把镜像放进全局内存, 然后走真正的 LOAD_KERNEL 命令(镜像头/CRC 都会被校验)
    if (gpu.mem.load_file_to_global(o.image.c_str(), KERNEL_IMG_BASE, &err) < 0) {
        std::fprintf(stderr, "读镜像失败: %s\n", err.c_str());
        return 1;
    }
    FILE* f = std::fopen(o.image.c_str(), "rb");
    if (!f) { std::fprintf(stderr, "打不开镜像 %s\n", o.image.c_str()); return 1; }
    std::fseek(f, 0, SEEK_END);
    const long isize = std::ftell(f);
    std::fclose(f);

    if (!cp.submit_load_kernel(KERNEL_IMG_BASE, uint32_t(isize), 0x3, &err)) {
        std::fprintf(stderr, "提交 LOAD_KERNEL 失败: %s\n", err.c_str());
        return 1;
    }
    cp.doorbell();
    if (cp.in_error()) { std::fprintf(stderr, "LOAD_KERNEL 失败\n"); return 1; }

    const uint32_t entry = gpu.mem.img[0].entry;
    const uint32_t flags = gpu.mem.img[0].flags;
    const uint32_t fb    = gpu.fb_base;

    if (!o.quiet) {
        std::printf("SimpleGPU demo: %s\n", spec.about.c_str());
        std::printf("  画布 %dx%d(= %d 像素), %d warp = %d 线程, 内核 %s\n",
                    o.w, o.h, o.w * o.h, o.warps, o.warps * WARP_SIZE, o.image.c_str());
        std::printf("  每帧: PARAMS → LAUNCH(signal) → WAIT ⇒ 画面由 shader 重算\n");
        std::printf("  显示: %s%s%s\n", o.view ? "SDL 窗口 " : "", o.ascii ? "终端 ASCII " : "",
                    o.ppm_dir.empty() ? "" : ("PPM→" + o.ppm_dir).c_str());
        std::fflush(stdout);
    }
    if (o.ascii) std::printf("\033[2J\033[?25l");           // 清屏 + 隐藏光标

#if defined(SIMU_HAVE_SDL)
    SDLViewer viewer;
    if (o.view && !viewer.init(o.w * o.scale, o.h * o.scale)) {
        std::fprintf(stderr, "SDL 初始化失败: %s(无显示环境时改用 --ascii 或 --ppm-dir)\n",
                     viewer.error() ? viewer.error() : "未知");
        return 1;
    }
#else
    if (o.view) { std::fprintf(stderr, "--view 需要 SDL2(编译时未找到), 请用 --ascii/--ppm-dir\n"); return 1; }
#endif

    std::vector<uint32_t> px(size_t(o.w) * size_t(o.h));
    std::vector<uint32_t> scaled;
    if (o.view) scaled.resize(size_t(o.w * o.scale) * size_t(o.h * o.scale));

    uint64_t total_cycles = 0, total_instr = 0;
    const Clock::time_point t_start = Clock::now();
    uint32_t frame = 0;
    int verify_fail = 0;
    bool quit = false;

    while (!quit && (o.frames == 0 || frame < uint32_t(o.frames))) {
        const Clock::time_point t0 = Clock::now();

        // 一帧 = 三条命令(参数 → 启动 → 等待依赖)。signal_id=0 让 LAUNCH 完成时 SEM[0] += 1,
        //    下一帧的 WAIT 等 SEM[0] >= frame+1 —— 这就是"顶点算完再跑下一步"的依赖写法。
        const FrameCtx ctx{frame, o.w, o.h, o.warps, fb};
        const std::vector<uint32_t> params = spec.params_for(ctx);
        if (params.empty() || !cp.submit_params(0, params, &err) ||
            !cp.submit_launch(entry, uint32_t(o.warps), flags, 0, SEM_NONE, &err) ||
            !cp.submit_wait(cp.sem_base(), frame + 1, &err)) {
            std::fprintf(stderr, "第 %u 帧提交命令失败: %s\n", frame, err.c_str());
            return 1;
        }
        cp.doorbell();
        if (cp.in_error() || gpu.sm[0].error != SmErr::NONE || gpu.sm[1].error != SmErr::NONE) {
            std::fprintf(stderr, "第 %u 帧执行失败: CP_ERROR=%s SM0=%s SM1=%s(FAULT_PC=0x%04x)\n", frame,
                         cp_err_name(cp.error()), sm_err_name(gpu.sm[0].error), sm_err_name(gpu.sm[1].error),
                         gpu.sm[0].error != SmErr::NONE ? gpu.sm[0].fault_pc : gpu.sm[1].fault_pc);
            return 2;
        }

        const uint64_t cyc = gpu.stats.cycles, ins = gpu.stats.issued;
        total_cycles += cyc;
        total_instr  += ins;

        // 读回帧缓冲(真实的 host 会读同一块内存; 这里"帧缓冲"就在模拟器的全局内存里)
        for (size_t i = 0; i < px.size(); ++i) px[i] = gpu.mem.ld_global(fb + uint32_t(i) * 4);
        if (frame == 0 && std::getenv("DEMO_DEBUG")) {
            std::fprintf(stderr, "[dbg] entry=0x%x flags=0x%x fb=0x%08x N=%u warps=%d\n",
                         entry, flags, fb, params[0], o.warps);
            std::fprintf(stderr, "[dbg] const[0..9]:");
            for (int i = 0; i < 10; ++i) std::fprintf(stderr, " 0x%08x", gpu.mem.ld_const(uint32_t(i) * 4));
            std::fprintf(stderr, "\n[dbg] fb[0..3]:");
            for (int i = 0; i < 4; ++i) std::fprintf(stderr, " 0x%08x", gpu.mem.ld_global(fb + uint32_t(i) * 4));
            std::fprintf(stderr, "\n[dbg] 命令流历史 %zu 条\n", cp.history().size());
            for (const auto& ci : cp.history())
                std::fprintf(stderr, "[dbg]   @0x%08x %s: %s\n", ci.addr, cmd_opcode_name(ci.opcode), ci.summary.c_str());
        }

        if (o.verify && spec.verify && !spec.verify(px, params, ctx)) {
            if (++verify_fail == 1)
                std::fprintf(stderr, "第 %u 帧与参考模型不一致(第一个不符像素见 --log trace)\n", frame);
        }

        if (o.view) {
            for (int y = 0; y < o.h * o.scale; ++y)
                for (int x = 0; x < o.w * o.scale; ++x)
                    scaled[size_t(y) * size_t(o.w * o.scale) + size_t(x)] =
                        px[size_t(y / o.scale) * size_t(o.w) + size_t(x / o.scale)];
#if defined(SIMU_HAVE_SDL)
            viewer.update(scaled);
            viewer.render();
            if (!viewer.poll()) quit = true;
#endif
        }
        if (o.ascii) {
            const std::string s = detail::ascii_frame(px, o.w, o.h, o.ascii_cols, o.ascii_rows);
            std::fwrite(s.data(), 1, s.size(), stdout);
            std::fflush(stdout);
        }
        if (!o.ppm_dir.empty()) {
            char path[512];
            std::snprintf(path, sizeof(path), "%s/frame_%04u.ppm", o.ppm_dir.c_str(), frame);
            if (!detail::write_ppm(path, px, o.w, o.h)) { std::fprintf(stderr, "写 %s 失败\n", path); return 1; }
        }

        const double frame_ms = std::chrono::duration<double, std::milli>(Clock::now() - t0).count();
        if (o.stats)
            std::fprintf(stderr, "帧 %4u: %6llu 周期 %6llu 条指令  仿真 %.1f 万周期/秒  本帧 %.1f ms%s\n",
                         frame, (unsigned long long)cyc, (unsigned long long)ins,
                         frame_ms > 0 ? double(cyc) / frame_ms / 10.0 : 0.0, frame_ms,
                         o.verify ? (verify_fail ? "  ✗校验" : "  ✔校验") : "");
        ++frame;

        if (o.fps > 0) {                                    // 限速: 让"帧率"像真实显示那样有意义
            const double target_ms = 1000.0 / double(o.fps);
            if (frame_ms < target_ms) std::this_thread::sleep_for(std::chrono::duration<double, std::milli>(target_ms - frame_ms));
        }
    }

    if (o.ascii) std::printf("\033[?25h\033[0m");           // 恢复光标
    const double wall = std::chrono::duration<double>(Clock::now() - t_start).count();
    std::printf("\n渲染 %u 帧: 平均 %llu 周期/帧(%llu 条 warp-指令), 仿真合计 %llu 周期;"
                "墙钟 %.2f 秒 ⇒ %.1f 帧/秒, 仿真速度 %.2f M周期/秒\n",
                frame, frame ? (unsigned long long)(total_cycles / frame) : 0,
                frame ? (unsigned long long)(total_instr / frame) : 0,
                (unsigned long long)total_cycles, wall, wall > 0 ? double(frame) / wall : 0.0,
                wall > 0 ? double(total_cycles) / wall / 1e6 : 0.0);
    if (o.verify && spec.verify) std::printf("画面校验: %s\n", verify_fail == 0 ? "全部帧与参考模型一致" : "有不符帧!");
    logger().flush();
    return verify_fail ? 3 : 0;
}

}  // namespace demo
