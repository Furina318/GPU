CXX      ?= g++
CXXFLAGS ?= -std=c++17 -O2 -Wall -Wextra -Iinclude
BUILD    := build
SRCS     := $(wildcard src/*.cpp)

.PHONY: all clean

all: $(BUILD)/simulator

$(BUILD)/simulator: $(SRCS) | $(BUILD)
	$(CXX) $(CXXFLAGS) $(SRCS) -o $@

$(BUILD):
	mkdir -p $(BUILD)

clean:
	rm -rf $(BUILD)
