; 死循环(j self): 验证 --max-cycles 会让模拟停成 TIMEOUT 而不是挂死(CMDS §7.3 的 TIMEOUT 兜底)
; 期望: 结束原因 = 超出周期上限, CP_ERROR=TIMEOUT, 退出码 1
spin:   j spin
