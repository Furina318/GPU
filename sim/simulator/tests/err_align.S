; BAD_ALIGN: ld.g 地址未 4B 对齐(ISA §3.2 / §2.6)。期望 SM0_ERROR=BAD_ALIGN(3)
        addi  r1, r0, 2
        ld.g  r2, 0(r1)           ; 地址 = 2 ⇒ [1:0] != 0
        exit
