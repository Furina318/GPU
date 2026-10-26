# cp.md —— CP(命令处理器)调度器设计方案

版本 0.2 · 关联文档:`CMDS.md`(**权威**,§2 队列/§2.4 校验表/§4 LAUNCH/§6 各命令/§7 MMIO/§8 错误/§9 状态机/§10 信号量)、`SPEC.md` §2/§3.1/§4、`ISA.md` §2.2/§2.5/§2.6/§8、`GFX.md` §3/§4/§7
参考模型:`sim/simulator/include/cp.hpp` + `sim/simulator/src/cp.cpp`(golden)、`sim/simulator/src/main.cpp`(host 侧 MMIO)、`sw/demo/demo.hpp`(驱动契约)

> **本文档回答什么**:CP 的**调度器**在 RTL 里长什么样 —— 模块怎么拆、状态机怎么走、
> 变长命令怎么取、依赖/完成/超时怎么判、错误怎么停与怎么恢复、拿什么验收。
> **不回答什么**:命令编码与 MMIO 偏移(权威在 `CMDS.md`)、SM 内部怎么执行
> (`ISA.md`)、2D 引擎内部语义(`GFX.md`)。本文档只在"实现计划 + 信号级接口"这一层说话,
> 与那三份冲突时以它们为准;两边不一致的地方列在 §12 与 §13,凡已回填到权威文档的会就地注明章节。

**实现状态**:`hw/rtl/cp/` 已全部落地(`cp_pkg.sv` / `axil_slave.sv` / `cp_regs.sv` / `cmd_decode.sv` / `crc32_word.sv` / `cp.sv`),15 个 cocotb 用例覆盖正例与错误路径(`tb/tests/cp/test_cp.py`),`--werror` 下零 lint 警告。本文档与实现的差异只保留在 §13(与参考模型的三处取舍)。

---

## 1. 职责边界

CP 是**单点顺序执行器**,只做命令级调度(CMDS §1):

| 做 | 不做 |
|---|---|
| 从命令环取命令、校验长度/保留位(§2.4)、按 `length_words` 跳过 | 不执行 shader 指令(那是 SM 的 warp scheduler) |
| 把内核镜像从全局内存**流式**预加载进指令 SRAM,校验镜像头 + CRC(§6.4) | 不解释 2D 参数含义(只按长度校验后原样转交,GFX.md §4) |
| 前置条件检查(LAUNCH 九条、GEMM 描述符、PARAMS 边界) | 不做乱序、不做抢占(CMDS §9 明确:in-order 是既定模型) |
| 分发:SM / 2D / GEMM;维护 `SM_KERNEL_MASK`、`SM_DONE` 观测 | 不碰帧缓冲几何/顶点格式(那是 GFX.md 与 2D 引擎的事) |
| 依赖与完成:信号量的读-改-写、`wait_id` 等待、完成判定(§10) | **不做 atomics** —— 信号量只由 CP 单点读改写(CMDS §10.2) |
| 错误:首个错误保留、置 `CP_ERROR`/IRQ、停机(§8.3);超时兜底(§9) | 不"尽力而为":错误态停机,只能 host 写 `CP_RESET`(§7.3) |

CP 与 SM 之间只有三件事:**分发(LAUNCH)/ 观测完成(`SM_DONE`、`SM_ERROR`)/ 观测现场(`SM*_FAULT_PC`)**(CMDS §1)。

---

## 2. 调度模型(设计核心)

一句话:**in-order、阻塞式、依赖只通过信号量表达**。

```
命令流:  [NOP] [PARAMS] [LAUNCH] [FENCE] [2D_DRAW] [WAIT] ...
             │        │          │        │         │
  执行时长:  1 拍    流式写常量   阻塞到 SM 全 DONE   等到前面全部完成  阻塞到 SEM >= value
```

每条命令的**完成语义**分三类(这张表是调度器的骨架):

| 类别 | 命令 | 什么时候算完成 | 占用多少拍(量级) |
|---|---|---|---|
| 立即完成 | NOP / WRITE_REG / READ_REG / SIGNAL / IRQ / PARAMS / FENCE | 做完就完了(READ_REG 要等 1 次 DMA 读、PARAMS 要等 N/16 次 64B 写) | 2 ~ 数十拍 |
| 引擎完成 | LAUNCH / GEMM / 2D_* | 等 `SM_DONE` 齐 / 等引擎 `DONE && !BUSY && !ERROR` | 整个 kernel 的时间 |
| 条件等待 | WAIT / 任何带 `wait_id` 的命令 | 等到条件满足(**不是**"等固定拍数") | 不确定 ⇒ 必须配超时 |

三条硬规则:

1. **一次只做一条命令**:上一条没到完成条件,`CMD_HEAD` 不前进、下一条不取(CMDS §9 的 `UPDATE` 相);
2. **`wait_id` 在分发前等**(§4.1 第 9 条):`SIGNAL/WAIT/FENCE/NOP` 等普通命令在**校验之后、执行之前**查;`LAUNCH/GEMM` 在自己的前置条件之后查(与参考模型 `cp.cpp:353` 同序);
3. **任何等待都有超时**:等 `SM_DONE`、等引擎、等信号量,统一用 `TIMEOUT_CYCLES` 计数 ⇒ `CP_ERROR = TIMEOUT`(§8.1 值 4)+ 停机。

`DOORBELL` 只是"提示",不是计数:CP 被触发后**连续取命令直到队列空或出错**(§2.1/§9)。落到 RTL 上是"锁存一个 pending 位",而不是"来一拍就处理一条命令" —— 尤其要覆盖"CP 正忙时 DOORBELL 又来了"(§9:期间新到的命令在下一轮取)。

---

## 3. 对外接口(信号级)

### 3.1 MMIO(host 侧,AXI4-Lite 从口)

