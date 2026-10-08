# simu —— SimpleGPU 功能模拟器

把 `docs/ISA.md`(48 条指令、SIMT 分歧、barrier、错误表)与 `docs/CMDS.md`(命令环、LAUNCH 前置条件、
MMIO、信号量)真正**跑起来**的 C++17 程序。它有两个用途:

1. **调试**: 单步看每一条指令怎么改 lane 掩码、SIMT 栈怎么压/弹、共享内存怎么被 8 个 lane 写。
2. **将来 RTL 的对照物**: 分层回归金字塔(PLAN.md §11)里的"模块级 golden model" —— 同一份内核镜像,
   RTL 与这里的**逐元素**(而不是只看第 0 行)结果必须一致。

```
make            # 编译 build/simulator(命令行前端) + build/anim(动画 demo)
make run                          # 汇编 clear.S 并运行
make run KERNEL=matmul ARGS="--log m.log"
make debug KERNEL=matmul          # 直接进交互式调试器
make check                        # 跑全部内核 + 结果校验
make test                         # 回归测试(正例 + ISA/CMDS 错误路径 + 运行模式 + 动画)
make anim DEMO_ARGS="--ascii"     # 动画 demo(sw/demo/anim.cpp, 见 sw/demo/README.md)
```

构建产物:`build/libsimu.a`(除命令行前端外的全部源码)+ `build/simulator` + `build/anim`。
库的形式是为了让 `sw/demo/` 里的 **host 驱动**能直接链接同一套 Machine/CP API —— 也就是
PLAN.md §11"系统级: host 驱动走完整流程: 写命令 → 引擎执行 → 读回验证"。

## 1. 三种运行方式

| 方式 | 命令 | 说明 |
|---|---|---|
| 直接运行 | `build/simulator build/matmul.bin` | 走完整命令流(`LOAD_KERNEL → PARAMS → LAUNCH`),打印统计与结果校验 |
| 交互调试 | `-d` / `--debug` | `si` 单步、断点、看寄存器/内存/SIMT 栈;支持管道脚本:`printf 'si 3\nc\nq\n' \| … -d` |
| 日志模式 | `--log run.log [--log-level trace]` | 结构化日志(`cycle=… warp=… pc=… level=… at=… msg=…`),可 grep;控制台输出与日志互不干扰 |

启动时先把"这次模拟的关键数据"打出来:硬件规模(SM/warp/lane/线程上限)、**周期模型的前提**、
内核镜像头(magic/entry/size/crc/flags)、CP 配置、**整条命令流**(含参数与线程数),
以及 LAUNCH 分发后每个 warp 落在哪个 SM 的哪个槽位 —— 避免"跑完了不知道跑了什么"。

## 2. 常用选项

| 选项 | 作用 |
|---|---|
| `--warps N` | 启动的 warp 数(1..8);默认按内核给(clear/gouraud/matmul = 4) |
| `--param OFF=VALUE` | 覆盖常量区参数(OFF常量区偏移) |
| `--no-init` / `--no-check` | 不预置输入缓冲 / 不做结果校验 |
| `--direct` | 绕过命令流(`load_img + 直接写常量区 + LAUNCH`),与命令流路径互为对照 |
| `--cp-demo` | 命令流尾部追加 `SIGNAL/WAIT/READ_REG/FENCE/NOP/IRQ`,自检 CP 的依赖与寄存器通路 |
| `--max-cycles N` | 周期上限(默认 1M,超时报 TIMEOUT 而不是挂死) |
| `--div-cost N` | 周期模型:div/rem 占用周期(默认 8) |
| `--trace` | 逐条指令日志(带 cycle/warp/pc) |
| `--dump A,L[,FILE]` | 把全局内存 `[A, A+L)` 按字导出(十六进制 + 浮点),便于脚本与参考模型比对 |
| `--fb-out FILE [--fb WxH]` | 导出帧缓冲为 PPM |
| `--strict-mem` | 访存越出已知内存区即报错(默认只警告一次;驱动自分配缓冲属正常) |
| `--cp-script FILE` | 用脚本定义整条命令流(`nop/load_kernel/params/launch/write_reg/read_reg/signal/wait/fence/irq/gemm/mmio/doorbell`),CP 边界与异常路径的测试入口 |
| `--write-word A=V` | 直接往全局内存写字(可重复;准备测试数据) |
| `--load-extra F@A` | 把一个文件写进全局内存地址 A(可重复;多镜像测试) |
| `--cp-demo` | 命令流尾部追加 `SIGNAL/WAIT/READ_REG/FENCE/NOP/IRQ`,自检 CP 的依赖与寄存器通路 |
| `--cp-fill N` | 先塞 N 条 8 字 NOP 并让 CP 处理掉,逼出命令环回绕时的 NOP 填充(§2.1) |

