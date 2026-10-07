; barrier 死锁: warp 0 提前 exit, warp 1 在 bar.sync 等一个永远不到的 warp。
; 每 SM 只有 warp 1 存活且已 stall ⇒ 模拟器应立刻报 DEADLOCK 而不是等到周期上限。
        wmov  r1, wid
        setp.eq p1, r1, r0        ; warp 0 为真(wid=0)⇒ 全体 lane 同一条件 ⇒ 均匀分支
        ipdom done
        @p1   br.simt skip, done
        bar.sync                  ; warp 1 在此等待
        j     done
skip:
        exit                      ; warp 0 直接退出
done:
        exit