| 方向 | 信号 | 说明 |
|---|---|---|
| in | `s_axil_awvalid/awaddr/awprot`、`wvalid/wdata/wstrb`、`bready` | 4KB 窗口,偏移见 CMDS §7;只支持 32b 对齐访问(非对齐/多拍 ⇒ 忽略并置 `BUS_ERROR`?见 D1) |
| out | `s_axil_awready/wready/bvalid/bresp` | 写响应固定 OKAY |
| in | `s_axil_arvalid/araddr`、`rready` | |
| out | `s_axil_arready/rvalid/rdata/rresp` | 读寄存器;未定义偏移返回 0 并计数(与参考模型 `cp.cpp:751` 一致) |
| out | `irq` | `IRQ_STATUS & IRQ_ENABLE`,一条线(§7.4) |

### 3.2 DMA(主口,AXI4)

| 用途 | 方向 | 访问模式 |
|---|---|---|
| 取命令环 | 读 | 顺序;建议 **64B(16 拍)INCR 突发** |
| `LOAD_KERNEL` 读镜像 | 读 | 顺序;按 64B 突发直到 `size_bytes` |
| `READ_REG` 落地写、`PARAMS` 写常量区、`SIGNAL`、信号量 `+= 1` | 写 | 单字写或按行合并 |

> **谁先谁后**:CP 的写必须在**分发下一条依赖它的命令之前**拿到 `B` 响应(否则 SM 可能读到旧参数)。
> 由于 CP 只有一个主口、in-order 发请求,**只要每条写命令在自己的完成条件里等 `B` 就够**(D6)。
> `FENCE` 在 RTL 里因此接近"空操作"(等自己所有未完成写落地),而语义上它保证的是
> "此前所有已分发命令都到了完成条件" —— 那些命令各自已经把写冲刷完了(ISA §2.4/§2.5 + §10.3)。

### 3.3 SM 侧(每个 SM 一组,共 2 组)

| 方向 | 信号 | 宽度 | 说明 |
|---|---|---|---|
| out | `sm_launch_valid[1:0]` | 1/个 | 分发脉冲 |
| out | `sm_launch_pc[1:0]` | 32 | = LAUNCH 的 `kernel_pc` |
| out | `sm_launch_warps[1:0]` | 4 | `num_warps`;SM 自己按"连续打包 + 槽号 = `wid mod 4`"算出哪些槽活(ISA §2.2) |
| out | `sm_launch_flags[1:0]` | 4 | 与镜像头交叉核对过的 `launch_flags` |
| in | `sm_done[1:0]` | 1 | 该 SM 的 `warp_alive` 全 0(§7.1);**CP 在分发时负责清它**,SM 侧只读 |
| in | `sm_busy[1:0]` | 1 | 可选:该 SM 是否有活跃 warp(便于区分"没启动"与"跑完了") |
| in | `sm_error[1:0]` | 4 | SM 错误码(CMDS §8.2);`!=0` ⇒ kernel 失败 ⇒ `CP_ERROR = SM_FAULT`、**不写信号量** |
| in | `sm_fault_pc[1:0]` | 32 | 出错现场;CP 只在 MMIO 读 `SM*_FAULT_PC` 时透传 |

**注意**:`sm_launch_*` 与现有 `hw/rtl/cp/` 之外的 RTL 要对得上 —— `core/fetch.sv` 的 LAUNCH 端口是
`launch`/`launch_pc`/`launch_warp`(lane 掩码),`simt_stack.launch_mask` 同义。**SM 顶层**负责把
`num_warps` 翻译成"哪些 warp 的 fetch 收到 `launch`,以及 `launch_warp` = 全 1";CP 只发 `num_warps`。

### 3.4 指令 SRAM 预加载(每个 SM 一组)

对接 `mem/instr_mem.sv` 已有的预加载口:

| 方向 | 信号 | 说明 |
|---|---|---|
| out | `sram_load_en[1:0]` | 写使能(按 `sm_mask` 逐位) |
| out | `sram_load_addr[1:0]` | 13 bit 字地址(多一位用于越界自检) |
| out | `sram_load_data[1:0]` | 32b |
| out | `sram_load_last[1:0]` | 最后一个字那一拍 |
| in | `sram_load_done[1:0]` | 下一拍脉冲 |
| in | `sram_err_ovf[1:0]` | 越界(CP 已校验 `size ≤ 16KB`,它响了就是实现出事 ⇒ `CMD_ERROR`) |

镜像**只读一次、同时广播给 `sm_mask` 里的每个 SM**(D3):一份数据流,两个写口,省一半带宽。

### 3.5 引擎侧(2D / GEMM)

SPEC §3.1 的形状 `engine_cmd = {engine[1:0], opcode[8], len[16], payload[]}` 需要落成可实现的接口:

| 方向 | 信号 | 说明 |
|---|---|---|
| out | `eng_valid`, `eng_engine[1:0]`(0=2D,1=GEMM), `eng_opcode[7:0]`, `eng_len[15:0]` | 命令头 |
| out | `eng_wdata[31:0]`, `eng_wvalid`, `eng_wlast` | **payload 流**(word2 起逐字推) |
| in | `eng_ready` | 引擎能收(忙时 CP 停在这里等,不丢命令) |
| in | `eng_status[2:0]` | `{ERROR, DONE, BUSY}`(2D:`0x68`,GEMM:`0x60` 的位定义) |
| in | `eng_error[3:0]` / `eng_error_ext[7:0]` | 引擎错误码(`2D_ERROR` 6 位枚举 / `GEMM_ERROR`) |

payload 上限:**GEMM 16 字(64B) / 2D 最大 9 字** ⇒ 引擎侧给 **16 字(64B)的输入缓冲**足够(D8);
`PARAMS`(可达 260 字)不进引擎,由 CP 自己写常量区。

---

## 4. 内部模块划分