退出码: `0` 正常,`1` 命令/CP 错误,`2` SM 架构错误,`3` 结果校验不符 —— 便于 CI 直接用。

## 3. 调试器命令

```
si [n] / s [n]   单步 n 条(默认 1),每条打印反汇编与 pc/mask 变化
c / continue     运行到结束 / 断点 / 错误(会自动推进 CP 命令流)
u <addr>         运行到地址;  b <addr> 设断点;  bd/bc 删/清
p [rN]           寄存器:8 个 lane 并排(或某个寄存器的 8 个 lane 值)
w [n]            切换/列出 warp(含 pc、mask、栈深、退役条数、是否卡在 bar.sync)
l / lanes        active_mask 与 p1..p3 的逐 lane 视图
st / stack       SIMT 栈(recov/fall/mask_full/mask_pending/pending)
x <addr> [n]     看全局内存(自动标注所在区域);  xs 看共享内存
dis [addr] [n]   反汇编(标出当前 PC)
state / cmd / stat / mmio / log [n]
set r5 0x10 | set r5.3 0x10 | set pc 0x40 | set mask 0x0f
trace on|off     q 退出
```

## 4. 代码结构

| 文件 | 职责 |
|---|---|
| `src/exec.cpp` | 执行引擎: 取指/译码/执行、barrier、访存、错误、FGMT 调度 |
| `src/inst.cpp` | ALU / FP32 / fma / setp 的 lane 级语义与掩码写回(可单独测试) |
| `src/decode.cpp` | 模式编译式译码器(`INSTPAT`)+ 保留字段校验 |
| `src/cp.cpp` | 命令环、§2.4 校验表、LAUNCH 前置条件交叉核对、信号量、矩阵引擎 v1(INT8→INT32) |
| `src/memory.cpp` | 稀疏全局内存、每 SM 共享内存/指令 SRAM、镜像头校验(CRC)、MMIO 边界 |
| `src/debugger.cpp` | 交互式调试器 |
| `src/log.cpp` | 分级日志(控制台/文件/环形缓冲) |
| `src/main.cpp` | 命令行、启动信息、结果校验(clear/gouraud/matmul 的参考模型)、PPM/内存导出 |

## 5. 已知的规范缝隙(实现按"报错"处理)—— 均已写进 CMDS §8.1/§8.2

- **命令环尾部只剩 4 字节时无法填充**: CMDS §2.1 要求驱动用 `NOP` 填到末尾再回绕,但 §6.1 规定 `NOP ≥ 2 字`,
  于是尾部恰好剩 4 字节时**没有任何合法填充**(队列大小只要求是 4 的倍数)。模拟器在这种情况下报错并
  建议"queue_size 取 8 的倍数"(`--cp-fill 126` 可复现),而不是静默错位。要不要把 §2.1 收紧成
  "queue_size 必须是 8 的倍数",是留给规范的一个决定。
- **`wait_id` 的比较值**: CMDS §10.2 只给了"完成计数"与 `WAIT` 的 `>= value` 两种语义,没说命令头的
  `wait_id` 比到多少。本模拟器按"该信号量至少完成过一次(`SEM[id] >= 1`)"实现,并在日志里写明。
- **SM 架构错误没有对应的 `CP_ERROR` 取值**: 已补 `CP_ERROR = 7 = SM_FAULT`(CMDS §8.1)—— §4.3 要求
  "任一 SM 出错 ⇒ 置 `CP_ERROR`",不给取值 host 只看 `CP_ERROR` 会以为一切正常; 定位仍看 `SM*_ERROR`/`SM*_FAULT_PC`(§8.3)。
