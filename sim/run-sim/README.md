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
# 1) 编译测试台(Verilator 全量 C++ 编译;只有 RTL 变了才需要重来)
make -C sim/run-sim

# 2) 跑内核。运行只有一个入口: make run,参数写进 ARGS
make -C sim/run-sim run ARGS="--image clear --warps 8"
make -C sim/run-sim run ARGS="--image gouraud"
make -C sim/run-sim run ARGS="--image matmul --warps 8"
make -C sim/run-sim run ARGS="--image clear --fst wave.fst"   # 存波形
make -C sim/run-sim run ARGS="--help"                         # 全部选项
```

`--image` 直接写**内核名**(`clear` / `gouraud` / `matmul` / `anim`),测试台按
"当前目录 → `sim/simulator/build/` → 可执行文件旁的 `../simulator/build/`" 的顺序
去找 `<名字>.bin`,所以在仓库根目录、在 `sim/run-sim/`、在 `sim/simulator/` 里跑
都一样,不用 `cd`,也不用写 `build/clear.bin`。给相对/绝对路径也照样能用。

**为什么参数不直接跟在 `make run` 后面**:GNU make 会先解析自己的命令行选项,
`--image` 会被它当成非法选项直接报 `unrecognized option '--image'`,所以统一走
`ARGS="..."`。

`--image` 和 `--kernel` 会互相推断,所以少写一个也行:

```bash
make run ARGS="--kernel matmul"                 # = --image matmul --kernel matmul
make run ARGS="--image anim"                    # 内核预设自动取 anim
make run ARGS="--image clear --view --frames 8" # clear 也能开 SDL 窗口
```

两个都给、但指向的不是同一个程序时,测试台会提示一句:预设里的常量区布局与期望
值是按内核写死的,拿 `anim` 的镜像跑 `clear` 的预设只会得到看起来像硬件故障的错。

想少打点字就用别名(它们只是替你写好了 `ARGS`):

```bash
make clear                     # 清屏内核, 校验帧缓冲像素
make gouraud                   # 顶点仿射变换, 逐顶点逐位校验
make matmul                    # 32×32×32 GEMM, 逐元素逐位校验
make anim                      # anim 逐帧动画, 4 帧逐像素对拍(无窗口)
make demo                      # anim + SDL 窗口连续显示(关窗 / ESC 结束)
make demo-headless FRAMES=60   # 同 demo 但不开窗口
make check                     # 依次跑 clear / gouraud / matmul / anim
```

`make demo` 每帧都是真的在 RTL 上跑一遍内核:host 写一帧参数(PARAMS)、LAUNCH、
等信号量、把帧缓冲读回推进 SDL 纹理。RTL 仿真约 2 万拍/秒,一帧 32×32 约 1.8 万
拍(≈1 秒)、64×64 约 6.5 万拍(≈3 秒),所以演示的是"逐帧推进"而不是实时动画;
想更快就缩小画布(`make demo W=16 H=16 SCALE=16`)。

### 显示、校验、波形

三种显示方式可以任选(互不冲突,也可以同时开):

| 选项 | 作用 | 说明 |
|---|---|---|
| `--view` | SDL2 窗口 | 需要 `libsdl2-dev`;启动时会打印 `SDL: video=… renderer=… 窗口 WxH` |
| `--ascii` | 终端 ANSI 真彩色 | 逐帧打印,不需要图形环境 |
| `--ppm-dir DIR` | 每帧写 `DIR/frame_0000.ppm` | 之后可以 `ffmpeg -i frame_%04d.ppm out.mp4` |

"画面"默认取 `--fb-base` 开始的 `w*h` 个 32 位字,每个字当一个 ARGB 像素看。`clear` /
`anim` 写的正是帧缓冲;`gouraud` / `matmul` 不碰帧缓冲,所以它们的默认画面指向自己的
输出区(`0x1000_1000` 的 32×6 个顶点字、`0x1000_2000` 的 32×32 个矩阵字)—— 想换就显式
给 `--fb-base` / `--w` / `--h`。

`clear` 是 memset:`N` 是**总像素数**、`stride` 是并行度(每 lane 隔 `stride` 个写一个),
所以 `--w/--h` 给多大它就填多大(`make run ARGS="--kernel clear --w 64 --h 64 --view"` 是
整幅 64×64 一个颜色)。`gouraud`/`matmul` 的输出大小是内核写死的,给 `--w/--h` 只会多展示
没被写过的内存。

`--frames N` 决定跑多少帧(默认:`--view` 时 `0` = 一直跑到关窗,否则 1 帧);
`--fps N` 只对"仿真跑得比人眼快"的情况限速。

校验可以关掉(`--no-verify`),用来单独看性能或看画面:默认每个内核都会与参考
模型对拍(clear 逐像素、gouraud 逐顶点、matmul 逐元素、anim 逐帧逐像素)。

波形用 `--fst FILE`(单位是拍,文件按需截断):

```bash
make run ARGS="--image clear --fst wave.fst --cycles 200000"
gtkwave wave.fst        # 或者 surge / verilator 自带的工具
```

波形是**逐拍全信号**的,越跑越大:只跑一个小内核(`clear --warps 1` 约 1300 拍)
或者用 `--cycles` 截断,取一段有代表性的窗口就够了。

波形功能在编译期决定,`make` 时用 `TRACE=` 切换:

| `TRACE=` | 效果 |
|---|---|
| `fst`(默认) | 支持 `--fst FILE`,FST 压缩、文件小、GtkWave/Surge 都能读 |
| `vcd` | 支持 `--vcd FILE`(纯文本,文件大得多) |
| `off` | 不编波形支持,编译与仿真都最快 |

FST 需要 lz4 的开发包(`liblz4-dev`,提供 `<lz4.h>` 与 `liblz4.so`)。没装时 `make`
会自动退回 VCD 并打印一行提示,不会编译失败。

### 别名的参数

别名支持的变量都不写就用默认值;要传别的参数就直接用 `make run ARGS="..."`。

| 变量 | 默认值 | 作用 |
|---|---|---|
| `WARPS` | 4 | 启动几个 warp(1–8,见下) |
| `W` / `H` | 32 / 32 | anim 画布尺寸(必须是边长 2 的幂的方形) |
| `BOX` | 6 | anim 菱形块边长 |
| `SCALE` | 8 | SDL 窗口缩放倍数 |
| `FPS` | 30 | 限速 |
| `FRAMES` | 空 | 跑多少帧(`make anim` 默认 4,`demo-headless` 默认 60) |
| `ARGS` | 空 | 追加到别名后面的参数(例如 `ARGS="--no-verify --ascii"`) |

例:`make demo W=64 H=64 BOX=10 SCALE=6 FPS=30`、`make anim WARPS=1 FRAMES=20`、
`make clear WARPS=8 ARGS="--ascii"`。

`make lint` 只做 Verilator 静态检查(`--lint-only -Wall`,约 25 s,不生成 C++、
不写 `build/`),改完 RTL 先跑它比全量重编快得多。当前 `gpu_system` 全层次
**零 warning 零 error**。

### warp 数:1–8 都支持

一颗芯片有 **2 SM × 4 warp = 8 个 warp**(`WARP_SIZE=8` lane,所以 64 个 lane)。
`--warps N` 是**全系统**的 warp 数(CMDS §4.2 的放置规则):

- `N ≤ 4`:只启动 SM0,`wid` 0..N−1;
- `N ≥ 5`:SM0 拿 `wid` 0..3、SM1 拿 4..7,每个 SM 都是 4 个 warp;
- `wmov` 给出的是**全局** `wid`(SM1 的基址是 4),所以 `idx = tid + wid*8` 这类
  写法在跨 SM 时仍然连续、不重不漏;
- 带 `bar.sync` / 跨 warp 共享内存的内核只能单 SM 跑(`num_warps ≤ 4`,CMDS §4.1
  第 7 条)—— 两个 SM 之间没有屏障设施。

内核的分工是 `idx = tid + wid*8`,也就是**只有前 `warps*8` 个元素会有人算**。所以
`--warps 1` 时 gouraud 只处理前 8 个顶点、matmul 只算前 8 行 —— 这不是硬件错误,
而是驱动契约(内核头注释写着"N ≤ num_warps*8")。测试台就按这个契约**自动缩放
工作量**:gouraud 的顶点数、matmul 校验的行数、clear 的像素数都取
`min(内核上限, warps*8)`,于是 `--warps 1/2/3` 也能通过校验(以前是拿满额的
期望值去比,于是报"顶点不符")。

镜像(`sim/simulator/build/*.bin`)由 `sw/` 的汇编器生成:

```bash
make -C sim/run-sim images     # = make -C sim/simulator images
```

## 测试台参数

`build/run_sim` 也可以直接在 `sim/simulator/` 下跑(镜像路径相对它),全部选项见
`--help`。常用的:

| 参数 | 含义 |
|---|---|
| `--image NAME` | 内核名(`clear`/`gouraud`/`matmul`/`anim`,自动找 `sim/simulator/build/<NAME>.bin`)或镜像路径(默认 `anim`) |
| `--kernel NAME` | `clear` / `gouraud` / `matmul` / `anim` 的预设输入与校验;省略时按镜像名推断,推断不出来就只跑不校验 |
| `--warps N` | 全系统 warp 数(1–8;>4 时 SM0 跑 `wid` 0..3、SM1 跑 4..7) |
| `--w N` / `--h N` | 画面尺寸(anim 默认 32×32;clear 默认 `warps*8` × 1,给多大就填多大;gouraud 6×顶点数、matmul 32×32) |
| `--box N` | anim 菱形块边长(默认 6) |
| `--frames N` | 跑多少帧;`0` = 一直跑到关窗(`--view` 时的默认值) |
| `--fps N` | 限速(默认 30;`0` = 不限速) |
| `--view` / `--ascii` / `--ppm-dir DIR` | 三种显示方式(见上);`--ascii-cols/--ascii-rows` 控制终端降采样 |
| `--scale N` | SDL 窗口放大倍数(默认 6;画面很扁时自动放大到至少 64 像素高、宽度不超过 1024) |
| `--vsync` | 让 SDL 跟显示器刷新同步(默认不跟,由 `--fps` 限速) |
| `--no-verify` | 跳过参考模型校验 |
| `--fst FILE` / `--vcd FILE` | 导出波形(见上) |
| `--cycles N` | 单条命令的超时拍数(默认 5 000 000) |
| `--fb-base ADDR` | 显示/读回用的缓冲基址(默认 `0x1000_0000`) |
| `--dump FILE` | 把读回的画面按原始 32 位字写文件 |
| `--params a,b,c,...` | 覆盖内核预设的常量区。浮点参数必须给**位模式十六进制**(`strtoul(...,0)` 会把 `1.5` 读成 1) |
| `--trace` | 逐拍打印 CP 状态迁移与 SM 内部状态(字符流调试,波形请看 `--fst`) |

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

**逐帧 demo 的环管理**:每帧开始前测试台把 `CMD_HEAD`/`CMD_TAIL` 都写回队列基址
(`CMDS.md` §7 允许 host 直写 `CMD_HEAD`),再写这一帧的三条命令。不这么做的话,
环绕满一圈后 CP 会先把基址残留的旧命令再读一遍(相当于重放整段历史),而测试台
同时在同一个位置写新帧 —— 命令与参数就错配了,表现为内核跑到一半卡死。

## 与参考模型的对拍

| 内核 | 参考模型 | RTL 全系统(`--warps 4`) |
|---|---|---|
| `clear.S` | `make test` → `clear` | 1478 拍,行请求 44,32 个像素逐位一致 |
| `gouraud.S` | `make test` → `gouraud` | 6434 拍,行请求 148,32 个顶点逐位一致 |
| `matmul.S` | `make test` → `matmul` | 162926 拍,1024 个元素逐位一致 |
| `anim.S` | `make anim` | 每帧约 17700 拍(32×32),逐帧逐像素一致 |

`gouraud`/`matmul` 的期望值在测试台里用 `std::fma` 重算一遍(单次舍入,与内核的
`fma.f32` 同语义),按位比较;`clear` 直接比较像素颜色;`anim` 按 `anim.S` 的公式
重算整幅画面。

`--warps` 只改"谁来算":`warps 8` 时 clear 1646 拍、gouraud 8520 拍(`matmul` 有
共享内存/屏障,按 `CMDS.md` §4.1 只能 ≤4 warp,给 8 会被 CP 判 `LAUNCH_REJECT`)。

## 目录

```
sim/run-sim/
├── Makefile            # Verilator 编译 + make run 入口(其余目标都是它的别名)
├── tb_main.cpp         # C++ 测试台: host 侧 MMIO 驱动、命令编码、显示、校验、波形
└── rtl/gpu_system.sv   # 系统顶层: CP + 2×core + 2×global_mem + axi_arb + DDR 模型
```

`gpu_system.sv` 里的 `loc_addr()` 把架构地址折叠到 `axi_ram_model` 的连续存储上
(它只有一块内存,不是真的分段地址空间);这只是仿真便利,不影响 RTL 行为。

## 还没接的部件

2D 引擎与矩阵引擎的 RTL 尚未实现,系统顶层把 `eng_ready` 接 0、引擎状态接 0。
因此走 2D 路径的 demo(`car` 的引擎版本)目前只能在参考模型上跑。