| 文件(建议) | 职责 | 关键点 |
|---|---|---|
| `cp/cp.sv` | 顶层:实例化 + 状态机 + 命令分发/完成的仲裁 | 只做"接线的状态机",不放数据通路 |
| `cp/cp_fsm.sv` | CMDS §9 的状态机(IDLE/FETCH/DECODE/DISPATCH/WAIT/UPDATE/ERROR)+ 每条命令的微序列 | §5 |
| `cp/cp_regs.sv` | MMIO 寄存器文件(CMDS §7 全表)+ 读写属性 + 复位值 | §11 |
| `cp/axil_slave.sv` | AXI4-Lite 从口 → 寄存器读写 | 流水响应 |
| `cp/cmd_fetch.sv` | 队列指针(`head/tail/base/size`)、64B 行缓冲、变长命令的边界检查 | §6,最易错的一块 |
| `cp/cmd_decode.sv` | §2.4 校验表的硬件版:长度/保留位/操作数范围 | §7 |
| `cp/kernel_loader.sv` | LOAD_KERNEL:流式读镜像 + 镜像头校验 + **CRC32** + 双 SM 广播 + 记 `{entry,flags}` | §9 |
| `cp/sem_unit.sv` | 信号量:`+= 1`(读-改-写)、`SIGNAL`、`WAIT` 轮询、`wait_id` 判定 | §8 |
| `cp/launch_unit.sv` | LAUNCH 九条前置条件 + 分发 + 完成判定(SM_DONE/SM_ERROR)+ 超时 | §8 |
| `cp/engine_unit.sv` | 2D/GEMM:payload 流 + 忙/完成/错误观测 + `ENGINE_FAULT` | §10 |
| `cp/cp_err.sv` | 首个错误保留、IRQ 聚合、停机 | §12 |
| `cp/cp_perf.sv`(可选) | 命令计数、每类命令的等待周期数 | §13 |

> `hw/rtl/cp/` 里**不要**再放一份 MMIO 从口到 `mem/`(SPEC §3.4 的"内存子系统 MMIO 从口"与
> SPEC §3.1 的"CP 的 mmio_req/rsp"口径重叠)—— 见 §14 的 D1 与 SPEC §3.1。

---

## 5. 状态机(把 CMDS §9 展开成可实现的状态)

顶层:FU = 命令取指单元,EX = 命令执行/等待单元,UP = 提交(更新 `HEAD`、打信号量、置 IRQ)。

```
        ┌──────────┐  DOORBELL & HEAD!=TAIL
        │  C_IDLE  │──────────────────────────┐
        └────┬─────┘                          │
             │ HEAD==TAIL                    ▼
             │                        ┌────────────┐
             │                        │ C_FETCH_HDR│ 读 word0/word1(一次 64B 行缓冲)
             │                        └─────┬──────┘
             │                              ▼
             │                        ┌────────────┐  长度/保留位/opcode 非法
             │                        │ C_DECODE   │───────────────► C_ERROR
             │                        └─────┬──────┘
             │                              ▼
             │                        ┌────────────┐  需要 wait_id / 需要等引擎
             │                        │ C_DISPATCH │──────► C_WAIT_DEP(信号量)
             │                        └─────┬──────┘──────► C_WAIT_DONE(SM/引擎)
             │                              │ 立即完成类命令
             │                              ▼
             │                        ┌────────────┐
             └────────────────────────│ C_UPDATE   │  HEAD += len*4(含回绕)、SEM[id]+=1、IRQ
                                      └─────┬──────┘
                                            │ 任一等待超时 / BUS_ERROR / SM 报错
                                            ▼
                                      ┌────────────┐
                                      │ C_ERROR    │ 停机: 锁首个错误 + IRQ_CP_ERROR
                                      └─────┬──────┘
                                            │ host 写 CP_RESET
                                            └──────► C_IDLE
```

子状态(在执行单元内部,不暴露给 MMIO):

| 子状态 | 干什么 |
|---|---|
| `X_STREAM` | 把命令体的第 3..len 字流给消费者(常量区写 / 引擎 FIFO / SRAM 预加载) |
| `X_DMA_W` | 发起一次写(DMA),等 `B` 响应 |
| `X_DMA_R` | 发起一次读,等 `R` 数据 |
| `X_SEM_RMW` | 信号量 `+= 1` 的读-改-写三拍 |
| `X_POLL` | 轮询等待(`WAIT` 命令、`wait_id`),每 N 拍重读一次(D6) |
| `X_LAUNCH` | 断言 `sm_launch_*` 一拍,然后进 `C_WAIT_DONE` |
| `X_DONE` | 命令完成,回 `C_UPDATE` |

上面的 `C_*` / `X_*` 是**概念状态**,便于对着 CMDS §9 讨论;RTL 里落地成 **21 个 `ST_*` 状态**(`cp.sv` 的 `cp_state_t`),两者的对应关系:

| 概念状态 | RTL 状态 | 说明 |
|---|---|---|
| `C_IDLE` | `ST_IDLE` | 等 `DOORBELL` 且 `HEAD != TAIL` |
| `C_FETCH_HDR` | `ST_LOADW` / `ST_LOADW2` | 读 word0/word1;行缓存 64B,一条命令一次取行 |
| (校验) | `ST_LENCHK` / `ST_DECODE` | CMDS §2.4 的长度/保留位/opcode 校验(只占 2 拍,不设独立概念状态) |
| `C_DISPATCH` | `ST_EXEC` | 按 opcode 分派;`WRITE_REG`/`READ_REG`/`NOP` 等立即完成类也在这里一次做完 |
| `X_STREAM` | `ST_PARAMS`/`ST_PARAMS_RET`、`ST_LOADK`/`ST_LOADK_ND`、`ST_ENG_SEND` | 常量区写、镜像读 + SRAM 预加载 + CRC、引擎 payload 流 |
| `X_DMA_R`/`X_DMA_W` | `ST_MEM` | **一个状态复用于 7 类操作**(经 `ret_st` 记录返回点):读行、读字、写字、SRAM 预加载读、`READ_REG` 落地写、PARAMS 写、信号量读写 |
| `X_POLL` | `ST_POLL` / `ST_POLL_WAIT` | `WAIT` 命令与 `wait_id` 的轮询;`poll_gap` 做 8 拍节流,实测重读周期 12 拍 |
| `X_LAUNCH` | `ST_LAUNCH` | 断言 `sm_launch_*` **一拍** |
| `C_WAIT_DONE` | `ST_WAIT_SM` / `ST_WAIT_ENG` | 等 `SM_DONE` / 引擎 `DONE`(电平协议) |
| `X_SEM_RMW` | `ST_SEM_WR` | 信号量 `+= 1` |
| `C_UPDATE` | `ST_UPD` / `ST_UPD_FIN` | 推进 `HEAD`(组合写法,见 §6)、清行缓存、置 IRQ |
| `C_ERROR` | `ST_ERR` | 锁首个错误,等 `CP_RESET` |

