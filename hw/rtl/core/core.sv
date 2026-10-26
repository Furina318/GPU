`include "defines.vh"
import core_pkg::*;

// ============================================================================
//  core —— 一个 SM(Streaming Multiprocessor)
//
//  每 SM:
//    · 4 个 warp, 每个 warp 私有 fetch / decode / exu / lsu 与 8 份 regfile;
//    · 共用 16KB 指令 SRAM、16KB 共享内存、一个 lsu_scheduler、一个取指口仲裁;
//    · 发射: 轮转, 每拍至多一个 warp 开始取指;
//    · bar.sync: SM 内屏障, 到齐后把 WCB 冲刷干净再一起放行;
//    · exit: warp 停用; 所有 warp 退出 ⇒ done。
// ============================================================================

module core #(
    parameter int WARPS = `WARPS_PER_SM,
    parameter int LANES = `WARP_SIZE
) (
    input  wire                    clk            ,
    input  wire                    rst            ,

    // ---- CP: LAUNCH(单拍脉冲) ----
    input  wire                    launch         ,
    input  wire [31:0]             launch_pc      ,
    input  wire [3:0]              launch_warps   ,   // 1..4
    /* verilator lint_off UNUSEDSIGNAL */
    input  wire [3:0]              launch_flags   ,   // 只影响 CP 的准入检查, SM 侧不解释
    /* verilator lint_on UNUSEDSIGNAL */

    // ---- CP: 指令预加载 ----
    input  wire                    sram_load_en   ,
    input  wire [12:0]             sram_load_addr ,
    input  wire [31:0]             sram_load_data ,
    input  wire                    sram_load_last ,
    output wire                    sram_load_done ,
    output wire                    sram_load_ovf  ,

    // ---- CP: 完成 / 错误 ----
    output logic                   sm_done        ,
    output logic [3:0]             sm_error       ,   // CMDS §8.2, 0 = 无错误
    output logic [31:0]            sm_fault_pc    ,

    // ---- 全局内存(经 lsu_scheduler → mem_subsystem) ----
    output logic                   gmem_rd_valid  ,
    input  wire                    gmem_rd_ready  ,
    output logic [26:0]            gmem_rd_line   ,
    output logic [LANES-1:0]       gmem_rd_wen    ,
    input  wire                    gmem_rsp_valid ,
    input  wire [26:0]             gmem_rsp_line  ,
    input  wire [LANES-1:0][31:0]  gmem_rsp_data  ,
    input  wire                    gmem_rsp_err   ,
    output logic                   gmem_wr_valid  ,
    input  wire                    gmem_wr_ready  ,
    output logic [26:0]            gmem_wr_line   ,
    output logic [LANES-1:0]       gmem_wr_wen    ,
    output logic [LANES-1:0][31:0] gmem_wr_wdata  ,
    input  wire                    gmem_wr_err    ,
    output logic                   flush_req      ,
    input  wire                    flush_done     ,
    output logic                   mem_busy
);

    // ======================================================================
    //  共享资源: 指令 SRAM / 共享内存 / 调度器
    // ======================================================================
    logic                     imem_fetch_en;
    logic [`CLOG_INSTR-1:0]   imem_fetch_addr;
    logic [31:0]              imem_fetch_data;

    instr_mem u_imem (
        .clk        (clk),
        .rst        (rst),
        .load_en    (sram_load_en),
        .load_addr  (sram_load_addr[`CLOG_INSTR:0]),
        .load_data  (sram_load_data),
        .load_last  (sram_load_last),
        .load_done  (sram_load_done),
        .err_ovf    (sram_load_ovf),
        .fetch_en   (imem_fetch_en),
        .fetch_addr (imem_fetch_addr),
        .fetch_data (imem_fetch_data)
    );

    logic                   smem_req_valid, smem_req_we;
    logic [LANES-1:0]       smem_lane_en;
    logic [LANES-1:0][31:0] smem_lane_addr, smem_lane_wdata;
    logic                   smem_req_ready, smem_rsp_valid;
    logic [LANES-1:0][31:0] smem_rsp_data;
    logic [3:0]             smem_conflict;
    logic                   smem_err_align, smem_err_oob;

    shared_mem #(.SIZE_BYTES(`SHARED_MEM_SIZE), .LANES(LANES), .BANKS(8)) u_smem (
        .clk            (clk),
        .rst            (rst),
        .req_valid      (smem_req_valid),
        .req_we         (smem_req_we),
        .lane_en        (smem_lane_en),
        .lane_addr      (smem_lane_addr),
        .lane_wdata     (smem_lane_wdata),
        .req_ready      (smem_req_ready),
        .rsp_valid      (smem_rsp_valid),
        .lane_rdata     (smem_rsp_data),
        .conflict_extra (smem_conflict),
        .err_align      (smem_err_align),
        .err_oob        (smem_err_oob)
    );

    // ---- 每 warp 的调度器端口 ----
    logic [WARPS-1:0]                   w_smem_req_valid, w_smem_req_we;
    logic [WARPS-1:0][LANES-1:0]        w_smem_lane_en;
    logic [WARPS-1:0][LANES-1:0][31:0]  w_smem_lane_addr, w_smem_lane_wdata;
    logic [WARPS-1:0]                   w_smem_req_ready, w_smem_rsp_valid;
    logic [WARPS-1:0][LANES-1:0][31:0]  w_smem_rsp_data;
    logic [WARPS-1:0][3:0]              w_smem_conflict;
    logic [WARPS-1:0]                   w_gmem_req_valid, w_gmem_req_we;
    logic [WARPS-1:0][26:0]             w_gmem_req_line;
    logic [WARPS-1:0][LANES-1:0]        w_gmem_req_wen;
    logic [WARPS-1:0][LANES-1:0][31:0]  w_gmem_req_wdata;
    logic [WARPS-1:0]                   w_gmem_req_ready, w_gmem_rsp_valid;
    logic [WARPS-1:0][LANES-1:0][31:0]  w_gmem_rsp_data;
    logic [WARPS-1:0]                   w_gmem_rsp_err;
    logic [WARPS-1:0]                   w_flush_req, w_flush_done;

    lsu_scheduler #(.WARPS(WARPS), .LANES(LANES)) u_sched (
        .clk              (clk),
        .rst              (rst),
        .w_smem_req_valid (w_smem_req_valid),
        .w_smem_req_we    (w_smem_req_we),
        .w_smem_lane_en   (w_smem_lane_en),
        .w_smem_lane_addr (w_smem_lane_addr),
        .w_smem_lane_wdata(w_smem_lane_wdata),
        .w_smem_req_ready (w_smem_req_ready),
        .w_smem_rsp_valid (w_smem_rsp_valid),
        .w_smem_rsp_data  (w_smem_rsp_data),
        .w_smem_conflict  (w_smem_conflict),
        .w_gmem_req_valid (w_gmem_req_valid),
        .w_gmem_req_we    (w_gmem_req_we),
        .w_gmem_req_line  (w_gmem_req_line),
        .w_gmem_req_wen   (w_gmem_req_wen),
        .w_gmem_req_wdata (w_gmem_req_wdata),
        .w_gmem_req_ready (w_gmem_req_ready),
        .w_gmem_rsp_valid (w_gmem_rsp_valid),
        .w_gmem_rsp_data  (w_gmem_rsp_data),
        .w_gmem_rsp_err   (w_gmem_rsp_err),
        .w_flush_req      (w_flush_req),
        .w_flush_done     (w_flush_done),
        .smem_req_valid   (smem_req_valid),
        .smem_req_we      (smem_req_we),
        .smem_lane_en     (smem_lane_en),
        .smem_lane_addr   (smem_lane_addr),
        .smem_lane_wdata  (smem_lane_wdata),
        .smem_req_ready   (smem_req_ready),
        .smem_rsp_valid   (smem_rsp_valid),
        .smem_rsp_data    (smem_rsp_data),
        .smem_conflict    (smem_conflict),
        .smem_err_align   (smem_err_align),
        .smem_err_oob     (smem_err_oob),
        .gmem_rd_valid    (gmem_rd_valid),
        .gmem_rd_ready    (gmem_rd_ready),
        .gmem_rd_line     (gmem_rd_line),
        .gmem_rd_wen      (gmem_rd_wen),
        .gmem_rsp_valid   (gmem_rsp_valid),
        .gmem_rsp_line    (gmem_rsp_line),
        .gmem_rsp_data    (gmem_rsp_data),
        .gmem_rsp_err     (gmem_rsp_err),
        .gmem_wr_valid    (gmem_wr_valid),
        .gmem_wr_ready    (gmem_wr_ready),
        .gmem_wr_line     (gmem_wr_line),
        .gmem_wr_wen      (gmem_wr_wen),
        .gmem_wr_wdata    (gmem_wr_wdata),
        .gmem_wr_err      (gmem_wr_err),
        .flush_req        (flush_req),
        .flush_done       (flush_done),
        .busy             (mem_busy)
    );

    // ======================================================================
    //  全局状态
    // ======================================================================
    logic [WARPS-1:0] warp_run;
    logic [WARPS-1:0] warp_exit;
    logic [WARPS-1:0] warp_bar;
    logic [WARPS-1:0] warp_err_pulse;
    logic [3:0]       warp_err_code [WARPS];
    logic [31:0]      warp_err_pc  [WARPS];
    logic             sm_halt;

    // ---- FPU(每 SM 一个, 4 个 warp 共享) ----
    // 一个 warp 同一时刻最多一条 op 在飞, 所以只需要"选一条请求送进 FPU" +
    // "按 fpu_id 把结果送回发起的 warp" 这两件事, 不需要结果队列。
    logic [WARPS-1:0]        f_req;
    logic [WARPS-1:0][6:0]   f_op;
    logic [WARPS-1:0][2:0]   f_rm;
    logic [WARPS-1:0][31:0]  f_src1, f_src2, f_src3;
    logic [WARPS-1:0]        f_ready, f_rsp;
    logic [31:0]             f_result;
    logic                    fpu_any, fpu_rdy, fpu_out;
    logic [1:0]              fpu_sel, fpu_ptr;
    logic [4:0]              fpu_id_o;
    logic [4:0]              fpu_fflags_unused;

    always_comb begin : fpu_arb
        integer k, idx;
        fpu_sel = '0;
        fpu_any = 1'b0;
        idx     = 0;
        for (k = 0; k < WARPS; k = k + 1) begin
            idx = (k + int'(fpu_ptr)) % WARPS;
            if (!fpu_any && f_req[idx]) begin
                fpu_any = 1'b1;
                fpu_sel = idx[1:0];
            end
        end
    end

    always_ff @(posedge clk) begin
        if (rst) fpu_ptr <= '0;
        else if (fpu_any && fpu_rdy) fpu_ptr <= fpu_sel + 2'd1;
    end

    fpu #(.ID_WIDTH(5)) u_fpu (
        .clk       (clk),
        .rst       (rst),
        .valid     (fpu_any),
        .ready     (fpu_rdy),
        .fpu_op    (f_op[fpu_sel]),
        .rm        (f_rm[fpu_sel]),
        .src1      (f_src1[fpu_sel]),
        .src2      (f_src2[fpu_sel]),
        .src3      (f_src3[fpu_sel]),
        .flush     (1'b0),
        .flush_id  (5'd0),
        .fpu_id    ({3'b000, fpu_sel}),
        .fpu_valid (fpu_out),
        .fpu_id_o  (fpu_id_o),
        .fpu_result(f_result),
        .fpu_fflags(fpu_fflags_unused)
    );

    /* verilator lint_off UNUSEDSIGNAL */
    wire [4:0] fpu_fflags_keep = fpu_fflags_unused;   // §: FP 异常全非 trapping, 标志位不参与控制
    /* verilator lint_on UNUSEDSIGNAL */

    // ---- 取指口仲裁(4 抢 1) ----
    logic [1:0] imem_owner, imem_owner_q;
    logic       imem_busy_q;
    logic [WARPS-1:0] f_imem_req_valid;
    logic [WARPS-1:0][31:0] f_imem_req_pc;

    always_comb begin : imem_arb
        integer k, idx;
        imem_fetch_en = 1'b0;
        imem_owner    = '0;
        idx           = 0;
        // 轮转: 从上一个被服务者之后开始找 —— 否则 0 号 warp 会一直以
        // "最小号"赢得仲裁, 其余 warp 永久饥饿(4 warp 用例死锁的根因)。
        if (!imem_busy_q) begin
            for (k = 0; k < WARPS; k = k + 1) begin
                idx = (int'(imem_owner_q) + 1 + k) % WARPS;
                if (f_imem_req_valid[idx] && !imem_fetch_en) begin
                    imem_fetch_en = 1'b1;
                    imem_owner    = idx[1:0];
                end
            end
        end
    end
    assign imem_fetch_addr = f_imem_req_pc[imem_owner][`CLOG_INSTR+1:2];

    // 取指数据下一拍有效: 记住这一拍是谁发的
    always_ff @(posedge clk) begin
        if (rst) begin
            imem_busy_q  <= 1'b0;
            imem_owner_q <= 2'd0;
        end else if (imem_fetch_en) begin
            imem_busy_q  <= 1'b1;
            imem_owner_q <= imem_owner;
        end else begin
            imem_busy_q  <= 1'b0;
        end
    end

    // ======================================================================
    //  每 warp 的数据通路
    // ======================================================================
    genvar wi;
    generate
        for (wi = 0; wi < WARPS; wi = wi + 1) begin : g_warp
            localparam int W = wi;

            /* verilator lint_off UNUSEDSIGNAL */
            logic                   fet_in_valid, fet_in_ready;   // ready 只作观察
            /* verilator lint_on UNUSEDSIGNAL */
            logic                   fet_out_valid, fet_out_ready;
            logic [31:0]            f_pc, f_inst;
            logic [LANES-1:0]       f_inst_mask;
            logic [31:0]            f_target_pc;
            logic                   f_target_valid;
            logic [LANES-1:0]       f_target_taken;
            /* verilator lint_off UNUSEDSIGNAL */
            logic                   f_br_pending, f_br_local, f_br_resolved;
            logic [LANES-1:0]       f_active_mask;
            logic                   f_stack_empty;
            logic [3:0]             f_stack_depth;
            logic                   f_halted;
            /* verilator lint_on UNUSEDSIGNAL */
            logic [31:0]            f_fault_pc;
            logic                   f_err_imem, f_err_no_recov, f_err_simt_ovf, f_err_internal;

            logic [31:0]            latch_pc, latch_inst;
            logic                   dec_in_v;
            core_pkg::handshake     dec_out;
            dec_info_t              dec_info, dec_lat;
            /* verilator lint_off UNUSEDSIGNAL */
            logic                   dec_lat_vld;
            /* verilator lint_on UNUSEDSIGNAL */
            logic [LANES-1:0]       mask_lat;

            logic                   ex_en, ex_ready;
            logic [LANES-1:0][31:0] rs1_d, rs2_d, rs3_d;
            logic [LANES-1:0]       pred_d, pred_raw;
            logic                   wb_valid, wb_gpr_we, wb_pred_we;
            logic [`CLOG_GPR-1:0]   wb_gpr_addr;
            logic [`CLOG_PRED-1:0]  wb_pred_addr;
            logic [LANES-1:0][31:0] wb_gpr_data;
            logic [LANES-1:0]       wb_pred_data;
            // 写回单元: 把"指令谓词 × SIMT 活跃掩码"落到每 lane 的写使能上。
            // 谓词为假的 lane 不能改寄存器 —— 未加谓词的指令用 p0(恒真, ISA §4.2)。
            logic [LANES-1:0]                 wb_eff_mask;
            logic [LANES-1:0]                 wb_gpr_en_l, wb_pred_en_l;
            logic [LANES-1:0][`CLOG_GPR-1:0]  wb_gpr_addr_l;
            logic [LANES-1:0][31:0]           wb_gpr_data_l;
            logic [LANES-1:0][`CLOG_PRED-1:0] wb_pred_addr_l;
            logic [LANES-1:0]                 wb_pred_data_l;
            logic [31:0]                      wb_gpr_data_u [0:LANES-1];
            logic                   x_br_valid;
            logic [31:0]            x_br_target;
            logic [LANES-1:0]       x_br_taken;
            logic                   x_mem_en, x_mem_we, x_mem_shared;
            logic [31:0]            x_mem_pc, x_mem_inst;
            logic [4:0]             x_mem_rd;
            logic [LANES-1:0][31:0] x_mem_addr, x_mem_wdata;
            logic                   x_mem_ready, x_mem_done, x_mem_done_rd, x_mem_err;
            logic [4:0]             x_mem_done_addr;
            logic [LANES-1:0][31:0] x_mem_done_rdata;
            logic [LANES-1:0]       x_mem_done_we;
            logic [3:0]             x_mem_err_code;
            logic                   x_exu_err;
            logic [3:0]             x_exu_err_code;
            /* verilator lint_off UNUSEDSIGNAL */
            logic                   x_exu_busy;
            /* verilator lint_on UNUSEDSIGNAL */
            logic                   br_pend_q;

            logic [2:0] wst;
            localparam logic [2:0]
                W_IDLE = 3'd0, W_DEC = 3'd2,
                W_EXEC = 3'd3, W_BAR = 3'd4, W_EXIT = 3'd5;

            // ---------------- fetch ----------------
            fetch u_fetch (
                .clk            (clk),
                .rst            (rst),
                .halt           (sm_halt),
                .launch         (launch && (W[3:0] < launch_warps)),
                .launch_pc      (launch_pc),
                .launch_warp    ({LANES{1'b1}}),
                .fet_in_valid   (fet_in_valid),
                .fet_in_ready   (fet_in_ready),
                .fet_out_valid  (fet_out_valid),
                .fet_out_ready  (fet_out_ready),
                .pc             (f_pc),
                .inst           (f_inst),
                .inst_mask      (f_inst_mask),
                .target_pc      (f_target_pc),
                .target_valid   (f_target_valid),
                .target_taken   (f_target_taken),
                .imem_req_valid (f_imem_req_valid[W]),
                .imem_req_pc    (f_imem_req_pc[W]),
                .imem_req_ready (imem_fetch_en && (imem_owner == W[1:0])),
                .imem_rsp_valid (imem_busy_q && (imem_owner_q == W[1:0])),
                .imem_rsp_inst  (imem_fetch_data),
                .br_pending     (f_br_pending),
                .br_local       (f_br_local),
                .br_resolved    (f_br_resolved),
                .active_mask    (f_active_mask),
                .stack_empty    (f_stack_empty),
                .stack_depth    (f_stack_depth),
                .fault_pc       (f_fault_pc),
                .err_imem       (f_err_imem),
                .err_no_recov   (f_err_no_recov),
                .err_simt_ovf   (f_err_simt_ovf),
                .err_internal   (f_err_internal),
                .halted         (f_halted)
            );

            always_ff @(posedge clk) begin
                if (rst)
                    br_pend_q <= 1'b0;
                else if (x_br_valid)
                    br_pend_q <= 1'b1;
                else if (f_br_resolved || f_err_no_recov || f_err_imem)
                    br_pend_q <= 1'b0;
            end
            assign f_target_valid = br_pend_q;
            assign f_target_pc    = x_br_target;
            assign f_target_taken = x_br_taken;

            // ---------------- decode ----------------
            core_pkg::handshake dec_in_w;
            assign dec_in_w = '{valid: dec_in_v, ready: 1'b1};
            decode u_decode (
                .clk     (clk),
                .rst     (rst),
                .dec_in  (dec_in_w),
                .dec_out (dec_out),
                .in_pc   (latch_pc),
                .in_inst (latch_inst),
                .dec_info(dec_info)
            );

            // ---------------- 写回单元(每 warp 一份) ----------------
            assign pred_d      = pred_raw ^ {LANES{dec_lat.pred_inv}};
            assign wb_eff_mask = dec_lat.pred_used ? (mask_lat & pred_d) : mask_lat;

            genvar gi;
            for (gi = 0; gi < LANES; gi = gi + 1) begin : g_wb_in
                assign wb_gpr_data_u[gi] = wb_gpr_data[gi];
            end

            wbu u_wbu (
                .clk             (clk),
                .rst             (rst),
                .valid           (wb_valid),
                .effective_mask  (wb_eff_mask),
                .gpr_wr_en_in    (wb_gpr_we),
                .gpr_wr_addr_in  (wb_gpr_addr),
                .gpr_wr_data_in  (wb_gpr_data_u),
                .pred_wr_en_in   (wb_pred_we),
                .pred_wr_addr_in (wb_pred_addr),
                .pred_wr_data_in (wb_pred_data),
                .gpr_wr_en       (wb_gpr_en_l),
                .gpr_wr_addr     (wb_gpr_addr_l),
                .gpr_wr_data     (wb_gpr_data_l),
                .pred_wr_en      (wb_pred_en_l),
                .pred_wr_addr    (wb_pred_addr_l),
                .pred_wr_data    (wb_pred_data_l)
            );

            // ---------------- 8 份 regfile ----------------
            genvar li;
            for (li = 0; li < LANES; li = li + 1) begin : g_lane
                regfile u_rf (
                    .clk          (clk),
                    .rst          (rst),
                    .valid        (1'b1),
                    .gpr_wr_addr  (wb_gpr_addr_l[li]),
                    .gpr_wr_en    (wb_gpr_en_l[li]),
                    .gpr_wr_data  (wb_gpr_data_l[li]),
                    .gpr_rs1_addr (dec_lat.rs1),
                    .gpr_rs2_addr (dec_lat.rs2),
                    .gpr_rs3_addr (dec_lat.rs3),
                    .gpr_rs1_data (rs1_d[li]),
                    .gpr_rs2_data (rs2_d[li]),
                    .gpr_rs3_data (rs3_d[li]),
                    .pred_wr_addr (wb_pred_addr_l[li]),
                    .pred_wr_en   (wb_pred_en_l[li]),
                    .pred_wr_data (wb_pred_data_l[li]),
                    .pred_rd_addr (dec_lat.pred_psel),
                    .pred_rd_data (pred_raw[li])
                );
            end

            // FPU 的发起许可与结果路由(每个 warp 一份)
            assign f_ready[W] = fpu_any && fpu_rdy && (fpu_sel == W[1:0]);
            assign f_rsp[W]   = fpu_out && (fpu_id_o[1:0] == W[1:0]);

            // ---------------- exu ----------------
            exu #(.LANES(LANES)) u_exu (
                .clk            (clk),
                .rst            (rst),
                .warp_active    (warp_run[W]),
                .warp_id        (W[1:0]),
                .dec            (dec_lat),
                .eff_mask       (mask_lat),
                .rs1_data       (rs1_d),
                .rs2_data       (rs2_d),
                .rs3_data       (rs3_d),
                .pred_data      (pred_d),
                .ex_en          (ex_en),
                .ex_ready       (ex_ready),
                .wb_valid       (wb_valid),
                .wb_gpr_we      (wb_gpr_we),
                .wb_gpr_addr    (wb_gpr_addr),
                .wb_gpr_data    (wb_gpr_data),
                .wb_pred_we     (wb_pred_we),
                .wb_pred_addr   (wb_pred_addr),
                .wb_pred_data   (wb_pred_data),
                .br_valid       (x_br_valid),
                .br_target      (x_br_target),
                .br_taken       (x_br_taken),
                .mem_en         (x_mem_en),
                .mem_we         (x_mem_we),
                .mem_shared     (x_mem_shared),
                .mem_pc         (x_mem_pc),
                .mem_inst       (x_mem_inst),
                .mem_rd         (x_mem_rd),
                .mem_lane_addr  (x_mem_addr),
                .mem_lane_wdata (x_mem_wdata),
                .mem_ready      (x_mem_ready),
                .mem_done       (x_mem_done),
                .mem_done_rd    (x_mem_done_rd),
                .mem_done_addr  (x_mem_done_addr),
                .mem_done_rdata (x_mem_done_rdata),
                .mem_done_we    (x_mem_done_we),
                .mem_err        (x_mem_err),
                .mem_err_code   (x_mem_err_code),
                .fpu_req        (f_req[W]),
                .fpu_op         (f_op[W]),
                .fpu_rm         (f_rm[W]),
                .fpu_src1       (f_src1[W]),
                .fpu_src2       (f_src2[W]),
                .fpu_src3       (f_src3[W]),
                .fpu_ready      (f_ready[W]),
                .fpu_rsp_valid  (f_rsp[W]),
                .fpu_result     (f_result),
                .exu_err        (x_exu_err),
                .exu_err_code   (x_exu_err_code),
                .exu_busy       (x_exu_busy)
            );

            // ---------------- lsu ----------------
            logic [31:0] mem_done_pc_unused, mem_done_inst_unused;
            logic lsu_busy_unused;
            logic [3:0] lsu_conf_unused;
            lsu #(.LANES(LANES)) u_lsu (
                .clk             (clk),
                .rst             (rst),
                .mem_en          (x_mem_en),
                .mem_we          (x_mem_we),
                .mem_shared      (x_mem_shared),
                .mem_pc          (x_mem_pc),
                .mem_inst        (x_mem_inst),
                .mem_rd          (x_mem_rd),
                .eff_mask        (mask_lat),
                .lane_addr       (x_mem_addr),
                .lane_wdata      (x_mem_wdata),
                .mem_ready       (x_mem_ready),
                .smem_req_valid  (w_smem_req_valid[W]),
                .smem_req_we     (w_smem_req_we[W]),
                .smem_lane_en    (w_smem_lane_en[W]),
                .smem_lane_addr  (w_smem_lane_addr[W]),
                .smem_lane_wdata (w_smem_lane_wdata[W]),
                .smem_req_ready  (w_smem_req_ready[W]),
                .smem_rsp_valid  (w_smem_rsp_valid[W]),
                .smem_rsp_data   (w_smem_rsp_data[W]),
                .smem_conflict   (w_smem_conflict[W]),
                .smem_err_align  (smem_err_align),
                .smem_err_oob    (smem_err_oob),
                .gmem_req_valid  (w_gmem_req_valid[W]),
                .gmem_req_we     (w_gmem_req_we[W]),
                .gmem_req_line   (w_gmem_req_line[W]),
                .gmem_req_wen    (w_gmem_req_wen[W]),
                .gmem_req_wdata  (w_gmem_req_wdata[W]),
                .gmem_req_ready  (w_gmem_req_ready[W]),
                .gmem_rsp_valid  (w_gmem_rsp_valid[W]),
                .gmem_rsp_data   (w_gmem_rsp_data[W]),
                .gmem_rsp_err    (w_gmem_rsp_err[W]),
                .mem_done        (x_mem_done),
                .mem_done_rd     (x_mem_done_rd),
                .mem_done_addr   (x_mem_done_addr),
                .mem_done_pc     (mem_done_pc_unused),
                .mem_done_inst   (mem_done_inst_unused),
                .mem_done_we     (x_mem_done_we),
                .mem_done_rdata  (x_mem_done_rdata),
                .mem_err         (x_mem_err),
                .mem_err_code    (x_mem_err_code),
                .mem_busy        (lsu_busy_unused),
                .mem_conflict    (lsu_conf_unused)
            );

            // ---------------- 取指侧错误汇总 ----------------
            logic       err_now;
            logic [3:0] err_code_now;
            logic [31:0] err_pc_now;
            always_comb begin : err_sel
                err_now      = f_err_imem | f_err_simt_ovf | f_err_no_recov | f_err_internal;
                err_code_now = 4'd8;
                err_pc_now   = f_fault_pc;
                if (f_err_imem)        err_code_now = 4'd1;
                else if (f_err_simt_ovf) err_code_now = 4'd2;
                else if (f_err_no_recov) err_code_now = 4'd5;
            end

            // ---------------- 每 warp 控制 ----------------
            always_ff @(posedge clk) begin
                if (rst) begin
                    wst           <= W_IDLE;
                    dec_lat_vld   <= 1'b0;
                    dec_lat       <= '0;
                    mask_lat      <= '0;
                    fet_out_ready <= 1'b0;
                    dec_in_v      <= 1'b0;
                    latch_pc      <= 32'h0;
                    latch_inst    <= 32'h0;
                    ex_en         <= 1'b0;
                    warp_exit[W]  <= 1'b0;
                    warp_bar[W]   <= 1'b0;
                end else begin
                    ex_en        <= 1'b0;
                    warp_exit[W] <= 1'b0;

                    if (launch) begin
                        // 新内核: 状态机回到取指。不写这一支的话, 上一轮停在 W_EXIT 的
                        // warp 会永远留在 W_EXIT —— 一个 SM 只能跑一次内核。
                        wst           <= W_IDLE;
                        dec_lat_vld   <= 1'b0;
                        dec_in_v      <= 1'b0;
                        dec_lat       <= '0;
                        fet_out_ready <= 1'b0;
                        ex_en         <= 1'b0;
                    end else if (sm_halt) begin
                        wst <= W_EXIT;
                    end else if (err_now) begin
                        // 取指侧的第一处错误(非法指令在 W_EXEC 里判)
                        warp_err_pulse[W] <= 1'b1;
                        warp_err_code[W]  <= err_code_now;
                        warp_err_pc[W]    <= err_pc_now;
                        wst               <= W_EXIT;
                    end else begin
                        unique case (wst)
                            W_IDLE: begin
                                // fet_out_ready 是寄存器: 前一拍先举手, 下一拍才谈握手。
                                // 握手那一拍必须让 fetch 看到 ready=1, 否则它永远停在 S_SENT。
                                if (fet_out_valid && fet_out_ready) begin
                                    latch_pc      <= f_pc;
                                    latch_inst    <= f_inst;
                                    mask_lat      <= f_inst_mask;
                                    dec_in_v      <= 1'b1;
                                    fet_out_ready <= 1'b0;
                                    wst           <= W_DEC;
                                end else begin
                                    fet_out_ready <= 1'b1;
                                end
                            end
                            W_DEC: begin
                                if (dec_out.valid) begin
                                    dec_in_v     <= 1'b0;
                                    dec_lat      <= dec_info;
                                    dec_lat_vld  <= 1'b1;
                                    wst          <= W_EXEC;
                                end
                            end
                            W_EXEC: begin
                                if (dec_lat.illegal) begin
                                    warp_err_pulse[W] <= 1'b1;
                                    warp_err_code[W]  <= 4'd1;
                                    warp_err_pc[W]    <= dec_lat.pc;
                                    wst               <= W_EXIT;
                                end else if (dec_lat.is_barrier) begin
                                    warp_bar[W] <= 1'b1;
                                    wst         <= W_BAR;
                                end else if (dec_lat.is_exit) begin
                                    warp_exit[W] <= 1'b1;
                                    wst          <= W_EXIT;
                                end else if (x_exu_err) begin
                                    warp_err_pulse[W] <= 1'b1;
                                    warp_err_code[W]  <= x_exu_err_code;
                                    warp_err_pc[W]    <= dec_lat.pc;
                                    wst               <= W_EXIT;
                                end else begin
                                    if (ex_ready) ex_en <= 1'b1;
                                    if (wb_valid) begin
                                        dec_lat_vld <= 1'b0;
                                        wst         <= W_IDLE;
                                    end
                                end
                            end
                            W_BAR: begin
                                if (!bar_hold && !bar_pending) wst <= W_IDLE;
                            end
                            W_EXIT: begin
                                warp_exit[W] <= 1'b1;
                            end
                            default: wst <= W_IDLE;
                        endcase
                    end
                end
            end

            // 只要 warp 还在跑, 就允许 fetch 取指(fetch 自己会在 S_SENT 等我们收)
            assign fet_in_valid = warp_run[W] && !sm_halt;

        end
    endgenerate

    // ======================================================================
    //  屏障与退出: 两者都要先把本 SM 的写缓冲冲刷干净(ISA §2.4/§2.5)
    //    · bar.sync:  全部在跑 warp 到齐 ⇒ 冲刷 ⇒ 放行
    //    · exit:      冲刷完才能退休(否则内核结束时 st.g 还留在 WCB 里,
    //                 host 读回帧缓冲会看到旧值)
    // ======================================================================
    logic             bar_pending;
    logic             bar_hold;
    logic             bar_flush_req;
    logic             exit_flush_req;

    always_comb begin : barrier_all
        integer k;
        bar_hold      = 1'b0;
        bar_flush_req = 1'b0;
        for (k = 0; k < WARPS; k = k + 1) begin
            // 正在退出的 warp 不再参与"到齐"判定, 否则它会把自己或别人卡死
            if (warp_run[k] && !warp_bar[k] && !warp_exit[k]) bar_hold = 1'b1;
        end
        bar_flush_req = !bar_hold && (|warp_bar);
    end

    // 有 warp 已经执行完 exit 但还没退休 ⇒ 先冲刷写缓冲
    assign exit_flush_req = (|warp_exit);

    // ======================================================================
    //  全局: 发射轮转 / 错误 / done / 停机
    // ======================================================================
    logic             bar_flush_done;

    always_ff @(posedge clk) begin
        if (rst) begin
            sm_done     <= 1'b0;
            sm_error    <= 4'd0;
            sm_fault_pc <= 32'h0;
            sm_halt     <= 1'b0;
            warp_run    <= '0;
            bar_pending <= '0;
        end else begin
            sm_done <= 1'b0;

            // 退休只在写缓冲排空后进行(见上面的 exit_flush_req)
            if (!exit_flush_req || flush_done) begin
                warp_run  <= warp_run & ~warp_exit;
                warp_exit <= '0;
            end

            if (launch) begin
                warp_run    <= (4'hF >> (4 - launch_warps));
                sm_error    <= 4'd0;
                sm_fault_pc <= 32'h0;
                sm_halt     <= 1'b0;
                warp_bar    <= '0;
            end

            // 屏障到齐 / 退出 ⇒ 冲刷一次 WCB, 排空后放行
            if ((bar_flush_req || exit_flush_req) && !bar_pending) bar_pending <= 1'b1;
            if (bar_pending && bar_flush_done) begin
                bar_pending <= 1'b0;
                warp_bar    <= '0;
            end

            // 错误汇总(取第一处, 停机)
            for (int k = 0; k < WARPS; k = k + 1) begin
                if (!sm_halt && warp_err_pulse[k]) begin
                    sm_error    <= warp_err_code[k];
                    sm_fault_pc <= warp_err_pc[k];
                    sm_halt     <= 1'b1;
                end
            end

            // 全部退出 ⇒ done(屏障冲刷完才算)
            if (!sm_halt && !(|warp_run) && !bar_pending && !launch)
                sm_done <= 1'b1;
        end
    end

    assign w_flush_req    = {WARPS{bar_pending}};
    assign bar_flush_done = |w_flush_done;

endmodule
