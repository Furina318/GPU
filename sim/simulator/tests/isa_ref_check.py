#!/usr/bin/env python3
"""ISA 参考完整性审计: ISA.md §5 的指令表 ↔ 汇编器 ↔ 译码器 三方对照 + 位域往返。

  ① 助记符集合比对: 文档 §5 列出的每条指令, 汇编器能不能编码、译码器认不认得(反之亦然);
  ② §5 是否真的给了"编码": 有些指令可能只有散文/代码块而没有表行 —— 那样按表查编码就会漏;
  ③ 位域往返: 按文档写的操作数形式生成一行汇编 → 编码 → 用模拟器的译码器反汇编回来,
     再逐字段核对(mnemonic / opcode / funct3 / funct4 / funct2 / setp 的 cond 与 family)。
     这就是 §6.1 要求的 "assembler → 二进制 → disassembler"。

用法: python3 tests/isa_ref_check.py     (退出码 0 = 三方一致)
"""

import os
import re
import struct
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SIMDIR = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(SIMDIR))
ASM = os.path.join(REPO, "sw", "assembly.py")
SIM = os.path.join(SIMDIR, "build", "simulator")
ISA = os.path.join(REPO, "docs", "ISA.md")
TEXT = open(ISA).read()

fails = []


def check(name, ok, detail=""):
    print(("  \033[32m✓\033[0m " if ok else "  \033[31m✗\033[0m ") + name + ("" if ok else "   " + detail))
    if not ok:
        fails.append(name)


def sec(start, end):
    i = TEXT.index(start)
    j = TEXT.index(end, i + len(start))
    return TEXT[i:j]


def rows(block):
    out = []
    for line in block.split("\n"):
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not cells or set(cells[0]) <= set("-: ") or cells[0].startswith("#"):
            continue
        out.append((cells[0], cells[1:]))
    return out


def doc_instructions():
    doc = {}
    for block, f3 in ((sec("### 5.1 ", "### 5.2 "), "000"), (sec("### 5.2 ", "### 5.3 "), "001")):
        for first, rest in rows(block):
            m = re.match(r"`([a-z0-9_.]+)\s+([^`]*)`", first)
            if m and rest and re.fullmatch(r"[01]{4}", rest[0]):
                doc[m.group(1)] = dict(ops=m.group(2), opcode="0110011", funct3=f3, funct4=rest[0])

    b53 = sec("### 5.3 ", "### 5.4 ")
    for chunk, f3, use_f2 in ((b53[b53.index("funct3 = 000"):b53.index("funct3 = 001")], "000", True),
                              (b53[b53.index("funct3 = 001"):], "001", False)):
        for first, rest in rows(chunk):
            m = re.match(r"`([a-z0-9_.]+)\s+([^`]*)`", first)
            if m:
                doc[m.group(1)] = dict(ops=m.group(2), opcode="1010011", funct3=f3,
                                       funct2=(rest[0] if use_f2 and rest else "00"))

    conds = ["eq", "ne", "lt", "ge", "le", "gt"]
    for first, rest in rows(sec("### 5.4 ", "### 5.5 ")):
        c = first.strip("` ")
        if c not in conds or len(rest) < 3:
            continue
        for fam in range(3):
            m = re.match(r"`([a-z0-9_.]+)`", rest[fam])
            if m and m.group(1) not in doc:
                doc[m.group(1)] = dict(ops="pdest, rs1, rs2", opcode="0001011",
                                       funct3=format(fam, "03b"), cond=conds.index(c), cond_name=c)

    b55 = sec("### 5.5 ", "### 5.6 ")
    for first, rest in rows(b55[b55.index("I 型立即数"):b55.index("**大立即数")]):
        m = re.match(r"`([a-z0-9_.]+)\s+([^`]*)`", first)
        if m and rest and re.fullmatch(r"[01]{3}", rest[0]):
            doc[m.group(1)] = dict(ops=m.group(2), opcode="0010011", funct3=rest[0])
    u = b55[b55.index("**大立即数"):]
    tabled = {f.strip("` ").split()[0] for f, _ in rows(u)}
    for m in re.finditer(r"`([a-z0-9_.]+)\s+([a-z0-9, \[\]]+)`", u):
        doc.setdefault(m.group(1), dict(ops=m.group(2).strip(), opcode="0110111", funct3=None,
                                        tabled=m.group(1) in tabled))

    for first, rest in rows(sec("### 5.6 ", "### 5.7 ")):
        m = re.match(r"`([a-z0-9_.]+)\s+([^`]*)`", first)
        if m and len(rest) >= 2 and re.fullmatch(r"[01]{7}", rest[0]):
            doc[m.group(1)] = dict(ops=m.group(2), opcode=rest[0], funct3=rest[1])

    for first, rest in rows(sec("### 5.7 ", "### 5.8 ")):
        m = re.match(r"`([a-z0-9_.]+)([^`]*)`", first)
        if not m or len(rest) < 2:
            continue
        op = rest[0].strip("*` ")
        if not re.fullmatch(r"[01]{7}", op):
            continue
        doc[m.group(1)] = dict(ops=m.group(2).strip(), opcode=op,
                               funct3=None if rest[1] in ("—", "-", "") else rest[1], tabled=True)

    b58 = sec("### 5.8 ", "### 5.9 ")
    m = re.match(r"### 5\.8 分支\(opcode (\d{7})\)", b58)
    doc["br.simt"] = dict(ops="label[, reconv]", opcode=m.group(1) if m else "1100011", funct3=None,
                          tabled=bool(re.search(r"^\|\s*`br\.simt", b58, re.M)))
    return doc


