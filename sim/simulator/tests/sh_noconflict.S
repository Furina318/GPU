; 对照: 8 个 lane 落 8 个不同 bank(addr = tid*4)⇒ 零冲突, 周期 = 4。
        tmov  r12, tid
        slli  r12, r12, 2          ; addr = tid*4 ⇒ bank = tid
        st.s  0(r12), r12
        exit
