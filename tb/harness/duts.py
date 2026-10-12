"""Registry of RTL modules that have a cocotb testbench.

Add a :class:`Dut` entry per DUT (or per DUT *group*, when several modules must
be elaborated together).  ``tb/run.py --list`` prints this table.

Only modules that actually exist and elaborate are registered.  The project is
still mid-build -- ``hw/rtl/core/{core,exu,fetch}.sv`` are empty and
``hw/rtl/mem/axi_arb.sv`` is a stub -- so the FPU is the first complete,
self-contained block available to verify.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .env import RTL_DIR

FPU_DIR = RTL_DIR / "fpu"


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
}


def get(name: str) -> Dut:
    try:
        return DUTS[name]
    except KeyError:
        known = ", ".join(sorted(DUTS))
        raise KeyError(f"unknown DUT {name!r}; known: {known}") from None
