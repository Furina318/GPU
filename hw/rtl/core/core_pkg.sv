`ifndef CORE_PKG_VH
`define CORE_PKG_VH

`include "defines.vh"

package core_pkg;
    typedef enum logic [7:0] {
        MDU_MUL    = 8'b0000_0001,
        MDU_MULH   = 8'b0000_0010,
        MDU_MULHSU = 8'b0000_0100,
        MDU_MULHU  = 8'b0000_1000,
        MDU_DIV    = 8'b0001_0000,
        MDU_DIVU   = 8'b0010_0000,
        MDU_REM    = 8'b0100_0000,
        MDU_REMU   = 8'b1000_0000
    } mdu_op_t;

    typedef enum logic [10:0] {
        ALU_ADD  = 11'h001,
        ALU_SUB  = 11'h002,
        ALU_SLT  = 11'h004,
        ALU_SLTU = 11'h008,
        ALU_AND  = 11'h010,
        ALU_OR   = 11'h020,
        ALU_XOR  = 11'h040,
        ALU_SLL  = 11'h080,
        ALU_SRL  = 11'h100,
        ALU_SRA  = 11'h200,
        ALU_LUI  = 11'h400
    } alu_op_t;

    // GPU ISA 的舍入模式固定(RNE, 仅 f2i 用 RTZ)
    typedef enum logic [6:0] {
        FPU_FADD   = 7'b001_0000,
        FPU_FSUB   = 7'b001_0001,
        FPU_FMUL   = 7'b001_0010,
        FPU_FMADD  = 7'b001_0011,
        FPU_FMSUB  = 7'b001_0100,
        FPU_FNMADD = 7'b001_0101,
        FPU_FNMSUB = 7'b001_0110,
        FPU_FMIN   = 7'b010_0001,
        FPU_FMAX   = 7'b010_0010,
        FPU_FCVT_W = 7'b011_0000,   // f2i
        FPU_FCVT_S = 7'b011_0010    // i2f
    } fpu_op_t;

    // 指令格式(用于 trace/调试, 不参与数据通路选择)
    typedef enum logic [3:0] {
        FMT_R, FMT_I, FMT_S, FMT_B, FMT_U, FMT_R4, FMT_SETP, FMT_CTRL, FMT_ILLEGAL
    } dec_fmt_t;

    // ILLEGAL_INSTR
    typedef enum logic [3:0] {
        ILL_NONE       = 4'h0,
        ILL_OPCODE     = 4'h1,     
        ILL_FUNCT      = 4'h2,     
        ILL_RS2_NZ     = 4'h3,      // f2i/i2f 的 rs2 必须为 r0
        ILL_SLLI_IMM   = 4'h4,      // slli 的 imm[8:5] 必须为 0
        ILL_SRI_IMM    = 4'h5,      // srli/srai 的 imm[8:6] 必须为 0
        ILL_CTRL_FIELD = 4'h6,      // bar/exit 的谓词或 rd/rs1/imm 非 0; tmov/wmov 的 rs1 非 0
        ILL_IPDOM_RD   = 4'h7,      // ipdom 的 rd 必须为 0
        ILL_SETP_PDEST = 4'h8       // setp 的目标只能是 p1/p2/p3
    } ill_code_t;

    typedef struct packed {
        dec_fmt_t       fmt;
        logic [31:0]    pc;             // 本指令 PC(ipdom/br 基址、trace)
        logic [31:0]    raw;            // 原始 32 位指令
        // 谓词(U/R4 型无谓词字段, pred_used=0)
        logic           pred_used;
        logic           pred_inv;
        logic [ 1:0]    pred_psel;
        logic [ 4:0]    rd;
        logic [ 4:0]    rs1;
        logic [ 4:0]    rs2;
        logic [ 4:0]    rs3;
        logic [31:0]    imm_raw;        // 按格式符号扩展、未移位
        logic [31:0]    imm;            // 已处理
        logic [ 4:0]    shamt;          // slli/srli/srai 的移位量
        logic [ 2:0]    funct3;
        logic [ 3:0]    funct4;
        logic [ 1:0]    funct2;
        // ALU
        logic           alu_en;
        alu_op_t        alu_op;         
        logic           alu_src2_is_imm;
        // MDU
        logic           mdu_en;
        mdu_op_t        mdu_op;         
        logic           imad_en;        
        // FPU
        logic           fpu_en;
        fpu_op_t        fpu_op;
        logic [ 2:0]    frm;            // 舍入模式: RNE, 仅 f2i 用 RTZ
        // setp
        logic           setp_en;
        logic [ 2:0]    setp_cond;      // 000=eq .. 101=gt
        logic [ 1:0]    setp_family;    // 00=有符号 01=无符号 10=FP
        logic [ 2:0]    setp_pdst;      // one-hot: p1/p2/p3 写使能
        // LSU
        logic           is_load;
        logic           is_store;
        logic           is_shared;     
        // 控制
        logic           is_branch;      
        logic [31:0]    br_target;      
        logic           is_ipdom;
        logic           is_barrier;
        logic           is_exit;
        logic           is_tmov;
        logic           is_wmov;
        // 写回
        logic           wr_gpr;     
        logic           wr_pred;    
        // 合法性
        logic           illegal;
        ill_code_t      ill_code;
    } dec_info_t;

endpackage

`endif // CORE_PKG_VH