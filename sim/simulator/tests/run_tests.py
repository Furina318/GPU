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
    for k in ("clear", "gouraud", "matmul"):
        ok, msg = assemble(os.path.join(REPO, "sw/kernels", k + ".S"), os.path.join(OUT, k + ".bin"))
        check("汇编 sw/kernels/%s.S" % k, ok, msg)
    for f in ("nested7", "spin", "err_align", "err_bar_div", "err_deadlock", "err_recov", "err_mmio"):
        ok, msg = assemble(os.path.join(HERE, f + ".S"), os.path.join(OUT, f + ".bin"))
        check("汇编 tests/%s.S" % f, ok, msg)

    print("\n== 正例: 三个内核结果校验 ==")
    for k in ("clear", "gouraud", "matmul"):
        rc, out = run([os.path.join(OUT, k + ".bin")])
        check("%s: 退出码 0 且校验通过" % k, rc == 0 and "校验通过" in out,
              "rc=%d" % rc if rc else out[-300:])

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
    check("调试模式: x 能看到常量区参数", "常量区" in out and "0x00001000" in out, out[-400:])

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

    print("\n== ⑧ SIMT 栈: 独立模型(按 §2.3.3 从零实现) vs C++ 模拟器 ==")
    p2 = subprocess.run([sys.executable, os.path.join(HERE, "stack_model_check.py")],
                        capture_output=True, text=True)
    check("执行过的 PC 序列逐一比对一致(含 7 层嵌套发散与发散下的 bar.sync)",
          p2.returncode == 0 and "完全一致" in p2.stdout, (p2.stdout + p2.stderr)[-400:])

    print("\n== ⑨ 与参考模型逐元素比对(matmul C 矩阵, 独立于 --check) ==")
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
