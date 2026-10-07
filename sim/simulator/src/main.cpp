#include "cp.hpp"
#include "debugger.hpp"
#include "log.hpp"
#include "machine.hpp"
#if defined(SIMU_HAVE_SDL)
#include "sdl_show.hpp"
#endif

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <sstream>
#include <string>
#include <vector>

using namespace simu;

struct Options {
    std::string img = "build/clear.bin";
    std::string kernel;                                   // 由镜像文件名推断
    bool        debug = false;
    std::string log_file;
    LogLevel    log_level = LogLevel::DEBUG;
    bool        verbose = false, quiet = false, trace = false;
    int         warps = -1;                               // -1 = 按内核默认
    std::map<uint32_t, uint32_t> params;                  // --param <off>=<value>
    uint64_t    max_cycles = 1u << 20;
    uint32_t    div_cost = 8;
    bool        strict_mem = false;
    bool        direct = false;                           // 不走命令流(直接 LOAD+LAUNCH)
    bool        do_check = true, do_init = true;
    bool        cp_demo = false;
    std::string cp_script;            // --cp-script: 用脚本定义整条命令流(CP 边界测试用)
    std::vector<std::pair<uint32_t,uint32_t>> write_words;   // --write-word ADDR=VAL
    std::vector<std::pair<std::string,uint32_t>> extra_imgs; // --load-extra PATH@ADDR
    bool        view = false;
    int         cp_fill = 0;          // --cp-fill N: 先塞 N 条 NOP, 用来测 §2.1 的队列回绕填充         // --view: 用 SDL2 窗口显示帧缓冲      // 在命令流尾部追加 SIGNAL/WAIT/READ_REG/FENCE/NOP/IRQ 各一条
    bool        dump_regs = false;
    std::string fb_out;
    std::string dump_spec;      // --dump ADDR,LEN[,FILE]
    int         fb_w = 640, fb_h = 480;
    bool        help = false;
};

static void usage(const char* a0) {
    std::printf(
        "用法: %s <kernel.bin> [选项]\n"
        "\n"
        "运行模式:\n"
        "  (默认)              直接运行命令流到结束, 打印统计与结果校验\n"
        "  -d, --debug         进入交互式调试器(si 单步 / c 继续 / x 看内存 / help)\n"
        "  --log FILE          运行信息写入日志文件(结构化, 可 grep)\n"
        "\n"
        "日志与输出:\n"
        "  --log-level L       trace|debug|info|warn|error   写文件的级别(默认 debug)\n"
        "  --trace             逐条指令日志(含 cycle/warp/pc); 也可在调试器里 trace on\n"
        "  --verbose           控制台也打印 debug 级日志\n"
        "  --quiet             控制台只打印 error\n"
        "  --dump-regs         运行结束后打印每个 warp 的寄存器堆\n"
        "\n"
        "内核与参数:\n"
        "  --warps N           启动的 warp 数(1..8); 默认按内核给(clear/gouraud/matmul = 4)\n"
        "  --param OFF=VALUE   覆盖常量区参数(十进制/0x 十六进制, 可重复);\n"
        "  --no-init           不预置输入缓冲(A/B/顶点缓冲保持 0)\n"
        "  --no-check          不做结果校验\n"
        "  --direct            绕过命令流, 直接 load_img + LAUNCH(对照用)\n"
        "  --max-cycles N      周期上限(默认 1048576; 同时作为 CP 的 TIMEOUT_CYCLES)\n"
        "  --div-cost N        周期模型: div/rem 占用周期(默认 8)\n"
        "  --strict-mem        访存越出已知内存区即报错(默认只警告一次)\n"
        "  --fb-out FILE       把帧缓冲导出为 PPM(默认 64x64, 用 --fb WxH 改)\n"
        "  --fb WxH            帧缓冲尺寸(默认 64x64)\n"
        "  --view              用 SDL2 窗口显示帧缓冲(需要 SDL2; 按 ESC/关窗退出)\n"
        "  --dump A,L[,FILE]   把全局内存 [A, A+L) 按字导出(十六进制+浮点), 便于脚本比对\n"
        "  -h, --help          本帮助\n"
        "\n"
        "退出码: 0 正常; 1 命令/CP 错误; 2 SM 架构错误; 3 结果校验不符\n",
        a0);
}

