"""Toolchain discovery and environment fix-ups for the cocotb testbenches.

This module exists because this machine needs three non-obvious adjustments
before ``cocotb + verilator`` will build anything:

1. The system Verilator (5.020) is *older* than the minimum supported by
   cocotb 2.x, whose VPI shim calls ``VerilatedVpi::clearEvalNeeded()`` /
   ``doInertialPuts()`` -- APIs that do not exist in 5.020.  A newer Verilator
   (5.48) is installed from PyPI into ``tb/.tools`` by ``make -C tb setup``.
   That wheel needs ``VERILATOR_ROOT`` pointing at its own ``verilator/``
   subdirectory and its ``bin/`` on ``PATH``.

2. That same wheel's generated ``verilated.mk`` ends with ``PYTHON3 = python``,
   but Debian/Ubuntu only ship ``python3``.  Without a ``python`` executable
   make dies with ``Error 127`` while generating ``<Top>__ALL.cpp``.  We create
   a private shim directory containing ``python -> python3``.

3. ``USE_CCACHE=1`` is exported globally here, so Verilator wraps the C++
   compiler in ccache, which then fails to create its temp dir under the
   read-only ``/run/user/1000``.  We redirect both ccache dirs into
   ``tb/.tools/cache``.

Everything is local to the workspace: no ``sudo``, no writes outside ``tb/``.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

TB_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = TB_DIR.parent

TOOLS_DIR = TB_DIR / ".tools"
PYTOOLS_DIR = TOOLS_DIR / "pytools"
SHIM_DIR = TOOLS_DIR / "shim"
CACHE_DIR = TOOLS_DIR / "cache"
BUILD_ROOT = TB_DIR / "build"

RTL_DIR = REPO_ROOT / "hw" / "rtl"

#: Simulators this harness knows how to drive, best first.
SUPPORTED_SIMS = ("verilator", "icarus")

#: Minimum Verilator version usable with cocotb 2.x (VPI API break).
MIN_VERILATOR_FOR_COCOTB2 = (5, 36)


def _pyverilator_root() -> Path | None:
    """Return the ``VERILATOR_ROOT`` of the PyPI-installed Verilator, if any."""
    root = PYTOOLS_DIR / "verilator"
    if (root / "bin" / "verilator").is_file():
        return root
    return None


def _ensure_python_shim() -> Path | None:
    """Create ``<shim>/python`` so makefiles that hardcode ``python`` work."""
    target = shutil.which("python3") or sys.executable
    if not target:
        return None
    SHIM_DIR.mkdir(parents=True, exist_ok=True)
    link = SHIM_DIR / "python"
    try:
        if link.is_symlink() or link.exists():
            if link.resolve() == Path(target).resolve():
                return SHIM_DIR
            link.unlink()
        link.symlink_to(target)
    except OSError:
        # Fall back to a tiny exec wrapper if symlinks are unavailable.
        link.write_text(f'#!/bin/sh\nexec "{target}" "$@"\n')
        link.chmod(0o755)
    return SHIM_DIR


def _ensure_ccache_dirs() -> None:
    """Keep ccache inside the workspace when the system asks for it."""
    if not os.environ.get("USE_CCACHE"):
        return
    if os.environ.get("CCACHE_DIR") and os.environ.get("CCACHE_TEMPDIR"):
        return
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_DIR / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    # CCACHE_TEMPDIR (not CCACHE_TMPDIR) is the variable ccache 4.x honours.
    os.environ.setdefault("CCACHE_DIR", str(CACHE_DIR / "ccache"))
    os.environ.setdefault("CCACHE_TEMPDIR", str(tmp))


def _prepend_path(*entries: Path) -> None:
    parts = [str(e) for e in entries if e is not None]
    current = os.environ.get("PATH", "")
    existing = current.split(os.pathsep) if current else []
    fresh = [p for p in parts if p not in existing]
    if fresh:
        os.environ["PATH"] = os.pathsep.join(fresh + existing)


def _version_from_distinfo() -> tuple[int, int] | None:
    """Read the pinned Verilator's version from its ``.dist-info`` directory.

    The PyPI wheel's ``verilator --version`` prints an unversioned
    ``rev vUNKNOWN-built...`` banner, so the usual parse fails and we fall back
    to the installed distribution metadata.
    """
    for dist in PYTOOLS_DIR.glob("verilator-*.dist-info"):
        m = re.match(r"verilator-(\d+)\.(\d+)", dist.name)
        if m:
            return int(m.group(1)), int(m.group(2))
    return None


def verilator_version() -> tuple[int, int] | None:
    """Parse the version of whichever ``verilator`` is first on ``PATH``."""
    exe = shutil.which("verilator")
    if not exe:
        return None
    # Prefer metadata when we are using the pinned PyPI wheel.
    try:
        if Path(exe).resolve().is_relative_to(PYTOOLS_DIR.resolve()):
            pinned = _version_from_distinfo()
            if pinned is not None:
                return pinned
    except (OSError, ValueError):
        pass
    try:
        out = subprocess.run(
            [exe, "--version"], capture_output=True, text=True, timeout=60
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"Verilator\s+(\d+)\.(\d+)", out)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def apply_env(sim: str) -> dict[str, str]:
    """Mutate ``os.environ`` so that ``sim`` can build, and return the changes.

    The returned mapping is handy for logging what the harness actually did.
    """
    if sim not in SUPPORTED_SIMS:
        raise ValueError(f"unsupported simulator {sim!r}; pick one of {SUPPORTED_SIMS}")

    before = dict(os.environ)
    changes: dict[str, str] = {}

    # 1. Prefer the pinned PyPI Verilator over any older system one.
    root = _pyverilator_root()
    if root is not None:
        os.environ["VERILATOR_ROOT"] = str(root)
        _prepend_path(root / "bin")

    # 2. Guarantee a `python` for Verilator-generated makefiles.
    shim = _ensure_python_shim()
    if shim is not None:
        _prepend_path(shim)

    # 3. Keep ccache writable.
    _ensure_ccache_dirs()

    for key, value in os.environ.items():
        if before.get(key) != value:
            changes[key] = value
    return changes


def describe(sim: str) -> str:
    """Human-readable one-liner about which tools a run will use."""
    if sim == "verilator":
        exe = shutil.which("verilator")
        ver = verilator_version()
        where = f"{exe}" if exe else "<not found>"
        shown = f"{ver[0]}.{ver[1]}" if ver else "unknown version"
        return f"verilator: {where} ({shown})"
    if sim == "icarus":
        return f"icarus: {shutil.which('iverilog') or '<not found>'}"
    return sim


def check_sim(sim: str) -> None:
    """Fail early, with actionable advice, when a simulator is unusable."""
    if sim == "verilator":
        exe = shutil.which("verilator")
        if not exe:
            raise RuntimeError(
                "verilator not found on PATH.\n"
                "Run `make -C tb setup` to install the pinned PyPI Verilator, "
                "or install Verilator >= 5.036 system-wide."
            )
        ver = verilator_version()
        if ver is not None and ver < MIN_VERILATOR_FOR_COCOTB2:
            import cocotb

            major = int(cocotb.__version__.split(".")[0])
            if major >= 2:
                raise RuntimeError(
                    f"Verilator {ver[0]}.{ver[1]} is too old for cocotb "
                    f"{cocotb.__version__} (needs >= "
                    f"{MIN_VERILATOR_FOR_COCOTB2[0]}.{MIN_VERILATOR_FOR_COCOTB2[1]}).\n"
                    "Run `make -C tb setup`, which installs Verilator 5.48 from "
                    "PyPI into tb/.tools and puts it ahead of the system one."
                )
    elif sim == "icarus":
        if not shutil.which("iverilog"):
            raise RuntimeError("iverilog not found on PATH (apt install iverilog)")
    else:
        raise ValueError(f"unsupported simulator {sim!r}")
