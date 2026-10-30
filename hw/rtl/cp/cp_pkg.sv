`ifndef CP_PKG_SV
`define CP_PKG_SV

`include "defines.vh"

package cp_pkg;

    // CP_ERROR
    typedef enum logic [3:0] {
        CPERR_NONE          = 4'd0,
        CPERR_CMD           = 4'd1,
        CPERR_LAUNCH_REJECT = 4'd2,
        CPERR_BUS           = 4'd3,
        CPERR_TIMEOUT       = 4'd4,
        CPERR_ILLEGAL_STATE = 4'd5,
        CPERR_STALE_SM_ERROR= 4'd6,
        CPERR_SM_FAULT      = 4'd7,
        CPERR_ENGINE_FAULT  = 4'd8
    } cp_err_t;

    // 引擎号
    typedef enum logic [1:0] { ENG_NONE = 2'd0, ENG_2D = 2'd1, ENG_GEMM = 2'd2 } cp_eng_t;

    // 引擎状态
    localparam int ENG_ST_BUSY  = 0;
    localparam int ENG_ST_DONE  = 1;
    localparam int ENG_ST_ERROR = 2;

    // 引擎 payload:GEMM 16 字(64B)、2D 最大 9 字 ⇒ 一条命令最多 16 字(64B)

    // 常量区与镜像
    localparam logic [31:0] CONST_SIZE     = 32'h1000;        // 4KB
    localparam logic [31:0] IMG_MAGIC      = 32'h5347_4B31;   // "SGK1"
    localparam logic [31:0] IMG_HDR_BYTES  = 32'd32;
    localparam logic [15:0] SEM_NONE       = 16'hFFFF;

    // 各命令的 length_words
    localparam logic [15:0] LEN_WRITE_REG   = 16'd4;
    localparam logic [15:0] LEN_READ_REG    = 16'd4;
    localparam logic [15:0] LEN_LOAD_KERNEL = 16'd5;
    localparam logic [15:0] LEN_LAUNCH      = 16'd5;
    localparam logic [15:0] LEN_FENCE       = 16'd2;
    localparam logic [15:0] LEN_SIGNAL      = 16'd4;
    localparam logic [15:0] LEN_IRQ         = 16'd3;
    localparam logic [15:0] LEN_GEMM        = 16'd16;

    // LAUNCH flags
    localparam int FLAG_USES_BARRIER      = 0;
    localparam int FLAG_USES_SHARED       = 1;
    // bit2 USES_GLOBAL_WRITE 只是提示(CP/FENCE 可据此省一次冲刷), 本实现不用
    localparam int FLAG_REQUIRES_SINGLE_SM= 3;

    // cmd_flags(CMDS §4.1): bit0 = CMD_SYNC 目前没有使用者 —— CP 执行完一条
    // 命令本来就已经把写缓冲冲干净了, 保留编号以免以后的扩展挪位。
    /* verilator lint_off UNUSEDPARAM */
    localparam int CFLAG_SYNC        = 0;
    /* verilator lint_on UNUSEDPARAM */
    localparam int CFLAG_IRQ_ON_DONE = 1;

    // IRQ 位
    localparam int IRQ_BIT_CP_ERROR    = 0;
    localparam int IRQ_BIT_CMD_DONE    = 1;
    localparam int IRQ_BIT_LAUNCH_DONE = 2;
    localparam int IRQ_BIT_2D_DONE     = 3;
    localparam int IRQ_BIT_GEMM_DONE   = 4;

endpackage

`endif // CP_PKG_SV
