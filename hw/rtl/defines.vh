`ifndef DEFINES_VH
`define DEFINES_VH

`define RESET_PC 32'h0000_0000

`define WARP_SIZE 8
`define NUM_SM    2
`define WARPS_PER_SM `WARP_SIZE/`NUM_SM
`define MAX_WARP_ID  `NUM_SM*`WARPS_PER_SM-1

`define NUM_GPR 32
`define NUM_PRED 4
`define CLOG_GPR 5
`define CLOG_PRED 2 

`define SIMT_STACK_DEPTH 8
`define SHARED_MEM_SIZE  16*1024
`define INSTR_SRAM_SIZE  16*1024
`define CLOG_INSTR       12      // 指令 SRAM 字数 4096 = 2^12

// MEMORY
`define CONST_BASE       32'h0200_0000
`define CONST_END        32'h0200_0fff
`define KERNEL_IMG_BASE  32'h0200_1000
`define KERNEL_IMG_END   32'h0200_7fff
`define FB_BASE          32'h1000_0000
`define CMD_QUEUE_BASE   32'h0100_0000
`define MMIO_BASE        32'h0000_0000
`define MMIO_END         32'h0000_0fff
`define DDR_BASE         32'h8000_0000

// CMD -- CMD.md$3
`define CMD_NOP         8'h00
`define CMD_WRITE_REG   8'h01   // CMDS §3: 0x01 = WRITE_REG(原宏名 CMD_WAIT_REG 是笔误)
`define CMD_READ_REG    8'h02
`define CMD_LOAD_KERNEL 8'h03
`define CMD_LAUNCH      8'h04
`define CMD_FENCE       8'h05
`define CMD_SIGNAL      8'h06
`define CMD_WAIT        8'h07
`define CMD_IRQ         8'h08
`define CMD_PARAMS      8'h09
`define CMD_GEMM        8'h18

// 2D 命令 -- GFX.md §4(CMDS §3: 0x10..0x17 归 2D)
`define CMD_2D_FILL     8'h10
`define CMD_2D_BLIT     8'h11
`define CMD_2D_DRAW     8'h12
`define CMD_2D_SET_REG  8'h13
`define CMD_2D_FLUSH    8'h14

// MMIO OFFSET-- CMD.md$7
`define MMIO_CP_STATUS      8'h00
`define MMIO_CP_ERROR       8'h04
`define MMIO_CMD_HEAD       8'h08
`define MMIO_CMD_TAIL       8'h0c
`define MMIO_DOORBELL       8'h10
`define MMIO_IRQ_STATUS     8'h14
`define MMIO_IRQ_ENABLE     8'h18
`define MMIO_SM_DONE        8'h1c
`define MMIO_SM0_ERROR      8'h20
`define MMIO_SM1_ERROR      8'h24
`define MMIO_SEM_BASE       8'h28
`define MMIO_CMD_QUEUE_BASE 8'h2c
`define MMIO_CMD_QUEUE_SIZE 8'h30
`define MMIO_CONST_BASE     8'h34
`define MMIO_FB_BASE        8'h38
`define MMIO_DDR_BASE       8'h3c
`define MMIO_CP_RESET       8'h40
`define MMIO_TIMEOUT_CYCLES 8'h44
`define MMIO_SM_KERNEL_MASK 8'h48
`define MMIO_SM0_FAULT_PC   8'h4c
`define MMIO_SM1_FAULT_PC   8'h50
`define MMIO_GEMM_STATUS    8'h60
`define MMIO_GEMM_ERROR     8'h64
`define MMIO_2D_STATUS      8'h68
`define MMIO_2D_ERROR       8'h6c



`define UNUSED_VAR(x)   /* verilator lint_off GENUNNAMED */ \
                        if (1) begin \
                            /* verilator lint_off UNUSED */ \
                            logic [$bits(x)-1:0] __unused = x; \
                            /* verilator lint_on UNUSED */ \
                        end \
                        /* verilator lint_on GENUNNAMED */

`define UNUSED_PIN(x)   /* verilator lint_off PINCONNECTEMPTY */ \
                        . x () \

`define ZERO_PIN(x)     /* verilator lint_off PINCONNECTEMPTY */ \
                        . x(0) \

`endif // DEFINES_VH
