`include "defines.vh"

// ============================================================================
//  axi_arb_wrap —— 仿真专用胶水:axi_arb + axi_ram_model
//
//  把 3 个主(CP / SM0 / SM1)的请求接到仲裁器,仲裁器的主口接一块 AXI RAM,
//  再用一个旁路口让 host 直接读写这块内存(铺数据 / 查结果)。
//  只在 tb 里用,不属于设计。
// ============================================================================

module axi_arb_wrap #(
    parameter int NUM_M      = 3,
    parameter int ADDR_W     = 32,
    parameter int DATA_W     = 32,
    parameter int LEN_W      = 8,
    parameter int DEPTH_WORDS = 1 << 14
) (
    input  wire clk,
    input  wire rst,

    input  wire [NUM_M-1:0]               s_arvalid,
    output logic [NUM_M-1:0]              s_arready,
    input  wire [NUM_M-1:0][ADDR_W-1:0]   s_araddr,
    input  wire [NUM_M-1:0][LEN_W-1:0]    s_arlen,
    output logic [NUM_M-1:0]              s_rvalid,
    input  wire [NUM_M-1:0]               s_rready,
    output logic [NUM_M-1:0][DATA_W-1:0]  s_rdata,
    output logic [NUM_M-1:0]              s_rlast,

    input  wire [NUM_M-1:0]               s_awvalid,
    output logic [NUM_M-1:0]              s_awready,
    input  wire [NUM_M-1:0][ADDR_W-1:0]   s_awaddr,
    input  wire [NUM_M-1:0][LEN_W-1:0]    s_awlen,
    input  wire [NUM_M-1:0]               s_wvalid,
    output logic [NUM_M-1:0]              s_wready,
    input  wire [NUM_M-1:0][DATA_W-1:0]   s_wdata,
    input  wire [NUM_M-1:0][DATA_W/8-1:0] s_wstrb,
    input  wire [NUM_M-1:0]               s_wlast,
    output logic [NUM_M-1:0]              s_bvalid,
    input  wire [NUM_M-1:0]               s_bready,

    // host 旁路
    input  wire        ram_we,
    input  wire [31:0] ram_addr,
    input  wire [31:0] ram_wdata,
    output logic [31:0] ram_rdata,

    // 观察口
    output logic [31:0] stat_ar_cnt,
    output logic [31:0] stat_aw_cnt
);

    logic                m_arvalid, m_arready;
    logic [ADDR_W-1:0]   m_araddr;
    logic [LEN_W-1:0]    m_arlen;
    logic [2:0]          m_arsize, m_awsize;
    logic [1:0]          m_arburst, m_awburst;
    logic [3:0]          m_arid, m_awid;
    /* verilator lint_off UNUSEDSIGNAL */
    logic [3:0]          unused_id;
    /* verilator lint_on UNUSEDSIGNAL */
    // m_arsize/m_awsize/m_arburst/m_awburst/m_arid/m_awid 由仲裁器给出但 DDR
    // 模型不看:模型固定按 4B 递增, id 在单从口下不参与路由。
    /* verilator lint_off UNUSEDSIGNAL */
    logic [2:0] unused_sz = m_arsize | m_awsize;
    logic [1:0] unused_burst = m_arburst | m_awburst;
    logic [3:0] unused_id2 = m_arid | m_awid;
    /* verilator lint_on UNUSEDSIGNAL */
    logic                m_rvalid, m_rready, m_rlast;
    logic [DATA_W-1:0]   m_rdata;
    logic [1:0]          m_rresp;

    logic                m_awvalid, m_awready;
    logic [ADDR_W-1:0]   m_awaddr;
    logic [LEN_W-1:0]    m_awlen;
    logic                m_wvalid, m_wready, m_wlast;
    logic [DATA_W-1:0]   m_wdata;
    logic [DATA_W/8-1:0] m_wstrb;
    logic                m_bvalid, m_bready;
    logic [1:0]          m_bresp;

    logic [NUM_M-1:0][1:0] s_rresp, s_bresp;

    axi_arb #(
        .NUM_M(NUM_M), .ADDR_W(ADDR_W), .DATA_W(DATA_W), .ID_W(4), .LEN_W(LEN_W)
    ) u_arb (
        .clk(clk), .rst(rst),
        .s_arvalid(s_arvalid), .s_arready(s_arready), .s_araddr(s_araddr),
        .s_arlen(s_arlen), .s_arsize('0), .s_arburst({NUM_M{2'b01}}),
        .s_rvalid(s_rvalid), .s_rready(s_rready), .s_rdata(s_rdata),
        .s_rresp(s_rresp), .s_rlast(s_rlast),
        .s_awvalid(s_awvalid), .s_awready(s_awready), .s_awaddr(s_awaddr),
        .s_awlen(s_awlen), .s_awsize('0), .s_awburst({NUM_M{2'b01}}),
        .s_wvalid(s_wvalid), .s_wready(s_wready), .s_wdata(s_wdata),
        .s_wstrb(s_wstrb), .s_wlast(s_wlast),
        .s_bvalid(s_bvalid), .s_bready(s_bready), .s_bresp(s_bresp),
        .m_arvalid(m_arvalid), .m_arready(m_arready), .m_araddr(m_araddr),
        .m_arlen(m_arlen), .m_arsize(m_arsize), .m_arburst(m_arburst), .m_arid(m_arid),
        .m_rvalid(m_rvalid), .m_rready(m_rready), .m_rdata(m_rdata),
        .m_rresp(m_rresp), .m_rlast(m_rlast),
        .m_awvalid(m_awvalid), .m_awready(m_awready), .m_awaddr(m_awaddr),
        .m_awlen(m_awlen), .m_awsize(m_awsize), .m_awburst(m_awburst), .m_awid(m_awid),
        .m_wvalid(m_wvalid), .m_wready(m_wready), .m_wdata(m_wdata),
        .m_wstrb(m_wstrb), .m_wlast(m_wlast),
        .m_bvalid(m_bvalid), .m_bready(m_bready), .m_bresp(m_bresp)
    );

    axi_ram_model #(.DEPTH_WORDS(DEPTH_WORDS)) u_ram (
        .clk(clk), .rst(rst),
        .m_arvalid(m_arvalid), .m_arready(m_arready), .m_araddr(m_araddr),
        .m_arlen(m_arlen),
        .m_rvalid(m_rvalid), .m_rready(m_rready), .m_rdata(m_rdata),
        .m_rresp(m_rresp), .m_rlast(m_rlast),
        .m_awvalid(m_awvalid), .m_awready(m_awready), .m_awaddr(m_awaddr),
        .m_awlen(m_awlen),
        .m_wvalid(m_wvalid), .m_wready(m_wready), .m_wdata(m_wdata),
        .m_wstrb(m_wstrb), .m_wlast(m_wlast),
        .m_bvalid(m_bvalid), .m_bready(m_bready), .m_bresp(m_bresp),
        .host_we(ram_we), .host_addr(ram_addr),
        .host_wdata(ram_wdata), .host_rdata(ram_rdata),
        .stat_ar_cnt(stat_ar_cnt), .stat_aw_cnt(stat_aw_cnt)
    );

    /* verilator lint_off UNUSEDSIGNAL */
    logic [NUM_M-1:0][1:0] rresp_unused, bresp_unused;
    assign rresp_unused = s_rresp;
    assign bresp_unused = s_bresp;
    /* verilator lint_on UNUSEDSIGNAL */

endmodule
