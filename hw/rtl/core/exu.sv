`include "defines.vh"

// ============================================================================
//  exu —— 每 warp 一个的执行单元
//
//  一条指令在这里走完:
//    · ALU / setp / tmov / wmov: 8 lane 并行, 1 拍出结果;
//    · MDU(乘除): 逐 lane 串行复用同一个 `mdu`(8 lane ⇒ 8 次);
//    · 访存: 交给同 warp 的 `lsu`, 等它回 `mem_done`;
//    · 分支: 只给出"哪些 lane 谓词为真", 压栈/收敛由 `fetch` 的 SIMT 栈处理。
//
//  完成时给 `wbu` 送一次写回(单拍脉冲 `wb_valid`)。
// ============================================================================

module exu import core_pkg::*; #(
    parameter int LANES = `WARP_SIZE
) (
    input  wire                     clk         ,
    input  wire                     rst         ,
    /* verilator lint_off UNUSEDSIGNAL */
    input  wire                     warp_active ,   // 本 warp 还没 exit(core 侧用停机控制)
    /* verilator lint_on UNUSEDSIGNAL */
    input  wire [2:0]               warp_id     ,   // wmov 用(= wid), 支持到每 SM 8 个 warp

    // ---- 译码结果与操作数(来自 regfile) ----
    input  wire core_pkg::dec_info_t dec        ,
    input  wire [`WARP_SIZE-1:0]    eff_mask    ,
    input  wire [LANES-1:0][31:0]   rs1_data    ,
    input  wire [LANES-1:0][31:0]   rs2_data    ,
    input  wire [LANES-1:0][31:0]   rs3_data    ,   // fma 的第三个源(inst[31:27])
    input  wire [LANES-1:0]         pred_data   ,   // 该指令所选谓词, 每 lane 1 bit
    input  wire                     ex_en       ,   // 本拍这条指令可以执行
    output logic                    ex_ready    ,   // 可以收(`ex_en` 只在 ready 时有效)

    // ---- 写回(wbu) ----
    output logic                    wb_valid    ,
    output logic                    wb_gpr_we   ,
    output logic [`CLOG_GPR-1:0]    wb_gpr_addr ,
    output logic [LANES-1:0][31:0]  wb_gpr_data ,
    output logic                    wb_pred_we  ,
    output logic [`CLOG_PRED-1:0]   wb_pred_addr,
    output logic [LANES-1:0]        wb_pred_data,

    // ---- 分支(给 fetch 的 SIMT 栈) ----
    output logic                    br_valid    ,
    output logic [31:0]             br_target   ,
    output logic [LANES-1:0]        br_taken    ,

    // ---- 访存(同 warp 的 lsu) ----
    output logic                    mem_en      ,
    output logic                    mem_we      ,
    output logic                    mem_shared  ,
    output logic [31:0]             mem_pc      ,
    output logic [31:0]             mem_inst    ,
    output logic [4:0]              mem_rd      ,
    output logic [LANES-1:0][31:0]  mem_lane_addr,
    output logic [LANES-1:0][31:0]  mem_lane_wdata,
    input  wire                     mem_ready   ,
    input  wire                     mem_done    ,
    input  wire                     mem_done_rd ,
    input  wire [4:0]               mem_done_addr,
    input  wire [LANES-1:0][31:0]   mem_done_rdata,
    /* verilator lint_off UNUSEDSIGNAL */
    input  wire [LANES-1:0]         mem_done_we ,   // 每 lane 是否有写回, 由 mem_done_rd 统一表达
    /* verilator lint_on UNUSEDSIGNAL */
    input  wire                     mem_err     ,
    input  wire [3:0]               mem_err_code,

    // ---- FPU(每 SM 一个, 由 core 仲裁; 本 warp 同一时刻最多一条 op 在飞) ----
    output logic                    fpu_req     ,
    output logic [6:0]              fpu_op      ,
    output logic [2:0]              fpu_rm      ,
    output logic [31:0]             fpu_src1    ,
    output logic [31:0]             fpu_src2    ,
    output logic [31:0]             fpu_src3    ,
    input  wire                     fpu_ready   ,
    input  wire                     fpu_rsp_valid,
    input  wire [31:0]              fpu_result  ,

    // ---- 错误(本 warp 的第一处, core 汇总) ----
    output logic                    exu_err     ,
    output logic [3:0]              exu_err_code,
    output logic                    exu_busy
);

    localparam int LW = $clog2(LANES);

    // ======================================================================
    //  ALU(8 lane 并行)
    // ======================================================================
    logic [LANES-1:0][31:0] alu_src2;
    logic [LANES-1:0][31:0] alu_res;

    genvar gi;
    generate
        for (gi = 0; gi < LANES; gi = gi + 1) begin : g_alu
            assign alu_src2[gi] = dec.alu_src2_is_imm ? dec.imm : rs2_data[gi];
            alu u_alu (
                .alu_op  (dec.alu_op  ),
                .alu_src1(rs1_data[gi]),
                .alu_src2(alu_src2[gi]),
                .alu_result(alu_res[gi])
            );
        end
    endgenerate

    // ======================================================================
    //  setp: 每 lane 一个比较位
    // ======================================================================
    logic [LANES-1:0] setp_bit;

    function automatic logic cmp_setp(input logic [2:0] cond, input logic [1:0] fam,
                                      input logic [31:0] a, input logic [31:0] b);
        logic eq, lt, gt;
        logic [31:0] fa, fb;
        begin
            fa = a;
            fb = b;
            // 比较只在整数域上做(setp 的 FP 家族暂未接 FPU, 按位比较)
            eq = (fa == fb);
            lt = (fam == 2'd1) ? (fa < fb) : ($signed(fa) < $signed(fb));
            gt = (fam == 2'd1) ? (fa > fb) : ($signed(fa) > $signed(fb));
            unique case (cond)
                3'd0: cmp_setp = eq;
                3'd1: cmp_setp = ~eq;
                3'd2: cmp_setp = lt;
                3'd3: cmp_setp = ~lt;
                3'd4: cmp_setp = ~gt;
                default: cmp_setp = gt;
            endcase
        end
    endfunction

    genvar gj;
    generate
        for (gj = 0; gj < LANES; gj = gj + 1) begin : g_setp
            assign setp_bit[gj] = cmp_setp(dec.setp_cond, dec.setp_family,
                                           rs1_data[gj], rs2_data[gj]);
        end
    endgenerate

    // ======================================================================
    //  分支: 谓词为真的 lane(谓词取反由 dec.pred_inv 给出, 已在 regfile 前处理)
    // ======================================================================
    assign br_taken = pred_data;
    assign br_target = dec.br_target;

    // ======================================================================
    //  FSM
    // ======================================================================
    typedef enum logic [2:0] {
        E_IDLE, E_LSU, E_MDU, E_FPU, E_WB, E_DONE
    } exu_state_t;

    exu_state_t st;

    logic [LW-1:0]  lane_q;          // MDU 串行: 当前 lane
    logic [31:0]    pc_q, inst_q;
    /* verilator lint_off UNUSEDSIGNAL */
    // 只锁存用得到的字段(wr_gpr/rd/fpu_op/frm/imad_en/...), 其余位宽留着不影响
    dec_info_t      dec_q;
    /* verilator lint_on UNUSEDSIGNAL */
    logic [LANES-1:0] mask_q;
    logic [LANES-1:0][31:0] rs1_q, rs2_q;
    logic [LANES-1:0]       wb_acc_done;
    logic [LANES-1:0][31:0] wb_acc;  // 多拍指令累积写回数据

    assign ex_ready = (st == E_IDLE);
    assign exu_busy = (st != E_IDLE);

    logic lsu_started;
    logic mdu_started;               // 已把一条 op 送进 MDU, 正在等结果
    logic fpu_started;               // 已把一条 op 送进 FPU, 正在等结果
    logic [LANES-1:0][31:0] rs3_q;

    // FPU 是标量单元 ⇒ 逐 lane 串行, 和 MDU 一样
    assign fpu_req  = (st == E_FPU) && mask_q[lane_q] && !wb_acc_done[lane_q] && !fpu_started;
    assign fpu_op   = dec_q.fpu_op;
    assign fpu_rm   = dec_q.frm;
    assign fpu_src1 = rs1_q[lane_q];
    assign fpu_src2 = rs2_q[lane_q];
    assign fpu_src3 = rs3_q[lane_q];
    assign mem_en = (st == E_LSU) && !lsu_started;

    // ---------------- MDU 串行 ----------------
    logic        mdu_en, mdu_ack;
    logic        mdu_ready, mdu_done;
    logic [31:0] mdu_src1, mdu_src2, mdu_result;
    /* verilator lint_off UNUSEDSIGNAL */
    // mdu 的完成现场(rd / gpr_we / pc / inst)本层不用: 写回用的是指令自身的
    // dec_q.rd / dec_q.wr_gpr, 这样即使 MDU 的完成队列有偏也不会串到别的 lane。
    logic [4:0]  mdu_rd_done;
    logic        mdu_gpr_we_done;
    logic [31:0] mdu_pc_done, mdu_inst_done;
    /* verilator lint_on UNUSEDSIGNAL */

    mdu u_mdu (
        .clk          (clk),
        .rst          (rst),
        .mdu_en       (mdu_en),
        .mdu_op       (dec_q.mdu_op),
        .mdu_src1     (mdu_src1),
        .mdu_src2     (mdu_src2),
        .mdu_rd       (dec_q.rd),
        .mdu_gpr_we   (dec_q.wr_gpr),
        .mdu_pc       (pc_q),
        .mdu_inst     (inst_q),
        .mdu_ready    (mdu_ready),
        .mdu_done     (mdu_done),
        .mdu_result   (mdu_result),
        .mdu_done_rd  (mdu_rd_done),
        .mdu_done_gpr_we(mdu_gpr_we_done),
        .mdu_done_pc  (mdu_pc_done),
        .mdu_done_inst(mdu_inst_done),
        .mdu_done_ack (mdu_ack)
    );

    assign mdu_src1 = rs1_q[lane_q];
    assign mdu_src2 = rs2_q[lane_q];
    // mdu 是流水单元(ready 高位常有效): 同一 lane 只能发起一次, 用 mdu_started 记住,
    // 否则每个周期都会重复发同一条 op, 拍完的结果按"当前 lane_q"错位回填。
    assign mdu_en   = (st == E_MDU) && mask_q[lane_q] && !wb_acc_done[lane_q] &&
                      !mdu_started;
    // 完成队列必须由消费方确认:常 1 的 ack 会让队头在 EXU 取数之前就被弹掉,
    // rd/写使能这些"随结果一起回来"的字段就会错位(mul 的结果被写到 r0 去)。
    assign mdu_ack  = (st == E_MDU) && mdu_started && mdu_done;

    // 下一个要处理的 lane(只在需要写的 lane 上走)
    function automatic logic [LW-1:0] next_lane(input logic [LW-1:0] cur,
                                                input logic [LANES-1:0] todo);
        logic found;
        int   k;
        begin
            found     = 1'b0;
            next_lane = cur;
            for (k = 1; k <= LANES; k = k + 1) begin
                if (!found && todo[(int'(cur) + k) % LANES]) begin
                    next_lane = LW'((int'(cur) + k) % LANES);
                    found     = 1'b1;
                end
            end
        end
    endfunction

    // ======================================================================
    //  写回数据选择
    // ======================================================================
    logic [LANES-1:0][31:0] wb_now;

    always_comb begin : wb_pick
        integer li;
        wb_now = wb_acc;
        if (st == E_IDLE && ex_en) begin
            for (li = 0; li < LANES; li = li + 1) begin
                if (dec.setp_en)
                    wb_now[li] = dec.imm;
                else if (dec.is_tmov)
                    wb_now[li] = li[31:0];              // tid
                else if (dec.is_wmov)
                    wb_now[li] = {29'h0, warp_id};              // wid(3 位)
                else if (dec.alu_en)
                    wb_now[li] = alu_res[li];
            end
        end else if (st == E_LSU && mem_done) begin
            for (li = 0; li < LANES; li = li + 1)
                wb_now[li] = mem_done_rdata[li];
        end else if (st == E_DONE) begin
            wb_now = wb_acc;
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            st           <= E_IDLE;
            lane_q       <= '0;
            pc_q         <= '0;
            inst_q       <= '0;
            dec_q        <= '0;
            mask_q       <= '0;
            rs1_q        <= '0;
            rs2_q        <= '0;
            wb_acc       <= '0;
            wb_acc_done  <= '0;
            wb_valid     <= 1'b0;
            wb_gpr_we    <= 1'b0;
            wb_gpr_addr  <= '0;
            wb_gpr_data  <= '0;
            wb_pred_we   <= 1'b0;
            wb_pred_addr <= '0;
            wb_pred_data <= '0;
            br_valid     <= 1'b0;
            exu_err      <= 1'b0;
            exu_err_code <= 4'd0;
            lsu_started  <= 1'b0;
            mdu_started  <= 1'b0;
            fpu_started  <= 1'b0;
            rs3_q        <= '0;
        end else begin
            wb_valid <= 1'b0;
            br_valid <= 1'b0;

            unique case (st)
                E_IDLE: begin
                    if (ex_en) begin
                        pc_q        <= dec.pc;
                        inst_q      <= dec.raw;
                        dec_q       <= dec;
                        mask_q      <= eff_mask;
                        rs1_q       <= rs1_data;
                        rs2_q       <= rs2_data;
                        rs3_q       <= rs3_data;
                        wb_acc      <= '0;
                        wb_acc_done <= '0;
                        lane_q      <= '0;

                        if (dec.ill_code != ILL_NONE) begin
                            exu_err      <= 1'b1;
                            exu_err_code <= 4'd1;       // ILLEGAL_INSTR
                            st           <= E_DONE;
                        end else if (dec.is_load || dec.is_store) begin
                            mem_we          <= dec.is_store;
                            mem_shared      <= dec.is_shared;
                            mem_pc          <= dec.pc;
                            mem_inst        <= dec.raw;
                            mem_rd          <= dec.rd;
                            mem_lane_addr   <= lane_addr_calc;
                            mem_lane_wdata  <= rs2_data;
                            st              <= E_LSU;
                        end else if (dec.fpu_en) begin
                            fpu_started <= 1'b0;
                            st          <= E_FPU;
                        end else if (dec.mdu_en || dec.imad_en) begin
                            mdu_started <= 1'b0;
                            st          <= E_MDU;
                        end else begin
                            // 单拍: ALU / setp / tmov / wmov / 分支 / nop
                            if (dec.is_branch) br_valid <= 1'b1;
                            wb_valid     <= 1'b1;
                            wb_gpr_we    <= dec.wr_gpr;
                            wb_gpr_addr  <= dec.rd;
                            wb_gpr_data  <= wb_now;
                            wb_pred_we   <= dec.setp_en;
                            // setp_pdst 是 p1/p2/p3 的 one-hot, 这里翻译成谓词号。
                            // (直接截取低 2 位会把 p3 的 3'b100 变成 p0, 于是 setp 写 p3
                            //  实际写进了只读的 p0, 循环条件永远读到旧值)
                            wb_pred_addr <= dec.setp_pdst[2] ? `CLOG_PRED'(2'd3)
                                          : dec.setp_pdst[1] ? `CLOG_PRED'(2'd2)
                                                             : `CLOG_PRED'(2'd1);
                            wb_pred_data <= setp_bit;
                            st           <= E_DONE;
                        end
                    end
                end

                E_LSU: begin
                    if (!lsu_started && mem_ready) lsu_started <= 1'b1;
                    if (mem_done) begin
                        lsu_started  <= 1'b0;
                        if (mem_err) begin
                            exu_err      <= 1'b1;
                            exu_err_code <= mem_err_code;
                        end
                        wb_valid     <= 1'b1;
                        wb_gpr_we    <= mem_done_rd;
                        wb_gpr_addr  <= mem_done_addr;
                        wb_gpr_data  <= mem_done_rdata;
                        st           <= E_DONE;
                    end
                end

                E_MDU: begin
                    // 逐个 lane 算: mdu 是单 lane 单元(但内部流水, 可背靠背发起)。
                    // 做完最后一个 lane 还要再等一拍, 因为 wb_acc 是非阻塞赋值 ——
                    // 当拍取不到刚算完的那个 lane 的结果。
                    if (!mdu_started) begin
                        if (mdu_en && mdu_ready) mdu_started <= 1'b1;
                    end else if (mdu_done) begin
                        mdu_started <= 1'b0;
                        wb_acc[lane_q]      <= dec_q.imad_en ? (mdu_result + rs3_q[lane_q])
                                                             : mdu_result;
                        wb_acc_done[lane_q] <= 1'b1;
                        if (|(mask_q & ~wb_acc_done & ~({{(LANES-1){1'b0}}, 1'b1} << lane_q))) begin
                            lane_q <= next_lane(lane_q, mask_q & ~wb_acc_done);
                        end else begin
                            st <= E_WB;
                        end
                    end
                end

                E_FPU: begin
                    // 逐 lane 串行; 一个 lane 的 op 送进 FPU 后要等它回来
                    if (!fpu_started) begin
                        if (fpu_req && fpu_ready) fpu_started <= 1'b1;
                    end else if (fpu_rsp_valid) begin
                        fpu_started         <= 1'b0;
                        wb_acc[lane_q]      <= fpu_result;
                        wb_acc_done[lane_q] <= 1'b1;
                        if (|(mask_q & ~wb_acc_done & ~({{(LANES-1){1'b0}}, 1'b1} << lane_q))) begin
                            lane_q <= next_lane(lane_q, mask_q & ~wb_acc_done);
                        end else begin
                            st <= E_WB;
                        end
                    end
                end

                // MDU 与 FPU 扫完所有 lane 后共用同一个写回拍:
                // 目标寄存器与写使能都取自指令自身(dec_q), 不依赖单元的完成队列。
                E_WB: begin
                    wb_valid    <= 1'b1;
                    wb_gpr_we   <= dec_q.wr_gpr;
                    wb_gpr_addr <= dec_q.rd;
                    wb_gpr_data <= wb_acc;
                    st          <= E_DONE;
                end

                E_DONE: st <= E_IDLE;

                default: st <= E_IDLE;
            endcase
        end
    end

    // 访存地址 = rs1 + sext(imm9)
    logic [LANES-1:0][31:0] lane_addr_calc;
    genvar gk;
    generate
        for (gk = 0; gk < LANES; gk = gk + 1) begin : g_addr
            assign lane_addr_calc[gk] = rs1_data[gk] + dec.imm;
        end
    endgenerate

endmodule
