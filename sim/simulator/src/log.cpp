#include "log.hpp"

#include <cstring>
#include <strings.h>
#include <ctime>

namespace simu {

const char* log_level_name(LogLevel l) {
    switch (l) {
    case LogLevel::TRACE: return "TRACE";
    case LogLevel::DEBUG: return "DEBUG";
    case LogLevel::INFO:  return "INFO";
    case LogLevel::WARN:  return "WARN";
    case LogLevel::ERR:   return "ERROR";
    case LogLevel::OFF:   return "OFF";
    }
    return "?";
}

bool parse_log_level(const char* s, LogLevel& out) {
    static const struct { const char* n; LogLevel l; } tab[] = {
        {"trace", LogLevel::TRACE}, {"debug", LogLevel::DEBUG}, {"info", LogLevel::INFO},
        {"warn", LogLevel::WARN},   {"warning", LogLevel::WARN}, {"error", LogLevel::ERR},
        {"err", LogLevel::ERR},     {"off", LogLevel::OFF},     {"none", LogLevel::OFF},
    };
    if (!s) return false;
    for (const auto& e : tab)
        if (strcasecmp(s, e.n) == 0) { out = e.l; return true; }
    return false;
}

Logger& logger() {
    static Logger inst;
    return inst;
}

void Logger::set_ring_capacity(size_t n) {
    if (n == 0) return;
    ring_cap_ = n;
    if (ring_.size() > n) ring_.erase(ring_.begin(), ring_.begin() + long(ring_.size() - n));
    if (ring_next_ >= ring_.size()) ring_next_ = 0;
}

bool Logger::open_file(const std::string& path) {
    close_file();
    fp_ = std::fopen(path.c_str(), "w");
    if (!fp_) return false;
    path_ = path;
    std::time_t t = std::time(nullptr);
    char ts[64] = "";
    std::tm tmv{};
#if defined(_WIN32)
    localtime_s(&tmv, &t);
#else
    localtime_r(&t, &tmv);
#endif
    std::strftime(ts, sizeof(ts), "%Y-%m-%d %H:%M:%S", &tmv);
    std::fprintf(fp_, "# SimpleGPU 模拟器日志 开始于 %s\n", ts);
    std::fprintf(fp_, "# 格式: cycle=<周期> warp=<warp 号或 -> pc=<PC> level=<级别> at=<文件:行> msg\n");
    std::fflush(fp_);
    return true;
}

void Logger::close_file() {
    if (fp_) { std::fflush(fp_); std::fclose(fp_); fp_ = nullptr; }
}

void Logger::flush() {
    if (fp_) std::fflush(fp_);
}

void Logger::emit_record(const LogRecord& r) {
    std::fflush(stdout);      // 日志走 stderr, 普通输出走 stdout: 保证管道里顺序不乱
    if (fp_ && int(r.lvl) >= int(file_level_) && file_level_ != LogLevel::OFF) {
        if (r.warp >= 0)
            std::fprintf(fp_, "cycle=%-10llu warp=%-2d pc=0x%08x level=%-5s at=%s msg=%s\n",
                         (unsigned long long)r.cycle, r.warp, r.pc, log_level_name(r.lvl),
                         r.where.c_str(), r.msg.c_str());
        else
            std::fprintf(fp_, "cycle=%-10llu warp=-  pc=---------- level=%-5s at=%s msg=%s\n",
                         (unsigned long long)r.cycle, log_level_name(r.lvl),
                         r.where.c_str(), r.msg.c_str());
        std::fflush(fp_);
    }
    if (!quiet_ && int(r.lvl) >= int(console_level_) && console_level_ != LogLevel::OFF) {
        const char* color = "\033[0m";
        switch (r.lvl) {
        case LogLevel::TRACE: color = "\033[90m"; break;
        case LogLevel::DEBUG: color = "\033[36m"; break;
        case LogLevel::INFO:  color = "\033[33m"; break;
        case LogLevel::WARN:  color = "\033[35m"; break;
        case LogLevel::ERR:   color = "\033[31m"; break;
        default: break;
        }
        const bool console_tty = true;
        if (r.warp >= 0)
            std::fprintf(stderr, "%s[%llu cyc w%d pc=0x%04x][%s] %s\033[0m\n", console_tty ? color : "",
                         (unsigned long long)r.cycle, r.warp, r.pc, r.where.c_str(), r.msg.c_str());
        else
            std::fprintf(stderr, "%s[%s] %s\033[0m\n", console_tty ? color : "", r.where.c_str(), r.msg.c_str());
    }
}

void Logger::vlogf(LogLevel l, const char* file, int line, const char* fmt, va_list ap) {
    if (l == LogLevel::OFF) return;
    ++counts_[int(l)];

    char buf[1024];
    va_list ap2;
    va_copy(ap2, ap);
    int n = std::vsnprintf(buf, sizeof(buf), fmt, ap2);
    va_end(ap2);
    if (n < 0) buf[0] = '\0';

    LogRecord r;
    r.lvl   = l;
    r.cycle = cycle_;
    r.warp  = warp_;
    r.pc    = pc_;
    r.where = std::string(file ? file : "?") + ":" + std::to_string(line);
    r.msg   = buf;

    emit_record(r);

    if (int(l) >= int(ring_level_) && ring_level_ != LogLevel::OFF) {
        ring_.push_back(std::move(r));
        while (ring_.size() > ring_cap_) ring_.erase(ring_.begin());
        ring_next_ = ring_.size() ? (ring_next_ + 1) % ring_.size() : 0;
    }
}

void Logger::logf(LogLevel l, const char* file, int line, const char* fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    vlogf(l, file, line, fmt, ap);
    va_end(ap);
}

uint64_t Logger::total() const {
    uint64_t t = 0;
    for (int i = 0; i < 5; ++i) t += counts_[i];
    return t;
}

}  // namespace simu
