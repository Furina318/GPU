# CP —— 命令处理器(Command Processor)

> 本文描述 **hw/rtl/cp 下真实存在的 RTL**。上一版文档是按"cp_fsm / cmd_fetch /
> kernel_loader / sem_unit / launch_unit"这种拆分写的，与实际代码不符（实际只有一个
> 大状态机 + 一个寄存器文件 + 一个小校验器 + 一个 CRC 单元）。这一版逐条对照源码，
> 文中 `cp.sv:NNN` 之类的引用都可以直接跳过去看。
>
> 命令语义与 MMIO 偏移的权威定义仍以 `docs/CMDS.md` 为准；本文只负责回答"RTL 是
> 怎么把它做出来的"。

---

## 1. CP 是什么

整颗 GPU 只有一个启动入口：**CP**。SM 不会自己去取指，2D/GEMM 引擎不会自己去干活，
它们都在等 CP 发放那个一拍的脉冲。

```
        host(驱动)                        CP                        其它单元
   ┌──────────────┐   AXI4-Lite   ┌────────────────┐   launch   ┌──────────┐
   │ 写命令环      │ ────────────► │ 读命令环        │ ─────────► │ SM0/SM1  │
   │ 敲 DOORBELL  │  (MMIO 4KB)   │ 校验 / 执行     │            └──────────┘
   │ 读 CMD_HEAD  │ ◄──────────── │ 推进 HEAD       │   payload  ┌──────────┐
   │ 等中断 / 信号量│               │ 加信号量 / 中断  │ ─────────► │ 2D/GEMM  │
   └──────────────┘               └────────────────┘            └──────────┘
                                        │ AXI4 主口(经 cp_mem_adapter)
                                        ▼
                                    全局内存(命令环/常量区/信号量/镜像)
```

### 1.1 它做什么

| 类别 | 具体职责 |
|------|----------|
| 取指 | 从命令环（全局内存里的一段环形缓冲）按 HEAD 逐条取命令，一次一条 |
| 校验 | 长度 / 保留位 / 对齐 / 地址窗口 / 各 opcode 的语义约束（`cmd_decode.sv`） |
| 执行 | 自己动手：NOP/FENCE/READ_REG/WRITE_REG/SIGNAL/WAIT/IRQ/PARAMS；或是分发：LAUNCH / GEMM / 2D |
| 装镜像 | `LOAD_KERNEL` 把 kernel 二进制从内存流式搬进 SM 指令 SRAM，边搬边算 CRC32 |
| 同步 | `signal_id` 使 SEM[id]+=1；`wait_id` 在执行前检查 SEM[wait_id]!=0 |
| 记账 | 推进 `CMD_HEAD`、置 `IRQ_STATUS` 位、亮 `irq_o`、记录首个 `CP_ERROR` |
| 超时 | 每个等待都有 `TIMEOUT_CYCLES` 上限，超了就报 `CPERR_TIMEOUT` 停机 |

### 1.2 它不做什么

- **不做地址翻译**：命令里的地址都是物理地址，直接送内存口。
- **不排队**：一次只有一条命令在飞，没有命令队列硬件、没有乱序、没有多发射。
- **不解释 kernel 语义**：只校验 `kernel_pc` 是否落在镜像内、flags 是否与镜像头一致，
  kernel 具体干什么它不知道也不关心。
- **不做原子操作**：信号量是"CP 单点读-改-写"，因为它自己是唯一的访问者。
- **不校验引擎参数**：GEMM/2D 的操作数是否合理由引擎自己判，CP 只做结构性检查
  （长度/保留位/维度范围）。

### 1.3 三条硬规则

所有 18 个状态都围着这三条转：

1. **in-order，一次只做一条。** `HEAD` 只在"这条命令确认完成"的那一拍前进
   （`cp.sv:480` 的 `head_we`）。命令失败则 HEAD 原地不动 + 停机，于是 host 读
   `CMD_HEAD` 就能知道卡在哪条命令上 —— 这是唯一的调试锚点。
2. **`wait_id` 在"校验之后、执行/分发之前"查**（`cp.sv:647` 的 `ST_DECODE`）。
   不是"取命令前"，也不是"执行中"。
3. **任何等待都有上限。** `TIMEOUT_CYCLES`（默认 `0x0010_0000`）数到就报
   `CPERR_TIMEOUT` + 停机，不允许无限等下去。

---

## 2. 命令环与取指模型

### 2.1 命令格式

命令在内存里按 32bit 字连续排布：

| 字 | 内容 |
|----|------|
| word0 | `length_words[31:16]`、`cmd_flags[15:8]`、`opcode[7:0]` |
| word1 | `signal_id[31:16]`、`wait_id[15:0]` |
| word2… | 各 opcode 的操作数（`cmd_decode.sv` 逐条校验） |

`length_words` 是**整条命令的字数（含 word0/word1）**。两个信号量字段用
`0xFFFF`（`cp_pkg::SEM_NONE`）表示"没有"。

命令体最多只有前 16 字会进 `cmd_w[0:15]`；`PARAMS` 这种最长 260 字的命令，
超出 16 字的部分由 `ST_PARAMS` 直接按全局地址从行缓存取（`cp.sv:816`）。

### 2.2 队列指针

```
 queue_base                                  queue_base + queue_size
    │                                                    │
    ▼                                                    ▼
    ┌────┬────┬────┬────┬────┬────┬────┬────┬────┬────┐    环形缓冲
         ▲                             ▲
         │                             │
       HEAD                          TAIL
  (CP 已完成到这里)              (host 已写到这里)
   已消费 / 可回填               已发布 / 未消费
    head == tail  ⇒  队列空
```

- `HEAD` 由 CP 推进；`TAIL` 由 host 推进；两者相等 ⇒ 队列空。
- host 写 `DOORBELL` 只是"敲一下"，真正的判据是 `head != tail`
  （`cp.sv:223` 的 `queued`）。空队列敲门时 CP 顺手把 `doorbell_pend` 消费掉。
- `CMD_HEAD` / `CMD_TAIL` 同时可读可写；host 写 `HEAD` 可以在 `[base, base+size)`
  内任意搬（用于把队列搬回已消费区域），越界或未对齐的写被静默丢弃。

### 2.3 两级取指：命令缓存 + 64B 行缓冲

命令体是连续的字，一条命令常常要读 5～16 个字。取指分两级：

**① 命令缓存 `hw/rtl/cache/cmd_cache.sv`（跨命令驻留）**

- 64 组 × 64B 行 = 4KB，正好覆盖整个默认命令环（`CMD_QUEUE_SIZE = 4096`），
  直接映射，标签/有效位/epoch 放 `tagv_sram`，16 个字位各放一个 `data_bank_sram`
  （与 `~/LoongArch32/myloong/core3/icache.v` 同一套 SRAM 划分）。
- CP 要一整行时给 `cc_req + cc_req_line`；**命中 2 拍**（accept + 标签/数据同拍
  读出）。缺失时向内存发一次读，回来的数据按 AXI R 的节奏**一拍一个字**直接写进
  对应字位的 bank（16 拍）。
