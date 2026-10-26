# MEM_LSU.md —— 内存子系统与 LSU 开发规划

版本 0.1 · 关联文档:`SPEC.md` §2/§3.2/§3.4 · `ISA.md` §2.6/§3.2/§5.6/§8 · `CMDS.md` §7.2 · `GFX.md` §3/§4
参考模型:`sim/simulator/src/memory.cpp`、`sim/simulator/shader/exec.cpp`(访存路径与 `region_name`)、`sim/simulator/include/{memory,machine}.hpp`

> **本文档回答什么**:`hw/rtl/mem/` 与 `hw/rtl/core/lsu*.sv` 要做成什么样、怎么拆、
> 按什么顺序做、每一步拿什么验收、以及哪几条**口径**必须先拍板(§5)。
> 权威语义仍在 `ISA.md`/`SPEC.md`/`CMDS.md`,本文档只负责"实现计划与接口落地",
> 两者冲突时以那三份为准,并把冲突登记到本文档 §9。

---

## 0. 进展

| 阶段 | 内容 | 状态 |
|---|---|---|
| **A** | 清障:修 `instr_mem.sv`/`axi_arb.sv` 的 lint 与死逻辑;重注册 core DUT;加 `instr_mem`/`fetch_imem` 用例 | ✅ 已完成(core/mem 用例在 Verilator 上 `--werror` 全绿;当时全量 `tb/run.py --all` = 10 个 DUT / 58 个用例全绿,现已增至 12 个 DUT / 74 个用例) |
| B | `shared_mem.sv`(8 bank 广播/冲突串行化)+ `sh_*.S` 计费对齐 | 待做 |
| C | `lsu_coalescer.sv`(§3.2 四种合并情形) | 待做 |
| D | `lsu.sv` + `lsu_scheduler.sv` + `mem_map.sv` + `global_mem.sv` + `wcb.sv` | 待做 |
| E | `axi_arb.sv`(多主)+ `mem_perf.sv`(计数器) | 待做 |
| F | 接进 `exu.sv`/`core.sv`,三个内核端到端对拍 | 待做 |

**Phase A 实际做了什么**(便于后来者对账):

1. `mem/instr_mem.sv`:模块名与文件名对齐(原 `instr_sram`)、`ADDR_W` 参数真正用上、
   **`err_ovf` 从死逻辑变成活逻辑**(见 §2.1)、取指口 1 拍延迟写清;
2. `mem/axi_arb.sv`:去掉端口列表多余逗号(`NULLPORT`),改成带 TODO 的干净占位;
3. `tb/harness/duts.py`:重新注册 `simt_stack` / `simt_stack_ovf` / `fetch`,新增
   `instr_mem` / `fetch_imem`;
4. 新增 `tb/tests/core/test_instr_mem.py`(4 例:预加载/回读、1 拍延迟、越界、
   真镜像布局)与 `tb/tests/core/test_fetch_imem.py`(2 例:`nested7.S` 与 4 个随机
   内核跑在**真 SRAM** 上);
5. 新增仿真专用胶水 `tb/harness/rtl/fetch_imem_wrap.sv`(cocotb 需要一个 toplevel
   才能把 `fetch` + `instr_mem` 一起 elaborate);
6. 测试侧适配了作者新增的 LAUNCH lane 掩码端口(`fetch.launch_warp` /
   `simt_stack.launch_mask`,正常内核是全 1)。

---

## 1. 范围

**做什么**:SM 侧访存(加载/存储指令的地址生成、合并、错误检查、写回)、共享内存、
每 SM 的全局访存出口(事务跟踪 + 写合并缓冲)、多主到 AXI4 主口的仲裁、地址区解码、
性能计数器。

**不做什么**(与 `ISA.md` §3.2、`SPEC.md` §7-5 一致):

- **不做 cache**——`hw/rtl/cache/README.md` 已写"暂不实现",ISA §3.2 规定全局空间无缓存;
  合并质量决定性能,内核靠"相邻 lane 访相邻字"优化;