def doc_pseudo():
    out = set()
    for first, _ in rows(sec("### 5.9 ", "### 5.10 ")):
        m = re.match(r"`([a-z0-9_.]+)", first)
        if m:
            out.add(m.group(1))
    return out


def assembler_set():
    s = open(ASM).read()
    names = set()
    for tbl in ("INT_ALU", "FP_ALU", "FMA", "IMM_ALU"):
        m = re.search(rf"^{tbl}\s*=\s*\{{(.*?)\}}", s, re.S | re.M)
        if m:
            names |= set(re.findall(r"'([^']+)'\s*:", m.group(1)))
    names |= {"f2i", "i2f", "lui", "ld.g", "ld.s", "st.g", "st.s", "ipdom", "bar.sync",
              "exit", "tmov", "wmov", "br.simt", "imad"}
    # setp 是 cond × family 组合出来的(不在上面的表里): 从汇编器的条件名表里取
    m = re.search(r"^SETP\s*=\s*\{(.*?)\}", s, re.S | re.M)
    if m:
        names |= {"setp." + c for c in re.findall(r"'([a-z]+)'", m.group(1))}
    return names


def decoder_set():
    # 译码器源码按模块分目录(shader/): 不要在检查里写死模块路径, 找不到就报清楚
    dec = next((os.path.join(SIMDIR, sub, "decode.cpp") for sub in ("shader", "src", "..")
                if os.path.exists(os.path.join(SIMDIR, sub, "decode.cpp"))), None)
    if dec is None:
        raise SystemExit("找不到译码器源码 shader/decode.cpp(模块布局变了? 见 sim/simulator/README.md)")
    d = open(dec).read()
    return set(re.findall(r'INSTPAT\(\s*"[^"]+"\s*,\s*([A-Za-z0-9_.]+)\s*,', d))


REGS = {"rd": "r5", "rs1": "r1", "rs2": "r2", "rs3": "r3", "pdest": "p1",
        "shamt": "3", "imm9": "8", "imm20": "0x2000", "imm": "8"}


def source_line(mn, ops):
    if mn in ("ld.g", "ld.s", "st.g", "st.s"):
        return "%s %s, 8(r1)" % (mn, "r5" if mn.startswith("ld") else "r2")
    if mn == "br.simt":
        return "br.simt target" + (", target" if "reconv" in ops else "")
    parts = []
    for tok in [t.strip() for t in re.split(r",(?![^\[\]]*\])", ops) if t.strip()]:
        for t2 in re.split(r"[\s()\[\]]+", tok):
            if t2 in REGS:
                parts.append(REGS[t2])
            elif t2 in ("tid", "wid"):
                parts.append(t2)
    return "%s %s" % (mn, ", ".join(parts))


