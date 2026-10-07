#!/usr/bin/env python3
"""独立的 SIMT 栈/收敛模型:按 ISA.md §2.3.3 从零实现一遍路径推进规则,
再把"执行过的 PC 序列"与 C++ 模拟器 `--trace` 的输出逐条比对。

它不看模拟器的实现,只看文档规则与汇编器给出的反汇编;两边若一致,
说明 §2.3.3 的压栈/切路径/弹栈在 C++ 里被忠实实现了(而不是"参考模型与实现一起错")。

用法: python3 tests/stack_model_check.py
"""

import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SIMDIR = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(SIMDIR))
SIM = os.path.join(SIMDIR, "build", "simulator")
ASM = os.path.join(REPO, "sw", "assembly.py")
ENTRY = 0x20
LANES = 8


def disassemble(src):
    """返回 {pc: (mnemonic, args)} —— 用汇编器自己的反汇编,避免手抄。"""
    p = subprocess.run([sys.executable, ASM, src, "--disasm"], capture_output=True, text=True)
    text = p.stdout + p.stderr
    prog = {}
    for line in text.splitlines():
        m = re.match(r"^([0-9a-f]{4}):\s+([0-9a-f]{8})\s+(.*)$", line.strip())
        if not m:
            continue
        pc = ENTRY + int(m.group(1), 16)
        body = m.group(3).strip()
        inv = False
        pred = 0
        pm = re.match(r"^(@!?p(\d))\s+(.*)$", body)
        if pm:
            inv = pm.group(1).startswith("@!")
            pred = int(pm.group(2))
            body = pm.group(3).strip()
        prog[pc] = {"text": body, "pred": pred, "inv": inv, "raw": line.strip()}
    return prog


def model_run(prog, max_steps=100000):
    """§2.3.3 的状态机。返回执行过的 PC 序列。"""
    pc, active = ENTRY, 0xFF
    r1 = [l for l in range(LANES)]          # tmov r1, tid
    r2 = 0
    p1 = [0] * LANES
    stack = []                              # 每帧: [recov, fall, mask_full, mask_pending, pending]
    recov_pc, recov_valid = 0, False
    seq = []

    def reconv(next_pc):
        nonlocal active
        guard = 0
        while stack and stack[-1][0] == next_pc:
            f = stack[-1]
            if f[4]:                        # pending: 第二条路径到达 ⇒ 弹栈, 完整 mask
                stack.pop()
                active = f[2]
            elif f[1] == f[0]:              # 空路径特例 ⇒ 弹栈, 完整 mask
                stack.pop()
                active = f[2]
            else:                           # taken 路径到达 ⇒ 切到未跳转路径, 不执行收敛点
                f[4] = True
                active = f[3]
                next_pc = f[1]
            guard += 1
            assert guard < 64, "收敛循环失控"
        return next_pc

    for _ in range(max_steps):
        ins = prog.get(pc)
        if ins is None:
            seq.append(pc)
            break
        seq.append(pc)
        t = ins["text"]
        nxt = pc + 4
        if t.startswith("tmov"):
            r1 = [l for l in range(LANES)]
        elif t.startswith("addi"):
            r2 += int(t.split(",")[-1])
        elif t.startswith("setp.lt"):
            p1 = [1 if r1[l] < r2 else 0 for l in range(LANES)]
        elif t.startswith("setp.eq"):
            p1 = [1 if r1[l] == r2 else 0 for l in range(LANES)]
        elif t.startswith("setp.ge"):
            p1 = [1 if r1[l] >= r2 else 0 for l in range(LANES)]
        elif t.startswith("wmov"):
            r1 = [0] * LANES                     # wid = 0(单 warp 单步时)
        elif t.startswith("ipdom"):
            recov_pc, recov_valid = ENTRY + int(t.split()[1].rstrip(","), 16), True
        elif t.startswith("br.simt"):
            taken_mask = sum(1 << l for l in range(LANES)
                             if (active >> l & 1) and ((not p1[l]) if ins["inv"] else p1[l]))
            # p0(无谓词字段/恒真)时 taken = active
            if ins["pred"] == 0 and not ins["inv"]:
                taken_mask = active
            not_taken = active & ~taken_mask
            target = ENTRY + int(t.split()[1].rstrip(","), 16)   # 反汇编里是代码相对地址
            have_recov, recov_valid = recov_valid, False
            if taken_mask == 0:
                pass                                        # 均匀不跳
            elif not_taken == 0:
                nxt = target                                # 均匀跳转
            else:
                assert have_recov, "发散但 recov 无效 ⇒ ERR_NO_RECOV(§5.7)"
                stack.append([recov_pc, pc + 4, active, not_taken, False])
                active, nxt = taken_mask, target
        elif t.startswith("bar.sync"):
            if active != 0xFF or stack:
                seq.append(("BAR_DIVERGENT", pc))       # 期望两边都在这里停下
                break
        elif t.startswith("exit"):
            break
        else:
            pass                                        # 非控制流指令: 不影响 PC/掩码/栈
        pc = reconv(nxt)
    return seq


