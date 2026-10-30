ROOT      := $(abspath ../..)
BUILD     := build
VERILATOR := $(ROOT)/tb/.tools/pytools/verilator/bin/verilator
CXX       ?= g++
CXXFLAGS  ?= -O3 -std=c++17
DEFS      :=
VERILATOR_FLAGS := -O3 -Wno-fatal --threads 2 --build-jobs 4 --x-assign fast --x-initial fast

# 波形支持: TRACE=fst(默认, --fst 可用) / TRACE=vcd(用 --vcd) / TRACE=off(不编)
TRACE ?= fst
ifeq ($(TRACE),fst)
  LZ4_DEV := $(firstword $(wildcard /usr/include/lz4.h /usr/local/include/lz4.h))
  ifeq ($(LZ4_DEV),)
    $(info [run-sim] 没找到 <lz4.h>: 退回 VCD 波形(装 liblz4-dev 可启用 FST), 用 --vcd 导出)
    TRACE := vcd
  endif
endif
ifeq ($(TRACE),fst)
  VERILATOR_FLAGS += --trace-fst
  DEFS            += -DRUNSIM_FST
else ifeq ($(TRACE),vcd)
  VERILATOR_FLAGS += --trace
  DEFS            += -DRUNSIM_VCD
endif

# SDL2 窗口(可选): 有就支持 --view, 没有也能编译(运行时提示装 libsdl2-dev)
SDL_CFLAGS := $(shell pkg-config --cflags sdl2 2>/dev/null)
SDL_LIBS   := $(shell pkg-config --libs sdl2 2>/dev/null)
ifneq ($(strip $(SDL_LIBS)),)
  DEFS    += -DRUNSIM_HAVE_SDL $(SDL_CFLAGS)
  LDFLAGS += $(SDL_LIBS)
else
  $(info [run-sim] 未找到 SDL2(pkg-config sdl2): --view 不可用, 其余功能正常)
endif

RTL_INC := -I$(ROOT)/hw/rtl -I$(ROOT)/hw/rtl/core -I$(ROOT)/hw/rtl/mem \
           -I$(ROOT)/hw/rtl/cp -I$(ROOT)/hw/rtl/fpu -I$(ROOT)/tb/harness/rtl

SRCS := \
  $(ROOT)/hw/rtl/core/core_pkg.sv \
  $(ROOT)/hw/rtl/core/alu.sv $(ROOT)/hw/rtl/core/mdu.sv \
  $(ROOT)/hw/rtl/core/exu.sv $(ROOT)/hw/rtl/core/fetch.sv \
  $(ROOT)/hw/rtl/core/simt_stack.sv $(ROOT)/hw/rtl/core/decode.sv \
  $(ROOT)/hw/rtl/core/regfile.sv $(ROOT)/hw/rtl/core/wbu.sv \
  $(ROOT)/hw/rtl/core/lsu.sv $(ROOT)/hw/rtl/core/lsu_coalescer.sv \
  $(ROOT)/hw/rtl/core/lsu_scheduler.sv $(ROOT)/hw/rtl/core/core.sv \
  $(ROOT)/hw/rtl/mem/mem_map.sv $(ROOT)/hw/rtl/mem/shared_mem.sv \
  $(ROOT)/hw/rtl/mem/instr_mem.sv $(ROOT)/hw/rtl/mem/global_mem.sv \
  $(ROOT)/hw/rtl/mem/wcb.sv $(ROOT)/hw/rtl/mem/axi_arb.sv \
  $(ROOT)/hw/rtl/mem/cp_mem_adapter.sv $(ROOT)/hw/rtl/mem/mem_perf.sv \
  $(ROOT)/hw/rtl/fpu/fpu.v $(ROOT)/hw/rtl/fpu/fma_unit.v \
  $(ROOT)/hw/rtl/fpu/fdiv.v $(ROOT)/hw/rtl/fpu/fsqrt.v \
  $(ROOT)/hw/rtl/fpu/fcvt.v $(ROOT)/hw/rtl/fpu/fquick.v \
  $(ROOT)/hw/rtl/fpu/fpu_arbiter.v $(ROOT)/hw/rtl/fpu/frm.v \
  $(ROOT)/hw/rtl/cp/cp_pkg.sv $(ROOT)/hw/rtl/cp/axil_slave.sv \
  $(ROOT)/hw/rtl/cp/cp_regs.sv $(ROOT)/hw/rtl/cp/cmd_decode.sv \
  $(ROOT)/hw/rtl/cp/crc32_word.sv $(ROOT)/hw/rtl/cp/cp.sv \
  $(ROOT)/tb/harness/rtl/axi_ram_model.sv \
  rtl/gpu_system.sv

OBJ_DIR := $(BUILD)/obj_dir
BIN     := $(BUILD)/run_sim

# 别名的默认参数(只有别名用得到; make run 完全走 ARGS)
WARPS  ?= 4
W      ?= 32
H      ?= 32
BOX    ?= 6
SCALE  ?= 8
FPS    ?= 30
FRAMES ?=
ARGS   ?=

# 运行入口: 不 cd(镜像由测试台自己按名字找), 参数原样透传
RUN := $(abspath $(BIN))

.PHONY: all run lint clear gouraud matmul anim check demo demo-headless clean images

all: $(BIN)

$(BIN): rtl/gpu_system.sv tb_main.cpp $(SRCS)
	@mkdir -p $(BUILD)
	$(VERILATOR) --cc --exe --build $(VERILATOR_FLAGS) \
	    --top-module gpu_system $(RTL_INC) \
	    --Mdir $(OBJ_DIR) -o $(abspath $(BIN)) \
	    $(SRCS) tb_main.cpp \
	    -CFLAGS "$(CXXFLAGS) $(DEFS)" -LDFLAGS "$(LDFLAGS)"

lint:
	$(VERILATOR) --lint-only -Wall --top-module gpu_system $(RTL_INC) $(SRCS)

images:
	$(MAKE) -C $(ROOT)/sim/simulator images

run: $(BIN)
	$(RUN) $(ARGS)

# 便捷别名
clear: $(BIN)
	$(RUN) --image clear --warps $(WARPS) $(ARGS)

gouraud: $(BIN)
	$(RUN) --image gouraud --warps $(WARPS) $(ARGS)

matmul: $(BIN)
	$(RUN) --image matmul --warps $(WARPS) $(ARGS)

anim: $(BIN)
	$(RUN) --image anim --warps $(WARPS) --w $(W) --h $(H) \
	    --box $(BOX) $(if $(FRAMES),--frames $(FRAMES),--frames 4) $(ARGS)

# 动画 demo: SDL 窗口逐帧重算画面(关窗或按 ESC 结束)
demo: $(BIN)
	$(RUN) --image anim --warps $(WARPS) --w $(W) --h $(H) \
	    --box $(BOX) --scale $(SCALE) --fps $(FPS) --view $(if $(FRAMES),--frames $(FRAMES)) $(ARGS)

# 无窗口版本: 逐帧跑并与参考模型对拍
demo-headless: $(BIN)
	$(RUN) --image anim --warps $(WARPS) --w $(W) --h $(H) \
	    --box $(BOX) $(if $(FRAMES),--frames $(FRAMES),--frames 60) $(ARGS)

check:
	$(MAKE) clear
	$(MAKE) gouraud
	$(MAKE) matmul
	$(MAKE) anim

clean:
	rm -rf $(BUILD)
