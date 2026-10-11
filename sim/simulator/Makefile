CXX      ?= g++
CXXFLAGS ?= -std=c++17 -O2 -Wall -Wextra -Iinclude
# 头文件依赖跟踪(-MMD 生成 .d, 末尾 -include 进来): **没有它, 改了头文件只会重编对应的 .cpp**,
# 于是"新 cp.o + 旧 main.o"这种 ABI 不一致的可执行文件会被悄悄链出来(表现为随机段错误)。
DEPFLAGS := -MMD -MP
SRCDIRS  := src shader 2D-engine
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
SRCS       := $(wildcard $(addsuffix /*.cpp,$(SRCDIRS)))
ifeq ($(HAVE_SDL),1)
  CXXFLAGS += -DSIMU_HAVE_SDL $(SDL_CFLAGS)
else
  SRCS := $(filter-out src/sdl_show.cpp src/sdl_viewer.cpp,$(SRCS))
endif
LDLIBS   += $(SDL_LIBS)

# 模拟器本体做成静态库(libsimu.a): 除了命令行前端(main.cpp)之外的源码都进去,
# 这样 sw/demo/ 里的 host 驱动可以直接链接同一套 Machine/CP API。
LIB_SRCS := $(filter-out src/main.cpp,$(SRCS))
LIB_OBJS := $(patsubst %.cpp,$(OBJDIR)/%.o,$(LIB_SRCS))
DEMOS     := anim car
DEMO_BINS := $(addprefix $(BUILD)/,$(DEMOS))

.PHONY: all run run-all debug dbg-all check test images demo anim car car-shader clean help

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

## make car        → 2D 引擎路径(FILL/BLIT/DRAW, 默认)
## make car-shader → 逐像素 shader 路径(sw/kernels/car.S), 与上面画同一幅画
car: $(BUILD)/car $(BUILD)/car.bin $(BUILD)/car_vtx.bin
	$(BUILD)/car $(DEMO_ARGS)

car-shader: $(BUILD)/car $(BUILD)/car.bin
	$(BUILD)/car --shader $(DEMO_ARGS)

## make demo → 只编译(不运行), 便于单独调
demo: $(DEMO_BINS)
## ======================================================================

## make check → 每个内核跑一遍并做结果校验(退出码非 0 即失败)
check: $(BUILD)/simulator $(IMAGES)
	@fail=0; for k in $(ALL_KERNELS); do \
		printf '%-10s ' $$k; \
		if $(BUILD)/simulator $(BUILD)/$$k.bin >$(BUILD)/$$k.log 2>&1; then \
			if grep -q '校验通过' $(BUILD)/$$k.log; then \
				sed -n 's/.*\(校验通过.*\)/\1/p' $(BUILD)/$$k.log | head -1; \
			else \
				echo "⚠ 跑通但没有参考模型(结果未校验)"; \
			fi; \
		else \
			echo "✗ 失败(退出码 $$?), 详见 $(BUILD)/$$k.log"; fail=1; \
		fi; \
	done; \
	echo; if [ $$fail -eq 0 ]; then echo "全部内核通过"; else echo "有内核失败"; exit 1; fi

## make test → 回归测试: 文档一致性 + ISA 表三方对照 + ISA §2.6 错误路径 + 运行模式 + 动画 demo
test: $(BUILD)/simulator $(BUILD)/anim $(BUILD)/car $(BUILD)/car.bin $(BUILD)/car_vtx.bin
	python3 tests/doc_consistency_check.py
	python3 tests/isa_ref_check.py
	python3 tests/stack_model_check.py
	python3 tests/run_tests.py

images: $(IMAGES)

$(BUILD)/%.bin: ../../sw/kernels/%.S $(ASM) | $(BUILD)
	python3 $(ASM) $< --format image -o $@

$(BUILD)/libsimu.a: $(LIB_OBJS)
	ar rcs $@ $^

$(BUILD)/simulator: $(OBJDIR)/src/main.o $(BUILD)/libsimu.a
	$(CXX) $^ -o $@ $(LDLIBS)

# 通用 demo 规则: sw/demo/<name>.cpp → build/<name>(链接 libsimu.a)。
$(BUILD)/%: ../../sw/demo/%.cpp $(BUILD)/libsimu.a | $(OBJDIR)
	$(CXX) $(CXXFLAGS) $(DEPFLAGS) -c $< -o $(OBJDIR)/demo_$*.o
	$(CXX) $(OBJDIR)/demo_$*.o $(BUILD)/libsimu.a -o $@ $(LDLIBS)

$(OBJDIR)/%.o: %.cpp
	@mkdir -p $(dir $@)
	$(CXX) $(CXXFLAGS) $(DEPFLAGS) -c $< -o $@

$(BUILD) $(OBJDIR):
	mkdir -p $@

clean:
	rm -rf $(BUILD)

# 头文件依赖(由 -MMD 生成): 放在最后, 首次构建时文件还不存在 ⇒ 用 wildcard + -include 宽容处理
-include $(wildcard $(OBJDIR)/*.d) $(wildcard $(OBJDIR)/*/*.d)
