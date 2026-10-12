#!/usr/bin/env python3
"""模拟器回归测试。

正例: 三个内核(clear/gouraud/matmul)结果校验 + 7 层嵌套发散(检验 SIMT 栈)。
反例: ISA §2.6 的每条架构错误、CMDS §8.1 的 CP 错误、镜像头/CRC 校验、barrier 死锁。
运行模式: 直接运行 / 交互调试(si/断点/查看) / 日志文件 / --direct 对照 / CP 命令自检。

用法: python3 tests/run_tests.py [--keep]
退出码 0 = 全部通过。
"""

import os
import re
import struct
import subprocess
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
SIMDIR = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(SIMDIR))
SIM = os.path.join(SIMDIR, "build", "simulator")
ASM = os.path.join(REPO, "sw", "assembly.py")
OUT = os.path.join(SIMDIR, "build", "tests")

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("  \033[32m✓\033[0m " if cond else "  \033[31m✗\033[0m ") + name + (("   " + detail) if detail and not cond else ""))


def run(args, stdin=None, timeout=120):
    p = subprocess.run([SIM] + args, input=stdin, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout + p.stderr


def write_script(outdir, name, lines):
    """把命令脚本写到 build/tests/<name>.txt(CP/2D 的边界用例靠它定义整条命令流)。"""
    path = os.path.join(outdir, name + ".txt")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return path


def assemble(src, out):
    p = subprocess.run([sys.executable, ASM, src, "--format", "image", "-o", out],
                       capture_output=True, text=True)
    return p.returncode == 0, p.stdout + p.stderr


def patch_image(src, dst, word_index, new_word):
    """把镜像里第 word_index 个 32 位字(从 entry 起算)换成 new_word, 并重算 CRC。"""
    data = bytearray(open(src, "rb").read())
    magic, entry, size, flags, crc, csize = struct.unpack_from("<6I", data, 0)
    assert magic == 0x53474B31, "不是 SGK1 镜像"
    off = entry + word_index * 4
    struct.pack_into("<I", data, off, new_word)
    struct.pack_into("<I", data, 16, zlib.crc32(bytes(data[entry:entry + csize])) & 0xFFFFFFFF)
    open(dst, "wb").write(bytes(data))
    return dst


def corrupt_crc(src, dst):
    data = bytearray(open(src, "rb").read())
    struct.pack_into("<I", data, 16, struct.unpack_from("<I", data, 16)[0] ^ 0xDEADBEEF)
    open(dst, "wb").write(bytes(data))
    return dst


def main():
    keep = "--keep" in sys.argv
    os.makedirs(OUT, exist_ok=True)
    if not os.path.exists(SIM):
        print("先 make 出 build/simulator")
        return 1

    print("\n== 汇编镜像 ==")
    for k in ("clear", "gouraud", "matmul", "anim", "abs"):
        ok, msg = assemble(os.path.join(REPO, "sw/kernels", k + ".S"), os.path.join(OUT, k + ".bin"))
        check("汇编 sw/kernels/%s.S" % k, ok, msg)
    for f in ("nested7", "spin", "err_align", "err_bar_div", "err_deadlock", "err_recov", "err_mmio",
              "sh_conflict", "sh_noconflict", "sh_twobank"):
        ok, msg = assemble(os.path.join(HERE, f + ".S"), os.path.join(OUT, f + ".bin"))
        check("汇编 tests/%s.S" % f, ok, msg)

    print("\n== 正例: 三个内核结果校验 ==")
    for k in ("clear", "gouraud", "matmul", "anim", "abs"):   # anim/abs 有默认参数块与参考模型
        rc, out = run([os.path.join(OUT, k + ".bin")])
        check("%s: 退出码 0 且校验通过" % k, rc == 0 and "校验通过" in out,
              "rc=%d" % rc if rc else out[-300:])

    rc, out = run([os.path.join(OUT, "abs.bin")])
    check("abs(§7.4): 结果正确且确实发散了(压栈 >0, 栈深 1/8)",
          rc == 0 and "abs 校验通过" in out and "0 次压栈" not in out and "栈最深 1/8" in out, out[-400:])

    print("\n== SIMT 栈: 7 层嵌套发散 ==")
    rc, out = run([os.path.join(OUT, "nested7.bin"), "--warps", "1", "--no-check"])
    check("nested7: 结束正常且 SIMT 栈最深 7/8", rc == 0 and "栈最深 7/8" in out, out[-300:])
    check("nested7: 7 次压栈 / 14 次收敛(每帧一次切路径 + 一次弹栈)",
          "7 次压栈 / 14 次收敛" in out, out[-300:])

    print("\n== 反例: ISA §2.6 架构错误 ==")
    rc, out = run([os.path.join(OUT, "err_align.bin"), "--warps", "1", "--no-check"])
    check("BAD_ALIGN: rc=2 且 SM0_ERROR=BAD_ALIGN", rc == 2 and "SM0_ERROR=BAD_ALIGN" in out, out[-300:])
    check("BAD_ALIGN: 报告 FAULT_PC 与出错指令", "FAULT_PC=0x0024" in out, out[-400:])

    rc, out = run([os.path.join(OUT, "err_bar_div.bin"), "--warps", "1", "--no-check"])
    check("BAR_DIVERGENT: rc=2 且 SM0_ERROR=BAR_DIVERGENT", rc == 2 and "SM0_ERROR=BAR_DIVERGENT" in out, out[-300:])

    rc, out = run([os.path.join(OUT, "err_deadlock.bin"), "--warps", "2", "--no-check"])
    check("barrier 死锁: 立刻报 DEADLOCK(不等超时)", "barrier 死锁" in out and "bar.sync" in out, out[-400:])

    rc, out = run([os.path.join(OUT, "spin.bin"), "--warps", "1", "--no-check", "--max-cycles", "5000"])
    check("死循环: --max-cycles 到点停成 TIMEOUT(不挂死)",
          rc == 1 and "超出周期上限" in out and "TIMEOUT" in out, out[-300:])

    # ERR_NO_RECOV: 把汇编器自动生成的 ipdom 抹成 addi r0,r0,0
    src = os.path.join(OUT, "err_recov.bin")
    raw = open(src, "rb").read()
    entry, size, csize = struct.unpack_from("<III", raw, 4)[0], struct.unpack_from("<I", raw, 8)[0], struct.unpack_from("<I", raw, 20)[0]
    ipdom_idx = None
    for i in range(csize // 4):
        w = struct.unpack_from("<I", raw, entry + i * 4)[0]
        if (w & 0x7F) == 0x57:            # ipdom 独占 opcode 1010111
            ipdom_idx = i
            break
    check("err_recov: 找到欲抹掉的 ipdom", ipdom_idx is not None)
    if ipdom_idx is not None:
        patched = patch_image(src, os.path.join(OUT, "err_recov_patched.bin"), ipdom_idx, 0x00000013)
        rc, out = run([patched, "--warps", "1", "--no-check"])
        check("ERR_NO_RECOV: rc=2 且 SM0_ERROR=ERR_NO_RECOV",
              rc == 2 and "SM0_ERROR=ERR_NO_RECOV" in out, out[-300:])

    # ILLEGAL_INSTR: 把第一条指令换成保留编码
    patched = patch_image(os.path.join(OUT, "clear.bin"), os.path.join(OUT, "illegal.bin"), 0, 0xFFFFFFFF)
    rc, out = run([patched, "--no-check"])
    check("ILLEGAL_INSTR: rc=2 且 SM0_ERROR=ILLEGAL_INSTR",
          rc == 2 and "SM0_ERROR=ILLEGAL_INSTR" in out, out[-300:])

    print("\n== 反例: CP / 镜像校验 (CMDS §4.1/§6.4/§8.1) ==")
    rc, out = run([os.path.join(OUT, "matmul.bin"), "--warps", "5", "--no-check"])
    check("LAUNCH_REJECT: barrier 内核 + 5 warp", rc == 1 and "LAUNCH_REJECT" in out, out[-300:])

    bad = corrupt_crc(os.path.join(OUT, "clear.bin"), os.path.join(OUT, "badcrc.bin"))
    rc, out = run([bad, "--no-check"])
    check("CRC 不符: rc=1 且 CP CMD_ERROR", rc == 1 and "CMD_ERROR" in out and "CRC" in out, out[-300:])

    print("\n== 运行模式 ==")
    rc, out = run([os.path.join(OUT, "matmul.bin"), "--direct"])
    check("--direct(绕过命令流)与命令流结果一致", rc == 0 and "校验通过" in out, out[-300:])

    script = "si 3\np r10\nl\nst\nw\nx 0x2000000 4\nb 0x40\nbd 0x40\nc\nstat\nlog 3\nq\n"
    rc, out = run([os.path.join(OUT, "clear.bin"), "-d"], stdin=script)
    check("调试模式: si/查看/断点/继续 全程无异常", rc == 0 and "退出调试模式" in out, out[-400:])
    check("调试模式: si 打印了反汇编", "lui r10, 0x10000" in out, out[-400:])
    check("调试模式: x 能看到常量区参数", "常量区" in out and "0xff00ff00" in out, out[-400:])

    logf = os.path.join(OUT, "run.log")
    if os.path.exists(logf):
        os.remove(logf)
    rc, out = run([os.path.join(OUT, "matmul.bin"), "--log", logf, "--log-level", "trace"])
    log = open(logf).read() if os.path.exists(logf) else ""
    check("日志模式: 生成日志文件", rc == 0 and os.path.exists(logf))
    check("日志模式: 含表头与结构化字段",
          "# SimpleGPU 模拟器日志" in log and "cycle=" in log and "level=" in log)
    check("日志模式: trace 级别含逐条指令(带 warp/pc)",
          re.search(r"cycle=\d+\s+warp=\d\s+pc=0x[0-9a-f]{8}.*level=TRACE", log) is not None)
    check("日志模式: 含 LAUNCH 分发与 exit 记录",
          "LAUNCH 分发" in log and "exit @" in log)

    # 命令环回绕(CMDS §2.1): 先把尾部推到接近末尾并让 CP 处理掉, 再发真命令 ⇒ 必须走 NOP 填充 + 回绕
    rc, out = run([os.path.join(OUT, "matmul.bin"), "--cp-fill", "127", "--verbose"])
    check("命令环回绕: NOP 填充后回绕, 内核结果仍然正确",
          rc == 0 and "队列回绕" in out and "校验通过" in out, out[-400:])
    rc, out = run([os.path.join(OUT, "matmul.bin"), "--cp-fill", "126"])
    check("命令环剩余 4 字节: 报错而不是静默错位(§6.1 要求 NOP ≥ 2 字)",
          rc == 1 and "放不下 NOP 填充" in out, out[-300:])

    rc, out = run([os.path.join(OUT, "clear.bin"), "--cp-demo"])
    check("CP 命令自检: SIGNAL/WAIT/READ_REG/FENCE/NOP/IRQ 全通过",
          rc == 0 and "IRQ=0xc" in out, out[-400:])
    dum3 = os.path.join(OUT, "reg.txt")
    rc, out = run([os.path.join(OUT, "clear.bin"), "--cp-demo", "--quiet", "--dump", "0x80000000,1," + dum3])
    reg = int(open(dum3).read().split()[1], 16) if os.path.exists(dum3) else -1
    check("CP 命令自检: READ_REG 把 SM_KERNEL_MASK 落到 DDR(不污染帧缓冲)", reg == 0x3, "得到 0x%x" % reg)

    print("\n== CP 边界与异常路径 ==")

    def img_flags(path):
        d = open(path, "rb").read()
        return struct.unpack_from("<I", d, 12)[0]

    def script(name, lines):
        p = os.path.join(OUT, name)
        open(p, "w").write("\n".join(lines) + "\n")
        return p

    clear_bin = os.path.join(OUT, "clear.bin")
    err_bin = os.path.join(OUT, "err_align.bin")
    f_clear, f_err = img_flags(clear_bin), img_flags(err_bin)

    # [A5] 内核访问 MMIO 窗口 ⇒ MMIO_ACCESS
    rc, out = run([os.path.join(OUT, "err_mmio.bin"), "--warps", "1", "--no-check"])
    check("内核碰 MMIO: SM0_ERROR=MMIO_ACCESS", rc == 2 and "SM0_ERROR=MMIO_ACCESS" in out, out[-300:])

    # [A1] GEMM_STATUS(0x60) 可读 + INT8 GEMM
    dum = os.path.join(OUT, "gemm_c.txt")
    sc = script("gemm.cp", [
        "gemm 0 0 0x10000000 0x10000010 0x10000020 0x00010001 1 0 0 0 0 0 0 0 0 0",
        "read_reg 0x60 0x10004000",
        "doorbell",
    ])
    rc, out = run([clear_bin, "--cp-script", sc, "--no-check", "--quiet",
                   "--write-word", "0x10000000=3", "--write-word", "0x10000010=5",
                   "--dump", "0x10000020,1," + dum])
    c_val = int(open(dum).read().split()[1], 16) if os.path.exists(dum) else -1
    check("GEMM(INT8 1×1×1, A=3,B=5): C=15", rc == 0 and c_val == 15, "C=%d" % c_val)
    dum2 = os.path.join(OUT, "gemm_st.txt")
    rc, out = run([clear_bin, "--cp-script", sc, "--no-check", "--quiet", "--dump", "0x10004000,1," + dum2])
    st_val = int(open(dum2).read().split()[1], 16) if os.path.exists(dum2) else -1
    check("GEMM_STATUS=0x2(BUSY=0,DONE=1,ERROR=0)", st_val == 0x2, "STATUS=0x%x" % st_val)

    # [A2]+[A3] SM*_ERROR 可写; 且前置条件按文档顺序(SM 错误优先于 num_warps)
    sc = script("smerr.cp", [
        "load_kernel 0x2001000 %d 0x3" % os.path.getsize(err_bin),
        "launch 0x20 1 0x%x" % f_err,
        "doorbell",                                  # 运行期 BAD_ALIGN ⇒ SM0_ERROR=3, CP_ERROR=SM_FAULT
        "mmio 0x04 0",                               # host 只清 CP_ERROR(§7: 写任意值清零), 保留 SM0_ERROR
        "launch 0x20 9 0x%x" % f_err,                # §4.1 第 2 条(SM 错误)优先于第 3 条(num_warps)
        "doorbell",
        "mmio 0x04 0",
        "mmio 0x20 0",                               # ③ host 清 SM0_ERROR(§7 的 "写任意值清零")
        "launch 0x20 1 0x%x" % f_err,                # ④ 清干净后应当能再次"分发"
        "doorbell",
    ])
    rc, out = run([err_bin, "--cp-script", sc, "--no-check"])
    check("SM0_ERROR 可写: 清错后 LAUNCH 能再次分发", out.count("LAUNCH 分发") == 2, out[-600:])
    check("§4.1 顺序: SM 错误优先于 num_warps ⇒ STALE_SM_ERROR",
          "STALE_SM_ERROR" in out and "num_warps 必须在 1..8" not in out, out[-600:])
    check("SM0_ERROR 的清除路径里没有 CMD_ERROR(写只读寄存器)",
          "只读寄存器 0x20" not in out, out[-400:])

    # [A4] kernel_pc 下界按文档取 [0, image_size): 0x04 应当被分发(然后取到非法指令), 而不是 CMD_ERROR
    sc = script("pc.cp", ["load_kernel 0x2001000 %d 0x3" % os.path.getsize(clear_bin),
                          "launch 0x04 1 0x%x" % f_clear])
    rc, out = run([clear_bin, "--cp-script", sc, "--no-check"])
    check("kernel_pc=0x04(镜像头区)被分发 ⇒ 运行期 ILLEGAL_INSTR",
          rc == 2 and "ILLEGAL_INSTR" in out and "不在镜像" not in out, out[-300:])

    # [A6] 两个 SM 上装不同镜像 ⇒ 一个 LAUNCH 必须被拒(CMDS §4.2)
    sc = script("twoimg.cp", [
        "load_kernel 0x2001000 %d 0x1" % os.path.getsize(clear_bin),
        "load_kernel 0x2002000 %d 0x2" % os.path.getsize(os.path.join(OUT, "gouraud.bin")),
        "launch 0x20 8 0x%x" % f_clear,
    ])
    rc, out = run([clear_bin, "--cp-script", sc, "--no-check",
                   "--load-extra", os.path.join(OUT, "gouraud.bin") + "@0x2002000"])
    check("两个 SM 装不同镜像: LAUNCH 被拒(不是同一个 kernel)",
          rc == 1 and "不是同一个" in out and "CMD_ERROR" in out, out[-400:])

    # [A7] WRITE_REG CMD_QUEUE_SIZE=0 ⇒ CMD_ERROR, 不能除零崩掉
    sc = script("qs.cp", ["write_reg 0x30 0"])      # 必须是最后一条(§2.1: 只在队列为空时改配置)
    rc, out = run([clear_bin, "--cp-script", sc, "--no-check"])
    check("CMD_QUEUE_SIZE=0: CMD_ERROR 且不崩溃(原实现 SIGFPE)", rc == 1 and "CMD_ERROR" in out, out[-300:])

    # [A8] PARAMS 越出 4KB 常量区 ⇒ CMD_ERROR, 不能断言中止
    sc = script("params.cp", ["params 4080 1 2 3 4 5 6 7 8"])
    rc, out = run([clear_bin, "--cp-script", sc, "--no-check"])
    check("PARAMS 越界: CMD_ERROR 且不中止(原实现 SIGABRT)", rc == 1 and "常量区" in out, out[-300:])

    # [B15] signal_id 落在保留区 ⇒ 命令校验阶段就拒(§10.2)
    sc = script("sig.cp", ["load_kernel 0x2001000 %d 0x3" % os.path.getsize(clear_bin),
                           "launch 0x20 4 0x%x 300 0xFFFF" % f_clear])
    rc, out = run([clear_bin, "--cp-script", sc, "--no-check"])
    check("signal_id=300(保留区): 校验阶段 CMD_ERROR", rc == 1 and "signal_id" in out, out[-300:])

    print("\n== 访存代价模型: 共享 bank 冲突(守一个真实 bug) ==")
    # 冲突算式曾对空 bank 也减 1, 把单个 bank 的冲突抵消成 0 ⇒ 冲突代价恒为 0。这三条用例固定住正确语义。
    rc, out = run([os.path.join(OUT, "sh_conflict.bin"), "--warps", "1", "--no-check"])
    check("8 lane 同 bank 异地址: +7 拍(未修正时是 0 拍/4 周期)",
          "bank 冲突额外 7 拍" in out and "周期     : 10" in out, out[-300:])
    rc, out = run([os.path.join(OUT, "sh_noconflict.bin"), "--warps", "1", "--no-check"])
    check("8 lane 落 8 个 bank: 0 冲突、4 周期", "bank 冲突额外 0 拍" in out and "周期     : 4" in out, out[-300:])
    rc, out = run([os.path.join(OUT, "sh_twobank.bin"), "--warps", "1", "--no-check"])
    check("2 个非空 bank 各 2 个地址: +2 拍(旧算式算成 -4 ⇒ 0)",
          "bank 冲突额外 2 拍" in out, out[-300:])

    print("\n== ⑧ SIMT 栈: 独立模型(按 §2.3.3 从零实现) vs C++ 模拟器 ==")
    p2 = subprocess.run([sys.executable, os.path.join(HERE, "stack_model_check.py")],
                        capture_output=True, text=True)
    check("执行过的 PC 序列逐一比对一致(含 7 层嵌套发散与发散下的 bar.sync)",
          p2.returncode == 0 and "完全一致" in p2.stdout, (p2.stdout + p2.stderr)[-400:])

    print("\n== ⑨ 动画 demo(sw/demo/anim.cpp + sw/kernels/anim.S) ==")
    anim_bin = os.path.join(OUT, "anim.bin")
    W = 64
    dum_a = os.path.join(OUT, "anim_f0.txt")
    rc, out = run([anim_bin, "--warps", "8",
                   "--param", "0=%d" % (W * W), "--param", "4=64", "--param", "8=0x10000000",
                   "--param", "12=0", "--param", "16=%d" % (W - 1), "--param", "20=6",
                   "--param", "24=12", "--param", "28=0xff101020", "--param", "32=0xff204080",
                   "--param", "36=0xffffc040", "--quiet", "--no-check",
                   "--dump", "0x10000000,%d,%s" % (W * W, dum_a)])
    got = [int(l.split()[1], 16) for l in open(dum_a)] if os.path.exists(dum_a) else []
    exp = []
    for idx in range(W * W):
        x, y = idx & (W - 1), idx >> 6
        c = 0xff204080 if (((x + y) >> 5) & 1) else 0xff101020
        if x + y < 12:
            c = 0xffffc040
        exp.append(c)
    check("anim 内核 frame 0 与 Python 参考逐像素一致",
          len(got) == W * W and got == exp, "得到 %d 个像素" % len(got))

    demo = os.path.join(SIMDIR, "build", "anim")
    if os.path.exists(demo):
        frdir = os.path.join(OUT, "frames")
        os.makedirs(frdir, exist_ok=True)
        for f in os.listdir(frdir):
            os.remove(os.path.join(frdir, f))
        p4 = subprocess.run([demo, "--image", anim_bin, "--frames", "6", "--ppm-dir", frdir],
                            capture_output=True, text=True, timeout=300)
        outs = p4.stdout + p4.stderr
        check("动画 demo: 6 帧全部通过画面校验(驱动自带参考模型)",
              p4.returncode == 0 and "全部帧与参考模型一致" in outs, outs[-300:])
        files = sorted(os.listdir(frdir))
        hashes = {open(os.path.join(frdir, f), "rb").read() for f in files}
        check("动画 demo: 6 帧画面互不相同(动态变化)", len(files) == 6 and len(hashes) == 6,
              "文件 %d 个, 不同内容 %d 种" % (len(files), len(hashes)))
        check("动画 demo: 每帧报告周期数(≈%d)" % 0,
              "周期/帧" in outs, outs[-200:])

    print("\n== ⑩ 与参考模型逐元素比对(matmul C 矩阵, 独立于 --check) ==")
    dump = os.path.join(OUT, "c.txt")
    rc, _ = run([os.path.join(OUT, "matmul.bin"), "--quiet", "--no-check", "--dump", "0x10002000,1024," + dump])
    vals = []
    if os.path.exists(dump):
        for line in open(dump):
            vals.append(float(line.split()[2]))
    N = 32
    exp = [sum((((i * 7 + k * 3) % 5) - 2) * (((j * 5 + k * 2) % 7) - 3) for k in range(N))
           for i in range(N) for j in range(N)]
    check("matmul: 1024 个 C 元素与 Python 参考逐位一致",
          len(vals) == 1024 and all(abs(a - b) < 1e-6 for a, b in zip(vals, exp)),
          "读到 %d 个元素" % len(vals))

    print("\n== ⑪ 2D 引擎(GFX.md): 与独立 Python 参考模型逐像素对拍 ==")
    REF2D = os.path.join(HERE, "gfx2d_ref.py")
    FB = 0x1000_0000
    W = H = 64
    PX = W * H

    def gfx_case(name, lines, fb_base=FB, preload=None, extra_args=None, img=None):
        """把同一份命令脚本喂给模拟器与 Python 参考模型, 逐像素比较帧缓冲。"""
        script = write_script(OUT, "gfx_%s" % name, lines)
        got = os.path.join(OUT, "gfx_%s_sim.txt" % name)
        exp = os.path.join(OUT, "gfx_%s_ref.txt" % name)
        args = [img or os.path.join(OUT, "clear.bin"), "--cp-script", script, "--no-check", "--quiet",
                "--dump", "0x%08x,%d,%s" % (fb_base, PX, got)] + list(extra_args or [])
        rc, out = run(args)
        cmd = [sys.executable, REF2D, "--script", script, "--fb-base", "0x%08x" % fb_base,
               "--fb-words", str(PX), "--out", exp]
        for addr, path in (preload or []):
            cmd += ["--preload", addr, path]
        p2 = subprocess.run(cmd, capture_output=True, text=True)
        if rc != 0 or p2.returncode != 0 or not os.path.exists(got):
            check("2D %s: 运行成功" % name, False, (out + p2.stdout + p2.stderr)[-300:])
            return None, None
        a = [int(l.split()[1], 16) for l in open(got)]
        b = [int(l.split()[1], 16) for l in open(exp)]
        bad = [(i, x, y) for i, (x, y) in enumerate(zip(a, b)) if x != y]
        check("2D %s: %d 像素与参考模型逐位一致" % (name, PX), not bad and len(a) == PX and len(b) == PX,
              "不符 %d 个, 首个 %s" % (len(bad), bad[:1]))
        return a, out

    # ① 填充 + 裁剪框(§4.1/§5.5)
    gfx_case("fill_clip", [
        "setreg 0x00 %d" % W, "setreg 0x04 %d" % H, "setreg 0x0c 0",
        "setreg 0x18 16", "setreg 0x1c 8", "setreg 0x10 20", "setreg 0x14 24",
        "fill 0x%08x 0xff203040 0 0 %d %d" % (FB, W, H),
        "fill 0x%08x 0xffa0b0c0 16 16 32 32" % FB,
    ])

    # ② 位块传送: 颜色键 + 双向翻转(§4.2)
    src = 0x8000_0000
    bmp = [("0x%08x" % (src + i * 4), "0x%08x" % ((0xFF000000 | (i * 0x010101)) if i % 3 else 0xFF00FF00))
           for i in range(16)]
    mem = ["mem " + " ".join([bmp[0][0]] + [w for _, w in bmp])]
    gfx_case("blit_colkey_flip", [
        "setreg 0x00 %d" % W, "setreg 0x04 %d" % H, "setreg 0x0c 0",
        "fill 0x%08x 0xff101010 0 0 %d %d" % (FB, W, H),
    ] + mem + [
        "setreg 0x24 0xff00ff00", "setreg 0x28 1",
        "blit 0x%08x 0x%08x 16 20 20 4 4 0x6 0xff00ff00" % (src, FB + (20 * W + 20) * 4),
    ])

    # ③ 格式转换: RGB565 源 → ARGB8888 目标(§6.3)
    two = ["0x%08x" % v for v in (0xF800, 0x07E0, 0x001F, 0x8410, 0xFFFF, 0x0000, 0x1234, 0xABCD)]
    gfx_case("blit_565", [
        "setreg 0x00 %d" % W, "setreg 0x04 %d" % H, "setreg 0x0c 0",
        "fill 0x%08x 0xff000000 0 0 %d %d" % (FB, W, H),
        "mem " + " ".join(["0x80001000"] + two),
        "blit 0x80001000 0x%08x 0 8 8 4 2 0x8 0" % (FB + (8 * W + 8) * 4),
    ])

    # ④ 混合 SRC_OVER + 共享边: top-left 规则保证共享边上的像素只被写一次(§5.3/§6.2)
    def vtx(x, y, r, g, b, a):
        f = lambda v: "0x%08x" % struct.unpack("<I", struct.pack("<f", v))[0]
        return [f(x), f(y), f(r), f(g), f(b), f(a)]

    quad = []
    for v in ((10.0, 10.0, 1, 0, 0, 0.5), (50.0, 10.0, 0, 1, 0, 0.5), (10.0, 50.0, 0, 0, 1, 0.5),
              (50.0, 10.0, 0, 1, 0, 0.5), (50.0, 50.0, 1, 1, 1, 0.5), (10.0, 50.0, 0, 0, 1, 0.5)):
        quad += vtx(*v)
    quad_mem = "mem 0x80002000 " + " ".join(quad)
    gfx_case("draw_blend", [
        "setreg 0x00 %d" % W, "setreg 0x04 %d" % H, "setreg 0x0c 0",
        "fill 0x%08x 0xff000000 0 0 %d %d" % (FB, W, H),
        "setreg 0x20 1",                       # BLEND_MODE = SRC_OVER
        quad_mem,
        "draw 0x80002000 6 0",
    ])

    # ④b 取色约定(GFX.md §2.2 的 ARGB8888): 黑的底上混一层 50% 白 ⇒ 字必须是 0xBF808080。
    #     这条断言守的是"帧缓冲字节序与 SDL/PPM/内核配色一致"—— 引擎若按 [31:24]=R 打包,
    #     混合出来的像素在显示时 R/B 互换(而原样搬移的像素正常), 从画面上极难发现。
    got_blend = raw_blend = None
    script = write_script(OUT, "gfx_argb", [
        "setreg 0x00 %d" % W, "setreg 0x04 %d" % H, "setreg 0x0c 0",
        "fill 0x%08x 0xff000000 0 0 %d %d" % (FB, W, H),
        "setreg 0x20 1",
        "mem 0x80003000 " + " ".join(vtx(4.0, 4.0, 1, 1, 1, 0.5) + vtx(40.0, 4.0, 1, 1, 1, 0.5) +
                                    vtx(4.0, 40.0, 1, 1, 1, 0.5)),
        "draw 0x80003000 3 0",
    ])
    d = os.path.join(OUT, "gfx_argb_words.txt")
    rc, out = run([os.path.join(OUT, "clear.bin"), "--cp-script", script, "--no-check", "--quiet",
                   "--dump", "0x%08x,%d,%s" % (FB, PX, d)])
    words = [int(l.split()[1], 16) for l in open(d)] if os.path.exists(d) else []
    check("2D 取色约定: 黑底 + 50% 白混合 ⇒ 0xBF808080(ARGB8888, A 在最高字节, 与显示路径一致)",
          0xBF808080 in words, "出现过的字: %s" % sorted({hex(v) for v in words})[:4])

    # ⑤ shader → 2D 联调: gouraud 算出顶点, 2D_DRAW 用 wait_id 等它完成(§1/§4.3)
    vs = []
    for v in ((0.0, 0.0, 1, 0, 0, 1), (48.0, 4.0, 0, 1, 0, 1), (4.0, 44.0, 0, 0, 1, 1)):
        vs += vtx(*v)
    cross = [
        "write_reg 0x38 0x20000000",           # FB_BASE 挪开: gouraud 的顶点缓冲在 0x1000_0000
        "setreg 0x00 %d" % W, "setreg 0x04 %d" % H, "setreg 0x0c 0",
        "fill 0x20000000 0xff101820 0 0 %d %d" % (W, H),
        "mem 0x10000000 " + " ".join(vs),       # 顶点输入(恒等变换)
        "load_kernel 0x02001000 %d 0x3" % os.path.getsize(os.path.join(OUT, "gouraud.bin")),
        "params 0 0x10000000 0x10001000 3 0x3f800000 0 0 0x3f800000 0 0 24",  # in/out/N + 单位矩阵 + stride_bytes
        "launch 0x20 4 0x4 1",                  # image_flags=USES_GLOBAL_WRITE, signal_id=1
        "draw 0x10001000 3 1 2 1",              # flags=CLIP_EN, signal=2, wait_id=1
    ]
    vdump = os.path.join(OUT, "gfx_vtx.txt")
    run([os.path.join(OUT, "gouraud.bin"), "--cp-script", write_script(OUT, "gfx_shader_to_2d", cross),
         "--no-check", "--quiet", "--warps", "4", "--dump", "0x10001000,18,%s" % vdump])
    got, out = gfx_case("shader_to_2d", cross, fb_base=0x2000_0000,
                        preload=[("0x10001000", vdump)],
                        extra_args=["--warps", "4"], img=os.path.join(OUT, "gouraud.bin"))
    if got is not None:
        nz = sum(1 for v in got if v != 0xFF101820)
        # 三角形 (0,0)-(48,4)-(4,44) 的面积 = ½|48·44 − 4·4| = 1048; top-left 规则会把
        # 落在边上的像素判给相邻三角形, 所以实测比面积略少几个
        check("2D shader→2D: gouraud 产出的顶点被 2D_DRAW 画成 %d 像素(面积 1048)" % nz,
              1000 <= nz <= 1048, "非背景像素 %d" % nz)

    # ⑥ 错误路径: 引擎报错 ⇒ CP_ERROR = ENGINE_FAULT(8) + 不释放信号量(GFX.md §7.3)
    def err_case(name, lines, want_gfx_err):
        script = write_script(OUT, "gfx_err_%s" % name, lines)
        sem = os.path.join(OUT, "gfx_err_%s_sem.txt" % name)
        rc, out = run([os.path.join(OUT, "clear.bin"), "--cp-script", script, "--no-check",
                       "--dump", "0x03000004,1,%s" % sem])   # SEM[1]: 命令带 signal_id=1
        semv = int(open(sem).read().split()[1], 16) if os.path.exists(sem) else -1
        m = re.search(r"退出码 (\d+)", out)
        check("2D 错误路径 %s: %s ⇒ CP 进错误态、不释放信号量" % (name, want_gfx_err),
              "ENGINE_FAULT" in out and semv == 0 and m and m.group(1) != "0",
              "sem=%s out=%s" % (semv, out[-200:]))

    base = ["setreg 0x00 %d" % W, "setreg 0x04 %d" % H, "setreg 0x0c 0"]
    err_case("bad_reg", base + ["setreg 0x3c 1", "fill 0x%08x 0xff00ff00 0 0 4 4 1" % FB], "BAD_REG")
    err_case("bad_format", ["setreg 0x00 %d" % W, "setreg 0x04 %d" % H, "setreg 0x0c 7",
                            "fill 0x%08x 0xff00ff00 0 0 4 4 1" % FB], "BAD_FORMAT")
    err_case("bad_param", base + ["fill 0x%08x 0xff00ff00 0 0 0 4 1" % FB], "BAD_PARAM")
    err_case("out_of_range", base + ["fill 0x%08x 0xff00ff00 60 60 8 8 1" % FB], "OUT_OF_RANGE")
    err_case("bad_prim", base + ["draw 0x80002000 4 0 1"], "BAD_PRIM")

    # ⑦ 成功路径的可观测性: 2D_STATUS.DONE / IRQ_2D_DONE / 信号量(§7.1/§7.2)
    st = os.path.join(OUT, "gfx_status.txt")
    rc, out = run([os.path.join(OUT, "clear.bin"), "--cp-script",
                   write_script(OUT, "gfx_status_cmd", base + [
                       "fill 0x%08x 0xff00ff00 0 0 8 8 7" % FB,
                       "read_reg 0x68 0x80003000", "read_reg 0x6c 0x80003004", "read_reg 0x14 0x80003008",
                   ]), "--no-check", "--dump", "0x80003000,3,%s" % st])
    vals = [int(l.split()[1], 16) for l in open(st)] if os.path.exists(st) else []
    ok = len(vals) == 3 and (vals[0] & 0x2) and (vals[0] & 0x4) == 0 and vals[1] == 0 and (vals[2] & 0x8)
    check("2D 状态: 2D_STATUS.DONE=1、2D_ERROR=0、IRQ_STATUS[3]=IRQ_2D_DONE", ok, str(vals))
    semv = 0
    rc, out = run([os.path.join(OUT, "clear.bin"), "--cp-script",
                   write_script(OUT, "gfx_sem", base + ["fill 0x%08x 0xff00ff00 0 0 8 8 7" % FB]),
                   "--no-check", "--dump", "0x0300001c,1,%s" % st])
    semv = int(open(st).read().split()[1], 16) if os.path.exists(st) else 0
    check("2D 完成 ⇒ SEM[7] 释放(完成语义与 LAUNCH/GEMM 一致)", semv == 1 and "信号量 SEM[7] += 1" in out,
          "SEM[7]=%s" % semv)

    print("\n== ⑫ car demo: 2D 引擎路径 vs 逐像素 shader, 同一幅画 ==")
    CAR = os.path.join(SIMDIR, "build", "car")
    if not os.path.exists(CAR):
        check("car demo: 二进制存在(直接跑 tests/run_tests.py 时需要先 `make demo`)", False, CAR)
        CAR = None

    def car_run(args, frames=80):
        if CAR is None:
            return 1, "build/car 不存在"         # 80 帧 > 环容量/帧开销 ⇒ 一定跨过多次队列回绕(守 64B 补齐)
        p = subprocess.run([CAR, "--frames", str(frames), "--fps", "0"] + args,
                           capture_output=True, text=True, timeout=300)
        return p.returncode, p.stdout + p.stderr

    for mode, args in (("2D 引擎 FILL/BLIT/DRAW", []), ("逐像素 shader", ["--shader"])):
        rc, out = car_run(args)
        check("car(%s): 80 帧全部通过参考模型校验(含队列回绕与精灵右边界裁剪)" % mode,
              rc == 0 and "全部帧与参考模型一致" in out, out[-300:])

    def car_cycles(args):
        _, out = car_run(args, frames=2)
        m = re.search(r"平均 (\d+) 周期/帧", out)
        return int(m.group(1)) if m else -1

    c_2d, c_sh = car_cycles([]), car_cycles(["--shader"])
    check("car: 2D 引擎把像素工作从 SM 手里拿走(%d 周期/帧 vs 逐像素 %d 周期/帧)" % (c_2d, c_sh),
          0 < c_2d < c_sh // 50, "2D=%d shader=%d" % (c_2d, c_sh))

    print("\n== ⑬ sg 编译器(sw/compiler.py + sw/grammer.md): 编译产物 ≡ 手写内核 ==")
    COMPILER = os.path.join(REPO, "sw", "compiler.py")
    SG_DIR = os.path.join(REPO, "sw", "sg")

    def sg_run(argv, timeout=120):
        p = subprocess.run([sys.executable, COMPILER] + argv, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, p.stdout + p.stderr

    # ① clear.sg: 与手写 clear.S 画同一幅画面(4096 像素逐位比对)
    sg_clear = os.path.join(OUT, "sg_clear.bin")
    rc, out = sg_run([os.path.join(SG_DIR, "clear.sg"), "--image", sg_clear, "-q"])
    check("sg: clear.sg 编译 + 汇编通过", rc == 0 and os.path.exists(sg_clear), out[-300:])
    if rc == 0:
        params = ["--param", "0=4096", "--param", "4=0xff00ff00", "--param", "8=32",
                  "--warps", "4", "--no-check"]
        d_hw = os.path.join(OUT, "sg_clear_hw.txt")
        d_sg = os.path.join(OUT, "sg_clear_me.txt")
        run([os.path.join(OUT, "clear.bin")] + params + ["--dump", "0x10000000,4096," + d_hw])
        run([sg_clear] + params + ["--dump", "0x10000000,4096," + d_sg])
        same = (os.path.exists(d_hw) and os.path.exists(d_sg) and
                open(d_hw).read() == open(d_sg).read())
        check("sg: clear.sg 的产物与手写 clear.S 的 4096 像素逐位一致", same)

    # ② abs.sg: 与手写 abs.S 逐字比对(输入含 -0.0 / +0.0 / NaN, 都是容易写错的边界)
    sg_abs = os.path.join(OUT, "sg_abs.bin")
    rc, out = sg_run([os.path.join(SG_DIR, "abs.sg"), "--image", sg_abs, "-q"])
    check("sg: abs.sg 编译 + 汇编通过(float 比较 + 谓词化取负)", rc == 0 and os.path.exists(sg_abs),
          out[-300:])
    if rc == 0:
        hw_abs = os.path.join(OUT, "sg_abs_hw.bin")
        assemble(os.path.join(REPO, "sw/kernels/abs.S"), hw_abs)
        ww = []
        for i in range(32):
            if i == 0:      x = -0.0
            elif i == 1:    x = 0.0
            elif i == 2:    x = float("nan")
            else:           x = float((i % 7) - 3) * (-1.0 if (i & 1) else 1.0) + 0.25
            ww += ["--write-word", "0x%08x=0x%08x" % (0x10000000 + i * 4,
                                                      struct.unpack("<I", struct.pack("<f", x))[0])]
        base = ["--warps", "4", "--no-check", "--param", "0=0x10000000", "--param", "4=32"]
        d_hw = os.path.join(OUT, "sg_abs_hw.txt")
        d_sg = os.path.join(OUT, "sg_abs_me.txt")
        run([hw_abs] + base + ww + ["--dump", "0x10000000,32," + d_hw])
        run([sg_abs] + base + ww + ["--dump", "0x10000000,32," + d_sg])
        same = (os.path.exists(d_hw) and os.path.exists(d_sg) and
                open(d_hw).read() == open(d_sg).read())
        check("sg: abs.sg 的产物与手写 abs.S 的 32 个字逐位一致(-0.0 不算负、NaN 不被取负)", same)

    # ②b §6.4 的谓词化范围: `when` 体内的**载入**也要屏蔽(否则屏蔽 lane 会发出越界/未对齐请求)
    when_src = ("const -: 0x02000000\nkernel k\n    var N, idx, addr\n    var x : float\n"
                "    param base : int @const[0]\n    param N    : int @const[4]\n"
                "    idx  = tid() + wid() * 8\n    addr = base + idx * 4\n"
                "    when idx < N : x = ldx(addr)\n    exit\nend\n")
    when_sg = os.path.join(OUT, "sg_when.sg")
    when_S = os.path.join(OUT, "sg_when.S")
    open(when_sg, "w").write(when_src)
    rc, out = sg_run([when_sg, "-o", when_S, "-q"])
    text = open(when_S).read() if os.path.exists(when_S) else ""
    check("sg: when 体内的载入被谓词化(@p1 ld.g), 屏蔽 lane 不发访存请求",
          rc == 0 and "@p1 ld.g" in text, text[-200:])

    # ③ relu_swap.sg: 共享内存 + 两次 bar() + while 循环 + 谓词化跨 lane 读
    sg_relu = os.path.join(OUT, "sg_relu_swap.bin")
    rc, out = sg_run([os.path.join(SG_DIR, "relu_swap.sg"), "--image", sg_relu, "-q"])
    check("sg: relu_swap.sg 编译通过(shared/bar/while/谓词化跨 lane 访存)", rc == 0 and os.path.exists(sg_relu),
          out[-300:])
    if rc == 0:
        N = 64
        rnd = __import__("random").Random(7)
        vals = [rnd.uniform(-4.0, 4.0) for _ in range(N)]
        ww = []
        for i, v in enumerate(vals):
            ww += ["--write-word",
                   "0x%08x=0x%08x" % (0x10000000 + i * 4,
                                      struct.unpack("<I", struct.pack("<f", v))[0])]
        d = os.path.join(OUT, "sg_relu_out.txt")
        rc2, out2 = run([sg_relu, "--warps", "4", "--no-check", "--param", "0=0x10000000",
                         "--param", "4=%d" % N] + ww + ["--dump", "0x10000000,%d,%s" % (N, d)])
        got = [l.split()[1].replace("0x", "") for l in open(d)] if os.path.exists(d) else []
        exp = ["%08x" % struct.unpack("<I", struct.pack("<f", abs(vals[(i // 32) * 32 + 31 - (i % 32)])))[0]
               for i in range(N)]
        check("sg: relu_swap 的 64 个字与参考(out[off+i]=|in[off+31-i]|)逐位一致(证明 bar() 真的同步了跨 warp 数据)",
              got == exp, "不符 %d 个" % sum(1 for a2, b2 in zip(got, exp) if a2 != b2))

    # ③b mask_low.sg: 位运算 / 移位 / 短路条件(语言 v0.3 新增的能力)
    sg_mask = os.path.join(OUT, "sg_mask_low.bin")
    sg_maskS = os.path.join(OUT, "sg_mask_low.S")
    rc, out = sg_run([os.path.join(SG_DIR, "mask_low.sg"), "--image", sg_mask, "-o", sg_maskS, "-q"])
    check("sg: mask_low.sg 编译通过(& | >>> + || 短路 + if/else)",
          rc == 0 and os.path.exists(sg_mask), out[-300:])
    if rc == 0:
        text = open(sg_maskS).read()
        check("sg: 位运算走立即形式(andi/ori/srli), 不退化成「先装常量再运算」",
              all(m in text for m in ("andi ", "ori ", "srli ")), text[-200:])
        N = 32
        rnd = __import__("random").Random(11)
        vals = [rnd.getrandbits(32) for _ in range(64)]
        ww = []
        for i, v in enumerate(vals):
            ww += ["--write-word", "0x%08x=0x%08x" % (0x10000000 + i * 4, v)]
        dm = os.path.join(OUT, "sg_mask_out.txt")
        run([sg_mask, "--warps", "4", "--no-check", "--param", "0=0x10000000", "--param", "4=%d" % N]
            + ww + ["--dump", "0x10000000,64," + dm])
        got = [l.split()[1].replace("0x", "") for l in open(dm)] if os.path.exists(dm) else []
        exp = []
        for i, v in enumerate(vals):
            if i < N and v != 0:
                v = ((v & 0xFF) | 0x80) if (i % 2 == 0) else (v >> 1)   # >>> 是逻辑右移
            exp.append("%08x" % v)
        check("sg: mask_low 的 64 个字与参考逐位一致(含 >>> 的逻辑右移语义)",
              got == exp, "不符 %d 个" % sum(1 for a2, b2 in zip(got, exp) if a2 != b2))

    # ③ 错误码: grammer.md §9 的 14 条都要能真的报出来(每条一个最小反例)
    NEG = [
        ("E001", "寄存器溢出", "const -: 0x02000000\nkernel k\n" +
         "\n".join("    var v%d" % i for i in range(1, 33)) + "\nend\n"),
        # E002 不在表里: 见下面的"保留但不可达"断言
        ("E003", "未知指令/函数", "kernel k\n    var a\n    a = nosuch(1)\nend\n"),
        ("E004", "类型不匹配", "kernel k\n    var a : int\n    var b : float\n    a = b\nend\n"),
        ("E005", "循环次数非标量", "const -: 0x02000000\nkernel k\n    var i, N\n"
         "    param N : int @const[0]\n    i = tid()\n    while i < N:\n        i = i + 1\n    end\n"
         "    exit\nend\n"),
        ("E006", "发散路径里 bar()", "const -: 0x02000000\nkernel k\n    var i, N\n"
         "    param N : int @const[0]\n    i = tid()\n    if i < N:\n        bar()\n    end\n"
         "    exit\nend\n"),
        ("E007", "未定义标签", "kernel k\n    var a\n    a = 0\n    goto nowhere\nend\n"),
        ("E008", "重复定义标签", "kernel k\n    var a\n    a = 0\nl:\nl:\n    exit\nend\n"),
        ("E009", "未定义符号", "kernel k\n    var N\n    param N : int @nosuch[0]\n    exit\nend\n"),
        ("E010", "未对齐地址", "const -: 0x02000000\nkernel k\n    var N\n"
         "    param N : int @const[2]\n    exit\nend\n"),
        ("E011", "共享内存溢出", "kernel k\n    __shared__ t : int[5000] shared\n    var a\n"
         "    a = t[0]\n    bar()\n    exit\nend\n"),
        ("E012", "FP 除法/开方", "kernel k\n    var a, b, c : float\n    c = a / b\n    exit\nend\n"),
        ("E013", "共享内存跨 warp 未同步", "kernel k\n    __shared__ t : int[8] shared\n    var a\n"
         "    a = t[0]\n    exit\nend\n"),
        ("E014", "插入指令非法", "kernel k\n    var a\n    ass foo r1, r2\n    exit\nend\n"),
    ]
    bad = []
    for code, why, src in NEG:
        path = os.path.join(OUT, "sg_neg_%s.sg" % code)
        open(path, "w").write(src)
        rc, out = sg_run([path, "--image", os.path.join(OUT, "sg_neg_%s.bin" % code), "-q"])
        if rc == 0 or code not in out:
            bad.append("%s(%s)" % (code, why))
    check("sg: grammer.md 错误码表里 %d 个可触发码都有反例" % len(NEG), not bad,
          "未触发/未报对: " + ", ".join(bad))

    # ④ 代码生成质量护栏: 这三条优化一旦丢失, 指令数会明显回升
    counts = {}
    for name in ("clear", "abs", "relu_swap"):
        _, o = sg_run([os.path.join(SG_DIR, name + ".sg"), "--stats", "-q",
                       "--image", os.path.join(OUT, "sg_c_%s.bin" % name)])
        m = re.search(r"汇编后共 (\d+) 条指令", o)
        counts[name] = int(m.group(1)) if m else -1
    check("sg: 代码生成不退化(clear %d≤34 / abs %d≤22 / relu_swap %d≤52)"
          % (counts["clear"], counts["abs"], counts["relu_swap"]),
          counts["clear"] <= 34 and counts["abs"] <= 22 and counts["relu_swap"] <= 52, str(counts))

    # E002 保留但不可达(与 ISA §2.6 的 SIMT_OVF 同一类): 复合/嵌套条件已改走分支,
    # 只有一个 when 会长期占住谓词 ⇒ 三层嵌套 when 现在能正常编译
    nest = ("const -: 0x02000000\nkernel k\n    var a, b, c, d, e, f, g, h\n"
            "    when a < b : when c < d : when e < f : h = 1\n    exit\nend\n")
    np2 = os.path.join(OUT, "sg_neg_E002b.sg")
    open(np2, "w").write(nest)
    rc, out = sg_run([np2, "--image", os.path.join(OUT, "sg_neg_E002b.bin"), "-q"])
    check("sg: E002(谓词溢出)保留但不可达: 三层嵌套 when 改走分支, 谓词不再累积",
          rc == 0 and "E002" not in out, out[-200:])

    npass = sum(1 for _, ok, _ in results if ok)
    print("\n" + "=" * 70)
    print("回归结果: %d/%d 项通过" % (npass, len(results)))
    for name, ok, detail in results:
        if not ok:
            print("  失败: %s" % name)
    if not keep and os.path.exists(OUT):
        pass
    return 0 if npass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
