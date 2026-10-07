#pragma once

// 分级日志: 控制台(带颜色) + 日志文件(结构化, 可 grep) + 内存环形缓冲(调试器 log 命令回看)。
// 执行引擎在每条指令前后调用 set_context(), 于是日志里能看到 cycle/warp/pc, 不用自己拼前缀。

#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <string>
#include <vector>

namespace simu {

enum class LogLevel { TRACE = 0, DEBUG, INFO, WARN, ERR, OFF };

const char* log_level_name(LogLevel l);
bool        parse_log_level(const char* s, LogLevel& out);

struct LogRecord {
    LogLevel    lvl = LogLevel::INFO;
    uint64_t    cycle = 0;
    int         warp = -1;          // -1 = 无执行上下文
    uint32_t    pc = 0;
    std::string where;              // "exec.cpp:210"
    std::string msg;
};

class Logger {
public:
    bool open_file(const std::string& path);        // 追加写; 失败返回 false 并置 *error
    void close_file();
    bool file_open() const { return fp_ != nullptr; }
    const std::string& file_path() const { return path_; }

    void set_file_level(LogLevel l)    { file_level_ = l; }
    void set_console_level(LogLevel l) { console_level_ = l; }
    void set_ring_level(LogLevel l)    { ring_level_ = l; }
    void set_ring_capacity(size_t n);
    void set_quiet(bool q)             { quiet_ = q; }
    bool quiet() const                 { return quiet_; }

    LogLevel file_level() const    { return file_level_; }
    LogLevel console_level() const { return console_level_; }
    LogLevel ring_level() const    { return ring_level_; }

    // 执行上下文(随指令变化, 日志前缀用)
    void set_context(uint64_t cycle, int warp, uint32_t pc) { cycle_ = cycle; warp_ = warp; pc_ = pc; }
    void clear_context() { warp_ = -1; pc_ = 0; }

    void logf(LogLevel l, const char* file, int line, const char* fmt, ...)
        __attribute__((format(printf, 5, 6)));
    void vlogf(LogLevel l, const char* file, int line, const char* fmt, va_list ap);

    const std::vector<LogRecord>& recent() const { return ring_; }   // 按写入顺序
    uint64_t count(LogLevel l) const { return counts_[int(l)]; }
    uint64_t total() const;
    void flush();

private:
    void emit_record(const LogRecord& r);

    FILE*        fp_ = nullptr;
    std::string  path_;
    LogLevel     file_level_    = LogLevel::DEBUG;
    LogLevel     console_level_ = LogLevel::INFO;
    LogLevel     ring_level_    = LogLevel::INFO;
    size_t       ring_cap_      = 2048;
    bool         quiet_         = false;
    std::vector<LogRecord> ring_;
    size_t       ring_next_ = 0;
    uint64_t     counts_[6] = {0, 0, 0, 0, 0, 0};
    uint64_t     cycle_ = 0;
    int          warp_  = -1;
    uint32_t     pc_    = 0;
};

Logger& logger();

}  // namespace simu

#define LogT(...) ::simu::logger().logf(::simu::LogLevel::TRACE, __FILE__, __LINE__, __VA_ARGS__)
#define LogD(...) ::simu::logger().logf(::simu::LogLevel::DEBUG, __FILE__, __LINE__, __VA_ARGS__)
#define LogI(...) ::simu::logger().logf(::simu::LogLevel::INFO,  __FILE__, __LINE__, __VA_ARGS__)
#define LogW(...) ::simu::logger().logf(::simu::LogLevel::WARN,  __FILE__, __LINE__, __VA_ARGS__)
#define LogE(...) ::simu::logger().logf(::simu::LogLevel::ERR,   __FILE__, __LINE__, __VA_ARGS__)