> 状态数与拍数无关:单条 `NOP` 的墙钟 8.4 拍(未对齐)/ 31 拍(64B 对齐后的取指行),多出来的状态都是**一拍臂**;把它们合并只会把状态位换成 flag 位,面积不减、可调试性变差。

**每条命令的微序列**(实现时照着写):

| 命令 | 微序列 |
|---|---|
| NOP | 校验 → 跳过 `len*4` 字节 → UPDATE |
| WRITE_REG | 校验(可写表)→ 寄存器写入(内部)→ UPDATE |
| READ_REG | 校验 → `X_DMA_R`(读 MMIO 寄存器)→ `X_DMA_W`(写 `dst_addr`)→ UPDATE |
| SIGNAL | 校验 → `X_DMA_W` → UPDATE |
| WAIT | 校验 → `X_POLL`(`SEM[addr] >= value`)→ UPDATE |
| IRQ | 校验 → 置 `IRQ_STATUS[irq_id]` → UPDATE |
| PARAMS | 校验 → `X_STREAM` 写常量区(按 64B 突发写)→ UPDATE |
| FENCE | 等自身未完成写落地 → UPDATE |
| LOAD_KERNEL | 校验 → `X_STREAM` 读镜像 + 写 SRAM + CRC → 比 CRC/flags → 置 `SM_KERNEL_MASK` → UPDATE |
| LAUNCH | 九条前置检查 → `wait_id` → `X_LAUNCH` → `C_WAIT_DONE` → 完成判定 → UPDATE |
| GEMM / 2D_* | 长度/保留位校验 → `wait_id` → `X_STREAM` 推 payload → `C_WAIT_DONE`(等引擎)→ UPDATE |
| 厂商自定义(0x80+) | 只按 `length_words` 跳过(§2.4)→ UPDATE |

---

## 6. 命令取指与队列指针(实现里最容易错的一块)

寄存器:`queue_base`、`queue_size`、`head`、`tail`(全局字节地址)。

1. **行缓冲**:一次 DMA 读 **64B(16 字)**到 `cmd_line[0:15]`,地址 = `head & ~63`(D2)。
   命令头一定是 `head` 起的前两个字;命令体从 `head+8` 起。行缓冲只在"命令确实落在这 64B 内"时可用;
   否则(命令跨 64B 边界)**退化成逐字读**。驱动按 64B 填充时永远走快路径。
   - **行缓冲的生命周期 = 一条命令**:完成判定那一步(`ST_UPD_FIN`)无条件清 `line_valid`。
     这是**正确性**要求而不是优化取舍:host 可以在命令之间改写队列里已被消费的区域 ——
     写 `CMD_HEAD`/`CMD_QUEUE_BASE`/`CMD_QUEUE_SIZE` 会把 `head` 搬回已被消费过的位置,环形队列回绕也会回到同一行。
     若跨命令复用行缓冲,CP 就会拿"旧命令的字节"重新取指(实测:改 `BASE` 搬回原基址后,缓存里的旧 `2D_FILL`
     被当成新命令执行)。参考模型没有缓存(逐字读内存),所以这条也保证了 RTL 与模型一致。
     代价:未按 64B 填充的命令流从"每行 2~3 条命令"退回"每条命令一行",按 §2.1 填充的驱动不受影响(本来就一条一行)。
2. **长度**:`length_words = word0[31:16]`;**`< 2` ⇒ `CMD_ERROR`**(§2.4)。
3. **不得跨越队列末尾**:`head + len*4 > base + size` ⇒ `CMD_ERROR`(驱动应当用 NOP 填到末尾,§2.1)。
4. **不得越过 `CMD_TAIL`**:命令体必须整个落在 `[head, tail)` 里(考虑回绕)。
   参考模型的判据是 `cp.cpp:328-337`,`RTL` 用一个 `wrap` 位 + 两次比较实现同一件事 —— **这一条要用穷举用例测**(现有 `test_queue_boundary_errors` 覆盖越界与回绕)。
