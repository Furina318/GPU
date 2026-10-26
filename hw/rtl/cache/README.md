# cache/(不实现,保留目录)

本设计**没有 icache,也没有 dcache** —— 这不是遗漏,是架构选择:

| 存储 | 为什么不需要 cache |
|---|---|
| 指令 | 内核由 CP 在 `LOAD_KERNEL` 时**整块预加载**进 SM 的指令 SRAM(16KB / 4096 条,`hw/rtl/mem/instr_mem.sv`)。运行期只按 PC 顺序读,没有"缺失"这回事;改代码必须重新 `LOAD_KERNEL`(SM 忙时拒绝,`CMDS.md` §4.1) |
| 数据 | 共享内存本身就是软件管理的 scratchpad(16KB / 8 bank,`hw/rtl/mem/shared_mem.sv`);全局访存靠**写合并缓冲**(每 SM 4–8 项)把 `st.g` 合成 burst,`exit` 与 `bar.sync` 负责冲刷(ISA §2.4/§2.5)。跨引擎可见性靠"命令完成 + `FENCE`",不需要相干协议(`CMDS.md` §10.3) |

将来若要加 cache(例如上板跑更大的工作集),约束是:

- **一致性**:SM 与 2D/矩阵引擎之间**没有任何硬件相干**(`CMDS.md` §10.3、`SPEC.md` §3.3)。加 dcache 就必须重新定义
  `FENCE` 的语义(冲刷范围、是否等待写回),否则"顶点算完但还在 cache 里"会让 2D 引擎读到旧值。
- **`MMIO_ACCESS`**:内核访问 MMIO 窗口是架构错误(`CMDS.md` §8.2 第 6 项),cache 不能把它"缓存掉"。
- **`LOAD_KERNEL` 与指令存储**:若引入 icache,`LOAD_KERNEL` 之后必须整片失效(现在的实现没有这个动作,因为不存在)。

在引入这些机制之前,本目录保持为空 —— 放一个空的 `cache.sv` 只会让人以为它被实现了。
