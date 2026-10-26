# SimpleGPU

一个自顶向下、文档驱动的教学级 GPU:从指令集、命令处理器、着色器核,一路到 2D 引擎、矩阵引擎与共享 AXI4 内存子系统;带两套可运行的参考模型(RTL 仿真 + C++ 全系统模拟器)、一套自定义着色语言的编译器,以及覆盖"正例 + 错误路径"的回归测试。

**从这里开始读**:想写内核 → [ISA 手册](docs/ISA.md);想写驱动 → [命令集手册](docs/CMDS.md);想画 2D 图形 → [2D 引擎手册](docs/GFX.md);想改 RTL → [系统接口规格](docs/SPEC.md) 与各模块的 `*.md` 设计说明。

## 1. 系统一览

```
                    host(SDL / demo)
                          │  AXI4-Lite(MMIO)
                    ┌─────▼──────┐
   命令环 / 信号量  │     CP      │──engine_cmd──► 2D 引擎(填充/位块/三角形/混合)
   常量区 / 镜像    │ 命令处理器  │──engine_cmd──► 矩阵引擎(16×16 INT8 脉动阵列)
                    └──┬──────┬──┘
                sm_launch│      │dma
                    ┌───▼─┐  ┌─▼───┐        ┌──────────────┐
                    │ SM0 │  │ SM1 │◄──────►│  内存子系统   │
                    └─────┘  └─────┘  AXI4  │ 共享/指令 SRAM│
                 4 warp × 8 lane / SM       │ + AXI4 主口    │
                                             └──────────────┘
```

