`include "defines.vh"

// ============================================================================
//  lsu_scheduler —— 每 SM 一个的访存调度器(MEM_LSU.md §4.4)
//
//  汇合 WARPS 个 warp 的 LSU 请求, 分两条路往外走:
//    · 共享内存: 一个 warp 级端口(8 个 lane 地址), 轮转仲裁;
//    · 全局内存: 一次一个 32B 行请求(写合并由 global_mem 的 WCB 负责), 轮转仲裁。
//  两条路各自只有一个在飞事务: 授予后记录 owner, 响应回来再按 owner 送回。
//  `flush`(bar.sync / exit / FENCE)也走这里, 等 WCB 排空才回 done。
// ============================================================================

module lsu_scheduler #(
    parameter int WARPS = `WARPS_PER_SM,
    parameter int LANES = `WARP_SIZE
) (
    input  wire                          clk           ,
    input  wire                          rst           ,

    // ---- 每 warp 的 LSU 端口 ----
    input  wire [WARPS-1:0]              w_smem_req_valid ,
    input  wire [WARPS-1:0]              w_smem_req_we    ,
    input  wire [WARPS-1:0][LANES-1:0]   w_smem_lane_en   ,
    input  wire [WARPS-1:0][LANES-1:0][31:0] w_smem_lane_addr,
    input  wire [WARPS-1:0][LANES-1:0][31:0] w_smem_lane_wdata,
    output logic [WARPS-1:0]             w_smem_req_ready ,
    output logic [WARPS-1:0]             w_smem_rsp_valid ,
    output logic [WARPS-1:0][LANES-1:0][31:0] w_smem_rsp_data,
    output logic [WARPS-1:0][3:0]        w_smem_conflict  ,

    input  wire [WARPS-1:0]              w_gmem_req_valid ,
    input  wire [WARPS-1:0]              w_gmem_req_we    ,
    input  wire [WARPS-1:0][26:0]        w_gmem_req_line  ,
    input  wire [WARPS-1:0][LANES-1:0]   w_gmem_req_wen   ,
    input  wire [WARPS-1:0][LANES-1:0][31:0] w_gmem_req_wdata,
    output logic [WARPS-1:0]             w_gmem_req_ready ,
    output logic [WARPS-1:0]             w_gmem_rsp_valid ,
    output logic [WARPS-1:0][LANES-1:0][31:0] w_gmem_rsp_data,
    output logic [WARPS-1:0]             w_gmem_rsp_err   ,

    // ---- bar.sync / exit: 某 warp 要求把 WCB 里未落盘的行冲刷掉 ----
    input  wire [WARPS-1:0]              w_flush_req      ,
    output logic [WARPS-1:0]             w_flush_done     ,

    // ---- 共享内存(一个实例) ----
    output logic                         smem_req_valid   ,
    output logic                         smem_req_we      ,
    output logic [LANES-1:0]             smem_lane_en     ,
    output logic [LANES-1:0][31:0]       smem_lane_addr   ,
    output logic [LANES-1:0][31:0]       smem_lane_wdata  ,
    input  wire                          smem_req_ready   ,
    input  wire                          smem_rsp_valid   ,
    input  wire [LANES-1:0][31:0]        smem_rsp_data    ,
    input  wire [3:0]                    smem_conflict    ,
    /* verilator lint_off UNUSEDSIGNAL */
    input  wire                          smem_err_align   ,   // 由 lsu 侧按请求地址预判
    input  wire                          smem_err_oob     ,
    /* verilator lint_on UNUSEDSIGNAL */

    // ---- 全局内存(一个实例, 行读写口) ----
    output logic                         gmem_rd_valid    ,
    input  wire                          gmem_rd_ready    ,
    output logic [26:0]                  gmem_rd_line     ,
    output logic [LANES-1:0]             gmem_rd_wen      ,
    input  wire                          gmem_rsp_valid   ,
    /* verilator lint_off UNUSEDSIGNAL */
    input  wire [26:0]                   gmem_rsp_line    ,   // 行号已由 owner 记住
    /* verilator lint_on UNUSEDSIGNAL */
    input  wire [LANES-1:0][31:0]        gmem_rsp_data    ,
    input  wire                          gmem_rsp_err     ,
    output logic                         gmem_wr_valid    ,
    input  wire                          gmem_wr_ready    ,
    output logic [26:0]                  gmem_wr_line     ,
    output logic [LANES-1:0]             gmem_wr_wen      ,
    output logic [LANES-1:0][31:0]       gmem_wr_wdata    ,
    /* verilator lint_off UNUSEDSIGNAL */
    input  wire                          gmem_wr_err      ,   // 写错误在 flush 时统一上报
    /* verilator lint_on UNUSEDSIGNAL */
    output logic                         flush_req        ,
    input  wire                          flush_done       ,
    output logic                         busy
);

    localparam int WP = $clog2(WARPS);

    // ---- 共享内存仲裁 ----
    logic              sm_busy;
    logic [WP-1:0]     sm_owner;
    logic [WP-1:0]     sm_rr;         // 轮转指针
    logic [WP-1:0]     sm_grant;
    logic              sm_grant_vld;

    always_comb begin : sm_pick
        integer wi;
        sm_grant_vld = 1'b0;
        sm_grant     = '0;
        for (wi = WARPS - 1; wi >= 0; wi = wi - 1) begin
            if (w_smem_req_valid[wi] && !sm_busy &&
                (!sm_grant_vld || (wi <= sm_rr))) begin
                sm_grant_vld = 1'b1;
                sm_grant     = wi[WP-1:0];
            end
        end
    end

    assign smem_req_valid  = sm_grant_vld;
    assign smem_req_we     = w_smem_req_we[sm_grant];
    assign smem_lane_en    = w_smem_lane_en[sm_grant];
    assign smem_lane_addr  = w_smem_lane_addr[sm_grant];
    assign smem_lane_wdata = w_smem_lane_wdata[sm_grant];

    // 只有被选中的 warp 看到 ready
    always_comb begin : sm_ready
        integer wi;
        w_smem_req_ready = '0;
        for (wi = 0; wi < WARPS; wi = wi + 1)
            w_smem_req_ready[wi] = (sm_grant_vld && (sm_grant == wi[WP-1:0]) && smem_req_ready);
    end

    // ---- 全局内存仲裁 ----
    logic              gm_busy;
    logic              gm_is_wr;      // 当前在飞的是写(WCB 已收下, 下一拍回 done)
    logic [WP-1:0]     gm_owner;
    logic [WP-1:0]     gm_rr;
    logic [WP-1:0]     gm_grant;
    logic              gm_grant_vld;

    always_comb begin : gm_pick
        integer wi;
        gm_grant_vld = 1'b0;
        gm_grant     = '0;
        for (wi = WARPS - 1; wi >= 0; wi = wi - 1) begin
            if (w_gmem_req_valid[wi] && !gm_busy &&
                (!gm_grant_vld || (wi <= gm_rr))) begin
                gm_grant_vld = 1'b1;
                gm_grant     = wi[WP-1:0];
            end
        end
    end

    // 写: 直接进 global_mem 的 WCB(写合并), 立刻回"已接受";
    // 读: 发 32B 行请求, 等整行回来。两条路共用一个 owner。
    assign gmem_rd_valid = gm_grant_vld && !w_gmem_req_we[gm_grant];
    assign gmem_rd_line  = w_gmem_req_line[gm_grant];
    assign gmem_rd_wen   = w_gmem_req_wen[gm_grant];
    assign gmem_wr_valid = gm_grant_vld &&  w_gmem_req_we[gm_grant];
    assign gmem_wr_line  = w_gmem_req_line[gm_grant];
    assign gmem_wr_wen   = w_gmem_req_wen[gm_grant];
    assign gmem_wr_wdata = w_gmem_req_wdata[gm_grant];

    always_comb begin : gm_ready
        integer wi;
        w_gmem_req_ready = '0;
        for (wi = 0; wi < WARPS; wi = wi + 1)
            w_gmem_req_ready[wi] = (gm_grant_vld && (gm_grant == wi[WP-1:0]) &&
                                    (w_gmem_req_we[wi] ? gmem_wr_ready : gmem_rd_ready));
    end

    // ---- 冲刷 ----
    logic              fl_busy;
    logic [WP-1:0]     fl_owner;

    assign flush_req = fl_busy;

    always_comb begin : flush_out
        integer wi;
        w_flush_done = '0;
        for (wi = 0; wi < WARPS; wi = wi + 1)
            w_flush_done[wi] = (fl_busy && (fl_owner == wi[WP-1:0]) && flush_done);
    end

    assign busy = sm_busy || gm_busy || fl_busy;

    // ---- 时序 ----
    always_ff @(posedge clk) begin
        if (rst) begin
            sm_busy <= 1'b0;
            sm_owner <= '0;
            sm_rr   <= '0;
            gm_busy <= 1'b0;
            gm_is_wr <= 1'b0;
            gm_owner <= '0;
            gm_rr   <= '0;
            fl_busy <= 1'b0;
            fl_owner <= '0;
            w_smem_rsp_valid  <= '0;
            w_smem_rsp_data   <= '0;
            w_smem_conflict   <= '0;
            w_gmem_rsp_valid  <= '0;
            w_gmem_rsp_data   <= '0;
            w_gmem_rsp_err    <= '0;
        end else begin
            w_smem_rsp_valid <= '0;
            w_gmem_rsp_valid <= '0;

            // 共享内存: 授予 → 等响应 → 归还
            if (sm_grant_vld && smem_req_ready) begin
                sm_busy  <= 1'b1;
                sm_owner <= sm_grant;
            end
            if (sm_busy && smem_rsp_valid) begin
                sm_busy <= 1'b0;
                sm_rr   <= sm_owner + 1'b1;
                w_smem_rsp_valid[sm_owner] <= 1'b1;
                w_smem_rsp_data[sm_owner]  <= smem_rsp_data;
                w_smem_conflict[sm_owner]  <= smem_conflict;
            end

            // 全局内存: 读等整行回来; 写进 WCB 后下一拍就回"完成"
            if (gm_grant_vld && (w_gmem_req_we[gm_grant] ? gmem_wr_ready : gmem_rd_ready)) begin
                gm_busy  <= 1'b1;
                gm_is_wr <= w_gmem_req_we[gm_grant];
                gm_owner <= gm_grant;
            end
            if (gm_busy && gm_is_wr) begin
                gm_busy <= 1'b0;
                gm_rr   <= gm_owner + 1'b1;
                w_gmem_rsp_valid[gm_owner] <= 1'b1;
                w_gmem_rsp_err[gm_owner]   <= 1'b0;
            end else if (gm_busy && gmem_rsp_valid) begin
                gm_busy <= 1'b0;
                gm_rr   <= gm_owner + 1'b1;
                w_gmem_rsp_valid[gm_owner] <= 1'b1;
                w_gmem_rsp_data[gm_owner]  <= gmem_rsp_data;
                w_gmem_rsp_err[gm_owner]   <= gmem_rsp_err;
            end

            // 冲刷: 任一 warp 发起(取最低号), 排空后回 done
            if (!fl_busy) begin
                for (int wi = WARPS - 1; wi >= 0; wi = wi - 1) begin
                    if (w_flush_req[wi] && !fl_busy) begin
                        fl_busy  <= 1'b1;
                        fl_owner <= wi[WP-1:0];
                    end
                end
            end else if (flush_done) begin
                fl_busy <= 1'b0;
            end
        end
    end

endmodule
