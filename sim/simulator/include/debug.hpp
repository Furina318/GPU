#pragma once

#include "log.hpp"

#include <cstdio>
#include <assert.h>

#define Assert(cond, msg) do { if (!(cond)) { fprintf(stderr, "\033[31m[Assertion failed]: %s\033[0m\n", msg); assert(0); } } while (0)

// 原有调用点的 Log(...) 一律走分级日志的 INFO 级(输出格式与文件/环形缓冲由 log.cpp 决定)
#define Log(fmt, ...) LogI(fmt, ##__VA_ARGS__)
