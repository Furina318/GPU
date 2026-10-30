`include "defines.vh"

// 按区搬进局部空间
function automatic logic [31:0] loc_addr(input logic [31:0] a);
    if (a >= `DDR_BASE)                       loc_addr = 32'h0100_0000 + (a - `DDR_BASE);
    else if (a >= 32'h1000_0000)              loc_addr = 32'h0010_0000 + (a - 32'h1000_0000);
    else if (a >= 32'h0300_0000)              loc_addr = 32'h0002_0000 + (a - 32'h0300_0000);
    else if (a >= `CONST_BASE)                loc_addr = 32'h0001_0000 + (a - `CONST_BASE);
    else if (a >= `CMD_QUEUE_BASE)            loc_addr = 32'h0000_0000 + (a - `CMD_QUEUE_BASE);
    else                                      loc_addr = 32'h01F0_0000 + {12'h0, a[19:0]};
endfunction

module gpu_system #(
    parameter int DDR_WORDS = 1 << 23  
) (
    input  wire clk,
    input  wire rst,

    // AXI4-Lite(CP 的 MMIO 窗口)
    input  wire         h_awvalid ,
    output logic        h_awready ,
    input  wire  [31:0] h_awaddr  ,
    input  wire         h_wvalid  ,
    output logic        h_wready  ,
    input  wire  [31:0] h_wdata   ,
    input  wire  [ 3:0] h_wstrb   ,
    output logic        h_bvalid  ,
    input  wire         h_bready  ,
    output logic [ 1:0] h_bresp   ,
    input  wire         h_arvalid ,
    output logic        h_arready ,
    input  wire  [31:0] h_araddr  ,
    output logic        h_rvalid  ,
    input  wire         h_rready  ,
    output logic [31:0] h_rdata   ,
    output logic [ 1:0] h_rresp   ,

    // DDR 旁路
    input  wire         ram_we    ,
    input  wire  [31:0] ram_addr  ,
    input  wire  [31:0] ram_wdata ,
    output logic [31:0] ram_rdata ,

    output logic        irq       ,
    output logic        sm0_done  ,
    output logic        sm1_done  ,
    output logic [ 3:0] sm0_error ,
    output logic [ 3:0] sm1_error ,
    // ---- 诊断探针(仿真专用): 内存子系统的关键状态位 ----
    //   [0] sched.gm_busy  [1] sched.fl_busy  [2] sched.sm_busy  [3] sched.gm_is_wr
    //   [4] arb.r_busy     [5] arb.w_busy     [6] gm0.rsp_vld_q  [7] gm0.wcb_empty
    //   [8] gm0.wcb_full   [9] gm0.flush_seen [10] cp_sm_err_clr[0] [11] sm_core_busy[0]
    //   [13:12] sched.gm_owner  [15:14] gm0.rst_r
    output logic [15:0] dbg_mem_ctl ,

    // 统计
    output logic [31:0] ctr_lines,          // 内存子系统发出的行事务数
    output logic [31:0] ctr_mmio,           // 内核访问 MMIO 的次数(应为 0)
    output logic [31:0] ctr_unmapped,       // 未映射地址次数(应为 0)
    output logic [31:0] ctr_shared_extra,   // 共享内存 bank 冲突的额外拍数
    output logic [31:0] ctr_ar,             // 仲裁器主口上的 AR 事务数
    output logic [31:0] ctr_aw              // 仲裁器主口上的 AW 事务数
);

    //  CP
    logic        cp_mem_rd_req, cp_mem_rd_line, cp_mem_rd_ack, cp_mem_rd_rsp, cp_mem_rd_err;
    logic [31:0] cp_mem_rd_addr;
    logic [16*32-1:0] cp_mem_rd_data;
    logic        cp_mem_wr_req, cp_mem_wr_ack, cp_mem_wr_rsp, cp_mem_wr_err;
    logic [31:0] cp_mem_wr_addr;
    logic [31:0] cp_mem_wr_data;
    logic [3:0]  cp_mem_wr_strb;

    logic [1:0]  cp_launch_valid;
    logic [31:0] cp_launch_pc;
    logic [3:0]  cp_launch_warps, cp_launch_flags;
    logic [1:0]  cp_sm_done;
    logic [3:0]  cp_sm0_error, cp_sm1_error;
    logic [31:0] cp_sm0_fault_pc, cp_sm1_fault_pc;
    logic [1:0]  cp_sm_err_clr;

    logic [1:0]  cp_sram_load_en, cp_sram_load_last, cp_sram_load_done, cp_sram_err_ovf;
    logic [12:0] cp_sram_load_addr;
    logic [31:0] cp_sram_load_data;

    logic        cp_eng_valid, cp_eng_wvalid, cp_eng_wlast;
    logic [1:0]  cp_eng_engine, cp_eng_err_clr;
    logic [7:0]  cp_eng_opcode;
    logic [15:0] cp_eng_len;
    logic [31:0] cp_eng_wdata;

    cp u_cp (
        .clk(clk), 
        .rst(rst),

        .s_awvalid    (h_awvalid      ), 
        .s_awready    (h_awready      ), 
        .s_awaddr     (h_awaddr       ),
        .s_wvalid     (h_wvalid       ), 
        .s_wready     (h_wready       ), 
        .s_wdata      (h_wdata        ), 
        .s_wstrb      (h_wstrb        ),
        .s_bvalid     (h_bvalid       ), 
        .s_bready     (h_bready       ), 
        .s_bresp      (h_bresp        ),
        .s_arvalid    (h_arvalid      ), 
        .s_arready    (h_arready      ), 
        .s_araddr     (h_araddr       ),
        .s_rvalid     (h_rvalid       ), 
        .s_rready     (h_rready       ), 
        .s_rdata      (h_rdata        ), 
        .s_rresp      (h_rresp        ),
        .irq          (irq            ),

        .mem_rd_req   (cp_mem_rd_req  ), 
        .mem_rd_addr  (cp_mem_rd_addr ),
        .mem_rd_line  (cp_mem_rd_line ),
        .mem_rd_ack   (cp_mem_rd_ack  ), 
        .mem_rd_rsp   (cp_mem_rd_rsp  ),
        .mem_rd_data  (cp_mem_rd_data ),
        .mem_rd_err   (cp_mem_rd_err  ),
        .mem_wr_req   (cp_mem_wr_req  ),
        .mem_wr_addr  (cp_mem_wr_addr ),
        .mem_wr_data  (cp_mem_wr_data ), 
        .mem_wr_strb  (cp_mem_wr_strb ),
        .mem_wr_ack   (cp_mem_wr_ack  ), 
        .mem_wr_rsp   (cp_mem_wr_rsp  ), 
        .mem_wr_err   (cp_mem_wr_err  ),

        .sm_launch_valid (cp_launch_valid  ), 
        .sm_launch_pc    (cp_launch_pc     ),
        .sm_launch_warps (cp_launch_warps  ), 
        .sm_launch_flags (cp_launch_flags  ),
        .sm_done         (cp_sm_done       ), 
        .sm0_error       (cp_sm0_error     ), 
        .sm1_error       (cp_sm1_error     ),
        .sm0_fault_pc    (cp_sm0_fault_pc  ), 
        .sm1_fault_pc    (cp_sm1_fault_pc  ),
        .sm_err_clr      (cp_sm_err_clr    ),

        .sram_load_en   (cp_sram_load_en   ), 
        .sram_load_addr (cp_sram_load_addr ),
        .sram_load_data (cp_sram_load_data ), 
        .sram_load_last (cp_sram_load_last ),
        .sram_load_done (cp_sram_load_done ), 
        .sram_err_ovf   (cp_sram_err_ovf   ),

        .eng_valid  (cp_eng_valid  ), 
        .eng_engine (cp_eng_engine ), 
        .eng_opcode (cp_eng_opcode ),
        .eng_len    (cp_eng_len    ), 
        .eng_wvalid (cp_eng_wvalid ), 
        .eng_wdata  (cp_eng_wdata  ),
        .eng_wlast  (cp_eng_wlast  ), 

        `ZERO_PIN(eng_ready   ),
        `ZERO_PIN(gemm_status ),
        `ZERO_PIN(gemm_error  ),
        `ZERO_PIN(twod_status ),
        `ZERO_PIN(twod_error  ),
        .eng_err_clr(cp_eng_err_clr)
    );

    assign dbg_mem_ctl = {2'(u_gm0.rst_r), 2'(u_core0.u_sched.gm_owner),
                          sm_core_busy[0], cp_sm_err_clr[0],
                          u_gm0.flush_seen, u_gm0.wcb_full, u_gm0.wcb_empty,
                          u_gm0.rsp_vld_q,
                          u_arb.w_busy, u_arb.r_busy,
                          u_core0.u_sched.gm_is_wr, u_core0.u_sched.sm_busy,
                          u_core0.u_sched.fl_busy, u_core0.u_sched.gm_busy};

    /* verilator lint_off UNUSEDSIGNAL */
    logic unused_eng = cp_eng_valid | cp_eng_wvalid | cp_eng_wlast | (|cp_eng_engine) |
                       (|cp_eng_opcode) | (|cp_eng_len) | (|cp_eng_wdata) | (|cp_eng_err_clr);
    /* verilator lint_on UNUSEDSIGNAL */

    //  两个 SM
    logic [ 1:0] sm_grd_valid, sm_grd_ready, sm_grsp_valid, sm_grsp_err;
    logic [ 1:0] sm_gwr_valid, sm_gwr_ready, sm_gwr_err, sm_flush_req, sm_flush_done;
    logic [ 1:0] sm_core_busy;    // core 的 mem_busy(LSU 调度器在忙)
    logic [ 1:0] sm_gm_busy;      // global_mem 的 busy(读/写 FSM 在忙)
    logic [26:0] sm_grd_line [2], sm_gwr_line [2], sm_grsp_line [2];
    logic [ 7:0] sm_grd_wen [2], sm_gwr_wen [2];
    logic [7:0][31:0] sm_grsp_data [2], sm_gwr_data [2];

    core u_core0 (
        .clk            (clk                  ), 
        .rst            (rst                  ),
        .launch         (cp_launch_valid[0]   ), 
        .launch_pc      (cp_launch_pc         ),
        .launch_warps   (cp_launch_warps      ), 
        .launch_flags   (cp_launch_flags      ),
        .warp_base      (3'd0                 ),
        .sram_load_en   (cp_sram_load_en[0]   ), 
        .sram_load_addr (cp_sram_load_addr    ),
        .sram_load_data (cp_sram_load_data    ), 
        .sram_load_last (cp_sram_load_last[0] ),
        .sram_load_done (cp_sram_load_done[0] ), 
        .sram_load_ovf  (cp_sram_err_ovf[0]   ),
        .sm_done        (cp_sm_done[0]        ), 
        .sm_error       (cp_sm0_error         ), 
        .sm_fault_pc    (cp_sm0_fault_pc      ),
        .gmem_rd_valid  (sm_grd_valid[0]      ), 
        .gmem_rd_ready  (sm_grd_ready[0]      ),
        .gmem_rd_line   (sm_grd_line[0]       ), 
        .gmem_rd_wen    (sm_grd_wen[0]        ),
        .gmem_rsp_valid (sm_grsp_valid[0]     ), 
        .gmem_rsp_line  (sm_grsp_line[0]      ),
        .gmem_rsp_data  (sm_grsp_data[0]      ), 
        .gmem_rsp_err   (sm_grsp_err[0]       ),
        .gmem_wr_valid  (sm_gwr_valid[0]      ), 
        .gmem_wr_ready  (sm_gwr_ready[0]      ),
        .gmem_wr_line   (sm_gwr_line[0]       ), 
        .gmem_wr_wen    (sm_gwr_wen[0]        ),
        .gmem_wr_wdata  (sm_gwr_data[0]       ), 
        .gmem_wr_err    (sm_gwr_err[0]        ),
        .flush_req      (sm_flush_req[0]      ), 
        .flush_done     (sm_flush_done[0]     ),
        .mem_busy       (sm_core_busy[0]      )
    );

    core u_core1 (
        .clk            (clk                  ), 
        .rst            (rst                  ),
        .launch         (cp_launch_valid[1]   ), 
        .launch_pc      (cp_launch_pc         ),
        .launch_warps   (cp_launch_warps      ), 
        .launch_flags   (cp_launch_flags      ),
        .warp_base      (3'd4                 ),
        .sram_load_en   (cp_sram_load_en[1]   ), 
        .sram_load_addr (cp_sram_load_addr    ),
        .sram_load_data (cp_sram_load_data    ), 
        .sram_load_last (cp_sram_load_last[1] ),
        .sram_load_done (cp_sram_load_done[1] ), 
        .sram_load_ovf  (cp_sram_err_ovf[1]   ),
        .sm_done        (cp_sm_done[1]        ), 
        .sm_error       (cp_sm0_error         ), 
        .sm_fault_pc    (cp_sm0_fault_pc      ),
        .gmem_rd_valid  (sm_grd_valid[1]      ), 
        .gmem_rd_ready  (sm_grd_ready[1]      ),
        .gmem_rd_line   (sm_grd_line[1]       ), 
        .gmem_rd_wen    (sm_grd_wen[1]        ),
        .gmem_rsp_valid (sm_grsp_valid[1]     ), 
        .gmem_rsp_line  (sm_grsp_line[1]      ),
        .gmem_rsp_data  (sm_grsp_data[1]      ), 
        .gmem_rsp_err   (sm_grsp_err[1]       ),
        .gmem_wr_valid  (sm_gwr_valid[1]      ), 
        .gmem_wr_ready  (sm_gwr_ready[1]      ),
        .gmem_wr_line   (sm_gwr_line[1]       ), 
        .gmem_wr_wen    (sm_gwr_wen[1]        ),
        .gmem_wr_wdata  (sm_gwr_data[1]       ), 
        .gmem_wr_err    (sm_gwr_err[1]        ),
        .flush_req      (sm_flush_req[1]      ), 
        .flush_done     (sm_flush_done[1]     ),
        .mem_busy       (sm_core_busy[1]      )
    );

    assign sm0_done  = cp_sm_done[0];
    assign sm1_done  = cp_sm_done[1];
    assign sm0_error = cp_sm0_error;
    assign sm1_error = cp_sm1_error;

    //  仲裁器 + DDR
    logic        m_arvalid, m_arready, m_rvalid, m_rready, m_rlast;
    logic [31:0] m_araddr, m_rdata;
    logic [ 7:0] m_arlen;
    logic [ 2:0] m_arsize;
    logic [ 1:0] m_arburst, m_rresp;
    logic        m_awvalid, m_awready, m_wvalid, m_wready, m_wlast, m_bvalid, m_bready;
    logic [31:0] m_awaddr, m_wdata;
    logic [ 7:0] m_awlen;
    logic [ 2:0] m_awsize;
    logic [ 1:0] m_awburst, m_bresp;
    logic [ 3:0] m_wstrb;
    logic [ 3:0] gm0_arid, gm0_awid, gm0_bid, gm1_arid, gm1_awid, gm1_bid;
    assign {gm1_bid, gm0_bid} = 8'h00;   // global_mem 是单主口, 写响应 id 不参与路由
    logic [ 3:0] arb_arid, arb_awid;
    /* verilator lint_off UNUSEDSIGNAL */   // 下面这些口只求"接到有名字的信号上"
    logic        unused_eq = (|gm0_arid) | (|gm0_awid) | (|gm0_bid) |
                             (|gm1_arid) | (|gm1_awid) | (|gm1_bid) |
                             (|arb_arid) | (|arb_awid) | (|m_arsize) | (|m_awsize) |
                             (|m_arburst) | (|m_awburst) |
                             (|cp_sm_err_clr) | (|sm_core_busy) | (|sm_gm_busy);
    /* verilator lint_on UNUSEDSIGNAL */

    // 0 = CP, 1 = SM0, 2 = SM1
    logic [2:0]        a_arvalid, a_arready, a_rvalid, a_rready, a_rlast;
    logic [2:0]        a_awvalid, a_awready, a_wvalid, a_wready, a_wlast;
    logic [2:0]        a_bvalid, a_bready;
    logic [2:0][31:0]  a_araddr, a_awaddr, a_rdata, a_wdata;
    logic [2:0][7:0]   a_arlen, a_awlen;
    logic [2:0][2:0]   a_arsize, a_awsize;
    logic [2:0][1:0]   a_arburst, a_awburst, a_rresp, a_bresp;
    logic [2:0][3:0]   a_wstrb;

    // CP 适配器 → 主 0
    cp_mem_adapter u_cp_ad (
        .clk      (clk           ), 
        .rst      (rst           ),
        .rd_req   (cp_mem_rd_req ), 
        .rd_addr  (cp_mem_rd_addr), 
        .rd_line  (cp_mem_rd_line),
        .rd_ack   (cp_mem_rd_ack ), 
        .rd_rsp   (cp_mem_rd_rsp ),
        .rd_data  (cp_mem_rd_data), 
        .rd_err   (cp_mem_rd_err ),
        .wr_req   (cp_mem_wr_req ), 
        .wr_addr  (cp_mem_wr_addr),
        .wr_data  (cp_mem_wr_data), 
        .wr_strb  (cp_mem_wr_strb),
        .wr_ack   (cp_mem_wr_ack ), 
        .wr_rsp   (cp_mem_wr_rsp ), 
        .wr_err   (cp_mem_wr_err ),
        .m_arvalid(a_arvalid[0]  ), 
        .m_arready(a_arready[0]  ), 
        .m_araddr (a_araddr[0]   ),
        .m_arlen  (a_arlen[0]    ), 
        .m_arsize (a_arsize[0]   ), 
        .m_arburst(a_arburst[0]  ),
        .m_rvalid (a_rvalid[0]   ), 
        .m_rready (a_rready[0]   ), 
        .m_rdata  (a_rdata[0]    ),
        .m_rresp  (a_rresp[0]    ), 
        .m_rlast  (a_rlast[0]    ),
        .m_awvalid(a_awvalid[0]  ), 
        .m_awready(a_awready[0]  ), 
        .m_awaddr (a_awaddr[0]   ),
        .m_awlen  (a_awlen[0]    ), 
        .m_awsize (a_awsize[0]   ), 
        .m_awburst(a_awburst[0]  ),
        .m_wvalid (a_wvalid[0]   ), 
        .m_wready (a_wready[0]   ), 
        .m_wdata  (a_wdata[0]    ),
        .m_wstrb  (a_wstrb[0]    ), 
        .m_wlast  (a_wlast[0]    ),
        .m_bvalid (a_bvalid[0]   ), 
        .m_bready (a_bready[0]   ), 
        .m_bresp  (a_bresp[0]    )
    );

    // SM 全局通路 → 主 1/2
    global_mem u_gm0 (
        .clk       (clk             ), 
        .rst       (rst             ),
        .rd_valid  (sm_grd_valid[0] ), 
        .rd_ready  (sm_grd_ready[0] ),
        .rd_line   (sm_grd_line[0]  ), 
        .rd_wen    (sm_grd_wen[0]   ),
        .rsp_valid (sm_grsp_valid[0]), 
        .rsp_line  (sm_grsp_line[0] ),
        .rsp_data  (sm_grsp_data[0] ), 
        .rsp_err   (sm_grsp_err[0]  ),
        .wr_valid  (sm_gwr_valid[0] ), 
        .wr_ready  (sm_gwr_ready[0] ),
        .wr_line   (sm_gwr_line[0]  ), 
        .wr_wen    (sm_gwr_wen[0]   ), 
        .wr_wdata  (sm_gwr_data[0]  ),
        .flush_req (sm_flush_req[0] ), 
        .flush_done(sm_flush_done[0]),
        .busy      (sm_gm_busy[0]   ), 
        .wr_err    (sm_gwr_err[0]   ),
        .m_arvalid (a_arvalid[1]    ), 
        .m_arready (a_arready[1]    ),
        .m_araddr  (a_araddr[1]     ), 
        .m_arlen   (a_arlen[1]      ),
        .m_arsize  (a_arsize[1]     ), 
        .m_arburst (a_arburst[1]    ),
        .m_arid    (gm0_arid        ),
        .m_rvalid  (a_rvalid[1]     ), 
        .m_rready  (a_rready[1]     ),
        .m_rdata   (a_rdata[1]      ), 
        .m_rresp   (a_rresp[1]      ), 
        .m_rlast   (a_rlast[1]      ),
        .m_rid     (4'd0            ),
        .m_awvalid (a_awvalid[1]    ), 
        .m_awready (a_awready[1]    ),
        .m_awaddr  (a_awaddr[1]     ), 
        .m_awlen   (a_awlen[1]      ),
        .m_awsize  (a_awsize[1]     ), 
        .m_awburst (a_awburst[1]    ),
        .m_awid    (gm0_awid        ),
        .m_wvalid  (a_wvalid[1]     ), 
        .m_wready  (a_wready[1]     ),
        .m_wdata   (a_wdata[1]      ), 
        .m_wstrb   (a_wstrb[1]      ), 
        .m_wlast   (a_wlast[1]      ),
        .m_bvalid  (a_bvalid[1]     ), 
        .m_bready  (a_bready[1]     ), 
        .m_bresp   (a_bresp[1]      ),
        .m_bid     (gm0_bid         )
    );

    global_mem u_gm1 (
        .clk       (clk             ), 
        .rst       (rst             ),
        .rd_valid  (sm_grd_valid[1] ), 
        .rd_ready  (sm_grd_ready[1] ),
        .rd_line   (sm_grd_line[1]  ), 
        .rd_wen    (sm_grd_wen[1]   ),
        .rsp_valid (sm_grsp_valid[1]), 
        .rsp_line  (sm_grsp_line[1] ),
        .rsp_data  (sm_grsp_data[1] ), 
        .rsp_err   (sm_grsp_err[1]  ),
        .wr_valid  (sm_gwr_valid[1] ), 
        .wr_ready  (sm_gwr_ready[1] ),
        .wr_line   (sm_gwr_line[1]  ), 
        .wr_wen    (sm_gwr_wen[1]   ), 
        .wr_wdata  (sm_gwr_data[1]  ),
        .flush_req (sm_flush_req[1] ), 
        .flush_done(sm_flush_done[1]),
        .busy      (sm_gm_busy[1]   ), 
        .wr_err    (sm_gwr_err[1]   ),
        .m_arvalid (a_arvalid[2]    ), 
        .m_arready (a_arready[2]    ),
        .m_araddr  (a_araddr[2]     ), 
        .m_arlen   (a_arlen[2]      ),
        .m_arsize  (a_arsize[2]     ), 
        .m_arburst (a_arburst[2]    ),
        .m_arid    (gm1_arid        ),
        .m_rvalid  (a_rvalid[2]     ), 
        .m_rready  (a_rready[2]     ),
        .m_rdata   (a_rdata[2]      ), 
        .m_rresp   (a_rresp[2]      ), 
        .m_rlast   (a_rlast[2]      ),
        .m_rid     (4'd0            ),
        .m_awvalid (a_awvalid[2]    ), 
        .m_awready (a_awready[2]    ),
        .m_awaddr  (a_awaddr[2]     ), 
        .m_awlen   (a_awlen[2]      ),
        .m_awsize  (a_awsize[2]     ), 
        .m_awburst (a_awburst[2]    ),
        .m_awid    (gm1_awid        ),
        .m_wvalid  (a_wvalid[2]     ), 
        .m_wready  (a_wready[2]     ),
        .m_wdata   (a_wdata[2]      ), 
        .m_wstrb   (a_wstrb[2]      ), 
        .m_wlast   (a_wlast[2]      ),
        .m_bvalid  (a_bvalid[2]     ), 
        .m_bready  (a_bready[2]     ), 
        .m_bresp   (a_bresp[2]      ),
        .m_bid     (gm1_bid         )
    );

    axi_arb #(.NUM_M(3), .ADDR_W(32), .DATA_W(32), .ID_W(4), .LEN_W(8)) u_arb (
        .clk(clk), .rst(rst),
        .s_arvalid(a_arvalid), .s_arready(a_arready), .s_araddr(a_araddr),
        .s_arlen(a_arlen), .s_arsize(a_arsize), .s_arburst(a_arburst),
        .s_rvalid(a_rvalid), .s_rready(a_rready), .s_rdata(a_rdata),
        .s_rresp(a_rresp), .s_rlast(a_rlast),
        .s_awvalid(a_awvalid), .s_awready(a_awready), .s_awaddr(a_awaddr),
        .s_awlen(a_awlen), .s_awsize(a_awsize), .s_awburst(a_awburst),
        .s_wvalid(a_wvalid), .s_wready(a_wready), .s_wdata(a_wdata),
        .s_wstrb(a_wstrb), .s_wlast(a_wlast),
        .s_bvalid(a_bvalid), .s_bready(a_bready), .s_bresp(a_bresp),
        .m_arvalid(m_arvalid), .m_arready(m_arready), .m_araddr(m_araddr),
        .m_arlen(m_arlen), .m_arsize(m_arsize), .m_arburst(m_arburst), .m_arid(arb_arid),
        .m_rvalid(m_rvalid), .m_rready(m_rready), .m_rdata(m_rdata),
        .m_rresp(m_rresp), .m_rlast(m_rlast),
        .m_awvalid(m_awvalid), .m_awready(m_awready), .m_awaddr(m_awaddr),
        .m_awlen(m_awlen), .m_awsize(m_awsize), .m_awburst(m_awburst), .m_awid(arb_awid),
        .m_wvalid(m_wvalid), .m_wready(m_wready), .m_wdata(m_wdata),
        .m_wstrb(m_wstrb), .m_wlast(m_wlast),
        .m_bvalid(m_bvalid), .m_bready(m_bready), .m_bresp(m_bresp)
    );

    wire [31:0] ddr_araddr = loc_addr(m_araddr);
    wire [31:0] ddr_awaddr = loc_addr(m_awaddr);
    wire [31:0] ddr_haaddr = loc_addr(ram_addr);

    axi_ram_model #(.DEPTH_WORDS(DDR_WORDS)) u_ddr (
        .clk         (clk        ), 
        .rst         (rst        ),
        .m_arvalid   (m_arvalid  ), 
        .m_arready   (m_arready  ), 
        .m_araddr    (ddr_araddr ),
        .m_arlen     (m_arlen    ),
        .m_rvalid    (m_rvalid   ), 
        .m_rready    (m_rready   ), 
        .m_rdata     (m_rdata    ),
        .m_rresp     (m_rresp    ), 
        .m_rlast     (m_rlast    ),
        .m_awvalid   (m_awvalid  ), 
        .m_awready   (m_awready  ), 
        .m_awaddr    (ddr_awaddr ),
        .m_awlen     (m_awlen    ),
        .m_wvalid    (m_wvalid   ), 
        .m_wready    (m_wready   ), 
        .m_wdata     (m_wdata    ),
        .m_wstrb     (m_wstrb    ), 
        .m_wlast     (m_wlast    ),
        .m_bvalid    (m_bvalid   ), 
        .m_bready    (m_bready   ), 
        .m_bresp     (m_bresp    ),
        .host_we     (ram_we     ), 
        .host_addr   (ddr_haaddr ),
        .host_wdata  (ram_wdata  ), 
        .host_rdata  (ram_rdata  ),
        .stat_ar_cnt (ctr_ar     ), 
        .stat_aw_cnt (ctr_aw     )
    );

    //  性能计数器: 行请求数 = 仲裁器主口上的 AR/AW 握手
    wire ev_line = (m_arvalid && m_arready) || (m_awvalid && m_awready);
    mem_perf u_perf (
        .clk             (clk              ), 
        .rst             (rst              ), 
        .clear           (1'b0             ),
        .ev_line         (ev_line          ), 
        .ev_shared_extra (4'd0             ),
        .ev_mmio         (1'b0             ), 
        .ev_unmapped     (1'b0             ),
        .ctr_lines       (ctr_lines        ), 
        .ctr_shared_extra(ctr_shared_extra ),
        .ctr_mmio        (ctr_mmio         ), 
        .ctr_unmapped    (ctr_unmapped     )
    );

endmodule