- **内核访问 MMIO**: 已补 `SM*_ERROR = 6 = MMIO_ACCESS`(CMDS §8.2)。放行会让内核误写 `CP_RESET`/`DOORBELL` ——
  实测一个算错的基址会把 CP 复位掉,而 kernel 却"无错完成"、信号量永不释放; 现在直接停机报错。

## 6. 动画 demo(sw/demo)

`make anim` 跑 `sw/demo/anim.cpp`:画面由 `sw/kernels/anim.S` 逐像素算出,驱动每帧只推进
`PARAMS(帧号+调色板) → LAUNCH(signal) → WAIT`,再读回帧缓冲显示(SDL 窗口 / 终端真彩色 / PPM 序列)。
默认 64×64 约 7.8k 周期/帧(≈30 fps 墙钟),128×128 约 31k 周期/帧。细节与实测表见 `sw/demo/README.md`。

## 7. 明确的边界

- **`exit` 发生在发散路径里**只记 WARN 不报错: ISA D5 已从汇编层面禁止(谓词化 `exit` 汇编不过),ISA §2.6 也没有对应错误码。
- **`ipdom` 的 recov 有效位**由"下一条**被执行**的 `br.simt`"消费(中间的普通指令不会清它)—— 这是 §5.7 "只对紧随其后的第一条 br.simt 生效"的字面实现。
- **PC 越出指令 SRAM / 未对齐**按 `ILLEGAL_INSTR` 报(取指失败没有单独的文档错误码)。
- **`wait_id` 在依赖不满足时立刻报 `TIMEOUT`**: 本模拟器的命令是顺序执行的,没有别的执行体能在等待期间推进信号量;
  真实硬件应当等到 `TIMEOUT_CYCLES` 才发现(§6.7)。

- **周期是"发射模型"不是时序仿真**: 每 SM 每周期发射 1 条(ISA §8.1)、div/rem 多周期占用、
  共享内存同 bank 异地址串行化 —— 后两条文档没给数值,是模拟器的假设(启动时打印)。
  它回答"指令数/占用槽/是否被 barrier 卡住",不回答"综合出来多少 MHz"。
- **没有建模**: cache/写缓冲/延迟、2D 引擎(命令按 `length_words` 跳过)、FP16 与重量化 GEMM。
- **架构错误一律硬停**(ISA §2.6): 非法指令、未对齐访存、发散下的 `bar.sync`、缺 `ipdom` 的发散、
  队列/镜像校验失败 —— 全部报错并给出 `FAULT_PC` + 出错指令 + 现场寄存器,不静默继续。

## 8. 测试

`make test` 会跑 `tests/run_tests.py`(37 项)+ `tests/stack_model_check.py`:

- **正例**: clear/gouraud/matmul 结果校验 + 与 Python 参考模型逐元素比对;
  7 层嵌套发散(`tests/nested7.S`)检验 SIMT 栈(LIFO、每帧一次切路径 + 一次弹栈)
- **反例**: `BAD_ALIGN` / `BAR_DIVERGENT` / `ERR_NO_RECOV` / `ILLEGAL_INSTR` / barrier 死锁 /
  `LAUNCH_REJECT` / CRC 不符 / `SIMT_OVF` 的不可达性论证
- **运行模式**: `--direct` 与命令流结果一致、调试器脚本、日志文件内容、CP 命令自检
- **CP 边界**: 命令环回绕时的 `NOP` 填充(§2.1)、尾部仅剩 4 字节时的可诊断报错
- **CP 边界(独立复核发现的问题逐条回归)**: `GEMM_STATUS` 可读且 INT8 GEMM 算对、`SM*_ERROR` 可写(清错后能重新 LAUNCH)、
  §4.1 前置条件顺序、`kernel_pc=0x04` 的分发、两个 SM 装不同镜像被拒、`CMD_QUEUE_SIZE=0` 不崩、`PARAMS` 越界不中止、
  `signal_id` 保留区、内核碰 MMIO 报 `MMIO_ACCESS`
- **独立模型**: `tests/stack_model_check.py` 按 ISA §2.3.3 从零实现一遍路径推进规则(不用模拟器的代码),
  把"执行过的 PC 序列"与模拟器 `--trace` 逐条比对 —— 这是防止"参考模型与实现一起错"的那一层
