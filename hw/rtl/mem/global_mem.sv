`include "defines.vh"

// ============================================================================
//  global_mem —— SM 侧的全局内存通路(行请求 → AXI4 + WCB,MEM_LSU.md §5 D1/D2/D3)
//
//  * `ld.g` : 32B 行请求 → WCB 读旁路,未命中才发 AXI4 读(arlen=7 的 8×4B burst),
//             回来的行再用 WCB 里"写过但还没写出去"的字叠加(store→load 可见);
//  * `st.g` : 先进 WCB 按行合并,再按行写出去(AW + 8 拍 W,每拍 wstrb 标记该
//             4B 字是否有效);`flush_req` 等缓冲排空且写通路空闲才回 done。
//  * 总线错误(`rresp/bresp != OKAY`)通过 `rsp_err` 上报,由 LSU 转成 `BUS_ERROR`。
//
//  v1 每 SM 单次在飞:一次只挂一个读/一个写,协议面小;参考模型有 4 项在飞,
//  这里为了先用最小面积跑通内核(MEM_LSU §9 已登记)。
// ============================================================================

module global_mem (
    input  wire                    clk,
    input  wire                    rst,

    // ---- SM 侧: 行读 ----
    input  wire                    rd_valid,
    output logic                   rd_ready,       // 上一拍末接受
    input  wire [26:0]             rd_line,
    input  wire [7:0]              rd_wen,
    output logic                   rsp_valid,
    output logic [26:0]            rsp_line,
    output logic [7:0][31:0]       rsp_data,
    output logic                   rsp_err,

    // ---- SM 侧: 行写(进 WCB) ----
    input  wire                    wr_valid,
    output logic                   wr_ready,
    input  wire [26:0]             wr_line,
    input  wire [7:0]              wr_wen,
    input  wire [7:0][31:0]        wr_wdata,

    // ---- 冲刷(bar.sync / exit) ----
    input  wire                    flush_req,
    output logic                   flush_done,
    output logic                   busy,
    output logic                   wr_err,         // 脉冲: 本次 AXI 写收到错误响应

    // ---- AXI4 主口 ----
    output logic                   m_arvalid,
    input  wire                    m_arready,
    output logic [31:0]            m_araddr,
    output logic [7:0]             m_arlen,
    output logic [2:0]             m_arsize,
    output logic [1:0]             m_arburst,
    output logic [3:0]             m_arid,

    input  wire                    m_rvalid,
    output logic                   m_rready,
    input  wire [31:0]             m_rdata,
    input  wire [1:0]              m_rresp,
    input  wire                    m_rlast,
    /* verilator lint_off UNUSEDSIGNAL */
    // 单主口, id 未参与路由(多主时由 axi_arb 用 id 分流)
    input  wire [3:0]              m_rid,

    output logic                   m_awvalid,
    input  wire                    m_awready,
    output logic [31:0]            m_awaddr,
    output logic [7:0]             m_awlen,
    output logic [2:0]             m_awsize,
    output logic [1:0]             m_awburst,
    output logic [3:0]             m_awid,

    output logic                   m_wvalid,
    input  wire                    m_wready,
    output logic [31:0]            m_wdata,
    output logic [3:0]             m_wstrb,
    output logic                   m_wlast,

    input  wire                    m_bvalid,
    output logic                   m_bready,
    input  wire [1:0]              m_bresp,
    input  wire [3:0]              m_bid
    /* verilator lint_on UNUSEDSIGNAL */
);

    localparam logic [2:0] AXI_SIZE_4B  = 3'b010;
    localparam logic [1:0] AXI_BURST_INC = 2'b01;

    // ---- WCB ----
    logic                   wcb_wr_ready;
    logic                   wcb_rd_hit;
    logic [7:0][31:0]       wcb_rd_data;
    logic [7:0][31:0]       bus_line;         // 总线回来的行(含最后一拍)
    logic [7:0][31:0]       mg_out;           // WCB 叠加后的行
    logic                   wcb_empty, wcb_full;
    logic                   wcb_out_valid, wcb_out_ready;
    logic [26:0]            wcb_out_line;
    logic [7:0]             wcb_out_wen;
    logic [7:0][31:0]       wcb_out_wdata;

    wcb u_wcb (
        .clk        (clk),
        .rst        (rst),
        .wr_valid   (wr_valid),
        .wr_line    (wr_line),
        .wr_wen     (wr_wen),
        .wr_wdata   (wr_wdata),
        .wr_ready   (wcb_wr_ready),
        .rd_line    (rd_line),
        .rd_wen     (rd_wen),
        .rd_hit     (wcb_rd_hit),
        .rd_data    (wcb_rd_data),
        .mg_line    (rsp_line),
        .mg_data_in (bus_line),
        .mg_data_out(mg_out),
        .empty      (wcb_empty),
        .full       (wcb_full),
        .out_valid  (wcb_out_valid),
        .out_line   (wcb_out_line),
        .out_wen    (wcb_out_wen),
        .out_wdata  (wcb_out_wdata),
        .out_ready  (wcb_out_ready)
    );

    assign wr_ready = wcb_wr_ready;

    // 冲刷完成 = 缓冲空 + 写通路回到空闲。只看 wcb 的"缓冲已空"不够:
    // 最后一项被取走后 AXI 写还在飞, 此时回 done 会让上层以为数据已落盘。
    logic flush_seen;
    assign flush_done = flush_req && !flush_seen && wcb_empty && (wr_st == W_IDLE);

    // ==========================================================================
    //  读通路
    // ==========================================================================
    typedef enum logic [1:0] {
        R_IDLE, R_AR, R_BEAT
    } rd_state_t;

    rd_state_t          rst_r;
    logic [26:0]        rd_line_q;
    logic [2:0]         beat_q;
    logic [7:0][31:0]   line_buf;

    // 响应锁存: 最后一拍的数据在这一拍内是组合的, 必须与本拍结束时一起锁存,
    // 下一拍打一个单拍 `rsp_valid` 脉冲输出(否则状态一清, rsp 就没了)。
    logic               rsp_vld_q;
    logic [7:0][31:0]   rsp_data_q;
    logic               rsp_err_q;

    assign rd_ready  = (rst_r == R_IDLE);
    assign rsp_valid = rsp_vld_q;
    assign rsp_line  = rd_line_q;
    assign rsp_data  = rsp_data_q;
    assign rsp_err   = rsp_err_q;

    // 总线回来的行(最后一拍的数据还没进 line_buf, 组合拼上)
    always_comb begin
        bus_line = line_buf;
        if ((rst_r == R_BEAT) && m_rvalid)
            bus_line[beat_q] = m_rdata;
    end

    assign m_arvalid = (rst_r == R_AR);
    assign m_araddr  = {rd_line_q, 5'b0};
    assign m_arlen   = 8'd7;
    assign m_arsize  = AXI_SIZE_4B;
    assign m_arburst = AXI_BURST_INC;
    assign m_arid    = 4'd0;
    assign m_rready  = (rst_r == R_BEAT);

    always_ff @(posedge clk) begin
        if (rst) begin
            rst_r      <= R_IDLE;
            rd_line_q  <= '0;
            beat_q     <= '0;
            line_buf   <= '0;
            rsp_vld_q  <= 1'b0;
            rsp_data_q <= '0;
            rsp_err_q  <= 1'b0;
        end else begin
            rsp_vld_q <= 1'b0;                 // 默认单拍脉冲
            unique case (rst_r)
                R_IDLE: begin
                    if (rd_valid) begin
                        if (wcb_rd_hit) begin
                            // 请求的字全在 WCB 里 ⇒ 当拍就回, 不必占总线(读旁路)。
                            // 数据必须在这里锁存: 冲刷随时可能把这一项移出缓冲。
                            rsp_vld_q  <= 1'b1;
                            rsp_data_q <= wcb_rd_data;
                            rsp_err_q  <= 1'b0;
                        end else begin
                            rd_line_q <= rd_line;
                            rst_r     <= R_AR;
                        end
                    end
                end
                R_AR: begin
                    if (m_arready) begin
                        rst_r  <= R_BEAT;
                        beat_q <= '0;
                    end
                end
                R_BEAT: begin
                    if (m_rvalid) begin
                        line_buf[beat_q] <= m_rdata;
                        if (m_rlast) begin
                            rsp_vld_q  <= 1'b1;
                            rsp_data_q <= mg_out;      // 总线行 + WCB 里更新的字
                            rsp_err_q  <= (m_rresp != 2'b00);
                            rst_r      <= R_IDLE;
                        end else begin
                            beat_q <= beat_q + 3'd1;
                        end
                    end
                end
                default: rst_r <= R_IDLE;
            endcase
        end
    end

    // ==========================================================================
    //  写通路: WCB → AXI4(AW + 8 拍 W)
    // ==========================================================================
    typedef enum logic [1:0] {
        W_IDLE, W_AW, W_DATA, W_RESP
    } wr_state_t;

    wr_state_t      wr_st;
    logic [26:0]    wr_line_q;
    logic [7:0]     wr_wen_q;
    logic [7:0][31:0] wr_data_q;
    logic [2:0]     wbeat_q;

    assign wcb_out_ready = (wr_st == W_IDLE);

    assign m_awvalid = (wr_st == W_AW);
    assign m_awaddr  = {wr_line_q, 5'b0};
    assign m_awlen   = 8'd7;
    assign m_awsize  = AXI_SIZE_4B;
    assign m_awburst = AXI_BURST_INC;
    assign m_awid    = 4'd1;

    assign m_wvalid  = (wr_st == W_DATA);
    assign m_wdata   = wr_data_q[wbeat_q];
    assign m_wstrb   = wr_wen_q[wbeat_q] ? 4'hF : 4'h0;
    assign m_wlast   = (wbeat_q == 3'd7);

    assign m_bready  = (wr_st == W_RESP);

    always_ff @(posedge clk) begin
        if (rst) begin
            wr_st     <= W_IDLE;
            wr_err    <= 1'b0;
            wr_line_q <= '0;
            wr_wen_q  <= '0;
            wr_data_q <= '0;
            wbeat_q   <= '0;
        end else begin
            wr_err <= 1'b0;
            unique case (wr_st)
                W_IDLE: begin
                    if (wcb_out_valid) begin
                        wr_line_q <= wcb_out_line;
                        wr_wen_q  <= wcb_out_wen;
                        wr_data_q <= wcb_out_wdata;
                        wbeat_q   <= 3'd0;      // 每笔事务都从第 0 拍开始
                        wr_st     <= W_AW;
                    end
                end
                W_AW: begin
                    if (m_awready) wr_st <= W_DATA;
                end
                W_DATA: begin
                    if (m_wready) begin
                        if (wbeat_q == 3'd7) wr_st <= W_RESP;
                        else                 wbeat_q <= wbeat_q + 3'd1;
                    end
                end
                W_RESP: begin
                    if (m_bvalid) begin
                        wr_err <= (m_bresp != 2'b00);   // 错误上报给 LSU(BUS_ERROR)
                        wr_st  <= W_IDLE;
                    end
                end
                default: wr_st <= W_IDLE;
            endcase
        end
    end

    always_ff @(posedge clk) begin
        if (rst)                     flush_seen <= 1'b0;
        else if (!flush_req)         flush_seen <= 1'b0;
        else if (flush_done)         flush_seen <= 1'b1;
    end

    assign busy = (rst_r != R_IDLE) || (wr_st != W_IDLE);

endmodule