def assemble(src_text):
    tmp = os.path.join(SIMDIR, "build", "tests", "_isa_ref.S")
    os.makedirs(os.path.dirname(tmp), exist_ok=True)
    open(tmp, "w").write(src_text)
    img = tmp.replace(".S", ".bin")
    p = subprocess.run([sys.executable, ASM, tmp, "--format", "image", "-o", img],
                       capture_output=True, text=True)
    if p.returncode != 0:
        return None, (p.stdout + p.stderr).strip().split("\n")[-1][:110]
    data = open(img, "rb").read()
    entry, size = struct.unpack_from("<II", data, 4)[0], struct.unpack_from("<I", data, 8)[0]
    words = [struct.unpack_from("<I", data, entry + i * 4)[0] for i in range((size - entry) // 4)]
    return (words, img), ""


def decode_first(img, nwords):
    p = subprocess.run([SIM, img, "-d", "--no-check"],
                       input="dis 0x20 %d\nq\n" % nwords, capture_output=True, text=True)
    return re.findall(r"0x[0-9a-f]{4}\s+0x[0-9a-f]{8}\s+(?:@!?p\d\s+)?([A-Za-z0-9_.]+)", p.stdout + p.stderr)


def main():
    doc, pseudo = doc_instructions(), doc_pseudo()
    aset, dset = assembler_set(), decoder_set()
    check("文档 §5 抽出了全部真指令(期望 >= 46 条)", len(doc) >= 46, "只抽出 %d 条" % len(doc))
    print("文档 §5 真指令 %d 条(含 %d 种 setp 助记符);汇编器 %d 条;译码器 %d 个助记符;伪指令 %d 条\n"
          % (len(doc), sum(1 for k in doc if k.startswith("setp.")), len(aset), len(dset), len(pseudo)))

    print("== (1) 助记符集合 ==")
    check("文档里的指令汇编器都能编码", not (set(doc) - aset), "文档有而汇编器没有: %s" % sorted(set(doc) - aset))
    check("汇编器的指令都在文档里", not (aset - set(doc)), "汇编器有而文档没有: %s" % sorted(aset - set(doc)))
    check("文档里的指令译码器都认得", not (set(doc) - dset), "文档有而译码器没有: %s" % sorted(set(doc) - dset))
    check("译码器的指令都在文档里", not (dset - set(doc) - pseudo),
          "译码器有而文档没有: %s" % sorted(dset - set(doc) - pseudo))

    print("\n== (2) §5 是否给了编码表行(只有散文 => 按表查编码会漏) ==")
    untabled = sorted(k for k, v in doc.items() if v.get("tabled") is False)
    check("§5 的每条指令都有编码表行", not untabled, "只有散文/代码块、没有表行: %s" % untabled)

    print("\n== (3) 位域往返(opcode/funct 与文档一致, 且译码器逐条回读) ==")
    bad, skip = [], []
    for mn in sorted(doc):
        v = doc[mn]
        ops = v.get("ops", "")
        if not ops and mn not in ("bar.sync", "exit"):
            skip.append(mn)
            continue
        line = source_line(mn, ops) if ops else mn
        src = "        %s\ntarget: exit\n" % line if mn == "br.simt" else "        %s\n        exit\n" % line
        packed, err = assemble(src)
        if packed is None:
            bad.append((mn, "汇编失败: " + err))
            continue
        words, img = packed
        w = words[0]
        if (w & 0x7F) != int(v["opcode"], 2):
            bad.append((mn, "opcode 不符: 编码 0b%07b, 文档 0b%07b" % (w & 0x7F, int(v["opcode"], 2))))
            continue
        f4 = ((w >> 29) & 7) << 1 | ((w >> 25) & 1)
        if v.get("funct4") is not None and f4 != int(v["funct4"], 2):
            bad.append((mn, "funct4 不符: 编码 %04b, 文档 %s" % (f4, v["funct4"])))
        if v.get("funct3") is not None and (w >> 12) & 7 != int(v["funct3"], 2):
            bad.append((mn, "funct3 不符: 编码 %03b, 文档 %s" % ((w >> 12) & 7, v["funct3"])))
        if v.get("funct2") is not None and (w >> 25) & 3 != int(v["funct2"], 2):
            bad.append((mn, "funct2 不符: 编码 %02b, 文档 %s" % ((w >> 25) & 3, v["funct2"])))
        if v.get("cond") is not None and (w >> 29) & 7 != v["cond"]:
            bad.append((mn, "setp 的 cond 不符: 编码 %d, 文档 %s" % ((w >> 29) & 7, v["cond_name"])))
        back = decode_first(img, len(words))
        if not back or back[0] != mn:
            bad.append((mn, "译码器回读成 %s(源码: %s)" % (back[0] if back else "?", line)))
    check("每条指令的编码与文档一致、且译码器逐条回读正确", not bad,
          "; ".join("%s: %s" % (a, b) for a, b in bad[:6]))
    if skip:
        print("      (无操作数形式的: %s)" % ", ".join(skip))

    print("\n" + "=" * 70)
    if fails:
        print("审计发现 %d 处不一致:" % len(fails))
        for f in fails:
            print("  - " + f)
    else:
        print("审计通过: 文档 / 汇编器 / 译码器三方一致")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