| 单元 | 规模 |
|---|---|
| 着色器核(SM) | 2 个;每核 4 warp × 8 lane = 32 线程;32 位整数寄存器 32 个 + FP32 寄存器 32 个 + 谓词 p0–p3 |
| 指令集 | RV32IMF 子集 + SIMT 扩展(`br.simt` / `ipdom` / `bar.sync` / `exit` / `tmov` / `wmov`);无字节访存、无 atomics(见 [ISA §10](docs/ISA.md#10-已知限制与扩展路线)) |
| SIMT 栈 | 每 warp 8 级,帧含 fall_pc / mask_pending |
| 存储 | 每 SM:指令 SRAM 16KB(4096 条,由 CP 预加载)+ 共享 SRAM 16KB(8 bank) |
| 命令处理器 | 环形命令队列、256 个信号量、内建 MMIO 从口;21 态 FSM,阻塞式顺序执行 |
| 2D 引擎 | 5 条命令(填充 / 位块传送 / 三角形光栅化 / 设寄存器 / 冲刷);填充 1 像素/拍 |
| 矩阵引擎 | 16×16 INT8 GEMM;命令 `0x18`,完成与错误状态在 CP MMIO |

## 2. 快速开始

依赖:Python 3、g++(C++17)、make;RTL 仿真另需 Verilator(由 `tb/` 自带的固定版本提供,见第 3 节)。SDL2 可选,只影响帧缓冲窗口显示。

```bash
# 1) C++ 全系统模拟器:汇编内核并运行
cd sim/simulator
make                      # 编译模拟器与 demo
make run KERNEL=clear     # 汇编 sw/kernels/clear.S 并执行,校验结果
make check                # 跑全部内核并校验
make car                  # 小汽车 demo(2D 引擎路径)
make car DEMO_ARGS=--shader   # 同一幅画,逐像素 shader 路径(对照用)
make debug KERNEL=matmul  # 单步调试

# 2) 回归测试
make test                 # 文档一致性 + ISA 参考检查 + 栈模型 + 正/负用例

# 3) 自定义着色语言
make sg                   # sw/sg/*.sg → build/sg/*.bin

# 4) RTL 仿真(cocotb + Verilator)
cd tb && make setup       # 一次性:安装固定版本 Verilator 到 tb/.tools
make test                 # 跑全部 DUT;make list 看注册表
make dut DUT=cp           # 单个 DUT;可加 TEST=<正则> 过滤用例
```

## 3. 目录结构

| 路径 | 内容 |
|---|---|
| `docs/` | 四份权威手册:ISA / CMDS / GFX / SPEC;`HISTORY.md` 是设计决策与历史存档 |
| `hw/rtl/core/` | `fetch.sv`、`simt_stack.sv` 及 SM 侧 RTL;`MEM_LSU.md` 是存储子系统的开发说明 |
| `hw/rtl/cp/` | CP:命令译码、寄存器、AXI4-Lite 从口、CRC、21 态 FSM;`cp.md` 是设计说明 |
| `hw/rtl/mem/` | 指令 SRAM、AXI 仲裁等内存子系统组件 |
| `hw/rtl/fpu/` | FPU(译码、FMA、除法、格式转换、仲裁) |
| `sim/simulator/` | C++ 全系统模拟器:参考模型(`src/`、`shader/`、`2D-engine/`)、内核校验、demo、调试器 |
| `sw/` | 汇编器 `assembly.py`、sg 语言编译器 `compiler.py`、内核 `kernels/*.S`、demo `demo/`、语言手册 `grammer.md` |
| `tb/` | cocotb 测试平台:12 个 DUT 配置、74 个用例;`README.md` 是测试指南 |

## 4. 文档地图

| 我想… | 读 |
|---|---|
| 写 shader 内核 | [`docs/ISA.md`](docs/ISA.md)(§2 执行模型、§4 指令格式、§5 指令参考、§7 内核示例、附录 A 汇编语法) |
| 写 host 驱动 / 管理 CP | [`docs/CMDS.md`](docs/CMDS.md)(§2 队列、§3 命令表、§6 命令格式、§7 MMIO、§8 错误码、§10 同步) |
| 用 2D 引擎画图 | [`docs/GFX.md`](docs/GFX.md)(§2 格式、§3 寄存器、§4 命令编码、§5 光栅化、§6 混合) |
| 用 sg 语言写内核 | [`sw/grammer.md`](sw/grammer.md) |
| 实现/修改 RTL | [`docs/SPEC.md`](docs/SPEC.md)(§2 地址归属、§3 信号级接口)、[`hw/rtl/cp/cp.md`](hw/rtl/cp/cp.md)、[`hw/rtl/core/MEM_LSU.md`](hw/rtl/core/MEM_LSU.md)、[`tb/README.md`](tb/README.md) |
| 查"为什么这么设计" | [`docs/HISTORY.md`](docs/HISTORY.md)(ADR、被否决的方案、跨文档冲突) |

权威归属:地址空间与接口契约归 SPEC;命令编码与 MMIO 归 CMDS;指令语义归 ISA;2D 参数/寄存器/光栅化规则归 GFX。同一事实只在一处定义,其余引用(见 [SPEC §5](docs/SPEC.md#5-与其它文档的关系权威归属))。

## 5. 当前状态

**已实现并验证**

- ISA:四个内核全部跑通(clear / gouraud / matmul / abs);RTL 侧 `fetch` / `simt_stack` / `instr_mem` / FPU(5 个 DUT)都有 cocotb 覆盖;C++ 参考模型 `make test` 全绿(95 项)。
- CP:全部命令、MMIO 寄存器、错误码与复位语义实现;15 个 cocotb 用例覆盖正例与错误路径,`--werror` 零 lint 警告。
- 2D 引擎:填充 / 位块传送 / 三角形光栅化 / 混合,与 Python 参考模型逐像素对拍;`car` demo 两种画法逐位可比。

**已知限制**(详细后果见 [ISA §10](docs/ISA.md#10-已知限制与扩展路线))

- 无字节/半字访存、无本地内存/栈、无 FP 除法开方、无 FP16/FP64、无 atomics/ballot、无纹理采样、无指令缓存。
- 每 kernel ≤ 8 warp(用 barrier 时 ≤ 4 且必须同 SM);SIMT 栈深 8;指令 SRAM 4096 条。
- CP 为阻塞式顺序执行:同一时刻只有一条命令在飞,多个引擎不能并发;长命令流(数百条)无回归覆盖。
- 矩阵引擎与 GEMM 命令已完成编码与完成语义,引擎 PE 阵列规模与 tile 深度仍在选型。

## 6. 约定

- 文档与实现必须同时改;改动摘要追加到各文档的"变更记录"表,编号唯一递增(`docs/HISTORY.md` 只存档过程,不是权威)。
- 设计文档放在对应 RTL 旁边(`hw/rtl/cp/cp.md`、`hw/rtl/core/MEM_LSU.md`、`hw/rtl/fpu/README.md`),`docs/` 下只放四份权威手册与历史存档。
- 每条规则引到具体章节(`§N.M`),交叉引用只指向存在的编号;`sim/simulator/tests/doc_consistency_check.py` 会机械校验这些约束,`make test` 的通过与否以它为准。
- 仓库面向教学:规模化数字(周期数、吞吐)都是设计目标或实测估算,不是签核数据。
