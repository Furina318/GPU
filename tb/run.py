#!/usr/bin/env python3
"""Build and run the cocotb testbenches under ``tb/``.

Examples
--------
List the registered DUTs::

    python3 tb/run.py --list

Run every test of one DUT::

    python3 tb/run.py --dut fpu

Run one test by name (regex), with waveforms::

    python3 tb/run.py --dut fpu --test test_fma_random --waves

Everything, on both simulators::

    python3 tb/run.py --all --sim verilator
    python3 tb/run.py --all --sim icarus
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

TB_DIR = Path(__file__).resolve().parent
# cocotb imports test modules by name, and forwards ``sys.path`` to the
# simulator process via PYTHONPATH -- so this must happen before build()/test().
if str(TB_DIR) not in sys.path:
    sys.path.insert(0, str(TB_DIR))

from harness import duts as dut_registry  # noqa: E402
from harness.env import (  # noqa: E402
    BUILD_ROOT,
    SUPPORTED_SIMS,
    apply_env,
    check_sim,
    describe,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="tb/run.py",
        description="Build and run the cocotb testbenches for this project.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    selection = parser.add_argument_group("selection")
    selection.add_argument(
        "-d", "--dut", action="append", default=None, help="DUT to test (repeatable)"
    )
    selection.add_argument(
        "--all", action="store_true", help="test every registered DUT"
    )
    selection.add_argument(
        "--list", action="store_true", help="list registered DUTs and exit"
    )
    selection.add_argument(
        "-t", "--test", default=None, help="regex filter on cocotb test names"
    )

    run = parser.add_argument_group("run control")
    run.add_argument(
        "-s",
        "--sim",
        default="verilator",
        choices=SUPPORTED_SIMS,
        help="simulator backend (default: verilator)",
    )
    run.add_argument("-w", "--waves", action="store_true", help="dump waveforms")
    run.add_argument(
        "--seed", type=int, default=None, help="cocotb random seed (for repro)"
    )
    run.add_argument(
        "--rebuild",
        action="store_true",
        help="force a rebuild even if the sources are unchanged",
    )
    run.add_argument(
        "--keep-going",
        action="store_true",
        help="with --all, keep testing other DUTs after a failure",
    )
    run.add_argument(
        "--werror",
        action="store_true",
        help="treat Verilator lint warnings as errors (strict mode)",
    )
    return parser.parse_args(argv)


def print_duts() -> None:
    print(f"{'DUT':<18} {'TOPLEVEL':<16} {'TESTS':<32} SOURCES")
    print("-" * 92)
    for name in sorted(dut_registry.DUTS):
        dut = dut_registry.DUTS[name]
        print(
            f"{dut.name:<18} {dut.toplevel:<16} {dut.test_module:<32} "
            f"{len(dut.sources)} file(s)"
        )


def run_one(name: str, args: argparse.Namespace) -> bool:
    """Build and test a single DUT.  Returns ``True`` on success."""
    from cocotb_tools.runner import get_runner

    dut = dut_registry.get(name)
    apply_env(args.sim)
    check_sim(args.sim)

    build_dir = BUILD_ROOT / args.sim / dut.name
    runner = get_runner(args.sim)

    build_kwargs: dict[str, object] = {}
    if args.sim == "icarus":
        # Icarus defaults to 1 s precision unless told otherwise, which makes
        # every cocotb timer with a ns period illegal.
        build_kwargs["timescale"] = ("1ns", "1ps")

    # Verilator's lint warnings are valuable but should not block the test flow
    # by default -- e.g. fpu_arbiter.v:73 indexes a 4-bit grant_o with a 3-bit
    # best_idx (WIDTHTRUNC).  See the "RTL findings" section of tb/README.md.
    build_args = dut.extra_args(args.sim)
    if args.sim == "verilator":
        build_args += ["-Wno-fatal"] if not args.werror else []

    print(f"\n=== {dut.name} on {args.sim} " + "=" * (40 - len(dut.name)))
    print(f"    {describe(args.sim)}")
    print(f"    toplevel : {dut.toplevel}")
    print(f"    build dir: {build_dir}")
    if dut.parameters:
        print(f"    params   : {dut.parameters}")

    started = time.time()
    runner.build(
        sources=list(dut.sources),
        includes=list(dut.include_dirs),
        hdl_toplevel=dut.toplevel,
        build_dir=build_dir,
        parameters=dut.parameters or {},
        build_args=build_args,
        always=args.rebuild,
        waves=args.waves,
        **build_kwargs,
    )
    runner.test(
        hdl_toplevel=dut.toplevel,
        test_module=dut.test_module,
        build_dir=build_dir,
        test_filter=args.test,
        waves=args.waves,
        seed=args.seed,
    )

    # cocotb returns normally even when tests fail, so the pass/fail verdict has
    # to come from the JUnit XML it wrote.
    from cocotb_tools.runner import get_results

    num_tests, num_failed = get_results(build_dir / "results.xml")
    elapsed = time.time() - started
    if num_failed:
        raise RuntimeError(f"{num_failed} of {num_tests} test(s) FAILED")
    if num_tests == 0:
        raise RuntimeError(
            "no tests ran -- check the --test filter and the test module name"
        )
    print(f"    {num_tests} test(s) passed in {elapsed:.1f}s")
    return True


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.list:
        print_duts()
        return 0

    if args.all:
        names = sorted(dut_registry.DUTS)
    elif args.dut:
        names = args.dut
    else:
        print("nothing to do: pass --dut NAME, --all, or --list\n", file=sys.stderr)
        print_duts()
        return 2

    failures: list[str] = []
    for name in names:
        try:
            run_one(name, args)
        except Exception as exc:  # noqa: BLE001 - report and continue
            import traceback

            failures.append(name)
            print(f"\n!!! {name} FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
            traceback.print_exc()
            if not args.keep_going and len(names) > 1:
                break

    print("\n" + "=" * 60)
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        return 1
    print(f"OK: {len(names)} DUT(s) passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
