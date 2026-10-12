# cocotb testbench entry points for the SimpleGPU project.
#
#   make            # same as `make test`
#   make setup      # one-time: install the pinned Verilator into tb/.tools
#   make test       # run every registered DUT on Verilator
#   make list       # show registered DUTs
#   make dut DUT=fpu
#   make waves DUT=fpu
#   make icarus     # run everything on Icarus Verilog instead
#   make clean      # drop build artefacts
#
# All paths are relative to the repository root; TB is this directory.

TB      := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))
REPO    := $(patsubst %/,%,$(dir $(TB)))
PYTHON  ?= python3
RUN     := $(PYTHON) $(TB)/run.py

# Default DUT for the single-target convenience rules.
DUT     ?= fpu
SIM     ?= verilator
TEST    ?=
WAVES   ?=

RUNFLAGS := --sim $(SIM)
ifneq ($(TEST),)
RUNFLAGS += --test $(TEST)
endif
ifneq ($(WAVES),)
RUNFLAGS += --waves
endif

.PHONY: all test setup list dut waves icarus clean icarus-all help

all: test

help:
	@grep -E '^#   ' $(MAKEFILE_LIST) | sed 's/^#   //'

# -- one-time toolchain bootstrap -----------------------------------------
setup:
	$(PYTHON) $(TB)/setup_tools.py

# -- run -------------------------------------------------------------------
test:
	$(RUN) --all $(RUNFLAGS)

dut:
	$(RUN) --dut $(DUT) $(RUNFLAGS)

waves:
	$(RUN) --dut $(DUT) --waves $(filter-out --waves,$(RUNFLAGS))

list:
	$(RUN) --list

icarus:
	$(RUN) --all --sim icarus $(filter-out --sim $(SIM),$(RUNFLAGS))

# -- maintenance -----------------------------------------------------------
clean:
	rm -rf $(TB)/build
	@echo "removed $(TB)/build"

# Nuke the local toolchain too (re-run `make setup` afterwards).
distclean: clean
	rm -rf $(TB)/.tools
	@echo "removed $(TB)/.tools"