- **不做 byte/half 访问**(只支持 32b 字对齐,`wstrb` 恒为全字);
- **不做虚拟内存/页表**、不做 GPU-CPU 缓存一致性(PLAN.md §1 的排除清单);
- 不做乱序 AXI 响应(v1 限定同 ID、in-order;乱序留待 v2)。

---

## 2. 现状盘点

| 文件 | 状态 | 备注 |
|---|---|---|
| `mem/axi4_bus.sv` | ✅ 已写 | AXI4 interface + `master`/`slave` modport。**无法单独 `--lint-only`**(interface 不能当 toplevel),要 lint/仿真得靠一个 wrapper |
| `mem/axi_arb.sv` | ⬜ 占位 | Phase E 实现 |
| `mem/instr_mem.sv` | ✅ 已写 | `instr_mem` 模块:预加载口 + 取指口(1 拍延迟)。与 `core/fetch.sv` 的 imem 协议天生对齐,已被 `fetch_imem` 用例验证 |
| `mem/shared_mem.sv` | ⬜ 空 | Phase B |
| `mem/global_mem.sv` | ⬜ 空 | Phase D |
| `mem/mem_perf.sv` | ⬜ 占位 | 目前只有一行"内存性能监视接口"的注释;Phase E 实现(已从 `mem.perf.sv` 改名 —— 文件名里带点容易在模块名/工具脚本上踩坑) |
| `core/lsu.sv` `lsu_coalescer.sv` `lsu_scheduler.sv` | ⬜ 空 | Phase C/D |
| `core/exu.sv` `core.sv` | ⬜ 空 | Phase F(LSU 的宿主与 warp 调度器) |
| `core/{fetch,simt_stack,decode,wbu,alu,mdu,regfile}.sv` | ✅ 已写 | LSU 要接的东西(`wbu` 的 `gpr_wr_data_in[0:7]` 需要数据源 mux) |

### 2.1 `instr_mem` 的越界自检(Phase A 修掉的一个真实缺陷)

原来 `load_addr` 是 12 bit 字地址、`NUM_WORDS = 4096`,于是 `load_addr < NUM_WORDS`
**恒为真**,`err_ovf` 永远不会置位——一个看起来在检查、实际上从不报错的比较器。
现在 `load_addr` 多留 1 位(13 bit),用最高位判越界:

```systemverilog
if (load_addr[ADDR_W]) err_ovf <= 1'b1;                    // 越界: 不写, 只记录
else                   sram[load_addr[ADDR_W-1:0]] <= load_data;
```

`err_ovf` 粘滞到 `rst`(与 ISA §2.6"错误即定位、由 host 复位"同一风格)。

---

## 3. 验收不变量

| # | 不变量 | 出处 | 由谁守 |
|---|---|---|---|
| 1 | 四种合并情形:8 lane 连续 → 1 次 32B burst;同地址 → 1 次请求 + 广播;部分 lane 屏蔽 → 按连续段子 burst;分散 → 最多 8 次 | ISA §3.2 | `lsu_coalescer` 用例(C)+ `gmem_lines` 计数(E) |
| 2 | 共享内存:8 bank × 32b,`bank = addr[4:2]`;同地址广播 1 拍;同 bank 异地址串行化的额外拍数 = `Σ(非空 bank 的不同地址数 − 1)` | ISA §3.2/§8.1 | `shared_mem` 用例 + `sh_conflict.S`/`sh_noconflict.S`/`sh_twobank.S`(B) |
| 3 | 错误各一条可触发路径:`BAD_ALIGN`(每 lane)/`MMIO_ACCESS`/`NO_MEM_REGION`/总线错误 | ISA §2.6、CMDS §8.2 | 每条错误一个反例(D/F) |
| 4 | 可见性:`st.g` 入写合并缓冲;**`exit` 与 `bar.sync` 负责冲刷**;同地址"写后读"必须看到新值 | ISA §2.4/§2.5、SPEC §3.4 | WCB 用例 + RAW 转发用例(D) |
| 5 | 计数器可复算:RTL 的"每次访存 32B 行请求数"要能复现 ISA §8.1 的 clear 1.00 / gouraud 3.73 / matmul 1.77 | ISA §8.1 | `mem_perf` + 三个内核(E/F) |
| 6 | 全局访存只占 1 个发射槽,合并度只影响请求数、不额外折算拍数(与参考模型同一口径) | ISA §8.1 | 计数器对拍(E) |