5. **`head` 的推进**在完成判定之后(`C_UPDATE`);失败的命令**不推进** `head`,而是进 `C_ERROR` 停机 ⇒ 不会重复执行(与 `cp.cpp:358-361` 同语义)。
6. `head`/`tail` 的写:host 可写 `CMD_TAIL`(驱动契约)、`CMD_HEAD`(异常恢复);`CMD_QUEUE_BASE/SIZE` 只在 **CP IDLE 且队列为空**时允许改(§2.1)。
   - **改写 `CMD_HEAD`/`CMD_QUEUE_BASE`/`CMD_QUEUE_SIZE` 之后,CP 必须重新从内存取指**:这三个寄存器会把 `head` 搬回已被消费的环位置,
     所以取指行缓存不能在命令之间存活(见上面第 1 条)。回归用例:`tb/tests/cp/test_cp.py::test_queue_cfg_rules` 的最后一步
     (改 `BASE` 到别处再搬回来,然后提交一条命令,必须按内存里的新命令执行)。
   - **两条访问路径的错误报告方式不同**(`cp.sv` + `cp_regs.sv` 的实现就是按这个分工写的):
     - 经 `WRITE_REG` 命令改 ⇒ **时机不对报 `ILLEGAL_STATE`、取值不对报 `CMD_ERROR`**(判定在 `cp.sv` 的 `ST_DECODE`:`wr_reg_bad_state`/`wr_reg_bad_value`);
     - host 直写 MMIO ⇒ **该写被忽略、寄存器保持原值、不置 `CP_ERROR`**(门控在 `cp_regs.sv` 的 `ext_wr_now = ext_wr & (que_cfg_wr ? idle_i : 1'b1)`;host 直写不挂在任何命令上,没有可归属错误的命令)。
   - `CMD_QUEUE_SIZE` 的取值必须 **≥ 8 且为 8 的倍数**(§2.1 + §6.1 的 NOP ≥ 2 字),否则按上面的分工处理;`0` 会让取模除零(参考模型修过一个 SIGFPE,`run_tests.py:266` 有用例)。取值规则在两条路径上**一致**(`cp_regs.sv:232` 的 `wr_data >= 8 && wr_data[2:0] == 0` 与 `cp.cpp` 的 `write_reg`/`WRITE_REG` 判定同规则)。
   - `CMD_QUEUE_BASE` 必须 4B 对齐且不在 MMIO 窗口;时机规则同上,取值非法时两条路径同样一个报错、一个忽略。
   - **例外:`CMD_HEAD` 只有 host 直写这一条写路径**(`WRITE_REG` 写它 ⇒ `CMD_ERROR`):它是 CP 的读取指针,命令流改它等于让 CP 跳过/重读命令;
     "丢弃未处理命令"是 `CP_RESET` 的职责(§7.3)。`CMD_TAIL` 相反 —— 它是驱动的前进指针,允许在命令流里推进。
   - 两条路径写的是**同一个寄存器**(一份状态,没有副本),差别只在"错误怎么报"——见 SPEC.md §3.1 契约 5~7。

---

## 7. 校验表在 RTL 里的落地(§2.4)

`cmd_decode.sv` 就是 CMDS §2.4 那张表的硬件版,输出 `{ok, err_code, len_expected, reserved_mask}`:

| opcode | 长度 | 保留位必须为 0 | 额外校验 |
|---|---|---|---|
| 0x00 NOP | ≥2 | — | — |
| 0x01 WRITE_REG | =4 | — | `addr` 4B 对齐、在 MMIO 窗口内、**在可写表里** |
| 0x02 READ_REG | =4 | — | `addr` 对齐且在 MMIO;**`dst` 对齐且不在 MMIO 窗口** |
| 0x03 LOAD_KERNEL | =5 | `w4[31:2]` | `size ≤ 16384`、4 的倍数、`sm_mask != 0`、`src` 对齐 |
| 0x04 LAUNCH | =5 | `w3[31:8]` | §4.1 九条(见 §8) |
| 0x05 FENCE | =2 | — | — |
| 0x06 SIGNAL | =4 | — | `sem_addr` 对齐且不在 MMIO 窗口 |
| 0x07 WAIT | =4 | — | `sem_addr` 对齐 |
| 0x08 IRQ | =3 | `w2[31:8]` | `irq_id ≤ 7` |
| 0x09 PARAMS | =4+N | `w2[31:16]`、`w3[31:16]` | `dst_off` 对齐且 `<4096`;`1 ≤ N ≤ 256`;`off + N*4 ≤ 4096` |
| 0x0A–0x0F | — | — | 保留 ⇒ `CMD_ERROR` |
| 0x10–0x14 | **固定** 6/9/8/4/2 | 各命令见 GFX §4.1–4.6 | 长度不等于规定值 ⇒ `CMD_ERROR`;参数/几何问题**不在这里报**(引擎报 `2D_ERROR`) |
| 0x15–0x17 | ≥2 | — | v1 保留 ⇒ 转发给 2D 引擎,由引擎报 `BAD_PARAM`(GFX §7.3 值 6) |
| 0x18 GEMM | =16 | `w10[31:16]`、`w12[31:8]`、`w13..w15` | 基址 16B 对齐;`1 ≤ M,N,K ≤ 4096`;`lda/ldb/ldc` 合法;`dtype==0`;`REQUANT` 未实现 ⇒ `CMD_ERROR` |
| 0x19–0x7F | — | — | 保留 ⇒ `CMD_ERROR` |
| 0x80–0xFF | ≥2 | — | **按长度跳过**并计数(厂商自定义) |

命令头的**两处通用校验**(在 `cmd_fetch` 里,先于 opcode):
`cmd_flags[7:2] != 0` ⇒ `CMD_ERROR`;`signal_id`/`wait_id` 落在 `0x0100..0xFFFE` ⇒ `CMD_ERROR`(`cp.cpp:338-344`)。

---

## 8. 依赖与完成判定

### 8.1 信号量单元(`sem_unit.sv`)

- 地址 = `SEM_BASE + id*4`,`id ∈ 0..255`;`0xFFFF` = 无依赖(`SEM_NONE`)。
- **完成计数**:命令完成 ⇒ `SEM[id] += 1`,实现是 `X_SEM_RMW` 三拍(读 → +1 → 写),**不缓存值**(多命令串行,不需要 cache)。
- **`SIGNAL`**:直接写指定地址。
- **`WAIT`**:轮询 `SEM[addr] >= value`(**`>=` 而不是 `==`**,§6.7);每 N 拍重读一次(D6)。
- **`wait_id`**:判据是 `SEM[wait_id] != 0`(`cp.cpp:379`),RTL 与模型一致;该口径已回填到 CMDS §10.2(命令头没有比较值字段,要等具体数值请用 `WAIT`)。

### 8.2 LAUNCH 的九条前置条件(§4.1,顺序不能变)

