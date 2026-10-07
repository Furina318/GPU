; 内核访问 MMIO 窗口: MMIO 属于 CP/host(CMDS §7), shader 碰它必是算错的地址。
; 期望 SM0_ERROR=MMIO_ACCESS(6) 而不是"悄悄写到 CP_RESET 上"。
        addi  r1, r0, 64          ; 0x40 = CP_RESET 的 MMIO 偏移
        addi  r2, r0, 1
        st.g  0(r1), r2           ; 期望在这里报 MMIO_ACCESS
        exit
