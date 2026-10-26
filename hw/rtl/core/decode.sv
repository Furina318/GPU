`include "defines.vh"

module decode (
    input  logic                clk        ,   
    input  logic                rst        ,
    input  core_pkg::handshake  dec_in     ,
    output core_pkg::handshake  dec_out    ,
    input  logic [31:0]         in_pc      ,
    input  logic [31:0]         in_inst    ,
    output core_pkg::dec_info_t dec_info
);
    import core_pkg::*;

    wire [ 6:0] opcode    = in_inst[ 6: 0];
    wire [ 2:0] funct3    = in_inst[14:12];
    wire [ 3:0] funct4    = {in_inst[31:29], in_inst[25]};  
    wire [ 1:0] funct2    = in_inst[26:25];
    wire [ 4:0] rd        = in_inst[11: 7];
    wire [ 4:0] rs1       = in_inst[19:15];
    wire [ 4:0] rs2       = in_inst[24:20];
    wire [ 4:0] rs3       = in_inst[31:27];
    wire [ 2:0] setp_cond = in_inst[31:29];

  
    wire [ 8:0] imm9_i = {in_inst[31:29], in_inst[25:20]};              // I / CTRL
    wire [ 8:0] imm9_s = {in_inst[31:29], in_inst[25], in_inst[11:7]};  // S
    wire [21:0] imm22  = {in_inst[31:29], in_inst[25: 7]};              // B
    wire [19:0] imm20  = {in_inst[31:26], in_inst[25:12]};              // U

    wire [31:0] imm_i = {{23{imm9_i[8]}}, imm9_i};
    wire [31:0] imm_s = {{23{imm9_s[8]}}, imm9_s};
    wire [31:0] imm_b = {{10{imm22 [21]}}, imm22 };
    wire [31:0] imm_u = {{12{imm20 [19]}}, imm20 };

    dec_fmt_t fmt_of_opcode;
    always_comb begin
        case (opcode)
            7'b0000011: fmt_of_opcode = FMT_I;       // ld.g / ld.s
            7'b0100011: fmt_of_opcode = FMT_S;       // st.g / st.s
            7'b0010011: fmt_of_opcode = FMT_I;       // 立即数 ALU
            7'b0110111: fmt_of_opcode = FMT_U;       // lui
            7'b0110011: fmt_of_opcode = FMT_R;       // 整数 ALU / FP32
            7'b1010011: fmt_of_opcode = FMT_R4;      // fma 家族 / imad
            7'b0001011: fmt_of_opcode = FMT_SETP;    // setp
            7'b0101011: fmt_of_opcode = FMT_CTRL;    // bar.sync / exit / tmov / wmov
            7'b1010111: fmt_of_opcode = FMT_U;       // ipdom
            7'b1100011: fmt_of_opcode = FMT_B;       // br.simt
            default:    fmt_of_opcode = FMT_ILLEGAL; // 未定义 opcode
        endcase
    end

    typedef struct packed {
        alu_op_t    alu_op;     logic alu_en;
        mdu_op_t    mdu_op;     logic mdu_en;
        fpu_op_t    fpu_op;     logic fpu_en;
        logic       imad_en;
        logic       setp_en;    logic [2:0] setp_cond;
        logic [1:0] setp_family; logic [2:0] setp_pdst;
        logic       is_load, is_store, is_shared;
        logic       is_branch;  logic [31:0] br_target;
        logic       is_ipdom, is_barrier, is_exit, is_tmov, is_wmov;
        logic [4:0] shamt;      logic [2:0] frm;
        logic       illegal;    ill_code_t ill_code;
    } dec_ctrl_t;

    dec_ctrl_t c;
    always_comb begin
        c = '0;

        // 注意: 这里刻意用 casez 的"首个匹配优先"语义 —— 后面的兜底分支
        // (如 "st: funct3>1") 在模式空间上覆盖了前面的合法分支, 写成 unique casez
        // 会在运行期触发 "multiple matches" 断言。合法性仍由图保障: 每类 opcode 的
        // 合法 funct 都在兜底分支之前逐一列出。
        casez (in_inst)
            // ALU
            32'b000_???_0_?????_?????_000_?????_0110011: begin c.alu_op = ALU_ADD ; c.alu_en = 1'b1; end // add
            32'b000_???_1_?????_?????_000_?????_0110011: begin c.alu_op = ALU_SUB ; c.alu_en = 1'b1; end // sub
            32'b001_???_0_?????_?????_000_?????_0110011: begin c.alu_op = ALU_AND ; c.alu_en = 1'b1; end // and
            32'b001_???_1_?????_?????_000_?????_0110011: begin c.alu_op = ALU_OR  ; c.alu_en = 1'b1; end // or
            32'b010_???_0_?????_?????_000_?????_0110011: begin c.alu_op = ALU_XOR ; c.alu_en = 1'b1; end // xor
            32'b010_???_1_?????_?????_000_?????_0110011: begin c.alu_op = ALU_SLL ; c.alu_en = 1'b1; end // sll
            32'b011_???_0_?????_?????_000_?????_0110011: begin c.alu_op = ALU_SRL ; c.alu_en = 1'b1; end // srl
            32'b011_???_1_?????_?????_000_?????_0110011: begin c.alu_op = ALU_SRA ; c.alu_en = 1'b1; end // sra
            32'b100_???_0_?????_?????_000_?????_0110011: begin c.alu_op = ALU_SLT ; c.alu_en = 1'b1; end // slt
            32'b100_???_1_?????_?????_000_?????_0110011: begin c.alu_op = ALU_SLTU; c.alu_en = 1'b1; end // sltu
            32'b101_???_0_?????_?????_000_?????_0110011: begin c.mdu_op = MDU_MUL ; c.mdu_en = 1'b1; end // mul
            32'b101_???_1_?????_?????_000_?????_0110011: begin c.mdu_op = MDU_DIV ; c.mdu_en = 1'b1; end // div
            32'b110_???_0_?????_?????_000_?????_0110011: begin c.mdu_op = MDU_DIVU; c.mdu_en = 1'b1; end // divu
            32'b110_???_1_?????_?????_000_?????_0110011: begin c.mdu_op = MDU_REM ; c.mdu_en = 1'b1; end // rem
            32'b111_???_0_?????_?????_000_?????_0110011: begin c.mdu_op = MDU_REMU; c.mdu_en = 1'b1; end // remu

            // FP32
            32'b000_???_0_?????_?????_001_?????_0110011: begin c.fpu_op = FPU_FADD; c.fpu_en = 1'b1; end // fadd
            32'b000_???_1_?????_?????_001_?????_0110011: begin c.fpu_op = FPU_FSUB; c.fpu_en = 1'b1; end // fsub
            32'b001_???_0_?????_?????_001_?????_0110011: begin c.fpu_op = FPU_FMUL; c.fpu_en = 1'b1; end // fmul
            32'b001_???_1_?????_?????_001_?????_0110011: begin c.fpu_op = FPU_FMIN; c.fpu_en = 1'b1; end // fmin
            32'b010_???_0_?????_?????_001_?????_0110011: begin c.fpu_op = FPU_FMAX; c.fpu_en = 1'b1; end // fmax
            32'b010_???_1_?????_?????_001_?????_0110011: begin  // f2i(转换类无第二个源 ⇒ rs2 必须 r0)
                c.fpu_op = FPU_FCVT_W; c.fpu_en = 1'b1; c.frm = 3'b001;      // RTZ
                if (|rs2) begin c.illegal = 1'b1; c.ill_code = ILL_RS2_NZ; end
            end
            32'b011_???_0_?????_?????_001_?????_0110011: begin  // i2f
                c.fpu_op = FPU_FCVT_S; c.fpu_en = 1'b1;
                if (|rs2) begin c.illegal = 1'b1; c.ill_code = ILL_RS2_NZ; end
            end

            // 融合乘加
            32'b?????_00_?????_?????_000_?????_1010011: begin c.fpu_op = FPU_FMADD ; c.fpu_en = 1'b1; end // fma.f32
            32'b?????_01_?????_?????_000_?????_1010011: begin c.fpu_op = FPU_FMSUB ; c.fpu_en = 1'b1; end // fmsub.f32
            32'b?????_10_?????_?????_000_?????_1010011: begin c.fpu_op = FPU_FNMADD; c.fpu_en = 1'b1; end // fnmsub.f32
            32'b?????_11_?????_?????_000_?????_1010011: begin c.fpu_op = FPU_FNMSUB; c.fpu_en = 1'b1; end // fnmadd.f32
            // imad = rs1*rs2 + rs3:乘积复用 MDU 的乘法器,rs3 由 exu 在 E_MDU 里补加
            32'b?????_00_?????_?????_001_?????_1010011: begin c.imad_en = 1'b1; c.mdu_op = MDU_MUL; end

            // ALU
            32'b???_???_??????_?????_000_?????_0010011: begin c.alu_op = ALU_ADD ; c.alu_en = 1'b1; end // addi
            32'b???_???_??????_?????_010_?????_0010011: begin c.alu_op = ALU_SLT ; c.alu_en = 1'b1; end // slti
            32'b???_???_??????_?????_011_?????_0010011: begin c.alu_op = ALU_SLTU; c.alu_en = 1'b1; end // sltiu
            32'b???_???_??????_?????_100_?????_0010011: begin c.alu_op = ALU_XOR ; c.alu_en = 1'b1; end // xori
            32'b???_???_??????_?????_110_?????_0010011: begin c.alu_op = ALU_OR  ; c.alu_en = 1'b1; end // ori
            32'b???_???_??????_?????_111_?????_0010011: begin c.alu_op = ALU_AND ; c.alu_en = 1'b1; end // andi
            32'b???_???_??????_?????_001_?????_0010011: begin                    // slli(移位量 = imm[4:0])
                c.alu_op = ALU_SLL; c.alu_en = 1'b1; c.shamt = in_inst[24:20];
                if (|{in_inst[31:29], in_inst[25]}) begin                        // imm[8:5] 必须为 0
                    c.illegal = 1'b1; c.ill_code = ILL_SLLI_IMM;
                end
            end
            32'b???_???_??????_?????_101_?????_0010011: begin                    // srli(imm[5]=0)/srai(imm[5]=1)
                c.shamt = in_inst[24:20];
                if (|in_inst[31:29]) begin                                       // imm[8:6] 必须为 0
                    c.illegal = 1'b1; c.ill_code = ILL_SRI_IMM;
                end else if (in_inst[25]) begin
                    c.alu_op = ALU_SRA; c.alu_en = 1'b1;
                end else begin
                    c.alu_op = ALU_SRL; c.alu_en = 1'b1;
                end
            end

            // U 型(无谓词)
            32'b??????_??????????????_?????_0110111: begin                       // lui(rd = imm20<<12)
                c.alu_op = ALU_LUI; c.alu_en = 1'b1;                             // alu.sv 的 ALU_LUI 直接输出 src2(= imm)
            end
            32'b??????_??????????????_?????_1010111: begin                       // ipdom(recov = PC + imm20<<2)
                c.is_ipdom = 1'b1;
                if (|rd) begin c.illegal = 1'b1; c.ill_code = ILL_IPDOM_RD; end
            end

            // 访存
            32'b???_???_??????_?????_000_?????_0000011:  begin c.alu_op = ALU_ADD; c.alu_en = 1'b1; c.is_load  = 1'b1; c.is_shared = 1'b0; end // ld.g
            32'b???_???_??????_?????_001_?????_0000011:  begin c.alu_op = ALU_ADD; c.alu_en = 1'b1; c.is_load  = 1'b1; c.is_shared = 1'b1; end // ld.s
            32'b???_???_?_?????_?????_000_?????_0100011: begin c.alu_op = ALU_ADD; c.alu_en = 1'b1; c.is_store = 1'b1; c.is_shared = 1'b0; end // st.g
            32'b???_???_?_?????_?????_001_?????_0100011: begin c.alu_op = ALU_ADD; c.alu_en = 1'b1; c.is_store = 1'b1; c.is_shared = 1'b1; end // st.s

            // 比较→谓词
            32'b???_???_0_?????_?????_???_?????_0001011: begin
                if (setp_cond > 3'd5 || funct3 > 3'd2) begin                     // 保留 cond / family
                    c.illegal = 1'b1; c.ill_code = ILL_FUNCT;
                end else begin
                    c.setp_en     = 1'b1;
                    c.setp_cond   = setp_cond;
                    c.setp_family = funct3[1:0];
                    if (rd >= 5'd1 && rd <= 5'd3)
                        c.setp_pdst = 3'b001 << (rd - 5'd1);                     // p1/p2/p3 one-hot
                    else begin
                        c.illegal = 1'b1; c.ill_code = ILL_SETP_PDEST;
                    end
                end
            end

            // 控制与特殊
            32'b???_???_??????_?????_001_?????_0101011: begin                   // bar.sync
                c.is_barrier = 1'b1;
                if (in_inst[28] | (|in_inst[27:26]) | (|rd) | (|rs1) | (|{in_inst[31:29], in_inst[25:20]})) begin
                    c.illegal = 1'b1; c.ill_code = ILL_CTRL_FIELD;
                end
            end
            32'b???_???_??????_?????_010_?????_0101011: begin                   // exit
                c.is_exit = 1'b1;
                if (in_inst[28] | (|in_inst[27:26]) | (|rd) | (|rs1) | (|{in_inst[31:29], in_inst[25:20]})) begin
                    c.illegal = 1'b1; c.ill_code = ILL_CTRL_FIELD;
                end
            end
            32'b???_???_??????_?????_011_?????_0101011: begin                   // tmov rd, tid
                c.is_tmov = 1'b1;
                if (|rs1) begin c.illegal = 1'b1; c.ill_code = ILL_CTRL_FIELD; end
            end
            32'b???_???_??????_?????_100_?????_0101011: begin                   // wmov rd, wid
                c.is_wmov = 1'b1;
                if (|rs1) begin c.illegal = 1'b1; c.ill_code = ILL_CTRL_FIELD; end
            end

            // 分支
            32'b???_???_???????????????????_1100011: begin
                c.is_branch = 1'b1;
                c.br_target = in_pc + (imm_b << 2);                              // PC + sext(imm22)<<2
            end

            // 兜底: opcode 认出, 但保留的 funct 组合 -> ILL_FUNCT 
            32'b???_???_?_?????_?????_???_?????_0110011: begin c.illegal = 1'b1; c.ill_code = ILL_FUNCT; end // R: funct3>=2 或保留 funct4
            32'b?????_??_?????_?????_???_?????_1010011:  begin c.illegal = 1'b1; c.ill_code = ILL_FUNCT; end // R4: funct3>1 或 imad 的 funct2!=0
            32'b???_???_??????_?????_???_?????_0000011:  begin c.illegal = 1'b1; c.ill_code = ILL_FUNCT; end // ld: funct3>1
            32'b???_???_?_?????_?????_???_?????_0100011: begin c.illegal = 1'b1; c.ill_code = ILL_FUNCT; end // st: funct3>1
            32'b???_???_1_?????_?????_???_?????_0001011: begin c.illegal = 1'b1; c.ill_code = ILL_FUNCT; end // setp: bit25 必须为 0
            32'b???_???_??????_?????_???_?????_0101011:  begin c.illegal = 1'b1; c.ill_code = ILL_FUNCT; end // 控制: 保留 funct3

            // 未知 opcode
            default: begin c.illegal = 1'b1; c.ill_code = ILL_OPCODE; end
        endcase
    end

    dec_info_t dec_next;
    always_comb begin
        dec_next        = '0;                        // 未用字段一律 0
        dec_next.pc     = in_pc;
        dec_next.raw    = in_inst;
        dec_next.fmt    = fmt_of_opcode;
        dec_next.rd     = rd;
        dec_next.rs1    = rs1;
        dec_next.rs2    = rs2;
        dec_next.rs3    = rs3;
        dec_next.funct3 = funct3;
        dec_next.funct4 = funct4;
        dec_next.funct2 = funct2;

        dec_next.alu_op          = c.alu_op;
        dec_next.alu_en          = c.alu_en;
        dec_next.mdu_op          = c.mdu_op;
        dec_next.mdu_en          = c.mdu_en;
        dec_next.fpu_op          = c.fpu_op;
        dec_next.fpu_en          = c.fpu_en;
        dec_next.imad_en         = c.imad_en;
        dec_next.setp_en         = c.setp_en;
        dec_next.setp_cond       = c.setp_cond;
        dec_next.setp_family     = c.setp_family;
        dec_next.setp_pdst       = c.setp_pdst;
        dec_next.is_load         = c.is_load;
        dec_next.is_store        = c.is_store;
        dec_next.is_shared       = c.is_shared;
        dec_next.is_branch       = c.is_branch;
        dec_next.br_target       = c.br_target;
        dec_next.is_ipdom        = c.is_ipdom;
        dec_next.is_barrier      = c.is_barrier;
        dec_next.is_exit         = c.is_exit;
        dec_next.is_tmov         = c.is_tmov;
        dec_next.is_wmov         = c.is_wmov;
        dec_next.shamt           = c.shamt;
        dec_next.frm             = c.frm;

        case (dec_next.fmt)
            FMT_I, FMT_CTRL: begin dec_next.imm_raw = imm_i; dec_next.imm = imm_i; end
            FMT_S:           begin dec_next.imm_raw = imm_s; dec_next.imm = imm_s; end
            FMT_B:           begin dec_next.imm_raw = imm_b; dec_next.imm = imm_b << 2; end
            FMT_U:           begin dec_next.imm_raw = imm_u;
                                   dec_next.imm = dec_next.is_ipdom ? (imm_u << 2) : (imm_u << 12); end
            default:         begin dec_next.imm_raw = '0;    dec_next.imm = '0; end  // R/R4/SETP 无立即数
        endcase

        // 谓词字段: 位置恒为 [28:26];U 型与 R4 型没有该字段(那几位并入了立即数/funct2)
        dec_next.pred_used = ~(dec_next.fmt inside {FMT_U, FMT_R4});
        dec_next.pred_inv  = dec_next.pred_used & in_inst[28];
        dec_next.pred_psel = dec_next.pred_used ? in_inst[27:26] : 2'b00;

        // ALU 第二个源: I/U 型取立即数, ld/st 的地址计算也取立即数, 其余取 rs2
        dec_next.alu_src2_is_imm = (dec_next.fmt inside {FMT_I, FMT_U}) | dec_next.is_load | dec_next.is_store;

        // 写回(与参考模型同一规则: S/B/SETP 格式与 ipdom/bar/exit 不写 GPR)
        dec_next.wr_gpr  = ~(dec_next.fmt inside {FMT_S, FMT_B, FMT_SETP})
                             & ~(dec_next.is_ipdom | dec_next.is_barrier | dec_next.is_exit);
        dec_next.wr_pred = (dec_next.fmt == FMT_SETP);

        // 非法指令: 报告原因, 且**不产生任何副作用**(停机信号传到各级之前不得写寄存器/发访存)
        dec_next.illegal  = c.illegal;
        dec_next.ill_code = c.ill_code;
        if (c.illegal) begin
            dec_next.alu_en      = 1'b0;
            dec_next.mdu_en      = 1'b0;
            dec_next.fpu_en      = 1'b0;
            dec_next.imad_en     = 1'b0;
            dec_next.setp_en     = 1'b0;
            dec_next.wr_gpr  = 1'b0;
            dec_next.wr_pred = 1'b0;
        end
    end

    dec_info_t dec_q;
    assign dec_info = dec_q;
    assign dec_out.ready = dec_in.ready;

    always_ff @(posedge clk) begin
        if (rst) begin
            dec_out.valid <= 1'b0;
            dec_q     <= '0;
        end else if (dec_out.ready) begin
            dec_out.valid <= dec_in.valid;
            if (dec_in.valid)
                dec_q <= dec_next;
        end
    end

endmodule