- **回填的最后一拍顺带发 SRAM 读**：那一拍 16 个 bank 里只有"最后一个字"所在的那个
  在做写（SRAM 同拍读写不绕回），其余 15 个 bank 读出来就是新数据；最后一个字单独
  记进 `last_word_q`，下一拍拼成整行回给 CP。所以缺失只比命中多"16 拍数据 + 1 拍
  回话"，没有额外的读回状态。
- **预取在缓存内部**：每次真实请求拿到行 L 之后，缓存自己挂上"预取 L+1"，
  下一次空闲就去读（只挂一次、不链式，被真实请求抢占也不会丢）。上界由
  `ring_end_line = (queue_base + queue_size) >> 6` 给出，环外的行不预取。
  `tail` 之后还没发布的区域**允许**读进来：host 每次写 `TAIL`/`DOORBELL`
  都会整片失效，不会读到半条命令。
- 失效用 epoch：`CP_RESET`、host 写 `DOORBELL`、host 写 `CMD_HEAD` /
  `CMD_TAIL` / `CMD_QUEUE_BASE` / `CMD_QUEUE_SIZE`，以及复位，都让 epoch+1
  （一拍作废全部行，不逐项清）。理由见 §2.2：这几种事件之后环里的字节才可能变。

**② 64B 行缓冲 `line_buf`（生命周期 = 一条命令）**

- `ST_MEM/OP_RD_LINE` 从命令缓存拿回 16 个字塞进 `line_buf[0:15]`，记
  `line_base` 并置 `line_valid`；同一行内的字直接命中（`line_hit`）。
- `ST_UPD_FIN` 里**无条件**清 `line_valid`：命令之间的可见性由命令缓存负责，
  行缓冲只管"这一条命令的 16 个字"，这样两条命令之间不会残留任何状态。
- 命令跨 64B 边界时退化成对命令缓存的又一次行请求（语义与单字读相同）。
- CP 自己的单字读写与命令缓存共用 CP 的内存口：单字读只回一拍（与行读同一条
  `rd_vld/rd_word/rd_last` 通路），单字写仍是 req → ack → 完成应答。

**失效与正确性**：命令环只有一个写者（host），它的发布顺序永远是"先写命令
字节、再写 `CMD_TAIL` / `DOORBELL`"。因此只要这几种事件都让缓存失效，CP 就
不会读到旧命令 —— 即使驱动把 `HEAD` 搬回已消费区域重写那里也一样。

### 2.4 三处长度/边界判定

在 `ST_LOADW` 取到 word1 的那一拍（`cp.sv:598` 起），一次判完三件事：

| 条件 | 含义 | 错误 |
|------|------|------|
| `len_q < 2` | 任何命令至少 2 字 | `CPERR_CMD` |
| `next_head_raw > queue_base + queue_size` | 命令不许跨环末尾 | `CPERR_CMD` |
| `tail >= head && next_head_raw > tail` | 不许读 host 还没写的区域 | `CPERR_CMD` |

`next_head_raw = head + len*4` 是"未回绕值"，回绕只在 `next_head_raw ==
queue_base + queue_size` 时发生，即**恰好等于末尾才允许回绕**：驱动要用 NOP 把
队列末尾填平，不能指望 CP 自动折返。

### 2.5 HEAD 什么时候动

只有一处：`head_we = (state == ST_UPD_FIN) & (sig_rd_sent | ~sig_ok_q)`
（`cp.sv:480`）。做成组合脉冲而不是写进某个分支，因为它必须和"清行缓存"落在
同一拍上。`sig_rd_sent` 负责区分信号量 RMW 的两趟（见 §8.2）。

---

## 3. 对外接口

### 3.1 六组端口（`cp.sv:45` 起）

| 组 | 信号 | 说明 |
|----|------|------|
| host | `s_awvalid/awready/awaddr`、`s_wvalid/wready/wdata/wstrb`、`s_bvalid/bready/bresp`、`s_arvalid/arready/araddr`、`s_rvalid/rready/rdata/rresp` | AXI4-Lite 从口，4KB MMIO 窗口 |
| 中断 | `irq` | 电平中断，= `irq_status & irq_enable` 的按位或归约 |
| 内存 | `mem_rd_{req,addr,line,ack,vld,word,last,err}`、`mem_wr_{req,addr,data,strb,ack,rsp,err}` | 读:req/ack + 按拍送字;写:req/ack/rsp |
| SM | `sm_launch_{valid,pc,warps,flags}`、`sm_done`、`sm0/1_{error,fault_pc}` | 分发脉冲 + 完成/错误回读 |
| SRAM | `sram_load_{en[1:0],addr[12:0],data,last[1:0]}`、`sram_load_done[1:0]`、`sram_err_ovf[1:0]` | 镜像预加载广播 |
| 引擎 | `eng_valid/engine/opcode/len/ready`、`eng_w{valid,data,last}`、`gemm/2d_{status,error}` | 结构化 payload 流 |

### 3.2 内存口时序（CP 侧视角）

下游 `cp_mem_adapter.sv` 把它翻成 AXI4。对 CP 来说只有三个时刻：

读是"请求 / 接受 / 按拍送字"三段，写是"请求 / 接受 / 落地应答"三段：

```
      t0      t1      t2                    t2+15      t2+16
req   ▔▔▔▔▔▔▔▔▔▔▔╲____________________（保持到 ack）
addr  ▔▔▔▔▔▔▔▔▔▔▔▔（全程稳定）
ack   __________▔▔▔╲__________________   单拍，表示"请求被接受"
vld   ________________▔▔▔▔▔▔▔▔▔▔▔▔▔▔╲______   行读连续 16 拍，字读 1 拍
word  ________________══════════════════════   与 vld 同拍的一个 32 位字
last  ____________________________▔▔▔╲______   最后一拍
err   ____________________________▔▔▔╲______   与 last 同拍（总线错误）
```

- CP 一次只有一个内存在飞，所以 `ST_MEM` 里一个 `req_sent` 位就够管握手；
- `cp_ret_state` 记住"做完回哪个态"，一个状态复用给读行/读字/写字/信号量读写
  等全部 7 类访问；
- 读 64B 行在 adapter 里是两段 8 拍 INCR burst（`arlen=7`），但**对 CP 与命令缓存
  都是一条 16 拍的 beat 流**——行内第几个字就是第几拍，中间不攒 512 位行缓冲；
  读 4B 字只有一拍（与行读同一条 `vld/word/last` 通路）。

### 3.3 MMIO 访问

`axil_slave.sv` 是每通道单事务在途的简单从口：等 `aw`+`w` 齐了打一拍 `reg_wr`
脉冲，`ar` 来了打一拍 `reg_rd`。响应固定 `OKAY`；地址高位不为 0（`awaddr[31:12]`）
的访问被当作 bad，**静默丢弃且不回 SLVERR** —— 驱动不能靠 SLVERR 发现写错地址。

---

## 4. 文件划分（实际存在的）

