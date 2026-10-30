// ============================================================================
//  tb_main.cpp —— sim/run-sim 的 Verilator 测试台
//
//  它扮演"host 驱动"的角色(与 C++ 参考模型的 sim/simulator 同一种用法):
//    1. 把镜像放进 DDR(KERNEL_IMG_BASE), 命令环里的命令也由 host 铺好;
//    2. 通过 AXI4-Lite 写 CP 的 MMIO(CMD_TAIL → DOORBELL), 轮询 CP_STATUS;
//    3. 程序跑完后从 DDR 读回帧缓冲并做基本校验。
// ============================================================================

#include "Vgpu_system.h"
#include "verilated.h"

#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <thread>
#include <unistd.h>
#include <vector>

#if defined(RUNSIM_HAVE_SDL)
#include <SDL.h>
#endif

#if defined(RUNSIM_FST)
#include <verilated_fst_c.h>
#endif
#if defined(RUNSIM_VCD)
#include <verilated_vcd_c.h>
#endif

static Vgpu_system* top;
static uint64_t cyc = 0;
static bool trace_on = false;          // --trace: 打印 CP 状态变化
// 波形: --fst/--vcd 打开后在 tick() 里逐拍 dump(需要建模型时带 --trace-fst/--trace)
#if defined(RUNSIM_FST)
static VerilatedFstC* tfp_fst = nullptr;
#endif
#if defined(RUNSIM_VCD)
static VerilatedVcdC* tfp_vcd = nullptr;
#endif
static uint64_t sim_ns = 0;            // 波形时间轴(半周期 5ns)

static unsigned last_busy = 0;   // trace 用:上一次打印时的总线计数
static constexpr uint32_t LANES_PER_WARP = 8;     
static constexpr uint32_t IMG_BASE = 0x0200'1000;
static constexpr uint32_t CONST_BASE = 0x0200'0000;
static constexpr uint32_t SEM_BASE = 0x0300'0000;

// ---------------------------------------------------------------------------
//  时钟 / 复位
// ---------------------------------------------------------------------------
static void tick(int n = 1)
{
    for (int i = 0; i < n; i++) {
        top->clk = 0;
        top->eval();
        cyc++;
        sim_ns += 5;
#if defined(RUNSIM_FST)
        if (tfp_fst) tfp_fst->dump(sim_ns);
#endif
#if defined(RUNSIM_VCD)
        if (tfp_vcd) tfp_vcd->dump(sim_ns);
#endif
        top->clk = 1;
        top->eval();
        cyc++;
        sim_ns += 5;
#if defined(RUNSIM_FST)
        if (tfp_fst) tfp_fst->dump(sim_ns);
#endif
#if defined(RUNSIM_VCD)
        if (tfp_vcd) tfp_vcd->dump(sim_ns);
#endif
        if (trace_on) {
            const unsigned bus = unsigned(top->ctr_lines) ^ unsigned(top->ctr_ar);
            if (bus != last_busy) {
                std::fprintf(stderr, "[s%llu] done=%u%u ctl=%04x lines=%u ar=%u aw=%u\n",
                             (unsigned long long)cyc, unsigned(top->sm1_done),
                             unsigned(top->sm0_done), unsigned(top->dbg_mem_ctl),
                             unsigned(top->ctr_lines), unsigned(top->ctr_ar),
                             unsigned(top->ctr_aw));
                last_busy = bus;
            }
        }
    }
}

static void reset()
{
    top->rst = 1;
    top->ram_we = 0;
    top->ram_addr = 0;
    top->ram_wdata = 0;
    top->h_awvalid = 0;
    top->h_wvalid = 0;
    top->h_bready = 1;
    top->h_arvalid = 0;
    top->h_rready = 1;
    tick(5);
    top->rst = 0;
    tick(2);
}

// ---------------------------------------------------------------------------
//  host 旁路:直接读写 DDR(镜像 / 常量 / 帧缓冲)
// ---------------------------------------------------------------------------
static void ram_write(uint32_t addr, uint32_t data)
{
    top->ram_we = 1;
    top->ram_addr = addr;
    top->ram_wdata = data;
    tick(1);
    top->ram_we = 0;
    tick(1);
}

static uint32_t ram_read(uint32_t addr)
{
    top->ram_addr = addr;
    top->eval();
    return top->ram_rdata;
}

// ---------------------------------------------------------------------------
//  AXI4-Lite 主机:读写 CP 的 MMIO
// ---------------------------------------------------------------------------
static bool axil_write(uint32_t addr, uint32_t data)
{
    top->h_awvalid = 1;
    top->h_awaddr = addr;
    top->h_wvalid = 1;
    top->h_wdata = data;
    top->h_wstrb = 0xF;
    int guard = 1000;
    while (guard-- > 0) {
        tick(1);
        if (top->h_bvalid) {
            top->h_awvalid = 0;
            top->h_wvalid = 0;
            return true;
        }
    }
    std::fprintf(stderr, "AXI-Lite 写超时: addr=0x%08x\n", addr);
    return false;
}

static bool axil_read(uint32_t addr, uint32_t* out)
{
    top->h_arvalid = 1;
    top->h_araddr = addr;
    int guard = 1000;
    while (guard-- > 0) {
        tick(1);
        if (top->h_rvalid) {
            *out = top->h_rdata;
            top->h_arvalid = 0;
            tick(1);
            return true;
        }
    }
    std::fprintf(stderr, "AXI-Lite 读超时: addr=0x%08x\n", addr);
    return false;
}

// ---------------------------------------------------------------------------
//  命令环
// ---------------------------------------------------------------------------
struct Queue {
    uint32_t base;
    uint32_t size = 0x1000;                   // CMD_QUEUE_SIZE 默认值(CMDS §7, 环在 4KB 窗口内)
    uint32_t off = 0;                         // 已用字节

    uint32_t tail_addr() const { return base + off; }

    // 把环回卷到基址: head := tail := base(CMDS §7 允许 host 直写 CMD_HEAD)。
    // 逐帧 demo 每帧都重新从基址写命令。否则环绕满一圈后, CP 会先把基址留下的
    // 旧命令再读一遍(重放整段历史), 而 TB 同时在同一个位置写新帧 ⇒ 命令与参数错配。
    void rewind()
    {
        off = 0;
        axil_write(0x08, base);                  // CMD_HEAD
        axil_write(0x0C, base);                  // CMD_TAIL
    }

