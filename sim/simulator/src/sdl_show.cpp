// SDL2 帧缓冲查看器(可选): 有 SDL2 时由 Makefile 定义 SIMU_HAVE_SDL 并编译。
#if defined(SIMU_HAVE_SDL)

#include "sdl_show.hpp"

#include <SDL2/SDL.h>
#include <cstdio>
#include <vector>

bool SDLViewer::init(int w, int h) {
    width_  = w;
    height_ = h;
    if (SDL_Init(SDL_INIT_VIDEO) != 0) {
        err_ = SDL_GetError();
        std::fprintf(stderr, "SDL_Init 失败: %s\n", err_);
        return false;
    }
    SDL_Window* window = SDL_CreateWindow("SimpleGPU",
                                          SDL_WINDOWPOS_CENTERED, SDL_WINDOWPOS_CENTERED,
                                          w, h, SDL_WINDOW_SHOWN);
    if (!window) { err_ = SDL_GetError(); SDL_Quit(); return false; }
    SDL_Renderer* renderer = SDL_CreateRenderer(window, -1, SDL_RENDERER_ACCELERATED);
    if (!renderer) { err_ = SDL_GetError(); SDL_DestroyWindow(window); SDL_Quit(); return false; }
    SDL_Texture* texture = SDL_CreateTexture(renderer, SDL_PIXELFORMAT_ARGB8888,
                                             SDL_TEXTUREACCESS_STREAMING, w, h);
    if (!texture) {
        err_ = SDL_GetError();
        SDL_DestroyRenderer(renderer);
        SDL_DestroyWindow(window);
        SDL_Quit();
        return false;
    }
    window_   = window;
    renderer_ = renderer;
    texture_  = texture;
    return true;
}

void SDLViewer::update(const std::vector<uint32_t>& pixels) {
    if (!texture_ || pixels.size() < size_t(width_) * size_t(height_)) return;
    SDL_UpdateTexture(static_cast<SDL_Texture*>(texture_), nullptr, pixels.data(), width_ * 4);
}

void SDLViewer::render() {
    if (!renderer_) return;
    SDL_RenderClear(static_cast<SDL_Renderer*>(renderer_));
    SDL_RenderCopy(static_cast<SDL_Renderer*>(renderer_), static_cast<SDL_Texture*>(texture_), nullptr, nullptr);
    SDL_RenderPresent(static_cast<SDL_Renderer*>(renderer_));
}

bool SDLViewer::poll() {
    SDL_Event e;
    while (SDL_PollEvent(&e)) {
        if (e.type == SDL_QUIT) return false;
        if (e.type == SDL_KEYDOWN && e.key.keysym.sym == SDLK_ESCAPE) return false;
    }
    return true;
}

bool SDLViewer::run_until_close(int ms_per_frame) {
    while (poll()) {
        render();
        SDL_Delay(Uint32(ms_per_frame));
    }
    return true;
}

void SDLViewer::shutdown() {
    const bool was_up = window_ != nullptr || renderer_ != nullptr || texture_ != nullptr;
    if (texture_)  { SDL_DestroyTexture(static_cast<SDL_Texture*>(texture_));   texture_ = nullptr; }
    if (renderer_) { SDL_DestroyRenderer(static_cast<SDL_Renderer*>(renderer_)); renderer_ = nullptr; }
    if (window_)   { SDL_DestroyWindow(static_cast<SDL_Window*>(window_));      window_ = nullptr; }
    if (was_up) SDL_Quit();
}

#endif  // SIMU_HAVE_SDL