static bool parse_args(int argc, char** argv, Options& o) {
    auto need = [&](int i, const char* what) {
        if (i + 1 >= argc) { std::fprintf(stderr, "%s 缺少参数\n", what); return false; }
        return true;
    };
    for (int i = 1; i < argc; ++i) {
        const std::string a = argv[i];
        if (a == "-h" || a == "--help") { o.help = true; return true; }
        else if (a == "-d" || a == "--debug") o.debug = true;
        else if (a == "--log") { if (!need(i, "--log")) return false; o.log_file = argv[++i]; }
        else if (a == "--log-level") {
            if (!need(i, "--log-level")) return false;
            if (!parse_log_level(argv[++i], o.log_level)) {
                std::fprintf(stderr, "未知日志级别 %s(trace|debug|info|warn|error)\n", argv[i]);
                return false;
            }
        }
        else if (a == "--verbose") o.verbose = true;
        else if (a == "--quiet"  ) o.quiet   = true;
        else if (a == "--trace"  ) o.trace   = true;
        else if (a == "--warps"  ) { if (!need(i, "--warps")) return false; o.warps = std::atoi(argv[++i]); }
        else if (a == "--param"  ) {
            if (!need(i, "--param")) return false;
            const std::string s = argv[++i];
            const size_t eq = s.find('=');
            if (eq == std::string::npos) { std::fprintf(stderr, "--param 需要 OFF=VALUE 形式\n"); return false; }
            o.params[uint32_t(std::strtoul(s.substr(0, eq).c_str(), nullptr, 0))] =
                uint32_t(std::strtoul(s.substr(eq + 1).c_str(), nullptr, 0));
        }
        else if (a == "--direct"    ) o.direct   = true;
        else if (a == "--no-init"   ) o.do_init  = false;
        else if (a == "--no-check"  ) o.do_check = false;
        else if (a == "--cp-demo"   ) o.cp_demo  = true;
        else if (a == "--view"      ) o.view     = true;
        else if (a == "--cp-script" ) { if (!need(i, "--cp-script")) return false; o.cp_script = argv[++i]; }
        else if (a == "--write-word") {
            if (!need(i, "--write-word")) return false;
            const std::string s2 = argv[++i];
            const size_t eq = s2.find('=');
            if (eq == std::string::npos) { std::fprintf(stderr, "--write-word 需要 ADDR=VAL\n"); return false; }
            o.write_words.push_back({uint32_t(std::strtoul(s2.substr(0, eq).c_str(), nullptr, 0)),
                                     uint32_t(std::strtoul(s2.substr(eq + 1).c_str(), nullptr, 0))});
        }
        else if (a == "--load-extra") {
            if (!need(i, "--load-extra")) return false;
            const std::string s2 = argv[++i];
            const size_t at = s2.find('@');
            if (at == std::string::npos) { std::fprintf(stderr, "--load-extra 需要 PATH@ADDR\n"); return false; }
            o.extra_imgs.push_back({s2.substr(0, at), uint32_t(std::strtoul(s2.substr(at + 1).c_str(), nullptr, 0))});
        }
        else if (a == "--cp-fill"   ) { if (!need(i, "--cp-fill")) return false; o.cp_fill = std::atoi(argv[++i]); }
        else if (a == "--max-cycles") { if (!need(i, "--max-cycles")) return false; o.max_cycles = std::strtoull(argv[++i], nullptr, 0); }
        else if (a == "--div-cost"  ) { if (!need(i, "--div-cost")) return false; o.div_cost = uint32_t(std::atoi(argv[++i])); }
        else if (a == "--strict-mem") o.strict_mem = true;
        else if (a == "--dump-regs" ) o.dump_regs = true;
        else if (a == "--fb-out"    ) { if (!need(i, "--fb-out")) return false; o.fb_out = argv[++i]; }
        else if (a == "--dump"      ) { if (!need(i, "--dump")) return false; o.dump_spec = argv[++i]; }
        else if (a == "--fb"        ) {
            if (!need(i, "--fb")) return false;
            const std::string s = argv[++i];
            const size_t x = s.find('x');
            if (x == std::string::npos) { std::fprintf(stderr, "--fb 需要 WxH 形式\n"); return false; }
            o.fb_w = std::atoi(s.substr(0, x).c_str());
            o.fb_h = std::atoi(s.substr(x + 1).c_str());
        }
        else if (!a.empty() && a[0] != '-') o.img = a;
        else { std::fprintf(stderr, "未知选项 %s(用 --help)\n", a.c_str()); return false; }
    }
    // 内核名 = 文件名去掉目录与扩展名
    const size_t slash = o.img.find_last_of('/');
    std::string base = slash == std::string::npos ? o.img : o.img.substr(slash + 1);
    const size_t dot = base.find_last_of('.');
    o.kernel = dot == std::string::npos ? base : base.substr(0, dot);
    return true;
}

static uint32_t fbits(float f) { uint32_t u; std::memcpy(&u, &f, 4); return u; }
static float    as_fp(uint32_t u) { float f; std::memcpy(&f, &u, 4); return f; }

struct Profile {
    int warps = 4;
    std::map<uint32_t, uint32_t> params;
};

// 常量区设置
static Profile profile_for(const std::string& k, int warps_override, int fb_w, int fb_h) {
    Profile p;
    p.warps = 4;
    if (k == "clear") {
        p.params[0] = fb_w * fb_h;              // N = 像素数
        p.params[4] = 0xFF00FF00u;             // color = RGBA8888
        p.params[8] = 0;                       // stride_elems, 下面按 warps*8 填(驱动契约)
    } else if (k == "gouraud") {
        p.params[0]  = 0x1000'0000u;           // in_base(顶点输入缓冲)
        p.params[4]  = 0x1000'1000u;           // out_base(顶点输出缓冲)
        p.params[8]  = 32;                     // N ≤ num_warps*8
        p.params[12] = fbits(1.5f);            // m00
        p.params[16] = fbits(0.25f);           // m01
        p.params[20] = fbits(-0.5f);           // m10
        p.params[24] = fbits(2.0f);            // m11
        p.params[28] = fbits(3.0f);            // tx
        p.params[32] = fbits(-1.0f);           // ty
        p.params[36] = 24;                     // stride_bytes
    } else if (k == "matmul") {
        p.params[0]  = 0x1000'0000u;           // a_base(A 列主序, M×K)
        p.params[4]  = 0x1000'1000u;           // b_base(B 行主序, K×N)
        p.params[8]  = 0x1000'2000u;           // c_base(C 行主序, M×N)
        p.params[12] = 32;                     // M
        p.params[16] = 32;                     // N(本内核要求 32)
        p.params[20] = 32;                     // K
    }
    if (warps_override > 0) p.warps = warps_override;
    if (k == "clear") p.params[8] = uint32_t(p.warps * WARP_SIZE);
    return p;
}

static const uint32_t MM_N = 32;      // matmul 内核要求的方阵边长

