# SimpleGPU 实现计划

> 目标: 用 SystemVerilog 从零设计一个教学型 GPU —— 可编程 SIMT shader 核 + 2D 图形固定功能引擎 + 矩阵加速器。

## 1. 项目定位与假设

**做什么:**
- 可编程 shader 核 (SIMT, 多 warp 交错, 处理顶点变换/像素着色/通用并行计算)
- 固定功能 2D 引擎: 三角形光栅化、纹理采样、alpha 混合、fill/blit/line
- 矩阵加速器: 16×16 脉动阵列 GEMM (INT8 起步)
- AXI4 接口、主机驱动、演示程序
- 仿真全程可跑, 最终目标是在 FPGA 上出图

**不做什么 (明确排除, 防止规模失控):**
- 完整 3D 管线 (裁剪、深度缓冲之外的 3D 特性、透视修正)
- 纹理压缩、mipmap、各向异性过滤
- GPU 与 CPU 缓存一致性、虚拟内存/页表 (用物理地址)
- 乱序执行、抢占调度、光线追踪

**核心假设 (可改, 默认按此规划):**
1. **复用你已有的 RV32IMFC 核** 作为 shader lane 的数据通路 (ALU/FPU/寄存器堆/LSU 直接复用) —— 这是你经验的最大杠杆
2. 仿真优先: Verilator + cocotb; 上板作为最终里程碑而非起点
3. 内存接口统一 AXI4 (master), 主机通过 AXI4-Lite (slave) 控制
4. 帧缓冲: 仿真阶段用内部 SRAM; 上板用 DDR (LiteX + LiteDRAM)
5. 2D 数学用定点数 (s16.15), 不上浮点
6. warp 宽度 = 8 lanes (从 32 起步会大幅增加调试难度)

## 2. Phase 0: 前置学习 (2周)

GPU 与 CPU 最本质的五个差异 (按学习优先级):
1. **SIMT 执行模型**: warp/wavefront、线程分派、分歧 (divergence) 与收敛 (reconvergence)、active mask
2. **吞吐优先**: 用大量线程 + 多 warp 交错隐藏延迟, 而不是靠缓存/预测
3. **内存合并 (coalescing)**: 相邻 lane 访问相邻地址才高效
4. **图形管线数学**: 光栅化 = 采样问题; 边方程/重心坐标; Early-Z; 纹理过滤; alpha 混合语义
5. **脉动阵列**: 数据流驱动的 GEMM

