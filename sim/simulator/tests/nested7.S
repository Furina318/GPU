; 7 层嵌套发散: 验证 SIMT 栈按 LIFO 压/弹, 且 8 lane 的 warp 最多压 7 帧
; (每压一层至少要留 1 个 lane 在另一条路径上 ⇒ 第 8 层必然退化成均匀分支, 不压栈)
; 期望: 结束原因 = 全部 warp exit, 无错误, SIMT 栈最深 = 7
        tmov  r1, tid
        addi  r2, r0, 7
        setp.lt p1, r1, r2
        ipdom done
        @p1   br.simt L1, done
        j done
L1:    addi  r2, r2, -1
        setp.lt p1, r1, r2
        ipdom done
        @p1   br.simt L2, done
        j done
L2:    addi  r2, r2, -1
        setp.lt p1, r1, r2
        ipdom done
        @p1   br.simt L3, done
        j done
L3:    addi  r2, r2, -1
        setp.lt p1, r1, r2
        ipdom done
        @p1   br.simt L4, done
        j done
L4:    addi  r2, r2, -1
        setp.lt p1, r1, r2
        ipdom done
        @p1   br.simt L5, done
        j done
L5:    addi  r2, r2, -1
        setp.lt p1, r1, r2
        ipdom done
        @p1   br.simt L6, done
        j done
L6:    addi  r2, r2, -1
        setp.lt p1, r1, r2
        ipdom done
        @p1   br.simt L7, done
        j done
L7:     j done
done:   exit
