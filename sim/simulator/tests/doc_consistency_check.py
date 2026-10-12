#!/usr/bin/env python3
"""跨文档一致性审计: docs/*.md 之间的引用、锚点、编号、编码是否自洽。

为什么需要它: 这几份文档之间存在大量"把决定推给另一份文档"的引用(如 CMDS 的 2D 命令参数
→ GFX.md)。**引用只有在目标真的存在、且说的与这里一致时才算数** —— 手写文档最容易出的两种错
(① 引用了不存在的章节/文件;② 两份文档对同一个编码各写一套)恰好都可以机械检出。

检查项:
  ① 章节引用: 同文档 `§N[.M]` 与跨文档 `X.md §N[.M]` 都指向**存在的**标题(`## N.` / `### N.M`);
  ② 目录锚点: TOC 的 `#anchor` 与标题按同一套 slug 规则算出的一致;
  ③ 变更记录: 每份文档附录里的编号唯一且严格递增;
  ④ 被引用的仓库路径真实存在(除非该行明确写成"建议/待实现"的提案);
  ⑤ `PLAN.md` 不得再被当作"活跃权威"引用(允许作为历史记录出现,但同一行必须标明);
  ⑥ 交叉编码: GFX.md §4 的 `length_words` 与 CMDS.md §2.4/§6.9 的表必须逐条相同;
  ⑦ 交叉寄存器: GFX.md 说的 `2D_STATUS`/`2D_ERROR` 偏移必须出现在 CMDS.md §7 的表里;
  ⑧ 每份文档都要有版本行;SPEC.md §5 权威表里点名的文档必须存在且不是占位。

用法: python3 tests/doc_consistency_check.py     (退出码 0 = 全部一致)
"""

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SIMDIR = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(SIMDIR))
DOCS = os.path.join(REPO, "docs")

# docs/ 里的四份架构文档 + 语言规格 sw/grammer.md(它同样引用 ISA/CMDS 的章节)
DOC_NAMES = ["ISA.md", "CMDS.md", "SPEC.md", "GFX.md", "grammer.md"]
DOC_DIRS = {"grammer.md": os.path.join(REPO, "sw")}
ALIAS = {"ISA": "ISA.md", "CMDS": "CMDS.md", "SPEC": "SPEC.md", "GFX": "GFX.md"}

fails = []
infos = []


def check(name, ok, detail=""):
    print(("  \033[32m✓\033[0m " if ok else "  \033[31m✗\033[0m ") + name + ("" if ok else "   " + detail))
    if not ok:
        fails.append(name)


def slug(title):
    """GitHub 风格的标题锚点: 小写、去掉标点、空格转连字符(保留中文与下划线)。"""
    s = title.strip().lower()
    s = re.sub(r"`", "", s)
    s = re.sub(r"[^\w\u4e00-\u9fff \-]", "", s)
    return s.replace(" ", "-")


def headings(text):
    """→ (top_level_sections:set, subsections:set, [(level, title, anchor)])"""
    top, subs, allh = set(), set(), []
    for line in text.split("\n"):
        m = re.match(r"^(#{2,3})\s+(.*)$", line)
        if not m:
            continue
        level, title = len(m.group(1)), m.group(2).strip()
        allh.append((level, title, slug(title)))
        m2 = re.match(r"^([0-9]+)\.", title)
        if level == 2 and m2:
            top.add(m2.group(1))
        m3 = re.match(r"^([0-9]+\.[0-9]+)", title)
        if level == 3 and m3:
            subs.add(m3.group(1))
    return top, subs, allh


# ---------------------------------------------------------------- 读取文档
docs = {}
for name in DOC_NAMES:
    p = os.path.join(DOC_DIRS.get(name, DOCS), name)
    if not os.path.exists(p):
        check("%s 存在" % name, False, p)
        continue
    text = open(p, encoding="utf-8").read()
    top, subs, allh = headings(text)
    docs[name] = dict(path=p, text=text, top=top, subs=subs, headings=allh,
                      lines=text.split("\n"))

