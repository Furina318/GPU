; 常量区参数(17 字):
;   [0]=N(像素数 = W*H)  
;   [4]=stride_elems(= num_warps*8)  
;   [8]=fb_base
;   [12]=car_x(补码, 可为负: 从画布左侧外驶入用负值)  
;   [16]=car_y
;   [20]=W_mask(= W-1)   
;   [24]=H_shift(= log2 W)
;   [28]=sprite_base 
;   [32]=sprite_w 
;   [36]=sprite_h
;   [40]=grass 
;   [44]=road 
;   [48]=dash(三种颜色)
;   [52]=road_top 
;   [56]=road_h 
;   [60]=dash_y 
;   [64]=dash_h
        lui   r9,  0x2000
        ld.g  r10, 0(r9)          ; N
        ld.g  r11, 4(r9)          ; stride_elems
        ld.g  r12, 8(r9)          ; fb_base
        ld.g  r13, 12(r9)         ; car_x
        ld.g  r14, 16(r9)         ; car_y
        ld.g  r15, 20(r9)         ; W_mask
        ld.g  r16, 24(r9)         ; H_shift
        ld.g  r17, 28(r9)         ; sprite_base
        ld.g  r18, 32(r9)         ; sprite_w
        ld.g  r19, 36(r9)         ; sprite_h
        ld.g  r4,  40(r9)         ; grass
        ld.g  r5,  44(r9)         ; road
        ld.g  r6,  48(r9)         ; dash
        ld.g  r7,  52(r9)         ; road_top
        ld.g  r8,  56(r9)         ; road_h
        ld.g  r1,  60(r9)         ; dash_y
        ld.g  r2,  64(r9)         ; dash_h
        tmov  r22, tid
        wmov  r25, wid
        slli  r25, r25, 3
        add   r22, r22, r25       ; idx = tid + wid*8
        slli  r23, r22, 2
        add   r23, r23, r12       ; 每 lane 地址 = fb + idx*4
        slli  r24, r11, 2         ; 字节步长 = stride_elems*4
        setp.eq p3, r11, r0       ; stride == 0 必须挡住(否则 divu 除零 → T 全 1 → 死循环)
        ipdom done
        @p3   br.simt done, done
        addi  r20, r10, -1
        add   r20, r20, r11
        divu  r20, r20, r11       ; T = ceil(N/stride) —— 标量循环次数
        setp.eq p3, r20, r0       ; N = 0 时 T = 0, 显式挡住(do-while 死循环)
        ipdom done
        @p3   br.simt done, done
        mov   r21, r0             ; i = 0
loop:
        setp.ltu p1, r22, r10     ; p1 = idx < N(尾部屏蔽)
        ; ── 像素坐标 ──
        and   r25, r22, r15       ; x = idx & (W-1)
        srl   r26, r22, r16       ; y = idx >> log2(W)
        ; ── 背景: 草地/公路/中线虚线 ──
        mov   r27, r4             ; 先全草地
        sub   r30, r26, r7        ; y - road_top
        setp.ltu p3, r30, r8      ; 在路面?
        @p3   mov r27, r5         ; 公路
        setp.ne p2, r0, r0        ; p2 = false(拼复合条件前先清)
        sub   r30, r26, r1        ; y - dash_y
        setp.ltu p3, r30, r2      ; p3 = 中线行
        srli  r30, r25, 3         ; x >> 3
        andi  r30, r30, 1         ; 虚线相位(8px 实、8px 虚)
        @p3   setp.ne p2, r30, r0 ; p2 = 中线行 且 实线段(谓词化 setp 拼 AND
        @p2   mov r27, r6         ; 虚线
        ; ── 精灵小车 ──
        sub   r28, r25, r13       ; dx = x - car_x
        sub   r29, r26, r14       ; dy = y - car_y
        setp.ltu p3, r29, r19     ; p3 = dy < sprite_h
        setp.ne p2, r0, r0
        @p3   setp.ltu p2, r28, r18 ; p2 = inside(dx < sprite_w 且 dy < sprite_h)
        imad  r30, r29, r18, r28  ; dy*sprite_w + dx(imad 一条 = mul+add)
        slli  r30, r30, 2
        add   r30, r30, r17       ; sprite 字地址
        @p2   ld.g r31, 0(r30)    ; 读精灵(非 inside lane 不产生请求 ⇒ 无越界访问)
        setp.eq p3, r31, r0       ; 透明键 0
        @p3   mov r31, r27        ; 透明 → 保留背景
        @p2   mov r27, r31        ; 精灵像素覆盖背景
        @p1   st.g 0(r23), r27    ; 写回(尾部谓词屏蔽)
        add   r23, r23, r24
        add   r22, r22, r11
        addi  r21, r21, 1
        setp.ne p3, r21, r20
        ipdom done
        @p3   br.simt loop, done
done:
        exit
