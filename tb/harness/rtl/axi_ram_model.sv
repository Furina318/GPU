`include "defines.vh"

// ============================================================================
//  axi_ram_model —— **仿真用**的 AXI4 从设备内存模型(不是设计本体)
//
//  为什么会有一块 Verilog 内存模型而不是在 cocotb 里用 Python 写:
//  Python 侧驱动/采样 AXI 握手在 Verilator 下有 1 拍可见性滞后, 很容易写成
//  "本拍置起、本拍撤销"或"早一拍握手", 实测会把主口卡死。用 RTL 建模后, 握手
//  语义与其余 RTL 完全同一套时钟, 没有歧义; 同一块模型后面还要给
//  `sim/run-sim/` 的全系统仿真当 DDR 用。
//
//  只实现本仓库用到的子集:
//    · INCR burst, awsize/arsize 固定 4B(`2'b010`),len 0..255;
//    · 从机永远 ready(awready/wready/arready 恒 1),响应不插气泡;
//    · 写:W 拍按 `wstrb` 逐字节落盘,最后一拍后拉 bvalid 直到 bready;
//    · 读:AR 被接受后逐拍给 rvalid,`rlast` 在最后一拍置起。
// ============================================================================

/* verilator lint_off UNUSEDSIGNAL */
// 地址只在字粒度上有意义: 低 2 位(字节偏移)被 burst 的逐拍 4B 步进取代,
// 高 7 位超出 `DEPTH_WORDS` 的寻址范围 —— 这两段地址位有意不参与译码。
module axi_ram_model #(
    parameter int DEPTH_WORDS = 1 << 16,   // 默认 256KB
    parameter     INIT_FILE   = ""         // 非空则 $readmemh 预载
) (
    input  wire         clk        ,
    input  wire         rst        ,

    // ---------------- AR / R ----------------
    input  wire         m_arvalid  ,
    output wire         m_arready  ,
    input  wire [31:0]  m_araddr   ,
    input  wire [7:0]   m_arlen    ,
    output wire         m_rvalid   ,
    output wire [31:0]  m_rdata    ,
    output wire [1:0]   m_rresp    ,
    output wire         m_rlast    ,
    input  wire         m_rready   ,

    // ---------------- AW / W / B ----------------
    input  wire         m_awvalid  ,
    output wire         m_awready  ,
    input  wire [31:0]  m_awaddr   ,
    input  wire [7:0]   m_awlen    ,
    input  wire         m_wvalid   ,
    output wire         m_wready   ,
    input  wire [31:0]  m_wdata    ,
    input  wire [3:0]   m_wstrb    ,
    input  wire         m_wlast    ,
    output wire         m_bvalid   ,
    output wire [1:0]   m_bresp    ,
    input  wire         m_bready   ,

    // ---------------- 观察/旁路口 ----------------
    // stat_*:给用例断言"这条行读/写到底发了几拍";
    // host_*:host 视角直接读写内存模型(铺初始数据、比对写结果),
    //        只在 DUT 空闲时由用例驱动。
    output logic [31:0] stat_ar_cnt ,
    output logic [31:0] stat_aw_cnt ,
    input  wire         host_we    ,
    input  wire [31:0]  host_addr  ,
    input  wire [31:0]  host_wdata ,
    output wire [31:0]  host_rdata
);

    // ---------------- 存储体 ----------------
    logic [31:0] mem [0:DEPTH_WORDS-1];
    integer      mi;

    initial begin
        for (mi = 0; mi < DEPTH_WORDS; mi = mi + 1) mem[mi] = 32'h0;
        if (INIT_FILE != "") $readmemh(INIT_FILE, mem);
    end

    localparam int IDX_W = $clog2(DEPTH_WORDS);

    assign host_rdata = mem[host_addr[IDX_W+1:2]];

    assign m_arready = 1'b1;
    assign m_awready = 1'b1;
    assign m_wready  = 1'b1;

    // ---------------- 读通路 ----------------
    logic        rd_act;
    logic [31:0] rd_addr;
    logic [7:0]  rd_len;
    logic [7:0]  rd_beat;

    assign m_rvalid = rd_act;
    assign m_rdata  = mem[rd_addr[IDX_W+1:2] + {{(IDX_W-8){1'b0}}, rd_beat}];
    assign m_rresp  = 2'b00;
    assign m_rlast  = (rd_beat == rd_len);

    always_ff @(posedge clk) begin
        if (rst) begin
            rd_act  <= 1'b0;
            rd_addr <= '0;
            rd_len  <= '0;
            rd_beat <= '0;
            stat_ar_cnt <= '0;
        end else if (!rd_act) begin
            if (m_arvalid) begin
                rd_act  <= 1'b1;
                rd_addr <= m_araddr;
                rd_len  <= m_arlen;
                rd_beat <= 8'd0;
                stat_ar_cnt <= stat_ar_cnt + 32'd1;
            end
        end else if (m_rready) begin
            if (rd_beat == rd_len) rd_act  <= 1'b0;
            else                   rd_beat <= rd_beat + 8'd1;
        end
    end

    // ---------------- 写通路 ----------------
    logic        wr_act;
    logic [31:0] wr_addr;
    logic [7:0]  wr_len;
    logic [7:0]  wr_beat;
    logic        b_pend;

    assign m_bvalid = b_pend;
    assign m_bresp  = 2'b00;

    integer bi;

    always_ff @(posedge clk) begin
        if (rst) begin
            wr_act  <= 1'b0;
            wr_addr <= '0;
            wr_len  <= '0;
            wr_beat <= '0;
            b_pend  <= 1'b0;
            stat_aw_cnt <= '0;
        end else begin
            if (!wr_act) begin
                if (m_awvalid) begin
                    wr_act  <= 1'b1;
                    wr_addr <= m_awaddr;
                    wr_len  <= m_awlen;
                    wr_beat <= 8'd0;
                    stat_aw_cnt <= stat_aw_cnt + 32'd1;
                end
            end else if (m_wvalid) begin
                for (bi = 0; bi < 4; bi = bi + 1) begin
                    if (m_wstrb[bi]) begin
                        mem[wr_addr[IDX_W+1:2] + {{(IDX_W-8){1'b0}}, wr_beat}][8*bi +: 8] <= m_wdata[8*bi +: 8];
                    end
                end
                if (m_wlast || (wr_beat == wr_len)) begin
                    wr_act <= 1'b0;
                    b_pend <= 1'b1;
                end else begin
                    wr_beat <= wr_beat + 8'd1;
                end
            end
            // B 响应:拉高后一直保持到 bready
            if (b_pend && m_bready) b_pend <= 1'b0;
            // host 旁路写
            if (host_we) mem[host_addr[IDX_W+1:2]] <= host_wdata;
        end
    end

endmodule
/* verilator lint_on UNUSEDSIGNAL */
