; BAR_DIVERGENT: 在发散路径里执行 bar.sync(ISA §2.4 / §2.6)。期望 SM0_ERROR=BAR_DIVERGENT(4)
        tmov  r1, tid
        setp.eq p1, r1, r0        ; 只有 lane 0 为真 ⇒ 会发散
        ipdom done
        @p1   br.simt divp, done
        j     done                ; 未跳转 lane(1..7)先到收敛点
divp:                             ; taken 路径 = lane 0,active_mask=0x01
        bar.sync                  ; 期望在此报 BAR_DIVERGENT
        j     done
done:
        exit