必读材料 (详见 §15 参考资料):
- 《General-Purpose Graphics Processor Architectures》前 3 章 —— 专为有 CPU 背景的人写的过渡书, 强烈推荐
- "A Trip through the Graphics Pipeline 2011" (fgiesen 博客) —— 光栅化/插值数学
- 快速浏览 TinyGPU 和 Vortex 的代码结构 (不必读懂每行, 目标是建立"一个 GPU 由哪些模块组成"的架构地图)
- PTX ISA 手册的指令分类部分 —— 看真实 GPU 的 ISA 长什么样
- Google TPU 论文 (ISCA'17) —— 脉动阵列如何工作
- AMBA AXI4 规范 (若没用过)

**产出:** 一份自己的架构地图笔记 + 2~3 个开源项目的源码阅读笔记。

## 3. 总体架构

```
                         ┌─────────────────────────────────────────┐
                         │              SimpleGPU SoC              │
  Host CPU               │                                         │
  (AXI4 master)          │  ┌──────────────┐                        │
  ────AXI4-Lite─────────►│  │ 命令处理器 CP │  取命令→解码→分发       │
  (MMIO 控制)            │  │  (front-end) │  →跟踪完成(信号量)      │
                         │  └──┬─────┬─────┴──┐                     │
                         │     │     │        │                     │
                         │  ┌──▼──┐┌─▼─────┐┌─▼───────┐            │
                         │  │Shader││2D 引擎││矩阵引擎 │            │
                         │  │ 核×2 ││(固定) ││(脉动)   │            │
                         │  └──┬──┘└─┬─────┘└─┬───────┘            │
                         │     └────┴────┬────┘                     │
                         │        ┌──────▼──────┐                   │
                         │        │ 内存子系统   │ AXI4 主口(仲裁)   │
                         │        │ + scratchpad│ + 写合并缓冲      │
                         │        └──────┬──────┘                   │
                         └───────────────┼──────────────────────────┘
                                         ▼ AXI4
                                  DDR / 帧缓冲 / 内核代码
```

**各模块职责:**

| 模块 | 职责 | 关键简化 |
|---|---|---|
| 命令处理器 (CP) | 从命令环取命令 (DRAW/BLIT/FILL/GEMM/LAUNCH/SYNC), 分发到引擎, 跟踪完成 | 固定长度命令、in-order 分发、简单信号量 |
| Shader 核 | SIMT 执行可编程内核: 顶点变换、像素着色、通用并行计算 | 每 lane 复用 RV32IMFC 数据通路; in-order 分派; 多 warp 交错隐藏延迟; scratchpad 无缓存 |
| 2D 引擎 | 三角形 setup → 光栅化 → Early-Z → 纹理采样 → ROP (混合/深度) | 定点数; 2×2 quad; 最近邻+双线性; 另含 FILL/BLIT/LINE |
| 矩阵引擎 | 16×16 脉动阵列 INT8 GEMM (A×B+C) | weight-stationary; 双缓冲 SRAM; 自动分块 |
| 内存子系统 | 各引擎共享一个 AXI4 主口 (轮转仲裁), 写合并缓冲, 共享 scratchpad | 无缓存一致性; 物理地址 |

**内存映射 (示例):**

| 地址范围 | 用途 |
|---|---|
| 0x0000_0000 | MMIO (CP 控制/状态、各引擎状态) |
| 0x0100_0000 | 命令环缓冲 |
| 0x0200_0000 | 内核代码 + 常量内存 |
| 0x1000_0000 | 帧缓冲 (基址/尺寸/格式可配置) |
| 0x8000_0000 | 外部 DDR (上板时) |

## 4. Phase 1: 规格设计 (2周)

产出文档 (放 `docs/`):
1. `SPEC.md` — 整体架构、模块接口、时钟/复位策略
2. `ISA.md` — shader 核指令集 (RISC-V 风格 32-bit 编码 + GPU 扩展)
3. `CMDS.md` — 命令集编码、内存映射、主机驱动接口 (寄存器定义)
4. `GFX.md` — 光栅化/混合/纹理的**精确语义** (定点数格式、像素归属填充规则、混合公式)

> ⚠️ 语义必须精确到"像素中心在边界的哪一侧算三角形内", 否则 RTL 和参考模型永远对不齐。

**此阶段拍板的关键决策 (后面不回头):**
- 帧缓冲格式: 建议内部 RGBA8888 处理, 输出 RGB565 (省一半带宽); 仿真阶段直接 RGBA8888 简化
- 顶点格式: 固定 {x, y (s16.15), r, g, b, a, u, v}
- 命令: 固定 64B 一条 (简单, 够用)
- 内核启动模型: grid = warp 数, 类似 CUDA 的 `<<<num_warps, 8>>>`

**ISA 扩展草图** (在 RV32I 基础上, 示意):
```
# SIMT 控制
setp.eq  p1, r1, r2      # per-lane 谓词
@!p1 add r3, r1, r2      # 谓词化执行 (lane 级屏蔽)
br.simt  label           # 分歧分支: 硬件压栈 mask, 两路先后执行
bar.sync                 # barrier
ballot r1                # active mask → 寄存器
tmov r1, tid             # lane id (0..7)
wmov r1, wid             # warp id
# 内存 (warp 级, 硬件自动合并: lane 地址连续 → 一次 burst)
ld.g r1, [r2]
st.g [r2], r1
# 算术 (直接复用 RV32IMFC 的 ALU/FPU 设计)
fma.f32 r1, r2, r3, r4
mul.i32 / mac.i32        # 矩阵/卷积内核用
# 特殊
tex.sample r1, u, v, tid # 发往纹理单元 (协处理器式)
```

> 💡 建议: 先手写 3 个目标内核的汇编 (clear / gouraud / matmul), 再反推指令集 —— 需求驱动, 避免设计一堆用不上的指令。

## 5. Phase 2: 基础设施 (2周)

- 建立目录结构与 git (第一天就 git)
- Verilator + cocotb 仿真环境, Makefile 一键跑全部测试
- AXI4-Lite slave (MMIO) + AXI4 master 基础封装 + cocotb AXI BFM/协议检查器
- **第一个闭环**: host 写 MMIO → CP 读命令环 → 执行 CLEAR 命令 → 读回帧缓冲比对。CP 内部是空壳也要先跑通整条数据链
- Lint 接入 (`verilator --lint-only`, 0 warning 目标)

```
gpu/
├── rtl/            # core/ gfx/ gemm/ mem/ axi/ soc/
├── tb/             # cocotb testbench + AXI BFM/VIP
├── sw/             # assembler.py, kernels/*.s, host driver, demos/
├── sim/            # Makefile, verilator wrapper
├── fpga/           # LiteX 集成, 约束文件
└── docs/           # SPEC.md ISA.md CMDS.md GFX.md
```

**交付:** M0 里程碑 —— 仿真下 MMIO 读写 + 清屏命令全链路跑通。

## 6. Phase 3: Shader 核 (4周) —— 最难, 时间留足

三步走:

1. **3a 单 warp 8 lanes 无分歧 (1.5周)**: 复用 RV32IMFC 的 ALU/FPU/寄存器堆作为 lane 数据通路; 8 lane 共享一个 PC; 实现基础 SIMT 指令、谓词、tmov/wmov、warp 级 ld/st (合并单元: lane 地址连续 → 一次 burst)。验证: 向量加法内核。
2. **3b 多 warp 交错 + 调度 (1.5周)**: 4 warps 轮流分派 (FGMT 风格调度器); 加 bar.sync。验证: 4 个 warp 的向量加法。此时开始看到"用并行度隐藏访存延迟"的效果 —— 这是 GPU 的核心思想。
3. **3c 分歧栈 (1周)**: SIMT stack {reconvergence PC, active mask}; br.simt 压栈, 收敛时弹栈。验证: if/else 内核。

> 学习要点: 3a/3b 你的 CPU 经验直接适用; **3c 是纯 GPU 新概念**, 动手前先读 TinyGPU/Vortex 的分歧处理代码。

**交付:** assembler.py + 3 个通过的内核测试 (向量加 / 多 warp / if-else)。

## 7. Phase 4: 2D 图形引擎 (4周)

每步都有可视输出:

1. **4a FILL/BLIT (1周)**: 矩形填充、拷贝 (含 write-combine 写合并)。demo: 棋盘格。
2. **4b 三角形光栅化 (1.5周)**: 顶点 → setup (边方程系数、插值系数、bounding box) → 逐 quad 评估边方程 → 重心插值 → 纯色输出。demo: 一个彩色三角形。
3. **4c 属性插值 (0.5周)**: 顶点颜色插值 (Gouraud)。demo: 渐变三角形 —— 这一步验证 shader 核 (算顶点) + 光栅器 (插值) 的完整协作。
4. **4d 纹理 + 混合 (1周)**: 纹理单元 (最近邻/双线性、wrap/clamp)、alpha 混合 (src-over)、Early-Z。demo: 贴图三角形 + 半透明叠加。

> 数学提醒: 全程定点数; 2D 仿射无需透视修正; 填充规则必须先写进 GFX.md 并在 Python 参考模型中实现同样规则, 再逐像素比对。

## 8. Phase 5: 矩阵引擎 (3周)

- 16×16 PE 脉动阵列, INT8×INT8 → INT32 累加, weight-stationary
- A/B tile 双缓冲 SRAM + 数据流控制状态机; C 累加器双缓冲
- GEMM 命令自动大矩阵分块 (tiling 状态机)
- 验证: 与 numpy 结果全对; 性能记录: 16×16 @ 200MHz ≈ **102 GOPS** (INT8) 理论峰值, 记录实际利用率
- 留升级接口: 输出量化、FP16、更大阵列

## 9. Phase 6: 系统集成 (3周)

- CP 完整实现: 多引擎并发 + 信号量依赖 (例: shader 算完顶点 → 2D 引擎才开始画)
- 主机驱动 (Python, 仿真时走 AXI-Lite BFM; 上板后走 LiteX CSR)
- 综合 demo: 渲染一帧 (渐变背景 + 贴图三角形) 的同时跑 GEMM
- 性能计数器 (cycle 计数、各引擎利用率) —— 为 Phase 7 优化提供数据

## 10. Phase 7: FPGA 上板与优化 (4周+)

- LiteX 集成: SimpleGPU 挂到 LiteX SoC 的 AXI 总线, DDR 用 LiteDRAM
- 板卡建议:
  - 有 DDR (推荐): PYNQ-Z2 / Arty Z7-20
  - 无 DDR 小预算: Arty A7-35T / Nexys A7 (帧缓冲用 BRAM, 256×192 RGB565)
  - 预算充足: VCU118
- 上板顺序: MMIO 读写 → CLEAR → 静态图 → 动画
- 输出显示: VGA/HDMI (LiteX 自带 video 外设), 或读回帧缓冲存图
- 时序优化: 脉动阵列打拍、帧缓冲读写流水化

## 11. 验证策略 (贯穿全程)

分层测试金字塔:
1. **单元级 (cocotb)**: 每模块独立激励 + 边界用例
2. **模块级 (golden model)**: Python 参考实现 (numpy/Pillow), RTL 输出逐像素/逐元素对比
3. **系统级 (Verilator 全 SoC)**: host 驱动走完整流程: 写命令 → 引擎执行 → 读回验证
4. **回归**: 每次提交 `make test` 全绿; 任何新功能先写 test 再写 RTL

图形正确性三件套: Python 参考模型 + 逐像素 diff + 输出 PNG 人眼检查 (很多 bug 一眼可见)。

## 12. 里程碑 (每个都以可演示产出为终点)

| # | 里程碑 | 演示内容 | 对应阶段 |
|---|---|---|---|
| M0 | 全链路空壳跑通 | MMIO 读写 + CLEAR 命令清屏 | Phase 2 |
| M1 | 第一个三角形 | 纯色三角形 (无 shader 参与) | Phase 4b |
| M2 | GPU 介入 | 渐变三角形 (shader 核算顶点 + 光栅器插值) | Phase 3+4c |
| M3 | 纹理与混合 | 贴图三角形 + 半透明叠加 | Phase 4d |
| M4 | GEMM 正确 | 与 numpy 全对 + 吞吐数据 | Phase 5 |
| M5 | 多引擎并发 | 渲染 + GEMM 同时执行 | Phase 6 |
| M6 | 上板 | FPGA 屏幕出图 | Phase 7 |

## 13. 风险与对策

| 风险 | 对策 |
|---|---|
| 规模失控 (按真实 GPU 设计) | 严守 §1 排除清单; 每阶段只为一个 demo 服务 |
| SIMT 分歧栈难调 | 先读 TinyGPU/Vortex 实现; 3a 先跑无分歧内核 |
| AXI 突发/对齐 bug | 第一天就写 AXI 协议检查器, 所有主口先过 VIP 再联调 |
| 帧缓冲放不进 BRAM | 小分辨率 16bpp; 或直接上 DDR |
| 参考模型与 RTL 语义不一致 | GFX.md 精确语义定义; 先写参考模型再写 RTL |
| 时序不收敛 (脉动阵列) | PE 内部打拍、阵列流水化、降低目标频率 |
| 单人精力有限 | 每阶段可独立验收, 随时可停而不烂尾 |

## 14. 决策点 (默认值, 可按你的情况调整)

1. 复用 RV32IMFC 核 (默认) vs 全新 warp 核 —— 复用, 你的经验直接变现
2. FPGA 上板 (默认) vs 纯仿真 —— 纯仿真也完全可行, 计划不变只是砍掉 Phase 7
3. INT8 矩阵 (默认, 简单且性能数字好看) vs FP32 起步 —— INT8 起步
4. 2D 图形 (默认主线) vs 矩阵优先 —— 2D 先行, 每步有可视反馈, 对入门最友好

## 15. 参考资料

**书:**
- T. Aamodt, W. Fung, T. Rogers, *General-Purpose Graphics Processor Architectures*, Morgan & Claypool, 2018 —— 必读, 就是写给有 CPU 背景的人的
- J. Hennessy, D. Patterson, 《计算机体系结构: 量化研究方法》(第 6 版) —— GPU 章节
- T. Akenine-Möller et al., *Real-Time Rendering* 4th ed. —— 光栅化/纹理/混合语义 (用到哪章读哪章)

**博客/文章:**
- F. Giesen, *A Trip through the Graphics Pipeline 2011* (ryg 博客) —— 光栅化数学讲得最清楚
- P. Warden, *Why GEMM is at the heart of deep learning*

**论文:**
- N. Jouppi et al., *In-Datacenter Performance Analysis of a Tensor Processing Unit*, ISCA'17 —— 脉动阵列
- Z. Jia et al., *Dissecting the NVIDIA Volta GPU Architecture via Microbenchmarking*, 2018
- H. Wong et al., *Demystifying GPU Microarchitecture through Microbenchmarking*, ISPASS'10

**开源参考 (必看代码结构):**
- **TinyGPU** (github.com/adam-maj/tiny-gpu) —— 教学用 SystemVerilog GPU, 小到可以读完, 强烈推荐作主线参考
- **Vortex** (github.com/vortexgpgpu/vortex) —— 开源 SystemVerilog GPGPU, RISC-V 扩展风格
- **MIAOW** (github.com/VerticalResearchGroup/miaow) —— Verilog, 完整但复杂, 只作浏览

**规范:**
- AMBA AXI4 规范 (ARM)
- RISC-V 指令集手册 + V 扩展 (如果想把扩展做成类 RVV)
- NVIDIA PTX ISA / AMD RDNA2 ISA 手册 —— 看真实 GPU 的 ISA 长什么样

**工具:**
- Verilator、cocotb、GTKWave、LiteX/LiteDRAM wiki
