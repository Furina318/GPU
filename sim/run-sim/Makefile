# sim/run-sim/Makefile —— RTL 全系统仿真的入口
#
#   make            编译 sim/run-sim/build/run_sim(Verilator, 约 5 分钟)
#   make clear      跑 clear 内核并校验帧缓冲
#   make gouraud    跑 gouraud 内核并逐顶点校验
#   make matmul     跑 matmul 内核并逐元素校验
#   make anim       逐帧跑 anim 并逐帧对拍(4 帧)
#   make check      依次跑 clear / gouraud / matmul / anim
#   make demo       anim 动画 demo: SDL 窗口逐帧连续显示(需 SDL2; 关窗/ESC 结束)
#   make demo-headless  anim demo 不显示窗口, 只逐帧对拍(默认 60 帧)
#   make images     用 sw/ 的汇编器重新生成内核镜像(build/*.bin)
#   make clean      清掉产物

ROOT    := $(abspath ../..)
BUILD   := build
VERILATOR := $(ROOT)/tb/.tools/pytools/verilator/bin/verilator
CXX     ?= g++
CXXFLAGS ?= -O3 -std=c++17
VERILATOR_FLAGS := -O3 -Wno-fatal --threads 2 --build-jobs 4 \
                   --x-assign fast --x-initial fast

# SDL2 窗口(可选): 有就打开 --view, 没有也能编译(运行时提示装 libsdl2-dev)
SDL_CFLAGS := $(shell pkg-config --cflags sdl2 2>/dev/null)
SDL_LIBS   := $(shell pkg-config --libs sdl2 2>/dev/null)
ifneq ($(strip $(SDL_LIBS)),)
  CXXFLAGS += -DRUNSIM_HAVE_SDL $(SDL_CFLAGS)
  LDFLAGS  += $(SDL_LIBS)
else
  $(info [run-sim] 未找到 SDL2(pkg-config sdl2): --view 不可用, 其余功能正常)
endif

RTL_INC := -I$(ROOT)/hw/rtl -I$(ROOT)/hw/rtl/core -I$(ROOT)/hw/rtl/mem \
           -I$(ROOT)/hw/rtl/cp -I$(ROOT)/hw/rtl/fpu -I$(ROOT)/tb/harness/rtl

SRCS := \
  $(ROOT)/hw/rtl/core/core_pkg.sv \
  $(ROOT)/hw/rtl/mem/axi4_bus.sv \
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

.PHONY: all run clear gouraud matmul anim check demo demo-headless clean images

all: $(BIN)

$(BIN): rtl/gpu_system.sv tb_main.cpp $(SRCS)
	@mkdir -p $(BUILD)
	$(VERILATOR) --cc --exe --build $(VERILATOR_FLAGS) \
	    --top-module gpu_system $(RTL_INC) \
	    --Mdir $(OBJ_DIR) -o $(abspath $(BIN)) \
	    $(SRCS) tb_main.cpp \
	    -CFLAGS "$(CXXFLAGS)" -LDFLAGS "$(LDFLAGS)"

# 内核镜像由 sw/ 的汇编器生成(sim/simulator 的 make images 会产出 build/*.bin)
images:
	$(MAKE) -C $(ROOT)/sim/simulator images

run: clear

clear: $(BIN)
	cd $(ROOT)/sim/simulator && $(abspath $(BIN)) --image build/clear.bin --warps 4 \
	    --params 64,0x0000ff00,32 --fb-count 8 --fb-expect 0x0000ff00

gouraud: $(BIN)
	cd $(ROOT)/sim/simulator && $(abspath $(BIN)) --image build/gouraud.bin --kernel gouraud --warps 4

matmul: $(BIN)
	cd $(ROOT)/sim/simulator && $(abspath $(BIN)) --image build/matmul.bin --kernel matmul --warps 4

anim: $(BIN)
	cd $(ROOT)/sim/simulator && $(abspath $(BIN)) --image build/anim.bin --kernel anim \
	    --warps 4 --w 64 --h 64 --frames 4

check: clear gouraud matmul anim

# 动画 demo: SDL 窗口逐帧重算画面(关窗或按 ESC 结束)
# 画布用 32×32: RTL 仿真约 2 万拍/秒, 一帧 64×64 要 ~6.5 万拍(3 秒), 32×32 约 1.8 万拍
DEMO_W ?= 32
DEMO_H ?= 32
DEMO_SCALE ?= 8
demo: $(BIN)
	cd $(ROOT)/sim/simulator && $(abspath $(BIN)) --image build/anim.bin --kernel anim \
	    --warps 4 --view --w $(DEMO_W) --h $(DEMO_H) --box 6 --scale $(DEMO_SCALE) --fps 30

# 无窗口版本: 逐帧跑并逐帧与参考模型对拍
FRAMES ?= 60
demo-headless: $(BIN)
	cd $(ROOT)/sim/simulator && $(abspath $(BIN)) --image build/anim.bin --kernel anim \
	    --warps 4 --w 64 --h 64 --frames $(FRAMES)

clean:
	rm -rf $(BUILD)