---

## 4. 模块划分与接口草案

```
core/lsu.sv            每 warp 一个:地址生成/对齐检查/读回写口/错误上报(风格对齐 mdu.sv)
core/lsu_coalescer.sv  8 lane → N 个 32B 行请求(纯组合, 最先做)
core/lsu_scheduler.sv  每 SM 一个:共享口仲裁 + 全局请求发射 + outstanding 表 + 响应路由 + 冲刷
mem/shared_mem.sv      每 SM 16KB, 8 bank:广播/冲突串行化, 输出冲突拍数
mem/global_mem.sv      每 SM 的全局出口:行请求 → AXI 事务, 读回拆 lane, 总线错误翻译
mem/wcb.sv (新增)      写合并缓冲(4~8 项 × 32B 行), 读旁路, 冲刷握手
mem/mem_map.sv (新增)  地址区解码:MMIO_ACCESS / NO_MEM_REGION / 区域归属
mem/axi_arb.sv         多主(CP/SM0/SM1/2D/GEMM) → 1 个 AXI4 主口, 读写通道各自仲裁
mem/mem_perf.sv        计数器(与参考模型 RunStats 同名同义)
mem/mem_subsystem.sv(新增, 可选)  2×global_mem + axi_arb + mem_map + mem_perf 收成顶层
```

**实例化位置**:`shared_mem` 与 `instr_mem` 虽然文件在 `mem/`,但物理上是 **SM 私有**,
由 SM 顶层(`core/core.sv`)各实例化一份(延迟关键路径短);`global_mem` 每 SM 一份,
由 `mem_subsystem` 实例化两份。

### 4.1 `core/lsu_coalescer.sv`(纯组合)

