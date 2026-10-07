; ERR_NO_RECOV: 发散时 recov 有效位为 0(汇编器漏生成 ipdom)。ISA §5.7 / §2.6
        tmov  r1, tid
        setp.eq p1, r1, r0
        ipdom done                
        @p1   br.simt divp, done
        j     done
divp:
        j     done
done:
        exit
