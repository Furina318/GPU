"""Registry of RTL modules that have a cocotb testbench.

Add a :class:`Dut` entry per DUT (or per DUT *group*, when several modules must
be elaborated together).  ``tb/run.py --list`` prints this table.


"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .env import RTL_DIR

FPU_DIR = RTL_DIR / "fpu"
CORE_DIR = RTL_DIR / "core"
MEM_DIR = RTL_DIR / "mem"
CP_DIR = RTL_DIR / "cp"
#: 仿真专用胶水(不属于设计本体):tb/harness/rtl/fetch_imem_wrap.sv
TB_RTL_DIR = Path(__file__).resolve().parent / "rtl"
#: ``defines.vh`` 的所在目录 —— 每个 core/mem 模块都要 ``-I`` 它
RTL_INCLUDES = (RTL_DIR, CORE_DIR, MEM_DIR, CP_DIR, TB_RTL_DIR)


@dataclass(frozen=True)
class Dut:
    """Everything the runner needs to elaborate one DUT and run its tests."""

    name: str
    toplevel: str
    sources: tuple[Path, ...]
    test_module: str
    include_dirs: tuple[Path, ...] = ()
    parameters: dict[str, object] = field(default_factory=dict)
    #: Extra args for a specific simulator, keyed by simulator name.
    build_args: dict[str, list[str]] = field(default_factory=dict)

    def extra_args(self, sim: str) -> list[str]:
        return list(self.build_args.get(sim, ()))


FPU_ALL_SOURCES = (
    FPU_DIR / "fpu.v",
    FPU_DIR / "fma_unit.v",
    FPU_DIR / "fdiv.v",
    FPU_DIR / "fsqrt.v",
    FPU_DIR / "fcvt.v",
    FPU_DIR / "fquick.v",
    FPU_DIR / "fpu_decoder.v",
    FPU_DIR / "fpu_arbiter.v",
    FPU_DIR / "frm.v",
)

DUTS: dict[str, Dut] = {
    # -- leaf blocks, purely combinational: cheapest place to start --------
    "fpu_decoder": Dut(
        name="fpu_decoder",
        toplevel="fpu_decoder",
        sources=(FPU_DIR / "fpu_decoder.v",),
        test_module="tests.fpu.test_fpu_decoder",
    ),
    "fcvt": Dut(
        name="fcvt",
        toplevel="fcvt",
        sources=(FPU_DIR / "fcvt.v", FPU_DIR / "frm.v"),
        include_dirs=(FPU_DIR,),
        test_module="tests.fpu.test_fcvt",
    ),
    "fpu_arbiter": Dut(
        name="fpu_arbiter",
        toplevel="fpu_arbiter",
        sources=(FPU_DIR / "fpu_arbiter.v",),
        include_dirs=(FPU_DIR,),
        parameters={"NUM_PORTS": 4, "ID_WIDTH": 5, "DATA_WIDTH": 32},
        test_module="tests.fpu.test_fpu_arbiter",
    ),
    # -- the real thing ---------------------------------------------------
    "fpu": Dut(
        name="fpu",
        toplevel="fpu",
        sources=FPU_ALL_SOURCES,
        include_dirs=(FPU_DIR,),
        parameters={"ID_WIDTH": 5},
        test_module="tests.fpu.test_fpu_top",
    ),
    #: The 5-port arbiter as instantiated inside `fpu` (same RTL, other config).
    "fpu_arbiter_5": Dut(
        name="fpu_arbiter_5",
        toplevel="fpu_arbiter",
        sources=(FPU_DIR / "fpu_arbiter.v",),
        include_dirs=(FPU_DIR,),
        parameters={"NUM_PORTS": 5, "ID_WIDTH": 5, "DATA_WIDTH": 32},
        test_module="tests.fpu.test_fpu_arbiter",
    ),
    # -- shader core front end (SystemVerilog) ------------------------------
    # NOTE: the `core`/`mem` DUTs are Verilator-only.  Icarus Verilog's
    # SystemVerilog support is a subset and cannot elaborate them (it rejects
    # member assignment into a packed-struct array, and it does not implement
    # the `inside` operator that `core/decode.sv` uses), so `--sim icarus`
    # covers the FPU only.  Lint them with, e.g.:
    #   tb/.tools/pytools/verilator/bin/verilator --lint-only -Wall \
    #       -Ihw/rtl -Ihw/rtl/core hw/rtl/core/simt_stack.sv hw/rtl/core/fetch.sv
    "simt_stack": Dut(
        name="simt_stack",
        toplevel="simt_stack",
        sources=(CORE_DIR / "simt_stack.sv",),
        include_dirs=RTL_INCLUDES,
        parameters={"DEPTH": 8, "LANES": 8},
        test_module="tests.core.test_simt_stack",
    ),
    #: SIMT_OVF (ISA §2.6) is unreachable with an 8-deep stack, so the overflow
    #: path is exercised on a deliberately tiny instance instead.
    "simt_stack_ovf": Dut(
        name="simt_stack_ovf",
        toplevel="simt_stack",
        sources=(CORE_DIR / "simt_stack.sv",),
        include_dirs=RTL_INCLUDES,
        parameters={"DEPTH": 2, "LANES": 8},
        test_module="tests.core.test_simt_stack_ovf",
    ),
    "fetch": Dut(
        name="fetch",
        toplevel="fetch",
        sources=(CORE_DIR / "simt_stack.sv", CORE_DIR / "fetch.sv"),
        include_dirs=RTL_INCLUDES,
        test_module="tests.core.test_fetch",
    ),
    # -- memory ------------------------------------------------------------
    "instr_mem": Dut(
        name="instr_mem",
        toplevel="instr_mem",
        sources=(MEM_DIR / "instr_mem.sv",),
        include_dirs=RTL_INCLUDES,
        test_module="tests.core.test_instr_mem",
    ),
    # -- command processor (SystemVerilog) ---------------------------------
    "cp": Dut(
        name="cp",
        toplevel="cp",
        sources=(CP_DIR / "cp_pkg.sv", CP_DIR / "axil_slave.sv", CP_DIR / "cp_regs.sv",
                 CP_DIR / "cmd_decode.sv", CP_DIR / "crc32_word.sv", CP_DIR / "cp.sv"),
        include_dirs=RTL_INCLUDES + (CP_DIR,),
        test_module="tests.cp.test_cp",
    ),
    "crc32_word": Dut(
        name="crc32_word",
        toplevel="crc32_word",
        sources=(CP_DIR / "crc32_word.sv",),
        include_dirs=RTL_INCLUDES + (CP_DIR,),
        test_module="tests.cp.test_crc32",
    ),
    #: `fetch` against the **real** instruction SRAM instead of the testbench's
    #: fake imem; the wrapper is simulation-only glue (see tests/core/fetch_env).
    "fetch_imem": Dut(
        name="fetch_imem",
        toplevel="fetch_imem_wrap",
        sources=(CORE_DIR / "simt_stack.sv", CORE_DIR / "fetch.sv",
                 MEM_DIR / "instr_mem.sv", TB_RTL_DIR / "fetch_imem_wrap.sv"),
        include_dirs=RTL_INCLUDES,
        test_module="tests.core.test_fetch_imem",
    ),
}


def get(name: str) -> Dut:
    try:
        return DUTS[name]
    except KeyError:
        known = ", ".join(sorted(DUTS))
        raise KeyError(f"unknown DUT {name!r}; known: {known}") from None
