# sw/sg —— 自定义语言(.sg)的编译器与示例

| 文件 | 说明 |
|---|---|
| `grammer.md` | **语言规格**(词法/类型/声明/表达式/语句/内置函数/限制/错误码) |
| `compiler.py` | 编译器: `.sg` → ISA 汇编(`.S`)→ 内核镜像(`.bin`,经 `assembly.py`) |
| `clear.sg` | 示例: 帧缓冲填充(与手写 `sw/kernels/clear.S` 语义等价) |
| `abs.sg` | 示例: 逐 lane 条件 + FP32 比较与取负(等价于 `sw/kernels/abs.S`) |
| `relu_swap.sg` | 示例: 共享内存 + 两次 `bar()` + `while` + 谓词化跨 lane 访存 |
| `mask_low.sg` | 示例: 位运算/移位(`&` `\|` `>>>`)+ `if/else` + `\|\|` 短路条件 |

## 用法

```bash
cd sw
python3 compiler.py sg/clear.sg -o build/clear.S          # 看生成的汇编
python3 compiler.py sg/clear.sg --image build/clear.bin   # 直接出内核镜像
python3 compiler.py sg/clear.sg --disasm                  # 编译后反汇编(人工核对)
python3 compiler.py sg/*.sg --asm-dir build --image-dir build   # 批量
python3 compiler.py sg/clear.sg --stats                   # 编译报告: 布局 + ISA 指令直方图

cd ../sim/simulator
make sg                    # 把 ../../sw/sg/*.sg 编译成 build/sg/*.bin
```

生成器只做**模板展开 + 寄存器分配 + 标签解析**(`grammer.md` §1),不做复杂优化:每个语言构造对应
一段固定模板,生成的汇编里每条指令都带注释。少数"模板级"的省事规则(`grammer.md` §5)是:

| 规则 | 效果(汇编后条数) |
|---|---|
| 常量折叠 + **立即形式**(`andi/ori/slli/srai/srli`) | `clear.sg` 39 → 33、`abs.sg` 25 → 21、`relu_swap` 59 → 51 |
| **就地写目标寄存器**(不多做一次位型拷贝) | 同上;谓词化时**不**就地写(算术没有谓词字段,否则条件不成立也会改掉变量) |
| **常驻常量**(出现最多的 2 个) | 循环体里不再每轮重新装载 `4` 这类常量 |

能力速览:位运算与移位、`&&`/`||` 短路条件、`if/else`、`while`、共享/全局数组、`bar()`、`ass` 插指令、
`f2i`/`i2f`。四个示例分别覆盖:填充(clear)、逐 lane 条件 + FP32 比较与取负(abs)、
共享内存 + 屏障 + 跨 lane 交换(relu_swap)、位运算 + 短路条件(mask_low)。

## 实现注记

下面这些是 v0.1 里没写死(或写错)的地方,**已经按实现回填进 `grammer.md` v0.2**(见其附录 A);
编译器的行为与规格现在一一对应,列在这里只是方便对照:


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
