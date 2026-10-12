"""Reusable cocotb building blocks for this project's testbenches.

The layering deliberately mirrors what a UVM environment would give you, so
that a later migration (or a side-by-side comparison) is mechanical:

======================  ===============================================
UVM concept             here
======================  ===============================================
``uvm_agent``           :class:`FpuAgent` (driver + monitor)
``uvm_driver``          :meth:`FpuAgent.send`
``uvm_monitor``         :meth:`FpuAgent._monitor`
``uvm_scoreboard``      :class:`Scoreboard`
``uvm_sequence_item``   :class:`FpuTxn`
``uvm_sequence``        generators in ``harness/lib/stimulus.py``
``uvm_config_db``       :mod:`harness.env` / :mod:`harness.duts`
``uvm_test``            the ``@cocotb.test()`` coroutines under ``tests/``
======================  ===============================================

Nothing here is FPU-specific except :class:`FpuAgent`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, ReadOnly, RisingEdge, Timer

DEFAULT_CLK_NS = 10


# --------------------------------------------------------------------------
# clock / reset
# --------------------------------------------------------------------------
def start_clock(clk, period_ns: float = DEFAULT_CLK_NS):
    """Start a free-running clock and return the task handle."""
    return cocotb.start_soon(Clock(clk, period_ns, unit="ns").start())


async def reset_dut(dut, rst_name: str = "rst", cycles: int = 3, clk=None):
    """Assert reset for ``cycles`` clock edges, then release it."""
    clk = clk if clk is not None else dut.clk
    rst = getattr(dut, rst_name)
    rst.value = 1
    for _ in range(cycles):
        await RisingEdge(clk)
    await FallingEdge(clk)
    rst.value = 0
    await RisingEdge(clk)


# --------------------------------------------------------------------------
# combinational DUT helper
# --------------------------------------------------------------------------
async def settle(ns: float = 1.0) -> None:
    """Let a purely combinational DUT's outputs settle before sampling."""
    await Timer(ns, unit="ns")


# --------------------------------------------------------------------------
# transactions / scoreboard
# --------------------------------------------------------------------------
@dataclass
class FpuTxn:
    """One request handed to the FPU (a UVM ``sequence_item``)."""

    op: int
    src1: int = 0
    src2: int = 0
    src3: int = 0
    rm: int = 0
    tag: int = 0
    #: Human-readable origin, used in failure messages.
    note: str = ""
    #: Filled in once the monitor sees the writeback.
    result: int | None = None
    fflags: int | None = None

    def summary(self) -> str:
        return (
            f"{self.note or 'txn'} op=0b{self.op:07b} "
            f"src1=0x{self.src1:08x} src2=0x{self.src2:08x} src3=0x{self.src3:08x} "
            f"rm={self.rm}"
        )


class Scoreboard:
    """Collects mismatches instead of failing on the first one.

    A test that checks thousands of vectors is far more useful when it reports
    "17 of 4000 mismatches, here are the first 10" than when it aborts on the
    first mismatch.
    """

    def __init__(self, name: str, max_reported: int = 10):
        self.name = name
        self.checks = 0
        self.errors: list[str] = []
        self.max_reported = max_reported

    def check(self, got, exp, ctx: str) -> None:
        self.checks += 1
        if got != exp:
            self.errors.append(f"{ctx}: got 0x{got:08x} expected 0x{exp:08x}")

    def check_int(self, got, exp, ctx: str) -> None:
        self.checks += 1
        if got != exp:
            self.errors.append(f"{ctx}: got {got} expected {exp}")

    def check_equal(self, got, exp, ctx: str) -> None:
        self.checks += 1
        if got != exp:
            self.errors.append(f"{ctx}: got {got!r} expected {exp!r}")

    def check_flags(self, got: int, exp: int, ctx: str) -> None:
        """Compare a 5-bit fflags vector, reporting both in binary."""
        self.checks += 1
        if got != exp:
            self.errors.append(
                f"{ctx}: got {got:05b} expected {exp:05b} "
                f"(NV DZ OF UF NX; XOR={got ^ exp:05b})"
            )

    def report(self) -> None:
        """Raise an ``AssertionError`` describing every mismatch, if any."""
        n_bad = len(self.errors)
        if not n_bad:
            return
        head = "\n".join(f"    {e}" for e in self.errors[: self.max_reported])
        more = (
            f"\n    ... and {n_bad - self.max_reported} more"
            if n_bad > self.max_reported
            else ""
        )
        raise AssertionError(
            f"[{self.name}] {n_bad} of {self.checks} checks failed:\n{head}{more}"
        )

    def log_summary(self, dut) -> None:
        dut._log.info(f"[{self.name}] {self.checks} checks, {len(self.errors)} failures")


