CXX      ?= g++
CXXFLAGS ?= -std=c++17 -O2 -Wall -Wextra -Iinclude
BUILD    := build
OBJDIR   := $(BUILD)/obj

ASM      := ../../sw/assembly.py
KERNEL   ?= clear
ARGS     ?=
DEMO_ARGS ?=
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

# 模拟器本体做成静态库(libsimu.a): 除了命令行前端(main.cpp)之外的源码都进去,
# 这样 sw/demo/ 里的 host 驱动可以直接链接同一套 Machine/CP API。
LIB_SRCS := $(filter-out src/main.cpp,$(SRCS))
LIB_OBJS := $(patsubst src/%.cpp,$(OBJDIR)/%.o,$(LIB_SRCS))
DEMOS     := anim car
DEMO_BINS := $(addprefix $(BUILD)/,$(DEMOS))

.PHONY: all run run-all debug dbg-all check test images demo anim clean help

all: $(BUILD)/simulator $(DEMO_BINS)

help:
	@echo "make run [KERNEL=clear|gouraud|matmul|anim] [ARGS=...]  汇编并运行内核"
	@echo "make debug [KERNEL=matmul]                              汇编并进入交互式调试器"
	@echo "make check                                              跑全部内核并校验结果"
	@echo "make test                                               回归测试(正例 + 错误路径 + 运行模式)"
	@echo "make anim [DEMO_ARGS=...]                               动画 demo(单帧渲染)"
	@echo "make car [DEMO_ARGS=...]                                小汽车 demo(精灵搬移)"
	@echo "make demo                                               只编译 demo(不运行)"
	@echo "make images                                             只汇编出镜像"
	@echo "make clean"

## make run KERNEL=matmul ARGS="--log run.log --trace"
run: $(BUILD)/simulator $(BUILD)/$(KERNEL).bin
	$(BUILD)/simulator $(BUILD)/$(KERNEL).bin $(ARGS)

## make debug KERNEL=matmul  → 直接进单步调试
debug dbg: $(BUILD)/simulator $(BUILD)/$(KERNEL).bin
	$(BUILD)/simulator $(BUILD)/$(KERNEL).bin --debug $(ARGS)

## ======================================================================
anim: $(BUILD)/anim $(BUILD)/anim.bin
	$(BUILD)/anim --image $(BUILD)/anim.bin $(DEMO_ARGS)

car: $(BUILD)/car $(BUILD)/car.bin
	$(BUILD)/car --image $(BUILD)/car.bin $(DEMO_ARGS)

## make demo → 只编译(不运行), 便于单独调
demo: $(DEMO_BINS)
## ======================================================================

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

## make test → 回归测试: 内核正例 + ISA §2.6 错误路径 + 运行模式 + 动画 demo
test: $(BUILD)/simulator $(BUILD)/anim
	python3 tests/stack_model_check.py
	python3 tests/run_tests.py

images: $(IMAGES)

$(BUILD)/%.bin: ../../sw/kernels/%.S $(ASM) | $(BUILD)
	python3 $(ASM) $< --format image -o $@

$(BUILD)/libsimu.a: $(LIB_OBJS)
	ar rcs $@ $^

$(BUILD)/simulator: $(OBJDIR)/main.o $(BUILD)/libsimu.a
	$(CXX) $^ -o $@ $(LDLIBS)

# 通用 demo 规则: sw/demo/<name>.cpp → build/<name>(链接 libsimu.a)。
$(BUILD)/%: ../../sw/demo/%.cpp $(BUILD)/libsimu.a | $(OBJDIR)
	$(CXX) $(CXXFLAGS) -c $< -o $(OBJDIR)/demo_$*.o
	$(CXX) $(OBJDIR)/demo_$*.o $(BUILD)/libsimu.a -o $@ $(LDLIBS)

$(OBJDIR)/%.o: src/%.cpp | $(OBJDIR)
	$(CXX) $(CXXFLAGS) -c $< -o $@

$(BUILD) $(OBJDIR):
	mkdir -p $@

clean:
	rm -rf $(BUILD)