| 文件 | 行数 | 职责 |
|------|------|------|
| `cp.sv` | 1128 | 18 态主状态机 + 全部组合握手；本文的主角 |
| `cp_regs.sv` | 262 | 唯一一份寄存器副本；ext（AXI-Lite）与 int（状态机）两个访问口 |
| `cmd_decode.sv` | 181 | 纯组合校验器：长度/保留位/对齐/窗口/opcode 语义 |
| `axil_slave.sv` | 96 | AXI4-Lite → 单拍 `reg_wr` / `reg_rd` |
| `crc32_word.sv` | 33 | 每拍 4 字节的 CRC32（poly `0xEDB88320`，小端），与 zlib 一致 |
| `cp_pkg.sv` | 69 | 错误码、引擎编号、长度常量、flag 位、IRQ 位 |
| `cp_mem_adapter.sv` | 183 | （在 `hw/rtl/mem/`）req/ack ↔ AXI4 主口；读按 beat 流回数据，64B 行 = 两段 burst |
| `cache/cmd_cache.sv` | 295 | （在 `hw/rtl/cache/`）命令缓存，见 §2.3 |
| `cache/cache_sram.sv` | 92 | （在 `hw/rtl/cache/`）`tagv_sram` + `data_bank_sram` 基础件 |

数据流是单向的、没有环：`cp.sv` 读 `cp_regs` 的配置、把校验结果写回 `cp_regs`；
`cmd_decode` 只吃 `cp.sv` 的组合输出并吐 `ok/err`。

---

## 5. 状态机

### 5.1 18 个状态（`cp.sv:150`）

| 组 | 状态 | 干什么 |
|----|------|--------|
| 取 | `ST_IDLE` | 等 DOORBELL / 队列非空。空队列敲门时只 ack，不前进 |
| 取 | `ST_LOADW` | 取第 `idx` 个字；`idx=0` 抽 word0，`idx=1` 抽 word1 并做三判（§2.4） |
| 取 | `ST_LOADW2` | 补齐命令体到 `min(len,16)` 字，然后锁出 `w2/w3/w4` |
| 取 | `ST_DECODE` | 跑 `cmd_decode` + 各命令前置条件，决定进 `ST_EXEC` 还是先轮询 |
| 执行 | `ST_EXEC` | 按 opcode 分派；本身几乎不占时序，只设参数和下一个态 |
| 执行 | `ST_MEM` | 唯一的内存执行器：读行 / 读字 / 写字，完成回 `cp_ret_state` |
| 执行 | `ST_PARAMS` | 逐字写常量区；发起写时 `idx` 就自增，直接回自己 |
| 执行 | `ST_LOADK` | 逐字读镜像 → 写指令 SRAM（按 `sm_mask` 广播）+ 喂 CRC |
| 执行 | `ST_LOADK_ND` | 等 SRAM 装完，校验镜像头 + CRC；全过才置 `SM_KERNEL_MASK` |
| 执行 | `ST_LAUNCH` | 一拍分发（`sm_launch_valid` 由组合逻辑直接给出） |
| 执行 | `ST_WAIT_SM` | 等涉及的 SM 全部 DONE；任一报错 ⇒ `SM_FAULT`，且不写信号量 |
| 执行 | `ST_ENG_SEND` | 先推引擎头，再逐字推 payload（`eng_ready` 背压） |
| 执行 | `ST_WAIT_ENG` | 等引擎 `DONE && !BUSY` |
| 执行 | `ST_POLL` | 发一次目标字读（信号量 / 任意同步地址） |
| 执行 | `ST_POLL_WAIT` | 判读回值，并用 `poll_gap` 做 8 拍节流 |
| 提交 | `ST_UPD_FIN` | 推进 HEAD、清行缓存、置 IRQ；要加信号量时先去 `ST_SEM_WR` |
| 提交 | `ST_SEM_WR` | `SEM[id] += 1`（读-改-写由 CP 单点完成） |
| 错误 | `ST_ERR` | 锁首个错误后停机，只认 `CP_RESET` |

### 5.2 主干

```
取命令    ST_IDLE ──► ST_LOADW ──► ST_LOADW2 ──► ST_DECODE
                       ▲  ▲                        │
                       └──┴── 行未命中: 读一次 64B 行, 回到 ST_LOADW/ST_LOADW2
                                                   │ ok 且 wait_id 已满足
                                                   ▼
执行      ST_EXEC 按 opcode 分到下面六条支路之一:
            ├─ 内存类   ST_MEM            (读行 / 读字 / 写字, 完成回 cp_ret_state)
            ├─ 常量区   ST_PARAMS         (N 次写, 发起即 idx++, 直接回自己)
            ├─ 装镜像   ST_LOADK → ST_LOADK_ND
            ├─ 跑 kernel ST_LAUNCH → ST_WAIT_SM
            ├─ 引擎     ST_ENG_SEND → ST_WAIT_ENG
            └─ 轮询     ST_POLL ⇄ ST_POLL_WAIT

提交      ST_UPD_FIN
            ├─ 第一次进来且带 signal_id ──► ST_MEM(读 SEM) ──► ST_SEM_WR ──► 回 ST_UPD_FIN
            ├─ 收尾那一拍: HEAD 前进 + 清行缓存 + 按需置 CMD_DONE
            ├─ 还有命令 ──► ST_LOADW
            └─ 队列空   ──► ST_IDLE

错误      任何状态遇错 ──► ST_ERR (锁首错、停机、只认 CP_RESET)
```

### 5.3 各命令的状态路径

| 命令 | 路径 |
|------|------|
| NOP / FENCE | `EXEC → UPD_FIN` |
| WRITE_REG | `EXEC`（置 `int_wr`）`→ UPD_FIN` |
| READ_REG | `EXEC → MEM`（写字到全局内存）`→ UPD_FIN` |
| SIGNAL | `EXEC → MEM`（写 value）`→ UPD_FIN` |
| WAIT | `EXEC → POLL ⇄ POLL_WAIT → UPD_FIN` |
| IRQ | `EXEC → UPD_FIN` |
| PARAMS | `EXEC → PARAMS ×N → UPD_FIN` |
| LOAD_KERNEL | `EXEC → LOADK ×N → LOADK_ND → UPD_FIN` |
| LAUNCH | `EXEC → LAUNCH → WAIT_SM → UPD_FIN` |
| GEMM / 2D_* / 0x15-0x17 | `EXEC → ENG_SEND ×N → WAIT_ENG → UPD_FIN` |
| 厂商自定义 0x80-0xFF | `EXEC → UPD_FIN`（按 `length_words` 直接跳过） |

带 `signal_id` 的命令在 `UPD_FIN` 里多两趟：`→ MEM`（读 SEM）`→ SEM_WR`（写回）`→ UPD_FIN`。

---

## 6. 命令逐条走查

### 6.1 NOP（0x00）