    // 写一条占满 len 字的 NOP(补齐物的合法形态: len=0 的全 0 字会被当成错误命令)
    void write_nop(uint32_t at, uint32_t nop_words)
    {
        ram_write(at, (nop_words << 16) | 0x00);         // len, op=NOP
        if (nop_words >= 2) ram_write(at + 4, 0xFFFF'FFFF);
        for (uint32_t i = 2; i < nop_words; i++) ram_write(at + 4 * i, 0);
    }

    // 一条命令不得跨环末尾回绕(驱动契约, CMDS §2.4)。装不下就先用一条 NOP 填满剩余空间,
    // 让 CP 的 head 正好走到 base+size 后自己绕回 base。
    void wrap_if_needed(uint32_t bytes)
    {
        if (off + bytes <= size) return;
        const uint32_t rest = size - off;
        if (rest >= 8) write_nop(base + off, rest / 4);
        off = 0;
    }

    // 补到 64B 边界(驱动契约, CMDS §2)。补齐物必须是一条 NOP 命令,
    // 不能是一串 0 —— 全 0 的字会被当成 len=0 的命令 ⇒ CMD_ERROR。
    void push(const std::vector<uint32_t>& words)
    {
        const uint32_t cmd_bytes = uint32_t(4 * words.size());
        uint32_t pad = (64 - cmd_bytes % 64) % 64;
        if (pad == 4) pad += 64;                     // 4 字节放不下 NOP(≥2 字)
        wrap_if_needed(cmd_bytes + pad);

        for (size_t i = 0; i < words.size(); i++) ram_write(base + off + uint32_t(4 * i), words[i]);
        off += cmd_bytes;
        if (pad) { write_nop(base + off, pad / 4); off += pad; }
    }
};

static std::vector<uint32_t> hdr(uint32_t op, uint32_t len, uint32_t sig, uint32_t wait)
{
    std::vector<uint32_t> w(len, 0);
    w[0] = (len << 16) | op;                         // flags = 0
    w[1] = (sig << 16) | wait;
    return w;
}

static void submit_load_kernel(Queue& q, uint32_t src, uint32_t bytes, uint32_t mask)
{
    auto w = hdr(0x03, 5, 0xFFFF, 0xFFFF);
    w[2] = src;
    w[3] = bytes;
    w[4] = mask;
    q.push(w);
}

static void submit_params(Queue& q, uint32_t dst_off, const std::vector<uint32_t>& params)
{
    auto w = hdr(0x09, 4 + uint32_t(params.size()), 0xFFFF, 0xFFFF);
    w[2] = dst_off;
    w[3] = uint32_t(params.size());
    for (size_t i = 0; i < params.size(); i++) w[4 + i] = params[i];
    q.push(w);
}

static void submit_launch(Queue& q, uint32_t entry, uint32_t warps, uint32_t flags,
                          uint32_t sig, uint32_t wait)
{
    auto w = hdr(0x04, 5, sig, wait);
    w[2] = entry;
    w[3] = warps;
    w[4] = flags;
    q.push(w);
}

static void submit_wait(Queue& q, uint32_t sem_addr, uint32_t value)
{
    auto w = hdr(0x07, 4, 0xFFFF, 0xFFFF);
    w[2] = sem_addr;
    w[3] = value;
    q.push(w);
}

// ---------------------------------------------------------------------------
//  等待 CP 空闲(轮询 CP_STATUS @ 0x00: bit0 IDLE, bit1 BUSY, bit2 ERROR)
// ---------------------------------------------------------------------------
static int wait_idle(uint64_t max_cycles)
{
    uint64_t t0 = cyc;
    while (cyc - t0 < max_cycles) {
        uint32_t st = 0;
        if (!axil_read(0x00, &st)) return -1;
        if (st & 4) return 1;                        // ERROR
        if (st & 1) return 0;                        // IDLE
        tick(4);
    }
    return -2;
}

static int flush_and_wait(Queue& q, uint64_t max_cycles, const char* what)
{
    if (!axil_write(0x0C, q.tail_addr())) return 3;   // CMD_TAIL 是队列内的绝对地址
    if (!axil_write(0x10, 1)) return 3;              // DOORBELL
    const int r = wait_idle(max_cycles);
    if (r == 0) return 0;

    uint32_t e = 0, sm0 = 0, sm1 = 0, fpc = 0, hd = 0, tl = 0, st = 0;
    axil_read(0x04, &e);
    axil_read(0x20, &sm0);
    axil_read(0x24, &sm1);
    axil_read(0x4C, &fpc);
    axil_read(0x08, &hd);
    axil_read(0x0C, &tl);
    axil_read(0x00, &st);
    std::fprintf(stderr, "%s 失败: r=%d CP_ERROR=%u SM0=%u SM1=%u FAULT_PC=0x%x\n",
                 what, r, e, sm0, sm1, fpc);
    // 最常见的两种是"驱动传错了"(不是硬件坏了), 直接说清楚, 免得看一屏寄存器
    if (e == 2)
        std::fprintf(stderr,
            "  CP_ERROR=2 = LAUNCH_REJECT: LAUNCH 的参数被拒(CMDS §4.1/§4.2)。常见原因:\n"
            "    - num_warps 超过 8, 或与镜像的 flags 不一致(内核用 bar.sync/共享内存时只能 ≤4 warp);\n"
            "    - entry 不在镜像范围内, 或镜像 CRC 与加载时不同。\n");
    else if (e == 4)
        std::fprintf(stderr,
            "  CP_ERROR=4 = TIMEOUT: 内核在 --cycles 拍内没跑完(可以调大 --cycles, 或查内核是否死循环)。\n");
    std::fprintf(stderr, "  CP_STATUS=0x%x CMD_HEAD=0x%x CMD_TAIL=0x%x\n", st, hd, tl);
    std::fprintf(stderr, "  done 信号: sm0_done=%u sm1_done=%u  mem_ctl=%04x\n",
                 unsigned(top->sm0_done), unsigned(top->sm1_done),
                 unsigned(top->dbg_mem_ctl));
    std::fprintf(stderr, "  SM0_ERROR=0x%x SM1_ERROR=0x%x ctr(lines=%u ar=%u aw=%u)\n",
                 unsigned(top->sm0_error), unsigned(top->sm1_error),
                 unsigned(top->ctr_lines), unsigned(top->ctr_ar), unsigned(top->ctr_aw));
    std::fprintf(stderr, "  FB: %08x %08x %08x %08x / RAM0: %08x %08x\n",
                 ram_read(0x1000'0000), ram_read(0x1000'0004),
                 ram_read(0x1000'0008), ram_read(0x1000'000c),
                 ram_read(0x0000'0000), ram_read(0x0000'0004));
    std::fprintf(stderr, "  CONST: %08x %08x %08x %08x\n",
                 ram_read(0x0200'0000), ram_read(0x0200'0004),
                 ram_read(0x0200'0008), ram_read(0x0200'000c));
    for (uint32_t o = 0; o < 0x100; o += 16) {
        std::fprintf(stderr, "  Q[0x%03x] %08x %08x %08x %08x\n", o,
                     ram_read(0x0100'0000 + o + 0), ram_read(0x0100'0000 + o + 4),
                     ram_read(0x0100'0000 + o + 8), ram_read(0x0100'0000 + o + 12));
    }
    return 3;
}

static std::vector<uint32_t> parse_list(const char* s)
{
    std::vector<uint32_t> out;
    std::string t(s);
    size_t pos = 0;
    while (pos < t.size()) {
        size_t comma = t.find(',', pos);
        if (comma == std::string::npos) comma = t.size();
        std::string item = t.substr(pos, comma - pos);
        if (!item.empty()) out.push_back(uint32_t(std::strtoul(item.c_str(), nullptr, 0)));
        pos = comma + 1;
    }
    return out;
}

// ---------------- 内核预设: 与 sim/simulator 的参考模型同一套输入与校验 ----------------
static uint32_t fbits(float f) { uint32_t u; std::memcpy(&u, &f, 4); return u; }
static float    as_fp(uint32_t u) { float f; std::memcpy(&f, &u, 4); return f; }

static const uint32_t G_IN = 0x1000'0000u, G_OUT = 0x1000'1000u, G_N = 32;
static const uint32_t MM_N = 32;
static const uint32_t MM_A = 0x1000'0000u, MM_B = 0x1000'1000u, MM_C = 0x1000'2000u;

// gouraud: 32 个顶点 {x=0.5i, y=0.25i, r=i, g=255-i, b=2i, a=255}
static void init_gouraud_bufs() {
    for (uint32_t i = 0; i < G_N; ++i) {
        const uint32_t v[6] = {fbits(float(i) * 0.5f), fbits(float(i) * 0.25f), fbits(float(i)),
                               fbits(float(255u - i)), fbits(float(i * 2u)), fbits(255.0f)};
        for (uint32_t j = 0; j < 6; ++j) ram_write(G_IN + (i * 6 + j) * 4, v[j]);
    }
    std::printf("gouraud: 顶点输入缓冲 0x%08x, %u 个顶点; 输出 0x%08x\n", G_IN, G_N, G_OUT);
}

// matmul: A 列主序、B 行主序, 元素是小整数 ⇒ 结果可按位比较
static void init_matmul_bufs() {
    for (uint32_t kk = 0; kk < MM_N; ++kk) {
        for (uint32_t i = 0; i < MM_N; ++i) {
            const float a = float((i * 7 + kk * 3) % 5) - 2.0f;
            ram_write(MM_A + (kk * MM_N + i) * 4, fbits(a));
        }
        for (uint32_t j = 0; j < MM_N; ++j) {
            const float b = float((j * 5 + kk * 2) % 7) - 3.0f;
            ram_write(MM_B + (kk * MM_N + j) * 4, fbits(b));
        }
    }
    std::printf("matmul: A(列主序) 0x%08x, B(行主序) 0x%08x, C 0x%08x, N=%u\n",
                MM_A, MM_B, MM_C, MM_N);
}

// 与内核完全相同的运算顺序(含单次舍入的 fma), 否则尾位对不上
static int check_gouraud(const std::vector<uint32_t>& P) {
    auto gp = [&](size_t i, uint32_t def) { return i < P.size() ? P[i] : def; };
    const float m00 = as_fp(gp(3, 0)), m01 = as_fp(gp(4, 0)), m10 = as_fp(gp(5, 0));
    const float m11 = as_fp(gp(6, 0)), tx = as_fp(gp(7, 0)), ty = as_fp(gp(8, 0));
    const uint32_t n_vtx = gp(2, G_N);
    uint32_t bad = 0, first = 0;
    for (uint32_t i = 0; i < n_vtx; ++i) {
        const float x = as_fp(ram_read(G_IN + i * 24)), y = as_fp(ram_read(G_IN + i * 24 + 4));
        const float xe = std::fma(x, m00, y * m01) + tx;
        const float ye = std::fma(y, m11, x * m10) + ty;
        bool ok = ram_read(G_OUT + i * 24) == fbits(xe) && ram_read(G_OUT + i * 24 + 4) == fbits(ye);
        for (int c = 2; c < 6 && ok; ++c)
            ok = ram_read(G_OUT + i * 24 + uint32_t(c) * 4) == ram_read(G_IN + i * 24 + uint32_t(c) * 4);
        if (!ok) {
            if (!bad) {
                first = i;
                std::fprintf(stderr,
                    "  首个不符顶点 v%u: in=(%f,%f) got=(%08x,%08x) exp=(%08x,%08x)\n",
                    i, (double)x, (double)y,
                    ram_read(G_OUT + i * 24), ram_read(G_OUT + i * 24 + 4),
                    fbits(xe), fbits(ye));
            }
            ++bad;
        }
    }
    if (bad) {
        std::fprintf(stderr, "gouraud 校验失败: %u/%u 个顶点不符; 首个顶点 %u\n", bad, n_vtx, first);
        return 4;
    }
    std::printf("gouraud 校验通过: %u 个顶点的仿射变换与颜色透传全部按位一致\n", n_vtx);
    return 0;
}

static int check_matmul(uint32_t rows) {
    if (rows > MM_N) rows = MM_N;
    uint32_t bad = 0, first = 0;
    for (uint32_t i = 0; i < rows; ++i) {
        for (uint32_t j = 0; j < MM_N; ++j) {
            float acc = 0.0f;
            for (uint32_t kk = 0; kk < MM_N; ++kk)
                acc = std::fma(as_fp(ram_read(MM_A + (kk * MM_N + i) * 4)),
                               as_fp(ram_read(MM_B + (kk * MM_N + j) * 4)), acc);
            if (ram_read(MM_C + (i * MM_N + j) * 4) != fbits(acc)) { if (!bad) first = i * MM_N + j; ++bad; }
        }
    }
    if (bad) {
        std::fprintf(stderr, "matmul 校验失败: %u/%u 个元素不符; 首个 C[%u][%u] = 0x%08x\n",
                     bad, rows * MM_N, first / MM_N, first % MM_N,
                     ram_read(MM_C + first * 4));
        return 4;
    }
    std::printf("matmul 校验通过: C 的前 %u 行(%u 个元素)与参考模型按位一致"
                "(%u×%u×%u = %u 次 MAC)\n",
                rows, rows * MM_N, rows, MM_N, MM_N, rows * MM_N * MM_N);
    return 0;
}

// ---------------------------------------------------------------------------
//  SDL2 窗口(可选): 把帧缓冲直接贴到纹理上, 每帧 present 一次
// ---------------------------------------------------------------------------
#if defined(RUNSIM_HAVE_SDL)
class Viewer {
public:
    bool init(int w, int h, int scale, bool vsync)
    {
        w_ = w; h_ = h;
        if (SDL_Init(SDL_INIT_VIDEO) != 0) {
            std::fprintf(stderr, "SDL_Init 失败: %s\n", SDL_GetError());
            return false;
        }
        // 画面很扁(比如 clear 的 64x1)时按比例放大, 免得窗口只有几个像素高
        int sx = scale > 0 ? scale : 1;
        if (h * sx < 64) sx = (64 + h - 1) / h;      // 太扁: 至少 64 像素高
        if (w * sx > 1024) sx = (1024 / w) > 0 ? 1024 / w : 1;   // 太宽: 不超过 1024
        win_ = SDL_CreateWindow("SimpleGPU", SDL_WINDOWPOS_CENTERED,
                                SDL_WINDOWPOS_CENTERED, w * sx, h * sx, SDL_WINDOW_SHOWN);
        if (!win_) { std::fprintf(stderr, "SDL_CreateWindow 失败: %s\n", SDL_GetError()); return false; }
        rnd_ = SDL_CreateRenderer(win_, -1,
                                  SDL_RENDERER_ACCELERATED | (vsync ? SDL_RENDERER_PRESENTVSYNC : 0));
        if (!rnd_) rnd_ = SDL_CreateRenderer(win_, -1, SDL_RENDERER_SOFTWARE);
        if (!rnd_) { std::fprintf(stderr, "SDL_CreateRenderer 失败: %s\n", SDL_GetError()); return false; }
        tex_ = SDL_CreateTexture(rnd_, SDL_PIXELFORMAT_ARGB8888, SDL_TEXTUREACCESS_STREAMING, w, h);
        if (!tex_) { std::fprintf(stderr, "SDL_CreateTexture 失败: %s\n", SDL_GetError()); return false; }
        SDL_RendererInfo ri{};
        const char* rname = (SDL_GetRendererInfo(rnd_, &ri) == 0) ? ri.name : "?";
        std::printf("SDL: video=%s renderer=%s 窗口 %dx%d(画面 %dx%d, 放大 %dx)\n",
                    SDL_GetCurrentVideoDriver(), rname, w * sx, h * sx, w, h, sx);
        std::fflush(stdout);
        return true;
    }

