#!/usr/bin/env python3
"""Install the pinned Verilator into ``tb/.tools`` -- workspace-local, no sudo.

Why this is needed
------------------
The system Verilator here is 5.020, but cocotb 2.x's VPI shim calls
``VerilatedVpi::clearEvalNeeded()`` / ``doInertialPuts()``, which only exist in
Verilator >= 5.036.  Rather than fight a system package, this installs a
recent Verilator from PyPI into ``tb/.tools/pytools`` and :mod:`harness.env`
puts it ahead of the system one on ``PATH``.

Usage::

    make -C tb setup          # or
    python3 tb/setup_tools.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

TB_DIR = Path(__file__).resolve().parent
if str(TB_DIR) not in sys.path:
    sys.path.insert(0, str(TB_DIR))

from harness import env  # noqa: E402

#: Anything >= 5.036 works with cocotb 2.x.
PIN = "verilator>=5.36"

#: Marker so the patch below is applied exactly once.
_PCH_PATCH_MARKER = "# Patched by tb/setup_tools.py."

_PCH_PATCH = f"""

######################################################################
{_PCH_PATCH_MARKER}
#
# The PyPI wheel ships a verilated.mk whose precompiled-header configuration
# was generated without usable values (CFG_CXXFLAGS_PCH_I and CFG_GCH_IF_CLANG
# are both empty), so the compile rules receive a bare "Vtop__pch.h.fast"
# argument.  Depending on the C++ compiler this fails either as a missing input
# file (clang: "linker input file not found") or as "-pch: No such file or
# directory" (gcc, which parses "-include-pch" as "-include -pch").  Designs
# small enough to compile as a single translation unit never hit it, which is
# why it only shows up once a DUT is big enough for Verilator to split the
# output.
#
# Rather than guess which C++ compiler will be used and encode the matching
# flags, stop *using* the precompiled header: each translation unit compiles
# the generated header itself, which is exactly what Verilator's non-PCH build
# path does.  The cost is a slightly slower build; the benefit is that the
# toolchain works with either gcc or clang.
VK_PCH_I_FAST =
VK_PCH_I_SLOW =
"""


def _patch_verilated_mk() -> str | None:
    """Neutralise PCH in the installed wheel's ``verilated.mk``.

    Returns a description of what happened, or ``None`` if there is nothing to
    patch (e.g. an older wheel that does not use PCH this way).
    """
    mk = env.PYTOOLS_DIR / "verilator" / "include" / "verilated.mk"
    if not mk.is_file():
        return None
    text = mk.read_text()
    if _PCH_PATCH_MARKER in text:
        return f"PCH already neutralised in {mk}"
    if "VK_PCH_I_FAST" not in text:
        return None
    mk.write_text(text + _PCH_PATCH)
    return f"neutralised PCH in {mk}"


def main() -> int:
    env.PYTOOLS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"installing {PIN} into {env.PYTOOLS_DIR} ...")
    cmd = [
        sys.executable, "-m", "pip", "install",
        "--target", str(env.PYTOOLS_DIR),
        "--upgrade",
        PIN,
    ]
    proc = subprocess.run(cmd)
    if proc.returncode != 0:
        print(
            "\npip failed. If the machine is offline, install Verilator >= 5.036 "
            "by other means (e.g. `sudo apt install verilator` on a newer distro, "
            "or build from source) and re-run; tb/run.py will pick up whatever "
            "`verilator` is first on PATH.",
            file=sys.stderr,
        )
        return proc.returncode

    # Create the `python` shim and the ccache dirs the build needs.
    env.apply_env("verilator")
    patch_note = _patch_verilated_mk()
    if patch_note:
        print(patch_note)
    print("\n" + env.describe("verilator"))
    ver = env.verilator_version()
    if ver is not None and ver < env.MIN_VERILATOR_FOR_COCOTB2:
        print(
            f"WARNING: resolved Verilator {ver[0]}.{ver[1]} is still older than "
            f"{env.MIN_VERILATOR_FOR_COCOTB2[0]}.{env.MIN_VERILATOR_FOR_COCOTB2[1]}",
            file=sys.stderr,
        )
        return 1
    print("toolchain ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