长度任意（≥2）。`cmd_flags` 保留位必须为 0；`signal_id` 若给则也要加信号量。
用途：**填充队列末尾**，让上一条命令恰好停在 `queue_base + queue_size` 上（§2.4）。
行命中时一条 2 字 NOP 是 7 拍（IDLE / LOADW×2 / LOADW2 / DECODE / EXEC / UPD_FIN），
每多一个字 +1 拍。

### 6.2 WRITE_REG（0x01）

`w2 = addr`，`w3 = data`。走内部写口，**同拍生效**（`int_rdata` 是组合读，所以
紧接着的 READ_REG 能读到刚写的值）。除 `cmd_decode` 的通用检查外，`ST_DECODE`
还有两道额外校验（组合门槛在 `cp.sv:284` 起，判断在 `ST_DECODE` 里）：

- `CP_QUEUE_BASE` / `CP_QUEUE_SIZE`：值必须合法（`>= MMIO_END` 且 4B 对齐 /
  `>= 8` 且 8 的倍数），否则 `CPERR_CMD`；
- 且时机必须是**本命令是队列里最后一条 且 没有 kernel 在跑**，否则
  `CPERR_ILLEGAL_STATE`。理由：改队列基址会让 `HEAD`/`TAIL` 一起跳到新基址，
  后面还有命令在排队就会读到垃圾。

注意这条命令**不能**写 `CMD_HEAD`、`SM_DONE`、`SM_KERNEL_MASK`、`FAULT_PC`、
`CONST_BASE`、`DDR_BASE`、`CP_STATUS`（不在 `cp_regs` 的白名单里 ⇒ `CPERR_CMD`）。
host 侧直写不受这个白名单限制（只有 `QUEUE_*` 受 `idle_i` 门控）。

### 6.3 READ_REG（0x02）

`w2 = 寄存器地址`（< `MMIO_END`），`w3 = 目标地址`（>= `MMIO_END`，4B 对齐）。
读是组合的，写是一次单字内存写。这是 host 以外唯一能把 CP 寄存器值带到内存的途径，
常用于自检。

### 6.4 SIGNAL（0x06）

`w2 = 地址`，`w3 = 值`。语义就是"往全局内存指定地址写一个值"，与信号量单元
（`SEM[id] += 1`）无关 —— 后者是每条命令按 `signal_id` 自动做的。

### 6.5 WAIT（0x07）

`w2 = 地址`，`w3 = 达标值`。判据是 `*addr >= value`（无符号）。地址要求
`>= MMIO_END`（不在 MMIO 窗口里，`cmd_decode.sv:109`），这点旧文档漏了。
它是真等待，不是"轮询一次就过"。

### 6.6 IRQ（0x08）

`w2[7:0] = irq_id`（0..7），直接 `IRQ_STATUS[irq_id] = 1`。与 `cmd_flags[1]`
（`CFLAG_IRQ_ON_DONE`）的区别：后者是"这条命令完成时自动置 `CMD_DONE`(bit1)"，
IRQ 命令是"显式置任意一位"。

### 6.7 PARAMS（0x09）

`w2[15:0] = dst_off`，`w3[15:0] = N`，随后 N 个数据字。逐个写到
`CONST_BASE + dst_off + i*4`。N ≤ 256、整条长度必须等于 `4+N`、
`dst_off + N*4 <= 4096`。

实现上 `ST_PARAMS`（`cp.sv:816`）"发起一次写就 `idx++`，然后回自己"，省掉一趟
回程态。注意它按需从行缓存补字，所以**不要求** N ≤ 12（`cmd_w` 只装得下
16 字，超出部分是直接从内存取的）。

### 6.8 FENCE（0x05）

长度必须 2，几乎空转。因为 CP 本身 in-order 且每条命令都等到完成才推进，
FENCE 在单 CP 模型下没有额外语义，保留它是为了与 `docs/CMDS.md` 对齐。

### 6.9 LOAD_KERNEL（0x03）

见 §9。

### 6.10 LAUNCH（0x04）

见 §10。

### 6.11 GEMM（0x18）/ 2D（0x10-0x17）

见 §11。

### 6.12 厂商自定义（0x80-0xFF）

`cmd_decode` 只要求 `len >= 2` 和通用头合法，然后 CP 按长度整条跳过。
这是给 host 塞"元数据/调试记录"用的后门。

---

## 7. 校验规则（`cmd_decode.sv` 全表）

### 7.1 通用头（所有命令）

| 检查 | 错误 |
|------|------|
| `length_words >= 2` | `CPERR_CMD` |
| `cmd_flags[7:2] == 0`（bit0/1 是 SYNC / IRQ_ON_DONE，不在这里查） | `CPERR_CMD` |
| `signal_id == 0xFFFF` 或 `< 0x0100` | `CPERR_CMD` |
| `wait_id` 同上 | `CPERR_CMD` |

### 7.2 逐 opcode

| opcode | 长度 | 附加约束 |
|--------|------|----------|
| NOP 0x00 | ≥2 | — |
| WRITE_REG 0x01 | 4 | `w2[1:0]==0 && w2 < MMIO_END` |
| READ_REG 0x02 | 4 | 同上；且 `w3[1:0]==0 && w3 >= MMIO_END` |
| LOAD_KERNEL 0x03 | 5 | `w4[31:2]==0`、`w4[1:0]!=0`、`w2[1:0]==0`、`w3!=0 && w3[1:0]==0 && w3<=16KB` |
| LAUNCH 0x04 | 5 | `w3[31:8]==0`、`1 <= w3[7:0] <= 8` |
| FENCE 0x05 | 2 | — |
| SIGNAL 0x06 | 4 | `w2[1:0]==0 && w2 >= MMIO_END` |
| WAIT 0x07 | 4 | 同 SIGNAL（`w2` 是地址） |
| IRQ 0x08 | 3 | `w2[31:8]==0`、`w2[7:0] <= 7` |
| PARAMS 0x09 | 4+N | `w2[31:16]==0 && w3[31:16]==0`、`w2[1:0]==0 && w2[15:0]<4096`、`1<=N<=256`、`len==4+N`、`w2[15:0]+N*4<=4096` |
| GEMM 0x18 | 16 | 见 §7.3 |
| 2D_FILL 0x10 | 6 | — |
| 2D_BLIT 0x11 | 9 | — |
| 2D_DRAW 0x12 | 8 | — |
| 2D_SET_REG 0x13 | 4 | — |
| 2D_FLUSH 0x14 | 2 | — |
| 0x15/0x16/0x17 | ≥2 | 只查通用头，转交 2D 引擎（参数由引擎报 `BAD_PARAM`） |
| 0x0A-0x0F、0x19-0x7F | — | `CPERR_CMD`（保留） |
| 0x80-0xFF | ≥2 | 合法，跳过 |

### 7.3 GEMM 的结构性检查

```
len == 16
(w2 | w3 | w4) & 0xF == 0          // word2..4 的低 4 位保留，必须为 0
1 <= M,N,K <= 4096
w10[31:16]==0 && w12[31:8]==0 && w13..w15==0
dtype == 0                          // v1 只支持 INT8
gflags[2] == 0                      // REQUANT 未实现
gflags[7:3] == 0                    // 保留
lda != 0 → lda >= k
ldb != 0 → ldb >= (transB ? k : n)
ldc != 0 → ldc >= n
```