    void present(const std::vector<uint32_t>& px)
    {
        // 纹理尺寸在 init 时定死; 万一调用方给的缓冲比它小, 宁可跳过也不要越界读
        if (!tex_ || px.size() < size_t(w_) * size_t(h_)) return;
        if (SDL_UpdateTexture(tex_, nullptr, px.data(), w_ * int(sizeof(uint32_t))) != 0) return;
        SDL_RenderClear(rnd_);
        SDL_RenderCopy(rnd_, tex_, nullptr, nullptr);
        SDL_RenderPresent(rnd_);
    }

    // false = 用户关窗或按 ESC
    bool poll()
    {
        SDL_Event e;
        while (SDL_PollEvent(&e)) {
            if (e.type == SDL_QUIT) return false;
            if (e.type == SDL_KEYDOWN && e.key.keysym.sym == SDLK_ESCAPE) return false;
        }
        return true;
    }

    ~Viewer()
    {
        if (tex_) SDL_DestroyTexture(tex_);
        if (rnd_) SDL_DestroyRenderer(rnd_);
        if (win_) SDL_DestroyWindow(win_);
        if (win_ || rnd_) SDL_Quit();
    }

private:
    SDL_Window*   win_ = nullptr;
    SDL_Renderer* rnd_ = nullptr;
    SDL_Texture*  tex_ = nullptr;
    int w_ = 0, h_ = 0;
};
#endif  // RUNSIM_HAVE_SDL

// ---------------------------------------------------------------------------
//  anim demo: 逐帧推参数, 每帧重算整幅画面(与 sw/demo/anim.cpp 同一套参数与公式)
// ---------------------------------------------------------------------------
static uint32_t hsv(double h, double s, double v)
{
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

// 常量区布局见 sw/kernels/anim.S(10 个字)
static std::vector<uint32_t> anim_params(uint32_t w, uint32_t h, uint32_t warps,
                                         uint32_t fb_base, uint32_t frame, uint32_t box)
{
    const double hue = double(frame % 360);
    std::vector<uint32_t> p(10, 0);
    p[0] = w * h;                                  // N
    p[1] = warps * LANES_PER_WARP;                 // stride_elems(驱动契约: 每 warp 8 lane)
    p[2] = fb_base;                                // fb_base
    p[3] = frame;                                  // frame
    p[4] = w - 1;                                  // W_mask
    p[5] = uint32_t(std::log2(double(w)));         // H_shift
    p[6] = box;                                    // box_size
    p[7] = hsv(hue, 0.55, 0.22);                   // bg0
    p[8] = hsv(hue + 45, 0.80, 0.55);              // bg1
    p[9] = hsv(hue + 180, 0.90, 1.00);             // box_color
    return p;
}

// 参考模型: 按 anim.S 的公式重算一帧(与 sw/demo/anim.cpp::verify_frame 一致)
static uint32_t anim_bad_pixels(const std::vector<uint32_t>& fb, const std::vector<uint32_t>& p,
                                uint32_t w, uint32_t h, uint32_t frame, uint32_t* first)
{
    const uint32_t box = p[6], bx = (frame * 3) & (w - 1), by = (frame * 2) & (h - 1);
    const uint32_t bg0 = p[7], bg1 = p[8], boxc = p[9];
    uint32_t bad = 0;
    for (uint32_t i = 0; i < w * h; i++) {
        const uint32_t x = i & (w - 1), y = i >> uint32_t(std::log2(double(w)));
        uint32_t expect = ((((x + y + frame * 4) >> 5) & 1) ? bg1 : bg0);
        if (((x - bx) & (w - 1)) + ((y - by) & (h - 1)) < box) expect = boxc;
        if (fb[i] != expect) { if (!bad && first) *first = i; ++bad; }
    }
    return bad;
}

// ---------------------------------------------------------------------------
//  把命令环里新写好的命令交给 CP(CMD_TAIL → DOORBELL)并等它跑完
//  返回 0 = 成功; 3 = CP/SM 出错或超时(打印诊断)
// ---------------------------------------------------------------------------
static int flush_and_wait(struct Queue& q, uint64_t max_cycles, const char* what);

// ---------------------------------------------------------------------------
//  显示后端的两个补充: 终端 ANSI 真彩色 / PPM 文件(与参考模型的 --ascii/--ppm-dir 对应)
// ---------------------------------------------------------------------------
static void print_ascii(const std::vector<uint32_t>& px, uint32_t w, uint32_t h,
                        uint32_t cols, uint32_t rows)
{
    // 终端字符约为 1:2 的方块, 所以默认按"两列像素一个字符"取; 给了 cols/rows 就按
    // 最邻近降采样(大画面也能塞进终端)。
    if (cols == 0) cols = (w + 1) / 2;
    if (rows == 0) rows = h;
    for (uint32_t ry = 0; ry < rows; ++ry) {
        std::string line;
        const uint32_t sy = uint32_t(uint64_t(ry) * h / rows);
        for (uint32_t rx = 0; rx < cols; ++rx) {
            const uint32_t sx = uint32_t(uint64_t(rx) * w / cols);
            const uint32_t c = px[size_t(sy) * w + sx];
            char buf[40];
            std::snprintf(buf, sizeof(buf), "\x1b[48;2;%u;%u;%um ",
                          (c >> 16) & 0xFF, (c >> 8) & 0xFF, c & 0xFF);
            line += buf;
        }
        line += "\x1b[0m\n";
        std::fputs(line.c_str(), stdout);
    }
    std::fflush(stdout);
}

static void write_ppm(const std::string& dir, uint32_t idx,
                      const std::vector<uint32_t>& px, uint32_t w, uint32_t h)
{
    char name[512];
    std::snprintf(name, sizeof(name), "%s/frame_%04u.ppm", dir.c_str(), idx);
    FILE* g = std::fopen(name, "wb");
    if (!g) { std::fprintf(stderr, "写不了 %s\n", name); return; }
    std::fprintf(g, "P6\n%u %u\n255\n", w, h);
    for (uint32_t i = 0; i < w * h; ++i) {
        const unsigned char rgb[3] = { (unsigned char)((px[i] >> 16) & 0xFF),
                                       (unsigned char)((px[i] >> 8) & 0xFF),
                                       (unsigned char)(px[i] & 0xFF) };
        std::fwrite(rgb, 1, 3, g);
    }
    std::fclose(g);
}

// clear: 每个被覆盖的像素都应当等于写入的颜色
static uint32_t bad_pixels_constant(const std::vector<uint32_t>& px, uint32_t n, uint32_t color,
                                    uint32_t* first)
{
    uint32_t bad = 0;
    for (uint32_t i = 0; i < n; ++i)
        if (px[i] != color) { if (!bad && first) *first = i; ++bad; }
    return bad;
}

// ---- 镜像定位: --image clear 也能用, 不必写 build/clear.bin, 也不必 cd 到 sim/simulator ----
// 查找顺序: 原样 → 原样+.bin → 测试台所在目录推出来的 <repo>/sim/simulator/build/<名>.bin
// → 当前目录下的 sim/simulator/build/<名>.bin。找到就用, 找不到再报错(报错时列出试过的路径)。
static bool file_readable(const std::string& path)
{
    FILE* f = std::fopen(path.c_str(), "rb");
    if (!f) return false;
    std::fclose(f);
    return true;
}

static std::string exe_dir()
{
    char buf[4096] = {0};
    const ssize_t n = ::readlink("/proc/self/exe", buf, sizeof(buf) - 1);
    if (n <= 0) return {};
    std::string p(buf, size_t(n));
    const size_t slash = p.find_last_of('/');
    return (slash == std::string::npos) ? std::string{} : p.substr(0, slash);
}

static std::string strip_bin(std::string name)
{
    if (name.size() > 4 && name.compare(name.size() - 4, 4, ".bin") == 0)
        name.resize(name.size() - 4);
    return name;
}

// 返回空串 = 没找到;tried 用于报错时列路径
static std::string resolve_image(const std::string& want, std::vector<std::string>* tried)
{
    std::string base = want;
    if (base.find('/') == std::string::npos) base = strip_bin(base);   // 只给名字时去掉 .bin
    std::vector<std::string> dirs;
    dirs.push_back("");                                       // 原样 / 当前目录
    const std::string ex = exe_dir();                         // <root>/sim/run-sim/build
    if (!ex.empty()) dirs.push_back(ex + "/../../simulator/build");
    dirs.push_back("sim/simulator/build");                    // 站在仓库根目录时
    // 顺序: 先 <名字>.bin 再裸名字 —— sim/simulator/build/ 里还有同名的
    // 可执行程序(anim/car/...), 裸名字会先撞上它们(ELF 头当镜像头用)。
    for (const std::string& d : dirs) {
        std::string prefix = d.empty() ? std::string() : d + "/";
        for (const char* ext : {".bin", ""}) {
            std::string cand = prefix + base + ext;
            if (tried) tried->push_back(cand);
            if (file_readable(cand)) {
                // 折叠 "a/b/../c" 这类片段, 打印出来的路径才能直接复制使用
                char canon[4096];
                if (realpath(cand.c_str(), canon) != nullptr) return std::string(canon);
                return cand;
            }
        }
    }
    return {};
}

static std::string base_name(std::string path)
{
    const size_t slash = path.find_last_of("/\\");
    if (slash != std::string::npos) path = path.substr(slash + 1);
    const size_t dot = path.find_last_of('.');
    if (dot != std::string::npos) path = path.substr(0, dot);
    for (char& c : path) c = char(std::tolower((unsigned char)c));
    return path;
}

static void usage(const char* prog)
{
    std::printf(
        "用法: %s [选项]\n"
        "\n"
        "  --image FILE    内核镜像(默认 build/anim.bin; 路径相对 sim/simulator)\n"
        "  --kernel NAME   预设 clear|gouraud|matmul|anim(none = 不预设)。给了它就用\n"
        "                  build/NAME.bin 当镜像; 省略时按镜像名推断\n"
        "  --warps N       启动几个 warp(1..8, 默认 4; >4 时两个 SM 各 4 个)\n"
        "  --w/--h N       画面尺寸(动画默认 32x32, 其它内核默认 warps*8 x 1)\n"
        "  --box N         anim 菱形块边长(默认 6)\n"
        "  --frames N      跑多少帧(默认: 有显示时 0 = 一直跑, 否则 1)\n"
        "  --fps N         限速(默认 30; 0 = 不限速)\n"
        "  --view          SDL2 窗口(需要 SDL2)\n"
        "  --ascii         终端真彩色(不需要图形环境; 可用 --ascii-cols/--ascii-rows 降采样)\n"
        "  --ppm-dir DIR   每帧写 DIR/frame_0000.ppm\n"
        "  --scale N       SDL 窗口放大倍数(默认 6; 画面很扁时自动放大到至少 64 像素高)\n"
        "  --vsync         让 SDL 跟显示器刷新同步(默认不跟, 由 --fps 限速)\n"
        "  --no-verify     不做参考模型校验\n"
        "  --fst FILE      导出 FST 波形(需要建模型时带 --trace-fst)\n"
        "  --vcd FILE      导出 VCD 波形(需要建模型时带 --trace)\n"
        "  --cycles N      单条命令的超时拍数(默认 5000000)\n"
        "  --fb-base ADDR  显示/读回用的缓冲基址(默认 0x10000000)\n"
        "  --dump FILE     把读回的画面按原始 32 位字写文件\n"
        "  --params a,b,c  覆盖内核预设的常量区(逗号分隔, 支持 0x..)\n"
        "  --trace         逐拍打印 CP / SM 的状态变化\n"
        "  --help          这份说明\n",
        prog);
}

int main(int argc, char** argv)
{
    // 行缓冲: 万一仿真崩了, 已经打印的进度不会丢(输出到管道时默认是全缓冲)
    std::setvbuf(stdout, nullptr, _IOLBF, 0);
    std::string image = "build/anim.bin";
    std::string kernel, dump, ppm_dir, fst_path, vcd_path;
    bool image_given = false, kernel_given = false;
    uint32_t warps = 4, disp_w = 0, disp_h = 0, box = 6, fb_base = 0x1000'0000;
    uint32_t ascii_cols = 0, ascii_rows = 0;
    bool fb_base_given = false, w_given = false, h_given = false;
    int frames = -1, fps = 30, scale = 6;
    bool view = false, ascii = false, verify = true, vsync = false;
    uint64_t max_cycles = 5'000'000;
    std::vector<uint32_t> params;

    for (int i = 1; i < argc; i++) {
        std::string a = argv[i];
        auto nxt = [&]() { return (i + 1 < argc) ? argv[++i] : ""; };
        auto num = [&]() { return uint32_t(std::strtoul(nxt(), nullptr, 0)); };
        if (a == "--image") { image = nxt(); image_given = true; }
        else if (a == "--kernel") { kernel = nxt(); kernel_given = true; }
        else if (a == "--warps") warps = num();
        else if (a == "--w") { disp_w = num(); w_given = true; }
        else if (a == "--h") { disp_h = num(); h_given = true; }
        else if (a == "--ascii-cols") ascii_cols = num();
        else if (a == "--ascii-rows") ascii_rows = num();
        else if (a == "--box") box = num();
        else if (a == "--frames") frames = std::atoi(nxt());
        else if (a == "--fps") fps = std::atoi(nxt());
        else if (a == "--scale") scale = std::atoi(nxt());
        else if (a == "--view") view = true;
        else if (a == "--vsync") vsync = true;
        else if (a == "--ascii") ascii = true;
        else if (a == "--ppm-dir") ppm_dir = nxt();
        else if (a == "--no-verify") verify = false;
        else if (a == "--verify") verify = true;
        else if (a == "--fst") fst_path = nxt();
        else if (a == "--vcd") vcd_path = nxt();
        else if (a == "--cycles") max_cycles = std::strtoull(nxt(), nullptr, 0);
        else if (a == "--fb-base") { fb_base = num(); fb_base_given = true; }
        else if (a == "--dump") dump = nxt();
        else if (a == "--params") params = parse_list(nxt());
        else if (a == "--trace") trace_on = true;
        else if (a == "--help" || a == "-h") { usage(argv[0]); return 0; }
        else { std::fprintf(stderr, "未知选项 %s(试 --help)\n", a.c_str()); return 2; }
    }
    if (warps < 1) warps = 1;
    if (warps > 8) warps = 8;
    const uint32_t lanes = warps * LANES_PER_WARP;      // 本配置总共多少 lane

    // 镜像与内核两边互相推断: 只给 --kernel clear 就自动用 build/clear.bin,
    // 只给 --image clear.bin 就自动用 clear 的预设。两边都给了还打架就提醒一句
    // —— 预设的常量布局 / 期望值是按内核写死的, 拿 anim 的镜像跑 clear 的预设
    // 只会得到一堆看起来像硬件故障的错。
    if (kernel.empty()) kernel = base_name(image);
    if (!image_given && kernel_given && kernel != "none") image = kernel;   // --image 由内核名解析
    if (base_name(image) != kernel && kernel != "none") {
        std::fprintf(stderr,
                     "注意: 镜像 %s 与内核预设 %s 不是同一个程序; 预设的常量布局/期望值"
                     "是按 %s 写的, 结果大概对不上。想跑 %s 就去掉 --image(%s 会自动用 "
                     "build/%s.bin), 或者用 make %s。\n",
                     image.c_str(), kernel.c_str(), kernel.c_str(), kernel.c_str(),
                     kernel.c_str(), kernel.c_str(), kernel.c_str());
    }

    Verilated::commandArgs(argc, argv);
    top = new Vgpu_system;

#if defined(RUNSIM_FST)
    if (!fst_path.empty()) {
        Verilated::traceEverOn(true);
        tfp_fst = new VerilatedFstC;
        top->trace(tfp_fst, 99);
        tfp_fst->open(fst_path.c_str());   // Verilator 5.x 的 open() 不返回状态
    }
#else
    if (!fst_path.empty()) {
        std::fprintf(stderr, "--fst 需要建模型时带 --trace-fst(改完 Makefile 重新 make)\n");
        return 6;
    }
#endif
#if defined(RUNSIM_VCD)
    if (!vcd_path.empty()) {
        Verilated::traceEverOn(true);
        tfp_vcd = new VerilatedVcdC;
        top->trace(tfp_vcd, 99);
        tfp_vcd->open(vcd_path.c_str());
    }
#else
    if (!vcd_path.empty()) {
        std::fprintf(stderr, "--vcd 需要建模型时带 --trace(改完 Makefile 重新 make)\n");
        return 6;
    }
#endif

    reset();

    // ---- 1. 镜像进 DDR ----
    std::vector<std::string> tried;
    {
        const std::string resolved = resolve_image(image, &tried);
        if (resolved.empty()) {
            std::fprintf(stderr, "找不到镜像 %s。试过:\n", image.c_str());
            for (const std::string& t : tried) std::fprintf(stderr, "  %s\n", t.c_str());
            std::fprintf(stderr, "先编内核镜像: cd sim/simulator && make images\n");
            return 2;
        }
        image = resolved;
    }
    FILE* f = std::fopen(image.c_str(), "rb");
    if (!f) { std::fprintf(stderr, "打不开镜像 %s\n", image.c_str()); return 2; }
    std::fseek(f, 0, SEEK_END);
    long sz = std::ftell(f);
    std::fseek(f, 0, SEEK_SET);
    std::vector<uint8_t> buf;
    buf.resize(size_t(sz));
    if (std::fread(buf.data(), 1, buf.size(), f) != buf.size()) {
        std::fprintf(stderr, "读镜像失败\n");
        return 2;
    }
    std::fclose(f);
    for (size_t i = 0; i < buf.size(); i += 4) {
        uint32_t w = 0;
        for (size_t b = 0; b < 4 && i + b < buf.size(); b++) w |= uint32_t(buf[i + b]) << (8 * b);
        ram_write(IMG_BASE + uint32_t(i), w);
    }
    const uint32_t entry = (uint32_t(buf[4]) | uint32_t(buf[5]) << 8 |
                            uint32_t(buf[6]) << 16 | uint32_t(buf[7]) << 24);
    // 镜像头 word3 = 标志位(uses_barrier / uses_shared / global_write)。LAUNCH 的 flags 必须
    // 与之一致(CMDS §5 交叉核对, 不匹配 CP 就报 CMD_ERROR), 所以驱动要把镜像的标志传下去。
    const uint32_t img_flags = (uint32_t(buf[12]) | uint32_t(buf[13]) << 8 |
                                uint32_t(buf[14]) << 16 | uint32_t(buf[15]) << 24);
    std::printf("镜像 %s: %ld 字节, entry=0x%x, warp=%u\n", image.c_str(), sz, entry, warps);

    // ---- 2. 画面尺寸与帧数默认值 ----
    // 这些内核不写帧缓冲(它们写自己的输出区), 所以默认把"画面"指向它们的输出:
    // 每个 32 位字当一个像素看, 至少能看到结果确实变了。想换就显式给 --fb-base/--w/--h。
    const bool display = view || ascii || !ppm_dir.empty();
    if (kernel == "matmul") {
        if (!fb_base_given) fb_base = MM_C;
        if (!w_given) disp_w = MM_N;
        if (!h_given) disp_h = MM_N;
    } else if (kernel == "gouraud") {
        if (!fb_base_given) fb_base = G_OUT;
        if (!w_given) disp_w = 6;
        if (!h_given) disp_h = (G_N < lanes) ? G_N : lanes;
    }
    if (disp_w == 0) disp_w = (kernel == "anim") ? 32 : lanes;
    if (disp_h == 0) disp_h = (kernel == "anim") ? 32 : 1;
    if (frames < 0) frames = view ? 0 : 1;   // 只有交互窗口才默认一直跑

    // ---- 3. 内核预设: 常量区参数 + 输入缓冲 ----
    // 内核契约: 每个 lane 算 idx = tid + wid*8, 所以只有前 warps*8 个元素会被写;
    // 顶点数 / 矩阵行数 都按这个上限缩, 不然后面的元素没人算(以前 warps<4 就"校验失败")。
    uint32_t gouraud_n = 0, matmul_rows = 0, clear_n = 0, clear_color = 0;
    if (kernel == "gouraud") {
        gouraud_n = (G_N < lanes) ? G_N : lanes;
        if (params.empty())
            params = { G_IN, G_OUT, gouraud_n,
                       fbits(1.5f), fbits(0.25f), fbits(-0.5f), fbits(2.0f),
                       fbits(3.0f), fbits(-1.0f), 24 };
        gouraud_n = params.size() > 2 ? params[2] : gouraud_n;
        init_gouraud_bufs();
    } else if (kernel == "matmul") {
        matmul_rows = (MM_N < lanes) ? MM_N : lanes;
        if (params.empty()) params = { MM_A, MM_B, MM_C, MM_N, MM_N, MM_N };
        init_matmul_bufs();
    } else if (kernel == "clear") {
        // clear 是 memset: N 是**总像素数**, stride 是并行度(每 lane 隔 stride 个写一个)。
        // 所以 N 要按画面大小给, 不是按 lane 数给 —— 否则 --w/--h 放大后只有一行被填。
        clear_color = (params.size() > 1) ? params[1] : 0x0000'ff00u;
        clear_n = disp_w * disp_h;
        if (params.empty()) params = { clear_n, clear_color, lanes };
        clear_n = params.size() > 0 ? params[0] : clear_n;
        clear_color = params.size() > 1 ? params[1] : clear_color;
        // 驱动契约: stride 必须等于并发 lane 数, 且 N 是 stride 的整数倍(尾部单独处理)
        if (params.size() >= 3) params[2] = lanes;
        else if (params.size() == 2) params.push_back(lanes);
    }

    // ---- 4. 命令环: LOAD_KERNEL 先跑完, 之后的每一帧再追加 PARAMS/LAUNCH/WAIT ----
    Queue q{0x0100'0000};
    submit_load_kernel(q, IMG_BASE, uint32_t(sz), 0x3);
    if (int rc = flush_and_wait(q, max_cycles, "LOAD_KERNEL")) return rc;
    std::printf("LOAD_KERNEL 完成\n");

#if defined(RUNSIM_HAVE_SDL)
    Viewer viewer;
    if (view && !viewer.init(int(disp_w), int(disp_h), scale > 0 ? scale : 6, vsync)) return 7;
#else
    if (view) {
        std::fprintf(stderr, "--view 需要 SDL2(编译时未找到), 请装 libsdl2-dev 后重新 make;"
                             " 没有图形环境时用 --ascii 或 --ppm-dir\n");
        return 7;
    }
#endif

    std::printf("内核 %s: 画面 %ux%u, warp %u(%u lane), 帧数 %s, 校验 %s\n",
                kernel.c_str(), disp_w, disp_h, warps, lanes,
                frames == 0 ? "0 = 一直跑" : std::to_string(frames).c_str(),
                verify ? "开" : "关");

    // ---- 5. 逐帧: PARAMS(可选) → LAUNCH → WAIT → 读回 → 校验 → 显示 ----
    const uint64_t c0 = cyc;
    const auto t_start = std::chrono::steady_clock::now();
    auto t_next = t_start;
    const auto frame_budget = std::chrono::duration<double>(fps > 0 ? 1.0 / fps : 0.0);

    std::vector<uint32_t> fb(size_t(disp_w) * size_t(disp_h), 0);
    uint32_t n_frames = 0, bad_frames = 0, bad_first = 0;
    bool quit = false;

    for (uint32_t frame = 0; !quit; ++frame) {
        std::vector<uint32_t> P = params;
        if (kernel == "anim") P = anim_params(disp_w, disp_h, warps, fb_base, frame, box);

        // 每帧都把环回卷到基址再写: 否则环绕满后 CP 会重放上一轮的旧命令
        q.rewind();
        if (!P.empty()) submit_params(q, 0, P);
        submit_launch(q, entry, warps, img_flags, 0, 0xFFFF);
        submit_wait(q, SEM_BASE, frame + 1);
        if (int rc = flush_and_wait(q, max_cycles, kernel == "anim" ? "anim 帧" : "内核")) return rc;

        for (uint32_t i = 0; i < disp_w * disp_h; ++i) fb[i] = ram_read(fb_base + 4 * i);
        ++n_frames;

        // 校验: anim / clear 每帧都校, gouraud / matmul 只在第 0 帧校(结果与帧号无关)
        if (verify) {
            uint32_t bad = 0, first = 0;
            if (kernel == "anim") {
                bad = anim_bad_pixels(fb, P, disp_w, disp_h, frame, &first);
            } else if (kernel == "clear") {
                bad = bad_pixels_constant(fb, clear_n, clear_color, &first);
                if (bad && bad_frames == 0)
                    std::fprintf(stderr, "  首个不符像素 %u: got=0x%08x exp=0x%08x\n",
                                 first, fb[first], clear_color);
            } else if (kernel == "gouraud" && frame == 0) {
                if (int rc = check_gouraud(P)) return rc;
            } else if (kernel == "matmul" && frame == 0) {
                if (int rc = check_matmul(matmul_rows)) return rc;
            }
            if (bad) {
                if (!bad_frames) bad_first = first;
                ++bad_frames;
                if (bad_frames <= 3)
                    std::fprintf(stderr, "帧 %u 与参考模型不符: %u/%u 个像素, 首个 %u\n",
                                 frame, bad, disp_w * disp_h, first);
            }
        }

        // 显示
#if defined(RUNSIM_HAVE_SDL)
        if (view) {
            viewer.present(fb);
            if (!viewer.poll()) quit = true;
        }
#endif
        if (ascii) print_ascii(fb, disp_w, disp_h, ascii_cols, ascii_rows);
        if (!ppm_dir.empty()) write_ppm(ppm_dir, frame, fb, disp_w, disp_h);

        if (!quit && frames > 0 && frame + 1 >= uint32_t(frames)) quit = true;

        if (!quit && (frame + 1) % 10 == 0) {
            const double el = std::chrono::duration<double>(
                std::chrono::steady_clock::now() - t_start).count();
            std::printf("  ... 第 %u 帧, %.1f 周期/帧, 实际 %.2f 帧/秒\n",
                        frame + 1, double(cyc - c0) / double(n_frames),
                        el > 0 ? double(n_frames) / el : 0.0);
            std::fflush(stdout);
        }

        if (fps > 0 && !quit) {
            t_next += std::chrono::duration_cast<std::chrono::steady_clock::duration>(frame_budget);
            std::this_thread::sleep_until(t_next);
        }
    }

    std::printf("%s 完成: %u 帧, 共 %llu 周期(%.1f 周期/帧)\n",
                kernel.c_str(), n_frames, (unsigned long long)(cyc - c0),
                n_frames ? double(cyc - c0) / double(n_frames) : 0.0);
    std::printf("统计: 行请求 %u, MMIO %u, 未映射 %u\n",
                top->ctr_lines, top->ctr_mmio, top->ctr_unmapped);

    if (!dump.empty()) {
        FILE* g = std::fopen(dump.c_str(), "wb");
        if (g) {
            std::fwrite(fb.data(), 4, fb.size(), g);
            std::fclose(g);
            std::printf("画面已写入 %s(%u 个 32 位字)\n", dump.c_str(), disp_w * disp_h);
        }
    }
#if defined(RUNSIM_FST)
    if (tfp_fst) { tfp_fst->close(); std::printf("波形已写入 %s\n", fst_path.c_str()); }
#endif
#if defined(RUNSIM_VCD)
    if (tfp_vcd) { tfp_vcd->close(); std::printf("波形已写入 %s\n", vcd_path.c_str()); }
#endif

    delete top;
    if (bad_frames) {
        std::fprintf(stderr, "校验失败: %u/%u 帧与参考模型不符(首个坏像素 %u)\n",
                     bad_frames, n_frames, bad_first);
        return 4;
    }
    if (verify) std::printf("校验通过\n");
    return 0;
}