def sim_trace(img):
    p = subprocess.run([SIM, img, "--warps", "1", "--no-check", "--trace"],
                       capture_output=True, text=True)
    seq = []
    for line in (p.stdout + p.stderr).splitlines():
        m = re.search(r"pc=0x([0-9a-f]{4})\]\[src/exec\.cpp:\d+\] pc=0x([0-9a-f]{4})", line)
        if m and ", " not in line.split("pc=0x")[-1][:8]:
            seq.append(int(m.group(2), 16))
    return seq


def main():
    os.makedirs(os.path.join(SIMDIR, "build", "tests"), exist_ok=True)   # make clean 会删掉 build/
    cases = ["nested7.S", "err_bar_div.S", "err_deadlock.S"]
    # abs 内核(§7.4)也在仓库里, 顺手一起比
    abs_src = os.path.join(REPO, "sw", "kernels", "abs.S")   # 若存在则一并比
    if os.path.exists(abs_src):
        cases.append(abs_src)
    ok = True
    print("SIMT 栈模型(§2.3.3 独立实现) vs C++ 模拟器:")
    for c in cases:
        src = c if os.path.isabs(c) else os.path.join(HERE, c)
        img = os.path.join(SIMDIR, "build", "tests", os.path.basename(src).replace(".S", ".bin"))
        if not os.path.exists(img):
            p3 = subprocess.run([sys.executable, ASM, src, "--format", "image", "-o", img],
                                capture_output=True, text=True)
            if p3.returncode != 0:
                print("  %-16s 汇编失败, 跳过: %s" % (os.path.basename(src), (p3.stdout + p3.stderr).strip()[:200]))
                ok = False
                continue
        prog = disassemble(src)
        mine = model_run(prog)
        stop = mine.pop() if mine and isinstance(mine[-1], tuple) else None
        theirs = sim_trace(img)
        if stop:
            theirs = theirs[:len(mine)]
        same = mine == theirs
        verdict = "一致" if same else "不一致 ✗"
        print("  %-16s 模型 %3d 条 / 模拟器 %3d 条  %s" % (os.path.basename(src), len(mine), len(theirs), verdict))
        if stop:
            print("      (模型与模拟器都应在 %s 处停下 @pc=0x%04x)" % (stop[0], stop[1]))
        if not same:
            ok = False
            for i, (a, b) in enumerate(zip(mine, theirs)):
                if a != b:
                    print("      首个分歧 @第 %d 步: 模型 pc=0x%04x, 模拟器 pc=0x%04x" % (i, a, b))
                    break
            if len(mine) != len(theirs):
                print("      长度不同: 模型 %d, 模拟器 %d" % (len(mine), len(theirs)))
    print("\n" + ("独立模型与模拟器完全一致" if ok else "有不一致, 需人工确认"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