这之后 CP 就撒手了：真正的参数合法性（例如 A/B 是否会越界）由 GEMM 引擎自己判，
错误通过 `gemm_error` 回读，`ST_WAIT_ENG` 看到 `ERROR` 位就报 `CPERR_ENGINE_FAULT`。

### 7.4 `ST_DECODE` 里额外的两道

1. `WRITE_REG` 写队列配置 → 值与时机（§6.2）；
2. `LOAD_KERNEL` 时若 `kernel_active_q`（有 kernel 在跑）→ `CPERR_ILLEGAL_STATE`：
   正在跑的 kernel 依赖 SRAM 里的代码，不能边跑边覆盖。

---

## 8. 依赖与完成语义

### 8.1 `wait_id`

`ST_DECODE` 里若 `wait_id != SEM_NONE`：清 `poll_gap`、`timeout_cnt`，把
`poll_addr` 指向 `sem_base + wait_id*4`、`poll_val = 1`，进 `ST_POLL`。
判据是 `SEM[wait_id] >= 1`，即"被依赖的命令至少完成了一次"。

由于 CP 是 in-order 的，`wait_id` 正常情况下不会阻塞（依赖的命令一定在前面已经做完）；
它真正派上用场的是：**另一个 CP/另一个队列**、或 host 在两条命令之间手工清 `SEM`、
或 SM 侧的"等我写完这里"式同步。对超时路径来说，它是唯一能让 CP 在无事可做时
也保持可观测的机制。

### 8.2 `signal_id` 与信号量 RMW

一条完成态的命令若带 `signal_id`，`ST_UPD_FIN` 会分成两趟：

```
第 1 趟：发 OP_RD_WORD 读 SEM[id] → 走 ST_MEM，结果进 scratch，回 ST_SEM_WR
第 2 趟：写 scratch+1 回去 → 走 ST_MEM，回到 ST_UPD_FIN
         （此时 head_we 才真正拉高，HEAD 前进、清行缓存）
```

不做原子操作是安全的：只有 CP 会碰信号量（SM 侧只写自己的完成标志，不碰 SEM），
所以"读-改-写"不存在竞争。`sig_rd_sent` 就是这个两趟的分相标志。

`SEM_NONE`（`0xFFFF`）表示"这条命令不加信号量"。

### 8.3 WAIT 的轮询节流

`ST_POLL` 发一次读，`ST_POLL_WAIT` 判值：

- 值 `>= poll_val` ⇒ 成功，清零超时计数，按 `poll_dst_upd` 去 `ST_EXEC`（WAIT 命令）
  或 `ST_UPD_FIN`（`wait_id` 路径）；
- 不满足 ⇒ `poll_gap = 8`；
- 之后每个周期递减，到 0 再回 `ST_POLL` 重读。

重读周期约 `8 + 一轮单字读握手（3～4 拍）≈ 10～12 拍`。节流的意义是别让 CP 拿
轮询把内存总线占满（同一条总线上还有 SM 在取指/访存）。

### 8.4 超时

`timeout_cnt` 在 `ST_WAIT_SM` / `ST_WAIT_ENG` / `ST_POLL_WAIT` 里自增，
`timeout_hit = (timeout_cnt >= timeout_cycles)`，默认 1,048,576 拍。
超时 ⇒ `CPERR_TIMEOUT` + `ST_ERR`。

---

## 9. LOAD_KERNEL 的数据通路

### 9.1 命令

`w2 = src`（源地址，4B 对齐）、`w3 = size_bytes`（≤ 16KB，4B 倍数，非 0）、
`w4[1:0] = sm_mask`（装给哪些 SM）。

### 9.2 镜像头（前 32B = 8 字）

| 字 | 含义 | RTL 校验 |
|----|------|----------|
| w0 | magic | `== 0x5347_4B31`（"SGK1"） |
| w1 | `entry_off` | 4B 对齐、`>= 32`、`<= size` |
| w2 | `size_bytes` | `== w3`（命令里的 size） |
| w3 | `flags` | 不校验；`LAUNCH` 时用来和命令的 `kernel_flags` 逐位比对 |
| w4 | `crc32` | 必须等于流式计算值 |
| w5 | `payload_bytes` | `== size - entry_off` |
| w6/w7 | 保留 | `== 0` |

### 9.3 流式装载

`ST_LOADK`（`cp.sv:848`）逐字：

1. 从行缓存（必要时读内存）取第 `idx` 个源字；
2. 写指令 SRAM：`sram_load_en = {2{sram_en_q}} & load_sm_mask`、
   `addr = idx`、`last = (idx == words-1)`，两个 SM 各一份独立口；
3. 前 8 字顺手存进 `img_hdr[0:7]`；
4. 从 `idx*4 >= entry_off` 开始把字喂给 `crc32_word`（`crc_en`），
   所以 CRC 只覆盖**指令区**，不含镜像头；
5. `idx` 走到 `words` 后进 `ST_LOADK_ND`。

`ST_LOADK_ND`（`cp.sv:872`）：

- `|sram_err_ovf` ⇒ `CPERR_CMD`（写超过 16KB）；
- 等 `|(sram_load_done & load_sm_mask)` 全高；
- 校验 §9.2 全部字段 + CRC；
- 全过 ⇒ `img_we = 1`，寄存器文件里置 `SM_KERNEL_MASK |= sm_mask` 并记下
  `entry_off/size/flags`。

失败则 `CPERR_CMD` 停机 —— 注意此时 SRAM 里可能已经写了一半垃圾，但
`SM_KERNEL_MASK` 没置，`LAUNCH` 会被拒绝，所以是安全的。

---

## 10. LAUNCH 与 SM 协作

### 10.1 九条前置

第 0 条在 `cmd_decode.sv`，第 1～6 条在 `cp.sv:264` 的 `launch_check` 里
（下面列的顺序就是代码里的判断顺序），第 7～8 条不是 RTL 检查项：

0. 长度 5、`w3[31:8]==0`、`1<=num_warps<=8`（`cmd_decode`）；
1. **涉及 SM 的 `SM*_ERROR == 0`**（`CPERR_STALE_SM_ERROR`）—— 必须最先判；
2. `kernel_pc[1:0]==0`；
3. `kernel_pc < img_size`（按目标 SM 逐个查）；
4. `SM_KERNEL_MASK & sm_mask == sm_mask`（涉及的 SM 都装过镜像）；
5. `kernel_flags[3:0]` 与镜像头 flags 逐位一致；
6. 若 `kernel_flags` 的 `USES_BARRIER / USES_SHARED / REQUIRES_SINGLE_SM` 任一为 1，
   而 `num_warps > 4`（会跨 SM）⇒ `CPERR_LAUNCH_REJECT`；
7. 下面几条是驱动契约、RTL 不校验：常量区已由 `PARAMS` 装好、SM0 的参数区指针
   已写对（SM 从固定地址 0 读）、要写的 buffer 已在常量区；
