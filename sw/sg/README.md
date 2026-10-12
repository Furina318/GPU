# sw/sg —— 自定义语言(.sg)的编译器与示例

| 文件 | 说明 |
|---|---|
| `grammer.md` | **语言规格**(词法/类型/声明/表达式/语句/内置函数/限制/错误码) |
| `compiler.py` | 编译器: `.sg` → ISA 汇编(`.S`)→ 内核镜像(`.bin`,经 `assembly.py`) |
| `clear.sg` | 示例: 帧缓冲填充(与手写 `sw/kernels/clear.S` 语义等价) |
| `abs.sg` | 示例: 逐 lane 条件 + FP32 比较与取负(等价于 `sw/kernels/abs.S`) |
| `relu_swap.sg` | 示例: 共享内存 + 两次 `bar()` + `while` + 谓词化跨 lane 访存 |

## 用法

```bash
cd sw
python3 compiler.py sg/clear.sg -o build/clear.S          # 看生成的汇编
python3 compiler.py sg/clear.sg --image build/clear.bin   # 直接出内核镜像
python3 compiler.py sg/clear.sg --disasm                  # 编译后反汇编(人工核对)
python3 compiler.py sg/*.sg --asm-dir build --image-dir build   # 批量

cd ../sim/simulator
make sg                    # 把 ../../sw/sg/*.sg 编译成 build/sg/*.bin
```

生成器只做**模板展开 + 寄存器分配 + 标签解析**(`grammer.md` §1),不做优化:每个语言构造对应
一段固定模板,生成的汇编里每条指令都带注释说明它对应源码的哪一部分。

## 实现注记

下面这些是 v0.1 里没写死(或写错)的地方,**已经按实现回填进 `grammer.md` v0.2**(见其附录 A);
编译器的行为与规格现在一一对应,列在这里只是方便对照:

| 位置 | 规格原文 | 编译器实际生成 | 理由 |
|---|---|---|---|
| `if/else`(§6.5) | `@p1 br.simt __else, __end` | `@!p1 br.simt __else, __end` | 规格模板会把"条件成立"的 lane 送去 else 块;手写内核(car.S/abs.S)都是"条件成立落进 then",所以按后者生成 |
| `uniform when … goto L`(§6.3) | "不生成 `ipdom`,直接 `j`" | 省掉显式 `ipdom`,由汇编器按 ISA 约定补一条 | ISA 的 `br.simt` 一律配对收敛点(汇编器对任何谓词化 `br.simt` 都补 `ipdom`);语义等价,代价是多一条空转 `ipdom` |
| 用户级 `when … goto L`(向后跳) | 未说明收敛点怎么取 | 取"此语句之后的下一个标签" | 向后跳(循环回边)的收敛点是**循环出口**不是循环头;`clear.sg` 的 `when i != T goto loop` 正好落在 `done:` |
| 关键字 `floar`(§2) | 关键字表里写的是 `floar` | 接受 `floar` 并按 `float` 处理 | 显然是 `float` 的笔误,两种写法都收 |

**语义检查**(`grammer.md` §9 的错误码全部有反例,见 `make test` 第 ⑬ 节):

- `E005`/`E006` 依赖一个**均匀性分析**:`tid()`/`wid()` 是"每 lane / 每 warp 不同"的,
  参数、常量与由它们算出的量是所有 lane 一致的。`while` 的条件必须均匀(循环次数标量),
  `bar()` 不能出现在不均匀条件的 `if`/`when`/`while` 体内 —— 但**均匀条件的块里可以**,
  这正是 `matmul` 与 `relu_swap` 里"循环里带 `bar.sync`"成立的原因。
- `E013`:声明为 `shared`(跨 warp 可见)的共享数组必须有 `bar()` 保护,否则编译期报错。
- `E010`:常量区偏移/常量地址的对齐在编译期检查;共享/全局数组元素是 4B 所以天然对齐。

## 其它限制(与 §8 一致)

- 一个 `.sg` 文件只能有一个 `kernel`(无函数调用);语法按行组织,一条语句一行。
- `when`/`if`/`while` 的条件必须是比较式(§3 只有比较谓词),`&&`/`||` 需改写成嵌套判断。
- 变量放寄存器:31 个可用(`r0` 恒零);表达式临时寄存器用尽报 `E001`。
- FP 除法/取余报 `E012`(§8:无 FP 除法/开方;`ceildiv` 是整数运算)。
