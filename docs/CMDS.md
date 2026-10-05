# 命令集编码、内存映射、驱动接口手册 (CMDS.md)

版本 0.3 (草案) · 关联文档: SPEC.md(整体架构)/ ISA.md(Shader 核 ISA)/ GFX.md(2D 引擎语义)· 变更记录见 [附录 B](#附录-b-变更记录)

**目录**

- [1. CP 职责](#1-cp-职责)
- [2. 命令队列与通用命令格式](#2-命令队列与通用命令格式)
- [3. 命令列表与编码](#3-命令列表与编码)
- [4. LAUNCH 命令详细](#4-launch-命令详细)
- [5. LAUNCH flags 位定义](#5-launch-flags-位定义)
- [6. 其他命令格式](#6-其他命令格式)
- [7. MMIO 寄存器映射](#7-mmio-寄存器映射)
- [8. 错误码定义](#8-错误码定义)
- [9. CP 状态机与复位](#9-cp-状态机与复位)
- [10. 完成与同步](#10-完成与同步)
- [11. 与 ISA.md 的接口](#11-与-isamd-的接口)
- [12. 与 GFX.md 的接口](#12-与-gfxmd-的接口)
- [13. 待定项](#13-待定项)
- [14. 总结](#14-总结)
- [附录 A: 跨文档冲突与处置](#附录-a-跨文档冲突与处置)
- [附录 B: 变更记录 (v0.1 → v0.2)](#附录-b-变更记录-v01--v02)

> **本文档由 ISA.md 反推得到**。ISA.md 明确把以下内容交给 CMDS.md —— 这张表就是本文档的验收清单,每一条都必须有落地位置:

| ISA 的要求 | ISA 位置 | 本文档的落地 |
|---|---|---|
| CP 的寄存器与命令编码 | ISA §2.2 | §2、§3、§7 |
| LAUNCH 的 `flags` 位定义 | ISA §2.2、D17 | §5 |
| 错误状态寄存器的位定义 | ISA §2.6 | §7、§8.2 |
| `sm_done` 的 MMIO 地址与位定义 | ISA §2.2、§2.5 | §7.1 |
| 内核元数据 `{entry, uses_barrier}` → LAUNCH flags 的路径 | ISA 附录 A、D17 | §6.4 内核镜像头 + §4.1 第 6 条交叉校验 |
| 常量区基址 `0x0200_0000` | ISA §3.4、D8 | §7.2 |
| 指令 SRAM 16KB / 4096 条 | ISA §2.2、§8 | §6.4 LOAD_KERNEL |
| warp 槽号 = `wid mod 4` | ISA §2.2 | §4.2 |
| "错误态不算完成"、不释放信号量 | ISA §2.5 | §4.3、§8.3、§10.1 |
| `exit`/`bar.sync` 的内存可见性 | ISA §2.4、§2.5 | §10.3 |

> **本文档的验证标准**: 每条命令的**长度、保留位、错误条件**都必须能被 CP 的 RTL 译码器与 Python 参考模型用同一张表判定。因此 §2.4 给出**命令校验表**、§8 给出**错误码表**、§4.1 给出**LAUNCH 前置条件表**;driver、RTL、参考模型三方共用这三张表,不允许任何一方"多知道一点"。

---

## 1. CP 职责

CP(Command Processor)是 SimpleGPU 的命令处理器,负责:

1. 从命令队列取命令(§2)。
2. 译码并校验长度/保留位(§2.4),再分发到对应引擎:Shader 核(SM0/SM1)、2D 引擎、矩阵引擎、内存子系统。
3. **把内核镜像从全局内存预加载到 SM 指令 SRAM**(§6.4),并维护"哪个 SM 已加载有效镜像"的状态(§7 `SM_KERNEL_MASK`)。
4. 跟踪命令完成,维护信号量与依赖关系(§10)。
5. 处理错误:任一 SM 错误 ⇒ 该 kernel 失败、**不释放**依赖信号量、置 `CP_ERROR` 与 IRQ(§8.3)。
6. 提供超时兜底与软件恢复路径(`TIMEOUT_CYCLES` + `CP_RESET`,§7、§9)—— barrier 挂起是设计上已知的失效模式,不能只靠"重启仿真"。

CP **不执行** shader 指令,只做命令级调度;shader 指令由 SM 内的 warp scheduler 执行。CP 与 SM 之间只有三个接口:**分发(LAUNCH)/ 观测完成(`SM_DONE`、`SM_ERROR`)/ 观测现场(`SM*_FAULT_PC`)**。

---

## 2. 命令队列与通用命令格式

### 2.1 命令队列(环形)

- 队列位于**全局内存**,基址 `CMD_QUEUE_BASE`(默认 `0x0100_0000`,按 PLAN §3),大小 `CMD_QUEUE_SIZE`(字节,必须是 4 的倍数,建议 ≥ 4KB)。
- `CMD_HEAD` 是 CP 的读取指针,`CMD_TAIL` 是驱动写入的边界;**CP 只处理区间 `[CMD_HEAD, CMD_TAIL)`,绝不超过 `CMD_TAIL`**。
- 空队列判据: `CMD_HEAD == CMD_TAIL`。
- 驱动追加命令后更新 `CMD_TAIL`,再写 `DOORBELL`(写任意值)提示 CP 取命令。CP 被触发后**连续取命令直到队列为空或出错**,不需要驱动重复敲门。
- **命令不得跨越队列末尾回绕**: 若从 `CMD_HEAD` 到队列末尾的剩余字放不下当前命令,驱动必须用 `NOP` 填充到末尾,再在偏移 0 处继续写。这是 `NOP` 的第一个实际用途(第二个用途是调试占位)。
- 队列在全局内存 ⇒ CP 读命令与 SM 的访存共享同一个 AXI 主口。所以命令应当**少而大**(用一条命令描述一批工作),而不是成千上万条小命令。
- `CMD_QUEUE_BASE`/`CMD_QUEUE_SIZE` 只允许在 CP `IDLE` 且队列为空时修改,否则 `ILLEGAL_STATE`。

### 2.2 通用命令头(**每个命令至少 2 个字**)

```
word0: [31:16] length_words   ; 含命令头的 32 位字数
       [15:8]  cmd_flags      ; 命令级标志,见 §2.3
       [7:0]   opcode         ; 命令类型,见 §3
word1: [31:16] signal_id      ; 完成时打信号量的槽位;0xFFFF = 不打
       [15:0]  wait_id        ; 分发前需等待满足的信号量槽位;0xFFFF = 不等
word2..: 命令参数(各命令自定,见 §4/§6)
```

- **为什么把 `signal_id`/`wait_id` 放进统一的 word1**(v0.2 的关键结构改动): v0.1 的 §4.3、§6.9、§10.1 都引用"命令带 `signal_id`"/"2D 命令可带 `wait_id`",但 §4 的 LAUNCH 编码里**根本没有这两个字段** —— 依赖关系无处安放。统一放在 word1 后: ① 任何命令(LAUNCH/2D/GEMM/内存搬运)都能挂依赖,CP 译码器不必按命令特判;② 以后给新命令加依赖时不必再改格式。
- 代价: 每条命令多 4B。相对命令数量(每帧几十条)与访存流量,可以忽略。
- `signal_id`/`wait_id` 的语义与槽位编号规则见 §10.2。

### 2.3 `cmd_flags` 位定义

| 位 | 名称 | 说明 |
|---|---|---|
| 0 | SYNC | 该命令完成前,CP 不取后续命令(强制串行点) |
| 1 | IRQ_ON_DONE | 完成时置 `IRQ_STATUS[IRQ_CMD_DONE]`(§7.4) |
| 2..7 | 保留 | **必须为 0**,否则 `CMD_ERROR` |

> v0.1 的命令头里有 `flags` 字节,但全文没有一处定义它 —— 保留字段不定义,实现者就会各写一个含义。这里按 ISA 的同一原则处理:**保留位非 0 一律报错**(ISA 侧对应 `ILLEGAL_INSTR`),不静默忽略。

### 2.4 命令校验表(CP 译码器 / Python 参考模型共用)

| opcode | 名称 | `length_words` | 保留位必须为 0 | 额外校验(失败 ⇒ §8.1 `CMD_ERROR`) |
|---|---|---|---|---|
| 0x00 | NOP | ≥ 2(可变长,用于回绕填充) | — | — |
| 0x01 | WRITE_REG | 4 | — | `addr` 落在 MMIO 窗口内且目标寄存器可写 |
| 0x02 | READ_REG | 4 | — | `dst_addr` 4B 对齐,且不在 MMIO 窗口内 |
| 0x03 | LOAD_KERNEL | 5 | word4[31:2] | `size_bytes ≤ 16384` 且为 4 的倍数;镜像头合法(§6.4);`sm_mask` 非 0 |
| 0x04 | LAUNCH | 5 | word3[31:8] | §4.1 的九条前置条件 |
| 0x05 | FENCE | 2 | — | — |
| 0x06 | SIGNAL | 4 | — | `sem_addr` 4B 对齐 |
| 0x07 | WAIT | 4 | — | `sem_addr` 4B 对齐 |
| 0x08 | IRQ | 3 | word2[31:8] | `irq_id ≤ 7` |
| 0x09 | PARAMS | 4 + N | word2[31:16]、word3[31:16] | `dst_off` 4B 对齐且 < 4096;`1 ≤ N ≤ 256` |
| 0x0A–0x0F | 保留 | — | — | 译码为非法 ⇒ `CMD_ERROR`(CP 域内未来扩展) |
| 0x10–0x17 | 2D_* | ≥ 2 | 由 GFX.md 定 | CP 只按 `length_words` 跳过并转交 2D 引擎 |
| 0x18 | GEMM | 16 | word10[31:16]、word12[31:8]、word13..word15 | 基址 16B 对齐;`1 ≤ M,N,K ≤ 4096`;`lda/ldb/ldc` 合法;dtype 已实现取值(0=INT8);`C_ACCUM` 时 C 需已初始化 |
| 0x19–0x1F | 保留 | — | — | 矩阵引擎未来扩展(如 GEMM_DESC 指针形式、量化校准表);译码为非法 ⇒ `CMD_ERROR` |
| 0x20–0x7F | 保留 | — | — | 同上,非法 ⇒ `CMD_ERROR` |
| 0x80–0xFF | 厂商自定义 | ≥ 2 | — | 教学项目自用:CP 只按 `length_words` 跳过,语义由实现决定 |

---

## 3. 命令列表与编码

| opcode | 名称 | 说明 |
|---|---|---|
| 0x00 | NOP | 空命令;队列回绕填充 / 调试占位 |
| 0x01 | WRITE_REG | 写 CP/引擎 MMIO 寄存器 |
| 0x02 | READ_REG | 读寄存器到全局内存 |
| 0x03 | LOAD_KERNEL | 把内核镜像从全局内存加载到 SM 指令 SRAM |
| 0x04 | LAUNCH | 启动 shader kernel |
| 0x05 | FENCE | 等待之前所有命令完成并冲刷,保证内存可见 |
| 0x06 | SIGNAL | 把信号量写成指定值 |
| 0x07 | WAIT | 等待信号量达到指定值 |
| 0x08 | IRQ | 触发中断 |
| 0x09 | PARAMS | 把参数块从命令流写入常量区(§6.10) |
| 0x0A–0x0F | 保留 | CP 域内未来扩展 |
| 0x10–0x17 | 2D_FILL / 2D_BLIT / 2D_DRAW / 2D_SET_REG / 2D_FLUSH … | 语义见 GFX.md;v0.1 只用到 0x10–0x14,故 0x15–0x17 也归 2D |
| 0x18 | GEMM | 矩阵引擎: `C = A×B (+C)`,16 字内联描述符(§6.11) |
| 0x19–0x1F | 保留 | 矩阵引擎未来扩展 |
| 0x20–0x7F | 保留 | 未来扩展 |
| 0x80–0xFF | 厂商自定义 | 教学项目保留 |

> **v0.1 的漏洞**: 表里从 0x08 直接跳到 0x10,既没写"0x09–0x0F 保留",也**完全没有矩阵引擎的命令** —— 而 PLAN §3/§8 明确有 GEMM 引擎与"GEMM 命令自动大矩阵分块"。命令空间必须一次划清,否则以后加 GEMM 命令时又要同时改驱动与 CP。

---

## 4. LAUNCH 命令详细

ISA.md 规定 `LAUNCH {kernel_pc, num_warps, flags}`(ISA §2.2)。CMDS.md 编码为:

```
word0: opcode=0x04, cmd_flags, length_words=5
word1: signal_id[31:16] / wait_id[15:0]
word2: kernel_pc[31:0]      ; SM 指令 SRAM 内的字节偏移,4B 对齐
word3: num_warps[7:0]       ; 1..8
       reserved[31:8]       ; 必须为 0
word4: launch_flags[31:0]   ; 见 §5
```

- **`kernel_pc` 是"指令 SRAM 内的字节偏移",不是全局地址**: ISA §2.2 说内核在启动前已被预加载到 16KB 的指令 SRAM,PC 是字节地址、4B 对齐(ISA §2.2/§4.2),因此 `kernel_pc ∈ [0, image_size − 4]`。v0.1 只写"字节地址,4B 对齐",会被实现成全局地址,而全局地址在 16KB 的 SRAM 里无法译码。
- **命令长度从 4 增到 5**: 因为依赖字(word1)必须有位置(§2.2)。v0.1 的 `length_words=4` 与 §4.3 的"若命令带 `signal_id`"直接矛盾。

### 4.1 LAUNCH 前置条件(按顺序检查,任一失败 ⇒ 置 `CP_ERROR` 并停机,**不得部分执行**)

| # | 条件 | 失败时 |
|---|---|---|
| 1 | 命令长度/保留位符合 §2.4 | `CMD_ERROR` |
| 2 | 涉及的 SM 的 `SM*_ERROR == 0`(host 已清) | `STALE_SM_ERROR` |
| 3 | `1 ≤ num_warps ≤ 8`(ISA §2.2) | `CMD_ERROR` |
| 4 | `kernel_pc` 4B 对齐,且落在已加载镜像的 `[0, image_size)` 内 | `CMD_ERROR` |
| 5 | 所有涉及的 SM 都已加载有效镜像(`SM_KERNEL_MASK` 对应位 = 1) | `CMD_ERROR` |
| 6 | `launch_flags` 与镜像头 `image_flags` **逐位一致** | `CMD_ERROR` |
| 7 | 若 `USES_BARRIER`/`USES_SHARED`/`REQUIRES_SINGLE_SM` 任一为 1,则 `num_warps ≤ 4` | `LAUNCH_REJECT` |
| 8 | 常量区已由驱动或 `PARAMS` 写好(基址固定 `0x0200_0000`) | 无法机械校验,记为驱动契约(§7.2) |
| 9 | 若 `wait_id != 0xFFFF`: 等待信号量满足条件(§10.2) | `TIMEOUT` |

- **第 5 条为什么必须查**: `sm_mask` 允许只加载一个 SM(§6.4);若 `num_warps ≥ 5` 而 SM1 没加载,SM1 的 warp 会从垃圾/旧镜像取指 —— 那是"看着在跑、结果随机"的一类故障,必须在分发前拒绝。
- **第 6 条为什么必须查**: 镜像头是"内核里到底有没有 `bar.sync`"的**权威来源**(ISA D17: 汇编器写入元数据)。驱动若低估(该置位却没置),CP 就不会拦跨 SM 的 barrier,结果是永久挂起。与其相信驱动,不如让 CP 拿两个来源对比,不一致就报错。
- **第 8 条为什么只能算契约**: 常量区是普通全局内存,CP 无法判断里面是不是"本内核的参数"。为把这个契约变成可执行的东西,v0.2 增加 `PARAMS` 命令(§6.10),让参数块随命令流写入 —— 那样顺序天然正确,不再依赖"驱动记得先写参数"。

### 4.2 warp 放置规则

CP 连续打包(ISA §2.2 的硬性规则):

- 先填满 SM0 的 4 个 warp 槽,再填 SM1。
- `num_warps ≤ 4` → 单 SM 常驻(SM0)。
- `num_warps ≥ 5` → 跨 SM: SM0 拿 `wid` 0..3,SM1 拿 4..7。
- SM 内的槽号 = **`wid mod 4`**(SM 内部所有位图/表都按槽号索引,ISA §2.2)。
- **一个 LAUNCH 只启动一个 kernel**,且同一时刻两个 SM 不跑不同 kernel —— 因为每个 SM 只有一块指令 SRAM、一个镜像槽(§6.4)。多内核常驻需要 SRAM 槽字段,列入 §13。

### 4.3 LAUNCH 完成条件(全部满足才算完成)

- 所有涉及 SM 的 `SM_DONE` 位 = 1(即各 SM 的 `warp_alive` 位图全 0,ISA §2.2);
- 所有涉及 SM 的 `SM*_ERROR == 0`;
- 各 warp 的 `st.g` 已落盘(`exit` 携带全局栅栏 + 写缓冲冲刷,ISA §2.5);
- 完成且无错,且 `signal_id != 0xFFFF` ⇒ `SEM[signal_id] += 1`(§10.2);
- **任一 SM 出错** ⇒ 置 `CP_ERROR`、**不写信号量**、置 IRQ(依赖它的命令自然不执行,ISA §2.5);
- **`SM_DONE` 的清除时机**: CP 在**分发** LAUNCH 时清空涉及 SM 的 `warp_alive`/`SM_DONE`(host 侧只读)。v0.1 没有这条 —— 若不清,第二个 kernel 会看到上一个 kernel 遗留的 `SM_DONE = 1`,分发后立刻"完成",信号量提前释放,**2D 引擎会读到还没算完的顶点**。

---

## 5. LAUNCH flags 位定义

`launch_flags` 是 LAUNCH 的 word4,与内核镜像头 `image_flags`(§6.4)一一对应:

| 位 | 名称 | 强制/提示 | 说明 |
|---|---|---|---|
| 0 | USES_BARRIER | **强制** | 内核里出现过 `bar.sync`(ISA §2.2 只强制了这一位)。与 `num_warps ≥ 5` 冲突 ⇒ `LAUNCH_REJECT` |
| 1 | USES_SHARED | **强制** | 共享内存被**跨 warp** 使用(依赖 `bar.sync` 或跨 warp 可见性)⇒ 与 barrier 同规则,要求 `num_warps ≤ 4`;若只当 warp 私有 scratch 用,编译器必须清此位 |
| 2 | USES_GLOBAL_WRITE | 提示 | 供 CP/FENCE 优化(例如确定不写全局内存的内核可跳过全局冲刷);不影响正确性 |
| 3 | REQUIRES_SINGLE_SM | **强制** | 无 barrier 但要求单 SM 常驻的内核(如共享内存做 warp 间缓冲);`num_warps ≥ 5` ⇒ `LAUNCH_REJECT` |
| 4..31 | 保留 | — | 必须为 0,否则 `CMD_ERROR` |

- **为什么把 bit1/bit3 从"提示"升为"强制"**: 共享内存是**每 SM 私有**的 16KB(ISA §3.2)。跨 SM 的两个 warp 用同一段共享地址,会各写各的 SRAM —— 这不会死锁,而是**静默错数据**,比挂起难查得多。既然 CP 只需一个比较器就能拦住,就应该拦(与 ISA D14 的"可诊断优于未定义"同一原则)。
- **契约失效的后果(必须写明)**: 若驱动漏置(内核实际用了 barrier 而 flags=0),CP 不会拦,barrier 会挂起 —— 这时由 `TIMEOUT_CYCLES` 兜底置 `TIMEOUT`,而不是静默错数据。这正是必须有"超时 + `CP_RESET` 恢复路径"的原因(§7、§9)。
- ISA 只强制 bit0,CMDS 增加 bit1/bit3 是**收紧**而非放宽 ⇒ 不违反 ISA,且 ISA 的 D17 已把 flags 的位定义交给 CMDS。

---

## 6. 其他命令格式

以下每条都遵循 §2.2 的两字头(word0 头 + word1 依赖),参数从 word2 开始。

### 6.1 NOP

```
word0: opcode=0x00, cmd_flags, length_words=N (N ≥ 2)
word1: signal_id / wait_id        ; 通常为 0xFFFF
word2..N-1: 0(填充)
```

用途: ① 队列回绕填充(§2.1);② 调试时在命令流里留占位。`NOP` 的 `wait_id` 同样生效(可当"延迟点"用)。

### 6.2 WRITE_REG

```
word0: opcode=0x01, cmd_flags, length_words=4
word1: signal_id / wait_id
word2: addr[31:0]      ; MMIO 地址,必须落在 0x0000_0000..0x0000_0FFF
word3: value[31:0]
```

- 只允许写 R/W 寄存器;写只读寄存器或不存在的地址 ⇒ `CMD_ERROR`。
- 这是驱动初始化 `CMD_QUEUE_BASE`/`CMD_QUEUE_SIZE`/`SEM_BASE`/`FB_BASE` 的手段(也可以由 host 直接写 MMIO,两条路径等价)。

### 6.3 READ_REG

```
word0: opcode=0x02, cmd_flags, length_words=4
word1: signal_id / wait_id
word2: addr[31:0]      ; MMIO 地址
word3: dst_addr[31:0]  ; 全局内存地址,4B 对齐,不得落在 MMIO 窗口
```

读回宽度固定 32b。用于调试: 驱动可以"把 `SM*_ERROR`/`FAULT_PC` 读到内存里再批量取回",避免每个状态都走一次 MMIO 往返。

### 6.4 LOAD_KERNEL 与内核镜像头

```
word0: opcode=0x03, cmd_flags, length_words=5
word1: signal_id / wait_id
word2: src_addr[31:0]  ; 全局内存里的镜像起始地址,4B 对齐
word3: size_bytes[31:0]; 镜像总字节数 ≤ 16384,且为 4 的倍数
word4: sm_mask[1:0]    ; bit0=SM0, bit1=SM1(非 0)
       reserved[31:2]  ; 必须为 0
```

**内核镜像头**(汇编器输出,CP 在 `LOAD_KERNEL` 时校验):

```
偏移 0 : magic       = 0x5347_4B31   ; "SGK1"
偏移 4 : entry_off                    ; 相对镜像起始的字节偏移,4B 对齐(通常 = 32)
偏移 8 : image_size                   ; 字节,4B 倍数,≤ 16384
偏移 12: image_flags                  ; bit0..bit3 与 §5 的 launch_flags 一一对应
偏移 16: code_crc32                   ; 指令区的 CRC-32
偏移 20: code_size                    ; 指令区字节数(= image_size − entry_off)
偏移 24..31: 保留                     ; 必须为 0
```

CP 的处理:

1. 校验 magic、保留字、`size_bytes` 与 `image_size` 一致且合规、`code_crc32` 相符;任一不符 ⇒ `CMD_ERROR`。
2. 把镜像拷贝到 `sm_mask` 指定的每个 SM 的**指令 SRAM 偏移 0 起**(ISA §2.2: 16KB = 4096 条指令)。
3. 记录该 SM 的 `{entry = entry_off, image_flags}` 到内部寄存器组,并置 `SM_KERNEL_MASK` 对应位(§7)。
4. 后续 LAUNCH 使用 `kernel_pc = entry`(或镜像内任意 4B 对齐偏移,§4.1 第 4 条)。

**为什么必须有镜像头**:

- ISA D17 说"flags 由汇编器从内核元数据写入",v0.1 却只让驱动传 `launch_flags` —— 中间缺了"元数据从哪里来、CP 怎么核对"。有了镜像头,链路是闭合的: **汇编器写头 → 驱动原样落到内存 → `LOAD_KERNEL` 校验 → LAUNCH 交叉核对**。
- `magic` + `code_crc32` 把"驱动传错指针""镜像只写了一部分""内存被覆盖"这类故障,从运行时的 `ILLEGAL_INSTR`(难定位)提前成 `LOAD_KERNEL` 时的 `CMD_ERROR`(一眼定位)。
- `entry_off` 独立存在(而不是恒为镜像起始)是为了让"头 + 代码 + 未来可能的常量/调试段"能在一个镜像里共存。

其他规则:

- 指令 SRAM 偏移固定 0 ⇒ **一个 SM 同时只常驻一个内核**(v0.1 未说明)。多内核常驻需要"镜像槽"字段,列入 §13。
- `size_bytes > 16KB` ⇒ `CMD_ERROR`(ISA §2.2 的指令 SRAM 上限;注意这是**静态代码**上限,与内核循环展开后的动态指令数无关)。
- 内核里 `ipdom`/`br.simt` 都是 PC 相对的(ISA §4.2)⇒ 镜像**位置无关**,同一份镜像可加载到任意 SM,这也是 `sm_mask` 能按 SM 独立加载的前提。
- CP `BUSY`(有 kernel 正在跑)时执行 `LOAD_KERNEL` ⇒ `ILLEGAL_STATE`(不能一边取指一边改它脚下的 SRAM)。

### 6.5 FENCE

```
word0: opcode=0x05, cmd_flags, length_words=2
word1: signal_id / wait_id
```

语义(**精确版**): FENCE 完成时保证

1. 此前**所有已分发**的命令(LAUNCH、2D、GEMM、内存搬运)都已到达各自的完成条件;
2. 各 SM 的写缓冲已冲刷、2D 引擎的 ROP 写已完成(§10.3);
3. 上述顺序对 host 可见(host 通过 `CMD_HEAD` 与该命令的 `signal_id` 判定)。

v0.1 只写"等待之前所有命令完成,保证内存可见" —— 没说是"哪些命令""哪一级存储",实现者会各自理解。命令粒度的 FENCE 与指令粒度的 `bar.sync`/`exit` 是两个层次: 后者保证 **SM 内部**的可见性,前者保证**跨引擎**的可见性。

### 6.6 SIGNAL

```
word0: opcode=0x06, cmd_flags, length_words=4
word1: signal_id / wait_id
word2: sem_addr[31:0]  ; 信号量地址,4B 对齐
word3: value[31:0]     ; 写入值
```

写**固定值**(用于驱动初始化与显式事件);"完成计数"式信号量由 §10.2 的 `SEM[id] += 1` 规则负责。

### 6.7 WAIT

```
word0: opcode=0x07, cmd_flags, length_words=4
word1: signal_id / wait_id
word2: sem_addr[31:0]
word3: value[31:0]     ; 等到 SEM >= value
```

**条件是"大于等于"而不是"等于"**(v0.2 修正): 完成计数可能跳过等值点(例如驱动等 3,而 CP 已把同一信号量加到 4),用 `==` 会永久死等。等待超过 `TIMEOUT_CYCLES` ⇒ `TIMEOUT`。

### 6.8 IRQ

```
word0: opcode=0x08, cmd_flags, length_words=3
word1: signal_id / wait_id
word2: irq_id[7:0]     ; ≤ 7,编码见 §7.4
       reserved[31:8]  ; 必须为 0
```

### 6.9 2D 命令(0x10–0x17)

- 参数由 GFX.md 定义;CMDS.md 只规定"CP 按 `length_words` 跳过并转交 2D 引擎",以及依赖关系走 word1。
- 2D 引擎读取顶点输出缓冲前,必须等到对应 LAUNCH 完成(用 `wait_id` 指向该 LAUNCH 的 `signal_id`,见 §10)。
- `2D_SET_REG`(0x13)是 2D 引擎寄存器的唯一合法写入路径(与 `WRITE_REG` 的区别: 它进 2D 引擎的寄存器空间)。

### 6.10 PARAMS(参数块写入常量区)

```
word0: opcode=0x09, cmd_flags, length_words=4+N
word1: signal_id / wait_id
word2: dst_off[15:0]     ; 相对常量区基址的字节偏移,4B 对齐,< 4096
       reserved[31:16]   ; 必须为 0
word3: N[15:0]           ; 参数字数,1 ≤ N ≤ 256
       reserved[31:16]   ; 必须为 0
word4..4+N-1: 参数(32b 字,按序写入 0x0200_0000 + dst_off)
```

- CP 把这 N 个字写进常量区(ISA §3.4 的固定基址 `0x0200_0000`)。
- **为什么需要这条命令**: 常量区基址被 ISA 硬编码(内核用 `lui r9, 0x2000` 直接寻址),所以**同一时刻只能有一个参数块占住 `0x0200_0000`**。若驱动要连续提交两个参数不同的 LAUNCH,它无法"在第一个跑完之前"安全地改参数 —— 除非每次都在主机侧停等(每帧多一次往返)。把参数写进命令流后,CP 按队列顺序执行 `PARAMS → LAUNCH → PARAMS → LAUNCH`,参数天然正确,主机不需要停等。
- 与 ISA 的关系: ISA 说参数走常量内存(§3.4/D8),并没有规定"由谁写"。驱动直接写是合法路径(单内核场景最简单);`PARAMS` 是并流场景的补充路径,两条路径的基址与布局完全一致。

### 6.11 GEMM(矩阵引擎,opcode 0x18)

**为什么 GEMM 放在引擎而不是 shader ISA**: 见 ISA.md D20 —— SIMT 核实测每 MAC 消耗 3.10 个发射槽(2 SM ≈ 1.0 GMAC/s FP32),而 PLAN §8 的 16×16 INT8 脉动阵列可达 51.2 GMAC/s(102 GOPS),差约 50×;而且引擎是自包含模块,不动 ISA 一个字。

命令本体就是 **16 字(64B)的内联描述符**(与 PLAN §4"固定 64B 一条命令"的意图一致,CP 不需要额外指针解引用):

```
word0 : opcode=0x18, cmd_flags, length_words=16
word1 : signal_id / wait_id
word2 : A_addr[31:0]      ; 全局基址, 16B 对齐
word3 : B_addr[31:0]      ; 全局基址, 16B 对齐
word4 : C_addr[31:0]      ; 全局基址, 16B 对齐
word5 : M[15:0] | N[31:16]        ; 1 ≤ M,N ≤ 4096
word6 : K[15:0] | k_tile[31:16]   ; 1 ≤ K ≤ 4096;k_tile=0 ⇒ 引擎自动选
word7 : lda[15:0] | ldb[31:16]    ; 行跨距, 以元素为单位(0 = 紧凑)
word8 : ldc[15:0] | m_tile[31:16] ; ldc=0 ⇒ 紧凑;m_tile=0 ⇒ 自动
word9 : dtype[7:0] | flags[15:8]
        ; dtype[1:0]=A, [3:2]=B, [5:4]=C: 0=INT8, 1=INT16, 2=FP16(预留), 3=保留
        ; flags bit0 = C_ACCUM(C = A×B + C, 而非覆盖)
        ;       bit1 = B_TRANSPOSED(B 按列主序存放)
        ;       bit2 = REQUANT(启用输出重量化, 用 word11/word12)
word10: zp_a[7:0] | zp_b[15:8]    ; 非对称量化的零点;reserved[31:16] 必须为 0
word11: requant_scale[31:0]       ; FP32 位型, 输出重量化乘数
word12: requant_shift[7:0]        ; 右移位数;reserved[31:8] 必须为 0
word13: 保留                      ; 必须为 0(给分块策略留位)
word14: 保留                      ; 必须为 0(给后续量化留位)
word15: 保留                      ; 必须为 0
```

**语义与完成条件**(与 LAUNCH 一致,见 §10.1):

- CP 把描述符交给矩阵引擎;引擎按 `k_tile`/`m_tile` 自行分块、用双缓冲 SRAM 流水(PLAN §8 的 tiling 状态机);
- 完成判据: `GEMM_STATUS.DONE=1` 且 `GEMM_STATUS.BUSY=0` 且 `GEMM_ERROR=0`(§7);
- 完成且无错,且 `signal_id != 0xFFFF` ⇒ `SEM[signal_id] += 1`;出错 ⇒ 置 `CP_ERROR`、**不写信号量**、置 IRQ(依赖它的命令不执行);
- **上游依赖**: `wait_id` 可以指向某个 LAUNCH 的 `signal_id` —— 这就是 PLAN §9"多引擎并发 + 信号量依赖"的落地形式(例如"顶点算完再跑 GEMM");
- 引擎与 shader 核之间**没有缓存一致性**(PLAN §1 明确排除),跨引擎可见性全靠"完成 + `FENCE`"(§10.3);
- 引擎忙时再发 GEMM ⇒ 排队(CP in-order,不会同时跑两个 GEMM);`CP_RESET` 必须同时复位引擎(§7.3)。

**v1 的范围与留白**: dtype 先只实现 INT8(A/B)→ INT32(C)(`dtype=0`);FP16 与输出重量化按 PLAN §8 的"留升级接口"逐步补。引擎自身的寄存器块(PE 阵列规模、tile 深度、性能计数器)与 `word13..15` 的用途随 Phase 5 一起定 —— 见 §13。
---

## 7. MMIO 寄存器映射

**基址 `0x0000_0000`**(按 PLAN §3 的内存映射;窗口大小 4KB)。v0.1 写的"建议 `0x4000_0000`"与 PLAN 冲突,已按 PLAN 更正。

| 偏移 | 名称 | 读写 | 说明 |
|---|---|---|---|
| 0x00 | CP_STATUS | R | bit0 IDLE,bit1 BUSY,bit2 ERROR |
| 0x04 | CP_ERROR | R / 写任意值清零 | 错误码,见 §8.1 |
| 0x08 | CMD_HEAD | R(host 写仅用于异常恢复) | CP 已处理的命令位置 |
| 0x0C | CMD_TAIL | R/W | 驱动写入的命令边界 |
| 0x10 | DOORBELL | W | 写任意值提示 CP 取命令 |
| 0x14 | IRQ_STATUS | R / 写任意值清零 | 中断状态,位定义见 §7.4 |
| 0x18 | IRQ_ENABLE | R/W | 中断使能,位定义同 IRQ_STATUS |
| 0x1C | SM_DONE | R | bit0=SM0_DONE,bit1=SM1_DONE(§7.1) |
| 0x20 | SM0_ERROR | R / 写任意值清零 | SM0 错误码,见 §8.2 |
| 0x24 | SM1_ERROR | R / 写任意值清零 | SM1 错误码,见 §8.2 |
| 0x28 | SEM_BASE | R/W | 信号量区基址(§10.2) |
| 0x2C | CMD_QUEUE_BASE | R/W | 命令队列基址,默认 `0x0100_0000` |
| 0x30 | CMD_QUEUE_SIZE | R/W | 命令队列字节数 |
| 0x34 | CONST_BASE | R | 常量区基址,固定 `0x0200_0000`(ISA §3.4) |
| 0x38 | FB_BASE | R/W | 帧缓冲基址,复位值 `0x1000_0000`(**可配置**,PLAN §3) |
| 0x3C | DDR_BASE | R | DDR 基址,固定 `0x8000_0000` |
| 0x40 | CP_RESET | W | 写 1: 复位 CP 状态机与全部错误/中断状态(§7.3) |
| 0x44 | TIMEOUT_CYCLES | R/W | 等待超时门限,复位值 `0x10_0000`(≈5ms @200MHz) |
| 0x48 | SM_KERNEL_MASK | R | bit0/bit1: 对应 SM 的指令 SRAM 里有有效镜像(§6.4) |
| 0x4C | SM0_FAULT_PC | R | SM0 出错时的 PC(定位到具体指令) |
| 0x50 | SM1_FAULT_PC | R | SM1 出错时的 PC |
| 0x54–0x5F | 保留 | — | 未来扩展 |
| 0x60 | GEMM_STATUS | R | bit0 BUSY,bit1 DONE,bit2 ERROR(引擎完成判据见 §6.11) |
| 0x64 | GEMM_ERROR | R / 写任意值清零 | 引擎错误码(枚举,取值随 Phase 5 定) |
| 0x68–0xFF | 保留 | — | 引擎寄存器块(PE 规模/计数器)待 Phase 5 |

**错误/中断寄存器的访问属性是"值编码 + 写任意值清零",不是 W1C 位图**(v0.2 修正): v0.1 把 `CP_ERROR`/`SM*_ERROR` 标成 `R/W1C`,但它们的取值是枚举(`1=CMD_ERROR`、`2=LAUNCH_REJECT`…)。按位图语义"写 1 清 bit0"会把寄存器**又写回 1**,自相矛盾。现在的规则: 寄存器存放**首个错误的值**(§8.3),软件写任意值把它清零。

### 7.1 SM_DONE 位定义

| 位 | 名称 | 说明 |
|---|---|---|
| 0 | SM0_DONE | SM0 的 `warp_alive` 位图全 0(ISA §2.2) |
| 1 | SM1_DONE | SM1 的 `warp_alive` 位图全 0 |
| 2..31 | 保留 | 0 |

- 语义是"**该 SM 当前没有活跃 warp**",不等于"某个 kernel 完成了"。所谓"kernel 完成"由 CP 内部状态(已分发 + 涉及 SM 全 DONE + 无错误)判定,不要只看这一位。
- **清除时机**: CP 在分发 LAUNCH 时清 `warp_alive`/`SM_DONE`(§4.3);host 只读。

### 7.2 内存映射总表(与 PLAN/ISA 对齐)

| 地址范围 | 用途 | 来源 |
|---|---|---|
| `0x0000_0000` – `0x0000_0FFF` | CP / 各引擎 MMIO(本表 §7) | PLAN §3 |
| `0x0100_0000` | 命令队列(环形,`CMD_QUEUE_BASE` 默认值) | PLAN §3 |
| `0x0200_0000` – `0x0200_0FFF` | **常量区**: 内核参数块。基址被 ISA 硬编码(`lui r9, 0x2000` + 小偏移) | ISA §3.4、PLAN §3 |
| `0x0200_1000` – `0x0200_7FFF` | **内核镜像区**: `LOAD_KERNEL` 的 `src_addr` 取这里(每个镜像 ≤ 16KB) | 本文档定义 |
| `0x1000_0000` | 帧缓冲(基址可配置: `FB_BASE`) | PLAN §3 |
| `0x8000_0000` | 外部 DDR(上板) | PLAN §3 |
| 其他 | 由驱动分配(顶点缓冲、纹理、信号量区…) | 驱动契约 |

- PLAN §3 把 `0x0200_0000` 写成"内核代码 + 常量内存",而 ISA §3.4 把常量区固定在 `0x0200_0000` 的**最低处**(内核从基址 +0 开始读参数)。两者只有靠**子布局**才能同时成立: 常量块占前 4KB,镜像从 `+0x1000` 起。
- **常量区的并发约束**: 因为内核用立即数硬编码了基址,`0x0200_0000` 同一时刻只能承载**一个**内核的参数块。顺序执行的 LAUNCH 靠 `PARAMS` 或驱动写保证正确;若将来要并发多个 shader kernel,需要 ISA 侧提供"参数块基址"(例如 LAUNCH 时写入一个基址寄存器),列入 §13。

### 7.3 复位与恢复(`CP_RESET`,偏移 0x40)

写 1 后:

1. CP 状态机回到 `IDLE`;
2. 清 `CP_ERROR`、`IRQ_STATUS`,并把 `CMD_HEAD` 置为 `CMD_TAIL`(丢弃未处理命令);
3. 清各 SM 的 `SM*_ERROR`、`SM*_FAULT_PC`,并清空 `warp_alive`(于是 `SM_DONE` 变为"无活跃 warp");
4. 清 `SM_KERNEL_MASK` —— **镜像视为失效**,驱动必须重新 `LOAD_KERNEL` 才能 LAUNCH;
5. **复位矩阵引擎**(清 `GEMM_STATUS.BUSY`、`GEMM_ERROR`,丢弃未完成的 GEMM),§6.11;
6. 保持 `CMD_QUEUE_*`、`SEM_BASE`、`FB_BASE` 等配置寄存器不变(方便重新提交)。

- 恢复流程固定为: `CP_RESET` → 读 `SM*_FAULT_PC`/`SM*_ERROR` 定位 → `LOAD_KERNEL` → 重新提交命令。
- **为什么必须有它**: barrier 挂起(flags 漏置)、信号量等不到、SM 停机都会让 CP 卡在 `WAIT`;没有软件复位路径,一次实验就只能重启仿真/断电。超时门限 `TIMEOUT_CYCLES` 与 `CP_RESET` 是一对: 前者保证"卡住会被发现",后者保证"发现后能继续"。

### 7.4 中断编号

| irq_id / 位 | 名称 | 触发条件 |
|---|---|---|
| 0 | IRQ_CP_ERROR | `CP_ERROR` 被置位 |
| 1 | IRQ_CMD_DONE | 命令带 `IRQ_ON_DONE`(§2.3) |
| 2 | IRQ_LAUNCH_DONE | 某个 LAUNCH 无错完成 |
| 3 | IRQ_2D_DONE | 某个 2D 命令完成 |
| 4 | IRQ_GEMM_DONE | 某个矩阵引擎命令完成 |
| 5–7 | 保留 | 未来扩展 |

`IRQ_ENABLE` 的位与该表一一对应;`IRQ_STATUS` 记录已发生的中断(写任意值清零)。

---

## 8. 错误码定义

### 8.1 CP_ERROR 编码(`CP_ERROR` 取值)

| 值 | 名称 | 触发条件 |
|---|---|---|
| 0 | NO_ERROR | 无错误 |
| 1 | CMD_ERROR | 命令长度/保留位错、镜像头非法、CRC 不符、参数越界(§2.4、§4.1、§6.4) |
| 2 | LAUNCH_REJECT | `USES_BARRIER`/`USES_SHARED`/`REQUIRES_SINGLE_SM` 与 `num_warps ≥ 5` 冲突(§5) |
| 3 | BUS_ERROR | AXI 总线错误(命令队列读取、镜像加载、MMIO 访问失败) |
| 4 | TIMEOUT | 等待信号量或 `SM_DONE` 超过 `TIMEOUT_CYCLES` |
| 5 | ILLEGAL_STATE | CP 状态机非法转移(如 kernel 运行中做 `LOAD_KERNEL`) |
| 6 | STALE_SM_ERROR | LAUNCH 时涉及的 SM 仍有未清除的错误码(§4.1 第 2 条) |
| 7–15 | 保留 | 未来扩展 |

- 字段宽度: 寄存器是 32 位、**取值编码(枚举)而不是位图**;上表 0..15 是全部已定义/保留取值,其余位恒 0。
- v0.1 的"本文档给出 4 位编码,待 SPEC 确认是否用位图"(§13 第 7 条)在此定死为**枚举**: CP 是停机语义,一次只需要记住"第一个错误";位图只会让"多个错误同时发生"的语义复杂化。

### 8.2 SM 错误码编码(`SM0_ERROR` / `SM1_ERROR`)

每 SM 一个错误码寄存器,与 ISA §2.6 逐条对应:

| 值 | 名称 | 触发条件(ISA §2.6) |
|---|---|---|
| 0 | NO_ERROR | 无错误 |
| 1 | ILLEGAL_INSTR | 未定义 opcode / 保留字段非 0 / 非法 funct 组合 |
| 2 | SIMT_OVF | 压栈时栈深已达 8 |
| 3 | BAD_ALIGN | ld/st 地址 `[1:0] != 0` |
| 4 | BAR_DIVERGENT | `bar.sync` 时 `active_mask != 8'hFF` 或 SIMT 栈非空 |
| 5 | ERR_NO_RECOV | 发散时 recov 有效位为 0 |
| 6–15 | 保留 | 未来扩展 |

- ISA §2.6 表里的 `LAUNCH_REJECT` **不进这张表**: 它由 CP 在分发前判定(§4.1 第 7 条),永远不会写进 SM 寄存器。v0.1 把它列在 SM 错误码表里(还加了"CP 侧拒绝,不写 SM 错误寄存器"的注释)是自相矛盾的 —— 参考模型若按表实现,会去 SM 寄存器里找一个永远不存在的值。
- 同一 SM 上的 `SM*_FAULT_PC`(§7)记录置错时的 PC,用于把错误定位到具体指令。

### 8.3 错误处理流程

1. 错误发生时: 该 SM 停止取指(ISA §2.6),错误码写入 `SM*_ERROR`,`FAULT_PC` 记录现场;CP 置 `CP_ERROR`(若尚未置位)、置 IRQ,进入 `ERROR` 态。
2. **首个错误保留**: 寄存器在软件清除前不被后续错误覆盖 —— 否则真正的根因会被后续的连锁错误(超时、总线错误)冲掉,调试变成猜谜。
3. 处于错误态的 kernel: 视为失败,**不写信号量**(依赖它的 2D/GEMM 命令不会执行),ISA §2.5 的"错误状态不算完成"由此落地。
4. host 处理: 读 `SM*_ERROR` + `SM*_FAULT_PC` + `CP_ERROR` → 修问题 → 写 `CP_RESET` → 重新 `LOAD_KERNEL` → 重新提交。

---

## 9. CP 状态机与复位

```
        ┌─────────┐   CP_RESET(任意状态可进)
        │  IDLE   │◄───────────────────────────┐
        └────┬────┘                            │
             │ DOORBELL 且 HEAD != TAIL        │
             ▼                                 │
        ┌─────────┐                            │
        │  FETCH  │ 从命令队列取命令            │
        └────┬────┘                            │
             ▼                                 │
        ┌─────────┐                            │
        │ DECODE  │ 校验 §2.4 的长度/保留位     │
        └────┬────┘                            │
             ▼                                 │
        ┌─────────┐                            │
        │DISPATCH │ 分发到 SM / 2D / 矩阵 / 内存│
        └────┬────┘                            │
             ▼                                 │
        ┌─────────┐                            │
        │  WAIT   │ 等 SM_DONE / 2D 完成 / 信号量│
        └────┬────┘   超时 → ERROR             │
             ▼                                 │
        ┌─────────┐   写信号量、更新 CMD_HEAD  │
        │ UPDATE  │────────────────────────────┘
        └────┬────┘
             │ HEAD == TAIL → IDLE
             ▼
        ┌─────────┐
        │  ERROR  │ 停机: 置 CP_ERROR/IRQ,不写信号量
        └────┬────┘
             │ host 写 CP_RESET
             └──────► IDLE
```

- **顺序执行**,除非命令带 `wait_id`(向前等待)或队列里显式插入了同步命令(`FENCE`/`SIGNAL`/`WAIT`)。CP 不做乱序、不做抢占 —— 这是 PLAN 的"in-order 分发"决策。
- `WAIT` 态的三种出口: 条件满足 → `UPDATE`;超时 → `ERROR`;SM 报错 → `ERROR`。
- `ERROR` 态不自动恢复(与 ISA 的"停机而不是尽力而为"同一原则),只能由 host 写 `CP_RESET` 离开。
- `DOORBELL` 只是"提示",不是计数: CP 每次被触发都从 `CMD_HEAD` 一直取到 `CMD_TAIL`,期间新到的命令在下一轮取。

---

## 10. 完成与同步

### 10.1 LAUNCH 完成

- CP 等所有涉及 SM 的 `SM_DONE` 位为 1(§7.1);
- 若任一 SM 的 `SM*_ERROR != 0` ⇒ 置 `CP_ERROR`,**不写信号量**,置 IRQ;
- 完成且无错: 若 `signal_id != 0xFFFF` ⇒ `SEM[signal_id] += 1`;
- 只有当上述判定结束时,CP 才更新 `CMD_HEAD` 并释放后续命令 —— 依赖该 kernel 的 2D 命令因此不会提前执行(ISA §2.5)。

### 10.2 信号量

- 信号量位于全局内存,槽位地址 = `SEM_BASE + id * 4`,`id ∈ 0..255`;命令头里的 `0xFFFF` 表示"无依赖"(§2.2),`0x0100..0xFFFE` 保留。
- 语义分两种,不要混用:
  - **完成计数**(由命令的 `signal_id` 触发): 命令完成时 CP 执行 `SEM[id] += 1`;
  - **显式赋值**(`SIGNAL` 命令): 把指定地址写成指定值(驱动初始化/复位时用)。
- **等待条件是 `>= value`**(§6.7): 完成计数会跳过等值点,`==` 会死等。
- **为什么不需要 atomics**: ISA 明确没有原子指令(D11),但信号量的读-改-写**只由 CP 单点完成**(SM 与 2D 引擎只读),所以命令级同步不受影响。这也是把"信号量写"放在 CP 而不是放在 shader 内核里的原因 —— 内核若要在结束时写信号量,就必须有 atomics。
- 驱动若要"等 kernel K 完成",标准做法是给 K 的 LAUNCH 一个 `signal_id`,再给下游命令一个 `wait_id`;或直接用 `WAIT` 命令等待。

### 10.3 内存可见性(两个层次,别混淆)

| 层次 | 机制 | 保证内容 |
|---|---|---|
| 指令级(SM 内) | `bar.sync` | 该 warp 之前的 ld/st(共享与全局)对放行后的 lane 可见(ISA §2.4) |
| 指令级(全局) | `exit` | 该 warp 之前的 `st.g` 在 exit 后全局可见(写缓冲冲刷,ISA §2.5) |
| 命令级(跨引擎) | LAUNCH 完成 + `FENCE` | 所有已分发命令到达完成条件、写缓冲冲刷完毕(§6.5) |

- **CP 等到 `SM_DONE` 且无错误之后,才允许 2D 引擎读取顶点输出缓冲** —— 这是"顶点不丢"的唯一依据(不能只看"指令发射完毕",ISA §2.5)。
- 2D 引擎与 shader 核之间没有缓存一致性协议(PLAN 明确排除),所以跨引擎的可见性完全靠"完成 + FENCE"这两条规则。

---

## 11. 与 ISA.md 的接口

| ISA 要求 | ISA 位置 | CMDS.md 实现 |
|---|---|---|
| `LAUNCH {kernel_pc, num_warps, flags}` | §2.2 | §4(word2/word3/word4) |
| `flags[0]=USES_BARRIER`(来自内核元数据) | §2.2、附录 A | §5 bit0、§6.4 镜像头 bit0 |
| barrier 与 `num_warps ≥ 5` 冲突 ⇒ 拒绝 | §2.2、D17 | §4.1 第 7 条、§8.1 值 2 |
| CP 连续打包 warp;槽号 = `wid mod 4` | §2.2 | §4.2 |
| CP 预加载内核到指令 SRAM(16KB/4096 条) | §2.2 | §6.4 |
| 内核参数走常量内存 `0x0200_0000` | §3.4、D8 | §7.2、§6.10 `PARAMS` |
| 每 SM `warp_alive` → `sm_done` | §2.2、§2.5 | §7.1 |
| CP 等所有涉及 SM 的 `sm_done` | §2.5 | §10.1 |
| 错误码写 MMIO、可读可复位 | §2.6 | §7、§8 |
| 错误态不算完成、不释放信号量 | §2.5 | §4.3、§8.3、§10.1 |
| `exit` 冲刷写缓冲后才算完成 | §2.5、§8 | §10.3 |
| 内核元数据 `{entry, uses_barrier}` | 附录 A、D17 | §6.4 镜像头 + §4.1 第 6 条 |
| 2D 引擎依赖 kernel 完成信号 | §2.5 | §10.2、§12 |

---

## 12. 与 GFX.md 的接口

- 2D 命令 opcode `0x10–0x17` 已分配,参数与语义由 GFX.md 定义;CMDS.md 只负责校验、收尾与分发。
- 2D 引擎读取顶点输出缓冲前,必须等待对应 LAUNCH 完成(`wait_id` 指向该 LAUNCH 的 `signal_id`)。
- 2D 引擎的寄存器映射(边方程系数、纹理、混合模式等)由 GFX.md 定义;`2D_SET_REG`(0x13)是唯一写入路径。
- 帧缓冲基址/尺寸/格式由 `FB_BASE` 与 GFX.md 共同描述(PLAN §3 说"基址/尺寸/格式可配置",故 `FB_BASE` 在本文档是 R/W)。

---

## 13. 待定项

v0.1 的 8 条待定项里,**5 条已在 v0.2 定死**(MMIO 基址、队列格式、错误码编码方式、LAUNCH flags 其余位、`sm_mask` 语义)。剩下的都是**真正依赖其它文档或 ISA 扩展**的:

| # | 待定项 | 依赖 | v0.2 的处置 |
|---|---|---|---|
| 1 | 2D 命令参数与寄存器映射 | GFX.md(未创建) | 只分配 opcode;参数表待 GFX.md |
| 2 | GEMM 描述符里 `word13..15` 的用途、引擎寄存器块与性能计数器 | PLAN §8(Phase 5) | 命令格式与完成语义已在 §6.11 定死;`0x18` 已分配,`0x19–0x1F` 保留 |
| 3 | **多个 shader kernel 并发**: 参数块基址 + 指令 SRAM 槽位 | 需要 ISA 扩展(参数块基址寄存器 / 镜像槽) | 当前约束写在 §7.2、§6.4;扩展需求登记在此 |
| 4 | MMIO 基址与窗口大小的最终确认 | SPEC.md(空) | 已按 PLAN §3 定为 `0x0000_0000`;若 SPEC 改动需同步 |
| 5 | "固定 64B 命令" vs 变长命令 | PLAN §4 的决策 | 见 附录 A: 格式保持变长(自描述),驱动可把每条命令填充到 64B |
| 6 | `code_crc32` 的多项式与初值 | 实现细节 | 建议 CRC-32/ISO-HDLC(与 `zlib.crc32` 一致),便于 Python 参考模型核对 |
| 7 | 中断线的消费方式(轮询 vs 真中断) | 平台(仿真/AXI-Lite) | `IRQ_STATUS`/`IRQ_ENABLE` 已足够;是否引物理中断线待定 |
| 8 | 信号量区大小与槽位分配策略 | 驱动约定 | 接口已定(`SEM_BASE + id*4`,`id ≤ 255`);分配策略属驱动 |

---

## 14. 总结

- **命令格式**: 32 位字序列;两字通用头(word0 = 长度/标志/opcode,word1 = 依赖关系),参数从 word2 起。§2.4 的校验表是 driver/RTL/参考模型三方共同契约。
- **LAUNCH**: `{kernel_pc, num_warps, flags}`,九条前置检查(§4.1)把 ISA 的三条硬约束(barrier 与 warp 数、连续打包、镜像已加载)全部变成可拒绝的条件。
- **LOAD_KERNEL + 内核镜像头**: 把 ISA D17 的"元数据 → flags"链路闭合;magic/CRC 让加载错误在加载时就暴露。
- **MMIO**: CP 状态/错误/中断、`SM_DONE`/`SM_ERROR`/`FAULT_PC`、队列与信号量配置;错误寄存器是值编码、写任意值清零;`CP_RESET` 提供软件恢复路径。
- **错误**: CP 错误与 SM 错误分离,首个错误保留,错误态停机且不释放信号量;`SM*_FAULT_PC` 定位到指令。
- **同步**: 完成计数式信号量(CP 单点读-改-写,故 ISA 无 atomics 也成立)+ 两层内存可见性(`bar.sync`/`exit` 管 SM 内,LAUNCH 完成/`FENCE` 管跨引擎)。

下一步: ① `SPEC.md` 目前是空文件,MMIO 基址、时钟/复位策略等需要它落定;② GFX.md 落定 2D 命令参数后,§6.9 与 §12 才能补完;③ 建议先按 §2.4/§8 的表写 CP 的 Python 参考模型(比 RTL 便宜得多),再用它验证驱动。

---

## 附录 A: 跨文档冲突与处置

| # | 冲突 | 各方说法 | 处置 |
|---|---|---|---|
| 1 | MMIO 基址 | PLAN §3: `0x0000_0000`;CMDS v0.1 §7: 建议 `0x4000_0000` | 按 PLAN 采用 `0x0000_0000`(§7);若 SPEC 另有决定再同步 |
| 2 | 命令长度 | PLAN §4: "命令: 固定 64B 一条";CMDS v0.1: 变长 `length_words` | **保留变长格式**(自描述、便于扩展),但要求驱动把每条命令填充到 64B 边界 ⇒ 环指针运算仍然简单,PLAN 的简化意图得以保留 |
| 3 | 帧缓冲基址 | PLAN §3: "基址/尺寸/格式可配置";CMDS v0.1: `FB_BASE` 固定只读 `0x1000_0000` | `FB_BASE` 改为 R/W,复位值 `0x1000_0000`(§7) |
| 4 | `0x0200_0000` 区用途 | PLAN §3: "内核代码 + 常量内存";ISA §3.4: 常量区基址固定 `0x0200_0000` | 定义子布局: 常量块占前 4KB,镜像自 `+0x1000` 起(§7.2) |
| 5 | 矩阵引擎命令 | PLAN §3/§8: 有 GEMM 引擎与分块命令;CMDS v0.1: 无 GEMM opcode | 预留 `0x18–0x1F`(§3) |
| 6 | 顶点格式 | PLAN §4: `{x, y(s16.15), r, g, b, a, u, v}`;ISA §7.2 的示例内核用 `6×f32 = 24B` | 属 GFX.md/ISA 的范畴,已在 ISA §7.2 标注;CMDS 侧只影响顶点缓冲的地址与大小计算,命令接口不变 |
| 7 | SPEC.md / GFX.md 不存在 | 两份文档被多处引用 | 本文档把所有"推给它们"的项集中到 §13,并标明当前默认值,避免实现停摆 |

---

## 附录 B: 变更记录

### v0.2 → v0.3

| # | 类别 | 变更 | 理由 |
|---|---|---|---|
| 24 | 命令·新增 | 新增 §6.11 **GEMM 命令**(opcode 0x18,16 字内联描述符: M/N/K/stride/dtype/零点/重量化 + `C_ACCUM`、`B_TRANSPOSED` 标志)与完成语义(完成且无错 ⇒ `SEM[id] += 1`;出错 ⇒ 不释放信号量) | `0x18–0x1F` 早已预留但参数未定;ISA.md D20 把"GEMM 走独立引擎"定了下来,命令接口必须跟上,否则决策落不了地。内联 16 字描述符同时满足 PLAN §4"固定 64B 一条命令" |
| 25 | 可观测性 | §7 补 `GEMM_STATUS`(0x60)/`GEMM_ERROR`(0x64);§7.3 的 `CP_RESET` 增加"复位矩阵引擎";§13 第 2 条改为记录剩余待定项 | 完成语义必须有一个可观测的位置,否则又是"引用了却不存在"这一类缺陷(本文件 v0.2 已修过两次同类问题) |

优化出发点与 ISA.md v0.2 一致: **每条命令的长度、保留位、错误条件都必须能被 CP 的 RTL 与参考模型用同一张表判定**;凡是"引用了却不存在的字段""自相矛盾的属性""跨文档冲突"都必须消灭。

| # | 类别 | 变更 | 理由 |
|---|---|---|---|
| 1 | 正确性·结构 | 通用命令头增加统一依赖字 word1(`signal_id`/`wait_id`) | v0.1 的 §4.3/§6.9/§10.1 都引用"命令带 `signal_id`"/"2D 命令可带 `wait_id`",但**编码里根本没有这两个字段** —— 依赖关系无处安放 |
| 2 | 正确性·结构 | LAUNCH 从 4 字改为 5 字;`kernel_pc` 明确为**指令 SRAM 内的字节偏移** | ① 要给依赖字腾位置(v0.1 的 4 字与 §4.3 矛盾);② "字节地址"会被实现成全局地址,而在 16KB 指令 SRAM 里无法译码 |
| 3 | 正确性·接口 | 新增内核镜像头(magic / entry_off / image_size / image_flags / code_crc32 / code_size),`LOAD_KERNEL` 负责校验 | ISA D17 说"flags 由汇编器从内核元数据写入",v0.1 只让驱动传 flags,中间缺一环;CRC 把"传错指针/镜像不完整"从运行时的 `ILLEGAL_INSTR` 提前成加载时的 `CMD_ERROR` |
| 4 | 安全性 | `launch_flags` 的 bit1(`USES_SHARED`)、bit3(`REQUIRES_SINGLE_SM`)从"提示位,CP 可忽略"升为**强制** | 共享内存是每 SM 私有的 16KB(ISA §3.2): 跨 SM 用同一段共享地址会**静默错数据**,比死锁更难查;CP 只需一个比较器就能拦 |
| 5 | 正确性 | LAUNCH 前置条件 5 条 → 9 条(新增: SM 错误码已清、镜像已加载、`kernel_pc` 在镜像范围内、flags 与镜像头一致、`wait_id` 超时) | 每一条都对应一类"能跑但结果错/静默挂起"的故障;其中 flags 交叉校验针对"驱动低估 barrier"这一最危险的漏报 |
| 6 | 正确性 | 明确 CP 在**分发时**清 `warp_alive`/`SM_DONE`;`SM_DONE` 语义写明为"该 SM 无活跃 warp" | 不清的话,第二个 kernel 会看到上一个 kernel 遗留的 `SM_DONE = 1`,立刻"完成"并提前释放信号量,2D 引擎会读到未算完的顶点 |
| 7 | 一致性 | MMIO 基址 `0x4000_0000` → `0x0000_0000` | 与 PLAN §3 的内存映射冲突(附录 A 第 1 条) |
| 8 | 正确性 | 错误/中断寄存器属性 `R/W1C` → "值编码 + 写任意值清零" | v0.1 的编码是枚举(1=CMD_ERROR…),按 W1C 位图语义"写 1 清 bit0"会把寄存器又写回 1,自相矛盾 |
| 9 | 正确性 | SM 错误码表删除 `LAUNCH_REJECT` 行 | 该码由 CP 判定,**永远不会写进 SM 寄存器**;参考模型按 v0.1 的表实现会去找一个不存在的值 |
| 10 | 可诊断 | 新增 `SM*_FAULT_PC`;`CP_ERROR` 增 `STALE_SM_ERROR`(6);明确"首个错误保留" | 没有出错 PC,"ILLEGAL_INSTR/SIMT_OVF" 只能靠猜;不保留首个错误,根因会被后续连锁错误冲掉 |
| 11 | 可用性 | 新增 `CP_RESET`(0x40)、`TIMEOUT_CYCLES`(0x44) | barrier 挂起(flags 漏报)、信号量等不到都会让 CP 卡死;没有软件复位路径,一次实验就得重启仿真。超时负责"发现",复位负责"继续" |
| 12 | 正确性 | `WAIT` 条件从"等于"改为"大于等于" | 完成计数会跳过等值点,`==` 会永久死等 |
| 13 | 语义·补齐 | 写明信号量语义: 完成计数 `SEM[id] += 1` 与显式赋值两条路径;并说明"CP 单点读-改-写 ⇒ ISA 无 atomics 也成立" | v0.1 只说"写信号量/等待等于",没说谁写、写什么值;而这条语义正是 2D 引擎依赖 kernel 完成的依据 |
| 14 | 能力·补齐 | 新增 `PARAMS`(0x09)命令 | 常量区基址被 ISA 硬编码 ⇒ 同一时刻只能有一个参数块;v0.1 下"连续两次参数不同的 LAUNCH"必须靠主机停等。`PARAMS` 把参数写进命令流,顺序天然正确 |
| 15 | 正确性 | 命令队列补回绕规则与 `NOP` 的填充用途;补 `DOORBELL` 语义(提示而非计数) | 变长命令不得跨越队列末尾,否则 CP 会读到拼接出来的假命令 |
| 16 | 完整性 | 新增 §2.3 `cmd_flags` 位定义 | v0.1 的命令头有 `flags` 字节却全文未定义 —— 保留字段不定义,实现者就各写一个含义 |
| 17 | 可验证 | 新增 §2.4 命令校验表、§4.1 前置条件表、§7.4 中断编号表 | 把"长度错/保留位非 0"从形容词变成可逐条判定的表;driver、RTL、参考模型共用 |
| 18 | 一致性 | `FB_BASE` 由"固定只读"改为 R/W(复位值 `0x1000_0000`) | PLAN §3 明确帧缓冲"基址/尺寸/格式可配置" |
| 19 | 完整性 | 命令表补 `0x0A–0x0F` 保留、2D 扩到 `0x10–0x17`、**新增矩阵引擎 `0x18–0x1F`** | v0.1 从 0x08 直接跳到 0x10,且完全没有 GEMM 命令,而 PLAN §3/§8 有矩阵引擎与分块命令 |
| 20 | 结构 | 新增 §7.2 内存映射总表(含常量区/镜像区子布局) | PLAN 的"`0x0200_0000` = 内核代码 + 常量内存"与 ISA 的"常量区固定 `0x0200_0000`"需要子布局才能同时成立 |
| 21 | 结构 | `LOAD_KERNEL` 明确"加载到指令 SRAM 偏移 0"、"镜像位置无关"、"BUSY 时拒绝";并登记"多内核常驻需要 SRAM 槽" | v0.1 没说加载到哪、能否多内核常驻 —— 这是实现时必须回答的问题 |
| 22 | 文档·结构 | 新增目录、验证标准、ISA 委托清单、附录 A(跨文档冲突)、本变更记录;待定项从 8 条收敛为 8 条但**定死其中 5 条** | SPEC.md 当前为空、GFX.md 未创建;能自己定的就不要再挂"待定",真正外部的项集中列出并给默认值 |
| 23 | 一致性 | §12 明确 `2D_SET_REG` 为 2D 引擎寄存器的唯一写入路径;`FB_BASE` 与 GFX.md 的分工写明 | 避免出现"两条路径写同一寄存器"的歧义 |

**本轮优化的取舍原则(后续修改本文档时建议沿用):**

1. **引用了就必须存在**: 每个被引用的字段/寄存器/错误码都要有定义位置(第 1、9、16 条);
2. **契约要能机器校验**: 镜像头 + CRC + flags 交叉校验,把"驱动说对了"变成"CP 能验证"(第 3、5 条);
3. **静默错数据优先于挂起被消除**: 跨 SM 用共享内存、`SM_DONE` 残留都属于"不报错但结果错",必须用检查或清零消灭(第 4、6 条);
4. **必须有恢复路径**: 任何会卡住的等待都要有超时,任何错误态都要有软件复位(第 11 条);
5. **跨文档冲突集中登记**: 冲突不消灭就会变成实现者的猜测(附录 A)。
