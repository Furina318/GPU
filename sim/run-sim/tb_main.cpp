// ============================================================================
//  tb_main.cpp —— sim/run-sim 的 Verilator 测试台
//
//  它扮演"host 驱动"的角色(与 C++ 参考模型的 sim/simulator 同一种用法):
//    1. 把镜像放进 DDR(KERNEL_IMG_BASE), 命令环里的命令也由 host 铺好;
//    2. 通过 AXI4-Lite 写 CP 的 MMIO(CMD_TAIL → DOORBELL), 轮询 CP_STATUS;
//    3. 程序跑完后从 DDR 读回帧缓冲并做基本校验。
//
//  命令行(全部可选):
//    --image FILE     内核镜像(默认 build/clear.bin)
//    --warps N        LAUNCH 的 warp 数(默认 4)
//    --params a,b,c   PARAMS 命令里的常量(十进制或 0x 十六进制, 逗号分隔)
//    --queue Q        命令环基址(默认 0x0100_0000)
//    --fb-base B      帧缓冲基址(默认 0x1000_0000)
//    --fb-count N     读回并校验的像素数(默认 0 = 不校验)
//    --fb-expect V    期望的帧缓冲内容(默认只打印前几个字)
//    --cycles N       仿真上限(默认 5_000_000)
//    --dump FILE      把读回的帧缓冲写成二进制(可选)
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
#include <vector>

// SDL2 窗口在编译期探测(sim/run-sim/Makefile 用 pkg-config), 没有也不影响其余功能。
#if defined(RUNSIM_HAVE_SDL)
#include <SDL.h>
#endif

static Vgpu_system* top;
static uint64_t cyc = 0;
static bool trace_on = false;          // --trace: 打印 CP 状态变化
static unsigned last_st = 0xFFFF;
static unsigned last_sm = 0xFFFF;

