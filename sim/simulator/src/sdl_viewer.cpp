// sdl_viewer.cpp
#include <SDL2/SDL.h>
#include <vector>

class SDLViewer {
public:
    SDL_Window*   window   = nullptr;
    SDL_Renderer* renderer = nullptr;
    SDL_Texture*  texture  = nullptr;
    int width, height;

    bool init(int w, int h) {
        width = w; height = h;
        if (SDL_Init(SDL_INIT_VIDEO) != 0) {
            fprintf(stderr, "SDL_Init: %s\n", SDL_GetError());
            return false;
        }
        window = SDL_CreateWindow("SimpleGPU",
            SDL_WINDOWPOS_CENTERED, SDL_WINDOWPOS_CENTERED,
            width, height, SDL_WINDOW_SHOWN);
        if (!window) return false;

        renderer = SDL_CreateRenderer(window, -1,
            SDL_RENDERER_ACCELERATED | SDL_RENDERER_PRESENTVSYNC);
        if (!renderer) return false;

        texture = SDL_CreateTexture(renderer,
            SDL_PIXELFORMAT_ARGB8888,       // 按实际格式改
            SDL_TEXTUREACCESS_STREAMING,
            width, height);
        return texture != nullptr;
    }

    void update(const std::vector<uint32_t>& pixels) {
        SDL_UpdateTexture(texture, nullptr, pixels.data(), width * 4);
    }

    void render() {
        SDL_RenderClear(renderer);
        SDL_RenderCopy(renderer, texture, nullptr, nullptr);
        SDL_RenderPresent(renderer);
    }

    // 返回 false 表示用户关闭窗口
    bool poll() {
        SDL_Event e;
        while (SDL_PollEvent(&e)) {
            if (e.type == SDL_QUIT) return false;
            if (e.type == SDL_KEYDOWN && e.key.keysym.sym == SDLK_ESCAPE)
                return false;
        }
        return true;
    }

    void shutdown() {
        if (texture)  SDL_DestroyTexture(texture);
        if (renderer) SDL_DestroyRenderer(renderer);
        if (window)   SDL_DestroyWindow(window);
        SDL_Quit();
    }
};