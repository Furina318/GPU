# RTL 全系统仿真(`sim/run-sim/`)

这里把**整颗 GPU 的 RTL 连起来跑真实程序**:host 通过 AXI4-Lite 敲 CP 的 MMIO,
CP 从命令队列取命令、把内核镜像搬进指令 SRAM、启动 SM;两个 SM 执行
`sw/kernels/*.S` 里的内核,访存走共享内存 / 写合并缓冲 / 多主 AXI4 仲裁,
最终落到一块 DDR 模型上。

它是**软件参考模型**(`sim/simulator/`)之外的第二个实现:同一个 `.S` 内核在两
边跑,结果按位对比。参考模型用来验证算法与 ISA 语义,RTL 仿真用来验证硬件。

```
  host (C++ 测试台)
        │ AXI4-Lite
   ┌────▼────┐   mem_rd/mem_wr    ┌──────────────────┐
   │   cp    ├──── 行/字访存 ─────►│ cp_mem_adapter   ├──┐
   └────┬────┘                    └──────────────────┘  │ AXI4 主口 0
        │ launch / sm_done                  ┌───────────┴───────────┐
   ┌────▼──────────┐  ┌────────────────┐   │                       │
   │   core (SM0)  │  │   core (SM1)   │   │      axi_arb          │
   │ fetch/decode  │  │ fetch/decode   │   │  3 主 → 1 个 AXI4     │
   │ exu/lsu/regs  │  │ exu/lsu/regs   │   │  CP 最高优先          │
   │ instr_mem     │  │ instr_mem      │   │  读/写通道各自轮转    │
   │ shared_mem    │  │ shared_mem     │   └───────────┬───────────┘
   └────┬──────────┘  └───────┬────────┘               │
        │ gmem 行读写          │ gmem 行读写              ▼
   ┌────▼─────┐        ┌──────▼────┐           ┌─────────────────┐
   │global_mem│        │global_mem │           │ axi_ram_model   │
   │  + WCB   │        │  + WCB    │           │   (DDR)         │
   └────┬─────┘        └─────┬─────┘           └─────────────────┘
        └──── AXI4 主口 1/2 ──┴──────────────────────►
```

## 快速开始

```bash
# 1) 编译测试台(Verilator 全量 C++ 编译,约 5 分钟,只在 RTL 变了才需要重来)
cd sim/run-sim && make

# 2) 一次跑三个内核并校验(需要先有镜像,见下)
make check

# 单个内核
make clear      # 清屏内核: 校验帧缓冲像素
make gouraud    # 顶点仿射变换: 逐顶点逐位校验
make matmul     # 32×32×32 INT→FP32 GEMM: 逐元素逐位校验
make demo       # anim 动画: SDL 窗口逐帧重算并显示(关窗 / ESC 结束)
make anim       # 逐帧跑 anim 并与参考模型对拍(4 帧, 无窗口)
make demo-headless   # 同上但帧数可调(默认 60 帧)
```

`make demo` 的画布默认 32×32(窗口 256×256):RTL 仿真的速度约 2 万拍/秒,而一帧
64×64 要 6.5 万拍、32×32 只要 1.8 万拍 —— 这样才能看到"逐帧在动"。想要更流畅
可以 `make demo DEMO_W=16 DEMO_H=16 DEMO_SCALE=16`,想要大画面就
`make demo DEMO_W=64 DEMO_H=64`(约 3 秒一帧)。

`make demo` 每帧都是真的在 RTL 上跑一遍内核:host 写一帧的参数(PARAMS)、
LAUNCH、等信号量、把帧缓冲读回来推进 SDL 纹理。RTL 仿真的速度约 1.5 万拍/秒,
一帧 64×64 大约 6.5 万拍(≈4 秒),所以它演示的是"逐帧推进"而不是实时动画;
想快一点就缩小画布(`--w 32 --h 32`)或降帧数。

镜像(`sim/simulator/build/*.bin`)由 `sw/` 的汇编器生成:

```bash
make -C sim/run-sim images     # = make -C sim/simulator images
```

## 测试台参数

`build/run_sim` 直接跑也可以,参数如下:

| 参数 | 含义 |
|---|---|
| `--image FILE` | 内核镜像(默认 `build/clear.bin`;相对路径按当前目录解析) |
| `--warps N` | 启动几个 warp(1–4;>4 时 CP 会把内核同时发到两个 SM) |
| `--params a,b,c,...` | 写进常量区 `0x0200_0000` 的 32 位字。浮点参数必须给**位模式十六进制**(`strtoul(...,0)` 会把 `1.5` 读成 1) |
| `--fb-base/-count/-expect` | 帧缓冲基址、要校验的像素个数、期望像素值 |
| `--kernel clear\|gouraud\|matmul` | 用内核自带的输入/校验逻辑(会自动铺输入缓冲与常量区) |
| `--cycles N` | 超时拍数(默认 5 000 000) |
| `--kernel anim` | anim demo: 每帧推参数 → LAUNCH → WAIT → 读回帧缓冲 → 显示 |
| `--view` | 打开 SDL 窗口显示(需要 SDL2;不给 `--frames` 就一直跑到关窗) |
| `--frames N` | 跑多少帧;`0` = 一直跑(`--view` 时默认 0,否则默认 1) |
| `--fps N` | 限速(默认 30;RTL 本身通常更慢,限速只影响快的那部分) |
| `--scale N` | 窗口缩放倍数(默认 4) |
| `--w N` / `--h N` | 画布尺寸(默认 64×64;anim 要求方形且边长是 2 的幂) |
| `--box N` | anim 菱形块半径(默认 12) |
| `--trace` | 打印 CP 状态迁移与 SM 内部状态(调试用) |
| `--dump FILE` | 跑完把 DDR 的一段导出到文件 |

## 跑一个内核时到底发生了什么

1. 测试台把镜像逐字写进 DDR 的 `0x0200_1000`(`KERNEL_IMG_BASE`),并从镜像头
   取 `entry` 与 `flags`。
2. 一次性铺好命令流:`LOAD_KERNEL → PARAMS → LAUNCH → WAIT`,每条命令按
   **64B 边界**补齐(见下),然后写 `CMD_TAIL` 与 `DOORBELL`。
3. CP 校验镜像 CRC、把代码搬进 SM 的指令 SRAM、把 `PARAMS` 写进常量区,再启动 SM。
4. SM 跑内核,`bar.sync` 触发写合并缓冲冲刷,`exit` 汇总成 `SM_DONE`;
   CP 置信号量,`WAIT` 命令满足后置 `LAUNCH_DONE` 中断。
5. 测试台回到 IDLE 后读回结果并校验。

**两个容易踩的契约**(都写在 `docs/CMDS.md`):

- 命令必须补齐到 64B 边界,而且**补齐物必须是一条 NOP**,不能是一串 0 ——
  `len=0` 的命令会被 CP 判成 `CMD_ERROR`。
- `LAUNCH` 的 `flags` 必须与镜像头 word3 一致,否则 CP 按 `CMDS.md` §5 判
  `CMD_ERROR`。

## 与参考模型的对拍

| 内核 | 参考模型 | RTL 全系统 |
|---|---|---|
| `clear.S` | `make check` → `clear` | 周期 2166,行请求 48 |
| `gouraud.S` | `make check` → `gouraud` | 周期 7720,行请求 348,32 顶点逐位一致 |
| `matmul.S` | `make check` → `matmul` | 周期 173438,行请求 1907,1024 元素逐位一致 |
| `anim.S` | `make anim` | `make anim` / `make demo-headless`:每帧逐像素一致(64×64 一帧 ≈ 6.5 万拍,32×32 ≈ 1.8 万拍) |

`gouraud`/`matmul` 的期望值在测试台里用 `std::fma`/`std::fmaf` 重算一遍(单次
舍入,与内核的 `fma.f32` 同语义),按位比较;`clear` 直接比较像素颜色。

## 目录

```
sim/run-sim/
├── Makefile            # Verilator 编译 + 各内核/demo 的运行目标
├── tb_main.cpp         # C++ 测试台: host 侧 MMIO 驱动、命令编码、结果校验
└── rtl/gpu_system.sv   # 系统顶层: CP + 2×core + 2×global_mem + axi_arb + DDR 模型
```

`gpu_system.sv` 里的 `loc_addr()` 把架构地址折叠到 `axi_ram_model` 的连续存储上
(它只有一块内存,不是真的分段地址空间);这只是仿真便利,不影响 RTL 行为。

## 还没接的部件

2D 引擎与矩阵引擎的 RTL 尚未实现,系统顶层把 `eng_ready` 接 0、引擎状态接 0。
因此走 2D 路径的 demo(`car` 的引擎版本)目前只能在参考模型上跑。