static constexpr uint32_t LANES_PER_WARP = 8;      // WARP_SIZE(ISA §1)
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
        top->clk = 1;
        top->eval();
        cyc++;
        if (trace_on) {
            unsigned sm = (unsigned(top->dbg_exu_st) << 8) | (unsigned(top->dbg_lsu_st) << 4) |
                          unsigned(top->dbg_gm_wst);
            if (sm != last_sm) {
                std::fprintf(stderr, "[s%llu] wst0=%u exu=%u lsu=%u gmw=%u pc=0x%x busy=%u\n",
                             (unsigned long long)cyc, unsigned(top->dbg_wst0),
                             unsigned(top->dbg_exu_st), unsigned(top->dbg_lsu_st),
                             unsigned(top->dbg_gm_wst), top->dbg_pc0,
                             unsigned(top->dbg_sched_busy));
                last_sm = sm;
            }
        }
        if (trace_on && top->dbg_state != last_st) {
            std::fprintf(stderr, "[c%llu] CP st %u -> %u (idx=%u head=0x%x w0=0x%08x)\n",
                         (unsigned long long)cyc, last_st, unsigned(top->dbg_state),
                         top->dbg_idx, top->dbg_head, top->dbg_w0);
            last_st = unsigned(top->dbg_state);
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
//  命令环:命令按 64B 对齐(驱动契约, CMDS §2)
// ---------------------------------------------------------------------------
struct Queue {
    uint32_t base;
    uint32_t size = 0x1000;                   // CMD_QUEUE_SIZE 默认值(CMDS §7, 环在 4KB 窗口内)
    uint32_t off = 0;                         // 已用字节

    uint32_t tail_addr() const { return base + off; }

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
    std::fprintf(stderr, "  CP_STATUS=0x%x CMD_HEAD=0x%x CMD_TAIL=0x%x\n", st, hd, tl);
    std::fprintf(stderr, "  调试: st=%u idx=%u words=%u crc=0x%08x hdr4=0x%08x\n",
                 unsigned(top->dbg_state), top->dbg_idx, top->dbg_words,
                 top->dbg_crc, top->dbg_hdr4);
    std::fprintf(stderr, "  hdr0=0x%08x hdr1=0x%08x hdr2=0x%08x hdr5=0x%08x w3=0x%08x\n",
                 top->dbg_hdr0, top->dbg_hdr1, top->dbg_hdr2, top->dbg_hdr5, top->dbg_w3);
    std::fprintf(stderr, "  len=%u cw2=0x%08x cw3=0x%08x cw4=0x%08x\n",
                 unsigned(top->dbg_len), top->dbg_cw2, top->dbg_cw3, top->dbg_cw4);
    std::fprintf(stderr, "  SM0: wst0=%u smerr=%u pc=0x%x warp_run=0x%02x\n",
                 unsigned(top->dbg_wst0), unsigned(top->dbg_smerr0),
                 top->dbg_pc0, unsigned(top->dbg_wrun0));
    std::fprintf(stderr, "  exu_st=%u lsu_st=%u gm_wst=%u wst_all=0x%x sched_busy=%u\n",
                 unsigned(top->dbg_exu_st), unsigned(top->dbg_lsu_st),
                 unsigned(top->dbg_gm_wst), unsigned(top->dbg_wst_all),
                 unsigned(top->dbg_sched_busy));
    std::fprintf(stderr, "  FB: %08x %08x %08x %08x / RAM0: %08x %08x\n",
                 ram_read(0x1000'0000), ram_read(0x1000'0004),
                 ram_read(0x1000'0008), ram_read(0x1000'000c),
                 ram_read(0x0000'0000), ram_read(0x0000'0004));
    std::fprintf(stderr, "  CONST: %08x %08x %08x %08x\n",
                 ram_read(0x0200'0000), ram_read(0x0200'0004),
                 ram_read(0x0200'0008), ram_read(0x0200'000c));
    std::fprintf(stderr, "  ovf=%u lddone=%u\n", unsigned(top->dbg_ovf), unsigned(top->dbg_lddone));
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

static int check_matmul() {
    uint32_t bad = 0, first = 0;
    for (uint32_t i = 0; i < MM_N; ++i) {
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
                     bad, MM_N * MM_N, first / MM_N, first % MM_N,
                     ram_read(MM_C + first * 4));
        return 4;
    }
    std::printf("matmul 校验通过: C=A×B 的 %u 个元素全部按位一致(%u×%u×%u = %u 次 MAC)\n",
                MM_N * MM_N, MM_N, MM_N, MM_N, MM_N * MM_N * MM_N);
    return 0;
}

// ---------------------------------------------------------------------------
//  SDL2 窗口(可选): 把帧缓冲直接贴到纹理上, 每帧 present 一次
// ---------------------------------------------------------------------------
#if defined(RUNSIM_HAVE_SDL)
class Viewer {
public:
    bool init(int w, int h, int scale)
    {
        w_ = w; h_ = h;
        if (SDL_Init(SDL_INIT_VIDEO) != 0) {
            std::fprintf(stderr, "SDL_Init 失败: %s\n", SDL_GetError());
            return false;
        }
        win_ = SDL_CreateWindow("SimpleGPU — RTL 全系统仿真", SDL_WINDOWPOS_CENTERED,
                                SDL_WINDOWPOS_CENTERED, w * scale, h * scale, SDL_WINDOW_SHOWN);
        if (!win_) { std::fprintf(stderr, "SDL_CreateWindow 失败: %s\n", SDL_GetError()); return false; }
        rnd_ = SDL_CreateRenderer(win_, -1, SDL_RENDERER_ACCELERATED | SDL_RENDERER_PRESENTVSYNC);
        if (!rnd_) rnd_ = SDL_CreateRenderer(win_, -1, SDL_RENDERER_SOFTWARE);
        if (!rnd_) { std::fprintf(stderr, "SDL_CreateRenderer 失败: %s\n", SDL_GetError()); return false; }
        tex_ = SDL_CreateTexture(rnd_, SDL_PIXELFORMAT_ARGB8888, SDL_TEXTUREACCESS_STREAMING, w, h);
        if (!tex_) { std::fprintf(stderr, "SDL_CreateTexture 失败: %s\n", SDL_GetError()); return false; }
        return true;
    }

    void present(const std::vector<uint32_t>& px)
    {
        if (!tex_) return;
        SDL_UpdateTexture(tex_, nullptr, px.data(), w_ * int(sizeof(uint32_t)));
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

int main(int argc, char** argv)
{
    std::string image = "build/clear.bin", kernel;
    std::string dump;
    uint32_t warps = 4, fb_base = 0x1000'0000, fb_count = 0, fb_expect = 0;
    bool have_expect = false;
    uint64_t max_cycles = 5'000'000;
    std::vector<uint32_t> params;

    // anim demo(逐帧循环)的选项
    bool  view = false;
    int   frames = 120, fps = 30, scale = 4, box = 12;
    uint32_t cw = 64, ch = 64;
    bool  frames_given = false;

    for (int i = 1; i < argc; i++) {
        std::string a = argv[i];
        auto nxt = [&]() { return (i + 1 < argc) ? argv[++i] : ""; };
        if (a == "--image") image = nxt();
        else if (a == "--warps") warps = uint32_t(std::strtoul(nxt(), nullptr, 0));
        else if (a == "--params") params = parse_list(nxt());
        else if (a == "--fb-base") fb_base = uint32_t(std::strtoul(nxt(), nullptr, 0));
        else if (a == "--fb-count") fb_count = uint32_t(std::strtoul(nxt(), nullptr, 0));
        else if (a == "--fb-expect") { fb_expect = uint32_t(std::strtoul(nxt(), nullptr, 0)); have_expect = true; }
        else if (a == "--cycles") max_cycles = std::strtoull(nxt(), nullptr, 0);
        else if (a == "--dump") dump = nxt();
        else if (a == "--trace") trace_on = true;
        else if (a == "--kernel") kernel = nxt();
        else if (a == "--view") view = true;
        else if (a == "--frames") { frames = std::atoi(nxt()); frames_given = true; }
        else if (a == "--fps") fps = std::atoi(nxt());
        else if (a == "--scale") scale = std::atoi(nxt());
        else if (a == "--box") box = std::atoi(nxt());
        else if (a == "--w") cw = uint32_t(std::strtoul(nxt(), nullptr, 0));
        else if (a == "--h") ch = uint32_t(std::strtoul(nxt(), nullptr, 0));
        else { std::fprintf(stderr, "未知选项 %s\n", a.c_str()); return 2; }
    }
    // 给了窗口就默认一直跑(关窗/按 ESC 结束); 没有窗口时跑一帧就够
    if (view && !frames_given) frames = 0;
    if (kernel == "anim" && !view && !frames_given) frames = 1;

    Verilated::commandArgs(argc, argv);
    top = new Vgpu_system;
    reset();

    // ---- 1. 镜像进 DDR ----
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
    // 镜像头 word3 = 标志位(BARRIER/SHARED/GLOBAL_WRITE)。LAUNCH 的 flags 必须与之一致
    // (CMDS §5 交叉核对, CP 不匹配就报 CMD_ERROR), 所以驱动要把镜像自己的标志传下去。
    const uint32_t img_flags = (uint32_t(buf[12]) | uint32_t(buf[13]) << 8 |
                                uint32_t(buf[14]) << 16 | uint32_t(buf[15]) << 24);
    std::printf("镜像 %s: %ld 字节, entry=0x%x\n", image.c_str(), sz, entry);

    // ---- 1.5 内核预设: 参数与输入缓冲 ----
    if (kernel == "gouraud") {
        if (params.empty()) {
            params = { G_IN, G_OUT, G_N,
                       fbits(1.5f), fbits(0.25f), fbits(-0.5f), fbits(2.0f),
                       fbits(3.0f), fbits(-1.0f), 24 };
        }
        init_gouraud_bufs();
    } else if (kernel == "matmul") {
        if (params.empty()) params = { MM_A, MM_B, MM_C, MM_N, MM_N, MM_N };
        init_matmul_bufs();
    }

    // ---- 2. 命令流: LOAD_KERNEL 先跑完, 后面的帧再逐条追加 ----
    Queue q{0x0100'0000};
    submit_load_kernel(q, IMG_BASE, uint32_t(sz), 0x3);
    if (int rc = flush_and_wait(q, max_cycles, "LOAD_KERNEL")) return rc;
    std::printf("LOAD_KERNEL 完成\n");

    // ---- 3. anim demo: 逐帧 PARAMS → LAUNCH → WAIT, 每帧读回并显示 ----
    if (kernel == "anim") {
        const uint64_t c0 = cyc;
        uint32_t n_frames = 0, bad_frames = 0, bad_first = 0;
        std::vector<uint32_t> fb(size_t(cw) * size_t(ch), 0);

#if defined(RUNSIM_HAVE_SDL)
        Viewer viewer;
        if (view && !viewer.init(int(cw), int(ch), scale > 0 ? scale : 4)) return 7;
#else
        if (view) {
            std::fprintf(stderr, "--view 需要 SDL2(编译时未找到), 请先安装 libsdl2-dev 并重新 make\n");
            return 7;
        }
#endif
        std::printf("anim demo: 画布 %ux%u, warp %u, 帧数 %d(%s), 限速 %d fps\n",
                    cw, ch, warps, frames, frames == 0 ? "0 = 一直跑" : "--frames", fps);

        const auto frame_budget = std::chrono::duration<double>(fps > 0 ? 1.0 / fps : 0.0);
        auto t_next = std::chrono::steady_clock::now();
        const auto t_start = t_next;
        bool quit = false;

        for (uint32_t frame = 0; !quit; frame++) {
            // 新的一帧: 推参数 → 启动 → 等信号量(LAUNCH 用 signal_id=0, 完成时 SEM[0] += 1)
            const std::vector<uint32_t> P = anim_params(cw, ch, warps, fb_base, frame, uint32_t(box));
            submit_params(q, 0, P);
            submit_launch(q, entry, warps, img_flags, 0, 0xFFFF);
            submit_wait(q, SEM_BASE, frame + 1);
            if (int rc = flush_and_wait(q, max_cycles, "anim 帧")) return rc;

            for (uint32_t i = 0; i < cw * ch; i++) fb[i] = ram_read(fb_base + 4 * i);
            ++n_frames;

            uint32_t first = 0;
            const uint32_t bad = anim_bad_pixels(fb, P, cw, ch, frame, &first);
            if (bad) {
                if (!bad_frames) bad_first = first;
                ++bad_frames;
                if (bad_frames <= 3)
                    std::fprintf(stderr, "帧 %u 与参考模型不符: %u/%u 个像素, 首个 %u\n",
                                 frame, bad, cw * ch, first);
            }

#if defined(RUNSIM_HAVE_SDL)
            if (view) {
                viewer.present(fb);
                if (!viewer.poll()) quit = true;
            }
#endif
            if (frames > 0 && frame + 1 >= uint32_t(frames)) quit = true;

            // 每 10 帧报一次进度(窗口模式下用户看不到 stdout 时才需要; 这里统一打)
            if ((frame + 1) % 10 == 0 && !quit) {
                const double el = std::chrono::duration<double>(
                    std::chrono::steady_clock::now() - t_start).count();
                std::printf("  ... 第 %u 帧, %.1f 拍/帧, 实际 %.2f 帧/秒\n",
                            frame + 1, double(cyc - c0) / double(n_frames),
                            el > 0 ? double(n_frames) / el : 0.0);
                std::fflush(stdout);
            }

            // 限速(不超过 fps; 仿真本身更慢时不额外等)
            if (fps > 0 && !quit) {
                t_next += std::chrono::duration_cast<std::chrono::steady_clock::duration>(frame_budget);
                std::this_thread::sleep_until(t_next);
            }
        }

        std::printf("anim 完成: %u 帧, 共 %llu 拍(%.1f 拍/帧), 每帧 %ux%u 像素\n",
                    n_frames, (unsigned long long)(cyc - c0),
                    n_frames ? double(cyc - c0) / double(n_frames) : 0.0, cw, ch);
        if (bad_frames) {
            std::fprintf(stderr, "anim 校验失败: %u/%u 帧与参考模型不符(首个坏像素 %u)\n",
                         bad_frames, n_frames, bad_first);
            return 4;
        }
        std::printf("anim 校验通过: %u 帧全部与参考模型按位一致\n", n_frames);
        std::printf("统计: 行请求 %u, MMIO %u, 未映射 %u\n",
                    top->ctr_lines, top->ctr_mmio, top->ctr_unmapped);
        delete top;
        return 0;
    }

    // ---- 3'. 其余内核: 一次 PARAMS → LAUNCH → WAIT ----
    if (!params.empty()) submit_params(q, 0, params);
    submit_launch(q, entry, warps, img_flags, 0, 0xFFFF);
    submit_wait(q, SEM_BASE, 1);
    if (int rc = flush_and_wait(q, max_cycles, "LAUNCH")) return rc;

    std::printf("内核执行完成, 周期 = %llu\n", (unsigned long long)cyc);

    // ---- 3.5 内核结果校验 ----
    if (kernel == "gouraud") { int rc = check_gouraud(params); if (rc) return rc; }
    else if (kernel == "matmul") { int rc = check_matmul(); if (rc) return rc; }

    // ---- 4. 读回帧缓冲 ----
    std::vector<uint32_t> fb;
    if (fb_count) {
        fb.reserve(fb_count);
        for (uint32_t i = 0; i < fb_count; i++) fb.push_back(ram_read(fb_base + 4 * i));
        std::printf("帧缓冲[0..3] = 0x%08x 0x%08x 0x%08x 0x%08x\n",
                    fb[0], fb.size() > 1 ? fb[1] : 0, fb.size() > 2 ? fb[2] : 0,
                    fb.size() > 3 ? fb[3] : 0);
        if (have_expect) {
            uint32_t bad = 0;
            for (uint32_t i = 0; i < fb_count; i++)
                if (fb[i] != fb_expect) {
                    if (bad < 4)
                        std::printf("  像素 %u = 0x%08x != 0x%08x\n", i, fb[i], fb_expect);
                    bad++;
                }
            if (bad) {
                std::fprintf(stderr, "帧缓冲校验失败: %u / %u 个像素不符\n", bad, fb_count);
                return 5;
            }
            std::printf("帧缓冲校验通过(%u 个像素)\n", fb_count);
        }
        if (!dump.empty()) {
            FILE* g = std::fopen(dump.c_str(), "wb");
            if (!g) return 6;
            std::fwrite(fb.data(), 4, fb.size(), g);
            std::fclose(g);
            std::printf("帧缓冲已写入 %s\n", dump.c_str());
        }
    }

    std::printf("统计: 行请求 %u, MMIO %u, 未映射 %u\n",
                top->ctr_lines, top->ctr_mmio, top->ctr_unmapped);
    delete top;
    return 0;
}