| # | 条件 | RTL 判定 | 失败码 |
|---|---|---|---|
| 1 | 长度/保留位 | `cmd_decode` | `CMD_ERROR` |
| 2 | 涉及 SM 的 `SM*_ERROR == 0` | `sm_error[sm_mask]` 全 0 | `STALE_SM_ERROR` |
| 3 | `1 ≤ num_warps ≤ 8` | 比较器 | `CMD_ERROR` |
| 4 | `kernel_pc` 4B 对齐且 `< image_size` | 与记录的 `image_size` 比 | `CMD_ERROR` |
| 5 | 涉及 SM 都加载过镜像 | `SM_KERNEL_MASK & sm_mask == sm_mask` | `CMD_ERROR` |
| 6 | `launch_flags` 与镜像头 `image_flags` 逐位一致 | 保存的 `image_flags` 与 `w4` 比 | `CMD_ERROR` |
| 7 | barrier/shared/single-SM 且 `num_warps ≥ 5` | `flags[0]|flags[1]|flags[3]` 与 `num_warps>4` | `LAUNCH_REJECT` |
| 8 | 常量区已写好 | 无法机械校验(驱动契约,§4.1 第 8 条) | — |
| 9 | `wait_id` 满足 | §8.1 | `TIMEOUT` |

**第 2 条必须排在 `num_warps` 之前**(`run_tests.py:240` 有用例守着:SM 有错时报 `STALE_SM_ERROR` 而不是 `CMD_ERROR`)。

### 8.3 LAUNCH 完成判定(§4.3)

```
分发时: 清涉及 SM 的 SM_DONE(通过 sm_launch_valid 脉冲,SM 侧负责清 warp_alive)
等待:   所有 sm_mask 位对应 SM 的 sm_done == 1
       └─ 任一 sm_error != 0  ⇒ CP_ERROR = SM_FAULT(=7)、不写信号量、置 IRQ
       └─ 超过 TIMEOUT_CYCLES  ⇒ CP_ERROR = TIMEOUT
完成:   SEM[signal_id] += 1(若 signal_id != 0xFFFF)、置 IRQ_LAUNCH_DONE
```

> `SM_DONE` 的清除时机是 §4.3 特意点出来的坑:不清就会**第二个 kernel 立刻"完成"**,
> 信号量提前释放,2D 读到没算完的顶点。RTL 里把它做成"分发脉冲的一部分",不要依赖 host。

---

## 9. LOAD_KERNEL 的数据通路

```
DMA 读(64B 突发) ──► 镜像头校验(前 32B) ──► 指令 SRAM 预加载口(广播到 sm_mask)
                        │                        │
                        │                        └─► CRC32 流水(1 字/拍)
                        └─► 记 {entry_off, image_flags, image_size}
完成 ⇒ 比 code_crc32 与流式算出的值 ⇒ 一致才置 SM_KERNEL_MASK 对应位
```

- **镜像头**(CMDS §6.4):magic `0x5347_4B31`、`entry_off`、`image_size`、`image_flags`、`code_crc32`、`code_size`、保留字必须 0。
- **CRC-32/ISO-HDLC**(poly `0xEDB88320`,初值/末值取反,与 `zlib.crc32` 一致):用一个 32b LFSR 单元边流边算,覆盖**指令区**(`entry_off` 起 `code_size` 字节)。
- **`err_ovf` 自检**:CP 已经保证 `size ≤ 16KB` 且 `entry_off < size`,所以 `instr_mem` 的 `err_ovf` 正常永不响;响了就是实现 bug ⇒ 归到 `CMD_ERROR`(不静默)。
- **前置状态检查**:`CP BUSY`(有 kernel 在跑)时执行 `LOAD_KERNEL` ⇒ `ILLEGAL_STATE`(§6.4)。
- 失败时**不留半成品**:`SM_KERNEL_MASK` 只在全部校验通过后才置位;已经写进 SRAM 的半份镜像无害(LAUNCH 会让它重新加载),但要保证 `SM_KERNEL_MASK=0`。

---

## 10. 引擎分发(2D / GEMM)

- **CP 的义务**只有"长度/保留位校验 + 原样转交"(§6.9/§12):参数含义、几何、格式都是引擎的事。
- 分发:`eng_valid/opcode/len` + payload 流 + `eng_ready` 背压;引擎忙时 CP 停在 `X_STREAM`(不丢命令)。
- 完成:引擎 `DONE && !BUSY && !ERROR`;完成且无错 ⇒ `SEM[signal_id] += 1` + `IRQ_2D_DONE`/`IRQ_GEMM_DONE`。
- 出错:引擎 `ERROR` ⇒ `CP_ERROR = ENGINE_FAULT(=8)`,**不写信号量**,细节看 `2D_ERROR`/`GEMM_ERROR`。
- `2D_STATUS`/`GEMM_STATUS`/`CP_STATUS` 的位定义:`{ERROR, DONE, BUSY}`(CMDS §7/§6.11、GFX §7.2)。
- **引擎未实现的取值**(`dtype != 0`、`REQUANT`、保留 opcode)一律 `CMD_ERROR` 而不是静默忽略(§2.4/§6.11)。

---

## 11. MMIO 寄存器文件(`cp_regs.sv`,CMDS §7 全表)

