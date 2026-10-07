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
        addi  r3, r3, 96          ; 指向本行最后一个 8 列块(N*4-32 = 96):jb 从 4 递减,写指针必须从尾部往前
        wmov  r6, wid
        slli  r6, r6, 8           ; 共享区基址 = wid*256
        tmov  r27, tid
        slli  r27, r27, 2         ; tid*4:本 lane 负责 B tile 的第 tid 列
        addi  r10, r0, 4          ; jb = 4(倒数,最外层)

jb_loop:                          ; jb: 4→0
        mov   r19, r0             ; C[0..7] 清零(r19..r26,8 条 mov 展开)
        mov   r20, r0             ; C[J] 清零(展开)
        mov   r21, r0             ; C[J] 清零(展开)
        mov   r22, r0             ; C[J] 清零(展开)
        mov   r23, r0             ; C[J] 清零(展开)
        mov   r24, r0             ; C[J] 清零(展开)
        mov   r25, r0             ; C[J] 清零(展开)
        mov   r26, r0             ; C[J] 清零(展开)
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
        ld.g  r30, 0(r28)
        st.s  0(r29), r30
        addi  r28, r28, 128
        addi  r29, r29, 32
        ld.g  r30, 0(r28)
        st.s  0(r29), r30
        addi  r28, r28, 128
        addi  r29, r29, 32
        ld.g  r30, 0(r28)
        st.s  0(r29), r30
        addi  r28, r28, 128
        addi  r29, r29, 32
        ld.g  r30, 0(r28)
        st.s  0(r29), r30
        addi  r28, r28, 128
        addi  r29, r29, 32
        ld.g  r30, 0(r28)
        st.s  0(r29), r30
        addi  r28, r28, 128
        addi  r29, r29, 32
        ld.g  r30, 0(r28)
        st.s  0(r29), r30
        addi  r28, r28, 128
        addi  r29, r29, 32
        ld.g  r30, 0(r28)
        st.s  0(r29), r30
        addi  r28, r28, 128
        addi  r29, r29, 32
        bar.sync                  ; ①→③ 栅栏:B tile 对本 SM 全部 warp 的 lane 可见

        ; ── ② A 列块(8 个 k)搬进寄存器 ──
        addi  r30, r9, -1
        slli  r30, r30, 10
        add   r8, r1, r30         ; A 指针 = 本 lane 的 A 第 row 列 + k 块偏移(必须带 r1!v0.1 漏了)
        ld.g  r11, 0(r8)          ; kk=0(列主序 ⇒ lane 间地址连续 ✓ 1 次 burst)
        addi  r8, r8, 128         ; M*4 = 128(仅当 M=32;M > 63 时 4M 超出 imm9,须改寄存器加法)
        ld.g  r12, 0(r8)          ; kk=1
        addi  r8, r8, 128
        ld.g  r13, 0(r8)          ; kk=2
        addi  r8, r8, 128
        ld.g  r14, 0(r8)
        addi  r8, r8, 128
        ld.g  r15, 0(r8)
        addi  r8, r8, 128
        ld.g  r16, 0(r8)
        addi  r8, r8, 128
        ld.g  r17, 0(r8)
        addi  r8, r8, 128
        ld.g  r18, 0(r8)

        ; ── ③ 计算:C[jj] += Σ_kk A[kk] × B_sh[kk][jj] ──
        ; jj 必须展开:8 个累加器分别对应 8 个 jj
        ; kk 必须展开:A[kk] 是按寄存器号选择的,ISA 不支持对寄存器堆变址
        ; 每个 (kk,jj) = 1 条 ld.s + 1 条 fma ⇒ 2 条指令/MAC
        ; B_sh[kk][jj] 地址 = r6 + jj*4 + kk*32:不含 lane 相关项 ⇒ 全 lane 同地址 = 广播(1 周期)
        ld.s  r28, 0(r6)             ; B_sh[0][0]
        fma.f32 r19, r11, r28, r19   ; C[0] += A[0] × B_sh[0][0]
        ld.s  r28, 32(r6)            ; B_sh[1][0]
        fma.f32 r19, r12, r28, r19   ; C[0] += A[1] × B_sh[1][0]
        ld.s  r28, 64(r6)             ; B_sh[2][0]
        fma.f32 r19, r13, r28, r19   ; C[0] += A[2] × B_sh[2][0]
        ld.s  r28, 96(r6)             ; B_sh[3][0]
        fma.f32 r19, r14, r28, r19   ; C[0] += A[3] × B_sh[3][0]
        ld.s  r28, 128(r6)             ; B_sh[4][0]
        fma.f32 r19, r15, r28, r19   ; C[0] += A[4] × B_sh[4][0]
        ld.s  r28, 160(r6)             ; B_sh[5][0]
        fma.f32 r19, r16, r28, r19   ; C[0] += A[5] × B_sh[5][0]
        ld.s  r28, 192(r6)             ; B_sh[6][0]
        fma.f32 r19, r17, r28, r19   ; C[0] += A[6] × B_sh[6][0]
        ld.s  r28, 224(r6)             ; B_sh[7][0]
        fma.f32 r19, r18, r28, r19   ; C[0] += A[7] × B_sh[7][0]
        ld.s  r28, 4(r6)             ; B_sh[0][1]
        fma.f32 r20, r11, r28, r20   ; C[1] += A[0] × B_sh[0][1]
        ld.s  r28, 36(r6)             ; B_sh[1][1]
        fma.f32 r20, r12, r28, r20   ; C[1] += A[1] × B_sh[1][1]
        ld.s  r28, 68(r6)             ; B_sh[2][1]
        fma.f32 r20, r13, r28, r20   ; C[1] += A[2] × B_sh[2][1]
        ld.s  r28, 100(r6)             ; B_sh[3][1]
        fma.f32 r20, r14, r28, r20   ; C[1] += A[3] × B_sh[3][1]
        ld.s  r28, 132(r6)             ; B_sh[4][1]
        fma.f32 r20, r15, r28, r20   ; C[1] += A[4] × B_sh[4][1]
        ld.s  r28, 164(r6)             ; B_sh[5][1]
        fma.f32 r20, r16, r28, r20   ; C[1] += A[5] × B_sh[5][1]
        ld.s  r28, 196(r6)             ; B_sh[6][1]
        fma.f32 r20, r17, r28, r20   ; C[1] += A[6] × B_sh[6][1]
        ld.s  r28, 228(r6)             ; B_sh[7][1]
        fma.f32 r20, r18, r28, r20   ; C[1] += A[7] × B_sh[7][1]
        ld.s  r28, 8(r6)             ; B_sh[0][2]
        fma.f32 r21, r11, r28, r21   ; C[2] += A[0] × B_sh[0][2]
        ld.s  r28, 40(r6)             ; B_sh[1][2]
        fma.f32 r21, r12, r28, r21   ; C[2] += A[1] × B_sh[1][2]
        ld.s  r28, 72(r6)             ; B_sh[2][2]
        fma.f32 r21, r13, r28, r21   ; C[2] += A[2] × B_sh[2][2]
        ld.s  r28, 104(r6)             ; B_sh[3][2]
        fma.f32 r21, r14, r28, r21   ; C[2] += A[3] × B_sh[3][2]
        ld.s  r28, 136(r6)             ; B_sh[4][2]
        fma.f32 r21, r15, r28, r21   ; C[2] += A[4] × B_sh[4][2]
        ld.s  r28, 168(r6)             ; B_sh[5][2]
        fma.f32 r21, r16, r28, r21   ; C[2] += A[5] × B_sh[5][2]
        ld.s  r28, 200(r6)             ; B_sh[6][2]
        fma.f32 r21, r17, r28, r21   ; C[2] += A[6] × B_sh[6][2]
        ld.s  r28, 232(r6)             ; B_sh[7][2]
        fma.f32 r21, r18, r28, r21   ; C[2] += A[7] × B_sh[7][2]
        ld.s  r28, 12(r6)             ; B_sh[0][3]
        fma.f32 r22, r11, r28, r22   ; C[3] += A[0] × B_sh[0][3]
        ld.s  r28, 44(r6)             ; B_sh[1][3]
        fma.f32 r22, r12, r28, r22   ; C[3] += A[1] × B_sh[1][3]
        ld.s  r28, 76(r6)             ; B_sh[2][3]
        fma.f32 r22, r13, r28, r22   ; C[3] += A[2] × B_sh[2][3]
        ld.s  r28, 108(r6)             ; B_sh[3][3]
        fma.f32 r22, r14, r28, r22   ; C[3] += A[3] × B_sh[3][3]
        ld.s  r28, 140(r6)             ; B_sh[4][3]
        fma.f32 r22, r15, r28, r22   ; C[3] += A[4] × B_sh[4][3]
        ld.s  r28, 172(r6)             ; B_sh[5][3]
        fma.f32 r22, r16, r28, r22   ; C[3] += A[5] × B_sh[5][3]
        ld.s  r28, 204(r6)             ; B_sh[6][3]
        fma.f32 r22, r17, r28, r22   ; C[3] += A[6] × B_sh[6][3]
        ld.s  r28, 236(r6)             ; B_sh[7][3]
        fma.f32 r22, r18, r28, r22   ; C[3] += A[7] × B_sh[7][3]
        ld.s  r28, 16(r6)             ; B_sh[0][4]
        fma.f32 r23, r11, r28, r23   ; C[4] += A[0] × B_sh[0][4]
        ld.s  r28, 48(r6)             ; B_sh[1][4]
        fma.f32 r23, r12, r28, r23   ; C[4] += A[1] × B_sh[1][4]
        ld.s  r28, 80(r6)             ; B_sh[2][4]
        fma.f32 r23, r13, r28, r23   ; C[4] += A[2] × B_sh[2][4]
        ld.s  r28, 112(r6)             ; B_sh[3][4]
        fma.f32 r23, r14, r28, r23   ; C[4] += A[3] × B_sh[3][4]
        ld.s  r28, 144(r6)             ; B_sh[4][4]
        fma.f32 r23, r15, r28, r23   ; C[4] += A[4] × B_sh[4][4]
        ld.s  r28, 176(r6)             ; B_sh[5][4]
        fma.f32 r23, r16, r28, r23   ; C[4] += A[5] × B_sh[5][4]
        ld.s  r28, 208(r6)             ; B_sh[6][4]
        fma.f32 r23, r17, r28, r23   ; C[4] += A[6] × B_sh[6][4]
        ld.s  r28, 240(r6)             ; B_sh[7][4]
        fma.f32 r23, r18, r28, r23   ; C[4] += A[7] × B_sh[7][4]
        ld.s  r28, 20(r6)             ; B_sh[0][5]
        fma.f32 r24, r11, r28, r24   ; C[5] += A[0] × B_sh[0][5]
        ld.s  r28, 52(r6)             ; B_sh[1][5]
        fma.f32 r24, r12, r28, r24   ; C[5] += A[1] × B_sh[1][5]
        ld.s  r28, 84(r6)             ; B_sh[2][5]
        fma.f32 r24, r13, r28, r24   ; C[5] += A[2] × B_sh[2][5]
        ld.s  r28, 116(r6)             ; B_sh[3][5]
        fma.f32 r24, r14, r28, r24   ; C[5] += A[3] × B_sh[3][5]
        ld.s  r28, 148(r6)             ; B_sh[4][5]
        fma.f32 r24, r15, r28, r24   ; C[5] += A[4] × B_sh[4][5]
        ld.s  r28, 180(r6)             ; B_sh[5][5]
        fma.f32 r24, r16, r28, r24   ; C[5] += A[5] × B_sh[5][5]
        ld.s  r28, 212(r6)             ; B_sh[6][5]
        fma.f32 r24, r17, r28, r24   ; C[5] += A[6] × B_sh[6][5]
        ld.s  r28, 244(r6)             ; B_sh[7][5]
        fma.f32 r24, r18, r28, r24   ; C[5] += A[7] × B_sh[7][5]
        ld.s  r28, 24(r6)             ; B_sh[0][6]
        fma.f32 r25, r11, r28, r25   ; C[6] += A[0] × B_sh[0][6]
        ld.s  r28, 56(r6)             ; B_sh[1][6]
        fma.f32 r25, r12, r28, r25   ; C[6] += A[1] × B_sh[1][6]
        ld.s  r28, 88(r6)             ; B_sh[2][6]
        fma.f32 r25, r13, r28, r25   ; C[6] += A[2] × B_sh[2][6]
        ld.s  r28, 120(r6)             ; B_sh[3][6]
        fma.f32 r25, r14, r28, r25   ; C[6] += A[3] × B_sh[3][6]
        ld.s  r28, 152(r6)             ; B_sh[4][6]
        fma.f32 r25, r15, r28, r25   ; C[6] += A[4] × B_sh[4][6]
        ld.s  r28, 184(r6)             ; B_sh[5][6]
        fma.f32 r25, r16, r28, r25   ; C[6] += A[5] × B_sh[5][6]
        ld.s  r28, 216(r6)             ; B_sh[6][6]
        fma.f32 r25, r17, r28, r25   ; C[6] += A[6] × B_sh[6][6]
        ld.s  r28, 248(r6)             ; B_sh[7][6]
        fma.f32 r25, r18, r28, r25   ; C[6] += A[7] × B_sh[7][6]
        ld.s  r28, 28(r6)             ; B_sh[0][7]
        fma.f32 r26, r11, r28, r26   ; C[7] += A[0] × B_sh[0][7]
        ld.s  r28, 60(r6)             ; B_sh[1][7]
        fma.f32 r26, r12, r28, r26   ; C[7] += A[1] × B_sh[1][7]
        ld.s  r28, 92(r6)             ; B_sh[2][7]
        fma.f32 r26, r13, r28, r26   ; C[7] += A[2] × B_sh[2][7]
        ld.s  r28, 124(r6)             ; B_sh[3][7]
        fma.f32 r26, r14, r28, r26   ; C[7] += A[3] × B_sh[3][7]
        ld.s  r28, 156(r6)             ; B_sh[4][7]
        fma.f32 r26, r15, r28, r26   ; C[7] += A[4] × B_sh[4][7]
        ld.s  r28, 188(r6)             ; B_sh[5][7]
        fma.f32 r26, r16, r28, r26   ; C[7] += A[5] × B_sh[5][7]
        ld.s  r28, 220(r6)             ; B_sh[6][7]
        fma.f32 r26, r17, r28, r26   ; C[7] += A[6] × B_sh[6][7]
        ld.s  r28, 252(r6)             ; B_sh[7][7]
        fma.f32 r26, r18, r28, r26   ; C[7] += A[7] × B_sh[7][7]

        bar.sync                  ; ③→① 栅栏:防止下一轮 tile 覆盖尚在读取的共享区
        addi  r9, r9, -1
        setp.ne p2, r9, r0
        ipdom kb_done
        @p2   br.simt kb_loop, kb_done
kb_done:
        ; ── ④ 写回本 jb 块的 8 列 C[row][j] ──
        st.g  0(r3), r19
        st.g  4(r3), r20
        st.g  8(r3), r21
        st.g  12(r3), r22
        st.g  16(r3), r23
        st.g  20(r3), r24
        st.g  24(r3), r25
        st.g  28(r3), r26
        addi  r3, r3, -32         ; 退到下一个(更低列号)的 8 列块 —— 与 jb 递减方向一致
        addi  r10, r10, -1
        setp.ne p2, r10, r0
        ipdom jb_done
        @p2   br.simt jb_loop, jb_done
jb_done:
        exit