8. `wait_id` 已在 `ST_DECODE` 查过。

顺序不能交换：kernel 死在前一个错误上时，再去讨论 `num_warps`/`pc`/`flags`
只会掩盖真正的原因。

### 10.2 分发

`sm_sel = (num_warps > 4) ? 2'b11 : 2'b01`（`cp.sv:296`）：

- `num_warps ∈ [1,4]`：只给 SM0，SM0 拿全部 `num_warps`；
- `num_warps ∈ [5,8]`：两个 SM 各拿 4 个，warp 0-3 落 SM0、4-7 落 SM1。

`ST_LAUNCH` 那一拍 `sm_launch_valid = sm_sel_q`（组合），SM 侧 `fetch.sv:168`
就在这一拍把 `pc ← launch_pc`。CP 只发 `num_warps`，**不**重复 ISA §2.2 的
"warp 槽位"放置逻辑（那是 SM 自己的事，`core.sv:366` 里
`launch && (W < launch_warps)`）。

### 10.3 完成

- `SM_DONE` 由分发那一拍负责清零（`launch` 脉冲承担），否则第二个 kernel 会
  立刻"完成"；
- `(sm_done & sm_sel_q) == sm_sel_q` ⇒ 涉及的 SM 全 DONE ⇒ `ev_launch_done`、
  恢复 `kernel_active_q=0`，进收尾；
- 任一涉及 SM 报错 ⇒ `CPERR_SM_FAULT` 停机，**不写信号量** —— host 靠
  "SEM 不增长 + `CP_ERROR` 有值"区分"失败"与"还在跑"。

---

## 11. 引擎路径（GEMM / 2D）

`ST_ENG_SEND`（`cp.sv:938`）是两段式：

```
eng_valid  ▔╲________                        命令头一拍
eng_wvalid ______▔▔▔▔▔▔▔▔▔▔╲__               payload 逐字，eng_ready 背压
eng_wlast  ______________▔╲___               最后一个字
eng_ready  ▔▔▔▔▔╲▔▔╲▔▔╲▔▔╲▔▔               CP 只在 ready 时前进
```

- payload 长度 = `length_words - 2`，数据来自 `cmd_w[idx+2]`；
- `2D_FLUSH` 这类 payload 为 0 的命令：头推完直接进 `ST_WAIT_ENG`；
- `ST_WAIT_ENG` 等 `status[DONE] && !status[BUSY]`；`status[ERROR]` ⇒
  `CPERR_ENGINE_FAULT`。错误码在 `gemm_error` / `2d_error` 里，CP 不解释。

引擎忙（`eng_ready=0`）时 CP 就停在这里 —— 这是背压，不是丢命令。

---

## 12. MMIO 寄存器表

基址 `0x0000_0000`，窗口 4KB。属性列含义：`R` 可读、`W` 任一访问口都能写、
`命令W` 命令与 host 都能写、`host W` **只有 host 直写有效**（命令写能过白名单但
不产生效果，见 §15.2 第 5 条）。

| 偏移 | 名称 | 属性 | 复位值 | 说明 |
|------|------|------|--------|------|
| 0x00 | `CP_STATUS` | R | — | `bit0=IDLE`、`bit1=BUSY`、`bit2=ERROR`（`cp_regs.sv:122`） |
| 0x04 | `CP_ERROR` | R/命令W/host W | 0 | 8 位错误码；**写任意值清零**；首错保留 |
| 0x08 | `CMD_HEAD` | R/host W | `QUEUE_BASE_DEF` | CP 推进；host 写需在 `[base,base+size)` 且 4B 对齐 |
| 0x0c | `CMD_TAIL` | R/W | `QUEUE_BASE_DEF` | host 推进发布命令 |
| 0x10 | `DOORBELL` | host W | — | 写任意值置 pending（读恒 0）；命令路径无效，见 §15.2 |
| 0x14 | `IRQ_STATUS` | R/命令W/host W | 0 | 5 位在用；写任意值清零 |
| 0x18 | `IRQ_ENABLE` | R/W | 0 | 中断使能；`irq` 是 status 与 enable 相与后的归约或 |
| 0x1c | `SM_DONE` | R | — | 每 SM 一位 |
| 0x20 | `SM0_ERROR` | R/host W | — | 写即清（数据忽略，`sm_err_clr[0]`） |
| 0x24 | `SM1_ERROR` | R/host W | — | 写即清（数据忽略，`sm_err_clr[1]`） |
| 0x28 | `SEM_BASE` | R/W | `0x0300_0000` | 信号量数组基址 |
| 0x2c | `CMD_QUEUE_BASE` | R/命令W/host W | `0x0100_0000` | 写合法值同时把 `HEAD=TAIL=新值`；host 写受 `idle_i` 门控 |
| 0x30 | `CMD_QUEUE_SIZE` | R/命令W/host W | 4096 | `>=8` 且 8 的倍数；写合法值同时 `HEAD=TAIL=queue_base` |
| 0x34 | `CONST_BASE` | R | `0x0200_0000` | 常量，不可写 |
| 0x38 | `FB_BASE` | R/W | `0x1000_0000` | 帧缓冲基址 |
| 0x3c | `DDR_BASE` | R | `0x8000_0000` | 常量，不可写 |
| 0x40 | `CP_RESET` | host W | — | 写 bit0 ⇒ 软复位（§13.3）；命令路径无效，见 §15.2 |
| 0x44 | `TIMEOUT_CYCLES` | R/W | `0x0010_0000` | 写 0 恢复默认值 |
| 0x48 | `SM_KERNEL_MASK` | R | 0 | `LOAD_KERNEL` 成功时按 `sm_mask` 置位；`CP_RESET` 清 |
| 0x4c | `SM0_FAULT_PC` | R | — | SM0 出错 PC 透传 |
| 0x50 | `SM1_FAULT_PC` | R | — | SM1 出错 PC 透传 |
| 0x60 | `GEMM_STATUS` | R | — | `bit0=BUSY,1=DONE,2=ERROR` |
| 0x64 | `GEMM_ERROR` | R/host W | — | 写即清（数据忽略，`eng_err_clr[0]`） |
| 0x68 | `2D_STATUS` | R | — | 同 GEMM |
| 0x6c | `2D_ERROR` | R/host W | — | 写即清（数据忽略，`eng_err_clr[1]`） |

未列出的偏移读回 0、写被忽略（`cp_regs.sv:147` 的 `default`）。

`WRITE_REG` 命令的白名单是 `cp_regs.sv:109` 列出的 15 个偏移；不在这 15 个里的
地址 ⇒ `CPERR_CMD`。这 15 个里真正能靠命令改到寄存器值的只有 9 个
（`CP_ERROR` / `IRQ_STATUS` / `IRQ_ENABLE` / `CMD_TAIL` / `SEM_BASE` / `FB_BASE` /
`TIMEOUT_CYCLES` / `CMD_QUEUE_BASE` / `CMD_QUEUE_SIZE`），另外 6 个
（`DOORBELL` / `CP_RESET` / `SM0_ERROR` / `SM1_ERROR` / `GEMM_ERROR` / `2D_ERROR`）
的事件或清零信号只由 host 访问触发 —— 见 §15.2 第 5 条。