| 偏移 | 名称 | 读 | 写 | 复位值 |
|---|---|---|---|---|
| 0x00 | `CP_STATUS` | `{ERROR,BUSY,IDLE}` | — | `0x1`(IDLE) |
| 0x04 | `CP_ERROR` | 首个错误码 | 写任意值清零 | 0 |
| 0x08 | `CMD_HEAD` | `head` | **只有 host 能写**(`WRITE_REG` 写它 ⇒ `CMD_ERROR`,`chk_wr_ok` 里没有它)做异常恢复;限 `[base, base+size)` **且 4B 对齐**,否则丢弃该写、保持原值 | `=base` |
| 0x0C | `CMD_TAIL` | `tail` | 驱动写 | `=base` |
| 0x10 | `DOORBELL` | 0 | 写任意值 ⇒ 置内部 pending 位 | — |
| 0x14 | `IRQ_STATUS` | 中断位图 | 写任意值清零 | 0 |
| 0x18 | `IRQ_ENABLE` | 使能位图 | R/W | 0 |
| 0x1C | `SM_DONE` | `{sm_done[1:0]}` | — | — |
| 0x20/0x24 | `SM0/1_ERROR` | SM 错误码 | 写任意值清零(经 SM 侧复位线) | 0 |
| 0x28 | `SEM_BASE` | R/W | 写 | `0x0300_0000` |
| 0x2C | `CMD_QUEUE_BASE` | R/W | 写(须 4B 对齐且 ≥ `MMIO_END`;非法值忽略、仅在 IDLE 可写;写后 `head=tail=新基址`) | `0x0100_0000` |
| 0x30 | `CMD_QUEUE_SIZE` | R/W | 写(须 ≥8 且 8 的倍数;非法值忽略、仅在 IDLE 可写) | 4096 |
| 0x34 | `CONST_BASE` | 只读 `0x0200_0000` | — | — |
| 0x38 | `FB_BASE` | R/W | 写 | `0x1000_0000` |
| 0x3C | `DDR_BASE` | 只读 `0x8000_0000` | — | — |
| 0x40 | `CP_RESET` | 0 | 写 1 ⇒ CP_RESET 序列(§7.3) | — |
| 0x44 | `TIMEOUT_CYCLES` | R/W | 写(0 ⇒ 回默认 `0x10_0000`) | `0x10_0000` |
| 0x48 | `SM_KERNEL_MASK` | `{sm0,sm1}` | — | 0 |
| 0x4C/0x50 | `SM0/1_FAULT_PC` | 透传 SM | — | 0 |
| 0x60/0x64 | `GEMM_STATUS`/`GEMM_ERROR` | 透传引擎 | `GEMM_ERROR` 写清零 | 0 |
| 0x68/0x6C | `2D_STATUS`/`2D_ERROR` | 透传引擎 | `2D_ERROR` 写清零 | 0 |

三条实现约定:

1. **错误寄存器是"值编码 + 写任意值清零"**,不是 W1C 位图(CMDS §7 的 v0.2 修正);
2. **首个错误保留**:`cp_err.sv` 只在 `CP_ERROR == 0` 时写入新错误(`cp.cpp:60-70`);
3. `CP_STATUS` 的精确定义(与模型 `cp.cpp:89-95` 一致):
   `IDLE = (queued==0 && !work_in_progress && !error)`,`BUSY = (queued!=0 || work_in_progress)`,`ERROR = error_latched`。

### 11.1 MMIO 访问结构:一份状态,两个访问口

```
                        ┌─────────────────────── cp 内部 ───────────────────────┐
   host / 其它单元 ──AXI4-Lite──► axil_slave ──►│  cp_regs(唯一的寄存器副本)  │◄── 直连线 ── cp_fsm
                        └───────▼───────────────┴──────────────▲───────────────┘
                                                               │ 每拍可读可写
   全局内存(命令环/镜像/常量区/信号量) ◄──AXI4 主口── cp 的 DMA 引擎(64B 突发)
```

- **寄存器文件(`cp_regs.sv`)是唯一副本**,CP 的状态机通过**直连线**读写它(零延迟、每拍可见);
  host 与其它单元经 `mmio_req/rsp` 这条 AXI4-Lite 从口访问**同一个**文件。**禁止影子副本** ——
  "CP 里一份、总线上又一份"必然出现"host 改了 `CMD_TAIL`、CP 还在读旧值"这类只在并发下暴露的错。
- 谁是拥有者就由谁存(契约 6):`SM*_ERROR`/`SM*_FAULT_PC` 在 SM,`2D_*`/`GEMM_*` 在引擎,CP 只**转发**到读口;
  host 的"写任意值清零"由 CP 译码后**扇出**到拥有者(需要 `sm_err_clr[1:0]`、`eng_err_clr[1:0]` 两根脉冲线)。
- **两类共享寄存器的 RTL 写法**(两处最容易出错的地方):
  | 寄存器 | 谁写 | RTL 结构 |
  |---|---|---|
  | `CMD_HEAD` | CP 推进 + host 异常恢复 | 两个写源,**CP 的推进优先级更高**;host 写在 CP 忙时丢弃(或按 §2.1 拒绝) |
  | `CMD_TAIL` / `IRQ_ENABLE` / `SEM_BASE` / `QUEUE_*` / `TIMEOUT_CYCLES` | host(或 `WRITE_REG` 命令) | 纯外部写、CP 只读;`QUEUE_*` 再叠一条"仅 IDLE 且队列空才允许改"的门 |
  | `CP_ERROR` / `IRQ_STATUS` | CP 置位、host 写清零 | 置位与清零分两条路径;`CP_ERROR` 加**首个错误保留**门(§12) |
  | `DOORBELL` / `CP_RESET` | host 写 | **边沿事件**,不是电平:写任意值 ⇒ 置一个内部 pending 位,由 CP 消费/自清(防止"CP 忙时敲门丢了") |
- **哪些东西**必须**留在主口(不能内部化)**:命令环、内核镜像、常量区、信号量 —— 它们物理上在**全局内存**里,
  只能走 `dma_req/rsp`。这里才是真正值得优化带宽的地方:64B 突发(§6/§13)。
- **效率账(为什么"效率低下"不是这里的理由)**:每帧的 MMIO 访问是**十几次**(写 `CMD_TAIL`、`DOORBELL`、
  清错误、偶尔读状态),每次 AXI4-Lite 往返 ≈ 10~20 拍 ⇒ **一二百拍/帧**;而一帧的 kernel 时间是**数万拍**
  (ISA §8.1 的实测:clear 4179 拍、matmul 12716 拍)。也就是说 MMIO 占比 < 1%,**改不改都不会更快**。
  真正值得做的是:① 寄存器内部直连(省掉状态机的等待,这是**正确性与简洁性**问题,不是带宽问题);
  ② 命令环/镜像/信号量走 64B 突发(这才是带宽问题)。

