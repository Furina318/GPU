#pragma once

// 可选的帧缓冲窗口(SDL2)。无 SDL2 时整个类不存在, 调用点用 SIMU_HAVE_SDL 包住。
// 头文件里不出现 SDL 类型, 以免把 SDL 依赖传染给其他翻译单元。

#include <cstdint>
#include <vector>

#if defined(SIMU_HAVE_SDL)

class SDLViewer {
public:
    ~SDLViewer() { shutdown(); }
    bool init(int w, int h);
    void update(const std::vector<uint32_t>& pixels);   // 0xAARRGGBB
    void render();
    bool poll();                                        // false = 用户关窗或按 ESC
    bool run_until_close(int ms_per_frame = 16);        // 停在窗口上直到用户关闭
    void shutdown();
    const char* error() const { return err_; }

private:
    void* window_   = nullptr;
    void* renderer_ = nullptr;
    void* texture_  = nullptr;
    int   width_ = 0, height_ = 0;
    const char* err_ = nullptr;
};

#endif  // SIMU_HAVE_SDL
