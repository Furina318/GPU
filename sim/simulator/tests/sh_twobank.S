; 多个 bank 各 2 个不同地址: bank0={0,32}, bank1={4,36} ⇒ 冲突 = Σ(2−1) = 2 拍。
; 这条用例专门区分"只对非空 bank 求和"的修正:旧算式会算成 1+1−6 = −4 ⇒ 0 拍。
        tmov  r12, tid
        srli  r12, r12, 2
        slli  r12, r12, 2          ; (tid>>2)*4  ⇒ 0 或 4
        tmov  r13, tid
        srli  r13, r13, 1
        andi  r13, r13, 1
        slli  r13, r13, 5          ; ((tid>>1)&1)*32
        add   r12, r12, r13        ; lane0,1→0; 2,3→32; 4,5→4; 6,7→36
        st.s  0(r12), r12
        exit
