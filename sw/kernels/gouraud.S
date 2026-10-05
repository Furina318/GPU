; gouraud.s — 2D 仿射变换顶点 + 颜色透传(多 warp 通用)
; 常量: [0]=in_base [4]=out_base [8]=N [12]=m00 [16]=m01
;       [20]=m10 [24]=m11 [28]=tx [32]=ty [36]=stride_bytes(=24)
; 顶点: {x,y,r,g,b,a} 6×f32 = 24B;输出同布局,写入顶点输出缓冲
; 驱动契约: N ≤ num_warps*8(本内核不做 grid-stride 循环)
        lui   r9, 0x2000
        ld.g  r10, 0(r9)          ; in_base
        ld.g  r11, 4(r9)          ; out_base
        ld.g  r12, 8(r9)          ; N
        ld.g  r20, 12(r9)         ; m00
        ld.g  r21, 16(r9)         ; m01
        ld.g  r22, 20(r9)         ; m10
        ld.g  r23, 24(r9)         ; m11
        ld.g  r24, 28(r9)         ; tx
        ld.g  r25, 32(r9)         ; ty
        ld.g  r13, 36(r9)         ; stride_bytes
        tmov  r14, tid
        wmov  r15, wid
        slli  r15, r15, 3
        add   r14, r14, r15       ; idx = tid + wid*8(多 warp 分工,v0.1 只用 tid ⇒ 多 warp 会重复写同一顶点)
        setp.ltu p1, r14, r12     ; idx < N(尾部屏蔽)
        imad  r16, r14, r13, r10  ; in 指针  = in_base  + idx*stride_bytes ← 乘数已在寄存器 ⇒ imad 省一条 mul 和一个临时寄存器
        imad  r17, r14, r13, r11  ; out 指针 = out_base + idx*stride_bytes
        @p1   ld.g  r30, 0(r16)   ; x(被屏蔽 lane 不产生请求 ⇒ 无越界访问)
        @p1   ld.g  r31, 4(r16)   ; y
        fmul  r28, r30, r22       ; m10*x
        fma.f32 r28, r31, r23, r28 ; m11*y + m10*x   ← 屏蔽 lane 在算垃圾(见下面前提)
        fadd  r28, r28, r25       ; + ty → y'
        fmul  r27, r31, r21       ; m01*y
        fma.f32 r30, r30, r20, r27 ; x*m00 + m01*y → x'
        fadd  r30, r30, r24       ; + tx
        mov   r31, r28
        @p1   st.g  0(r17), r30
        @p1   st.g  4(r17), r31
        @p1   ld.g  r30, 8(r16)   ; r
        @p1   st.g  8(r17), r30
        @p1   ld.g  r30, 12(r16)  ; g
        @p1   st.g  12(r17), r30
        @p1   ld.g  r30, 16(r16)  ; b
        @p1   st.g  16(r17), r30
        @p1   ld.g  r30, 20(r16)  ; a
        @p1   st.g  20(r17), r30
        exit