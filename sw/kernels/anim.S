; anim.s — 动画内核: 斜条纹背景 + 沿对角线弹跳的菱形块
; 画面完全在 shader 里算出来, host 每帧只改几个参数(帧号与调色板)⇒ "PARAMS + LAUNCH" 就是一帧。
;
; 常量区参数:
;   [0]=N(像素数 = W*H)  [4]=stride_elems(= num_warps*8)  [8]=fb_base
;   [12]=frame(帧号)     [16]=W_mask(= W-1)               [20]=H_shift(= log2 W)
;   [24]=box_size(菱形半径, |dx|+|dy| < box_size 为块内)
;   [28]=bg0  [32]=bg1(条纹两色)  [36]=box_color
; 画布要求: 方形、边长 W 是 2 的幂 ⇒ 用 and/srl 从像素号取出 x/y(非 2 的幂要 divu, 每像素多好几拍)。
; 驱动契约: stride_elems 必须严格等于 num_warps*8(理由同 clear.s); 同一份镜像可反复 LAUNCH。
        lui   r9,  0x2000
        ld.g  r10, 0(r9)          ; N
        ld.g  r11, 4(r9)          ; stride_elems
        ld.g  r12, 8(r9)          ; fb_base
        ld.g  r13, 12(r9)         ; frame
        ld.g  r22, 16(r9)         ; W_mask
        ld.g  r25, 20(r9)         ; H_shift
        ld.g  r31, 24(r9)         ; box_size
        ld.g  r4,  36(r9)         ; box color(块颜色)
        tmov  r16, tid
        wmov  r17, wid
        slli  r17, r17, 3
        add   r16, r16, r17       ; idx = tid + wid*8
        slli  r18, r16, 2
        add   r18, r18, r12       ; 每 lane 地址 = fb + idx*4
        slli  r19, r11, 2         ; 字节步长 = stride_elems*4
        addi  r20, r10, -1
        add   r20, r20, r11
        divu  r20, r20, r11       ; T = ceil(N/stride) —— 标量循环次数
        setp.eq p3, r20, r0       ; N = 0 必须显式挡住(否则 do-while 死循环)
        ipdom done
        @p3   br.simt done, done
        mov   r21, r0             ; i = 0
loop:
        setp.ltu p1, r16, r10     ; p1 = idx < N(尾部屏蔽)
        ; ── 像素坐标 ──
        and   r23, r16, r22       ; x = idx & (W-1)
        srl   r24, r16, r25       ; y = idx >> log2(W)
        ; ── 背景: 斜条纹, 相位随帧号平移 ──
        slli  r26, r13, 2         ; frame*4
        add   r26, r26, r23
        add   r26, r26, r24       ; x + y + frame*4
        srli  r26, r26, 5         ; 条纹宽 32 像素
        andi  r26, r26, 1         ; 0/1
        slli  r26, r26, 2         ; ×4 字节
        addi  r27, r9, 28         ; 调色板基址 = 常量区 + 28
        add   r27, r27, r26
        ld.g  r28, 0(r27)         ; bg = 条纹 ? bg1 : bg0(两个地址同一条 32B 行 ⇒ 1 次请求)
        ; ── 菱形块: 中心 (bx,by) 沿对角线移动, 用 mod W 实现环绕 ──
        slli  r29, r13, 1         ; frame*2
        add   r30, r29, r13       ; frame*3
        and   r30, r30, r22       ; bx
        and   r29, r29, r22       ; by
        sub   r30, r23, r30
        and   r30, r30, r22       ; dx = (x - bx) mod W
        sub   r29, r24, r29
        and   r29, r29, r22       ; dy = (y - by) mod W
        add   r30, r30, r29
        setp.ltu p2, r30, r31     ; p2 = 块内(|dx| + |dy| < box_size ⇒ 菱形)
        @p2   mov r28, r4         ; 块颜色覆盖背景
        @p1   st.g 0(r18), r28    ; 写回(尾部谓词屏蔽)
        add   r18, r18, r19
        add   r16, r16, r11
        addi  r21, r21, 1
        setp.ne p3, r21, r20
        ipdom done
        @p3   br.simt loop, done
done:
        exit
