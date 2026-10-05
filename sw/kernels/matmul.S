; matmul.s — lane 计算 C 的第 row 行,row = wid*8 + tid
; 常量: [0]=a_base [4]=b_base [8]=c_base [12]=M(未用) [16]=N [20]=K(未用)
; 寄存器: r1=A 列基址 r2=b_base r3=C 行基址 r5=N r6=warp 共享区基址 r7=row*4
;         r8=A 指针 r9=kb 计数 r10=jb 计数 r11..r18=A 块(8 个 k) r19..r26=C 累加(8 个 j)
;         r27=tid*4 r28..r31=临时            (共 30 个通用寄存器,r4 空闲)
        lui   r9, 0x2000
        ld.g  r1, 0(r9)           ; a_base        (注意: r9 先当常量区基址,启动完成后被复用为 kb 计数器)
        ld.g  r2, 4(r9)           ; b_base
        ld.g  r3, 8(r9)           ; c_base
        ld.g  r5, 16(r9)          ; N(本内核要求 = 32)
        tmov  r7, tid
        wmov  r30, wid
        slli  r30, r30, 3
        add   r7, r7, r30         ; row = tid + wid*8
        slli  r7, r7, 2           ; row*4
        add   r1, r1, r7          ; A 第 row 列基址(A 列主序 ⇒ A[k][row] 随 k 以 M*4 步进)
        imad  r3, r7, r5, r3      ; C 行基址 = c_base + row*N*4 ← imad:乘数 N 已在寄存器
        wmov  r6, wid
        slli  r6, r6, 8           ; 共享区基址 = wid*256
        tmov  r27, tid
        slli  r27, r27, 2         ; tid*4:本 lane 负责 B tile 的第 tid 列
        addi  r10, r0, 4          ; jb = 4(倒数,最外层)

jb_loop:                          ; jb: 4→0
        mov   r19, r0             ; C[0..7] 清零(r19..r26,8 条 mov 展开)
        ... (r20..r26) ...
        addi  r9, r0, 4           ; kb = 4(倒数,内层)

kb_loop:                          ; kb: 4→0
        ; ── ① B tile(8k × 8j)搬进本 warp 私有共享区 ──
        ; 全局地址 = b_base + (kb-1)*1024 + (jb-1)*32 + tid*4
        ; 固定 k 时 8 lane 地址连续(32B)⇒ 1 次 burst ✓
        addi  r30, r9, -1
        slli  r30, r30, 10        ; (kb-1)*1024 = k 块偏移(8*N*4,N=32)
        addi  r31, r10, -1
        slli  r31, r31, 5         ; (jb-1)*32 = j 块偏移(8*4)
        add   r28, r2, r30
        add   r28, r28, r31
        add   r28, r28, r27       ; + tid*4
        add   r29, r6, r27        ; 共享槽 = warp 私有区 + tid*4
        ld.g  r30, 0(r28)
        st.s  0(r29), r30         ; addr[4:2] = (tid + kk*8) mod 8 = tid ⇒ 8 lane 落 8 个不同 bank ✓ 零冲突
        addi  r28, r28, 128       ; 下一 k 行(N*4 = 128,N=32)
        addi  r29, r29, 32        ; 共享内下一个 kk 行(tile 是 kk 主序 32B / jj 次序 4B)
        ... (kk=1..7 同模式,共 8 组) ...
        bar.sync                  ; ①→③ 栅栏:B tile 对本 SM 全部 warp 的 lane 可见

        ; ── ② A 列块(8 个 k)搬进寄存器 ──
        addi  r30, r9, -1
        slli  r30, r30, 10
        add   r8, r1, r30         ; A 指针 = 本 lane 的 A 第 row 列 + k 块偏移(必须带 r1!v0.1 漏了)
        ld.g  r11, 0(r8)          ; kk=0(列主序 ⇒ lane 间地址连续 ✓ 1 次 burst)
        addi  r8, r8, 128         ; M*4 = 128(仅当 M=32;M > 63 时 4M 超出 imm9,须改寄存器加法)
        ld.g  r12, 0(r8)          ; kk=1
        ... (kk=2..7 → r13..r18) ...

        ; ── ③ 计算:C[jj] += Σ_kk A[kk] × B_sh[kk][jj] ──
        ; jj 必须展开:8 个累加器分别对应 8 个 jj
        ; kk 必须展开:A[kk] 是按寄存器号选择的,ISA 不支持对寄存器堆变址
        ; 每个 (kk,jj) = 1 条 ld.s + 1 条 fma ⇒ 2 条指令/MAC
        ; B_sh[kk][jj] 地址 = r6 + jj*4 + kk*32:不含 lane 相关项 ⇒ 全 lane 同地址 = 广播(1 周期)
        ld.s  r28, 0(r6)             ; B_sh[0][0]
        fma.f32 r19, r11, r28, r19   ; C[0] += A[0] × B_sh[0][0]
        ld.s  r28, 32(r6)            ; B_sh[1][0]
        fma.f32 r19, r12, r28, r19   ; C[0] += A[1] × B_sh[1][0]
        ... (kk=2..7,累加器保持 r19) ...
        ld.s  r28, 4(r6)             ; B_sh[0][1]
        fma.f32 r20, r11, r28, r20   ; C[1] += A[0] × B_sh[0][1]
        ... (jj=1..7 同模式,累加器换 r20..r26;最大偏移 = 28+224 = 252 ≤ 255 ✓ 正好落在 imm9 内) ...

        bar.sync                  ; ③→① 栅栏:防止下一轮 tile 覆盖尚在读取的共享区
        addi  r9, r9, -1
        setp.ne p2, r9, r0
        ipdom kb_done
        @p2   br.simt kb_loop, kb_done
kb_done:
        ; ── ④ 写回本 jb 块的 8 列 C[row][j] ──
        st.g  0(r3), r19
        st.g  4(r3), r20
        ... (8 条,偏移 0..28) ...
        addi  r3, r3, 32          ; 下一个 j 块
        addi  r10, r10, -1
        setp.ne p2, r10, r0
        ipdom jb_done
        @p2   br.simt jb_loop, jb_done
jb_done:
        exit