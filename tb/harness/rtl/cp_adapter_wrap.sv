`include "defines.vh"

// 仿真胶水:cp_mem_adapter + axi_ram_model
module cp_adapter_wrap #(
    parameter int DEPTH_WORDS = 1 << 14
) (
    input  wire        clk,
    input  wire        rst,

    input  wire        rd_req,
    input  wire [31:0] rd_addr,
    input  wire        rd_line,
    output wire        rd_ack,
    output wire        rd_rsp,
    output wire [16*32-1:0] rd_data,
    output wire        rd_err,

    input  wire        wr_req,
    input  wire [31:0] wr_addr,
    input  wire [31:0] wr_data,
    input  wire [3:0]  wr_strb,
    output wire        wr_ack,
    output wire        wr_rsp,
    output wire        wr_err,

    input  wire        ram_we,
    input  wire [31:0] ram_addr,
    input  wire [31:0] ram_wdata,
    output wire [31:0] ram_rdata,
    output wire [31:0] stat_ar_cnt,
    output wire [31:0] stat_aw_cnt
);

    logic m_arvalid, m_arready, m_rvalid, m_rready, m_rlast;
    logic [31:0] m_araddr, m_rdata;
    logic [7:0]  m_arlen;
    logic [2:0]  m_arsize;
    logic [1:0]  m_arburst, m_rresp;

    logic m_awvalid, m_awready, m_wvalid, m_wready, m_wlast, m_bvalid, m_bready;
    logic [31:0] m_awaddr, m_wdata;
    logic [7:0]  m_awlen;
    logic [3:0]  m_wstrb;
    logic [2:0]  m_awsize;
    logic [1:0]  m_awburst, m_bresp;

    cp_mem_adapter u_ad (
        .clk(clk), .rst(rst),
        .rd_req(rd_req), .rd_addr(rd_addr), .rd_line(rd_line),
        .rd_ack(rd_ack), .rd_rsp(rd_rsp), .rd_data(rd_data), .rd_err(rd_err),
        .wr_req(wr_req), .wr_addr(wr_addr), .wr_data(wr_data), .wr_strb(wr_strb),
        .wr_ack(wr_ack), .wr_rsp(wr_rsp), .wr_err(wr_err),
        .m_arvalid(m_arvalid), .m_arready(m_arready), .m_araddr(m_araddr),
        .m_arlen(m_arlen), .m_arsize(m_arsize), .m_arburst(m_arburst),
        .m_rvalid(m_rvalid), .m_rready(m_rready), .m_rdata(m_rdata),
        .m_rresp(m_rresp), .m_rlast(m_rlast),
        .m_awvalid(m_awvalid), .m_awready(m_awready), .m_awaddr(m_awaddr),
        .m_awlen(m_awlen), .m_awsize(m_awsize), .m_awburst(m_awburst),
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
        .m_wstrb('1), .m_wlast(m_wlast),
        .m_bvalid(m_bvalid), .m_bready(m_bready), .m_bresp(m_bresp),
        .host_we(ram_we), .host_addr(ram_addr),
        .host_wdata(ram_wdata), .host_rdata(ram_rdata),
        .stat_ar_cnt(stat_ar_cnt), .stat_aw_cnt(stat_aw_cnt)
    );

    // m_arsize/m_awsize/m_arburst/m_awburst/m_wstrb 在 DDR 模型里不使用
    // (模型固定 4B 递增、写按整字落盘), 这里只是把它们引出来避免悬空端口。
    /* verilator lint_off UNUSEDSIGNAL */
    logic [2:0] u1 = m_arsize | m_awsize;
    logic [1:0] u2 = m_arburst | m_awburst;
    logic [3:0] u4 = m_wstrb;
    /* verilator lint_on UNUSEDSIGNAL */

endmodule
