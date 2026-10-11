# sw/demo —— host 驱动侧的 demo

这里每个 demo 都是"**一个内核镜像 + 一个 host 驱动**"的组合:

| 文件 | 角色 |
|---|---|
| `sw/demo/demo.hpp` | **公共框架**(header-only): 所有 demo 共用的样板 |
| `sw/demo/anim.cpp` + `sw/kernels/anim.S` | demo 1: 画面**由 shader 逐像素算出来** |
| `sw/demo/car.cpp` + `sw/kernels/car_vtx.S` | demo 2(默认): **2D 引擎**画 —— shader 只算 6 个顶点, 填充/搬移/光栅化交给固定功能单元 |
| `sw/demo/car.cpp --shader` + `sw/kernels/car.S` | demo 2(另一条路): 逐像素 shader 版, 与上面**同一幅画** |

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
| `extra_cmds(cp, ctx, err)` | **每帧的附加命令**: 在 LAUNCH 之后、WAIT 之前提交(2D 引擎命令走这里) |
| `image_override()` | 镜像替换(命令行没给 `--image` 时生效): `car --shader` 靠它切到另一个内核 |
| `custom_opt(opt, val)` | demo 特有选项(返回 1=已处理 / 0=不归我 / -1=错误) |
| `extra_usage` | 追加进 `--help` 的文本 |

`FrameCtx` 由框架填充:`frame`(帧号)、`w/h`(画布)、`warps`、`fb_base`。

**框架约定**(与 docs/CMDS.md 一致):

- 一帧 = `PARAMS(0, params)` → `LAUNCH(entry, warps, flags, signal_id=0)` → *(可选 `extra_cmds`)*
  → `WAIT(SEM[0] >= frame+1)`;`signal_id=0` 被框架占用: LAUNCH 完成时 `SEM[0] += 1`(§4.3),出错不打信号量;
- 2D 命令写在 `extra_cmds` 里(见 `car.cpp`): 它们排在 LAUNCH 之后 ⇒ 队列顺序天然保证"顶点算完再画",
  同时 `2D_DRAW` 还带上 `wait_id=0` 把依赖显式写出来(GFX.md §7.1);
- 驱动契约: stride 类参数必须严格等于 `warps*8`(各内核注释里都有说明);
- 驱动契约: 框架建好 CP 后立刻 `cp.set_submit_pad(64)` —— 每条命令补到 64B 边界(CMDS §2 的约定)。
  变长命令 + 4096B 的环, 不补齐时会在某一帧正好剩 4 字节, 而 `NOP` 至少 2 字(§6.1)⇒ 没有合法填法;
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
make car  DEMO_ARGS="--view"               # 2D 引擎路径;按 ESC/关窗退出
make car-shader DEMO_ARGS="--ascii"        # 同一幅画, 但由逐像素 shader 算
ffmpeg -framerate 30 -i /tmp/frames/frame_%04d.ppm out.mp4    # PPM 序列合成视频
```

## car demo: 两种画法, 同一幅画

`make car`(默认)与 `make car-shader` 渲染的是**同一场景**,区别只在谁来画像素:

| | 默认(2D 引擎) | `--shader`(逐像素) |
|---|---|---|
| 内核 | `car_vtx.S`: 每个 lane 算**一个顶点**(6 个) | `car.S`: 每个线程算一批像素 |
| 每帧命令 | `PARAMS → LAUNCH → 6×SET_REG → 11×2D_FILL → 2D_BLIT → 2D_DRAW → WAIT` | `PARAMS → LAUNCH → WAIT` |
| 背景(草地/公路/虚线) | `2D_FILL`(固定功能, 不占发射槽) | 每像素用 `setp` 拼条件 |
| 小车精灵 | `2D_BLIT` + 颜色键(`COLKEY_EN`) | 每像素 `ld.g` 精灵位图 + 透明键判断 |
| 车顶高光 | `2D_DRAW`(shader 算顶点 + 重心插值 + `SRC_OVER` 混合) | 无 |
| 参考模型 | 背景公式 + 精灵合成 + 高光几何(面积 = `body_w×body_h`) | 背景公式 + 精灵合成(全图逐像素) |
| 实测(128×64, 4 warp 的机器上) | **53 周期/帧** | **33834 周期/帧** |

最后一行的对比就是 GFX.md §1 里"顶点由 shader 算、像素由 2D 引擎算"的量化理由: 2D 引擎的
53 周期只是"启动内核算 6 个顶点"的成本 —— 像素工作(114 条 2D 命令)完全不占 SM 的发射槽。
注意高光是**平色**的: 同色顶点经重心插值后逐像素为常量, demo 的参考模型才能精确对拍;
渐变的插值由 `make test` 的 `draw_blend` 用例守(GFX.md §5.4)。

`--shader` 路径与默认路径的背景/精灵像素**必须完全一致** —— 这不是巧合: 两条路用的
虚线相位(8px 实/8px 虚)、精灵位图与配色都来自同一个 `Scene` 结构, 参考模型也是同一份。

---
