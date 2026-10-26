`include "defines.vh"

// ============================================================================
//  global_mem_wrap —— **仿真用**的接线:global_mem + axi_ram_model
//
//  global_mem 的 AXI4 主口必须有从机才谈得上验证。这里挂一块 RTL 内存模型
//  (tb/harness/rtl/axi_ram_model.sv), 让用例只通过 SM 侧的行读写口观察行为;
//  内存内容也能经 SM 侧的读回来自检, 不需要 Python 侧再维护第二份内存。
//
//  `ram_word_*` 是给用例的旁路口:直接按字地址读/写内存模型, 用于铺设初始
//  数据与比对写结果(等价于驱动外部 DDR 的 host)。
// ============================================================================

module global_mem_wrap #(
    parameter int RAM_WORDS = 1 << 12
) (
    input  wire                     clk          ,
    input  wire                     rst          ,

    // SM 侧:行读
    input  wire                     rd_valid     ,
    output wire                     rd_ready     ,
    input  wire [26:0]              rd_line      ,
    input  wire [7:0]               rd_wen       ,
    output wire                     rsp_valid    ,
    output wire [26:0]              rsp_line     ,
    output wire [7:0][31:0]         rsp_data     ,
    output wire                     rsp_err      ,

    // SM 侧:行写
    input  wire                     wr_valid     ,
    output wire                     wr_ready     ,
    input  wire [26:0]              wr_line      ,
    input  wire [7:0]               wr_wen       ,
    input  wire [7:0][31:0]         wr_wdata     ,

    // 冲刷
    input  wire                     flush_req    ,
    output wire                     flush_done   ,
    output wire                     busy         ,
    output wire                     wr_err       ,

    // 内存模型旁路口(host 视角)
    input  wire                     ram_we       ,
    input  wire [31:0]              ram_addr     ,
    input  wire [31:0]              ram_wdata    ,
    output wire [31:0]              ram_rdata    ,

    // 事务计数(给用例断言"这条行读到底发了几拍 AXI 事务")
    output wire [31:0]              stat_ar_cnt  ,
    output wire [31:0]              stat_aw_cnt
);

    global_mem u_global_mem (
        .clk        (clk        ),
        .rst        (rst        ),
        .rd_valid   (rd_valid   ),
        .rd_ready   (rd_ready   ),
        .rd_line    (rd_line    ),
        .rd_wen     (rd_wen     ),
        .rsp_valid  (rsp_valid  ),
        .rsp_line   (rsp_line   ),
        .rsp_data   (rsp_data   ),
        .rsp_err    (rsp_err    ),
        .wr_valid   (wr_valid   ),
        .wr_ready   (wr_ready   ),
        .wr_line    (wr_line    ),
        .wr_wen     (wr_wen     ),
        .wr_wdata   (wr_wdata   ),
        .flush_req  (flush_req  ),
        .flush_done (flush_done ),
        .busy       (busy       ),
        .wr_err     (wr_err     ),
        .m_arvalid  (gm_arvalid ),
        .m_arready  (gm_arready ),
        .m_araddr   (gm_araddr  ),
        .m_arlen    (gm_arlen   ),
        .m_arsize   (gm_arsize  ),
        .m_arburst  (gm_arburst ),
        .m_arid     (gm_arid    ),
        .m_rvalid   (gm_rvalid  ),
        .m_rready   (gm_rready  ),
        .m_rdata    (gm_rdata   ),
        .m_rresp    (gm_rresp   ),
        .m_rlast    (gm_rlast   ),
        .m_rid      (4'd0       ),
        .m_awvalid  (gm_awvalid ),
        .m_awready  (gm_awready ),
        .m_awaddr   (gm_awaddr  ),
        .m_awlen    (gm_awlen   ),
        .m_awsize   (gm_awsize  ),
        .m_awburst  (gm_awburst ),
        .m_awid     (gm_awid    ),
        .m_wvalid   (gm_wvalid  ),
        .m_wready   (gm_wready  ),
        .m_wdata    (gm_wdata   ),
        .m_wstrb    (gm_wstrb   ),
        .m_wlast    (gm_wlast   ),
        .m_bvalid   (gm_bvalid  ),
        .m_bready   (gm_bready  ),
        .m_bresp    (gm_bresp   ),
        .m_bid      (4'd0       )
    );

    /* verilator lint_off UNUSEDSIGNAL */
    wire         gm_arvalid, gm_arready;
    wire [31:0]  gm_araddr;
    wire [7:0]   gm_arlen;
    wire [2:0]   gm_arsize;      // 内存模型只支持 4B, 不检查
    wire [1:0]   gm_arburst;     // 同上
    wire [3:0]   gm_arid;        // 单主, 不检查 id
    /* verilator lint_on UNUSEDSIGNAL */
    wire         gm_rvalid, gm_rready, gm_rlast;
    wire [31:0]  gm_rdata;
    wire [1:0]   gm_rresp;
    wire         gm_awvalid, gm_awready, gm_wvalid, gm_wready, gm_wlast;
    wire [31:0]  gm_awaddr, gm_wdata;
    wire [7:0]   gm_awlen;
    /* verilator lint_off UNUSEDSIGNAL */
    wire [2:0]   gm_awsize;
    wire [1:0]   gm_awburst;
    wire [3:0]   gm_awid;
    /* verilator lint_on UNUSEDSIGNAL */
    wire [3:0]   gm_wstrb;
    wire         gm_bvalid, gm_bready;
    wire [1:0]   gm_bresp;
    axi_ram_model #(
        .DEPTH_WORDS (RAM_WORDS)
    ) u_ram (
        .clk        (clk       ),
        .rst        (rst       ),
        .m_arvalid  (gm_arvalid),
        .m_arready  (gm_arready),
        .m_araddr   (gm_araddr ),
        .m_arlen    (gm_arlen  ),
        .m_rvalid   (gm_rvalid ),
        .m_rdata    (gm_rdata  ),
        .m_rresp    (gm_rresp  ),
        .m_rlast    (gm_rlast  ),
        .m_rready   (gm_rready ),
        .m_awvalid  (gm_awvalid),
        .m_awready  (gm_awready),
        .m_awaddr   (gm_awaddr ),
        .m_awlen    (gm_awlen  ),
        .m_wvalid   (gm_wvalid ),
        .m_wready   (gm_wready ),
        .m_wdata    (gm_wdata  ),
        .m_wstrb    (gm_wstrb  ),
        .m_wlast    (gm_wlast  ),
        .m_bvalid   (gm_bvalid ),
        .m_bresp    (gm_bresp  ),
        .m_bready   (gm_bready ),
        .stat_ar_cnt(stat_ar_cnt),
        .stat_aw_cnt(stat_aw_cnt),
        .host_we  (ram_we      ),
        .host_addr(ram_addr    ),
        .host_wdata(ram_wdata  ),
        .host_rdata(ram_rdata  )
    );

endmodule