print("== ① 章节引用: §N[.M] 是否指向存在的标题 ==")
bad_refs = []
for name, d in docs.items():
    for i, line in enumerate(d["lines"], 1):
        # 行内出现过的文档名(用于 "CMDS.md 的 §2.4/§6.9"、"ISA.md §3.2、§8" 这类列举)
        doc_marks = [(m.end(), ALIAS[m.group(1)])
                     for m in re.finditer(r"(ISA|CMDS|SPEC|GFX)(?:\.md)?", line)]
        last_target, last_end = None, -1
        for m in re.finditer(r"(?:(ISA|CMDS|SPEC|GFX)(?:\.md)?`?\s*)?§\s*([0-9]+)(?:\.([0-9]+))?", line):
            word, major, minor = m.group(1), m.group(2), m.group(3)
            target = None
            if word:
                target = ALIAS[word]
            else:
                # ① 紧跟在行内上一个"已解析引用"之后, 且只隔着列举分隔符 ⇒ 继承它
                if last_target:
                    gap = line[last_end:m.start()].strip()
                    if gap in ("", "的") or (len(gap) <= 1 and gap in ("/", "、", "–", "-", "和", "与")):
                        target = last_target
                # ② 否则看行内最近的文档名: 中间只隔着 "的" 才继承
                if target is None:
                    cand = [(pos, doc) for pos, doc in doc_marks if pos <= m.start()]
                    if cand:
                        pos, doc = cand[-1]
                        if line[pos:m.start()].strip() in ("", "的"):
                            target = doc
                if target is None:
                    target = name
            if target != name or word:
                last_target, last_end = target, m.end()
            if target not in docs:
                bad_refs.append("%s:%d 引用未加载的 %s" % (name, i, target))
                continue
            td = docs[target]
            if major not in td["top"]:
                bad_refs.append("%s:%d 引用 %s §%s(该文档没有第 %s 节)" % (name, i, target, major, major))
            elif minor and ("%s.%s" % (major, minor)) not in td["subs"]:
                bad_refs.append("%s:%d 引用 %s §%s.%s(没有该小节)" % (name, i, target, major, minor))
check("全部章节引用都指向存在的标题(%d 处)" % sum(len(d["lines"]) for d in docs.values()), not bad_refs,
      "; ".join(bad_refs[:6]))

print("\n== ② 目录锚点 ↔ 标题 ==")
anchor_bad = []
for name, d in docs.items():
    anchors = set(a for _, _, a in d["headings"])
    for i, line in enumerate(d["lines"], 1):
        for m in re.finditer(r"\[([^\]]+)\]\(#([^)]+)\)", line):
            if m.group(2) not in anchors:
                anchor_bad.append("%s:%d #%s" % (name, i, m.group(2)))
check("所有 TOC 锚点都能对上某个标题", not anchor_bad, "; ".join(anchor_bad[:8]))

print("\n== ③ 变更记录编号: 唯一且递增 ==")
num_bad = []
for name, d in docs.items():
    # 只看**最后一个**附录(变更记录): 前面的附录 A(跨文档冲突)、正文里的编号表都不是变更记录
    appidx = [i for i, l in enumerate(d["lines"]) if re.match(r"^##\s*附录", l)]
    app = appidx[-1] if appidx else 0
    body = d["lines"][app:]
    # 按 `### vX → vY` 分节: 每节内编号必须唯一且递增(节与节之间允许倒序/正序两套排法)
    secs, cur = [], None
    for line in body:
        if line.startswith("### "):
            cur = []
            secs.append((line.strip(), cur))
        elif cur is not None and line.startswith("|"):
            first = line.strip("|").split("|")[0].strip()
            if re.fullmatch(r"[0-9]+", first):
                cur.append(int(first))
    nums = []
    if not secs and body:
        # SPEC/GFX 的附录是一张平表(没有 `### vX → vY` 小节): 整体当作一节
        flat = [int(l.strip("|").split("|")[0].strip()) for l in body
                if l.startswith("|") and re.fullmatch(r"[0-9]+", l.strip("|").split("|")[0].strip())]
        if flat:
            secs = [("(平表)", flat)]
    for title, ns in secs:
        if not ns:
            continue
        if ns != sorted(set(ns)):
            num_bad.append("%s %s 编号重复或逆序: %s" % (name, title, ns))
        nums += ns
    if not secs:
        num_bad.append("%s 没有可识别的变更记录小节" % name)
    elif len(nums) != len(set(nums)):
        num_bad.append("%s 各节编号跨节重复: %s" % (name, sorted(nums)))
