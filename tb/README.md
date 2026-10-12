# cocotb verification environment (`tb/`)

Python-based verification for the SimpleGPU RTL, driven by
[cocotb](https://www.cocotb.org/) 2.x. Everything lives under `tb/` and needs no
`sudo` and no writes outside this directory.

```bash
make -C tb setup     # one time: install a working Verilator into tb/.tools
make -C tb test      # run every registered DUT
make -C tb dut DUT=fpu
```

---

## 1. Quick start

| command | what it does |
|---|---|
| `make -C tb setup` | installs Verilator 5.48 from PyPI into `tb/.tools/` and creates the shim the build needs |
| `make -C tb list` | show the registered DUTs |
| `make -C tb test` | run all DUTs on Verilator |
| `make -C tb dut DUT=fpu` | run one DUT |
| `make -C tb dut DUT=fpu TEST=fma_random` | run one test (regex filter) |
| `make -C tb waves DUT=fpu` | same, with a VCD dump |
| `make -C tb icarus` | run everything on Icarus Verilog instead |
| `make -C tb clean` | drop `tb/build/` |

Two environment knobs are useful when reproducing a failure:

```bash
TB_SEED=4 python3 tb/run.py --dut fpu          # re-run the random tests with different vectors
TB_TIMEOUT_NS=200000 python3 tb/run.py ...     # tell "dropped" apart from "very slow"
```

The same thing without `make`:

```bash
python3 tb/run.py --list
python3 tb/run.py --all --sim verilator
python3 tb/run.py --dut fpu --test test_fma_random --waves
python3 tb/run.py --all --sim verilator --werror   # strict Verilator lint
```

## 2. Why setup is needed (three environment traps)

`make -C tb setup` is not ceremony; on this machine `cocotb + verilator` cannot
build without stepping around three unrelated problems. All three are handled
automatically by `harness/env.py`, but they are worth knowing about.

**1. The system Verilator is too old for cocotb 2.x.** The installed Verilator is
5.020, but cocotb 2.x's VPI shim calls `VerilatedVpi::clearEvalNeeded()` and
`VerilatedVpi::doInertialPuts()`, which do not exist before Verilator 5.036, so
the shim fails to compile:

```
verilator.cpp:180:31: error: no member named 'clearEvalNeeded' in 'VerilatedVpi'
```

`setup` installs **Verilator 5.48 from PyPI** into `tb/.tools/pytools` and
`harness/env.py` puts it ahead of the system one on `PATH` and points
`VERILATOR_ROOT` at it. `tb/run.py` refuses to run with a Verilator that is too
old, and tells you to run `setup`.

**2. The PyPI Verilator wheel needs a `python` on `PATH`.** Its generated
`verilated.mk` ends with `PYTHON3 = python`, but Debian/Ubuntu ship only
`python3`, so the build dies with

```
make: *** [verilated.mk:231: Vtop__ALL.cpp] Error 127
```

`setup` creates `tb/.tools/shim/python -> python3` and prepends it to `PATH`.

**3. `USE_CCACHE=1` is exported globally here.** Verilator then wraps the C++
compiler in ccache, which cannot create its temp directory under the read-only
`/run/user/1000`:

```
ccache: error: Failed to create directory /run/user/1000/ccache-tmp: Read-only file system
```

`harness/env.py` sets `CCACHE_DIR` and `CCACHE_TEMPDIR` (note: **not**
`CCACHE_TMPDIR`) to `tb/.tools/cache`.

There is a fourth, intermittent one: the wheel ships a `verilated.mk` whose
precompiled-header configuration is empty (`CFG_CXXFLAGS_PCH_I` and
`CFG_GCH_IF_CLANG` are both blank), which breaks the build of any design large
enough to be split into several `.cpp` files. `setup_tools.py` neutralises PCH
in that file rather than guess the C++ compiler; see the comment it appends.

### Icarus Verilog as a fallback

`iverilog` 11.0 is present and cocotb drives it through plain VPI, so it has no
version coupling to cocotb at all:

```bash
python3 tb/run.py --all --sim icarus
```

Icarus needs an explicit timescale, which `tb/run.py` passes automatically.
It is roughly 3-10x slower than Verilator and its SystemVerilog support is a
subset, so Verilator stays the default:

```bash
python3 tb/run.py --all --sim icarus     # 29 tests, all green
```

**But do run it — it is not redundant.** Icarus is a 4-state simulator, so it
catches uninitialised/unknown values that Verilator's 2-state model silently
reads as 0. That is exactly how finding G was found: an out-of-range bit select
that produced X fraction bits for deeply-underflowing divisions, where Verilator
happened to get the right answer from the zeros. If you only run Verilator, that
whole class of bug is invisible.

The monitor warns loudly when a write-back carries X or Z rather than dropping
it, so the failure shows up as a readable warning instead of a mysterious
timeout:

```
write-back with X/Z on the bus at 9170.0 ns (fpu_valid=1 fpu_id_o=11100
fpu_result=100000000XXXXXXXXXXXXXXXXXXXXXXX) -- total 1
```

## 3. Layout

```
tb/
├── run.py                  # the single entry point (build + run + verdict)
├── setup_tools.py          # installs the pinned Verilator into tb/.tools/
├── Makefile                # convenience wrappers
├── harness/
│   ├── env.py              # toolchain discovery + the environment fixes above
│   ├── duts.py             # DUT registry: sources, toplevel, params, test module
│   └── lib/
│       ├── fp32.py         # exact IEEE-754 binary32 reference model
│       └── tb.py           # clock/reset, FpuAgent, Scoreboard
├── tests/fpu/              # one module per DUT
│   ├── test_fpu_decoder.py
│   ├── test_fcvt.py
│   ├── test_fpu_arbiter.py
│   └── test_fpu_top.py
├── build/                  # generated, gitignored
└── .tools/                 # generated, gitignored
```

### Adding a DUT

Add one `Dut` entry to `harness/duts.py`:

```python
DUTS["my_block"] = Dut(
    name="my_block",
    toplevel="my_block",
    sources=(RTL_DIR / "where" / "my_block.sv",),
    include_dirs=(RTL_DIR / "where",),
    parameters={"WIDTH": 32},
    test_module="tests.where.test_my_block",
)
```

then create `tb/tests/where/test_my_block.py` with `@cocotb.test()` coroutines.
`parameters` and per-simulator `build_args` are passed straight through to the
simulator. Nothing else needs editing — `run.py --list`, `--all` and the
`Makefile` pick the new DUT up automatically.

## 4. The reference model (`harness/lib/fp32.py`)

This is the most valuable asset in `tb/`, and it is deliberately **not** numpy.

Every arithmetic operation is evaluated **exactly** with
`fractions.Fraction` and rounded **once**, in software, according to the RISC-V
rounding mode — including the rounding-unit semantics of `frm.v`, guard/round/
sticky extraction, subnormals, overflow, and the `NV/DZ/OF/UF/NX` flags. It also
models operand decoding exactly as `fpu_decoder` does (a 24-bit integer
significand with `value = sig * 2**(exp-23)`), plus all four conversions.

Why not numpy: `a*b+c` is a *fused* operation. Computing it as
`float32(float32(a*b) + c)` rounds twice and is off by one ulp in roughly one
vector in a few thousand — precisely the cases worth testing. The exact model
gets those right.

The model was validated against numpy before being trusted:

```
60 000 random operand pairs, add/sub/mul/div/sqrt
  -> 100% agreement on every non-NaN result
20 000 random operands, decoder normalisation
  -> 0 mismatches against the exact rational value
```

The remaining differences against numpy are NaN payload conventions only: this
model follows RISC-V (every NaN-producing arithmetic op returns the canonical
`0x7fc00000`), which is also what `fma_unit.v`/`fdiv.v` hardcode.

## 5. What is covered

29 tests over 5 DUT configurations. All green on **both** simulators, and stable
across `TB_SEED=0..11` (a 12-seed sweep is a good regression to run after any RTL
change):

| DUT | tests | coverage |
|---|---|---|
| `fpu_decoder` | 4 | all classes, every exponent x 14 fractions, 20 000 random, `sig*2**exp` vs exact value |
| `fcvt` | 2 | 4 conversions, all 5 rounding modes, saturation, 6 000 random |
| `fpu_arbiter` (4 ports) | 3 | one-hot grant, oldest-id priority, wrap-around, flush, 4 000 random |
| `fpu_arbiter` (5 ports) | 3 | same, on the configuration `fpu` actually instantiates |
| `fpu` (top) | 18 | sign-injection, FCLASS, compares, min/max, add/sub/mul/div/sqrt, FMA family, 5 rounding modes, conversions, saturation boundary, subnormal ties, deep underflow, both flushing paths, and the request/write-back handshake |

A full `fpu` pass is ~46 000 randomised checks and takes ~15 s on Verilator
(~110 s on Icarus). Three tests deliberately check *protocol* rather than values,
because findings A and B were protocol bugs:

* `test_multi_cycle_backpressure` — three back-to-back `FSQRT`/`FDIV` all
  complete, and `ready` is refused per opcode (a busy divider must not block
  FADD/FMUL/FCLASS).
* `test_back_to_back_fma_pipe` / `test_back_to_back_quick_units` — 32 ops
  accepted in 32 cycles, so the write-back hold logic did not cost throughput.
* `test_mixed_burst_no_loss` — the exact op mix that used to lose 4 of 12
  results now loses none.

Tests are written so that a mass of vectors produces a readable failure: the
`Scoreboard` collects mismatches and reports "N of M checks failed" with the
first handful instead of aborting on the first one. Random tests print the
deferred/skipped vector counts so a silently shrinking test is visible.

## 6. RTL findings

The suite found seven real issues. **Six are fixed** in the RTL and their tests
now assert the *correct* behaviour; the remaining one is still pinned by a
`*_KNOWN_DEFECT` test that asserts today's wrong behaviour and will start
failing as soon as it is fixed. Re-run with `--werror` to also fail on Verilator
lint.

| # | finding | status |
|---|---|---|
| A | FDIV/FSQRT accepted requests they could not start | **fixed** — `busy_o` + per-op `ready` |
| B | write-backs silently dropped when arbitration collided | **fixed** — hold-until-acknowledged on every source |
| C | `fcvt` overflow detection missed 2\*\*64 … 2\*\*87 | **fixed** — saturation bound corrected |
| D | FMA 1-ulp errors next to a rounding midpoint | **open** — see the diagnosis below |
| E | FDIV broke ties away from zero for subnormal results | **fixed** — sticky no longer includes guard/round |
| F | `UF` used tininess-before-rounding | **fixed** — after-rounding convention |
| G | FDIV returned **unknown bits** for deep underflow | **fixed** — out-of-window bit select clamped |

Every run of the fixed RTL is green on **both** simulators: 18 tests per full
`fpu` pass, stable across `TB_SEED=0..11` on Verilator and seeds 0/4/7 on Icarus,
with §5's throughput and handshake tests guarding against the fixes costing
performance or protocol correctness.

### A. No back-pressure from FDIV/FSQRT — requests were silently dropped — FIXED

`fsqrt.v` / `fdiv.v` start work with `issue_valid & ~busy` and used to expose
**no** busy or ready output, while `fpu.v` hardwired `assign ready = 1'b1;`. A
divide or square root issued on the next cycle was therefore discarded without a
trace: three back-to-back `FSQRT`s produced **one** write-back, and a core
waiting on the missing instruction id would hang forever.

**Fix.** `fdiv.v` and `fsqrt.v` now export `busy_o`, and `fpu.v` derives `ready`
from it — deliberately *per operation*:

```verilog
wire op_is_div  = (fpu_op == `FDIV_S );
wire op_is_sqrt = (fpu_op == `FSQRT_S);
assign ready = (~op_is_div  | ~fdiv_busy ) &
               (~op_is_sqrt | ~fsqrt_busy) & ...;
```

so a busy divider refuses only further divides and square roots; FADD/FMUL/
FCLASS/FCVT keep flowing at full rate while it computes, which preserves the
overlap the multi-cycle units exist for. Note the interface contract this
implies: **the caller must present `fpu_op` while sampling `ready`, and hold
`valid` until it is accepted.** `FpuAgent.send` does exactly that.

→ `test_multi_cycle_backpressure` (three back-to-back `FSQRT` and three
back-to-back `FDIV` all complete; `ready` is checked per opcode)

### B. Write-backs dropped when arbitration collided — FIXED

`fpu.v` builds the quick-unit write-back **combinationally**:

```verilog
wire fquick_wb_valid = valid & is_fquick & ~flush;
```

so a quick result exists for exactly one cycle and is never registered. The
arbiter forwards one writer per cycle and prefers the oldest id, so whenever a
pipelined `fma_unit` write-back lands in the same cycle the quick result loses
arbitration and vanishes. `fma_unit` is two cycles deep, so with the burst
`[FADD, FMUL, FMAX, FMIN, FCLASS, FSGNJ]` the ops at index `i+2` are lost:

```
mixed burst: 12 issued, 8 writebacks, lost ids [2, 3, 8, 9]
              (mechanism predicts [2, 3, 8, 9])
```

The same could happen to a `fma_unit` or divider result: a long-latency divide
finishing in the same cycle as a pipelined FMA write-back made the FMA result
the loser, and it vanished the same way.

**Fix.** Every write-back source now holds its result until the arbiter
acknowledges it, and the arbiter's one-hot `grant_o` *is* that acknowledgement:

* `fdiv.v` / `fsqrt.v` keep `wb_valid_r` asserted until `wb_ack` instead of
  releasing `busy` after a single presentation cycle.
* `fpu.v` latches the single-cycle results (`fquick`, `fcvt`) into holding
  registers at issue time and presents those to the arbiter, so a quick result
  can no longer evaporate when `valid` drops. `ready` includes
  `qk_free`/`cv_free` so a new result cannot overwrite an unacknowledged one.
* `fma_unit` freezes its whole pipeline (and asserts `busy_o`, which feeds
  `ready`) while its stage-2 result is unacknowledged. Because `flush` is
  normally a one-cycle pulse, a flush arriving during such a freeze is latched
  and re-applied until the pipeline holds no op it could cancel — otherwise the
  mark would be missed and a cancelled instruction would still write back.

Results:

```
mixed burst: 12 issued, 12 writebacks, lost ids [], 16 cycles   (was 8/12, 4 lost)
FMA pipe burst:   32 ops accepted in 32 cycles                  (full rate kept)
quick-unit burst: 32 ops accepted in 32 cycles                  (full rate kept)
```

→ `test_mixed_burst_no_loss`, `test_back_to_back_fma_pipe`,
`test_back_to_back_quick_units`

### C. `fcvt` overflow detection missed 2\*\*64 … 2\*\*87 — FIXED

`fcvt.v` builds the integer magnitude with a **64-bit** shift:

```verilog
f2i_mag = {40'd0, src_sig} << (src_exp - 9'sd23);
```

A 24-bit significand shifted by `src_exp-23` needs `src_exp-23+24` bits, so the
high bits fall off the end from `src_exp >= 64` onward. When the surviving low
bits happen to be zero the magnitude becomes **0**, the overflow test
(`f2i_mag > 32'h80000000`) never fires and the conversion returns 0 with no
flags. The saturation guard that was meant to catch this uses the wrong bound:

```verilog
if (src_exp > 9'sd86) f2i_mag = 64'hffffffffffffffff;   // far too late
```

```
FCVT.W.S(2**64): rtl -> 0x00000000 flags 00000; correct -> 0x7fffffff flags 10000
truncation window 64..86: 90 mismatching conversions (of 138)
```

Both the value *and* the flags were lost, so a caller trusting `NV` could not
notice.

**Fix.** The saturation bound is now the real limit of the shift —
`src_exp > 63` — and the saturation value is `2**32` (large enough to be seen as
overflow by both the signed and unsigned paths, small enough that adding the
round bit cannot wrap). The whole window is now exact:

```
FCVT.W.S(2**64): rtl 0x7fffffff flags 10000   (was 0x00000000 flags 00000)
saturation scan over exponents 64..94: 0 mismatching conversion(s)
```

→ `test_fcvt_saturation_boundary` (exponent sweep 64..94 in all four sign and
significand combinations, plus the 2\*\*31 / 2\*\*32 boundaries)

### D. FMA: one-ulp errors next to a rounding midpoint — OPEN

All three failing vectors share one signature: the exact `a*b+c` sits a hair
*below* a binary32 midpoint (by ~2\*\*-100 of an ulp) yet the unit's **guard bit
comes out as 1**, so it rounds the wrong way:

```
FMADD (3.0, 0x3f066a43, -0x8049c3c2)       rtl 0x3fc99f65   correct 0x3fc99f64
FNMSUB(0x3fba7d02, -5.0, -0x8047d8ec)      rtl 0x40e91c43   correct 0x40e91c42
FMADD (0x80fd8dfe, 0x7e400000, 0x0dd3d860) rtl 0xbfbe2a7f   correct 0xbfbe2a7e
```

The gaps are 2\*\*-127.5, 2\*\*-129.7 and 2\*\*-99.8 (relative), far below what a
53-bit `double` oracle can resolve — which is why the exact rational model was
necessary to see them at all.

**Diagnosis.** The cause is the alignment window, not the sticky bit.
`fma_unit` keeps 64 exact bits (`p1_p_fixed`/`p1_c_fixed`) plus a 24-bit drop
field, and collapses everything further down into the single boolean
`p1_far_sticky`. That collapse is lossy, and on the *subtractive* path — where
one operand's low bits have to borrow through the window — the intermediate sum
is biased. Whenever the true remainder is within ~2\*\*-88 ulp of the guard
boundary the bias is enough to flip the guard, and the result is one ulp out.

**Why it is still open.** A correct guard needs either the real bits below the
window (a wider fixed field and drop field) or a borrow-aware sticky that takes
part in the subtraction. Both are datapath changes, not a condition tweak, and
they cannot be validated by reasoning alone — they need the exact-model
comparison this harness provides at scale. Doing that properly is the next
piece of work; patching the guard condition blind would trade a rare 1-ulp error
for an unknown, more common one.

`test_fma_random` defers vectors whose exact value lands within 2\*\*-60 of a
midpoint (1.6% of them; the gap distribution is sharply bimodal, with almost
nothing between 2\*\*-60 and 2\*\*-110) and reports the count.
→ `test_fma_near_midpoint_1ulp_KNOWN_DEFECT`

### E. FDIV broke ties away from zero for subnormal results — FIXED

Dividing the smallest normal by two gives a quotient exactly halfway between two
subnormals:

```
FDIV(0x008b3f79, 2.0) = 0x8b3f79 * 2**-150 = 4562876.5 * 2**-149   <- exact tie
rtl 0x00459fbd (odd)   correct 0x00459fbc (even)
```

Round-to-nearest-even must take the even neighbour; the RTL took the odd one.
`frm` itself was wired correctly, and the culprit was one line in the subnormal
result path:

```verilog
wire sub_sticky_all = div_sticky | sub_shifted_sticky;   // <- sub_shifted_sticky
wire sub_sticky     = sub_sticky_drop | sub_sticky_all;  //    ORs bits 0..sub_rsh-1
```

`sub_shifted_sticky` ORs **every** quotient bit below the shift point, which
includes the guard bit (position `sub_rsh-1`) and the round bit
(`sub_rsh-2`). On an exact tie that forces `guard & (round | sticky | lsb)` to 1,
so RNE rounded away from zero.

**Fix.** Sticky only collects bits strictly below the round bit, plus the
division's own remainder; `|quo` is added back only for `sub_rsh > 25`, where the
whole quotient really has shifted out:

```verilog
wire sub_sticky_below = (sub_rsh > 10'sd25) ? (|quo) : sub_sticky_drop;
wire sub_sticky       = sub_sticky_below | div_sticky;
```

```
FDIV(0x008b3f79, 2.0): rtl 0x00459fbc (lsb 0) vs correct 0x00459fbc
subnormal division sweep: 300 vector(s), 0 failures
```

→ `test_fdiv_subnormal_ties` (the reproducer in all five rounding modes, plus a
300-vector sweep of divisions by powers of two through the whole subnormal
range). This also let `test_arith_random` drop the deferral it needed for these
vectors, so they are now checked strictly.

### F. `UF` used tininess-before-rounding — FIXED

`NMADD(0.5, -min_subnormal, 2**-126)` yields `-(2**-150) - 2**-126`, whose
magnitude is just below the smallest normal and which rounds (ties to even) up
to exactly `2**-126`. Both the RTL and the model return that value, but the RTL
raised `UF` (0b00011) where RISC-V/SoftFloat, which use *tininess after
rounding*, raise only `NX` (0b00001). IEEE-754 permits either convention, so
this was a conformance deviation rather than a wrong result — but it is a
deviation from the spike/SoftFloat reference the rest of the FPU is written
against.

**Fix.** Both units already intended after-rounding tininess but tested an
exponent computed *before* the rounding carry, so a carry-to-normal result still
looked tiny. The condition now also excludes that case:

```verilog
if (norm_exp_final < -126 && !sub_to_normal)   // fma_unit.v
if (exp_norm       < -126 && !sub_to_normal)   // fdiv.v
```

```
NMADD(0.5, -min_subnormal, 2**-126): rtl 0x80800000/00001 model 0x80800000/00001
underflow boundary sweep: 60 vector(s), 0 failures
```

`test_underflow_flag_convention` also asserts that genuinely tiny results still
raise `UF|NX`, so the fix cannot silently stop reporting underflow.
(`fsqrt` needs no `UF`: the square root of the smallest float32 is `2**-74.5`,
a normal number, so it can never underflow.)
→ `test_underflow_flag_convention`

### Traceability against `docs/ISA.md` §7

`docs/ISA.md` ends with a table of what the demo kernels do **not** exercise and
therefore "必须另做定向单元测试" (must be covered by directed unit tests). The FPU
rows of that table are now covered:

| `ISA.md` requirement | status |
|---|---|
| `fmin`, `fmax` | `test_minmax_ops` — directed plus 3 000 random, including the NaN one-operand and +-0 rules |
| `f2i`, `i2f` | `test_conversions`, `test_fcvt` — 4 conversions x 5 rounding modes |
| NaN / subnormal boundary vectors | covered throughout; `fpu_decoder` sweeps every exponent |
| **f2i saturation boundary** | covered — and it is exactly where finding C lives |
| `fmsub.f32`, `fnmsub.f32`, `fnmadd.f32` sign combinations | `test_fma_random` round-robins all four fused variants |
| FP `setp` conditions `flt`, `fle` (and `feq`) | `test_compare_ops`, including the differing `NV` rules |

Two gaps to be aware of:

- `fne`/`fge`/`fgt` from the `setp` row have **no opcode** in
  `hw/rtl/fpu/fpu_config.vh` (only `FEQ_S`, `FLT_S`, `FLE_S` exist), so they
  cannot be tested here. If the shader ISA needs them they must be synthesised in
  the core by swapping operands / inverting the result, or added to the FPU.
- The integer ALU, integer `setp` conditions, memory and control/error paths in
  that table have no RTL yet (`hw/rtl/core/*.sv` are empty), so there is nothing
  to test. `tb/` is ready for them: add a `Dut` entry and a test module.

### G. FDIV returned unknown bits for deep underflow — FIXED

This one was found by the **Icarus** run, and it is the reason both simulators
are worth keeping (see §2).

`fdiv.v` takes the subnormal rounding inputs out of `sub_dropped` using
`sub_rsh[4:0]` as the bit index. `sub_rsh` is a 10-bit signed value equal to
`-norm_exp0 - 125`, so when the quotient is shifted entirely out of the 25-bit
window — deep underflow, `sub_rsh >= 26` — the index wraps:

```
sub_rsh = 32   ->   sub_rsh[4:0] = 0   ->   sub_dropped[0 - 1] = sub_dropped[31]
```

`sub_dropped` is 25 bits wide, and an **out-of-range bit select is X** in
Verilog. Every fraction bit of the delivered subnormal result came out unknown:

```
FDIV(0x00000003, 0x44000000)  ->  fpu_result = 0_00000000_XXXX...XXXX
```

Verilator is 2-state and read those X as 0, which happens to be the right answer
for underflow-to-zero, so it never complained. Icarus is 4-state and surfaced it
as an X write-back, which the monitor cannot key by instruction id — so it first
presented as `TimeoutError: no writeback for tag N`, and finding it needed the
harness to log the X instead of swallowing it.

**Fix.** The extraction is clamped to the window: outside it the guard and round
bits are necessarily 0 and the whole quotient becomes sticky, which is also what
makes `UF` come out right.

```verilog
wire       sub_in_window = (sub_rsh <= 10'sd25);
wire [4:0] sub_sh        = sub_in_window ? sub_rsh[4:0] : 5'd0;
wire sub_guard = (sub_in_window && (sub_rsh >= 1)) ? sub_dropped[sub_sh - 5'd1] : 1'b0;
wire sub_round = (sub_in_window && (sub_rsh >= 2)) ? sub_dropped[sub_sh - 5'd2] : 1'b0;
```

```
FDIV(0x00000003, 0x44000000):  result 0x00000000  fflags 00011   (was X / 00001)
deep-underflow sweep: 70 division(s), 0 failures
```

→ `test_fdiv_deep_underflow` (the pinned reproducers plus a 70-vector sweep, and
an assertion that no write-back carries X)


`fpu_arbiter.v:73` indexes the 4-bit `grant_o` with a 3-bit `best_idx`
(`WIDTHTRUNC`), so `verilator --lint-only` fails on it. `best_idx` should be
`$clog2(NUM_PORTS)` bits wide. The harness passes `-Wno-fatal` by default so
lint does not block the test flow; use `--werror` to see it.

### Also worth knowing

- `hw/rtl/mem/axi4_bus.sv` used to include `"define.vh"` while the file on disk
  is `defines.vh`, so the include could not resolve — nothing compiles the
  interface yet, so it went unnoticed until this review. Now corrected.
- `hw/rtl/core/{core,exu,fetch}.sv` are empty and `hw/rtl/mem/axi_arb.sv` is a
  6-line stub, so there is nothing to verify there yet. The FPU is currently the
  only complete block, which is why `tb/` starts there.
- The `fpu_decoder` header comment says the output is `(-1)^s * sig * 2^exp`.
  `sig` is a 24-bit *integer* significand, so the actual relation is
  `sig * 2**(exp-23)`; the test pins the real contract.
- `fpu_config.vh` defines `FMV_W_X`/`FMV_X_W` (and `fpu_config.vh`-style opcodes
  generally), but `fpu.v` decodes only `001`(fma) `010`(quick) `011`(cvt)
  `100`(div) `110`(sqrt). Any other `fpu_op` value — including the two `FMV`
  opcodes — falls through with **no write-back at all**, the same silent-drop
  failure mode as findings A and B. A default case that asserts, or an explicit
  illegal-op output, would make an unimplemented opcode loud instead of a hang.

## 7. Conventions for new tests

- Compare against `harness/lib/fp32.py`, never against `numpy.float32`, for
  anything involving a fused operation, a flag, or a rounding mode.
- Prefer one `Scoreboard` per `@cocotb.test()`; call `sb.report()` at the end so
  all mismatches are shown at once.
- Use `FpuAgent` rather than driving signals by hand: it drives on the falling
  edge and samples on the rising edge, so driver and monitor never race.
- Keep random tests reproducible — seed them explicitly (`random.Random(<const>)`).
- When a test must skip vectors, count and log them (`dut._log.info`), so the
  coverage cannot silently shrink.
- Pin any real RTL defect with a `*_KNOWN_DEFECT` test rather than deleting or
  weakening the check, and record it in section 6 above. When a defect is fixed,
  invert that test so it asserts the correct behaviour and update section 6 —
  do not just delete it.
- Run the suite on **both** simulators before believing a fix. Icarus is
  4-state and catches unknown-value bugs that Verilator hides (finding G);
  Verilator is much faster and is the better inner loop.
- Treat an unexpected `TimeoutError` as a possible X/uninitialised value, not
  necessarily a dropped request: check the log for the monitor's X/Z warning.