// 数据初始化
static void init_buffers(Machine& m, const std::string& k, const std::map<uint32_t, uint32_t>& P) {
    auto gp = [&](uint32_t off, uint32_t def) { auto it = P.find(off); return it == P.end() ? def : it->second; };
    // 初始化顶点输入缓冲(A/B/顶点缓冲)与矩阵 A/B
    if (k == "gouraud") {
        const uint32_t in = gp(0, 0x1000'0000u);
        for (uint32_t i = 0; i < 32; ++i) {
            const uint32_t v[6] = {fbits(float(i) * 0.5f), fbits(float(i) * 0.25f), fbits(float(i)),
                                   fbits(float(255 - i)), fbits(float(i * 2)), fbits(255.0f)};
            for (uint32_t j = 0; j < 6; ++j) m.mem.st_global(in + (i * 6 + j) * 4, v[j]);
        }
        LogI("初始化顶点输入缓冲 0x%08x: 32 个顶点 {x=0.5i, y=0.25i, r=i, g=255-i, b=2i, a=255}", in);
    }
    // matmul 内核要求 A 列主序, B 行主序, 且输入是小整数 
    else if (k == "matmul") {
        const uint32_t A = gp(0, 0x1000'0000u), B = gp(4, 0x1000'1000u);
        for (uint32_t kk = 0; kk < MM_N; ++kk) {
            for (uint32_t i = 0; i < MM_N; ++i) {          // A[k][i], 列主序 ⇒ 偏移 (k*M + i)
                const float a = float((i * 7 + kk * 3) % 5) - 2.0f;
                m.mem.st_global(A + (kk * MM_N + i) * 4, fbits(a));
            }
            for (uint32_t j = 0; j < MM_N; ++j) {          // B[k][j], 行主序 ⇒ 偏移 (k*N + j)
                const float b = float((j * 5 + kk * 2) % 7) - 3.0f;
                m.mem.st_global(B + (kk * MM_N + j) * 4, fbits(b));
            }
        }
        LogI("初始化 A(列主序 0x%08x)与 B(行主序 0x%08x): 输入是小整数 ⇒ 参考结果可按位比较", A, B);
    }
}

static uint32_t rd(Machine& m, uint32_t addr) { return m.mem.ld_global(addr); }
static float    rdf(Machine& m, uint32_t addr) { return as_fp(rd(m, addr)); }

static std::string fmt_bits(uint32_t u) {
    char b[64];
    std::snprintf(b, sizeof(b), "0x%08x(%.6g)", u, double(as_fp(u)));
    return b;
}

// 校验
static int check_result(Machine& m, const std::string& k, const std::map<uint32_t, uint32_t>& P) {
    auto gp = [&](uint32_t off, uint32_t def) { auto it = P.find(off); return it == P.end() ? def : it->second; };

    if (k == "clear") {
        const uint32_t N = gp(0, 4096), color = gp(4, 0xFF00FF00u);
        uint32_t bad = 0, first = 0;
        for (uint32_t i = 0; i < N; ++i)
            if (rd(m, m.fb_base + i * 4) != color) { if (!bad) first = i; ++bad; }
        if (bad) {
            LogE("clear 校验失败: %u/%u 个像素不符; 首个 @ 像素 %u(地址 0x%08x) = 0x%08x, 期望 0x%08x",
                 bad, N, first, m.fb_base + first * 4, rd(m, m.fb_base + first * 4), color);
            return 3;
        }
        LogI("clear 校验通过: 帧缓冲 %u 个像素全部 = 0x%08x", N, color);
        return 0;
    }

    if (k == "gouraud") {
        const uint32_t in = gp(0, 0x1000'0000u), out = gp(4, 0x1000'1000u), N = gp(8, 32);
        const float m00 = as_fp(gp(12, 0)), m01 = as_fp(gp(16, 0)), m10 = as_fp(gp(20, 0));
        const float m11 = as_fp(gp(24, 0)), tx = as_fp(gp(28, 0)), ty = as_fp(gp(32, 0));
        uint32_t bad = 0, first = 0;
        for (uint32_t i = 0; i < N; ++i) {
            const float x = rdf(m, in + i * 24), y = rdf(m, in + i * 24 + 4);
            // 与内核完全相同的运算顺序(含单次舍入的 fma), 否则尾位对不上
            const float xe = std::fma(x, m00, y * m01) + tx;
            const float ye = std::fma(y, m11, x * m10) + ty;
            bool ok = rd(m, out + i * 24) == fbits(xe) && rd(m, out + i * 24 + 4) == fbits(ye);
            for (int c = 2; c < 6 && ok; ++c)                     // 颜色 4 个分量透传
                ok = rd(m, out + i * 24 + c * 4) == rd(m, in + i * 24 + c * 4);
            if (!ok) { if (!bad) first = i; ++bad; }
        }
        if (bad) {
            const uint32_t i = first;
            const float x = rdf(m, in + i * 24), y = rdf(m, in + i * 24 + 4);
            LogE("gouraud 校验失败: %u/%u 个顶点不符; 首个顶点 %u:", bad, N, i);
            LogE("  输入 (%.6g, %.6g) 期望 (%.6g, %.6g), 得到 (x'=%s, y'=%s)", double(x), double(y),
                 double(std::fma(x, m00, y * m01) + tx), double(std::fma(y, m11, x * m10) + ty),
                 fmt_bits(rd(m, out + i * 24)).c_str(), fmt_bits(rd(m, out + i * 24 + 4)).c_str());
            return 3;
        }
        LogI("gouraud 校验通过: %u 个顶点的仿射变换与颜色透传全部按位一致", N);
        return 0;
    }

    if (k == "matmul") {
        const uint32_t A = gp(0, 0x1000'0000u), B = gp(4, 0x1000'1000u), C = gp(8, 0x1000'2000u);
        uint32_t bad = 0, first = 0;
        for (uint32_t i = 0; i < MM_N; ++i) {
            for (uint32_t j = 0; j < MM_N; ++j) {
                float acc = 0.0f;
                for (uint32_t kk = 0; kk < MM_N; ++kk)
                    acc = std::fma(rdf(m, A + (kk * MM_N + i) * 4), rdf(m, B + (kk * MM_N + j) * 4), acc);
                if (rd(m, C + (i * MM_N + j) * 4) != fbits(acc)) { if (!bad) first = i * MM_N + j; ++bad; }
            }
        }
        if (bad) {
            const uint32_t i = first / MM_N, j = first % MM_N;
            LogE("matmul 校验失败: %u/%u 个元素不符; 首个 C[%u][%u] 得到 %s",
                 bad, MM_N * MM_N, i, j, fmt_bits(rd(m, C + first * 4)).c_str());
            return 3;
        }
        LogI("matmul 校验通过: C=A×B 的 %u 个元素全部按位一致(%u×%u×%u = %u 次 MAC)",
             MM_N * MM_N, MM_N, MM_N, MM_N, MM_N * MM_N * MM_N);
        return 0;
    }

    LogI("未知内核 '%s': 跳过结果校验(可用 --param 传参数, 用调试器 x 命令或 --fb-out 看内存)",
         k.c_str());
    return 0;
}

// 打印启动信息
static void print_banner(const Options& o, Machine& m, CommandProcessor& cp,
                         const std::vector<uint8_t>& img_bytes, const std::string& img_name) {
    (void)o;
    char fl[128] = "";
    uint32_t magic = 0, entry = 0, isize = 0, flags = 0, crc = 0, csize = 0;
    if (img_bytes.size() >= 32) {
        auto rd32 = [&](size_t off) {
            return uint32_t(img_bytes[off]) | uint32_t(img_bytes[off + 1]) << 8 |
                   uint32_t(img_bytes[off + 2]) << 16 | uint32_t(img_bytes[off + 3]) << 24;
        };
        magic = rd32(0); entry = rd32(4); isize = rd32(8);
        flags = rd32(12); crc = rd32(16); csize = rd32(20);
        const struct { uint32_t bit; const char* n; } fb[] = {
            {1, "USES_BARRIER"}, {2, "USES_SHARED"}, {4, "USES_GLOBAL_WRITE"}, {8, "REQUIRES_SINGLE_SM"}};
        for (const auto& b : fb) {
            if (!(flags & b.bit)) continue;
            if (fl[0]) std::strncat(fl, "|", sizeof(fl) - std::strlen(fl) - 1);
            std::strncat(fl, b.n, sizeof(fl) - std::strlen(fl) - 1);
        }
    }
    std::printf("\033[1;32mSimpleGPU 模拟器 \033[0m\n");
    std::printf(" 硬件规模 : %d SM × %d warp × %d lane = 最多 %d 线程并行\n",
                NUM_SM, WARPS_PER_SM, WARP_SIZE, NUM_SM * WARPS_PER_SM * WARP_SIZE);
    std::printf("            指令 SRAM %dKB/SM(%d 条), 共享内存 %dKB/SM\n",
                INSTR_SRAM_SIZE / 1024, INSTR_WORDS, SHARED_SIZE / 1024);
    std::printf(" 周期模型 : 每 SM 每周期发射 %u 条; div/rem 占用 %u 周期;\n"
                "            共享内存同 bank 异地址按 +1 拍/额外地址串行化\n",
                m.model.issue_per_sm, m.model.div_cost);
    std::printf(" 内核镜像 : %s\n", img_name.c_str());
    std::printf("            magic=0x%08x(%s) entry=0x%03x image_size=%u code_size=%u crc=0x%08x\n",
                magic, magic == IMG_MAGIC ? "SGK1" : "非法!", entry, isize, csize, crc);
    std::printf("            image_flags=0x%x (%s)   SM_KERNEL_MASK=0x%x\n", flags,
                fl[0] ? fl : "无", 0);
    std::printf(" 驱动/CP  : CMD_QUEUE_BASE=0x%08x SIZE=%u  SEM_BASE=0x%08x  TIMEOUT_CYCLES=%u\n",
                cp.queue_base(), cp.queue_size(), cp.sem_base(), cp.timeout_cycles());
    std::printf("            CONST_BASE=0x%08x(4KB)  FB_BASE=0x%08x  DDR_BASE=0x%08x\n",
                CONST_BASE, m.fb_base, DDR_BASE);
    std::printf("--------------------------------------------------------------------------------\n");
}

// 从环形队列里把命令流读出来打印
static void print_cmd_stream(Machine& m, CommandProcessor& cp) {
    const uint32_t qend = cp.queue_base() + cp.queue_size();
    int total = 0;
    for (uint32_t q = cp.head(); q != cp.tail();) {
        const uint32_t len = rd(m, q) >> 16;
        if (len < 2) break;
        q += len * 4;
        if (q >= qend) q -= cp.queue_size();
        ++total;
    }
    std::printf(" 命令流   : %u 字节, %d 条命令, 区间 [0x%08x, 0x%08x)\n",
                (cp.tail() - cp.head() + cp.queue_size()) % cp.queue_size(), total, cp.head(), cp.tail());
    uint32_t p = cp.head();
    int idx = 0;
    while (p != cp.tail()) {
        const uint32_t w0 = rd(m, p), w1 = rd(m, p + 4);
        const uint32_t len = w0 >> 16;
        const uint8_t op = uint8_t(w0), cfl = uint8_t(w0 >> 8);
        const uint16_t sig = uint16_t(w1 >> 16), wait = uint16_t(w1);
        if (len < 2) break;
        const std::string s_sig = sig == SEM_NONE ? "0xFFFF" : std::to_string(sig);
        const std::string s_wait = wait == SEM_NONE ? "0xFFFF" : std::to_string(wait);
        std::printf("   [%d] @0x%08x %-12s len=%-3u cmd_flags=0x%x signal=%s wait=%s\n",
                    idx, p, cmd_opcode_name(op), len, cfl, s_sig.c_str(), s_wait.c_str());
        if (op == CMD_LOAD_KERNEL)
            std::printf("        src_addr=0x%08x size=%u sm_mask=0x%x\n",
                        rd(m, p + 8), rd(m, p + 12), rd(m, p + 16));
        else if (op == CMD_PARAMS) {
            const uint32_t off = rd(m, p + 8) & 0xFFFFu, n = rd(m, p + 12) & 0xFFFFu;
            std::printf("        常量区 0x%08x + 0x%03x, N=%u:", CONST_BASE, off, n);
            for (uint32_t i = 0; i < n && i < 12; ++i)
                std::printf(" [%u]=0x%08x", off / 4 + i, rd(m, p + 16 + i * 4));
            std::printf("%s\n", n > 12 ? " ..." : "");
        } else if (op == CMD_LAUNCH) {
            const uint32_t pc = rd(m, p + 8), nw = rd(m, p + 12) & 0xFFu, lf = rd(m, p + 16);
            std::printf("        kernel_pc=0x%03x num_warps=%u(%u 线程) launch_flags=0x%x\n",
                        pc, nw, nw * WARP_SIZE, lf);
        }
        p += len * 4;
        if (p >= qend) p -= cp.queue_size();
        ++idx;
    }
    std::printf("--------------------------------------------------------------------------------\n");
}

static void print_launch_summary(Machine& m) {
    std::printf(" LAUNCH 分发: %u 个 warp × %d lane = %u 个线程\n", m.num_warps(), WARP_SIZE,
                unsigned(m.threads()));
    for (uint32_t i = 0; i < m.num_warps(); ++i)
        std::printf("   warp %u → SM%d 槽 %d   PC=0x%03x  (warp 内 lane 0..%d)\n",
                    i, m.warp_sm(int(i)), m.warp_slot(int(i)), m.kernel_pc(), WARP_SIZE - 1);
    std::printf("   SM0 常驻 %d 个 warp, SM1 常驻 %d 个 warp; 共享内存按 warp 私有分区\n",
                m.sm[0].resident, m.sm[1].resident);
    std::printf("================================================================================\n");
}

static void print_report(Machine& m, CommandProcessor& cp) {
    const RunStats& s = m.stats;
    int act_sm = 0;
    for (int t = 0; t < NUM_SM; ++t) if (m.sm[t].resident > 0) ++act_sm;
    const double ipc_sm = (s.cycles && act_sm) ? double(s.issued) / double(s.cycles) / act_sm : 0.0;
    std::printf("\n--------------------------------------------------------------------------------\n");
    std::printf(" 运行结果\n");
    std::printf("   结束原因 : %s%s%s\n", m.stop().name(),
                m.stop().detail.empty() ? "" : " —— ", m.stop().detail.c_str());
    std::printf("   周期     : %llu   活跃 SM %d 个 ⇒ 每 SM 每周期发射 %.2f 条\n",
                (unsigned long long)s.cycles, act_sm, ipc_sm);
    std::printf("   指令     : %llu 条 warp-指令", (unsigned long long)s.issued);
    for (int i = 0; i < int(m.num_warps()); ++i)
        std::printf("%s w%d=%llu", i ? "," : " (", i, (unsigned long long)s.retired[i]);
    if (m.num_warps()) std::printf(")");
    std::printf("; FP 指令 %llu 条 ⇒ %.1f 个 lane-FMA/周期\n", (unsigned long long)s.fp_ops,
                s.cycles ? double(s.fp_ops) * WARP_SIZE / double(s.cycles) : 0.0);
    std::printf("   barrier  : %llu 次, 等待造成 %llu 个 warp-周期空转\n",
                (unsigned long long)s.barriers, (unsigned long long)s.issue_stall_barrier);
    std::printf("   发散     : %llu 次压栈 / %llu 次收敛, SIMT 栈最深 %llu/8\n",
                (unsigned long long)s.divergences, (unsigned long long)s.reconvergences,
                (unsigned long long)s.stack_max);
    std::printf("   访存     : ld.g %llu, st.g %llu (共 %llu 个 32B 行请求 ⇒ 平均每次 %.2f 行)\n",
                (unsigned long long)s.ld_global, (unsigned long long)s.st_global,
                (unsigned long long)s.gmem_lines,
                (s.ld_global + s.st_global) ? double(s.gmem_lines) / double(s.ld_global + s.st_global) : 0.0);
    std::printf("              ld.s %llu, st.s %llu (bank 冲突额外 %llu 拍)\n",
                (unsigned long long)s.ld_shared, (unsigned long long)s.st_shared,
                (unsigned long long)s.shared_extra_cycles);
    if (s.unmapped_access)
        std::printf("              已知内存区之外的访问 %llu 次(驱动自分配缓冲属正常)\n",
                    (unsigned long long)s.unmapped_access);
    std::printf("   状态     : SM_DONE=0x%x SM_KERNEL_MASK=0x%x SM0_ERROR=%s SM1_ERROR=%s CP_ERROR=%s IRQ=0x%x\n",
                m.mmio_read(MMIO_SM_DONE), m.mmio_read(MMIO_SM_KERNEL_MASK), sm_err_name(m.sm[0].error),
                sm_err_name(m.sm[1].error), cp_err_name(cp.error()), cp.irq_status());
    for (int t = 0; t < NUM_SM; ++t) {
        if (m.sm[t].error == SmErr::NONE) continue;
        std::printf("   SM%d 出错 @FAULT_PC=0x%04x (warp %d):\n", t, m.sm[t].fault_pc, m.sm[t].fault_warp);
        uint32_t inst = 0;
        if (m.mem.fetch(t, m.sm[t].fault_pc, inst)) {
            DecodedInst d;
            if (decode(inst, d))
                std::printf("     0x%04x  0x%08x  %s\n", m.sm[t].fault_pc, inst, itrace(d, m.sm[t].fault_pc).c_str());
            else
                std::printf("     0x%04x  0x%08x  <非法指令>\n", m.sm[t].fault_pc, inst);
        }
    }
    if (!s.icount.empty()) {
        std::vector<std::pair<std::string, uint64_t>> v(s.icount.begin(), s.icount.end());
        std::sort(v.begin(), v.end(), [](const std::pair<std::string, uint64_t>& a,
                                         const std::pair<std::string, uint64_t>& b) { return a.second > b.second; });
        std::printf("   指令分布 : ");
        int n = 0;
        for (const auto& kv : v) {
            std::printf("%s=%llu ", kv.first.c_str(), (unsigned long long)kv.second);
            if (++n % 6 == 0) std::printf("\n              ");
        }
        std::printf("\n");
    }
    std::printf("--------------------------------------------------------------------------------\n");
}

static bool write_ppm(const std::string& path, Machine& m, int w, int h) {
    FILE* f = std::fopen(path.c_str(), "wb");
    if (!f) return false;
    std::fprintf(f, "P6\n%d %d\n255\n", w, h);
    for (int i = 0; i < w * h; ++i) {
        const uint32_t px = m.mem.ld_global(m.fb_base + uint32_t(i) * 4);
        const uint8_t rgb[3] = {uint8_t(px >> 16), uint8_t(px >> 8), uint8_t(px)};
        std::fwrite(rgb, 1, 3, f);
    }
    std::fclose(f);
    return true;
}

// --dump ADDR,LEN[,FILE]: 把一段全局内存导出为文本(每行 "地址 十六进制 浮点")
static int dump_region(Machine& m, const std::string& spec) {
    const size_t c1 = spec.find(',');
    if (c1 == std::string::npos) { std::fprintf(stderr, "--dump 需要 ADDR,LEN[,FILE] 形式\n"); return 1; }
    const size_t c2 = spec.find(',', c1 + 1);
    const uint32_t addr = uint32_t(std::strtoul(spec.substr(0, c1).c_str(), nullptr, 0));
    const uint32_t len  = uint32_t(std::strtoul(spec.substr(c1 + 1, c2 - c1 - 1).c_str(), nullptr, 0));
    FILE* out = stdout;
    if (c2 != std::string::npos) {
        const std::string path = spec.substr(c2 + 1);
        out = std::fopen(path.c_str(), "w");
        if (!out) { std::fprintf(stderr, "打不开导出文件 %s\n", path.c_str()); return 1; }
    }
    for (uint32_t i = 0; i < len; ++i) {
        const uint32_t a = addr + i * 4, v = m.mem.ld_global(a);
        std::fprintf(out, "0x%08x 0x%08x %.9g\n", a, v, double(as_fp(v)));
    }
    if (out != stdout) { std::fclose(out); LogI("内存已导出: %s (0x%08x + %u 字)", spec.substr(c2 + 1).c_str(), addr, len); }
    return 0;
}

// --cp-script: 把文件里的每一行变成一条命令(CP 边界/异常路径的测试入口)
static bool run_cp_script(CommandProcessor& cp, Machine& gpu, const std::string& path, std::string* err) {
    FILE* f = std::fopen(path.c_str(), "r");
    if (!f) { *err = "打不开命令脚本 " + path; return false; }
    char line[1024];
    int lineno = 0;
    bool ok = true;
    while (std::fgets(line, sizeof(line), f)) {
        ++lineno;
        std::string t = line;
        const size_t hash = t.find('#');
        if (hash != std::string::npos) t = t.substr(0, hash);
        std::istringstream is(t);
        std::vector<std::string> tok;
        std::string w;
        while (is >> w) tok.push_back(w);
        if (tok.empty()) continue;
        auto num = [&](size_t i) { return uint32_t(std::strtoul(tok[i].c_str(), nullptr, 0)); };
        const std::string& c = tok[0];
        bool r = true;
        if (c == "nop" && tok.size() >= 2)                                  r = cp.submit_nop(num(1), err);
        else if (c == "load_kernel" && tok.size() >= 4)                      r = cp.submit_load_kernel(num(1), num(2), uint8_t(num(3)), err);
        else if (c == "params" && tok.size() >= 3) {
            std::vector<uint32_t> vals;
            for (size_t i = 2; i < tok.size(); ++i) vals.push_back(num(i));
            r = cp.submit_params(num(1), vals, err);
        }
        else if (c == "launch" && tok.size() >= 4) {
            const uint16_t sig = tok.size() > 4 ? uint16_t(num(4)) : SEM_NONE;
            const uint16_t wait = tok.size() > 5 ? uint16_t(num(5)) : SEM_NONE;
            r = cp.submit_launch(num(1), num(2), num(3), sig, wait, err);
        }
        else if (c == "write_reg" && tok.size() >= 3)                        r = cp.submit_write_reg(num(1), num(2), err);
        else if (c == "read_reg" && tok.size() >= 3)                         r = cp.submit_read_reg(num(1), num(2), err);
        else if (c == "signal" && tok.size() >= 3)                           r = cp.submit_signal(num(1), num(2), err);
        else if (c == "wait" && tok.size() >= 3)                             r = cp.submit_wait(num(1), num(2), err);
        else if (c == "fence")                                               r = cp.submit_fence(err);
        else if (c == "irq" && tok.size() >= 2)                              r = cp.submit_irq(uint8_t(num(1)), err);
        else if (c == "gemm" && tok.size() >= 17) {
            uint32_t d[16];
            for (int i = 0; i < 16; ++i) d[i] = num(size_t(i + 1));
            r = cp.submit_gemm(d, err);
        }
        else if (c == "mmio" && tok.size() >= 3) {   // host 直接写 MMIO(与 WRITE_REG 等价, CMDS §6.2/§7)
            const uint32_t a2 = num(1);
            if (a2 >= MMIO_END) { *err = "MMIO 偏移越界"; r = false; }
            else { LogI("host 直接写 MMIO 0x%02x ← 0x%x", a2, num(2)); gpu.mmio_write(a2, num(2)); continue; }
        }
        else if (c == "doorbell") { cp.doorbell(); continue; }
        else r = (*err = "第 " + std::to_string(lineno) + " 行: 未知命令或不完整的参数: " + c, false);
        if (!r) {
            std::fprintf(stderr, "命令脚本 %s 第 %d 行失败: %s\n", path.c_str(), lineno, err->c_str());
            ok = false;
            break;
        }
        LogD("脚本第 %d 行: %s", lineno, c.c_str());
    }
    std::fclose(f);
    return ok;
}

int main(int argc, char** argv) {
    Options o;
    if (!parse_args(argc, argv, o)) { usage(argv[0]); return 1; }
    if (o.help) { usage(argv[0]); return 0; }

    logger().set_file_level(o.log_level);
    logger().set_console_level(o.quiet ? LogLevel::ERR
                                       : (o.verbose || o.trace ? LogLevel::TRACE : LogLevel::INFO));
    if (o.trace) logger().set_ring_level(LogLevel::TRACE);
    if (!o.log_file.empty() && !logger().open_file(o.log_file)) {
        std::fprintf(stderr, "打不开日志文件 %s\n", o.log_file.c_str());
        return 1;
    }
    LogI("SimpleGPU started: %s", argv[0]);

    // 读镜像
    std::vector<uint8_t> img;
    {
        FILE* f = std::fopen(o.img.c_str(), "rb");
        if (!f) { std::fprintf(stderr, "Can't open image %s\n", o.img.c_str()); return 1; }
        std::fseek(f, 0, SEEK_END);
        const long n = std::ftell(f);
        std::fseek(f, 0, SEEK_SET);
        if (n <= 0) { std::fclose(f); std::fprintf(stderr, "Image is empty\n"); return 1; }
        img.resize(size_t(n));
        if (std::fread(img.data(), 1, img.size(), f) != img.size()) {
            std::fclose(f);
            std::fprintf(stderr, "Image read incomplete\n");
            return 1;
        }
        std::fclose(f);
    }

    Machine gpu;
    CommandProcessor cp(gpu);
    cp.set_timeout_cycles(uint32_t(o.max_cycles));     // --max-cycles 同时作为 CP 的 TIMEOUT_CYCLES(§6.7/§7)
    gpu.model.div_cost = o.div_cost;
    gpu.strict_mem = o.strict_mem;

    const Profile prof = profile_for(o.kernel, o.warps, o.fb_w, o.fb_h);
    std::map<uint32_t, uint32_t> P = prof.params;
    for (const auto& kv : o.params) P[kv.first] = kv.second;

    print_banner(o, gpu, cp, img, o.img);
    cp.set_launch_hook([&] { print_launch_summary(gpu); });
    if (o.do_init) init_buffers(gpu, o.kernel, P);

    const uint32_t img_flags = uint32_t(img[12]) | uint32_t(img[13]) << 8 |
                               uint32_t(img[14]) << 16 | uint32_t(img[15]) << 24;
    std::string err;
    bool cp_script_mode = false;

    for (const auto& kv : o.write_words) gpu.mem.st_global(kv.first, kv.second);   // host 预置内存
    for (const auto& e : o.extra_imgs) {
        if (gpu.mem.load_file_to_global(e.first.c_str(), e.second, &err) < 0) {
            std::fprintf(stderr, "Can't load %s @0x%08x: %s\n", e.first.c_str(), e.second, err.c_str());
            return 1;
        }
        LogI("已把 %s 写入全局内存 0x%08x(用于多镜像测试)", e.first.c_str(), e.second);
    }

    if (!o.direct) {
        // ① 驱动把镜像落到全局内存的镜像区(CMDS §7.2)
        if (gpu.mem.load_file_to_global(o.img.c_str(), KERNEL_IMG_BASE, &err) < 0) {
            std::fprintf(stderr, "镜像写入全局内存失败: %s\n", err.c_str());
            return 1;
        }
        if (!o.cp_script.empty()) {                 // 脚本自己定义命令流(替掉内置的三条)
            LogI("使用命令脚本 %s 定义命令流(§2.4 的边界用例靠它覆盖)", o.cp_script.c_str());
            if (!run_cp_script(cp, gpu, o.cp_script, &err)) return 1;
            cp_script_mode = true;
        }
        if (!cp_script_mode && o.cp_fill > 0) {
            // 先用 NOP 把队列尾部推到接近末尾并让 CP 处理掉(head 跟着前进), 这样后面的真命令
            // 就会撞上"尾部放不下"的情况, 从而走 §2.1 的 NOP 填充 + 回绕路径。
            for (int i = 0; i < o.cp_fill; ++i)
                if (!cp.submit_nop(8, &err)) { std::fprintf(stderr, "NOP 填充失败: %s\n", err.c_str()); return 1; }
            LogI("已塞入 %d 条 8 字 NOP(共 %d 字节)并让 CP 处理完; 接下来的命令应当触发队列回绕填充",
                 o.cp_fill, o.cp_fill * 32);
            cp.doorbell();
            if (cp.in_error()) { std::fprintf(stderr, "填充阶段 CP 出错\n"); return 1; }
        }
        // ② 命令流: LOAD_KERNEL → PARAMS → LAUNCH(脚本模式由脚本负责)
        if (cp_script_mode) { print_cmd_stream(gpu, cp); }
        else {
        if (!cp.submit_load_kernel(KERNEL_IMG_BASE, uint32_t(img.size()), 0x3, &err)) {
            std::fprintf(stderr, "提交 LOAD_KERNEL 失败: %s\n", err.c_str());
            return 1;
        }
        uint32_t maxoff = 0;
        for (const auto& kv : P) maxoff = std::max(maxoff, kv.first);
        std::vector<uint32_t> pwords(maxoff / 4 + 1, 0);
        for (const auto& kv : P) pwords[kv.first / 4] = kv.second;
        if (!pwords.empty() && !cp.submit_params(0, pwords, &err)) {
            std::fprintf(stderr, "提交 PARAMS 失败: %s\n", err.c_str());
            return 1;
        }
        const uint16_t launch_sig = o.cp_demo ? 3 : SEM_NONE;   // demo: 让 LAUNCH 完成时给 SEM[3] 打信号
        if (!cp.submit_launch(0x20, uint32_t(prof.warps), img_flags, launch_sig, SEM_NONE, &err)) {
            std::fprintf(stderr, "提交 LAUNCH 失败: %s\n", err.c_str());
            return 1;
        }
        if (o.cp_demo) {
            // 命令流尾部的 CP 自检: 依赖链(LAUNCH 完成 → WAIT 满足)+ 寄存器读写 + 栅栏 + 中断
            const uint32_t sem = cp.sem_base();
            bool ok = cp.submit_signal(sem + 7 * 4, 0x1234u, &err) &&
                      cp.submit_wait(sem + 3 * 4, 1, &err) &&
                      cp.submit_read_reg(MMIO_SM_KERNEL_MASK, 0x1000'4000u, &err) &&
                      cp.submit_fence(&err) &&
                      cp.submit_nop(4, &err) &&
                      cp.submit_irq(3, &err);
            if (!ok) { std::fprintf(stderr, "提交 CP 自检命令失败: %s\n", err.c_str()); return 1; }
        }
        print_cmd_stream(gpu, cp);
        }
    } else {
        LogI("--direct: 绕过命令流, 直接 load_img + LOAD 参数 + LAUNCH(对照用)");
        if (gpu.mem.load_img(o.img.c_str(), 0x3, &err) < 0) {
            std::fprintf(stderr, "load_img 失败: %s\n", err.c_str());
            return 1;
        }
        for (const auto& kv : P) gpu.mem.st_const(kv.first, kv.second);   // 驱动直接写常量区(CMDS §6.10 允许)
        std::string why;
        uint32_t code = 0;
        if (!gpu.launch(gpu.mem.img[0].entry, uint32_t(prof.warps), img_flags, &why, &code)) {
            std::fprintf(stderr, "LAUNCH 失败: %s\n", why.c_str());
            return 1;
        }
        print_launch_summary(gpu);
    }

    std::printf(" 开始执行%s\n", o.debug ? "(调试模式)" : "");
    if (!o.debug) {
        if (!o.direct) cp.doorbell();          // 处理命令流; LAUNCH 会阻塞到 kernel 完成
        else           gpu.run(o.max_cycles);
    } else {
        if (!o.direct) {
            const int n = cp.process(false, &err);
            LogI("调试模式: 已处理 %d 条命令, 内核已分发, 交给你单步", n);
            print_launch_summary(gpu);
        }
        Debugger dbg(gpu, cp);
        dbg.run();
    }

    print_report(gpu, cp);

    int rc = 0;
    if (cp.in_error() || cp.error() != CpErr::NO_ERROR) rc = 1;
    for (int t = 0; t < NUM_SM; ++t)
        if (gpu.sm[t].error != SmErr::NONE) rc = 2;

    if (o.do_check) {
        if (gpu.finished() && !gpu.active()) {
            const int c = check_result(gpu, o.kernel, P);
            if (c) rc = c;
        } else {
            LogW("内核没有跑完(结束原因: %s), 跳过结果校验", gpu.stop().name());
        }
    }

    if (o.dump_regs)
        for (int i = 0; i < MAX_WARPS; ++i)
            if (gpu.warps[i].alive || gpu.stats.retired[i]) std::printf("%s", gpu.dump_regs(i).c_str());

    if (!o.dump_spec.empty()) {
        const int d = dump_region(gpu, o.dump_spec);
        if (d) rc = d;
    }

    if (o.view) {
#if defined(SIMU_HAVE_SDL)
        SDLViewer viewer;
        if (viewer.init(o.fb_w, o.fb_h)) {
            std::vector<uint32_t> px(size_t(o.fb_w) * size_t(o.fb_h));
            for (int i = 0; i < o.fb_w * o.fb_h; ++i) px[size_t(i)] = gpu.mem.ld_global(gpu.fb_base + uint32_t(i) * 4);
            viewer.update(px);
            viewer.render();
            LogI("帧缓冲窗口已打开(%dx%d, 基址 0x%08x); 按 ESC 或关窗退出", o.fb_w, o.fb_h, gpu.fb_base);
            viewer.run_until_close();
            viewer.shutdown();
        } else {
            LogE("--view 失败: %s(无显示环境时请用 --fb-out 导出 PPM)", viewer.error() ? viewer.error() : "未知");
        }
#else
        LogE("--view 需要 SDL2: 编译时未找到 sdl2(pkg-config --exists sdl2), 请改用 --fb-out 导出 PPM");
#endif
    }

    if (!o.fb_out.empty()) {
        if (write_ppm(o.fb_out, gpu, o.fb_w, o.fb_h))
            LogI("帧缓冲已导出: %s (%dx%d, 基址 0x%08x, RGBA8888 → RGB)",
                 o.fb_out.c_str(), o.fb_w, o.fb_h, gpu.fb_base);
        else
            LogE("写 PPM 失败: %s", o.fb_out.c_str());
    }

    LogI("=== 模拟结束: 退出码 %d (%s) ===", rc,
         rc == 0 ? "正常" : rc == 1 ? "命令/CP 错误" : rc == 2 ? "SM 架构错误" : "结果校验不符");
    logger().flush();
    return rc;
}
