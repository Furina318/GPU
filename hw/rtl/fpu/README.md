# FPU(+fdiv/fsqrt 数据通路)

ISA 只暴露 RV32F 的一个子集(ISA §3.3 / §5.2),但 RTL 把**完整的单精度数据通路**都实现了:
`fma` 是唯一有真实意义的融合单元,`fdiv`/`fsqrt` 存在但 **ISA 不发射这两条指令**(D6) —— 它们目前只被
单元测试驱动,留给以后若要做 `div`/`sqrt` 时直接接。

## 模块

| 文件 | 职责 | 延迟 |
|---|---|---|
| `fpu.v` | 顶层:按 `fpu_op` 选择单元、仲裁 5 个子单元的写回口、`flush` 冲刷、`fflags` 汇总 | 各单元不同,见下 |
| `fpu_decoder.v` | FP32 拆成 `{sign, exp[8:0], sig[23:0]}` 并给出 `zero/subnormal/normal/inf/nan/snan/qnan` 分类位;纯组合 | 0 |
| `fma_unit.v` | 融合乘加(3 级流水):Booth+Wallace 乘积 → 对齐/粘滞位 → 规范化/次正规化与舍入;`FADD/FSUB/FMUL/FMADD/FMSUB/FNMADD/FNMSUB` 都在这里 | 3 |
| `fquick.v` | 一周期单元:`FCLASS`/`FMIN`/`FMAX`/`FLT`/`FEQ`/`FLE`/`FSGNJ*` | 1 |
| `fcvt.v` | `FCVT_W_S`/`FCVT_WU_S`/`FCVT_S_W`/`FCVT_S_WU`;`f2i` 用向零舍入 + 饱和(NaN/溢出 ⇒ 饱和值,ISA §5.2) | 多周期 |
| `fdiv.v` | SRT 除法;单槽、`busy` 期间反压(`issue_valid & ~busy`) | 迭代 |
| `fsqrt.v` | radix-4 SRT 开方,选位表在 `sqrt_srt_table.vh` | 迭代 |
| `frm.v` | 舍入:5 种模式由 `fpu_config.vh` 的 `` `RNE/RTZ/RDN/RUP/RMM `` 编码 | 组合 |
| `fpu_arbiter.v` | 5 路写回仲裁:按**年龄**(age order)选出最早的有效结果,不按端口号;参数 `NUM_PORTS`/`ID_WIDTH` | 组合 |
| `fpu_config.vh` | 操作码常量(`FADD_S`…)、舍入模式、`fflags` 位序(`NV/DZ/OF/UF/NX`)与 `fcsr` 字段位置 | — |

## 顶层接口

```systemverilog
fpu #(ID_WIDTH=5) (
  clk, rst,
  valid, ready,                 // 请求握手
  fpu_op[6:0], rm[2:0],         // 操作码 + 舍入模式
  src1, src2, src3,             // 三个源操作数(fma 用满 3 个)
  flush, flush_id,              // 冲刷:丢弃 ID 匹配的在途结果(分支误预测/异常)
  fpu_id,                       // 乱序核的指令 ID,写回时原样带回
  fpu_valid, fpu_id_o, fpu_result, fpu_fflags
);
```

- **ID 随结果返回**:FGMT/乱序调度靠 `fpu_id_o` 把结果配回指令,顶层不允许重排 ID。
- **`flush`**:只有一个 `flush_id`,即只冲刷**一条**在途结果;多条在飞时由核侧保证不冲突。
- **`fflags`**:`{NV, DZ, OF, UF, NX}` 5 位,与 RISC-V 的 `fcsr` 低 5 位同序。

## 与 ISA 的边界

- ISA 暴露:`fadd/fsub/fmul/fma/fnma`、`fmin/fmax`、`feq/flt/fle`、`fclass`、`fsgnj*`、`f2i/i2f`(ISA §5.2)。
- **不暴露**:`fdiv`/`fsqrt`(D6)。原因是逐 lane 的除法单元面积不可接受,而目标内核里没有除法 ——
  内核需要倒数时用 `divu` + 常数或直接查表(ISA §7.1/§7.3 的写法)。
- 舍入模式固定为 RNE(`rm` 由译码器给出,但 ISA 没有 `fcsr` 指令可改)。

## 验证

| DUT | 用例文件 | 覆盖 |
|---|---|---|
| `fpu_decoder` | `tb/tests/fpu/test_fpu_decoder.py` | 定向 + 字段扫掠 + 随机 + 与 Python 分类器逐位比对 |
| `fcvt` | `tb/tests/fpu/test_fcvt.py` | 定向 + 随机(含饱和、NaN、±0) |
| `fpu_arbiter` / `fpu_arbiter_5` | `tb/tests/fpu/test_fpu_arbiter.py` | 4 口与 5 口两种配置、年龄序、随机 |
| `fpu` | `tb/tests/fpu/test_fpu_top.py` | 逐运算对拍 Python 参考 `tb/harness/lib/fp32.py`(精确 `Fraction` 的 binary32 模型,融合运算不会二次舍入)、随机向量、5 种舍入、次正规/平局、`fflags` 约定 |

跑法:`cd tb && python3 run.py --dut fpu`(其余 DUT 同理,见 `tb/README.md`)。

## 已知问题

1. **FMA 在"恰好差 1 ulp"时的有符号舍入缺陷**:当精确 `a*b+c` 落在舍入中点下方约 `2**-100` ulp 时,
   guard 位会算成 1 而朝错误方向舍入。根因是 `fma_unit` 把 64 位窗口以下的信息压成一个
   `p1_far_sticky` 位,在**减法路径**上引入了偏差。修它需要加宽对齐窗口或做借位感知的 sticky(数据通路改动),
   因此现在用 `test_fma_near_midpoint_1ulp_KNOWN_DEFECT` 把三条反例**钉住**而不是盲改。ISA 侧不暴露 FMA
   的"精确结果"语义,所以这个缺陷不影响已实现的内核(它们的误差在 ulp 级,且不与参考模型逐位比较 FP 结果)。
2. **严格 lint 未通过**:`fdiv.v` 报 `WIDTHEXPAND`、`fma_unit.v` 报 `UNOPTFLAT`(既有的组合环警告),
   所以 `--werror` 下 `fpu`/`fpu_arbiter` 构建失败;去掉 `--werror` 后用例全过。这两条是遗留问题,不是本次引入。