> 另一个常被顺手提出、但应当**拒绝**的做法:给常用寄存器做"影子副本 + 同步逻辑"(例如 CP 内部再存一份
> `TAIL` 的镜像,由 host 写的时候更新)。多一份就多一处不一致;本设计统一按"一份状态 + 两个访问口"。

---

## 12. 错误与恢复

| CP 错误(§8.1) | 触发 | 停机后 |
|---|---|---|
| 1 `CMD_ERROR` | 长度/保留位/镜像头/CRC/参数越界/未实现取值 | 锁存,等 `CP_RESET` |
| 2 `LAUNCH_REJECT` | flags 与 `num_warps ≥ 5` 冲突 | 同上 |
| 3 `BUS_ERROR` | 命令环读 / 镜像读 / `READ_REG` 落地写 / 信号量读写 的 AXI 错误响应 | 同上 |
| 4 `TIMEOUT` | 等 `SM_DONE`/引擎/信号量 超 `TIMEOUT_CYCLES` | 同上 |
| 5 `ILLEGAL_STATE` | BUSY 时 `LOAD_KERNEL`;IDLE+队列非空时改 `CMD_QUEUE_*` | 同上 |
| 6 `STALE_SM_ERROR` | LAUNCH 时涉及 SM 仍有未清错误 | 同上 |
| 7 `SM_FAULT` | 任一涉及 SM `sm_error != 0` ⇒ kernel 失败、**不写信号量** | 同上 |
| 8 `ENGINE_FAULT` | 2D/GEMM 引擎报错 | 同上 |

**恢复路径**(§7.3,固定顺序):`CP_RESET` → 读 `SM*_ERROR`/`SM*_FAULT_PC`/`CP_ERROR` 定位 → `LOAD_KERNEL` → 重新提交。

`CP_RESET` 的六件事(§7.3 逐条):① 状态机回 IDLE;② 清 `CP_ERROR`/`IRQ_STATUS`、`HEAD ← TAIL`;
③ 清 SM 错误/FAULT_PC/`warp_alive`;④ 清 `SM_KERNEL_MASK`(镜像失效);
⑤ 复位引擎(丢弃未完成的 GEMM/2D 命令与 2D 寄存器);⑥ **保持** `CMD_QUEUE_*`/`SEM_BASE`/`FB_BASE` 不变。
第六项是"方便重新提交"的关键,不要一复位就把配置也清了。

---

## 13. 与参考模型的三处已知差异(必须在 RTL 里写清楚)

| # | 参考模型 | RTL 应该怎么做 | 为什么要写下来 |
|---|---|---|---|
| 1 | `WAIT`/`wait_id` 不满足时**立刻**报 `TIMEOUT`(`cp.cpp:379-382`),理由是"模拟器单点顺序执行,没有别的执行体能推进它" | RTL **真的等待**(kernel 与引擎在并发跑),超时靠 `TIMEOUT_CYCLES` | 这是模型的**实现取舍**,不是架构语义;照抄会让 RTL 在正常并发场景下误报超时 |
| 2 | 全局写"直达内存",`FENCE` 是空操作 | `FENCE` 等自身未完成写落地即可(前置命令已完成 ⇒ SM 的 WCB 已冲刷) | 语义一致,但理由不同(ISA §2.5 的 `exit` 已负责冲刷) |
| 3 | `LOAD_KERNEL` 一次读完整镜像再校验 CRC(内存里 buf) | **边流边算 CRC**,不缓冲整份镜像 | 16KB 缓冲不值得;CRC 流水是 1 字/拍的等价实现 |

---

## 14. 实现决策(均已落地)

| # | 决策 | 结论 | 理由 |
|---|---|---|---|
| D1 | MMIO Lite 从口放哪 | **放 CP**(它是寄存器的唯一拥有者) | 省掉一层"Lite → 内部寄存器总线"的转发;SPEC §3.1/§3.4 已按此写 |
| D2 | 命令取指粒度 | **64B 行缓冲 + 落不下时退化逐字读** | 快路径 1 次突发;退化路径保证不依赖驱动的填充行为 |
| D3 | `LOAD_KERNEL` 写两个 SM | **读一份、广播两份** | 省一半 DMA 带宽;依赖 `instr_mem` 的两个独立预加载口(已有) |
| D4 | CRC32 | **专用 LFSR 单元,流式 1 字/拍** | 不需要缓冲整份镜像 |
| D5 | `wait_id` 的比较值 | **`SEM[id] != 0`**(与参考模型一致) | 判据已回填到 CMDS §10.2 |
| D6 | 等待/轮询策略 | `wait_id` 在分发前查一次;**`WAIT` 命令每 8 拍轮询**;超时按"总等待周期"计 | 轮询太快挤占 DMA,太慢增加延迟 |
| D7 | `DOORBELL` 语义 | **锁存 pending 位**,处理到队列空/出错为止 | 覆盖"忙时又敲门"的情形(§9) |
| D8 | 引擎 payload 深度 | 16 字(64B,覆盖 GEMM 与最大 2D 命令) | `PARAMS` 不进引擎 |
| D9 | `num_warps` 的翻译 | CP 只发 `num_warps`,**SM 顶层**算槽位/`launch_warp` | CP 不重复 ISA §2.2 的放置逻辑 |
| D10 | 队列基址/大小的修改时机 | 严格按 §2.1:仅 `IDLE` 且队列空;"空"= **本命令是最后一条**(`cp.cpp:412-416`) | 早一拍拒绝会误伤合法用法 |
| D11 | 引擎忙碌时 | CP 停在 `X_STREAM` 背压等待(不排队、不丢) | 与 in-order 模型一致;排队是 v2 的事 |
| D12 | MMIO 访问结构 | **一份状态 + 两个访问口**(寄存器文件在 CP 内,状态机直连;外部走 AXI4-Lite 从口) | §11.1:影子副本会引入并发不一致;而 MMIO 每帧只占 < 1% 的拍数,效率不是理由 |
