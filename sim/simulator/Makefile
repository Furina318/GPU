CXX      ?= g++
CXXFLAGS ?= -std=c++17 -O2 -Wall -Wextra -Iinclude
BUILD    := build

ASM      := ../../sw/assembly.py
KERNEL   ?= clear
ARGS     ?=
ALL_KERNELS := $(patsubst ../../sw/kernels/%.S,%,$(wildcard ../../sw/kernels/*.S))
IMAGES   := $(addprefix $(BUILD)/,$(addsuffix .bin,$(ALL_KERNELS)))

# SDL2 只用于可选的帧缓冲窗口: 有就编译 sdl_show.cpp, 没有就排除掉(不影响其他功能)
HAVE_SDL   ?= $(shell pkg-config --exists sdl2 2>/dev/null && echo 1)
SDL_CFLAGS := $(if $(HAVE_SDL),$(shell pkg-config --cflags sdl2 2>/dev/null))
SDL_LIBS   := $(if $(HAVE_SDL),$(shell pkg-config --libs sdl2 2>/dev/null))
SRCS       := $(wildcard src/*.cpp)
ifeq ($(HAVE_SDL),1)
  CXXFLAGS += -DSIMU_HAVE_SDL $(SDL_CFLAGS)
else
  SRCS := $(filter-out src/sdl_show.cpp src/sdl_viewer.cpp,$(SRCS))
endif
LDLIBS   += $(SDL_LIBS)

.PHONY: all run run-all debug dbg-all check test images clean help

all: $(BUILD)/simulator

help:
	@echo "make run [KERNEL=clear|gouraud|matmul] [ARGS=...]   汇编并运行内核"
	@echo "make debug [KERNEL=matmul]                          汇编并进入交互式调试器"
	@echo "make check                                          跑全部内核并校验结果"
	@echo "make test                                           回归测试(正例 + 错误路径 + 运行模式)"
	@echo "make images                                         只汇编出镜像"
	@echo "make clean"

## make run KERNEL=matmul ARGS="--log run.log --trace"
run: $(BUILD)/simulator $(BUILD)/$(KERNEL).bin
	$(BUILD)/simulator $(BUILD)/$(KERNEL).bin $(ARGS)

## make debug KERNEL=matmul  → 直接进单步调试
debug dbg: $(BUILD)/simulator $(BUILD)/$(KERNEL).bin
	$(BUILD)/simulator $(BUILD)/$(KERNEL).bin --debug $(ARGS)

## make check → 每个内核跑一遍并做结果校验(退出码非 0 即失败)
check: $(BUILD)/simulator $(IMAGES)
	@fail=0; for k in $(ALL_KERNELS); do \
		printf '%-10s ' $$k; \
		if $(BUILD)/simulator $(BUILD)/$$k.bin >$(BUILD)/$$k.log 2>&1; then \
			sed -n 's/.*\(校验通过.*\)/\1/p' $(BUILD)/$$k.log | head -1; \
		else \
			echo "✗ 失败(退出码 $$?), 详见 $(BUILD)/$$k.log"; fail=1; \
		fi; \
	done; \
	echo; if [ $$fail -eq 0 ]; then echo "全部内核通过"; else echo "有内核失败"; exit 1; fi

## make test → 回归测试: 内核正例 + ISA §2.6 错误路径 + 三种运行模式
test: $(BUILD)/simulator
	python3 tests/stack_model_check.py
	python3 tests/run_tests.py

images: $(IMAGES)

$(BUILD)/%.bin: ../../sw/kernels/%.S $(ASM) | $(BUILD)
	python3 $(ASM) $< --format image -o $@

$(BUILD)/simulator: $(SRCS) | $(BUILD)
	$(CXX) $(CXXFLAGS) $(SRCS) -o $@ $(LDLIBS)

$(BUILD):
	mkdir -p $(BUILD)

clean:
	rm -rf $(BUILD)