---

## 13. 错误模型与恢复

### 13.1 八种错误（`cp_pkg.sv`）

| 码 | 名称 | 典型来源 |
|----|------|----------|
| 0 | `CPERR_NONE` | — |
| 1 | `CPERR_CMD` | 长度/保留位/对齐/窗口/镜像头/CRC 不合法 |
| 2 | `CPERR_LAUNCH_REJECT` | 单 SM 语义的 kernel 却要求跨 SM |
| 3 | `CPERR_BUS` | 内存口回 `err` |
| 4 | `CPERR_TIMEOUT` | 等 SM / 等引擎 / 轮询超时 |
| 5 | `CPERR_ILLEGAL_STATE` | 队列配置不是最后一条 / kernel 跑着时装镜像 |
| 6 | `CPERR_STALE_SM_ERROR` | 目标 SM 还挂着上一次的错误 |
| 7 | `CPERR_SM_FAULT` | kernel 跑挂了 |
| 8 | `CPERR_ENGINE_FAULT` | 2D/GEMM 报错 |

### 13.2 首错保留

`cp_regs` 里 `if (cp_err_we && cp_error == CPERR_NONE) cp_error <= cp_err_wdata`
（`cp_regs.sv:194`）—— 只有第一个错误能写进去，后续都被忽略。配合"失败不推进
HEAD"，`CP_ERROR` + `CMD_HEAD` 两个寄存器就能定位到出错的命令。

### 13.3 `CP_RESET` 到底重置什么

写 `CP_RESET[0]=1` 时（`cp_regs.sv:243`）：

- `HEAD <= TAIL`（丢弃队列里剩余命令，不阻塞 host 继续用）
- `CP_ERROR <= 0`、`IRQ_STATUS <= 0`
- `SM_KERNEL_MASK <= 0`（之后的 `LAUNCH` 会被拒绝，直到重新 `LOAD_KERNEL`）
- `doorbell_pend <= 0`

**不**清：`QUEUE_BASE/SIZE`、`SEM_BASE`、`FB_BASE`、`TIMEOUT_CYCLES`、各 SM 的
`img_entry/size/flags`（它们已被 `KERNEL_MASK` 屏蔽）、`IRQ_ENABLE`。

状态机侧同时回 `ST_IDLE`、清 `mop/req_sent/line_valid/kernel_active_q/timeout_cnt`
（`cp.sv:549`）。注意它**不会**清 `ST_ERR` 里已经锁死的 `CP_ERROR` 之外的任何东西 ——
复位之后 SM 的 `SM*_ERROR` 仍在，必须单独写一次清零，否则下一次 `LAUNCH`
会得到 `CPERR_STALE_SM_ERROR`。这是设计意图（错误现场保留）。

---

## 14. 时序与性能备忘

单条命令的周期数 = `取命令` + `执行` + `收尾`：

| 阶段 | 周期 | 备注 |
|------|------|------|
| `ST_IDLE→LOADW` | 1 | 空队列敲门是 0 |
| 取 word0/word1 | 2（命令缓存命中）/ +两段 8 拍突发 | 缓存未命中才走内存；见 §2.3 |
| 补命令体 | `len-2`（行命中） | 每字 1 拍 |
| `ST_DECODE` | 1 | |
| `ST_EXEC` | 1 | |
| 内存操作 | 3～5（单字读）/ 5～7（单字写） | 读: req→ack→一拍数据；写: req→ack→完成应答 |
| `ST_UPD_FIN` | 1（无信号量）/ +6～8（有信号量） | |
| 行未命中的惩罚 | +20～22 | 16 拍数据 + 回话一拍 + 握手；`cmd_cache` 命中后这条基本消失(命令环 4KB 全驻留)，并靠顺序预取摊薄 |

所以：2 字 NOP 约 7 拍，5 字命令约 10 拍（都是命令缓存命中口径；未命中再 +18～20）；
整套 `sim/run-sim` 上实测(4 warp)：clear 1240 拍、gouraud 6098 拍、matmul 156234 拍、
anim 每帧 14851 拍。`LOAD_KERNEL` 每字约 2～3 拍
（读行 + 写 SRAM + CRC 三件事并行）；`PARAMS` 每字 4～5 拍（一次写握手）；轮询空转
每 10～12 拍一次内存读。

---

## 15. 已知差异、坑与注意事项

### 15.1 与参考模型的差异（有意为之）

| 行为 | 参考模型 | RTL |
|------|----------|-----|
| WAIT 超时 | 立刻 `TIMEOUT` | 真的等 `TIMEOUT_CYCLES` |
| FENCE | 空操作 | 空操作（语义已由 in-order 覆盖） |
| LOAD_KERNEL | 先把镜像读进缓冲再算 CRC | 边流边算，不缓冲（省 BRAM） |

### 15.2 五个必须知道的实现细节

1. **行缓冲必须每条命令清零。** 有人觉得"跨命令复用会更省"，那是错的：host 可以
   搬回 `HEAD` 重写已消费区域，缓存会读到旧字节。
2. **`next_head_raw`（未回绕值）同时用于三个判断。** 改成用回绕后的 `next_head`
   去比 `tail` 会把"命令正好结束在环末尾"判错。
3. **`ST_MEM` 里 `mop` / `op_is_rd` 用的是本拍旧值。** 因为 `mop <= OP_NONE`
   是非阻塞赋值。往这个块里加逻辑时不要以为 `mop` 已经变了。
4. **空队列敲门会被消费掉。** `doorbell_pend` 是电平位而不是计数器：host 在 CP
   忙时敲 10 次，CP 只看到一次。命令是否该执行由 `head != tail` 决定，所以这不影响
   正确性，但"敲门次数"没有意义。
5. **6 个偏移"命令写能过校验但不生效"。** `cp_regs.sv:109` 的 `chk_wr_ok` 白名单
   收了 `DOORBELL` / `CP_RESET` / `SM0_ERROR` / `SM1_ERROR` / `GEMM_ERROR` /
   `2D_ERROR`，但这几个的动作信号（`doorbell_evt`、`cp_reset_evt`、`sm_err_clr`、
   `eng_err_clr`）全部由 **host 直写**的 `ext_wr_en` 触发（`cp_regs.sv:253-260`），
   而主写 `case` 里没有它们的条目 —— 所以 `WRITE_REG` 命令写这 6 个地址只会
   "什么都不发生"（既不报错也不生效）。要复位/敲门/清错误现场，只能由 host 直接写
   这些寄存器。反过来，`WRITE_REG` 命令也不能用来敲 `DOORBELL` 自杀式连发命令 ——
   这是当前实现的确定性行为，改的话要同时动 `chk_wr_ok` 和事件产生逻辑。