# --------------------------------------------------------------------------
# FPU agent: driver + monitor
# --------------------------------------------------------------------------
class FpuAgent:
    """Drives the ``fpu`` top level's single request port and watches writeback.

    The DUT under test is expected to expose::

        clk, rst, valid, ready, fpu_op, rm, src1, src2, src3,
        flush, flush_id, fpu_id -> fpu_valid, fpu_id_o, fpu_result, fpu_fflags

    Driving happens on the *falling* edge and sampling on the rising edge, so
    the driver and the monitor never race within a cycle.
    """

    def __init__(self, dut, clk_period_ns: float = DEFAULT_CLK_NS):
        self.dut = dut
        self.clk = dut.clk
        self.clk_period_ns = clk_period_ns
        #: flat log of every writeback the monitor observed
        self.observed: list[tuple[int, int, int]] = []
        #: how many write-backs carried X/Z (a 4-state simulator only)
        self.x_count = 0
        self._results: dict[int, tuple[int, int]] = {}
        self._next_tag = 0
        self._monitor_task = None

    # -- lifecycle --------------------------------------------------------
    async def start(self) -> None:
        """Initialise the DUT, start the clock and the monitor."""
        d = self.dut
        d.valid.value = 0
        d.fpu_op.value = 0
        d.rm.value = 0
        d.src1.value = 0
        d.src2.value = 0
        d.src3.value = 0
        d.flush.value = 0
        d.flush_id.value = 0
        d.fpu_id.value = 0
        start_clock(self.clk, self.clk_period_ns)
        await reset_dut(self.dut, clk=self.clk)
        self._monitor_task = cocotb.start_soon(self._monitor())

    async def stop(self) -> None:
        if self._monitor_task is not None:
            self._monitor_task.kill()
            self._monitor_task = None

    # -- monitor ----------------------------------------------------------
    async def _monitor(self) -> None:
        d = self.dut
        while True:
            await RisingEdge(self.clk)
            await ReadOnly()
            try:
                if int(d.fpu_valid.value) == 1:
                    tag = int(d.fpu_id_o.value)
                    res = int(d.fpu_result.value)
                    ffl = int(d.fpu_fflags.value)
                    self._results[tag] = (res, ffl)
                    self.observed.append((tag, res, ffl))
            except ValueError:
                # Signal is X/Z (only a 4-state simulator can produce this).
                # Surface it loudly instead of losing the write-back silently.
                self.x_count += 1
                self.dut._log.warning(
                    f"write-back with X/Z on the bus at "
                    f"{cocotb.utils.get_sim_time(unit='ns')} ns "
                    f"(fpu_valid={d.fpu_valid.value!s} "
                    f"fpu_id_o={d.fpu_id_o.value!s} "
                    f"fpu_result={d.fpu_result.value!s}) "
                    f"-- total {self.x_count}"
                )
                self._results[-1] = (-1, -1)
                self.observed.append((-1, -1, -1))

    # -- driver -----------------------------------------------------------
    def alloc_tag(self) -> int:
        """Return the next 5-bit instruction id, wrapping around."""
        tag = self._next_tag & 0x1F
        self._next_tag += 1
        return tag

    async def send(self, txn: FpuTxn) -> None:
        """Hand ``txn`` to the request port, holding ``valid`` until accepted.

        ``fpu.ready`` is *per op*: the FPU refuses a divide or square root only
        while the corresponding single-slot unit is busy, and keeps accepting
        everything else.  So the opcode has to be on the port before ``ready``
        means anything -- present first, then wait for the accept.
        """
        d = self.dut
        # Tags are 5 bits and wrap, so drop any stale writeback recorded for
        # this tag on an earlier lap before requesting it again.
        self._results.pop(txn.tag, None)

        await FallingEdge(self.clk)
        d.fpu_op.value = txn.op
        d.rm.value = txn.rm
        d.src1.value = txn.src1
        d.src2.value = txn.src2
        d.src3.value = txn.src3
        d.fpu_id.value = txn.tag
        d.valid.value = 1

        # Hold valid across rising edges until one is accepted (ready high).
        while int(d.ready.value) == 0:
            await RisingEdge(self.clk)

        await FallingEdge(self.clk)
        d.valid.value = 0

    async def wait_result(self, tag: int, timeout_ns: float = 2000.0):
        """Block until the monitor sees a writeback with ``tag``."""
        budget = max(int(timeout_ns / self.clk_period_ns), 4)
        for _ in range(budget):
            if tag in self._results:
                return self._results[tag]
            await RisingEdge(self.clk)
        raise TimeoutError(
            f"no writeback for tag {tag} within {timeout_ns} ns "
            f"({len(self.observed)} writebacks seen: {self.observed[:8]})"
        )

    async def run(self, txn: FpuTxn, timeout_ns: float = 2000.0):
        """Send one request and return its ``(result, fflags)``."""
        if not txn.tag:
            txn.tag = self.alloc_tag()
        await self.send(txn)
        res, ffl = await self.wait_result(txn.tag, timeout_ns)
        txn.result, txn.fflags = res, ffl
        return res, ffl

    # -- convenience ------------------------------------------------------
    async def flush(self, tag: int) -> None:
        """Pulse ``flush``/``flush_id`` for one cycle."""
        d = self.dut
        await FallingEdge(self.clk)
        d.flush.value = 1
        d.flush_id.value = tag
        await FallingEdge(self.clk)
        d.flush.value = 0
        d.flush_id.value = 0

    def saw_tag(self, tag: int) -> bool:
        return tag in self._results

    def forget(self, tag: int) -> None:
        """Drop any writeback recorded for ``tag`` (tags are 5 bits and wrap)."""
        self._results.pop(tag, None)
