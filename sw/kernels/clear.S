; clear.s — 用单一颜色填充整个帧缓冲
; 常量: [0]=N(像素数) [4]=color(RGBA8888) [8]=stride_elems
; 驱动契约: stride_elems == num_warps*8(必须严格相等!各 warp 的 idx = tid + wid*8
;   恰好覆盖 0..num_warps*8-1 且无重叠无空洞,前提就是这个步长;写成 ≥8 会漏元素)
; divu 除零会给出全 1,所以 stride_elems 不能为 0;N = 0 由下面的 T==0 判定挡住
; 帧缓冲基址 0x1000_0000
        lui   r10, 0x10000        ; fb_base(4KB 对齐 ⇒ 一条 lui 就够,§4.2)
        lui   r9,  0x2000         ; const_base = 0x0200_0000
        ld.g  r11, 0(r9)          ; N        ← 8 lane 同地址 → 合并器广播
        ld.g  r12, 4(r9)          ; color
        ld.g  r13, 8(r9)          ; stride_elems
        tmov  r16, tid            ; lane id
        wmov  r15, wid            ; warp id
        slli  r15, r15, 3
        add   r16, r16, r15       ; idx = tid + wid*8(各 warp 恰好覆盖 0..num_warps*8-1,无重叠无空洞)
        slli  r14, r16, 2
        add   r14, r14, r10       ; 每 lane 地址 = fb + idx*4
        slli  r15, r13, 2         ; 字节步长 = stride_elems*4(不用 imad:乘数是常量 4,装进寄存器反而多一条)
        addi  r17, r11, -1
        add   r17, r17, r13
        divu  r17, r17, r13       ; T = ceil(N/stride_elems) —— 标量循环次数(为什么必须标量见下)
        setp.eq p3, r17, r0       ; 退化情形 T==0 必须显式挡住,否则下面的 do-while 会死循环
        ipdom done
        @p3   br.simt done, done  ; 全 lane 同一条件 ⇒ 均匀跳转,不压栈
        mov   r18, r0             ; i = 0
loop:
        setp.ltu p1, r16, r11     ; p1 = idx < N。idx 单调增 ⇒ p1 只会由真变假,每轮重算即可(不需要 andp)
        @p1   st.g 0(r14), r12    ; 尾部谓词屏蔽;8 lane 地址连续 → 1 次 32B burst
        add   r14, r14, r15
        add   r16, r16, r13
        addi  r18, r18, 1
        setp.ne p2, r18, r17
        ipdom done
        @p2   br.simt loop, done  ; 条件分支必须显式给收敛点(汇编器在此前生成 ipdom)
done:
        exit