### 15.3 顶层示例里的接线（`sim/run-sim/rtl/gpu_system.sv`）

- `eng_ready` / `gemm_status` / `gemm_error` / `twod_status` / `twod_error` 被
  `` `ZERO_PIN `` 接 0（该示例没接引擎）。所以任何 `GEMM` / `2D_*` 命令都会卡在
  `ST_ENG_SEND` 直到 `TIMEOUT`，这是**预期**行为，不是 CP 死锁。要测引擎路径得把
  这五个信号接到真的引擎，或者接一个最小桩（`eng_ready=1`、`status` 在 payload
  收完后置 `DONE`）。
- SM 侧接线是对的：`u_core0 → cp_sm0_{error,fault_pc}`、`u_core1 →
  cp_sm1_{error,fault_pc}`，`sm_done[0]/[1]` 分别回 CP。该文件当前有未提交改动
  （`git status` 里的 `M`），核对接线时以 working tree 为准。

### 15.4 与旧版 cp.md 的勘误

| 旧文档说 | 实际 |
|----------|------|
| 模块拆分 `cp_fsm.sv / cmd_fetch.sv / kernel_loader.sv / sem_unit.sv / launch_unit.sv / engine_unit.sv / cp_err.sv` | 只有 `cp.sv` + `cp_regs.sv` + `cmd_decode.sv` + `axil_slave.sv` + `crc32_word.sv` |
| `ST_PARAMS_RET`、`ST_LENCHK`、`ST_UPD` 等状态 | 不存在；`ST_PARAMS` 直接回自己，长度检查合在 `ST_LOADW`，`ST_UPD_FIN` 就是收尾 |
| "21 个状态" | 18 个（`cp.sv:150`） |
| 信号名 `s_axil_*`、`eng_error_ext` | 实际是 `s_aw*` / `s_w*` / `s_ar*` / `s_r*`，引擎错误走 `gemm_error` / `2d_error` |
| WAIT 只要求地址对齐 | 还要求 `>= MMIO_END`（`cmd_decode.sv:109`） |

---

## 16. 验证

### 16.1 用例清单（`tb/tests/cp/test_cp.py`，15 个）

| 用例 | 覆盖 |
|------|------|
| `test_mmio_registers` | 全部寄存器读写、复位值、`CP_STATUS` 位 |
| `test_nop_walk_and_irq_flag` | 纯 NOP 队列走完、`CMD_DONE` 位 |
| `test_signal_wait_readreg_irq_fence` | SIGNAL / WAIT / READ_REG / IRQ / FENCE |
| `test_params_writes_constant_region` | PARAMS 落常量区、长参数（>16 字） |
| `test_queue_boundary_errors` | 长度 <2、跨环末尾、越过 TAIL |
| `test_queue_cfg_rules` | `QUEUE_BASE/SIZE` 的值与时机两道门 |
| `test_wait_timeout_and_wait_id` | WAIT 超时、`wait_id` 放行 |
| `test_load_kernel_and_launch` | 装镜像 → 分发 → 完成 |
| `test_load_kernel_crc_and_flag_checks` | magic / size / CRC / 保留位 / flags 不符 |
| `test_launch_reject_and_stale_sm_error` | 跨 SM 拒绝、SM 残留错误 |
| `test_sm_fault_does_not_signal` | SM 报错 ⇒ 不加信号量 |
| `test_gemm_and_2d_dispatch` | 引擎 payload 逐字、`2D_FLUSH` 零 payload |
| `test_engine_fault_does_not_signal` | 引擎报错 ⇒ 不加信号量 |
| `test_cp_reset_semantics` | `CP_RESET` 六件事、不清的项 |
| `test_bus_error_on_queue_read` | 内存口 `err` ⇒ `CPERR_BUS` |

`test_crc32.py` 用 zlib 交叉验证 `crc32_word`。

### 16.2 怎么跑

```bash
# 1) 静态检查(整颗 SoC, -Wall)
make -C sim/run-sim lint

# 2) CP 的 cocotb 用例(15 个)
make -C tb dut DUT=cp                 # 或 make -C tb test 跑全部 DUT
make -C tb dut DUT=cp TEST=test_nop   # 只跑名字匹配的用例
make -C tb waves DUT=cp               # 带波形
make -C tb dut DUT=crc32_word         # 单独验证 CRC32

# 3) 顶层整机 demo(不是 CP 的单元测试)
make -C sim/run-sim run
make -C sim/run-sim check             # 跑 clear/gouraud/matmul/anim 四个 demo
```

`make -C tb list` 可以看到注册的 DUT 列表，CP 相关的有两个：`cp`（顶层状态机）
与 `crc32_word`。

---

## 17. 调试检查单

CP 卡住时按这个顺序看：

| 现象 | 先看 | 再想 |
|------|------|------|
| `BUSY=1` 但 `HEAD` 不动 | `CP_ERROR` | 有值 ⇒ 看 §13.1 对应码；为 0 ⇒ 看 `CP_STATUS` 是否卡在 `ST_ERR` 之前的等待 |
| `BUSY=1`、无错误、一直等 | 波形里 `cp_state` | `ST_WAIT_SM`⇒SM 没完成/`SM_DONE` 没接对；`ST_WAIT_ENG`⇒`eng_ready/status` 接法；`ST_POLL_WAIT`⇒值永远不够 |
| `BUSY=0` 且 `IDLE=0` | `CP_ERROR` | 这一组合只可能是“有错误”：停机后只有 `CP_RESET` 能出去 |
| 命令"没执行" | `CMD_HEAD` vs `CMD_TAIL` | 相等 ⇒ host 没写 TAIL 或没敲门；不等 ⇒ CP 卡在前一条 |
| `LAUNCH` 报 `STALE_SM_ERROR` | `SM0/1_ERROR` | 上一次错误没清（`CP_RESET` 不清它，要单独写回读寄存器） |
| `LOAD_KERNEL` 报 `CMD` | 镜像头 8 字 + CRC | 特别是 `w2 == size`、`w5 == size-entry`、CRC 只覆盖 `entry_off` 起 |
| 引擎命令必超时 | `eng_ready` | 顶层可能是 `ZERO_PIN`（§15.3） |

---

## 附:一页速查

```
命令环: head/tail 相等=空; 命令不跨环末尾; HEAD 只在完成后前进
取指  : 64B 行缓冲(16 字), 每条命令清零
校验  : cmd_decode(长度/保留位/对齐/窗口) + ST_DECODE(队列配置时机/缺镜像/LAUNCH 六条)
执行  : 自己做的有 NOP FENCE READ_REG WRITE_REG SIGNAL WAIT IRQ PARAMS
        分发出去的有 LAUNCH -> SM0/SM1, GEMM/2D -> 引擎
同步  : signal_id => SEM[id]+=1(RMW 两趟); wait_id => SEM[wait_id]!=0 才放行
错误  : 首错保留 + 停机, 只认 CP_RESET; 失败不推进 HEAD 不加信号量
超时  : 所有等待态共享 TIMEOUT_CYCLES(默认 1M 拍)
```
