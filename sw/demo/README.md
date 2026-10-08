# sw/demo —— host 驱动侧的 demo

这里每个 demo 都是"**一个内核镜像 + 一个 host 驱动**"的组合:

| 文件 | 角色 |
|---|---|
| `sw/demo/demo.hpp` | **公共框架**(header-only): 所有 demo 共用的样板 |
| `sw/demo/anim.cpp` + `sw/kernels/anim.S` | demo 1: 画面**由 shader 算出来** |
| `sw/demo/car.cpp` + `sw/kernels/car.S` | demo 2: 小车**精灵搬移**(位图从全局内存搬进帧缓冲) |

驱动直接链接模拟器静态库 `sim/simulator/build/libsimu.a`,用与真实驱动相同的命令路径
(`LOAD_KERNEL` → 逐帧 `PARAMS/LAUNCH/WAIT` → 读回帧缓冲)

## demo.hpp: 公共框架

框架负责所有 demo 相同的部分:**选项解析、镜像加载(`LOAD_KERNEL`)、逐帧
`PARAMS → LAUNCH → WAIT` 循环、帧缓冲读回、`--ascii/--ppm-dir/--view` 三种显示、
统计与限速**。每个 demo 只填一个 `demo::Spec` 就拥有全部:

| Spec 字段 | 作用 |
|---|---|
| `image` / `w,h,warps` / `frames,fps` / `scale` | 默认值(均可被命令行覆盖) |
| `about` | 横幅与 `--help` 里的一句话说明 |
| `setup(Machine&)` | 预置: `Machine` 建好后、加载镜像前调用(如把精灵位图写进全局内存) |
| `check_canvas(w,h)` | 画布合法性: 返回空 = 合法,否则返回错误信息 |
| `params_for(FrameCtx)` | **每帧参数块**(与内核常量布局一一对应) |
| `verify(fb, params, ctx)` | 参考模型逐像素校验(空 = 不校验) |
| `custom_opt(opt, val)` | demo 特有选项(返回 1=已处理 / 0=不归我 / -1=错误) |
| `extra_usage` | 追加进 `--help` 的文本 |

`FrameCtx` 由框架填充:`frame`(帧号)、`w/h`(画布)、`warps`、`fb_base`。

**框架约定**(与 docs/CMDS.md 一致):

- 一帧 = `PARAMS(0, params)` → `LAUNCH(entry, warps, flags, signal_id=0)` → `WAIT(SEM[0] >= frame+1)`;
  `signal_id=0` 被框架占用: LAUNCH 完成时 `SEM[0] += 1`(§4.3),出错不打信号量;
- 驱动契约: stride 类参数必须严格等于 `warps*8`(各内核注释里都有说明);
- 退出码: `0` 正常 / `1` 用法·命令错误 / `2` SM 架构错误 / `3` 画面校验不符;
- `DEMO_DEBUG=1` 环境变量: 首帧打印 entry/flags/常量区/命令流历史。

**新增 demo 的步骤**(以 foo 为例):

1. 写内核 `sw/kernels/foo.S`(常量区布局写进头注释);
2. 写驱动 `sw/demo/foo.cpp`: 参数块 (可选 + 参考模型) + 填 `Spec`,最后 `return demo::run(argc, argv, spec);`
3. `sim/simulator/Makefile` 里 `DEMOS += foo` 并仿照 anim/car 加一个运行入口;
4. `make foo [DEMO_ARGS=...]`。

## 通用命令行选项

所有 demo 共有(`--help` 里会追加各自 `extra_usage`):

```
--view / --ascii / --ppm-dir DIR / --scale N     显示方式(SDL2 / 终端真彩色 / PPM 序列)
--w N --h N / --warps N                          画布与 warp 数
--frames N(0 = 一直跑)/ --fps N(0 = 不限速)
--stats / --no-verify / --quiet
--log FILE --log-level trace                     命令级/指令级日志
--image FILE                                     内核镜像
```

```bash
cd sim/simulator
make demo                                  # 编译全部 demo(anim、car)
make anim DEMO_ARGS="--ascii --fps 20"     # 终端真彩色动画(SSH 里也能看)
make car  DEMO_ARGS="--view"               # SDL2 窗口, 按 ESC/关窗退出
ffmpeg -framerate 30 -i /tmp/frames/frame_%04d.ppm out.mp4    # PPM 序列合成视频
```

---
