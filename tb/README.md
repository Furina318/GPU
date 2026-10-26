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
├── harness/rtl/            # simulation-only glue (not part of the design)
│   └── fetch_imem_wrap.sv  # fetch + the real instr_mem, for one cocotb DUT
├── tests/cp/               # command processor + CRC32
│   ├── cp_env.py           # memory model, AXI4-Lite host, SM/engine/SRAM mocks
│   ├── test_cp.py
│   └── test_crc32.py
├── tests/core/             # shader core front end + instruction memory
│   ├── stack_model.py      # ISA §2.3.3 reference model + a mini datapath
│   ├── dut_drivers.py      # shared event driver / state comparison helpers
│   ├── test_simt_stack.py
│   ├── test_simt_stack_ovf.py
│   ├── test_fetch.py
│   ├── test_fetch_imem.py  # nested7.S + random kernels on the real instr SRAM
│   └── test_instr_mem.py   # preload port, 1-cycle read, overflow, image layout
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

74 tests over 12 DUT configurations: 30 on the FPU (all green on **both**
simulators, stable across `TB_SEED=0..11` -- a 12-seed sweep is a good regression
to run after any RTL change) plus 44 on the shader core front end, instruction
memory and the command processor (Verilator only, see below):

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

### Shader core front end + instruction memory (`simt_stack`, `fetch`, `instr_mem`)

| DUT | tests | coverage |
|---|---|---|
| `simt_stack` | 8 | §2.3.1 three cases, §2.3.3 path switch / pop / empty-path fast case, several pops inside one advance, a branch landing exactly on its convergence point, 7-deep nesting, `ERR_NO_RECOV`, an EXU sending an unmasked `taken`, and **3 000 random events compared cycle-by-cycle** (combinational outputs *and* registered state) against an independently written §2.3.3 model |
| `simt_stack_ovf` (`DEPTH=2`) | 2 | `SIMT_OVF` on a deliberately tiny stack (ISA §2.6 notes it is unreachable at DEPTH=8), reset/launch clearing the stack and mask |
| `fetch` | 12 | sequential PC, `ready` back-pressure, `nested7.S` PC/mask/depth sequence against two independent models, the `ERR_NO_RECOV` kernel (patched image, same trick as `run_tests.py`), `BAR_DIVERGENT` state visibility, illegal PC (`err_imem` + `FAULT_PC` + halt), **17 generated nested if/else kernels** (~580 instructions) diffed against the model (one forced `ERR_NO_RECOV`), the **three EXU handshake styles** the header allows (answer after `br_pending` / same-cycle combinational / level held until the `br_resolved` ack), a sticky external `halt`, a run with random `ready` back-pressure plus a 2-3 cycle instruction-SRAM latency, and the four uniform-branch encodings (`@!p0` resolved in the front end, predicate-all-false / all-true answered by the EXU) |
| `instr_mem` | 4 | preload port (CP `LOAD_KERNEL`) + `load_done` pulse, exactly 1-cycle read latency, out-of-range write (`err_ovf` sticky, SRAM untouched), and the "image at offset 0, PC = entry_off" layout against a real assembled image |
| `fetch_imem` | 2 | `nested7.S` + 4 generated kernels running against the **real** instruction SRAM (the testbench plays `LOAD_KERNEL`), with every fetched instruction word checked against the image |

The `fetch` testbench plays the *environment*, not the answer: a fake instruction
SRAM (1-cycle `imem_req`/`imem_rsp`), a fake decoder that always accepts, and a
fake EXU that answers only what `fetch.sv` delegates -- the branch resolution of
a `br.simt` whose predicate field is not `p0`.  Expectations come from
`tests/core/stack_model.py` (a from-the-document re-implementation of §2.3.3,
with a mini datapath) and, for `nested7.S`, additionally from
`sim/simulator/tests/stack_model_check.py` -- the doc-derived model that already
validates the C++ simulator.  So the RTL is pinned against two independent
transcriptions of the same rules rather than against itself.

### Command processor (`cp`)

| DUT | tests | coverage |
|---|---|---|
| `cp` | 15 | CMDS §7 register file (reset values, RO, write-any-value-clears, `CP_STATUS`), NOP/variable-length walk + `IRQ_ON_DONE`, SIGNAL/WAIT/READ_REG/IRQ/FENCE/PARAMS, queue boundary errors (len<2, past `CMD_TAIL`, reserved opcode, reserved `cmd_flags`), `WAIT` timeout and real `wait_id` blocking, `LOAD_KERNEL` (header + CRC + both SMs' instruction SRAM + `SM_KERNEL_MASK`) followed by LAUNCH to `SM_DONE` (semaphore + `IRQ_LAUNCH_DONE`), CRC/flag mismatch, `LAUNCH_REJECT` / `STALE_SM_ERROR` ordering, SM fault without signalling, GEMM/2D payload dispatch, engine fault, `CP_RESET` semantics, `BUS_ERROR` on a queue read, and the queue-pointer register rules (§2.1/§6.2/§7: `CMD_QUEUE_SIZE` must be `>= 8` and a multiple of 8, `CMD_QUEUE_BASE` 4B-aligned outside the MMIO window, `CMD_HEAD` in-range and 4B-aligned and **host-only**; IDLE-only timing; the command path reports `CMD_ERROR`/`ILLEGAL_STATE` while a host MMIO write is silently ignored) -- including the check that a host write moving `head` back onto already-consumed commands makes the CP **re-read the queue from memory** instead of reusing its line buffer |
| `crc32_word` | 1 | CRC-32/ISO-HDLC word-at-a-time against `zlib.crc32` (standard vector, a real kernel image, 20 random streams) -- this is what makes `LOAD_KERNEL`'s header check trustworthy |

The `cp` testbench plays the environment (see `tests/cp/cp_env.py`): a sparse global
memory that answers the CP's line/word memory protocol, an AXI4-Lite host driver,
two SM mocks (kernel run time, injectable `sm_error`), 2D/GEMM engine mocks that
drain the payload stream, and a model of the instruction-SRAM preload port.
Note the convention it encodes: **a command that ends in an architectural error
leaves the CP in ERROR on purpose**, so such a test waits for `CP_ERROR` instead of
waiting for IDLE.

These seven DUTs are **Verilator-only**: Icarus cannot elaborate them (it rejects
member assignment into a packed-struct array, and `core/decode.sv` already uses
the `inside` operator it lacks) -- see the note in `harness/duts.py`.  Lint the
SV core instead; both new files are warning-clean under `-Wall`:

```bash
tb/.tools/pytools/verilator/bin/verilator --lint-only -Wall \
    -Ihw/rtl -Ihw/rtl/core -Ihw/rtl/mem \
    hw/rtl/core/simt_stack.sv hw/rtl/core/fetch.sv hw/rtl/mem/instr_mem.sv
```

`tb/harness/rtl/` holds **simulation-only glue** — currently just
`fetch_imem_wrap.sv`, which connects `fetch` to `instr_mem` so that one cocotb
DUT can elaborate both. It is not part of the design (the synthesizable wiring
belongs in the SM top level, see `hw/rtl/core/MEM_LSU.md` §4.6).

Tests are written so that a mass of vectors produces a readable failure: the
`Scoreboard` collects mismatches and reports "N of M checks failed" with the
first handful instead of aborting on the first one. Random tests print the
deferred/skipped vector counts so a silently shrinking test is visible.

## 6. Conventions for new tests

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
