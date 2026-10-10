; abs.s — y[i] = |x[i]|, 用 if/else 逼出真实发散(relu 是同形写法: 把 neg 块换成 `mov r14, r0`)
; 这是 ISA.md §7.4 的验收内核: §7.1-§7.3 的分支全部均匀, 一个都不碰 SIMT 栈, 而栈是最容易错的部件。
; 常量: [0]=base [4]=N(元素为 FP32);驱动契约: N ≤ num_warps*8
        lui   r9, 0x2000
        ld.g  r10, 0(r9)          ; base
        ld.g  r11, 4(r9)          ; N
        tmov  r12, tid
        wmov  r13, wid
        slli  r13, r13, 3
        add   r12, r12, r13       ; idx = tid + wid*8
        setp.ltu p1, r12, r11     ; 尾部屏蔽
        slli  r13, r12, 2
        add   r13, r13, r10       ; 元素地址 = base + idx*4
        @p1   ld.g  r14, 0(r13)   ; x
        setp.flt p2, r14, r0      ; p2 = (x < 0) —— 与 +0.0f 比较(r0 的位型就是 +0.0f)
        ipdom join                ; 汇编器也会自动生成;这里写出来是为了让代价看得见(§8.1)
        @p2   br.simt neg, join   ; ← 真正的发散点: x < 0 的 lane 走 neg, 其余走 fall-through
        j join                    ; then 路径(x ≥ 0)什么都不做, 直接去收敛点
neg:
        fsub  r14, r0, r14        ; x < 0 ⇒ -x
join:                             ; 两条路径在此会合;此处的指令只以"完整 mask"执行一次
        @p1   st.g 0(r13), r14
        exit
