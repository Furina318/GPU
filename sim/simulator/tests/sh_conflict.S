; 共享 bank 冲突: 8 个 lane 写**同一个 bank**(addr[4:2] 全 0)的 8 个不同地址。
; 期望: bank 冲突额外 7 拍;指令数 4(tmov/slli/st.s/exit)⇒ 周期 10。
; 这条用例守的是一个真实 bug:冲突算式对空 bank 也减了 1,把单个 bank 的冲突正好抵消成 0。
        tmov  r12, tid
        slli  r12, r12, 5          ; addr = tid*32 ⇒ (addr>>2)&7 = 0, 全落 bank0
        st.s  0(r12), r12
        exit
