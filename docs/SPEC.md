# SPEC.md —— SimpleGPU 系统规格与模块接口

版本 0.5 · 关联文档:`ISA.md`(Shader 核)/ `CMDS.md`(命令集)/ `GFX.md`(2D 引擎)· 变更记录见 [附录 A](#附录-a变更记录)

> **读者指引**
>
> | 你要做的事 | 读 |
> |---|---|
> | 接总线 / 连模块(信号级) | §3 四个模块的端口表 + 契约(`hw/rtl/` 里的实现按它写) |
> | 判断"这个地址/这件事归谁管" | §2 地址空间(唯一权威)+ §5 文档权威归属表 |
> | 知道"哪部分还没做" | §7 待定项登记 |
> | 跑起来看效果 | 仓库根 `README.md` 的"快速开始" |

---

## 1. 系统组成

一台**固定功能 + 可编程**混合的教学 GPU:一个命令处理器(CP)、两个 SIMT 着色器核(SM)、
一个矩阵引擎、一个 2D 引擎,共享一套内存子系统与 host 接口(MMIO + 命令环)。

```
        host(driver)
           │  MMIO 写(门铃/队列指针)          ▲ 读(状态/错误/计数)
           ▼                                 │
   ┌───────────────────────────────────────────────────────────┐
   │ CP  命令处理器                                             │
   │  · 从命令环取命令 → 校验(CMDS §2.4 表)→ 执行/分发                 │
   │  · 依赖与完成: 信号量 SEM[id]、SM_DONE、超时与复位           │
   └───┬───────────────┬───────────────┬───────────────┬───────┘
       │ LAUNCH        │ GEMM 描述符   │ 2D 命令        │ LOAD_KERNEL
       ▼               ▼               ▼               ▼
  ┌─────────┐    ┌───────────┐   ┌───────────┐    ┌──────────────┐
  │ SM0/SM1 │    │ 矩阵引擎   │   │ 2D 引擎   │    │ 指令 SRAM    │
  │ 4 warp  │    │ 16×16 脉动 │   │ 光栅/填充/│    │ 16KB/SM      │
  │ ×8 lane │    │ (INT8)    │   │ 混合(待定) │    │ (4096 条)    │
  └────┬────┘    └─────┬─────┘   └─────┬─────┘    └──────────────┘
       │               │               │
       └───────────────┴───────────────┘
                       ▼
        ┌──────────────────────────────────────┐
        │ 内存子系统: AXI4-Lite(MMIO) + AXI4    │
        │ 主口(DDR/帧缓冲)+ 写合并缓冲           │
        └──────────────────────────────────────┘
```

| 模块 | 职责 | 规格出处 | 参考模型(已有) |
|---|---|---|---|
| **CP** | 命令环取指/校验/分发、依赖(信号量)、完成判定、超时与复位 | `CMDS.md` | `sim/simulator/src/cp.cpp` |
| **SM ×2** | 取指/译码/发射(FGMT)、lane 数据通路、谓词、SIMT 栈、访存合并、共享内存、barrier、错误停机 | `ISA.md` | `sim/simulator/shader/{exec,inst,decode,regfie,trace}.cpp` |
| **FPU**(在 SM 内) | RV32F 语义的加法/乘法/融合乘加/比较/转换;`fdiv`/`fsqrt` 可以存在于 RTL 模块里,**但 ISA 不暴露这两条指令**(ISA §3.3:D6) | `ISA.md` §3.3/§5.2 | `shader/inst.cpp`(`fp_r`/`fp_fma`) |
| **矩阵引擎** | `C = A×B (+C)`,INT8→INT32,重量化(待定) | `CMDS.md` §6.11 | `sim/simulator/src/cp.cpp`(标量参考版) |
| **2D 引擎** | 填充/位块传送/三角形光栅化/混合/ROP 写缓冲 | `GFX.md`(全篇) | `sim/simulator/2D-engine/gfx2d.cpp` + **独立 Python 参考模型** `sim/simulator/tests/gfx2d_ref.py` |
| **内存子系统** | MMIO 从口、DDR/帧缓冲主口、写合并缓冲 | --- | `sim/simulator/src/memory.cpp` |
| **host 接口** | MMIO 寄存器表 + 命令环 + 信号量区 | `CMDS.md` §2/§7/§10 | 同上 |

**规模(定值,来自 `ISA.md` §2.2/§3.2)**:2 SM × 4 warp × 8 lane = 最多 64 线程;
指令 SRAM 16KB/SM(4096 条,**无 icache**);共享内存 16KB/SM(每 SM 私有);
SIMT 栈 8 深/warp(12B/帧,96B/warp)。

**参考模型的代码布局**(与模块一一对应,见 `sim/simulator/README.md` §4):
`shader/` = SIMT 核 · `2D-engine/` = 固定功能 2D 引擎 · `src/` = CP/内存/前端/工具 · `include/` = 公共头。

---

## 2. 地址空间与归属

**本节是唯一的地址权威**(`CMDS.md` §7.2 是它的详版,含每个 MMIO 寄存器的读写属性):

| 区间 | 大小 | 用途 | 谁访问 |
|---|---|---|---|
| `0x0000_0000`–`0x0000_0FFF` | 4KB | MMIO:CP/各引擎寄存器 | host(R/W)、CP 内部 |
| `0x0100_0000`–`0x0100_0FFF` | 4KB(默认) | 命令环(环形,`CMD_QUEUE_BASE/SIZE`) | host 写、CP 读 |
| `0x0200_0000`–`0x0200_0FFF` | 4KB | **常量区**:内核参数(基址被 ISA 硬编码) | host / `PARAMS` 写、内核读 |
| `0x0200_1000`–`0x0200_7FFF` | 28KB | 内核镜像区(`LOAD_KERNEL` 的源) | host 写、CP 读 |
| `0x0300_0000` 起 | — | 信号量区(`SEM_BASE`,槽位 `id*4`,`id ≤ 255`) | CP 读改写、驱动读写 |
| `0x1000_0000` 起 | 见下 | **帧缓冲**(`FB_BASE`,R/W 可配置) | SM 写、2D 引擎读写、host 读 |
| `0x8000_0000` 起 | — | 外部 DDR(上板) | 所有主口 |
| 其余 | — | 驱动自分配(顶点缓冲、纹理、临时缓冲) | 驱动契约 |

- **帧缓冲尺寸/格式**:`FB_BASE` 可配置(复位 `0x1000_0000`);**尺寸与格式是 2D 引擎的寄存器**
  (`FB_WIDTH/HEIGHT/STRIDE/FORMAT`,经 `2D_SET_REG` 写,`GFX.md` §3)。格式默认 RGBA8888(可选 RGB565)、行主序;
  参考模型默认 640×480(= 307200 像素 ≈ 1.2MB,即 `clear.S` 的 `N` 参数)。
  内核不需要知道帧缓冲几何:它从常量区参数拿自己的 `N`/`W`(§7-2 已关闭)。
- **2D 引擎的状态/错误**在 CP MMIO 的 `2D_STATUS`(0x68)/`2D_ERROR`(0x6C),编码见 `GFX.md` §7.2/§7.3
  (**已定义、未实现**,见 §7-1)。
- **MMIO 基址定为 `0x0000_0000`**。
- **命令长度定为变长 `length_words`,但驱动应把每条命令填充到 64B 边界**
  (`CMDS.md` §2.1:保留自描述的可扩展性)。
- **内核访问 MMIO 窗口 = 架构错误 `MMIO_ACCESS`**(`CMDS.md` §8.2 第 6 项):MMIO 属 CP/host,
  放行等于允许内核误写 `CP_RESET`/`DOORBELL`。

---

## 3. 模块接口(给 RTL 的信号级契约)

约定:`clk`/`rst_n` 全局;**单时钟域**(先跑通再谈分频);`*_req`/`*_rsp` 是一问一答的 valid/ready 握手;
下面每一行的宽度都已定死。

### 3.1 CP(命令处理器)

| 方向 | 端口 | 宽度/组成 | 说明 |
|---|---|---|---|
| in/out | `mmio_req/rsp` | AXI4-Lite | host 写门铃/队列指针、读状态与错误。**CMDS §7 的寄存器只有一份实现,位于 CP 内部**(契约 5) |
| in/out | `dma_req/rsp` | AXI4 | **全局内存**一侧:命令环读取、镜像加载、`READ_REG` 落地写、`PARAMS`/`SIGNAL`/信号量 |
| out | `sm_launch[1:0]` | `{pc[32], num_warps[4], flags[4], valid}` | 分发 LAUNCH(放置规则见 `CMDS.md` §4.2);SM 顶层再翻译成每个 warp 的 `{launch, launch_pc, launch_warp}`(`launch_warp` = 启动时的 lane 掩码,典型全 1) |
| in | `sm_done[1:0]` / `sm_error[1:0]` / `sm_fault_pc[1:0]` | 1 / 4 / 32 | 完成与错误(ISA §2.5/§2.6);这三个**归 SM 所有**,CP 只转发到 MMIO 读口(契约 6) |
| out/in | `sram_load[1:0]` | `{load_en, load_addr[13], load_data[32], load_last}` / `{load_done, err_ovf}` | `LOAD_KERNEL` 写指令 SRAM;**端口以 `hw/rtl/mem/instr_mem.sv` 为准**:13 bit **字**地址、`load_last` 定序、下一拍 `load_done` 脉冲、越界 `err_ovf` 粘滞 |
| out/in | `engine_cmd` / `engine_rsp` | `{valid, engine[1:0], opcode[7:0], len[15:0]}` + payload 流 `{wdata[32], wvalid, wlast}` / `{ready, status[2:0], error[7:0]}` | 转交 2D/GEMM 命令;`status` = `{ERROR, DONE, BUSY}`(CMDS §7/GFX §7.2),`error` = `2D_ERROR`/`GEMM_ERROR` 的码字 |
| out | `irq` | 1 | `IRQ_STATUS & IRQ_ENABLE` |

**契约**:
1. `LOAD_KERNEL` 只在 CP `IDLE` 且队列为空时执行(镜像在取指脚下,不支持原地改)。
2. LAUNCH 之后 CP 阻塞到"涉及的 SM 全部 `SM_DONE=1` 且 `SM_ERROR==0`"才处理下一条命令(`CMDS.md` §4.3);
   `SM_DONE` 由 CP 在**分发时**清(warp 侧只读),否则第二个 kernel 会看到残留的 `SM_DONE=1` 而立刻"完成"。
3. 依赖:`wait_id` 在分发前等 `SEM[id]`;`signal_id` 在完成后 `SEM[id] += 1`;超过 `TIMEOUT_CYCLES` ⇒ `TIMEOUT`。
4. 任一 SM 出错 ⇒ `CP_ERROR = SM_FAULT`,不写信号量、不执行依赖它的命令(ISA §2.5)。
5. **MMIO 访问模型(一份状态,两个访问口)**:CP 的状态机**直接读写自己的寄存器**(每拍可见,不经总线);
   host 与其它单元经 `mmio_req/rsp` 访问**同一个**寄存器文件。**不允许影子副本** —— 一份状态两个副本
   正是"host 改了 `CMD_TAIL` 而 CP 还在读旧值"这类 bug 的来源。
6. **寄存器的拥有者**:`SM*_ERROR`/`SM*_FAULT_PC` 归 SM,`2D_*`/`GEMM_*` 归引擎,CP 只把它们转发到 MMIO 读口;
   host 的"写任意值清零"由 CP 译码后**扇出到拥有者**(不是 CP 自己存一份可写的副本)。2D 引擎的寄存器空间
   只能经 `2D_SET_REG` 命令写(GFX.md §3),不走 MMIO。
7. **驱动写序**:同源 MMIO 写按发出顺序生效;驱动应等 `CMD_TAIL` 写的 `B` 响应之后再写 `DOORBELL`。

> **两种访问路径别混**:命令环、内核镜像、常量区、信号量都在**全局内存**里 ⇒ 只能走 `dma_req/rsp`(主口),
> 要用 64B 突发;而 CP 自己的寄存器(HEAD/TAIL/BASE/SIZE/ERROR/IRQ/配置)是**内部 flop**,既不进内存也不占
> DMA 带宽。把"常用寄存器"做成内部寄存器、只留一条 MMIO 口给外部,正是本设计的选择(理由见 §3.1 契约 5)。

### 3.2 SM(着色器核,实例 ×2)

| 方向 | 端口 | 宽度/组成 | 说明 |
|---|---|---|---|
| in | `launch` | `{pc[32], num_warps[4], flags[4]}` | 来自 CP;`flags` 已与镜像头交叉核对 |
| out | `imem_req` | `{pc[32], valid}` | 每周期最多 1 次取指;`valid` 限定"这一拍要读";SM 内部接 `mem/instr_mem.sv` 的取指口(取 `pc[13:2]` 当字地址) |
| in | `imem_rsp` | `{inst[32], valid}` | 指令 SRAM(预加载,无 icache);`valid` 比 `req` 晚 1 拍 |
| in | `imem_load` | 见 §3.1 的 `sram_load` | `LOAD_KERNEL` 的预加载口(来自 CP),端口定义以 `mem/instr_mem.sv` 为准 |
| out | `dmem_req` | `{op[1], addr[32], wdata[32][8], mask[8]}` | 全局访存(8 lane 合并后的请求) |
| in | `dmem_rsp` | `{rdata[32][8], err}` | 合并读回;`err` 含对齐/越界 |
| out | `done` / `error[4]` / `fault_pc[32]` | 1 / 4 / 32 | `warp_alive == 0` 与错误码(CMDS §8.2) |

**契约**:
1. **发射**:每周期从"就绪"的 warp 里挑 1 个发射(FGMT);每 SM 每周期 1 条 warp 指令 = 最多 8 条 lane 指令。
2. **访存**:地址必须 4B 对齐(否则 `BAD_ALIGN`);被谓词屏蔽的 lane **不产生请求**;
   同地址合并为广播、连续地址合并为 burst(ISA §3.2 的四种情形)。
3. **共享内存**:每 SM 16KB、8 bank × 32b;`bar.sync` 之前写入的数据对放行后的所有 lane 可见
   (由写合并缓冲的冲刷保证,§3.4)。
4. **分支与栈**:`ipdom` 写 recov 寄存器,`br.simt` 用后清 valid;发散压 8 深栈,`PC == recov_pc` 时切路径(ISA §2.3.3)。
5. **错误即停机**:ISA §2.6 的每条错误都置错误码 + `FAULT_PC` 并停止取指,不"尽力而为"。

### 3.3 矩阵引擎

| 方向 | 端口 | 宽度/组成 | 说明 |
|---|---|---|---|
| in | `engine_cmd` | 见 §3.1(命令头 + payload 流) | 16 字内联描述符:A/B/C 基址、M/N/K、stride、dtype、零点、重量化(`CMDS.md` §6.11) |
| out | `dma_req/rsp` | AXI4 | 读 A/B、写 C(与 CP 的 DMA 一起进 §3.4 的仲裁) |
| out | `status` / `error` | `{ERROR, DONE, BUSY}` / 码字 | 经 §3.1 的 `engine_rsp` 回 CP,再由 CP 暴露到 MMIO `0x60`/`0x64` |

**契约**:v1 只实现 `dtype=0`(INT8 A/B → INT32 C),支持 `C_ACCUM`、`B_TRANSPOSED` 与零点;
`REQUANT` 属 Phase 5,未实现时**报 `CMD_ERROR` 而不是静默忽略**。与 SM 之间**没有缓存一致性**,
跨引擎可见性靠"完成 + `FENCE`"。命令只经 `engine_cmd` 一条路进来(不再有并行的第二条描述符通路),
"忙/完成/出错"只经 `engine_rsp` 一条路出去 —— 与 `2D_STATUS`/`2D_ERROR` 同约定。

### 3.4 内存子系统

| 方向 | 端口 | 说明 |
|---|---|---|
| in | `master_req/rsp`(多主) | CP 的 DMA + 2 个 SM 的访存 + 各引擎,仲裁后走 AXI4 主口 |
| — | 写合并缓冲 | 每 SM 一个(约 4–8 项):`st.g` 先入缓冲、按地址合并;`exit` 与 `bar.sync` 负责冲刷 |
| — | 共享内存 / 指令 SRAM | 每 SM 各 16KB,物理上 SM 私有(文件在 `hw/rtl/mem/`,由 SM 顶层实例化) |

**契约**:
1. **MMIO 从口不在这里**:4KB 的 MMIO 窗口由 **CP** 拥有(`cp` 的 AXI4-Lite 从口 + 寄存器文件,§3.1);
   内存子系统只负责 DDR/帧缓冲一侧的主口与写合并。
2. MMIO 访问不经过写缓冲(必须立即生效);DDR/帧缓冲访问可合并;总线错误 ⇒ `BUS_ERROR`。
3. 区域解码(MMIO 窗口 / 常量区 / 镜像区 / 信号量区 / 帧缓冲 / DDR)与"内核访问 MMIO = `MMIO_ACCESS` 错误"
   的判定口径见 `CMDS.md` §7.2/§8.2。

### 3.5 2D 引擎与 host 接口

2D 引擎的**参数、寄存器映射、顶点与帧缓冲格式全部由 `GFX.md` 定义**。
本文档只固定接口形状与边界:

- 命令 opcode `0x10–0x17`(`2D_FILL`/`2D_BLIT`/`2D_DRAW`/`0x15–0x17` 保留),
  各自的 `length_words`(6/9/8/4/2)与参数编码见 `GFX.md` §4;CP 只按 `length_words` 跳过并转交(C.P 不解释参数);
- 引擎寄存器**只能经 `2D_SET_REG`(0x13)写**(`GFX.md` §3),含帧缓冲几何、裁剪框、混合模式、颜色键;
- 读顶点输出缓冲前必须用 `wait_id` 等对应 LAUNCH 完成(跨引擎无缓存一致性,`CMDS.md` §10.3);
- **完成/错误**:完成且无错 ⇒ `SEM[id] += 1` + `IRQ_2D_DONE`;引擎错误 ⇒ `2D_ERROR`(权威)+ `CP_ERROR = ENGINE_FAULT`;
- **成熟度**:引擎本体**已实现**(`2D-engine/gfx2d.cpp`),并有独立 Python 参考模型逐像素对拍(`tests/gfx2d_ref.py`,`make test` 第 ⑪ 节);v1 不含纹理与 `0x15–0x17` 三条保留命令(§7-1)。

host 接口 = MMIO 寄存器表(`CMDS.md` §7)+ 命令环(§2)+ 信号量区(`CMDS.md` §10),三条路径的编码互不重叠。

---

## 4. 全局约定

| 项 | 约定 | 出处 |
|---|---|---|
| 时钟/复位 | 单时钟域;复位后 CP `IDLE`、`SM_KERNEL_MASK=0`、`SM_DONE=1`(无活跃 warp) | 本文档 + `CMDS.md` §7 |
| 端序 | 小端(指令与数据同) | `ISA.md` §4 |
| 数据宽度 | 32b;访存只支持字对齐(byte/half 见 §7-5) | `ISA.md` §3.2、`ISA.md` §10 |
| 错误语义 | 能用一个比较器换来的"报错即定位"一律换;**不静默继续** | `ISA.md` D14 |
| 性能模型 | 每 SM 每周期 1 条(FGMT);发散块两条路径串行 | `ISA.md` §8.1 |
| 完成语义 | 只有 `exit` 之后的 `SM_DONE` 才算完成(不能用"发射完毕"代替) | `ISA.md` §2.5 |

**实测基线**(功能模拟器,4 warp 常驻 SM0;前提是发射模型,不是综合结果):
`clear` 307200 像素 = 每 SM 每周期 1.00 条、barrier 空转 0 拍;`matmul` 32³ = 12716 周期、
**3.10 发射槽/MAC**、2.6 lane-FMA/周期;`gouraud` 每次 `ld.g` 平均跨 3.73 个 32B 行。
RTL 综合后要回来标定 `ISA.md` §8.1 的目标值。

---

## 5. 与其它文档的关系(权威归属)

| 内容 | 权威文档 | 其它文档只能引用 |
|---|---|---|
| 指令编码/语义、执行模型、分歧、SM 侧错误码 | **`ISA.md`** | CMDS 只引用 |
| 命令编码、MMIO 寄存器、信号量、CP 错误码、镜像头 | **`CMDS.md`** | ISA 只引用 |
| 系统组成、模块接口、地址归属、文档边界、待定项总表 | **`SPEC.md`**(本文档) | 计划 |
| 2D 引擎(光栅/混合/纹理)与顶点/帧缓冲格式的最终定义 | **`GFX.md`** | ISA §7.2 的 `6×f32` 是它的一个用户,不是权威 |

> **同一件事只允许在一份文档里"定义"**:两处定义不一致时以本表为准,并把冲突登记到 §7。

---

## 6. 验证与工具链

两条回归入口,全部可一条命令跑完:

| 入口 | 覆盖内容 |
|---|---|
| `cd sim/simulator && make test` | 文档审计(`tests/doc_consistency_check.py`:章节引用、目录锚点、变更记录编号、跨文档编码一致)+ ISA 表↔汇编器↔译码器往返(`isa_ref_check.py`)+ SIMT 栈状态机的独立实现(`stack_model_check.py`)+ 功能回归(`tests/run_tests.py`:错误路径、运行模式、2D 引擎逐像素对拍、car demo、`sg` 编译器) |
| `cd tb && python3 run.py --all` | cocotb + Verilator 的 RTL 用例(12 个 DUT 配置);`--werror` 打开严格 lint |

- **参考模型是独立的第二实现**:shader 核(`sim/simulator/shader/`)、2D 引擎(`2D-engine/gfx2d.cpp` + `tests/gfx2d_ref.py`)、CP(`src/cp.cpp`)。RTL 与模型逐位对齐,不允许"自证"。
- **能机器检查的结论不写在文档里**:凡是能被 `make test` 的一条断言守住的东西,都写成断言(ISA 编码表、2D 命令长度、MMIO 偏移、错误码可达性)。
- **文档与实现同时改**:改编码/语义必须在同一次提交里改对应文档并追加变更记录(`ISA.md` 附录 B、`CMDS.md` 附录 B、`GFX.md` 附录 B、本文档附录 A)。

## 7. 待定项登记(跨文档汇总)

| # | 事项 | 现状 / 默认值 | 依赖 |
|---|---|---|---|
| 1 | 2D 引擎的纹理路径(`TEX_*` 寄存器、带 uv 的顶点)与 `0x15–0x17` 三条保留命令 | v1 实现五条命令 + RGBA8888/RGB565,不含纹理;保留 opcode 报 `BAD_PARAM` | `GFX.md` v2 |
| 2 | **帧缓冲尺寸/格式** | 已关闭:`FB_WIDTH/HEIGHT/STRIDE/FORMAT` 是 2D 引擎寄存器,经 `2D_SET_REG` 写(`GFX.md` §3);`FB_BASE` 在 `CMDS.md` §7 | — |
| 3 | GEMM 的 `REQUANT`/FP16、`word13..15` 用途、引擎计数器 | 未实现时报 `CMD_ERROR` | 矩阵引擎实现 |
| 4 | 多 kernel 并发:参数块基址 + 指令 SRAM 槽位 | 一次一个 kernel;常量区只能被一个内核占用 | ISA 扩展 |
| 5 | byte/half 访存与打包(纹理/量化的前提) | 只支持 32b 字对齐 | `ISA.md` §10 |
| 6 | ballot/vote(active mask → 寄存器) | 用逐 lane 迭代与谓词绕开 | `ISA.md` §10 |
| 7 | 中断的消费方式(轮询 vs 物理中断线) | `IRQ_STATUS`/`IRQ_ENABLE` 轮询 | 平台 |
| 8 | 命令环尾部的填充粒度 | 已关闭:`CMD_QUEUE_SIZE` 必须 **≥ 8 且为 8 的倍数**(尾部剩 4B 时没有合法 `NOP` 可填,`CMDS.md` §2.1) | — |
| 9 | 随机/故障注入回归的覆盖深度 | ISA §2.6 每类错误各有一例 | 工具投入 |

---

## 附录 A:变更记录

| # | 版本 | 变更摘要 |
|---|---|---|
| 1 | v0.1 → v0.2 | 帧缓冲尺寸/格式登记为待定项并写明当时的默认(RGBA8888、尺寸走常量区参数) |
| 2 | v0.3 | 与 `GFX.md` 闭环:§1 模块表补 2D 引擎的规格出处、§2 的帧缓冲几何改为"2D 引擎寄存器"、§3.5 按接口重写、§5 权威表定稿、§7-1/§7-2 改写与关闭;修正三处指向不存在章节的引用 |
| 3 | v0.4 | 2D 引擎与 `sg` 编译器落地回填(§1、§3.5、§6);§3.2 的 SM 取指口补 `valid` 与 1 拍回读延迟;§3.1 按 RTL 重写预加载口与 `engine_cmd` 流,并补 MMIO 访问模型契约 5–7;§7 第 8 项关闭(命令环填充粒度定在 `CMDS.md` §2.1) |
| 4 | v0.5(本次) | 手册化:§6 改为"验证与工具链"(原开发流程叙述移至 `docs/HISTORY.md` §5),§7 待定项改为现状表,附录压缩为版本摘要,头部加读者指引 |

> 每条改动的理由与跨文档冲突的处置过程见 `docs/HISTORY.md`;逐条提交记录见 `git log`。
