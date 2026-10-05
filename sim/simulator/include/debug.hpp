#pragma once

#include <cstdio>
#include <assert.h>

#define Assert(cond, msg) do { if (!(cond)) { fprintf(stderr, "\033[31m[Assertion failed]: %s\033[0m\n", msg); assert(0); } } while (0)
#define Log(fmt, ...) \
    do { \
        fprintf(stderr, "[%s:%d] " fmt "\n", \
                __FILE__, __LINE__, ##__VA_ARGS__); \
    } while (0)