```systemverilog
module lsu_coalescer #(parameter int LANES = `WARP_SIZE) (
    input  wire [LANES-1:0]         lane_en   ,   // effective_mask = active_mask ∩ 谓词
    input  wire [LANES-1:0][31:0]   lane_addr ,
    input  wire [LANES-1:0][31:0]   lane_wdata,
    input  wire                     is_store  ,
    output logic [3:0]              req_num   ,   // 0..8
    output logic [LANES-1:0][31:0]  req_line  ,   // 32B 对齐的行地址(addr & ~31), 升序
    output logic [LANES-1:0][7:0]   req_lanes ,   // 该行覆盖的 lane 集合
    output logic [LANES-1:0][7:0]   req_wen   ,   // 该行内要写的字(32B/4)
    output logic [LANES-1:0][255:0] req_wdata ,   // 该行的 8 个字(store 用)
    output logic [LANES-1:0][2:0]   lane_word     // lane → 行内字号(响应回填用)
);
```

算法(组合, 输出按地址升序):① 按 `lane_en` 过滤(谓词为假的 lane **不产生请求**,ISA §3.2);
② 反复"在未服务 lane 里取 `addr[31:5]` 最小者",把同行 lane 归成一组,最多 8 轮。
四种情形自然全覆盖。**决策点 D5**:`req_wdata` 直接拼好 8 个字(约 2 kb 的 mux),还是只出
`{line, wen}`、由 `global_mem` 从 `lane_wdata` 收集。

### 4.2 `mem/shared_mem.sv`

```systemverilog
module shared_mem #(parameter int SIZE_BYTES = `SHARED_MEM_SIZE, LANES = `WARP_SIZE, BANKS = 8) (
    input  wire                    clk, rst,
    input  wire                    req_valid, req_we,
    input  wire [LANES-1:0]        lane_en,
    input  wire [LANES-1:0][31:0]  lane_addr, lane_wdata,
    output logic                   req_ready,
    output logic [LANES-1:0][31:0] lane_rdata,
    output logic [3:0]             conflict_extra,   // 额外串行拍 = Σ(非空 bank 的不同地址数 − 1)
    output logic                   err_align, err_oob
);
```

每 bank 一块 2KB SRAM + "取最小未服务地址"的选择器 + 多轮 FSM;每轮把各 bank 的结果写进
对应 lane 的 `lane_rdata`。地址/对齐检查放在模块内(自洽、好测),由 LSU 转成 ISA §2.6 的错误码。

### 4.3 `core/lsu.sv`

职责:地址 = `rs1 + imm9`(若 EXU 的加法器已算好则直接收)、每 lane 对齐检查、按 `.s`/`.g`
分派、等待、把读数据 + `rd` + `eff_mask` 交给 `wbu`、错误上报。握手风格对齐 `mdu.sv`
(`en/ready/done/ack` + 元数据:`done_rd/done_gpr_we/done_pc/done_inst`)。

端口草案:
```systemverilog
mem_en, mem_we, mem_shared, mem_rd, mem_pc, mem_inst, eff_mask,
lane_addr[8], lane_wdata[8],                 // 由 EXU/寄存器堆给出
mem_ready, mem_done, mem_done_rd, mem_done_rdata[8], mem_done_we[8],
smem_req_*, gmem_req_*,                       // 到 lsu_scheduler
err_bad_align, err_mmio, err_no_region, err_smem_oob
```

### 4.4 `core/lsu_scheduler.sv`(每 SM 一个)

① 4 个 warp 争用**一个**共享内存端口 → 仲裁;② 全局请求发射;③ outstanding 表(建议 4 项,
够 4 warp 各 1 条在飞);④ 响应按 warp 回填;⑤ 接收 `bar.sync`/`exit` 的 `flush_req` 并回
`flush_done`;⑥ 给 warp 调度器 `warp_stall[3:0]`/`warp_ready[3:0]`(warp 因访存挂起时让出
发射槽,ISA §8.1 的 FGMT)。

### 4.5 `mem/global_mem.sv` 与 `mem/wcb.sv`

- 读:行请求 → AXI AR(`arlen=7`,8 拍 4B)→ 收齐一行 → 按 `lane_word` 拆回各 lane;
- 写:**先入 WCB**,按 32B 行合并,满/被冲刷时生成 AXI AW+W;
- **读旁路(D2)**:`ld.g` 的行命中 WCB 项 ⇒ 直接用 WCB 里的数据;
- 总线错误 → `BUS_ERROR`(CMDS §8.2 的错误码)。

### 4.6 SM 侧接线(Phase F)

- `fetch.imem_req_valid/imem_req_pc` → `instr_mem.fetch_en/fetch_addr`(取 `pc[13:2]`),
  `instr_mem.fetch_data` → `fetch.imem_rsp_inst`(1 拍延迟一致);`pc_ok` 已在 `fetch` 里检查,
  越界 PC 不会下到 SRAM。**Phase A 已用 `fetch_imem` 用例把这条件验过。**
- LAUNCH:作者新增了 lane 掩码(`fetch.launch_warp` / `simt_stack.launch_mask`),正常内核是
  全 1;`num_warps` 的放置规则(ISA §2.2 连续打包、`wid mod 4` 槽号)由 CP 与 SM 顶层负责。
- `exu.sv`:ALU/FPU/MDU/LSU 的结果汇到 `wbu.gpr_wr_data_in[0:7]`,load 用 LSU 的
  `mem_done_rdata`,需要一个数据源 mux。
- **SPEC §3.2 的 `dmem_req/rsp` 需要细化**(见 §9):现在写作
  `{op[1], addr[32], wdata[32][8], mask[8]}` / `{rdata[32][8], err}`,应落成"每拍一个 32B 行
  请求"的字段表,并按 SPEC §6 第 1 条回填文档。

---

## 5. 关键设计决策(推荐默认值)

| # | 决策 | 推荐 | 代价/理由 |
|---|---|---|---|
| D1 | WCB 位置与大小 | 独立 `mem/wcb.sv`,每 SM **8 项 × 32B 行**(文档写 4~8 项) | 面积可忽略;每项比较一次地址 |
| D2 | 写后读(RAW)语义 | **读旁路/命中转发** | 参考模型的 `st.g` 立即生效,不转发就得"读前全冲刷",既慢又难对齐 |
| D3 | 每 SM outstanding 数 | **4**(=4 warp 各 1 条);将来按需加到 8 | 更多需要更大的响应路由表;少了会在多 warp 下暴露 AXI 延迟 |
| D4 | 共享内存端口 | 每 SM **一个 warp 级端口**(8 地址),warp 间由 `lsu_scheduler` 仲裁 | 结构简单;barrier 型内核(§7.3 的 matmul)够用 |
| D5 | 写数据收集位置 | coalescer 只出 `{line, wen}`,由 `global_mem` 收集 | 省掉 2 kb 的 mux,代价是多一级逻辑 |
| D6 | 共享内存同拍读写 | **写优先、读返回旧值**,并写进 SPEC | 必须钉死,否则竞争场景下 RTL 与模型不一致 |
| D7 | `NO_MEM_REGION` 默认行为 | RTL **默认报错**(ISA D14);参考模型的 `strict_mem` 默认同步改为开 | 会改变参考模型行为,要一起改 `make test` 与文档(§9) |
| D8 | 访存粒度 | 只支持 32b 字对齐,`wstrb` 恒 F | byte/half 是 SPEC §7-5 待定项;帧缓冲 RGBA8888 = 4B/像素 |
| D9 | 冲突计费归属 | `shared_mem` 输出 `conflict_extra`,LSU 据此 stall | 计费口径集中一处,便于与 ISA §8.1 的 `Σ(...)` 对拍 |
| D10 | 相邻行合成长 burst | v1 每行一条 burst(8 拍);v2 再把相邻行并成 INCR burst | gouraud 的 3.73 行/访存要靠它才能真正降下来 |

---

## 6. 里程碑与顺序

**Phase B —— 共享内存**
`shared_mem.sv` + DUT + 用例:无冲突 1 拍、全同地址广播 1 拍、`sh_conflict.S` 的 7 拍、
`sh_noconflict.S`/`sh_twobank.S` 的 0 拍、`err_align`/`err_oob`,再加随机对拍(Python 参照
按 §3.2 与计费公式独立实现)。
*验收*:`tb/run.py --dut shared_mem --werror` 全绿 + 三个 `sh_*.S` 的期望拍数逐条一致。

**Phase C —— 合并器**
`lsu_coalescer.sv` + 穷举/随机对拍(8 lane × 同行/跨行/同地址/屏蔽子集/全分散),
比对 `req_num`、行地址序、每行 lane 集合与 `wen`,以及"每次访存的 32B 行数"。
*验收*:对 clear/gouraud 的访存地址模式复现 1.00 / 3.73 行。

**Phase D —— LSU + 调度器 + 全局路径**
`lsu.sv` + `lsu_scheduler.sv` + `mem_map.sv` + `global_mem.sv` + `wcb.sv`,先接一个 cocotb
AXI4 RAM 从模型(固定/随机延迟、可选 SLVERR)。
*验收*:`--dut lsu --werror` 全绿;含"随机 ready 背压""响应迟到""4 项 outstanding 打满"
"WCB 合并 + 命中转发(RAW)""`bar.sync`/`exit` 冲刷""四类错误注入"。

**Phase E —— 多主仲裁与计数器**
`axi_arb.sv` + `mem_perf.sv`:CP/2 SM/2D 的公平性与饿死检查,计数器复现参考模型的
`gmem_lines` / `shared_extra_cycles` / `mmio_from_kernel` / `unmapped_access`。
*验收*:随机多主压力下不丢请求、不错序;计数与 `sim/simulator` 同名统计一致。

**Phase F —— 集成到 SM**
`exu.sv` 数据源 mux、`wbu` 写回、warp 调度器的 stall/ready;`clear.S`/`gouraud.S`/`matmul.S`
端到端与参考模型对拍(帧缓冲内容 + 行请求计数),并把 ISA §8.1 的实测值回填。

---

## 7. 验证手法

- **纯组合**(`lsu_coalescer`、`mem_map`):穷举 + 随机 + Python 金标准(规则写两遍,
  独立那份只抄文档);
- **时序**(`lsu`/`lsu_scheduler`/`wcb`/`shared_mem`):事件驱动对拍 + 扰动(随机 `ready`、
  随机响应延迟、满 outstanding)—— 与 `tb/tests/core/test_fetch.py` 里那套扰动用例同风格;
- **系统**(含 AXI):AXI4 RAM 从模型 + 随机读写序列 + 与"顺序语义"对拍;
- **真实内核**:`sw/kernels/*.S` 编译成镜像跑,或把访存序列抽成 trace 做 trace-driven 回放
  (不依赖尚未实现的 warp 调度器);
- **错误路径**:每类错误一个反例(ISA §2.6 的既有要求),参考模型里已有可复用内核。

---

## 8. 风险与依赖

| 风险 | 对策 |
|---|---|
| 依赖还没写的 warp 调度器(谁 stall 谁) | 先用"单 warp + mock 调度器"把 LSU 解耦出来测,接口只要求 `warp_stall/ready` |
| 参考模型没有 WCB(store 立即生效) | 用 D2 的读旁路保证架构语义一致,否则端到端对拍会假失败 |
| AXI 响应乱序 / ID 复用 | v1 限定同 ID、in-order;乱序留 v2 |
| 共享内存竞争语义未定义(跨 warp 无 barrier) | 按 D6 钉死,并在 SPEC 写明"约定不发生竞争"(ISA §3.2 原话) |
| 帧缓冲与 2D 引擎共享主口 | `axi_arb` 用计数器验证公平性,别让 shader 写爆帧缓冲时把 2D 饿死 |
| interface 无法单独 lint | `mem/axi4_bus.sv` 要配一个 wrapper 才能 lint/仿真(Phase E 一并做) |

---

## 9. 需要回填到权威文档的地方

1. **SPEC §3.2 的 `dmem_req`/`dmem_rsp`**:从简写细化成"每拍一个 32B 行请求"的字段表
   (`{valid, we, line_addr[32], wdata[8][32], word_en[8], lane_mask[8]}` /
   `{valid, rdata[8][32], err[2]}`),并追加一行变更记录
   (与 `imem_req/imem_rsp` 补 `valid` 是同一种补全);
2. **CMDS §7.2 的信号量区**:参考模型的 `region_name()` **没有**信号量区
   (`0x0300_0000` 起,CMDS §7.2 有),`mem_map` 要按 CMDS 实现,并把这个差异在参考模型里补上;
3. **`NO_MEM_REGION` 的口径**(D7):参考模型默认只告警、`strict_mem` 才报错,而 ISA D14 要求
   报错即定位;两者取其一并写进 ISA/SPEC;
4. **WCB 的规格**:ISA §8 只说"约 4~8 项",建议把最终选定的项数、行粒度(32B)、
   冲刷触发(`exit`/`bar.sync`/满)与**读旁路**语义补进 SPEC §3.4;
5. **SPEC §3.4 的仲裁口径**:CP 优先级、读/写通道独立仲裁、饿死检查方式。