check("四份文档的变更记录: 每节编号唯一递增、跨节不重复", not num_bad, "; ".join(num_bad))

print("\n== ④ 被引用的仓库路径是否存在 ==")
PROPOSAL = ("建议", "待实现", "待定", "若", "计划", "预留", "拟", "尚未", "提案", "未实现",
            "不存在", "早已", "移到", "改名")
# 仓库内所有文件的路径索引: 文档写作 "tests/isa_ref_check.py" 时, 允许它指向
# 仓库任意深度下的同名后缀(如 sim/simulator/tests/isa_ref_check.py)
repo_files = []
for root, dirs, files in os.walk(REPO):
    dirs[:] = [x for x in dirs if x not in (".git", "build", "node_modules")]
    for f in files:
        repo_files.append(os.path.relpath(os.path.join(root, f), REPO))


def path_exists(rel):
    return any(p == rel or p.endswith("/" + rel) for p in repo_files)


missing = []
proposals = []
for name, d in docs.items():
    for i, line in enumerate(d["lines"], 1):
        for m in re.finditer(r"`([a-zA-Z0-9_./-]+\.(?:py|cpp|hpp|S|sv|md|sh|vh))`", line):
            rel = m.group(1)
            if not re.match(r"^(docs|sim|sw|hw|tests)/", rel):
                continue
            if path_exists(rel):
                continue
            if any(w in line for w in PROPOSAL):
                proposals.append(rel)
            else:
                missing.append("%s:%d %s" % (name, i, rel))
check("文档里提到的仓库文件都真实存在", not missing, "; ".join(missing[:8]))
if proposals:
    infos.append("提案中的文件(该行已标明建议/待实现): %s" % ", ".join(sorted(set(proposals))))

print("\n== ⑤ PLAN.md 只作为历史记录出现 ==")
plan_bad = []
plan_n = 0
for name, d in docs.items():
    # 附录(变更记录)天然是历史记录: 那里的 PLAN 引用是被允许的
    app = next((i for i, l in enumerate(d["lines"]) if re.match(r"^##\s*附录", l)), len(d["lines"]))
    for i, line in enumerate(d["lines"], 1):
        if "PLAN" not in line:
            continue
        plan_n += 1
        if i > app:
            continue
        if re.search(r"PLAN\s*(\.md)?\s*§", line):
            plan_bad.append("%s:%d 把 PLAN 当活跃权威引用(带 §)" % (name, i))
        if not any(w in line for w in ("历史", "计划文档", "可能被移除", "不再依赖", "功能性依赖")):
            plan_bad.append("%s:%d 提到 PLAN.md 但没标明是历史/外部" % (name, i))
check("docs/ 里对 PLAN.md 的 %d 处提及都只是历史记录" % plan_n, not plan_bad, "; ".join(plan_bad[:6]))

print("\n== ⑥ 交叉编码: GFX.md §4 的 length_words ↔ CMDS.md ==")


def gfx_lengths():
    """GFX §4.x 标题里的 `length_words = N` → {opcode: N}"""
    out = {}
    for line in docs["GFX.md"]["lines"]:
        m = re.match(r"^### 4\.[0-9]+ `(2D_[A-Z_]+)`\(0x([0-9A-Fa-f]{2}),\s*`?length_words = ([0-9]+)`?\)", line)
        if m:
            out[int(m.group(2), 16)] = (m.group(1), int(m.group(3)))
    return out


