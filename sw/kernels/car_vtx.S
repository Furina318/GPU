; car_vtx.S — 小车 demo 的**顶点**内核(GFX.md §1 的分工: shader 算顶点, 2D 引擎画像素)
;
; 与 car.S 的区别: car.S 逐像素算整幅画面(背景公式 + 精灵搬移), 本内核只输出 **6 个顶点**
; (两个三角形 = 车顶反光/高光四边形), 由 2D 引擎的 2D_DRAW 光栅化(GFX.md §4.3)。
;
; 常量区参数(17 字):
;   [0]=vtx_base   [4]=car_x    [8]=car_y    [12]=body_w   [16]=body_h   [20]=shear
;   [24..36]=顶边颜色 {r,g,b,a}(FP32, 0..1)
;   [40..52]=底边颜色 {r,g,b,a}
; 顶点布局(GFX.md §2.1): {x,y,r,g,b,a} 6×f32 = 24B, 三角形列表顺序
;   (0)=(0,0) (1)=(W,0) (2)=(0,H) | (3)=(W,0) (4)=(W,H) (5)=(0,H)
; 变换: x' = fma(shear, dy, dx) + car_x ; y' = car_y + dy   (shear 让高光随手绘轮廓倾斜)
; 驱动契约: 只用到 6 个 lane(1 个 warp 足够)
        lui   r9,  0x2000
        ld.g  r10, 0(r9)          ; vtx_base
        ld.g  r11, 4(r9)          ; car_x
        ld.g  r12, 8(r9)          ; car_y
        ld.g  r13, 12(r9)         ; body_w
        ld.g  r14, 16(r9)         ; body_h
        ld.g  r15, 20(r9)         ; shear
        ; 比较用的常量(setp 没有立即数形式, 只能是寄存器-寄存器)
        addi  r5,  r0, 1
        addi  r2,  r0, 2
        addi  r3,  r0, 3
        addi  r30, r0, 4
        addi  r31, r0, 5
        addi  r6,  r0, 6          ; 顶点数
        addi  r28, r0, 24         ; stride_bytes
        tmov  r16, tid
        wmov  r17, wid
        slli  r17, r17, 3
        add   r16, r16, r17       ; idx = tid + wid*8
        setp.ltu p1, r16, r6      ; idx < 6(尾部屏蔽)
        ; ── dx = body_w 当 idx ∈ {1, 3, 4}(三角形列表的右边界顶点) ──
        mov   r20, r0
        setp.eq p2, r16, r5
        @p2   mov r20, r13
        setp.eq p2, r16, r3
        @p2   mov r20, r13
        setp.eq p2, r16, r30
        @p2   mov r20, r13
        ; ── dy = body_h 当 idx ∈ {2, 4, 5}(下边界顶点) ──
        mov   r21, r0
        setp.eq p2, r16, r2
        @p2   mov r21, r14
        setp.eq p2, r16, r30
        @p2   mov r21, r14
        setp.eq p2, r16, r31
        @p2   mov r21, r14
        ; ── 顶点变换(单次舍入的 fma, 与参考模型逐位对齐) ──
        fma.f32 r22, r15, r21, r20 ; shear*dy + dx
        fadd  r22, r22, r11        ; + car_x → x'
        fadd  r23, r21, r12        ; car_y + dy → y'
        ; ── 颜色: 顶边(dy==0)用 [24..36], 底边用 [40..52] ──
        ld.g  r24, 24(r9)
        ld.g  r25, 28(r9)
        ld.g  r26, 32(r9)
        ld.g  r27, 36(r9)
        setp.ne p3, r21, r0        ; dy != 0 ⇒ 底边
        @p3   ld.g r24, 40(r9)
        @p3   ld.g r25, 44(r9)
        @p3   ld.g r26, 48(r9)
        @p3   ld.g r27, 52(r9)
        ; ── 写出顶点 ──
        imad  r29, r16, r28, r10   ; vtx 指针 = vtx_base + idx*24
        @p1   st.g 0(r29), r22
        @p1   st.g 4(r29), r23
        @p1   st.g 8(r29), r24
        @p1   st.g 12(r29), r25
        @p1   st.g 16(r29), r26
        @p1   st.g 20(r29), r27
        exit