def cmds_lengths():
    """CMDS §2.4 的 2D 行 + §6.9 的表格 → {opcode: N}"""
    out = {}
    for line in docs["CMDS.md"]["lines"]:
        m = re.match(r"^\| 0x([0-9A-Fa-f]{2}) \| `?(2D_[A-Z_]+)`? \| ([0-9]+) \|", line)
        if m:
            out[int(m.group(1), 16)] = (m.group(2), int(m.group(3)))
    return out


gl, cl = gfx_lengths(), cmds_lengths()
if not gl:
    check("能从 GFX.md 解析出 2D 命令的 length_words", False, "标题格式没匹配上")
else:
    check("GFX.md §4 解析出 %d 条 2D 命令" % len(gl), True)
    diffs = ["0x%02X %s: GFX=%s CMDS=%s" % (op, gl.get(op, ("?", "?"))[0],
                                            gl.get(op, (None, None))[1], cl.get(op, (None, None))[1])
             for op in sorted(set(gl) | set(cl)) if gl.get(op, (None, None))[1] != cl.get(op, (None, None))[1]]
    check("GFX.md §4 与 CMDS.md §2.4/§6.9 的 length_words 逐条相同", not diffs, "; ".join(diffs))

# CMDS §2.4 的 2D 行(整段 opcode)也要与逐命令表一致
sec24 = ""
lines24 = docs["CMDS.md"]["lines"]
for i, l in enumerate(lines24):
    if re.match(r"^### 2\.4 ", l):
        sec24 = "\n".join(lines24[i:i + 25])
        break
check("CMDS.md §2.4 的 2D 行给出了同样的长度列表(6/9/8/4/2)",
      "6/9/8/4/2" in sec24, sec24[:120].replace("\n", " "))

print("\n== ⑦ 交叉寄存器: 2D_STATUS / 2D_ERROR 的偏移 ==")
gfx_regs = {}
for m in re.finditer(r"`(0x[0-9A-Fa-f]{2}) (2D_[A-Z_]+)`", docs["GFX.md"]["text"]):
    gfx_regs[m.group(2)] = m.group(1).lower()
cmds_regs = {}
for line in docs["CMDS.md"]["lines"]:
    m = re.match(r"^\| (0x[0-9A-Fa-f]{2}) \| (2D_[A-Z_]+) \|", line)
    if m:
        cmds_regs[m.group(2)] = m.group(1).lower()
check("GFX.md 定义了 2D_STATUS/2D_ERROR 的偏移", set(gfx_regs) >= {"2D_STATUS", "2D_ERROR"}, str(gfx_regs))
check("CMDS.md §7 的 MMIO 表里有同样的偏移(与 GEMM 同约定)",
      all(cmds_regs.get(k) == v for k, v in gfx_regs.items() if k in ("2D_STATUS", "2D_ERROR")),
      "GFX=%s CMDS=%s" % (gfx_regs, cmds_regs))

print("\n== ⑧ 版本行与权威表 ==")
ver_bad = [n for n, d in docs.items() if not re.search(r"^版本\s*[0-9]", d["text"], re.M)]
check("每份文档都有 '版本 x.y' 行", not ver_bad, str(ver_bad))

auth_bad = []
for m in re.finditer(r"`?([A-Z]+\.md)`?", docs["SPEC.md"]["text"]):
    name = m.group(1)
    if name not in DOC_NAMES:
        continue
    p = os.path.join(DOCS, name)
    if not os.path.exists(p) or len(open(p, encoding="utf-8").read().split("\n")) < 20:
        auth_bad.append(name)
check("SPEC.md 点名的文档都存在且不是占位", not auth_bad, str(set(auth_bad)))

print("\n" + "=" * 70)
for line in infos:
    print("提示: " + line)
if fails:
    print("文档一致性审计失败: %d 项" % len(fails))
    for f in fails:
        print("  失败: " + f)
    sys.exit(1)
print("文档一致性审计通过: 引用、锚点、编号、交叉编码全部自洽")
